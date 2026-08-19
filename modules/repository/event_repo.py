"""Recipient engagement events — likes and unsubscribes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.enums import EmailAction
from modules.repository.orm_models import EmailEventORM


class EmailEventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        email: str,
        campaign_id: int,
        action: EmailAction,
        *,
        user_agent: str | None = None,
    ) -> bool:
        """Record one engagement event. Returns whether it was new.

        Writes first and handles the collision, rather than checking for an
        existing row and then inserting. Two clicks arriving together would both
        pass a prior check and produce a duplicate; the unique constraint is the
        only thing that actually holds, so it is what decides.

        Returning ``False`` for a repeat is not an error — it lets the page say
        "you have already unsubscribed" instead of pretending something changed.
        """
        savepoint = self.session.begin_nested()
        try:
            self.session.add(
                EmailEventORM(
                    email=email.strip().lower(),
                    campaign_id=campaign_id,
                    action=action,
                    user_agent=(user_agent or None),
                )
            )
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            return False
        return True

    def has(self, email: str, campaign_id: int, action: EmailAction) -> bool:
        """Whether this person already took this action on this campaign."""
        found = self.session.execute(
            select(EmailEventORM.id).where(
                EmailEventORM.email == email.strip().lower(),
                EmailEventORM.campaign_id == campaign_id,
                EmailEventORM.action == action,
            )
        ).first()
        return found is not None

    def actions_for(self, campaign_ids: list[int]) -> dict[tuple[str, int], set[str]]:
        """Every event for these campaigns, keyed by (email, campaign_id).

        Shaped for the analytics table, which needs to annotate up to a few
        hundred delivery rows. One query returning a lookup beats a query per
        row, and the page can then answer "did this person like this one" with a
        dictionary hit.
        """
        if not campaign_ids:
            return {}
        rows = self.session.execute(
            select(EmailEventORM.email, EmailEventORM.campaign_id, EmailEventORM.action).where(
                EmailEventORM.campaign_id.in_(campaign_ids)
            )
        )
        lookup: dict[tuple[str, int], set[str]] = {}
        for email, campaign_id, action in rows:
            lookup.setdefault((email, int(campaign_id)), set()).add(str(action))
        return lookup

    def totals(self, *, since: datetime | None = None) -> dict[str, int]:
        """Event counts per action, for the summary tiles."""
        query = select(EmailEventORM.action, func.count()).group_by(EmailEventORM.action)
        if since is not None:
            query = query.where(EmailEventORM.created_at >= since)
        return {str(action): int(count) for action, count in self.session.execute(query)}
