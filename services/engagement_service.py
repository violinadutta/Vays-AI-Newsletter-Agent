"""What a recipient does with a delivered email: likes it, or opts out.

Both arrive the same way — a click on a signed link in the email — so both are
handled here, and both go through :meth:`EngagementService.apply`.

**Confirmation is required, and that is a correctness property rather than a
nicety.** Gmail and Outlook prefetch links in messages with security scanners,
so a URL that acts on GET would be fired by a robot: people would be
unsubscribed without asking and likes would be recorded that nobody clicked.
:meth:`inspect` is what a scanner reaches — it only reads — and :meth:`apply` is
reached only by a form submission, which a scanner does not make.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import get_logger, get_settings
from core.auth import sign_recipient_token, verify_recipient_token
from core.enums import EmailAction, SuppressionReason
from modules.repository.database import unit_of_work
from modules.repository.event_repo import EmailEventRepository
from modules.tunnel import resolve_base_url

log = get_logger(__name__)

#: Query-parameter name carrying the token. Same for both actions: the action
#: itself is inside the signed payload, so it cannot be changed by editing the
#: URL, and a single name keeps the links uniform.
TOKEN_PARAM = "t"  # noqa: S105 - a query parameter name, not a secret

#: Recipient links land on the *root* path with only this parameter, and the
#: action is read from inside the signed token. Two reasons, both practical:
#: the app gates every page behind a login and a customer can never have an
#: account, so exactly one public entry point is easier to reason about than a
#: set of paths; and an action carried in the signature cannot be changed by
#: editing the URL, which a path segment could be.


@dataclass(frozen=True)
class LinkCheck:
    """The result of inspecting a link, before anything is written."""

    valid: bool
    email: str = ""
    campaign_id: int = 0
    action: EmailAction | None = None
    already_done: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ActionResult:
    """The outcome of a confirmed click."""

    ok: bool
    changed: bool = False
    message: str = ""


class EngagementService:
    """Issues recipient links and applies the actions behind them."""

    def link(self, email: str, campaign_id: int, action: EmailAction) -> str:
        """The URL to put in an email for this recipient, campaign and action."""
        settings = get_settings()
        token = sign_recipient_token(
            email, campaign_id, str(action), settings.app.secret_key.get_secret_value()
        )
        base = resolve_base_url(settings.agent.app_base_url)
        return f"{base}/?{TOKEN_PARAM}={token}"

    def inspect(self, token: str, action: EmailAction | None = None) -> LinkCheck:
        """Validate a link **without changing anything**.

        This is the read-only half, and the half a mail scanner's prefetch will
        reach. It must stay free of side effects.

        ``action`` is optional: with none given it is read from the verified
        payload, which is safe because the signature covers it. Passing one
        binds the check to a caller that already knows which action it handles.
        """
        payload = verify_recipient_token(
            token,
            get_settings().app.secret_key.get_secret_value(),
            expected_action=str(action) if action is not None else None,
        )
        if payload is None:
            # Forged, tampered, expired, or minted for the other action — all
            # reported identically, because distinguishing them tells whoever is
            # probing which part they got right.
            return LinkCheck(valid=False, reason="This link is not valid.")

        email = str(payload.get("e", ""))
        campaign_id = int(payload.get("c", 0))
        try:
            resolved = action if action is not None else EmailAction(str(payload.get("a", "")))
        except ValueError:
            # A signed token naming an action this build does not have. Possible
            # after a rename; treated as invalid rather than guessed at.
            return LinkCheck(valid=False, reason="This link is not valid.")
        if not email or not campaign_id:
            return LinkCheck(valid=False, reason="This link is not valid.")

        with unit_of_work() as session:
            already = EmailEventRepository(session).has(email, campaign_id, resolved)

        return LinkCheck(
            valid=True,
            email=email,
            campaign_id=campaign_id,
            action=resolved,
            already_done=already,
        )

    def apply(
        self, token: str, action: EmailAction | None = None, *, user_agent: str | None = None
    ) -> ActionResult:
        """Record the action. Only ever called from a confirmed submission.

        Re-validates the token rather than trusting the earlier
        :meth:`inspect` — the two are separated by a round trip through the
        browser, and nothing that crossed it can be assumed unchanged.
        """
        check = self.inspect(token, action)
        if not check.valid or check.action is None:
            return ActionResult(ok=False, message=check.reason or "This link is not valid.")
        resolved = check.action

        with unit_of_work() as session:
            recorded = EmailEventRepository(session).record(
                check.email, check.campaign_id, resolved, user_agent=user_agent
            )

        if resolved is EmailAction.UNSUBSCRIBED:
            # Suppression is applied even when the event row already existed. The
            # event is a record of the click; the suppression is what actually
            # stops mail. If those ever disagree — an event written but a
            # suppression missed — the person keeps receiving email, so this is
            # deliberately not inside the `if recorded` branch.
            self._stop_sending(check.email)

        log.info(
            "engagement.recorded",
            action=str(resolved),
            campaign_id=check.campaign_id,
            first_time=recorded,
        )
        return ActionResult(
            ok=True,
            changed=recorded,
            message=self._message(resolved, first_time=recorded),
        )

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _stop_sending(email: str) -> None:
        """Suppress the address and take it off the master list.

        Both, because they answer different questions. The suppression list is
        checked before every send and survives a re-uploaded CSV; deactivating
        the subscriber keeps the recipient count honest on the dashboard.
        """
        from services.delivery_service import DeliveryService

        DeliveryService.suppress(email, SuppressionReason.UNSUBSCRIBED)

        try:
            from services.subscriber_service import SubscriberService

            SubscriberService().remove(email, removed_by="unsubscribe-link")
        except Exception:  # noqa: BLE001 - suppression already stops the mail
            # The suppression above is what enforces the opt-out. Failing to
            # tidy the master list must not turn a successful unsubscribe into
            # an error page for someone who has every right to leave.
            log.warning("engagement.deactivate_failed", exc_info=True)

    @staticmethod
    def _message(action: EmailAction, *, first_time: bool) -> str:
        if action is EmailAction.UNSUBSCRIBED:
            return (
                "You have been unsubscribed. You will not receive further newsletters."
                if first_time
                else "You were already unsubscribed. No further newsletters will be sent."
            )
        return (
            "Thanks — glad you found it useful."
            if first_time
            else "You have already liked this newsletter. Thanks again."
        )
