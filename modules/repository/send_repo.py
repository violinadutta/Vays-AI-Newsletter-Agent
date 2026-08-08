"""Per-recipient send-record persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.enums import SendStatus
from core.models import SendResult
from modules.repository.orm_models import RecipientORM, SendRecordORM


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
