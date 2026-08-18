"""Management approval: issuing review links, and recording the decision.

**The link does not approve anything.** It opens the review page, which requires
a login and an approver role before the buttons do anything. A URL that approves
on GET would mean an email forward, a link preview fetcher, or a corporate
security scanner clicking it could dispatch a campaign to customers — and those
scanners follow every link in every message.

So the token is a *pointer with an expiry*, not an authorisation:

* **Cryptographically random** — ``secrets.token_urlsafe(32)``, 256 bits.
* **Stored hashed** — only sha256 is written down, so a database read, a backup
  or a screenshot of a query yields nothing usable. Same reasoning as passwords.
* **Single use** — spent the moment a decision is recorded, so a forwarded link
  cannot be replayed to reverse someone else's call.
* **Expiring** — configurable, default 72 hours. An expired link is refused, and
  refusing means *not sent*.
* **Campaign-scoped** — it names one campaign and cannot reach another.
* **Server-validated** — the URL carries the token and nothing else. No campaign
  content, no recipient data, no counts, no secrets.

Comparison is constant-time. Token lookup by hash equality is not a timing
oracle in practice, but ``compare_digest`` costs nothing and removes the
argument.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_logger, get_settings
from core.enums import SENDING_ROLES, CampaignStatus, UserRole
from core.exceptions import ValidationError
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.orm_models import ApprovalTokenORM

log = get_logger(__name__)

#: Query parameter carrying the token. Deliberately opaque — it names nothing
#: about the campaign it points at.
TOKEN_PARAM = "review"  # noqa: S105 - a query parameter name, not a secret


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a datetime read back from the database.

    **SQLite stores no timezone**, so a column declared ``DateTime(timezone=True)``
    round-trips as *naive* — and comparing that to an aware ``now(UTC)`` raises
    ``TypeError`` rather than returning a wrong answer.

    Everything here is written as ``datetime.now(UTC)``, so a naive value read
    back is UTC by construction and labelling it as such is correct rather than
    an assumption. Without this, every expiry check would crash — which is
    exactly what the test suite caught.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ApprovalLink:
    """A freshly issued review link. The raw token exists only here and in the
    email — it is never persisted."""

    token: str
    url: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenCheck:
    """Outcome of validating a token from a URL."""

    valid: bool
    campaign_id: int | None = None
    reason: str = ""

    @property
    def rejected(self) -> bool:
        return not self.valid


class ApprovalService:
    """Issues review links and records approve/reject decisions."""

    # ── issuing ──────────────────────────────────────────────────────────────
    def issue(self, campaign_id: int) -> ApprovalLink:
        """Create a review link for one campaign.

        Any earlier unused token for the same campaign is spent first. Two live
        links for one campaign means a stale email can still act on it after a
        newer request superseded it.
        """
        settings = get_settings().agent
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=settings.approval_token_ttl_hours)

        with unit_of_work() as session:
            for stale in session.execute(
                select(ApprovalTokenORM).where(
                    ApprovalTokenORM.campaign_id == campaign_id,
                    ApprovalTokenORM.used_at.is_(None),
                )
            ).scalars():
                stale.used_at = datetime.now(UTC)
                stale.decision = "superseded"

            session.add(
                ApprovalTokenORM(
                    token_hash=_hash(token),
                    campaign_id=campaign_id,
                    expires_at=expires_at,
                )
            )

        log.info("approval.link_issued", campaign_id=campaign_id, expires_at=expires_at.isoformat())
        return ApprovalLink(token=token, url=review_url(token), expires_at=expires_at)

    # ── validating ───────────────────────────────────────────────────────────
    def check(self, token: str) -> TokenCheck:
        """Validate a token from a URL without spending it.

        Never raises and never says *why* beyond a generic reason: a message
        distinguishing "no such token" from "expired" tells someone probing the
        endpoint which guesses were closer.
        """
        if not token or not token.strip():
            return TokenCheck(valid=False, reason="No review link was supplied.")

        with unit_of_work() as session:
            row = self._find(session, token)

            if row is None:
                return TokenCheck(valid=False, reason="This review link isn't valid.")
            if row.used_at is not None:
                return TokenCheck(
                    valid=False,
                    campaign_id=row.campaign_id,
                    reason="This review link has already been used.",
                )
            if _as_utc(row.expires_at) <= datetime.now(UTC):
                return TokenCheck(
                    valid=False,
                    campaign_id=row.campaign_id,
                    reason=(
                        "This review link has expired, so the campaign was not sent. "
                        "Open the Approvals page to decide on it."
                    ),
                )
            return TokenCheck(valid=True, campaign_id=row.campaign_id)

    @staticmethod
    def _find(session: Session, token: str) -> ApprovalTokenORM | None:
        """Look a token up by hash, comparing in constant time."""
        candidate = _hash(token.strip())
        row: ApprovalTokenORM | None = session.execute(
            select(ApprovalTokenORM).where(ApprovalTokenORM.token_hash == candidate)
        ).scalar_one_or_none()
        if row is None or not secrets.compare_digest(row.token_hash, candidate):
            return None
        return row

    # ── deciding ─────────────────────────────────────────────────────────────
    def approve(
        self, campaign_id: int, *, by: str, role: UserRole, token: str | None = None
    ) -> None:
        """Mark a campaign approved. It becomes eligible at the next send time.

        Raises:
            ValidationError: The user may not approve, or the campaign is not
                awaiting a decision.
        """
        self._decide(campaign_id, CampaignStatus.APPROVED, by=by, role=role, token=token)

    def reject(
        self, campaign_id: int, *, by: str, role: UserRole, token: str | None = None
    ) -> None:
        """Mark a campaign rejected. It is never sent and never re-offered."""
        self._decide(campaign_id, CampaignStatus.REJECTED, by=by, role=role, token=token)

    def _decide(
        self,
        campaign_id: int,
        decision: CampaignStatus,
        *,
        by: str,
        role: UserRole,
        token: str | None,
    ) -> None:
        # Authorisation is the session's role, never the token. Holding a link
        # is not permission to send mail to customers.
        if role not in SENDING_ROLES:
            raise ValidationError(
                f"{by!r} has role {role} and may not approve campaigns",
                user_message=(
                    "Your account can't approve campaigns. That needs an approver or admin role."
                ),
            )

        with unit_of_work() as session:
            repo = CampaignRepository(session)
            campaign = repo.get(campaign_id)
            if campaign is None:
                raise ValidationError(
                    f"campaign {campaign_id} not found",
                    user_message="That campaign no longer exists.",
                )
            if campaign.status != CampaignStatus.AWAITING_APPROVAL:
                raise ValidationError(
                    f"campaign {campaign_id} is {campaign.status}, not awaiting approval",
                    user_message=_already_decided_message(str(campaign.status)),
                )

            repo.transition_or_raise(campaign_id, decision)

            if decision is CampaignStatus.APPROVED:
                # Stamped once, here, and never touched again. The send gate is
                # computed from it, so a later edit to the row must not move the
                # scheduled send.
                campaign.approved_at = datetime.now(UTC)
                campaign.approved_by = by

            # Spend every live token for this campaign, not only the one used:
            # a decision has been made, so no other link should still work.
            for row in session.execute(
                select(ApprovalTokenORM).where(
                    ApprovalTokenORM.campaign_id == campaign_id,
                    ApprovalTokenORM.used_at.is_(None),
                )
            ).scalars():
                row.used_at = datetime.now(UTC)
                row.used_by = by
                row.decision = str(decision)

        log.info(
            "approval.decision",
            campaign_id=campaign_id,
            decision=str(decision),
            by=by,
            via_link=token is not None,
        )

    # ── housekeeping ─────────────────────────────────────────────────────────
    @staticmethod
    def pending_campaigns() -> list[int]:
        """Campaigns waiting on a human, oldest first."""
        with unit_of_work() as session:
            from modules.repository.orm_models import CampaignORM

            rows = session.execute(
                select(CampaignORM.id)
                .where(CampaignORM.status == CampaignStatus.AWAITING_APPROVAL)
                .order_by(CampaignORM.created_at.asc())
            ).scalars()
            return [int(r) for r in rows]


class ApprovalNotifier:
    """Renders and sends the review request to management.

    **Deliberately not routed through ``TemplateRenderer``.** That path enforces
    marketing-email compliance — an unsubscribe link and a postal address in
    every message — which is right for a customer newsletter and wrong here.
    This is internal operational mail to one configured colleague; giving it an
    unsubscribe link would invite someone to opt out of their own approval
    requests, and the campaign would then wait forever with nobody noticing.

    It carries no recipient data. The reviewer sees a count, never an address.
    """

    def __init__(self) -> None:
        from jinja2 import FileSystemLoader, StrictUndefined, select_autoescape
        from jinja2.sandbox import SandboxedEnvironment

        from config.constants import INTERNAL_TEMPLATES_DIR

        # Same posture as the newsletter renderer: the campaign subject and body
        # excerpt below are LLM output derived from a scraped page, so they are
        # untrusted and must be escaped.
        self._env = SandboxedEnvironment(
            loader=FileSystemLoader(INTERNAL_TEMPLATES_DIR),
            autoescape=select_autoescape(default_for_string=True, default=True),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def send(self, campaign_id: int, recipient_count: int, link: ApprovalLink) -> bool:
        """Send the request. Returns whether it was accepted for delivery.

        Never raises: a failure to notify must leave the campaign parked and
        retryable, not lose the draft that was just paid for in tokens.
        """
        from modules.email.factory import create_email_provider
        from modules.repository.orm_models import DiscoveredPostORM

        settings = get_settings()
        to_address = settings.agent.approval_email.strip()
        if not to_address:
            log.error("approval.no_recipient_configured", campaign_id=campaign_id)
            return False

        with unit_of_work() as session:
            campaign = CampaignRepository(session).get(campaign_id)
            if campaign is None:
                return False
            source = (
                session.execute(
                    select(DiscoveredPostORM).where(DiscoveredPostORM.campaign_id == campaign_id)
                )
                .scalars()
                .first()
            )
            context = self._context(campaign, source, recipient_count, link)

        try:
            html = self._env.get_template("approval_request.html").render(**context)
        except Exception:  # noqa: BLE001 - a template fault must not lose the draft
            log.exception("approval.render_failed", campaign_id=campaign_id)
            return False

        from core.models import EmailMessage

        message = EmailMessage(
            to_email=to_address,
            subject=f"[Approval needed] {context['subject']}",
            html=html,
            text=_plain_text(context),
            # No List-Unsubscribe: this is internal operational mail, not a
            # marketing send, and the headers would be both wrong and harmful.
        )

        provider = None
        try:
            provider = create_email_provider()
            result = provider.send(message)
        except Exception:  # noqa: BLE001 - same reason
            log.exception("approval.send_failed", campaign_id=campaign_id)
            return False
        finally:
            if provider is not None:
                provider.close()

        log.info("approval.request_sent", campaign_id=campaign_id, ok=result.ok)
        return bool(result.ok)

    @staticmethod
    def _context(
        campaign: object, source: object, recipient_count: int, link: ApprovalLink
    ) -> dict[str, object]:
        from modules.template.brand import resolve_brand

        settings = get_settings().agent
        body = str(getattr(campaign, "newsletter", "") or "")
        excerpt = body.strip().split("\n\n")[0][:400]

        return {
            "brand": resolve_brand(),
            "subject": getattr(campaign, "subject", "") or "(no subject)",
            "preview_text": getattr(campaign, "preview_text", "") or "—",
            "excerpt": excerpt or "(no body text)",
            "recipient_count": recipient_count,
            "model_name": getattr(campaign, "model_name", None) or "the AI service",
            "source_title": getattr(source, "title", None),
            "source_url": getattr(source, "url", None),
            "review_url": link.url,
            "send_time_label": settings.describe_schedule(),
            "expires_label": link.expires_at.astimezone(settings.zone).strftime("%d %b at %H:%M"),
        }


def _plain_text(context: dict[str, object]) -> str:
    """The text alternative, composed from the same fields (D-16)."""
    lines = [
        "A newsletter is ready for your review.",
        "",
        "Nothing has been sent. It goes out only if you approve it, and then not",
        f"until {context['send_time_label']}.",
        "",
        f"Subject   : {context['subject']}",
        f"Preview   : {context['preview_text']}",
        f"Recipients: {context['recipient_count']}",
        f"Written by: {context['model_name']}",
    ]
    if context.get("source_url"):
        lines.append(f"Source    : {context['source_url']}")
    lines += [
        "",
        "Opening lines:",
        str(context["excerpt"]),
        "",
        "Review and approve:",
        str(context["review_url"]),
        "",
        f"The link works once and expires {context['expires_label']}.",
        "An expired link means the campaign is not sent.",
    ]
    return "\n".join(lines)


def _already_decided_message(status: str) -> str:
    """Explain why no decision is possible, in the reviewer's terms.

    Phrased per state rather than interpolating the raw status, which produces
    things like "already draft" — technically accurate and not English.
    """
    return {
        str(CampaignStatus.APPROVED): (
            "This campaign has already been approved and is waiting to be sent."
        ),
        str(CampaignStatus.REJECTED): (
            "This campaign was already rejected, so it will not be sent."
        ),
        str(CampaignStatus.SENDING): "This campaign is being sent right now.",
        str(CampaignStatus.SENT): "This campaign has already been sent.",
        str(CampaignStatus.DRAFT): (
            "This campaign is still a draft and has not been submitted for approval. "
            "Open it on the Preview page instead."
        ),
    }.get(
        status,
        f"This campaign is {status.lower().replace('_', ' ')}, so there is nothing to decide.",
    )


def review_url(token: str) -> str:
    """The absolute URL of the review page for a token.

    Built from ``AGENT_APP_BASE_URL`` because an email is read somewhere other
    than the machine the app runs on, and a relative link is unclickable there.
    Set to ``auto``, the current ngrok tunnel is looked up at this moment rather
    than trusted from configuration — a free-tier tunnel changes hostname on
    every restart, and a stale one produces emails with dead buttons.
    The token is the only parameter — no campaign id, no counts, nothing that
    would leak from a forwarded message or a proxy log.
    """
    from modules.tunnel import resolve_base_url

    base = resolve_base_url(get_settings().agent.app_base_url)
    return f"{base}/approvals?{urlencode({TOKEN_PARAM: token})}"
