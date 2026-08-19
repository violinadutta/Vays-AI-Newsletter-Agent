"""Per-recipient send-record persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.enums import SendStatus
from core.models import SendResult
from modules.repository.orm_models import (
    CampaignORM,
    RecipientORM,
    SendRecordORM,
    SubscriberORM,
)


class SendRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        campaign_id: int,
        recipient_id: int,
        result: SendResult,
        *,
        batch_number: int | None = None,
    ) -> SendRecordORM:
        row = SendRecordORM(
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            status=result.status,
            provider_message_id=result.provider_message_id,
            error_code=result.error_code,
            error_message=result.error_message,
            attempt_count=result.attempts,
            batch_number=batch_number,
            sent_at=datetime.now(UTC) if result.status == SendStatus.SENT else None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def counts_for_campaign(self, campaign_id: int) -> dict[str, int]:
        rows = self.session.execute(
            select(SendRecordORM.status, func.count())
            .where(SendRecordORM.campaign_id == campaign_id)
            .group_by(SendRecordORM.status)
        )
        return {str(status): count for status, count in rows}

    def failed_recipients(self, campaign_id: int) -> list[tuple[SendRecordORM, RecipientORM]]:
        """Failed sends joined to their recipients, for the 'retry failed' flow.

        Joined rather than fetched separately so the UI can show the address next
        to the reason without an N+1 query per failure.
        """
        rows = self.session.execute(
            select(SendRecordORM, RecipientORM)
            .join(RecipientORM, SendRecordORM.recipient_id == RecipientORM.id)
            .where(
                SendRecordORM.campaign_id == campaign_id,
                SendRecordORM.status.in_([SendStatus.FAILED, SendStatus.BOUNCED]),
            )
            .order_by(SendRecordORM.id)
        )
        return [(record, recipient) for record, recipient in rows]

    def delivery_log(
        self,
        *,
        statuses: list[SendStatus] | None = None,
        campaign_id: int | None = None,
        search: str | None = None,
        since: datetime | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[tuple[SendRecordORM, RecipientORM, CampaignORM, str | None]]:
        """Every delivery attempt, joined to its recipient and campaign.

        One query with three joins rather than a lookup per row: the analytics
        page shows hundreds of records at once, and an N+1 here would issue a
        query per line on every rerun.

        The fourth element is the *subscriber's* name, outer-joined on email.
        ``RecipientORM.name`` is a per-campaign snapshot and is frequently null
        for addresses that arrived as a bare email, whereas the master list may
        have learned a name since. Preferring the snapshot and falling back to
        the master list is done in the service, not here — this returns both and
        lets the caller decide.

        Args:
            statuses: Restrict to these delivery statuses. None means all.
            campaign_id: Restrict to one campaign.
            search: Case-insensitive substring match on recipient email or name.
            since: Only records created at or after this moment.
            limit: Maximum rows returned.
            offset: Rows to skip, for paging.
        """
        query = (
            select(SendRecordORM, RecipientORM, CampaignORM, SubscriberORM.name)
            .join(RecipientORM, SendRecordORM.recipient_id == RecipientORM.id)
            .join(CampaignORM, SendRecordORM.campaign_id == CampaignORM.id)
            .outerjoin(SubscriberORM, SubscriberORM.email == RecipientORM.email)
        )
        if statuses:
            query = query.where(SendRecordORM.status.in_(statuses))
        if campaign_id is not None:
            query = query.where(SendRecordORM.campaign_id == campaign_id)
        if since is not None:
            query = query.where(SendRecordORM.created_at >= since)
        if search:
            pattern = f"%{search.strip().lower()}%"
            query = query.where(
                func.lower(RecipientORM.email).like(pattern)
                | func.lower(func.coalesce(RecipientORM.name, "")).like(pattern)
            )

        # Newest first. sent_at is null for anything that never left, so records
        # are ordered by it descending with a created_at tiebreak, keeping
        # never-sent rows adjacent to the attempt that produced them rather than
        # stranded at one end of the table.
        query = (
            query.order_by(
                SendRecordORM.sent_at.desc().nullslast(),
                SendRecordORM.created_at.desc(),
                SendRecordORM.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return [tuple(row) for row in self.session.execute(query)]

    def delivery_log_count(
        self,
        *,
        statuses: list[SendStatus] | None = None,
        campaign_id: int | None = None,
        search: str | None = None,
        since: datetime | None = None,
    ) -> int:
        """How many rows :meth:`delivery_log` would return without paging."""
        query = select(func.count()).select_from(SendRecordORM)
        if search:
            query = query.join(RecipientORM, SendRecordORM.recipient_id == RecipientORM.id)
            pattern = f"%{search.strip().lower()}%"
            query = query.where(
                func.lower(RecipientORM.email).like(pattern)
                | func.lower(func.coalesce(RecipientORM.name, "")).like(pattern)
            )
        if statuses:
            query = query.where(SendRecordORM.status.in_(statuses))
        if campaign_id is not None:
            query = query.where(SendRecordORM.campaign_id == campaign_id)
        if since is not None:
            query = query.where(SendRecordORM.created_at >= since)
        return int(self.session.execute(query).scalar_one())

    def status_totals(self, *, since: datetime | None = None) -> dict[str, int]:
        """Row counts per delivery status, for the summary tiles."""
        query = select(SendRecordORM.status, func.count()).group_by(SendRecordORM.status)
        if since is not None:
            query = query.where(SendRecordORM.created_at >= since)
        return {str(status): int(count) for status, count in self.session.execute(query)}

    def campaigns_with_sends(self) -> list[tuple[int, str]]:
        """Campaigns that have at least one send record, newest first.

        Only these can appear in the analytics filter. Listing every campaign
        would offer drafts that can produce no rows, so choosing one would look
        like a broken filter rather than an empty result.
        """
        rows = self.session.execute(
            select(CampaignORM.id, CampaignORM.subject, CampaignORM.name)
            .join(SendRecordORM, SendRecordORM.campaign_id == CampaignORM.id)
            .group_by(CampaignORM.id)
            .order_by(CampaignORM.id.desc())
        )
        return [
            (int(cid), (subject or name or f"Campaign #{cid}").strip())
            for cid, subject, name in rows
        ]

    def already_sent_recipient_ids(self, campaign_id: int) -> set[int]:
        """Recipients already delivered to.

        A retry must skip these. Without it, "retry failed only" would re-mail
        everyone who succeeded the first time — the exact duplicate-send problem
        the status guard exists to prevent, reintroduced one layer down.
        """
        rows = self.session.execute(
            select(SendRecordORM.recipient_id).where(
                SendRecordORM.campaign_id == campaign_id,
                SendRecordORM.status == SendStatus.SENT,
            )
        ).scalars()
        return set(rows)

    def total_sent(self) -> int:
        return self.session.execute(
            select(func.count())
            .select_from(SendRecordORM)
            .where(SendRecordORM.status == SendStatus.SENT)
        ).scalar_one()
