"""Campaign persistence — including the guarded status transition.

:meth:`CampaignRepository.transition_status` is the most safety-critical method
in the codebase. Everything else here is ordinary CRUD.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.orm import Session

from core.enums import SENDABLE_STATES, CampaignStatus, allowed_transitions, can_transition
from core.exceptions import InvalidStateTransition, PersistenceError
from core.models import (
    CampaignFilter,
    CampaignSummary,
    ContentPatch,
    NewsletterContent,
    Page,
)
from modules.repository.orm_models import CampaignArticleORM, CampaignORM

_AI_FIELDS = (
    "title",
    "summary",
    "newsletter",
    "subject",
    "preview_text",
    "cta",
    "keywords",
    "category",
    "tone",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CampaignRepository:
    """Persistence for campaigns. Never commits — the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── writes ───────────────────────────────────────────────────────────────
    def create(
        self,
        *,
        name: str,
        content: NewsletterContent,
        provenance: dict[str, Any] | None = None,
        created_by: str | None = None,
    ) -> CampaignORM:
        """Create a campaign, seeding the editable fields from the AI original.

        Both copies are written: ``ai_*`` is the immutable audit record and the
        unprefixed columns are what the user edits. Their divergence is the
        edit-ratio quality metric.
        """
        values: dict[str, Any] = {"name": name, "created_by": created_by}
        for field in _AI_FIELDS:
            value = getattr(content, field)
            if hasattr(value, "value"):  # StrEnum -> plain string
                value = value.value
            values[f"ai_{field}"] = value
            values[field] = value
        values.update(provenance or {})

        campaign = CampaignORM(**values)
        self.session.add(campaign)
        self.session.flush()  # assign the id without committing
        return campaign

    def link_articles(
        self,
        campaign_id: int,
        article_ids: list[int],
        summaries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Attach source articles in display order, with their stage-1 summaries."""
        for position, article_id in enumerate(article_ids):
            self.session.add(
                CampaignArticleORM(
                    campaign_id=campaign_id,
                    article_id=article_id,
                    position=position,
                    section_summary=(
                        summaries[position] if summaries and position < len(summaries) else None
                    ),
                )
            )
        self.session.flush()

    def update_content(self, campaign_id: int, patch: ContentPatch) -> CampaignORM:
        """Apply a partial edit.

        Only fields the caller explicitly set are touched — ``exclude_unset`` is
        what distinguishes "clear this" from "leave it alone".
        """
        campaign = self.get(campaign_id)
        if campaign is None:
            raise PersistenceError(f"campaign {campaign_id} not found")

        for field, value in patch.model_dump(exclude_unset=True).items():
            setattr(campaign, field, value)
        campaign.updated_at = _utcnow()
        self.session.flush()
        return campaign

    def set_rendered(self, campaign_id: int, html: str, text: str, template_id: str) -> None:
        self.session.execute(
            update(CampaignORM)
            .where(CampaignORM.id == campaign_id)
            .values(
                rendered_html=html,
                rendered_text=text,
                template_id=template_id,
                updated_at=_utcnow(),
            )
        )

    def increment_regeneration(self, campaign_id: int) -> None:
        self.session.execute(
            update(CampaignORM)
            .where(CampaignORM.id == campaign_id)
            .values(regeneration_count=CampaignORM.regeneration_count + 1)
        )

    def update_delivery_counts(
        self,
        campaign_id: int,
        *,
        recipients: int | None = None,
        sent: int | None = None,
        failed: int | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if recipients is not None:
            values["recipient_count"] = recipients
        if sent is not None:
            values["sent_count"] = sent
        if failed is not None:
            values["failed_count"] = failed
        if values:
            self.session.execute(
                update(CampaignORM).where(CampaignORM.id == campaign_id).values(**values)
            )

    # ── the double-send guard ────────────────────────────────────────────────
    def transition_status(self, campaign_id: int, to: CampaignStatus) -> bool:
        """Attempt a status transition. Returns whether it happened.

        This is a **single conditional UPDATE**, not a read-then-write::

            UPDATE campaigns SET status = :to
             WHERE id = :id AND status IN (<states that may reach :to>)

        Why that matters: Streamlit re-executes the whole script on every
        interaction, so a send handler can fire twice. With read-then-write, both
        reads see ``READY`` and both proceed — and 487 customers get the
        newsletter twice. With a conditional UPDATE, the second attempt matches
        zero rows and returns ``False``. The database, not application timing,
        is what makes this safe.

        Returns:
            ``True`` if the row was updated, ``False`` if the campaign was not in
            a state from which ``to`` is reachable (including because a
            concurrent call already moved it).
        """
        sources = [s for s in CampaignStatus if can_transition(s, to)]
        if not sources:
            raise InvalidStateTransition(
                f"no state can transition to {to}",
                context={"target": to.value},
            )

        values: dict[str, Any] = {"status": to, "updated_at": _utcnow()}
        if to in (CampaignStatus.SENT, CampaignStatus.PARTIAL_FAILURE):
            values["sent_at"] = _utcnow()

        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(CampaignORM)
                .where(CampaignORM.id == campaign_id, CampaignORM.status.in_(sources))
                .values(**values)
            ),
        )
        return bool(result.rowcount == 1)

    def transition_or_raise(self, campaign_id: int, to: CampaignStatus) -> None:
        """As :meth:`transition_status`, but raises when the transition is refused.

        Raises:
            InvalidStateTransition: With the campaign's actual current state, so
                the message can explain *why* rather than just that it failed.
        """
        if self.transition_status(campaign_id, to):
            return

        campaign = self.get(campaign_id)
        current = campaign.status if campaign else "missing"
        raise InvalidStateTransition(
            f"campaign {campaign_id} is {current!r}; cannot move to {to.value!r}",
            context={
                "campaign_id": campaign_id,
                "current": str(current),
                "target": to.value,
                "allowed": sorted(str(s) for s in allowed_transitions(campaign.status))
                if campaign
                else [],
            },
        )

    def begin_send(self, campaign_id: int) -> bool:
        """Claim a campaign for sending. ``False`` means someone else already did."""
        result = cast(
            "CursorResult[Any]",
            self.session.execute(
                update(CampaignORM)
                .where(
                    CampaignORM.id == campaign_id,
                    CampaignORM.status.in_(sorted(SENDABLE_STATES)),
                )
                .values(status=CampaignStatus.SENDING, updated_at=_utcnow())
            ),
        )
        return bool(result.rowcount == 1)

    def delete(self, campaign_id: int) -> None:
        campaign = self.get(campaign_id)
        if campaign is not None:
            self.session.delete(campaign)
            self.session.flush()

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, campaign_id: int) -> CampaignORM | None:
        return self.session.get(CampaignORM, campaign_id)

    def get_article_ids(self, campaign_id: int) -> list[int]:
        rows = self.session.execute(
            select(CampaignArticleORM.article_id)
            .where(CampaignArticleORM.campaign_id == campaign_id)
            .order_by(CampaignArticleORM.position)
        )
        return [row[0] for row in rows]

    def list_page(self, filters: CampaignFilter | None = None) -> Page[CampaignSummary]:
        """Paginated, filtered campaign list for the History page.

        Named ``list_page`` rather than ``list``: a method called ``list`` shadows
        the builtin inside the class body, so every later ``list[X]`` annotation
        in this class silently resolves to the method instead of the type.
        """
        f = filters or CampaignFilter()
        conditions = []

        if f.search:
            pattern = f"%{f.search.lower()}%"
            conditions.append(
                or_(
                    func.lower(CampaignORM.name).like(pattern),
                    func.lower(CampaignORM.subject).like(pattern),
                )
            )
        if f.statuses:
            conditions.append(CampaignORM.status.in_(f.statuses))
        if f.created_after:
            conditions.append(CampaignORM.created_at >= f.created_after)
        if f.created_before:
            conditions.append(CampaignORM.created_at <= f.created_before)

        total = self.session.execute(
            select(func.count()).select_from(CampaignORM).where(*conditions)
        ).scalar_one()

        rows = self.session.execute(
            select(CampaignORM)
            .where(*conditions)
            .order_by(CampaignORM.created_at.desc())
            .offset((f.page - 1) * f.page_size)
            .limit(f.page_size)
        ).scalars()

        return Page[CampaignSummary](
            items=[self._to_summary(row) for row in rows],
            total=total,
            page=f.page,
            page_size=f.page_size,
        )

    def recent(self, limit: int = 5) -> list[CampaignSummary]:
        rows = self.session.execute(
            select(CampaignORM).order_by(CampaignORM.created_at.desc()).limit(limit)
        ).scalars()
        return [self._to_summary(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.session.execute(
            select(CampaignORM.status, func.count()).group_by(CampaignORM.status)
        )
        return {str(status): count for status, count in rows}

    @staticmethod
    def _to_summary(row: CampaignORM) -> CampaignSummary:
        return CampaignSummary(
            id=row.id,
            name=row.name,
            status=CampaignStatus(row.status),
            subject=row.subject,
            recipient_count=row.recipient_count,
            sent_count=row.sent_count,
            failed_count=row.failed_count,
            created_at=row.created_at,
            sent_at=row.sent_at,
        )
