"""The last gate before a customer's inbox.

Every test here is about one rule:

    send only if  status == APPROVED  AND  now >= configured send time

The failures it prevents are the two worst this system has — mail going out that
nobody approved, and mail going out at the wrong hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.enums import CampaignStatus, Category, Tone, UserRole
from core.models import CampaignSendReport, NewsletterContent
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.orm_models import CampaignORM
from services.approval_service import ApprovalService
from services.dispatch_service import DispatchService

pytestmark = pytest.mark.integration

IST = ZoneInfo("Asia/Kolkata")


class StubDelivery:
    """Records what it was asked to send, and sends nothing."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[int] = []

    def send_campaign(
        self, campaign_id: int, _content: object, **_kw: object
    ) -> CampaignSendReport:
        if self.fail:
            msg = "the email provider rejected the credentials"
            raise RuntimeError(msg)
        self.sent.append(campaign_id)
        with unit_of_work() as session:
            CampaignRepository(session).transition_or_raise(campaign_id, CampaignStatus.SENDING)
            CampaignRepository(session).transition_or_raise(campaign_id, CampaignStatus.SENT)
        return CampaignSendReport(
            campaign_id=campaign_id, attempted=2, sent=2, failed=0, duration_s=0.1
        )


@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from config.settings import reset_settings_cache
    from tests.conftest import MINIMAL_ENV

    set_env(
        **MINIMAL_ENV,
        AGENT_ENABLED="true",
        AGENT_APPROVAL_EMAIL="management@vaysinfotech.com",
        # Daily, so "approved two days ago" reliably means the window has passed.
        # The monthly arithmetic has its own calendar tests in
        # tests/unit/test_agent_settings.py; what is under test here is the gate.
        # TestTheProductionSchedule below covers the monthly config end to end.
        AGENT_SEND_SCHEDULE="daily",
        AGENT_SEND_TIME="09:00",
        AGENT_TIMEZONE="Asia/Kolkata",
    )
    reset_settings_cache()


def a_campaign(status: CampaignStatus = CampaignStatus.AWAITING_APPROVAL) -> int:
    content = NewsletterContent(
        title="What a rugged industrial firewall actually costs",
        summary="The sticker price is the smallest part of a rugged firewall's cost.",
        newsletter=(
            "A rugged firewall sits where the plant network meets everything else.\n\n"
            "Power, mounting and commissioning outweigh the hardware over five years."
        ),
        subject="What a rugged firewall actually costs",
        preview_text="Total cost of ownership, not sticker price",
        cta="Read the breakdown",
        keywords=["ot", "firewall", "security"],
        category=Category.SECURITY,
        tone=Tone.PROFESSIONAL,
    )
    with unit_of_work() as session:
        repo = CampaignRepository(session)
        campaign_id = int(repo.create(name="Agent draft", content=content).id)
        if status is not CampaignStatus.DRAFT:
            repo.transition_or_raise(campaign_id, CampaignStatus.AWAITING_APPROVAL)
    return campaign_id


def approve(campaign_id: int, *, at: datetime | None = None) -> None:
    ApprovalService().approve(campaign_id, by="admin", role=UserRole.ADMIN)
    if at is not None:
        with unit_of_work() as session:
            # Converted to UTC exactly as production does. SQLite keeps no
            # timezone, so storing an IST-aware value would read back as that
            # wall clock in UTC — 5.5 hours adrift, and a send on the wrong day.
            session.get(CampaignORM, campaign_id).approved_at = at.astimezone(UTC)


def status_of(campaign_id: int) -> str:
    with unit_of_work() as session:
        return str(CampaignRepository(session).get(campaign_id).status)


