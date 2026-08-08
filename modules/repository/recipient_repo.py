"""Recipient and suppression-list persistence.

These live together because they answer one question: *who may we mail?*
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from core.enums import SuppressionReason
from core.models import Recipient
from modules.repository.orm_models import RecipientORM, SuppressionORM


class RecipientRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def replace_all(self, campaign_id: int, recipients: list[Recipient]) -> int:
        """Replace a campaign's recipient list wholesale.

        Re-uploading a CSV should *replace* the list, not append to it —
        otherwise a corrected upload silently mails everyone on the first list
        as well.
        """
        self.session.execute(delete(RecipientORM).where(RecipientORM.campaign_id == campaign_id))
        for recipient in recipients:
            self.session.add(
                RecipientORM(
                    campaign_id=campaign_id,
                    email=recipient.email,
                    name=recipient.name,
                    company=recipient.company,
                    extra=recipient.extra or None,
                )
            )
        self.session.flush()
        return len(recipients)

    def list_for_campaign(self, campaign_id: int, *, valid_only: bool = True) -> list[RecipientORM]:
        stmt = select(RecipientORM).where(RecipientORM.campaign_id == campaign_id)
        if valid_only:
            stmt = stmt.where(RecipientORM.is_valid.is_(True))
        return list(self.session.execute(stmt.order_by(RecipientORM.id)).scalars())

    def count_for_campaign(self, campaign_id: int) -> int:
        return self.session.execute(
            select(func.count())
            .select_from(RecipientORM)
            .where(RecipientORM.campaign_id == campaign_id)
        ).scalar_one()


class SuppressionRepository:
    """The do-not-send list.

    Checked before every send without exception, even when the address appears
    in a freshly uploaded CSV. Someone who unsubscribed must stay unsubscribed
    regardless of what a later upload contains — that is both a legal
    requirement and the whole point of the list.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, email: str, reason: SuppressionReason) -> None:
        """Idempotent: suppressing an already-suppressed address is a no-op."""
        normalised = email.strip().lower()
        if self.session.get(SuppressionORM, normalised) is None:
            self.session.add(SuppressionORM(email=normalised, reason=reason))
            self.session.flush()

    def is_suppressed(self, email: str) -> bool:
        return self.session.get(SuppressionORM, email.strip().lower()) is not None

    def filter_suppressed(self, emails: list[str]) -> set[str]:
        """Return which of ``emails`` are suppressed, in one query.

        Bulk, not per-address: a 10,000-recipient campaign would otherwise issue
        10,000 round trips before it could send anything.
        """
        if not emails:
            return set()
        normalised = [e.strip().lower() for e in emails]
        rows = self.session.execute(
            select(SuppressionORM.email).where(SuppressionORM.email.in_(normalised))
        ).scalars()
        return set(rows)

    def remove(self, email: str) -> None:
        row = self.session.get(SuppressionORM, email.strip().lower())
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def count(self) -> int:
        return self.session.execute(select(func.count()).select_from(SuppressionORM)).scalar_one()
