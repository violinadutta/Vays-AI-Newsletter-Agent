"""The last gate: sending approved campaigns when their time comes.

**Two conditions, both required, and neither optional:**

1. ``status == APPROVED`` — a human decided.
2. ``now >= next_send_after(approved_at)`` — the configured hour arrived.

They are checked in that order and expressed in one place, because a send that
happens for any other reason is mail a customer did not expect from a person who
did not authorise it.

The first condition is not really enforced here. ``AWAITING_APPROVAL`` and
``REJECTED`` are absent from ``SENDABLE_STATES``, so the repository's guarded
UPDATE refuses them regardless of what this file does — the check below is the
polite refusal, and the database is the guarantee.

Sending itself is the existing ``DeliveryService``: batching, pacing, retry,
suppression and per-recipient results are all unchanged. Nothing about delivery
differs because a timer started it rather than a button.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from config import bind_correlation_id, get_logger, get_settings
from core.enums import CampaignStatus
from core.exceptions import NewsletterAppError
from core.models import CampaignSendReport
from modules.repository.database import unit_of_work
from modules.repository.orm_models import CampaignORM

log = get_logger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Label a naive datetime from SQLite as UTC — it is one by construction."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class DueCampaign:
    """An approved campaign and whether its send window has opened."""

    campaign_id: int
    name: str
    approved_at: datetime
    due_at: datetime

    @property
    def is_due(self) -> bool:
        return datetime.now(UTC) >= self.due_at


@dataclass
class DispatchReport:
    """What one dispatch pass did."""

    considered: int = 0
    sent: list[int] = field(default_factory=list)
    waiting: list[DueCampaign] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    recipients_sent: int = 0


class DispatchService:
    """Sends approved campaigns once their configured send time arrives."""

    def __init__(self, delivery: object | None = None) -> None:
        self._delivery = delivery

    # ── the gate ─────────────────────────────────────────────────────────────
    @staticmethod
    def approved_campaigns() -> list[DueCampaign]:
        """Every approved campaign with the moment it becomes sendable.

        Campaigns approved before ``approved_at`` existed fall back to
        ``updated_at``. That is only reachable for rows created before this
        column was added, and treating them as approved-now would hold them an
        extra day for no reason.
        """
        settings = get_settings().agent

        with unit_of_work() as session:
            rows = (
                session.execute(
                    select(CampaignORM)
                    .where(CampaignORM.status == CampaignStatus.APPROVED)
                    .order_by(CampaignORM.approved_at.asc().nulls_first())
                )
                .scalars()
                .all()
            )
            due = []
            for row in rows:
                approved_at = _as_utc(row.approved_at or row.updated_at)
                due.append(
                    DueCampaign(
                        campaign_id=int(row.id),
                        name=row.name,
                        approved_at=approved_at,
                        due_at=settings.next_send_after(approved_at),
                    )
                )
            return due

    # ── the run ──────────────────────────────────────────────────────────────
    def dispatch_due(self) -> DispatchReport:
        """Send every approved campaign whose window has opened. Never raises.

        Called on a timer, so a failure must be recorded and survived rather
        than propagated — there is nobody to catch it.
        """
        correlation_id = bind_correlation_id()
        report = DispatchReport()

        if not get_settings().agent.enabled:
            log.info("dispatch.skipped", reason="agent disabled")
            return report

        candidates = self.approved_campaigns()
        report.considered = len(candidates)

        for candidate in candidates:
            if not candidate.is_due:
                report.waiting.append(candidate)
                continue
            self._send_one(candidate, report)

        if report.sent or report.failed:
            log.info(
                "dispatch.complete",
                sent=len(report.sent),
                failed=len(report.failed),
                waiting=len(report.waiting),
                recipients=report.recipients_sent,
                correlation_id=correlation_id,
            )
        return report

    def _send_one(self, candidate: DueCampaign, report: DispatchReport) -> None:
        from services.campaign_service import CampaignService
        from services.delivery_service import DeliveryService

        campaign_id = candidate.campaign_id
        log.info("dispatch.starting", campaign_id=campaign_id, due_at=candidate.due_at.isoformat())

        try:
            content = CampaignService().get_content(campaign_id)
            service = self._delivery or DeliveryService()
            result: CampaignSendReport = service.send_campaign(campaign_id, content)  # type: ignore[attr-defined]
        except NewsletterAppError as exc:
            report.failed.append((campaign_id, exc.user_message))
            log.warning("dispatch.failed", campaign_id=campaign_id, reason=exc.message[:300])
            return
        except Exception as exc:  # noqa: BLE001 - one campaign must not end the pass
            report.failed.append((campaign_id, "Unexpected error — see the Logs page."))
            log.exception("dispatch.error", campaign_id=campaign_id, error=type(exc).__name__)
            return

        report.sent.append(campaign_id)
        report.recipients_sent += result.sent
        log.info(
            "dispatch.sent",
            campaign_id=campaign_id,
            attempted=result.attempted,
            sent=result.sent,
            failed=result.failed,
        )

    # ── reporting ────────────────────────────────────────────────────────────
    @staticmethod
    def next_due() -> DueCampaign | None:
        """The campaign that will go out soonest, for the dashboard."""
        pending = sorted(DispatchService.approved_campaigns(), key=lambda c: c.due_at)
        return pending[0] if pending else None