# ─────────────────────────────────────────────────────────────────────────────
#  Approval is required
# ─────────────────────────────────────────────────────────────────────────────
class TestApprovalIsRequired:
    def test_an_unapproved_campaign_is_never_dispatched(self) -> None:
        """The single most important assertion in the automation."""
        a_campaign()
        delivery = StubDelivery()

        report = DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == []
        assert report.considered == 0

    def test_a_rejected_campaign_is_never_dispatched(self) -> None:
        campaign_id = a_campaign()
        ApprovalService().reject(campaign_id, by="admin", role=UserRole.ADMIN)
        delivery = StubDelivery()

        DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == []
        assert status_of(campaign_id) == CampaignStatus.REJECTED

    def test_a_plain_draft_is_never_dispatched(self) -> None:
        """A manually created campaign belongs to whoever made it, not to the
        agent — the timer must not pick it up and send it."""
        with unit_of_work() as session:
            repo = CampaignRepository(session)
            content = NewsletterContent(
                title="A manual draft about industrial networking gear",
                summary="Something a person is still working on and has not sent yet.",
                newsletter=(
                    "A paragraph a person wrote by hand and has not finished yet.\n\n"
                    "And a second one, long enough to satisfy the content contract."
                ),
                subject="A manual draft nobody approved",
                preview_text="Still being written",
                cta="Read more",
                keywords=["manual", "draft", "wip"],
                category=Category.INDUSTRY_NEWS,
                tone=Tone.PROFESSIONAL,
            )
            repo.create(name="Manual", content=content)
        delivery = StubDelivery()

        DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == []


# ─────────────────────────────────────────────────────────────────────────────
#  The send time is required
# ─────────────────────────────────────────────────────────────────────────────
class TestTheSendWindow:
    def test_an_approved_campaign_waits_for_its_window(self) -> None:
        """Approved is not the same as sendable. The hour has to arrive."""
        campaign_id = a_campaign()
        # Approved a minute ago, so the next 09:00 is still ahead.
        approve(campaign_id, at=datetime.now(UTC) - timedelta(minutes=1))
        delivery = StubDelivery()

        report = DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == []
        assert len(report.waiting) == 1
        assert status_of(campaign_id) == CampaignStatus.APPROVED

    def test_it_sends_once_the_window_has_opened(self) -> None:
        campaign_id = a_campaign()
        # Approved two days ago: yesterday's 09:00 has long passed.
        approve(campaign_id, at=datetime.now(UTC) - timedelta(days=2))
        delivery = StubDelivery()

        report = DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == [campaign_id]
        assert report.recipients_sent == 2
        assert status_of(campaign_id) == CampaignStatus.SENT

    def test_the_due_time_is_computed_from_the_approval(self) -> None:
        campaign_id = a_campaign()
        approved_at = datetime(2026, 8, 13, 8, 0, tzinfo=IST)
        approve(campaign_id, at=approved_at)

        [due] = DispatchService.approved_campaigns()

        assert due.due_at == datetime(2026, 8, 13, 9, 0, tzinfo=IST)

    def test_an_approval_after_the_window_waits_for_the_next_day(self) -> None:
        campaign_id = a_campaign()
        approve(campaign_id, at=datetime(2026, 8, 13, 9, 5, tzinfo=IST))

        [due] = DispatchService.approved_campaigns()

        assert due.due_at == datetime(2026, 8, 14, 9, 0, tzinfo=IST)


#: ``datetime.weekday()`` is Monday-based, so Wednesday is 2. Named because a
#: bare 2 in a scheduling assertion is indistinguishable from a count.
WEDNESDAY = 2


