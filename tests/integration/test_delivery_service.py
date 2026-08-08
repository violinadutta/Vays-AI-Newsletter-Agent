"""Delivery and campaign service tests, against a real database.

The four guards between "click send" and a customer's inbox get the most
attention. Each covers a failure that is cheap to prevent and expensive to
explain to 487 people.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from core.enums import CampaignStatus, Category, SendStatus, SuppressionReason, Tone
from core.exceptions import InvalidCSVError, ValidationError
from core.models import ContentPatch, NewsletterContent, Recipient, SendResult
from modules.email.batcher import BatchSender
from modules.email.console_provider import ConsoleEmailProvider
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.recipient_repo import RecipientRepository, SuppressionRepository
from modules.repository.send_repo import SendRepository
from services.campaign_service import CampaignService
from services.delivery_service import DeliveryService

pytestmark = pytest.mark.integration

ADDRESS = "Vays Infotech, 4th Floor, Tech Park, Pune 411045, India"


@pytest.fixture(autouse=True)
def _env(set_env, db_session: Session) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(
        **MINIMAL_ENV,
        BRAND_ADDRESS=ADDRESS,
        EMAIL_SENDER_ADDRESS="newsletter@vays.com",
        UNSUBSCRIBE_BASE_URL="https://vaysinfotech.com/unsubscribe",
        EMAIL_BATCH_DELAY_S="0",
    )


@pytest.fixture
def content() -> NewsletterContent:
    return NewsletterContent(
        title="Dell's New PowerEdge Servers",
        summary="Dell refreshed its two-socket rack line with efficiency as the headline change.",
        newsletter=(
            "Hi {{name}}, Dell has announced the PowerEdge R7xx series, a refresh of its "
            "mainstream two-socket rack line.\n\nEfficiency is the headline change."
        ),
        subject="Dell's new servers cut power costs",
        preview_text="What the refresh means for your cycle",
        cta="Talk to our team",
        keywords=["dell", "poweredge", "servers"],
        category=Category.PRODUCT_LAUNCH,
        tone=Tone.PROFESSIONAL,
    )


@pytest.fixture
def campaign_id(db_session: Session, content: NewsletterContent) -> int:
    campaign = CampaignRepository(db_session).create(name="Test", content=content)
    db_session.commit()
    return int(campaign.id)


@pytest.fixture
def service(tmp_path: Path) -> DeliveryService:
    """Delivery wired to a console provider writing into a temp outbox."""
    provider = ConsoleEmailProvider(outbox=tmp_path / "outbox")
    return DeliveryService(sender=BatchSender(provider, batch_size=10, batch_delay_s=0))


def csv_bytes(rows: str) -> bytes:
    return rows.encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
#  CSV validation
# ─────────────────────────────────────────────────────────────────────────────
class TestRecipientValidation:
    def test_a_clean_list_parses(self, service: DeliveryService) -> None:
        result = service.validate_recipients(
            csv_bytes("email,name,company\na@vays.com,Ann,Acme\nb@vays.com,Bob,Beta\n")
        )

        assert result.sendable_count == 2
        assert result.valid[0].name == "Ann"

    def test_a_bad_row_is_reported_and_skipped_not_fatal(self, service: DeliveryService) -> None:
        """One bad row in 500 must not force the user to fix a spreadsheet first."""
        result = service.validate_recipients(
            csv_bytes("email\ngood@vays.com\nnot-an-email\nalso@vays.com\n")
        )

        assert result.sendable_count == 2
        assert len(result.invalid) == 1
        assert "row 3" in next(iter(result.invalid))

    def test_duplicates_are_removed_and_counted(self, service: DeliveryService) -> None:
        result = service.validate_recipients(
            csv_bytes("email\na@vays.com\nA@VAYS.COM\nb@vays.com\n")
        )

        assert result.sendable_count == 2
        assert result.duplicates == ["a@vays.com"]

    @pytest.mark.parametrize("header", ["email", "Email", "EMAIL ADDRESS", "e-mail"])
    def test_common_column_spellings_are_accepted(
        self, service: DeliveryService, header: str
    ) -> None:
        """Marketing tools label it differently in every export. Rejecting
        'Email Address' would be defensible and useless."""
        result = service.validate_recipients(csv_bytes(f"{header}\na@vays.com\n"))

        assert result.sendable_count == 1

    def test_a_missing_email_column_says_what_was_found(self, service: DeliveryService) -> None:
        with pytest.raises(InvalidCSVError) as exc_info:
            service.validate_recipients(csv_bytes("name,phone\nAnn,123\n"))

        assert "name" in exc_info.value.user_message

    def test_an_excel_bom_is_handled(self, service: DeliveryService) -> None:
        """Excel writes UTF-8 with a BOM; failing on it would be correct and
        unhelpful."""
        result = service.validate_recipients(b"\xef\xbb\xbfemail\na@vays.com\n")

        assert result.sendable_count == 1

    def test_cp1252_is_handled(self, service: DeliveryService) -> None:
        result = service.validate_recipients("email,name\na@vays.com,Renée\n".encode("cp1252"))

        assert result.sendable_count == 1

    def test_blank_lines_are_ignored_rather_than_reported(self, service: DeliveryService) -> None:
        result = service.validate_recipients(csv_bytes("email\na@vays.com\n\n\nb@vays.com\n"))

        assert result.sendable_count == 2
        assert not result.invalid


class TestSuppression:
    def test_suppressed_addresses_are_excluded_and_reported(
        self, service: DeliveryService, db_session: Session
    ) -> None:
        """Someone who unsubscribed stays unsubscribed even if their address is
        in a freshly uploaded CSV. Legal requirement, and the point of the list."""
        SuppressionRepository(db_session).add("gone@vays.com", SuppressionReason.UNSUBSCRIBED)
        db_session.commit()

        result = service.validate_recipients(csv_bytes("email\nok@vays.com\ngone@vays.com\n"))

        assert result.sendable_count == 1
        assert result.suppressed == ["gone@vays.com"]

    def test_suppression_is_case_insensitive(
        self, service: DeliveryService, db_session: Session
    ) -> None:
        SuppressionRepository(db_session).add("Gone@Vays.com", SuppressionReason.HARD_BOUNCE)
        db_session.commit()

        result = service.validate_recipients(csv_bytes("email\nGONE@VAYS.COM\n"))

        assert result.sendable_count == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Sending
# ─────────────────────────────────────────────────────────────────────────────
class TestSendCampaign:
    def _load(self, service: DeliveryService, campaign_id: int, count: int = 3) -> None:
        service.save_recipients(
            campaign_id,
            [Recipient(email=f"u{i}@vays.com", name=f"User{i}") for i in range(count)],
        )

    def test_a_campaign_sends_to_every_recipient(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent
    ) -> None:
        self._load(service, campaign_id)

        report = service.send_campaign(campaign_id, content)

        assert report.attempted == 3
        assert report.sent == 3
        assert report.fully_successful

    def test_the_second_send_is_refused(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent
    ) -> None:
        """The guard. A Streamlit rerun firing the handler twice must not mail
        every customer twice."""
        self._load(service, campaign_id)
        service.send_campaign(campaign_id, content)

        with pytest.raises(ValidationError) as exc_info:
            service.send_campaign(campaign_id, content)

        assert "already" in exc_info.value.user_message

    def test_sending_without_recipients_is_refused(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent
    ) -> None:
        with pytest.raises(ValidationError) as exc_info:
            service.send_campaign(campaign_id, content)

        assert "recipient list" in exc_info.value.user_message

    def test_per_recipient_records_are_persisted(
        self,
        service: DeliveryService,
        campaign_id: int,
        content: NewsletterContent,
        db_session: Session,
    ) -> None:
        self._load(service, campaign_id)

        service.send_campaign(campaign_id, content)

        counts = SendRepository(db_session).counts_for_campaign(campaign_id)
        assert counts.get(SendStatus.SENT.value) == 3

    def test_the_campaign_reaches_a_settled_state(
        self,
        service: DeliveryService,
        campaign_id: int,
        content: NewsletterContent,
        db_session: Session,
    ) -> None:
        self._load(service, campaign_id)

        service.send_campaign(campaign_id, content)

        db_session.expire_all()
        assert CampaignRepository(db_session).get(campaign_id).status == CampaignStatus.SENT

    def test_progress_is_reported(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent
    ) -> None:
        self._load(service, campaign_id, count=5)
        updates: list[tuple[int, int, int]] = []

        service.send_campaign(
            campaign_id, content, on_progress=lambda s, f, r: updates.append((s, f, r))
        )

        assert updates and updates[-1][2] == 0

    def test_each_recipient_gets_their_own_personalisation(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent, tmp_path: Path
    ) -> None:
        service.save_recipients(
            campaign_id,
            [
                Recipient(email="ann@vays.com", name="Ann"),
                Recipient(email="bob@vays.com", name="Bob"),
            ],
        )

        service.send_campaign(campaign_id, content)

        written = sorted((tmp_path / "outbox").glob("*.eml"))
        bodies = [p.read_text(encoding="utf-8", errors="replace") for p in written]
        assert any("Ann" in b for b in bodies)
        assert any("Bob" in b for b in bodies)


class TestRetryFailed:
    def test_retry_skips_already_delivered_recipients(
        self,
        service: DeliveryService,
        campaign_id: int,
        content: NewsletterContent,
        db_session: Session,
    ) -> None:
        """Without this filter, "retry failed only" re-mails everyone who
        succeeded — the duplicate-send problem one layer down."""
        service.save_recipients(
            campaign_id,
            [Recipient(email="ok@vays.com"), Recipient(email="bad@vays.com")],
        )
        rows = RecipientRepository(db_session).list_for_campaign(campaign_id)
        SendRepository(db_session).record(
            campaign_id,
            rows[0].id,
            SendResult(email="ok@vays.com", status=SendStatus.SENT),
        )
        db_session.commit()

        report = service.retry_failed(campaign_id, content)

        assert report.attempted == 1  # only the one that was never delivered

    def test_retry_with_nothing_outstanding_is_refused(
        self,
        service: DeliveryService,
        campaign_id: int,
        content: NewsletterContent,
    ) -> None:
        service.save_recipients(campaign_id, [Recipient(email="ok@vays.com")])
        service.send_campaign(campaign_id, content)

        with pytest.raises(ValidationError) as exc_info:
            service.retry_failed(campaign_id, content)

        assert "already been delivered" in exc_info.value.user_message


class TestSendTest:
    def test_a_test_send_creates_no_campaign_record(
        self,
        service: DeliveryService,
        content: NewsletterContent,
        db_session: Session,
    ) -> None:
        """A test send is not a campaign; counting it would corrupt the stats."""
        before = SendRepository(db_session).total_sent()

        result = service.send_test(content, "priya@vays.com")

        assert result.ok
        assert SendRepository(db_session).total_sent() == before

    def test_an_invalid_test_address_is_rejected(
        self, service: DeliveryService, content: NewsletterContent
    ) -> None:
        from core.exceptions import InvalidEmailError

        with pytest.raises(InvalidEmailError):
            service.send_test(content, "not-an-email")


# ─────────────────────────────────────────────────────────────────────────────
#  Campaign lifecycle
# ─────────────────────────────────────────────────────────────────────────────
class TestCampaignService:
    def test_content_can_be_edited_while_draft(self, campaign_id: int) -> None:
        CampaignService().update_content(campaign_id, ContentPatch(subject="A human subject"))

        assert CampaignService().get_content(campaign_id).subject == "A human subject"

    def test_a_sent_campaign_is_locked(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent
    ) -> None:
        """Editing mid-flight would mean two recipients received materially
        different emails from one campaign, and History matched neither."""
        service.save_recipients(campaign_id, [Recipient(email="a@vays.com")])
        service.send_campaign(campaign_id, content)

        with pytest.raises(ValidationError) as exc_info:
            CampaignService().update_content(campaign_id, ContentPatch(subject="too late"))

        assert "already been sent" in exc_info.value.user_message

    def test_duplicate_copies_content_but_not_recipients(
        self, service: DeliveryService, campaign_id: int
    ) -> None:
        """A duplicate inheriting the old list would be a surprising way to mail
        500 people."""
        service.save_recipients(campaign_id, [Recipient(email="a@vays.com")])

        copy_id = CampaignService().duplicate(campaign_id)

        assert copy_id != campaign_id
        assert CampaignService().get_content(copy_id).subject == (
            CampaignService().get_content(campaign_id).subject
        )
        assert CampaignService().recipient_count(copy_id) == 0

    def test_a_sent_campaign_cannot_be_deleted(
        self, service: DeliveryService, campaign_id: int, content: NewsletterContent
    ) -> None:
        """Sent campaigns are the audit trail."""
        service.save_recipients(campaign_id, [Recipient(email="a@vays.com")])
        service.send_campaign(campaign_id, content)

        with pytest.raises(ValidationError) as exc_info:
            CampaignService().delete(campaign_id)

        assert "Archive it instead" in exc_info.value.user_message

    def test_a_draft_can_be_deleted(self, campaign_id: int) -> None:
        CampaignService().delete(campaign_id)

        with pytest.raises(ValidationError):
            CampaignService().get_content(campaign_id)

    def test_deleting_twice_is_not_an_error(self, campaign_id: int) -> None:
        CampaignService().delete(campaign_id)
        CampaignService().delete(campaign_id)
