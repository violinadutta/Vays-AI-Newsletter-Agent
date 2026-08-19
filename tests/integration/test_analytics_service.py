"""Delivery analytics: the five reported columns, and where each one comes from.

Most of these are about the *fallbacks*. Three of the five columns are nullable
in the schema, so the interesting question is never "does it read the field" but
"what does it show when the field is empty". A blank Recipient column for every
address imported from a bare CSV would make the page look broken while being
technically correct.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.enums import CampaignStatus, SendStatus
from modules.repository.orm_models import (
    CampaignORM,
    RecipientORM,
    SendRecordORM,
    SubscriberORM,
)
from services.analytics_service import AnalyticsService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)


#: Distinguishes "caller did not say" from "caller explicitly said None".
#: sent_at=None is meaningful here — it is what a failed send looks like — so a
#: plain ``or`` default would silently turn that case into a delivered one and
#: the test would assert against the wrong scenario.
_UNSET = object()


def make_campaign(session, **overrides) -> CampaignORM:  # noqa: ANN001, ANN003
    values = {
        "name": "internal working title",
        "status": CampaignStatus.SENT,
        "subject": "The subject line customers saw",
    }
    values.update(overrides)
    campaign = CampaignORM(**values)
    session.add(campaign)
    session.flush()
    return campaign


def make_send(  # noqa: ANN201, PLR0913
    session,  # noqa: ANN001
    campaign: CampaignORM,
    *,
    email: str = "dana@client.com",
    name: str | None = "Dana Whitfield",
    status: SendStatus = SendStatus.SENT,
    sent_at: datetime | None | object = _UNSET,
    created_at: datetime | None = None,
    error_message: str | None = None,
):
    recipient = RecipientORM(campaign_id=campaign.id, email=email, name=name)
    session.add(recipient)
    session.flush()
    record = SendRecordORM(
        campaign_id=campaign.id,
        recipient_id=recipient.id,
        status=status,
        sent_at=datetime.now(UTC) if sent_at is _UNSET else sent_at,
        error_message=error_message,
    )
    if created_at is not None:
        record.created_at = created_at
    session.add(record)
    session.commit()
    return record


class TestTheFiveColumns:
    def test_a_delivered_email_reports_all_five(self, db_session) -> None:  # noqa: ANN001
        """The whole feature, in one row."""
        moment = datetime(2026, 8, 14, 18, 13, tzinfo=UTC)
        campaign = make_campaign(db_session, subject="IEC 62443 compliance")
        make_send(db_session, campaign, email="dana@client.com", sent_at=moment)

        record = AnalyticsService().records()[0]

        assert record.recipient_name == "Dana Whitfield"
        assert record.email == "dana@client.com"
        assert record.newsletter == "IEC 62443 compliance"
        assert record.status == str(SendStatus.SENT)
        assert record.delivered_at is not None
        assert record.delivered


class TestTheRecipientName:
    def test_the_campaign_snapshot_is_preferred(self, db_session) -> None:  # noqa: ANN001
        """It records who they were when the campaign was sent."""
        db_session.add(SubscriberORM(email="dana@client.com", name="Renamed Since"))
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, name="Dana Whitfield")

        assert AnalyticsService().records()[0].recipient_name == "Dana Whitfield"

    def test_it_falls_back_to_the_master_list(self, db_session) -> None:  # noqa: ANN001
        """A CSV of bare addresses stores no name against the campaign, so
        without this the column would be empty for most real rows."""
        db_session.add(SubscriberORM(email="dana@client.com", name="Dana Whitfield"))
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, name=None)

        assert AnalyticsService().records()[0].recipient_name == "Dana Whitfield"

    def test_an_unknown_name_shows_a_dash_not_an_empty_cell(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, name=None)

        assert AnalyticsService().records()[0].recipient_name == "—"

    def test_whitespace_counts_as_missing(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, name="   ")

        assert AnalyticsService().records()[0].recipient_name == "—"


class TestTheNewsletterHeading:
    def test_the_delivered_subject_wins(self, db_session) -> None:  # noqa: ANN001
        """What the recipient saw in their inbox, not the internal name."""
        campaign = make_campaign(
            db_session, subject="Edited subject", ai_subject="Generated", name="internal"
        )
        make_send(db_session, campaign)

        assert AnalyticsService().records()[0].newsletter == "Edited subject"

    def test_it_falls_back_to_the_generated_subject(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session, subject=None, ai_subject="Generated subject")
        make_send(db_session, campaign)

        assert AnalyticsService().records()[0].newsletter == "Generated subject"

    def test_then_to_the_internal_name(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session, subject=None, ai_subject=None, name="Q3 partner push")
        make_send(db_session, campaign)

        assert AnalyticsService().records()[0].newsletter == "Q3 partner push"


class TestTheDeliveryTime:
    def test_sent_at_is_the_delivery_time(self, db_session) -> None:  # noqa: ANN001
        moment = datetime(2026, 8, 14, 18, 13, tzinfo=UTC)
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, sent_at=moment)

        record = AnalyticsService().records()[0]

        assert record.delivered_at is not None
        assert record.delivered_at.hour == moment.hour
        assert record.is_estimated_time is False

    def test_a_failure_shows_the_attempt_time_and_is_flagged(self, db_session) -> None:  # noqa: ANN001
        """A failed send has no delivery time. Showing the attempt is more use
        than an empty cell, but it must not read as a delivery — hence the flag
        the page uses to mark it."""
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, status=SendStatus.FAILED, sent_at=None)

        record = AnalyticsService().records()[0]

        assert record.delivered_at is not None, "attempt time should stand in"
        assert record.is_estimated_time is True
        assert record.delivered is False


class TestFiltering:
    def test_by_status(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, email="ok@client.com")
        make_send(db_session, campaign, email="bad@client.com", status=SendStatus.FAILED)

        failed = AnalyticsService().records(statuses=[SendStatus.FAILED])

        assert [r.email for r in failed] == ["bad@client.com"]

    def test_by_campaign(self, db_session) -> None:  # noqa: ANN001
        first = make_campaign(db_session, subject="First")
        second = make_campaign(db_session, subject="Second")
        make_send(db_session, first, email="a@client.com")
        make_send(db_session, second, email="b@client.com")

        rows = AnalyticsService().records(campaign_id=second.id)

        assert [r.email for r in rows] == ["b@client.com"]

    def test_search_matches_email_or_name_case_insensitively(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, email="dana@client.com", name="Dana Whitfield")
        make_send(db_session, campaign, email="raj@other.com", name="Raj Patel")

        service = AnalyticsService()

        assert [r.email for r in service.records(search="WHITFIELD")] == ["dana@client.com"]
        assert [r.email for r in service.records(search="other.com")] == ["raj@other.com"]

    def test_the_time_window_excludes_older_records(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        old = datetime.now(UTC) - timedelta(days=45)
        make_send(db_session, campaign, email="old@client.com", created_at=old, sent_at=old)
        make_send(db_session, campaign, email="new@client.com")

        recent = AnalyticsService().records(days=30)

        assert [r.email for r in recent] == ["new@client.com"]

    def test_count_agrees_with_the_rows_returned(self, db_session) -> None:  # noqa: ANN001
        """The count drives paging, so a disagreement means empty pages."""
        campaign = make_campaign(db_session)
        for index in range(5):
            make_send(db_session, campaign, email=f"user{index}@client.com")
        make_send(db_session, campaign, email="bad@client.com", status=SendStatus.FAILED)

        service = AnalyticsService()

        assert service.count() == len(service.records())
        assert service.count(statuses=[SendStatus.FAILED]) == 1


class TestOrdering:
    def test_newest_first(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        base = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
        make_send(db_session, campaign, email="older@client.com", sent_at=base)
        make_send(db_session, campaign, email="newer@client.com", sent_at=base + timedelta(days=2))

        assert [r.email for r in AnalyticsService().records()] == [
            "newer@client.com",
            "older@client.com",
        ]


class TestSummary:
    def test_counts_split_by_outcome(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, email="a@client.com")
        make_send(db_session, campaign, email="b@client.com")
        make_send(db_session, campaign, email="c@client.com", status=SendStatus.FAILED)
        make_send(db_session, campaign, email="d@client.com", status=SendStatus.BOUNCED)

        summary = AnalyticsService().summary()

        assert (summary.total, summary.delivered, summary.failed) == (4, 2, 2)
        assert summary.delivery_rate == 50.0

    def test_queued_rows_are_excluded_from_the_rate(self, db_session) -> None:  # noqa: ANN001
        """Counting a message that has not been attempted yet as a failure would
        make the rate dip mid-send and recover afterwards, which reads as a
        fault rather than as work in progress."""
        campaign = make_campaign(db_session)
        make_send(db_session, campaign, email="a@client.com")
        make_send(db_session, campaign, email="b@client.com", status=SendStatus.QUEUED)

        summary = AnalyticsService().summary()

        assert summary.pending == 1
        assert summary.delivery_rate == 100.0

    def test_the_rate_is_zero_when_nothing_has_been_sent(self, db_session) -> None:  # noqa: ANN001
        assert AnalyticsService().summary().delivery_rate == 0.0


class TestCampaignList:
    def test_only_campaigns_with_sends_are_listed(self, db_session) -> None:  # noqa: ANN001
        """A draft can produce no rows, so offering it would look like a broken
        filter rather than an empty result."""
        sent = make_campaign(db_session, subject="Was sent")
        make_campaign(db_session, subject="Never sent", status=CampaignStatus.DRAFT)
        make_send(db_session, sent)

        assert AnalyticsService().campaigns() == [(sent.id, "Was sent")]

    def test_each_campaign_appears_once_however_many_recipients(self, db_session) -> None:  # noqa: ANN001
        campaign = make_campaign(db_session, subject="One entry please")
        for index in range(3):
            make_send(db_session, campaign, email=f"user{index}@client.com")

        assert len(AnalyticsService().campaigns()) == 1