class TestTheProductionSchedule:
    """The gate under the configuration Vays actually runs: 3rd Wednesday, 11:00."""

    @pytest.fixture(autouse=True)
    def _monthly(self, set_env) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache

        set_env(
            AGENT_SEND_SCHEDULE="monthly",
            AGENT_SEND_WEEKDAY="wednesday",
            AGENT_SEND_WEEK_OF_MONTH="3",
            AGENT_SEND_TIME="11:00",
        )
        reset_settings_cache()

    def test_it_waits_for_the_third_wednesday(self) -> None:
        """Approved now, so the next 3rd Wednesday is always still ahead.

        This asserted a fixed date (19 Aug 2026) until that day arrived and the
        campaign became genuinely due, turning a real pass into a failure. The
        property being tested is not *which* Wednesday but that the gate holds
        until one arrives, so it is now expressed that way: approve at ``now``
        and assert the shape of the resulting moment.
        """
        campaign_id = a_campaign()
        approve(campaign_id, at=datetime.now(UTC))
        delivery = StubDelivery()

        report = DispatchService(delivery=delivery).dispatch_due()

        [due] = report.waiting
        assert due.due_at > datetime.now(UTC), "a fresh approval cannot already be due"
        local = due.due_at.astimezone(IST)
        assert local.weekday() == WEDNESDAY
        assert 15 <= local.day <= 21, "the 3rd Wednesday always falls in this range"
        assert (local.hour, local.minute) == (11, 0)
        assert delivery.sent == []

    def test_it_sends_once_that_wednesday_has_passed(self) -> None:
        """Approved in a previous month, so the window is long open."""
        campaign_id = a_campaign()
        approve(campaign_id, at=datetime.now(UTC) - timedelta(days=70))
        delivery = StubDelivery()

        DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == [campaign_id]

    def test_approved_at_is_not_moved_by_a_later_edit(self) -> None:
        """``updated_at`` shifts on every write. If the gate used it, touching
        the row would slide the scheduled send forward indefinitely."""
        campaign_id = a_campaign()
        approved_at = datetime.now(UTC) - timedelta(days=2)
        approve(campaign_id, at=approved_at)

        with unit_of_work() as session:
            session.get(CampaignORM, campaign_id).name = "renamed later"

        [due] = DispatchService.approved_campaigns()
        assert abs((due.approved_at - approved_at).total_seconds()) < 2


# ─────────────────────────────────────────────────────────────────────────────
#  Safety
# ─────────────────────────────────────────────────────────────────────────────
class TestSafety:
    def test_a_disabled_agent_dispatches_nothing(self, set_env) -> None:  # noqa: ANN001
        """Turning the agent off must stop sends, not merely stop discovery."""
        from config.settings import reset_settings_cache

        campaign_id = a_campaign()
        approve(campaign_id, at=datetime.now(UTC) - timedelta(days=2))
        set_env(AGENT_ENABLED="false")
        reset_settings_cache()
        delivery = StubDelivery()

        DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == []

    def test_a_delivery_failure_is_contained(self) -> None:
        """A scheduled pass that raises kills the worker, and nobody is
        watching when it fires."""
        campaign_id = a_campaign()
        approve(campaign_id, at=datetime.now(UTC) - timedelta(days=2))

        report = DispatchService(delivery=StubDelivery(fail=True)).dispatch_due()

        assert report.sent == []
        assert len(report.failed) == 1
        assert report.failed[0][0] == campaign_id

    def test_one_failure_does_not_stop_the_others(self) -> None:
        first, second = a_campaign(), a_campaign()
        approve(first, at=datetime.now(UTC) - timedelta(days=2))
        approve(second, at=datetime.now(UTC) - timedelta(days=2))

        class FailFirst(StubDelivery):
            def send_campaign(self, campaign_id: int, content: object, **kw: object):  # noqa: ANN201
                if campaign_id == first:
                    msg = "boom"
                    raise RuntimeError(msg)
                return super().send_campaign(campaign_id, content, **kw)

        report = DispatchService(delivery=FailFirst()).dispatch_due()

        assert report.sent == [second]
        assert len(report.failed) == 1

    def test_a_sent_campaign_is_not_sent_again(self) -> None:
        """The duplicate-send failure, one layer up: a second pass must find
        nothing to do."""
        campaign_id = a_campaign()
        approve(campaign_id, at=datetime.now(UTC) - timedelta(days=2))
        delivery = StubDelivery()
        DispatchService(delivery=delivery).dispatch_due()

        DispatchService(delivery=delivery).dispatch_due()

        assert delivery.sent == [campaign_id]

    def test_nothing_approved_is_a_quiet_no_op(self) -> None:
        report = DispatchService(delivery=StubDelivery()).dispatch_due()

        assert report.considered == 0
        assert report.sent == []


class TestReporting:
    def test_next_due_reports_the_soonest(self) -> None:
        later, sooner = a_campaign(), a_campaign()
        approve(later, at=datetime.now(UTC) - timedelta(hours=1))
        approve(sooner, at=datetime.now(UTC) - timedelta(days=2))

        assert DispatchService.next_due().campaign_id == sooner

    def test_next_due_is_none_with_nothing_approved(self) -> None:
        assert DispatchService.next_due() is None
