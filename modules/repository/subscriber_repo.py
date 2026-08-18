"""Persistence for the master mailing list.

The property everything else depends on: **importing the same CSV twice adds
nobody twice.** ``email`` is the primary key, so that is guaranteed by the
database rather than by remembering to check first.

Removal deactivates rather than deletes. A hard delete would let the next import
of the original file silently put the person back — the same class of mistake
the suppression list exists to prevent one level up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.models import Recipient
from modules.repository.orm_models import SubscriberORM


@dataclass
class ImportOutcome:
    """What an import actually did, in terms a person can act on."""

    added: int = 0
    already_present: int = 0
    reactivated: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.reactivated)


class SubscriberRepository:
    """Reads and writes the master list."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── writing ──────────────────────────────────────────────────────────────
    def add_many(
        self,
        recipients: list[Recipient],
        *,
        source: str = "manual",
        added_by: str | None = None,
        reactivate: bool = False,
    ) -> ImportOutcome:
        """Append recipients to the list. Existing entries are left alone.

        Args:
            reactivate: Whether an import should bring back someone previously
                removed. Default ``False`` — a re-upload of the original file
                must not undo a deliberate removal. The manual "add" path passes
                ``True``, because there the intent is explicit.
        """
        outcome = ImportOutcome()

        for recipient in recipients:
            email = recipient.email.strip().lower()
            if not email:
                continue

            existing = self.session.get(SubscriberORM, email)
            if existing is None:
                self.session.add(
                    SubscriberORM(
                        email=email,
                        name=recipient.name,
                        company=recipient.company,
                        source=source,
                        added_by=added_by,
                    )
                )
                outcome.added += 1
                continue

            if not existing.is_active and reactivate:
                existing.is_active = True
                existing.updated_at = datetime.now(UTC)
                outcome.reactivated += 1
            else:
                outcome.already_present += 1
                # Fill in details that were blank before — a later import with a
                # name column should enrich an address imported without one.
                if recipient.name and not existing.name:
                    existing.name = recipient.name
                if recipient.company and not existing.company:
                    existing.company = recipient.company

        self.session.flush()
        return outcome

    def deactivate(self, email: str) -> bool:
        """Remove someone from sending, keeping the record. Returns whether it
        changed anything."""
        row = self.session.get(SubscriberORM, email.strip().lower())
        if row is None or not row.is_active:
            return False
        row.is_active = False
        row.updated_at = datetime.now(UTC)
        self.session.flush()
        return True

    def reactivate(self, email: str) -> bool:
        row = self.session.get(SubscriberORM, email.strip().lower())
        if row is None or row.is_active:
            return False
        row.is_active = True
        row.updated_at = datetime.now(UTC)
        self.session.flush()
        return True

    def delete(self, email: str) -> bool:
        """Erase the record entirely — for a deletion request, not for "remove
        from the list". Deactivation is the everyday action."""
        row = self.session.get(SubscriberORM, email.strip().lower())
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    # ── reading ──────────────────────────────────────────────────────────────
    def active(self) -> list[Recipient]:
        """Everyone currently on the list, as send-ready recipients.

        The suppression list is **not** applied here — that check belongs with
        sending, where the existing validator already performs it in bulk. Doing
        it in two places would let the two disagree.
        """
        rows = self.session.execute(
            select(SubscriberORM)
            .where(SubscriberORM.is_active.is_(True))
            .order_by(SubscriberORM.added_at.asc())
        ).scalars()
        return [Recipient(email=r.email, name=r.name, company=r.company) for r in rows]

    def search(
        self, term: str = "", *, include_inactive: bool = True, limit: int = 200
    ) -> list[SubscriberORM]:
        """List entries for the management page, newest first."""
        query = select(SubscriberORM)
        if not include_inactive:
            query = query.where(SubscriberORM.is_active.is_(True))
        if term.strip():
            like = f"%{term.strip().lower()}%"
            query = query.where(
                or_(
                    func.lower(SubscriberORM.email).like(like),
                    func.lower(func.coalesce(SubscriberORM.name, "")).like(like),
                    func.lower(func.coalesce(SubscriberORM.company, "")).like(like),
                )
            )
        return list(
            self.session.execute(query.order_by(SubscriberORM.added_at.desc()).limit(limit))
            .scalars()
            .all()
        )

    def get(self, email: str) -> SubscriberORM | None:
        return self.session.get(SubscriberORM, email.strip().lower())

    def counts(self) -> dict[str, int]:
        """Active and inactive totals, for the page header."""
        rows = self.session.execute(
            select(SubscriberORM.is_active, func.count()).group_by(SubscriberORM.is_active)
        ).all()
        counts = {"active": 0, "inactive": 0}
        for is_active, count in rows:
            counts["active" if is_active else "inactive"] = int(count)
        counts["total"] = counts["active"] + counts["inactive"]
        return counts
