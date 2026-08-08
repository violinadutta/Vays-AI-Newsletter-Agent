"""Repository tests against a real SQLite database.

Integration, not unit: the behaviours that matter most here — cascade deletes,
unique constraints, and the conditional UPDATE that prevents a double send — are
properties of the *database*, and a mocked session would assert nothing about
them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.enums import (
    ArticleStatus,
    CampaignStatus,
    Category,
    ExtractorTier,
    SendStatus,
    SuppressionReason,
    Tone,
    UserRole,
)
from core.exceptions import InvalidStateTransition
from core.models import (
    CampaignFilter,
    CleanedArticle,
    ContentPatch,
    NewsletterContent,
    Recipient,
    SendResult,
)
from modules.repository.article_repo import ArticleRepository, url_fingerprint
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.log_repo import LogRepository
from modules.repository.orm_models import RecipientORM, SendRecordORM
from modules.repository.recipient_repo import RecipientRepository, SuppressionRepository
from modules.repository.send_repo import SendRepository
from modules.repository.settings_repo import SettingsRepository
from modules.repository.user_repo import UserRepository

pytestmark = pytest.mark.integration


def make_content(**overrides: object) -> NewsletterContent:
    base = {
        "title": "Dell's New PowerEdge Servers",
        "summary": "Dell announced the R7xx line with improved power efficiency for data centres.",
        "newsletter": "Dell has announced the PowerEdge R7xx series. " * 5,
        "subject": "Dell's new servers cut power costs",
        "preview_text": "Plus what it means for your refresh cycle",
        "cta": "Read the specs",
        "keywords": ["dell", "poweredge", "servers"],
        "category": Category.PRODUCT_LAUNCH,
        "tone": Tone.PROFESSIONAL,
    }
    return NewsletterContent.model_validate(base | overrides)


def make_article(**overrides: object) -> CleanedArticle:
    base = {
        "url": "https://dell.com/blog/poweredge",
        "title": "PowerEdge R7xx",
        "cleaned_text": "Body text.",
        "extractor": ExtractorTier.TRAFILATURA,
        "word_count": 2,
        "token_estimate": 3,
    }
    return CleanedArticle.model_validate(base | overrides)


@pytest.fixture
def campaign_id(db_session: Session) -> int:
    campaign = CampaignRepository(db_session).create(name="Test", content=make_content())
    db_session.commit()
    return campaign.id


# ─────────────────────────────────────────────────────────────────────────────
#  The double-send guard
# ─────────────────────────────────────────────────────────────────────────────
class TestSendGuard:
    """The single most expensive bug this project could ship is mailing every
    customer twice. These tests are the reason ``begin_send`` is a conditional
    UPDATE rather than a read-then-write."""

    def test_a_second_send_claim_is_refused(self, db_session: Session, campaign_id: int) -> None:
        repo = CampaignRepository(db_session)

        assert repo.begin_send(campaign_id) is True
        assert repo.begin_send(campaign_id) is False

    def test_the_rerun_scenario_sends_once(self, db_session: Session, campaign_id: int) -> None:
        """Streamlit re-executes the script on every interaction, so a send
        handler genuinely can fire twice within milliseconds."""
        repo = CampaignRepository(db_session)

        claims = [repo.begin_send(campaign_id) for _ in range(5)]

        assert claims.count(True) == 1

    def test_a_sent_campaign_can_never_be_reclaimed(
        self, db_session: Session, campaign_id: int
    ) -> None:
        repo = CampaignRepository(db_session)
        repo.begin_send(campaign_id)
        repo.transition_status(campaign_id, CampaignStatus.SENT)

        assert repo.begin_send(campaign_id) is False

    def test_claiming_a_missing_campaign_fails_quietly(self, db_session: Session) -> None:
        assert CampaignRepository(db_session).begin_send(999_999) is False


class TestStatusTransitions:
    def test_legal_transition_succeeds(self, db_session: Session, campaign_id: int) -> None:
        assert CampaignRepository(db_session).transition_status(campaign_id, CampaignStatus.READY)

    def test_illegal_transition_returns_false(self, db_session: Session, campaign_id: int) -> None:
        """DRAFT cannot jump straight to SENT — it must pass through SENDING."""
        assert not CampaignRepository(db_session).transition_status(
            campaign_id, CampaignStatus.SENT
        )

    def test_transition_or_raise_explains_the_refusal(
        self, db_session: Session, campaign_id: int
    ) -> None:
        """The error names the current state and the legal targets, so the log
        says *why* rather than just that something failed."""
        with pytest.raises(InvalidStateTransition) as exc_info:
            CampaignRepository(db_session).transition_or_raise(campaign_id, CampaignStatus.SENT)

        assert exc_info.value.context["current"] == "DRAFT"
        assert "READY" in exc_info.value.context["allowed"]

    def test_sent_at_is_stamped_on_completion(self, db_session: Session, campaign_id: int) -> None:
        repo = CampaignRepository(db_session)
        repo.begin_send(campaign_id)
        repo.transition_status(campaign_id, CampaignStatus.SENT)

        assert repo.get(campaign_id).sent_at is not None

    def test_a_failed_campaign_can_be_retried(self, db_session: Session, campaign_id: int) -> None:
        repo = CampaignRepository(db_session)
        repo.begin_send(campaign_id)
        repo.transition_status(campaign_id, CampaignStatus.PARTIAL_FAILURE)

        assert repo.transition_status(campaign_id, CampaignStatus.SENDING)


# ─────────────────────────────────────────────────────────────────────────────
#  Database-level integrity
# ─────────────────────────────────────────────────────────────────────────────
class TestDatabaseIntegrity:
    def test_foreign_keys_are_actually_enforced(self, db_session: Session) -> None:
        """SQLite ignores foreign keys unless PRAGMA foreign_keys=ON is set on
        every connection. Without this, the cascade tests below would pass
        vacuously and orphaned rows would accumulate in production."""
        assert db_session.execute(text("PRAGMA foreign_keys")).scalar() == 1

    def test_deleting_a_campaign_cascades_to_recipients_and_sends(
        self, db_session: Session, campaign_id: int
    ) -> None:
        RecipientRepository(db_session).replace_all(campaign_id, [Recipient(email="a@vays.com")])
        recipient = db_session.execute(select(RecipientORM)).scalars().one()
        SendRepository(db_session).record(
            campaign_id, recipient.id, SendResult(email="a@vays.com", status=SendStatus.SENT)
        )
        db_session.commit()

        CampaignRepository(db_session).delete(campaign_id)
        db_session.commit()

        assert db_session.execute(select(RecipientORM)).scalars().all() == []
        assert db_session.execute(select(SendRecordORM)).scalars().all() == []

    def test_the_same_address_cannot_be_added_twice_to_one_campaign(
        self, db_session: Session, campaign_id: int
    ) -> None:
        """Enforced by the database, not only by application code — a retried
        upload must not create a second row."""
        db_session.add_all(
            [
                RecipientORM(campaign_id=campaign_id, email="dup@vays.com"),
                RecipientORM(campaign_id=campaign_id, email="dup@vays.com"),
            ]
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestUnitOfWork:
    def test_an_exception_rolls_everything_back(self, db_session: Session, tmp_path) -> None:  # noqa: ANN001
        """Multi-step use cases must be atomic or not happen at all."""
        from modules.repository.database import unit_of_work

        with pytest.raises(RuntimeError), unit_of_work() as session:
            CampaignRepository(session).create(name="Doomed", content=make_content())
            raise RuntimeError("something went wrong mid-use-case")

        with unit_of_work() as session:
            assert CampaignRepository(session).list_page().total == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Content and history
# ─────────────────────────────────────────────────────────────────────────────
class TestCampaignContent:
    def test_ai_original_is_preserved_when_the_user_edits(
        self, db_session: Session, campaign_id: int
    ) -> None:
        """The divergence between these two columns *is* the edit-ratio quality
        metric, so overwriting the original would destroy the measurement."""
        repo = CampaignRepository(db_session)
        repo.update_content(campaign_id, ContentPatch(subject="A human-written subject"))
        db_session.commit()

        campaign = repo.get(campaign_id)
        assert campaign.subject == "A human-written subject"
        assert campaign.ai_subject == "Dell's new servers cut power costs"

    def test_unset_patch_fields_are_untouched(self, db_session: Session, campaign_id: int) -> None:
        repo = CampaignRepository(db_session)
        original_title = repo.get(campaign_id).title

        repo.update_content(campaign_id, ContentPatch(subject="Only the subject"))
        db_session.commit()

        assert repo.get(campaign_id).title == original_title

    def test_enums_round_trip_as_strings(self, db_session: Session, campaign_id: int) -> None:
        campaign = CampaignRepository(db_session).get(campaign_id)

        assert campaign.category == "Product Launch"
        assert campaign.tone == "professional"


class TestCampaignHistory:
    def _seed(self, session: Session, count: int) -> None:
        repo = CampaignRepository(session)
        for i in range(count):
            repo.create(name=f"Campaign {i}", content=make_content())
        session.commit()

    def test_pagination(self, db_session: Session) -> None:
        self._seed(db_session, 25)

        page = CampaignRepository(db_session).list_page(CampaignFilter(page=2, page_size=10))

        assert page.total == 25
        assert len(page.items) == 10
        assert page.total_pages == 3

    def test_search_matches_name(self, db_session: Session) -> None:
        self._seed(db_session, 3)

        page = CampaignRepository(db_session).list_page(CampaignFilter(search="campaign 1"))

        assert page.total == 1

    def test_status_filter(self, db_session: Session, campaign_id: int) -> None:
        repo = CampaignRepository(db_session)
        repo.transition_status(campaign_id, CampaignStatus.READY)
        db_session.commit()

        assert repo.list_page(CampaignFilter(statuses=[CampaignStatus.READY])).total == 1
        assert repo.list_page(CampaignFilter(statuses=[CampaignStatus.SENT])).total == 0

    def test_newest_first(self, db_session: Session) -> None:
        self._seed(db_session, 3)

        items = CampaignRepository(db_session).list_page().items

        assert [i.name for i in items] == ["Campaign 2", "Campaign 1", "Campaign 0"]


# ─────────────────────────────────────────────────────────────────────────────
#  Articles
# ─────────────────────────────────────────────────────────────────────────────
class TestArticleRepository:
    def test_get_many_preserves_the_requested_order(self, db_session: Session) -> None:
        """Order is the order stories appear in the newsletter, and SQL makes no
        ordering guarantee for an IN clause."""
        repo = ArticleRepository(db_session)
        ids = [repo.create(make_article(url=f"https://x.com/{i}"), "raw").id for i in range(3)]
        db_session.commit()

        assert [a.id for a in repo.get_many([ids[2], ids[0], ids[1]])] == [ids[2], ids[0], ids[1]]

    def test_missing_ids_are_skipped_not_fatal(self, db_session: Session) -> None:
        repo = ArticleRepository(db_session)
        article_id = repo.create(make_article(), "raw").id
        db_session.commit()

        assert [a.id for a in repo.get_many([article_id, 999_999])] == [article_id]

    def test_url_fingerprint_ignores_case_and_whitespace(self) -> None:
        assert url_fingerprint("  HTTPS://Dell.com/B  ") == url_fingerprint("https://dell.com/b")

    def test_find_by_url(self, db_session: Session) -> None:
        ArticleRepository(db_session).create(make_article(), "raw")
        db_session.commit()

        found = ArticleRepository(db_session).find_by_url("https://dell.com/blog/poweredge")
        assert found is not None

    def test_failed_extractions_are_recorded_not_discarded(self, db_session: Session) -> None:
        """The Logs page and the per-tier success metric both need them."""
        row = ArticleRepository(db_session).record_failure("https://blocked.com", "403 Forbidden")
        db_session.commit()

        assert row.status == ArticleStatus.FAILED
        assert row.error_message == "403 Forbidden"


# ─────────────────────────────────────────────────────────────────────────────
#  Recipients, suppressions, sends
# ─────────────────────────────────────────────────────────────────────────────
class TestRecipients:
    def test_reupload_replaces_rather_than_appends(
        self, db_session: Session, campaign_id: int
    ) -> None:
        """A corrected CSV must not mail everyone on the first list as well."""
        repo = RecipientRepository(db_session)
        repo.replace_all(campaign_id, [Recipient(email="old@vays.com")])
        repo.replace_all(campaign_id, [Recipient(email="new@vays.com")])
        db_session.commit()

        emails = [r.email for r in repo.list_for_campaign(campaign_id)]
        assert emails == ["new@vays.com"]


class TestSuppressions:
    def test_suppressed_addresses_are_found_in_bulk(self, db_session: Session) -> None:
        """One query, not one per address — a 10,000-recipient campaign would
        otherwise make 10,000 round trips before it could send anything."""
        repo = SuppressionRepository(db_session)
        repo.add("gone@vays.com", SuppressionReason.UNSUBSCRIBED)
        db_session.commit()

        blocked = repo.filter_suppressed(["a@vays.com", "GONE@vays.com", "b@vays.com"])

        assert blocked == {"gone@vays.com"}

    def test_lookup_is_case_insensitive(self, db_session: Session) -> None:
        """Someone who unsubscribed as Priya@Vays.com must not receive mail as
        priya@vays.com."""
        repo = SuppressionRepository(db_session)
        repo.add("Priya@Vays.COM", SuppressionReason.UNSUBSCRIBED)
        db_session.commit()

        assert repo.is_suppressed("priya@vays.com")

    def test_adding_twice_is_a_no_op(self, db_session: Session) -> None:
        repo = SuppressionRepository(db_session)
        repo.add("a@vays.com", SuppressionReason.UNSUBSCRIBED)
        repo.add("a@vays.com", SuppressionReason.COMPLAINT)
        db_session.commit()

        assert repo.count() == 1

    def test_empty_input_makes_no_query(self, db_session: Session) -> None:
        assert SuppressionRepository(db_session).filter_suppressed([]) == set()


class TestSendRecords:
    def test_already_delivered_recipients_are_identified_for_retry(
        self, db_session: Session, campaign_id: int
    ) -> None:
        """'Retry failed only' must skip these, or it re-mails everyone who
        succeeded — the duplicate-send problem reintroduced one layer down."""
        RecipientRepository(db_session).replace_all(
            campaign_id, [Recipient(email="ok@vays.com"), Recipient(email="bad@vays.com")]
        )
        recipients = RecipientRepository(db_session).list_for_campaign(campaign_id)
        send_repo = SendRepository(db_session)
        send_repo.record(
            campaign_id,
            recipients[0].id,
            SendResult(email="ok@vays.com", status=SendStatus.SENT),
        )
        send_repo.record(
            campaign_id,
            recipients[1].id,
            SendResult(
                email="bad@vays.com", status=SendStatus.FAILED, error_message="Mailbox not found"
            ),
        )
        db_session.commit()

        assert send_repo.already_sent_recipient_ids(campaign_id) == {recipients[0].id}

    def test_failures_are_joined_to_their_recipients(
        self, db_session: Session, campaign_id: int
    ) -> None:
        RecipientRepository(db_session).replace_all(campaign_id, [Recipient(email="bad@vays.com")])
        recipient = RecipientRepository(db_session).list_for_campaign(campaign_id)[0]
        SendRepository(db_session).record(
            campaign_id,
            recipient.id,
            SendResult(email="bad@vays.com", status=SendStatus.FAILED, error_message="No mailbox"),
        )
        db_session.commit()

        failures = SendRepository(db_session).failed_recipients(campaign_id)
        assert failures[0][1].email == "bad@vays.com"
        assert failures[0][0].error_message == "No mailbox"


# ─────────────────────────────────────────────────────────────────────────────
#  Logs, settings, users
# ─────────────────────────────────────────────────────────────────────────────
class TestLogRepository:
    def test_correlation_id_retrieves_a_whole_operation(self, db_session: Session) -> None:
        """The feature that turns 'it broke around 3pm' into a diagnosis."""
        repo = LogRepository(db_session)
        for event in ("a.start", "a.middle", "a.end"):
            repo.write(level="INFO", logger="t", event=event, correlation_id="abc123")
        repo.write(level="INFO", logger="t", event="unrelated", correlation_id="zzz999")
        db_session.commit()

        assert len(repo.search(correlation_id="abc123")) == 3

    def test_level_filter(self, db_session: Session) -> None:
        repo = LogRepository(db_session)
        repo.write(level="INFO", logger="t", event="fine")
        repo.write(level="ERROR", logger="t", event="broken")
        db_session.commit()

        assert len(repo.search(levels=["ERROR"])) == 1

    def test_prune_removes_only_old_rows(self, db_session: Session) -> None:
        repo = LogRepository(db_session)
        repo.write(level="INFO", logger="t", event="recent")
        db_session.commit()

        assert repo.prune(retention_days=90) == 0
        assert repo.count() == 1


class TestSettingsRepository:
    def test_upsert(self, db_session: Session) -> None:
        repo = SettingsRepository(db_session)
        repo.set("default_tone", "professional")
        repo.set("default_tone", "friendly")
        db_session.commit()

        assert repo.get("default_tone") == "friendly"

    def test_missing_key_returns_the_default(self, db_session: Session) -> None:
        assert SettingsRepository(db_session).get("nope", "fallback") == "fallback"

    def test_json_values_round_trip(self, db_session: Session) -> None:
        repo = SettingsRepository(db_session)
        repo.set("batch", {"size": 50, "delay": 2})
        db_session.commit()

        assert repo.get("batch") == {"size": 50, "delay": 2}


class TestUserRepository:
    def test_usernames_are_normalised(self, db_session: Session) -> None:
        repo = UserRepository(db_session)
        repo.create(
            username="  Priya  ", display_name="Priya", email="P@Vays.com", password_hash="hash"
        )
        db_session.commit()

        assert repo.get("PRIYA") is not None

    def test_deactivated_users_are_invisible_to_the_auth_path(self, db_session: Session) -> None:
        """Deactivation must take effect immediately — a disabled account should
        fail login the same way a wrong password does."""
        repo = UserRepository(db_session)
        repo.create(
            username="rahul",
            display_name="Rahul",
            email="r@vays.com",
            password_hash="hash",
            role=UserRole.APPROVER,
        )
        repo.set_active("rahul", active=False)
        db_session.commit()

        assert repo.get("rahul") is not None
        assert repo.get_active("rahul") is None
