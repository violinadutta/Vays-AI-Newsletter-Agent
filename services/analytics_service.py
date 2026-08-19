"""Delivery analytics: who received which newsletter, when, and whether it landed.

Reads only. Nothing here writes, sends, or retries — the page this feeds is a
record of what happened, and a reporting screen that can mutate state is a
screen people are afraid to click on.

The five columns the page shows are assembled here rather than in the page, so
the fallback rules below are testable without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from config import get_logger
from core.enums import SendStatus
from modules.repository.database import unit_of_work
from modules.repository.send_repo import SendRepository

log = get_logger(__name__)

#: Statuses that mean the message reached the provider and was accepted.
DELIVERED_STATUSES = (SendStatus.SENT,)

#: Statuses that mean it definitively did not arrive.
FAILED_STATUSES = (SendStatus.FAILED, SendStatus.BOUNCED)


@dataclass(frozen=True)
class DeliveryRecord:
    """One delivery attempt, in the shape the analytics table displays."""

    recipient_name: str
    email: str
    newsletter: str
    status: str
    delivered_at: datetime | None
    campaign_id: int
    is_estimated_time: bool
    error: str | None = None

    @property
    def delivered(self) -> bool:
        return self.status == str(SendStatus.SENT)


@dataclass(frozen=True)
class DeliverySummary:
    """Headline totals for the tiles above the table."""

    total: int = 0
    delivered: int = 0
    failed: int = 0
    pending: int = 0

    @property
    def delivery_rate(self) -> float:
        """Delivered as a percentage of *attempts that resolved*.

        Queued rows are excluded from the denominator on purpose: counting a
        message that has not been attempted yet as a failure would make the rate
        dip during a send and recover afterwards, which reads as a fault.
        """
        resolved = self.delivered + self.failed
        return (self.delivered / resolved * 100) if resolved else 0.0


class AnalyticsService:
    """Delivery history, assembled for reporting."""

    def records(
        self,
        *,
        statuses: list[SendStatus] | None = None,
        campaign_id: int | None = None,
        search: str | None = None,
        days: int | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[DeliveryRecord]:
        """Delivery attempts, newest first.

        Args:
            statuses: Restrict to these statuses. None means all.
            campaign_id: Restrict to a single campaign.
            search: Substring match on recipient email or name.
            days: Only the last N days. None means all time.
            limit: Maximum rows.
            offset: Rows to skip, for paging.
        """
        since = self._since(days)
        with unit_of_work() as session:
            rows = SendRepository(session).delivery_log(
                statuses=statuses,
                campaign_id=campaign_id,
                search=search,
                since=since,
                limit=limit,
                offset=offset,
            )
            return [self._to_record(*row) for row in rows]

    def count(
        self,
        *,
        statuses: list[SendStatus] | None = None,
        campaign_id: int | None = None,
        search: str | None = None,
        days: int | None = None,
    ) -> int:
        """Total matching rows, ignoring paging."""
        with unit_of_work() as session:
            return SendRepository(session).delivery_log_count(
                statuses=statuses,
                campaign_id=campaign_id,
                search=search,
                since=self._since(days),
            )

    def summary(self, *, days: int | None = None) -> DeliverySummary:
        """Totals per status over the window."""
        with unit_of_work() as session:
            totals = SendRepository(session).status_totals(since=self._since(days))

        delivered = sum(totals.get(str(s), 0) for s in DELIVERED_STATUSES)
        failed = sum(totals.get(str(s), 0) for s in FAILED_STATUSES)
        return DeliverySummary(
            total=sum(totals.values()),
            delivered=delivered,
            failed=failed,
            pending=sum(totals.values()) - delivered - failed,
        )

    def campaigns(self) -> list[tuple[int, str]]:
        """(id, heading) for every campaign that has send records."""
        with unit_of_work() as session:
            return SendRepository(session).campaigns_with_sends()

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _since(days: int | None) -> datetime | None:
        return None if days is None else datetime.now(UTC) - timedelta(days=days)

    @staticmethod
    def _to_record(
        record: object, recipient: object, campaign: object, subscriber_name: str | None
    ) -> DeliveryRecord:
        """Flatten one joined row into the five display columns.

        Three fallbacks, each covering a real gap in the data rather than a
        hypothetical one:

        **Name** — the per-campaign snapshot first, then the master list, then a
        dash. A CSV of bare addresses stores no name at all, so most rows would
        otherwise be blank; the master list often has one the snapshot predates.

        **Newsletter** — the delivered *subject line* first, because that is the
        heading the recipient actually saw in their inbox. ``ai_subject`` is the
        generated original before any human edit, and ``name`` is the internal
        working title, so both are poorer answers to "which newsletter was this"
        and are used only when the subject is missing.

        **Time** — ``sent_at`` is the moment of delivery and is null for anything
        that never left. Rather than show an empty cell for a failure, the
        attempt time (``created_at``) is shown and flagged via
        ``is_estimated_time``, so the table can mark it instead of implying the
        message was delivered then.
        """
        name = (
            (getattr(recipient, "name", None) or "").strip()
            or (subscriber_name or "").strip()
            or "—"
        )
        newsletter = (
            (getattr(campaign, "subject", None) or "").strip()
            or (getattr(campaign, "ai_subject", None) or "").strip()
            or (getattr(campaign, "name", None) or "").strip()
            or f"Campaign #{getattr(campaign, 'id', '?')}"
        )
        sent_at = getattr(record, "sent_at", None)
        return DeliveryRecord(
            recipient_name=name,
            email=getattr(recipient, "email", ""),
            newsletter=newsletter,
            status=str(getattr(record, "status", "")),
            delivered_at=sent_at or getattr(record, "created_at", None),
            campaign_id=int(getattr(record, "campaign_id", 0)),
            is_estimated_time=sent_at is None,
            error=getattr(record, "error_message", None) or getattr(record, "error_code", None),
        )
