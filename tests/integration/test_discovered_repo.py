"""The agent's duplicate guard — the reason it can run every six hours safely.

Without this record, every scheduled discovery run would re-process the same
articles and mail customers about them again. These tests are named after that
failure, because it is the one that would reach a customer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.enums import PostState
from core.models import DiscoveredPost
from modules.repository.discovered_repo import (
    DiscoveredPostRepository,
    dedupe_key,
    normalise_url,
)

pytestmark = pytest.mark.integration


def post(
    url: str = "https://vaysinfotech.com/ai-security/",
    external_id: str | None = "12694",
    title: str = "AI Security Is Becoming a New Category",
    source: str = "wordpress-api",
    published_at: datetime | None = None,
) -> DiscoveredPost:
    return DiscoveredPost(
        url=url,
        title=title,
        external_id=external_id,
        source=source,
        published_at=published_at or datetime(2026, 8, 7, tzinfo=UTC),
    )


@pytest.fixture
def repo(db_session) -> DiscoveredPostRepository:  # noqa: ANN001
    return DiscoveredPostRepository(db_session)


def real_campaign(session) -> int:  # noqa: ANN001
    """A genuine campaign row.

    The foreign keys are enforced (``foreign_keys=ON``), so a made-up id is
    rejected — which is the schema behaving correctly and worth keeping.
    """
    from core.enums import Category, Tone
    from core.models import NewsletterContent
    from modules.repository.campaign_repo import CampaignRepository

    content = NewsletterContent(
        title="A generated newsletter about industrial security",
        summary="Industrial security guidance drawn from the latest Vays Infotech post.",
        newsletter=(
            "Rugged firewalls sit where the plant network meets everything else, and the "
            "sticker price is the smallest part of what they cost.\n\n"
            "Power, mounting, spares and the hours spent commissioning them add up to more "
            "than the hardware over a five-year life."
        ),
        subject="What a rugged firewall actually costs",
        preview_text="Total cost of ownership, not sticker price",
        cta="Read the breakdown",
        keywords=["ot", "firewall", "security"],
        category=Category.SECURITY,
        tone=Tone.PROFESSIONAL,
    )
    return int(CampaignRepository(session).create(name="Agent draft", content=content).id)


def real_article(session) -> int:  # noqa: ANN001
    from core.models import CleanedArticle
    from modules.repository.article_repo import ArticleRepository

    article = CleanedArticle(
        url="https://vaysinfotech.com/rugged-industrial-firewall-tco/",
        title="Total Cost of Ownership of a Rugged Industrial Firewall",
        cleaned_text="A rugged firewall costs more than its price tag. " * 30,
        word_count=270,
        token_estimate=360,
        extractor="trafilatura",
        language="en",
    )
    return int(ArticleRepository(session).create(article, raw_text="raw").id)


# ─────────────────────────────────────────────────────────────────────────────
#  Identity
# ─────────────────────────────────────────────────────────────────────────────
class TestDedupeKey:
    def test_the_post_id_is_preferred_over_the_url(self) -> None:
        assert dedupe_key(post()) == "wordpress-api:12694"

    def test_a_retitled_post_keeps_its_identity(self) -> None:
        """Editing a headline rewrites the WordPress slug. Matching on URL alone
        would treat the correction as a brand-new article and send a second
        newsletter about it."""
        original = post(url="https://vaysinfotech.com/ai-security/")
        retitled = post(url="https://vaysinfotech.com/ai-security-new-category/")

        assert dedupe_key(original) == dedupe_key(retitled)

    def test_without_an_id_the_url_is_used(self) -> None:
        key = dedupe_key(post(external_id=None, source="rss-feed"))

        assert key.startswith("url:")

    def test_tracking_parameters_do_not_create_a_new_identity(self) -> None:
        """A feed URL with ?utm_source= is the same article."""
        plain = post(external_id=None, url="https://vaysinfotech.com/ai-security/")
        tracked = post(external_id=None, url="https://vaysinfotech.com/ai-security/?utm_source=rss")

        assert dedupe_key(plain) == dedupe_key(tracked)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://vaysinfotech.com/a/", "https://vaysinfotech.com/a"),
            ("https://VaysInfotech.com/a", "https://vaysinfotech.com/a"),
            ("https://vaysinfotech.com/a#top", "https://vaysinfotech.com/a"),
        ],
    )
    def test_url_normalisation(self, left: str, right: str) -> None:
        assert normalise_url(left) == normalise_url(right)

    def test_different_posts_stay_different(self) -> None:
        assert dedupe_key(post(external_id="1")) != dedupe_key(post(external_id="2"))


# ─────────────────────────────────────────────────────────────────────────────
#  The guard itself
# ─────────────────────────────────────────────────────────────────────────────
class TestDuplicatePrevention:
    def test_a_new_post_is_recorded(self, repo: DiscoveredPostRepository) -> None:
        created = repo.record_new([post()])

        assert len(created) == 1
        assert created[0].state is PostState.DISCOVERED

    def test_the_same_post_is_not_recorded_twice(self, repo: DiscoveredPostRepository) -> None:
        """The failure this whole table exists to prevent: a second newsletter
        about an article the customer already received."""
        repo.record_new([post()])

        second_run = repo.record_new([post()])

        assert second_run == []
        assert repo.count() == 1

    def test_a_repeated_run_returns_only_the_genuinely_new(
        self, repo: DiscoveredPostRepository
    ) -> None:
        """The normal case on every run after the first: the API returns the same
        20 posts and only one of them is new."""
        repo.record_new([post(external_id="1"), post(external_id="2")])

        created = repo.record_new(
            [post(external_id="1"), post(external_id="2"), post(external_id="3")]
        )

        assert [c.external_id for c in created] == ["3"]
        assert repo.count() == 3

    def test_duplicates_within_one_batch_are_collapsed(
        self, repo: DiscoveredPostRepository
    ) -> None:
        created = repo.record_new([post(external_id="7"), post(external_id="7")])

        assert len(created) == 1

    def test_is_known_answers_without_writing(self, repo: DiscoveredPostRepository) -> None:
        assert not repo.is_known(post())
        repo.record_new([post()])
        assert repo.is_known(post())

    def test_the_unique_constraint_is_the_real_guarantee(
        self, repo: DiscoveredPostRepository, db_session
    ) -> None:  # noqa: ANN001
        """Application-level checking has a race between read and write. The
        constraint is what actually holds, so it is asserted directly."""
        from sqlalchemy.exc import IntegrityError

        from modules.repository.orm_models import DiscoveredPostORM

        repo.record_new([post()])

        db_session.add(
            DiscoveredPostORM(dedupe_key="wordpress-api:12694", url="https://x/", title="collision")
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


# ─────────────────────────────────────────────────────────────────────────────
#  Work queue
# ─────────────────────────────────────────────────────────────────────────────
class TestPending:
    def test_newly_discovered_posts_are_pending(self, repo: DiscoveredPostRepository) -> None:
        repo.record_new([post()])

        assert len(repo.pending()) == 1

    def test_a_generated_post_is_not_picked_up_again(
        self, repo: DiscoveredPostRepository, db_session
    ) -> None:  # noqa: ANN001
        [row] = repo.record_new([post()])
        repo.mark(row.id, PostState.GENERATED, campaign_id=real_campaign(db_session))

        assert repo.pending() == []

    def test_a_skipped_post_is_never_retried(self, repo: DiscoveredPostRepository) -> None:
        """SKIPPED means "deliberately not processed", which is different from
        FAILED and must not consume a slot on every run forever."""
        [row] = repo.record_new([post()])
        repo.mark(row.id, PostState.SKIPPED)

        assert repo.pending() == []

    def test_a_failed_post_is_retried(self, repo: DiscoveredPostRepository) -> None:
        [row] = repo.record_new([post()])
        repo.mark(row.id, PostState.FAILED, error="site timed out")

        assert len(repo.pending()) == 1

    def test_retries_are_bounded(self, repo: DiscoveredPostRepository) -> None:
        """A site that permanently blocks extraction must stop being attempted."""
        [row] = repo.record_new([post()])
        for _ in range(3):
            repo.mark(row.id, PostState.FAILED, error="blocked")

        assert repo.pending(max_attempts=3) == []

    def test_the_backlog_drains_oldest_first(self, repo: DiscoveredPostRepository) -> None:
        """Newest-first would starve older posts permanently."""
        now = datetime(2026, 8, 7, tzinfo=UTC)
        repo.record_new(
            [
                post(external_id="new", published_at=now),
                post(external_id="old", published_at=now - timedelta(days=30)),
            ]
        )

        assert [p.external_id for p in repo.pending()] == ["old", "new"]

    def test_the_limit_is_respected(self, repo: DiscoveredPostRepository) -> None:
        """Bounds the work one run can start — the Groq token ceiling is the
        real constraint behind this."""
        repo.record_new([post(external_id=str(i)) for i in range(5)])

        assert len(repo.pending(limit=2)) == 2


# ─────────────────────────────────────────────────────────────────────────────
#  State transitions
# ─────────────────────────────────────────────────────────────────────────────
class TestMark:
    def test_success_does_not_consume_the_retry_budget(
        self, repo: DiscoveredPostRepository, db_session
    ) -> None:  # noqa: ANN001
        """Counting successful steps would exhaust the budget during normal
        progress and park a healthy post as permanently failed."""
        [row] = repo.record_new([post()])

        repo.mark(row.id, PostState.EXTRACTED, article_id=real_article(db_session))

        assert repo.get(row.id).attempts == 0

    def test_failure_records_the_reason(self, repo: DiscoveredPostRepository) -> None:
        [row] = repo.record_new([post()])

        repo.mark(row.id, PostState.FAILED, error="HTTP 403 from the site")

        stored = repo.get(row.id)
        assert stored.attempts == 1
        assert "403" in stored.last_error

    def test_a_later_success_clears_a_stale_error(
        self, repo: DiscoveredPostRepository, db_session
    ) -> None:  # noqa: ANN001
        """A post showing an error it has since recovered from would send someone
        chasing a problem that no longer exists."""
        [row] = repo.record_new([post()])
        repo.mark(row.id, PostState.FAILED, error="timeout")

        repo.mark(row.id, PostState.EXTRACTED, article_id=real_article(db_session))

        assert repo.get(row.id).last_error is None

    def test_the_campaign_link_is_stored(self, repo: DiscoveredPostRepository, db_session) -> None:  # noqa: ANN001
        """Provenance: which blog post produced which campaign."""
        [row] = repo.record_new([post()])
        campaign_id = real_campaign(db_session)

        repo.mark(row.id, PostState.GENERATED, campaign_id=campaign_id)

        assert repo.get(row.id).campaign_id == campaign_id

    def test_marking_a_missing_row_is_a_no_op(self, repo: DiscoveredPostRepository) -> None:
        repo.mark(9999, PostState.FAILED, error="x")  # must not raise


class TestReporting:
    def test_state_counts_drive_the_dashboard(
        self, repo: DiscoveredPostRepository, db_session
    ) -> None:  # noqa: ANN001
        rows = repo.record_new([post(external_id=str(i)) for i in range(3)])
        repo.mark(rows[0].id, PostState.GENERATED, campaign_id=real_campaign(db_session))

        counts = repo.state_counts()

        assert counts[PostState.DISCOVERED] == 2
        assert counts[PostState.GENERATED] == 1

    def test_recent_is_newest_first(self, repo: DiscoveredPostRepository) -> None:
        repo.record_new([post(external_id="1"), post(external_id="2")])

        assert len(repo.recent(limit=5)) == 2
