"""Brevo transactional email (D-10).

Chosen for the most generous permanent free tier (300/day) and because marketing
staff can manage lists in its UI without a developer.

Implemented directly on ``httpx`` rather than the vendor SDK, for the same reason
as the LLM client: the surface we need is one POST, and the SDK would obscure the
error mapping that turns a 402 into *"the daily limit is reached; 240 of 487 were
sent"*.
"""

from __future__ import annotations

import httpx

from config import get_logger, get_settings
from core.enums import SendStatus
from core.exceptions import EmailAuthError, EmailProviderError, EmailQuotaExceeded
from core.models import EmailMessage, HealthStatus, SendResult
from modules.email.base import EmailProvider

log = get_logger(__name__)

BREVO_API = "https://api.brevo.com/v3"

#: Provider responses that mean *this address*, not *this account*. They become a
#: per-recipient FAILED result so the rest of the batch continues.
_RECIPIENT_ERRORS = frozenset(
    {"invalid_parameter", "blocked_domain", "blocked_contact", "invalid_email"}
)


class BrevoEmailProvider(EmailProvider):
    """Sends through Brevo's transactional API."""

    name = "brevo"

    def __init__(self, client: httpx.Client | None = None) -> None:
        settings = get_settings().email
        self._sender = {"name": settings.sender_name, "email": settings.sender_address}
        self._reply_to = settings.reply_to
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=30.0,
            headers={
                "api-key": settings.brevo_api_key.get_secret_value(),
                "content-type": "application/json",
                "accept": "application/json",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def verify_credentials(self) -> HealthStatus:
        try:
            response = self._client.get(f"{BREVO_API}/account", timeout=10.0)
        except httpx.HTTPError as exc:
            return HealthStatus(
                healthy=False, detail=f"Could not reach Brevo ({type(exc).__name__})."
            )

        if response.status_code == 401:
            return HealthStatus(healthy=False, detail="Brevo rejected the API key.")
        if response.status_code >= 400:
            return HealthStatus(
                healthy=False, detail=f"Brevo returned HTTP {response.status_code}."
            )

        # Surfacing remaining credit turns "the send stopped" into "you have 60
        # sends left today", which is a different conversation.
        try:
            plan = (response.json().get("plan") or [{}])[0]
            credits = plan.get("credits")
            detail = f"connected ({credits} credits)" if credits is not None else "connected"
        except (ValueError, KeyError, IndexError, TypeError):
            detail = "connected"
        return HealthStatus(healthy=True, detail=detail)

    def send(self, message: EmailMessage) -> SendResult:
        payload: dict[str, object] = {
            "sender": self._sender,
            "to": [
                {
                    "email": message.to_email,
                    **({"name": message.to_name} if message.to_name else {}),
                }
            ],
            "subject": message.subject,
            "htmlContent": message.html,
            "textContent": message.text,
        }
        if message.headers:
            payload["headers"] = message.headers
        if message.tags:
            payload["tags"] = message.tags
        if self._reply_to:
            payload["replyTo"] = {"email": self._reply_to}

        try:
            response = self._client.post(f"{BREVO_API}/smtp/email", json=payload)
        except httpx.HTTPError as exc:
            # Network faults are per-attempt, not per-account: the batcher retries.
            return SendResult(
                email=message.to_email,
                status=SendStatus.FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc)[:200],
            )

        return self._interpret(response, message)

    def _interpret(self, response: httpx.Response, message: EmailMessage) -> SendResult:
        if response.status_code in (200, 201):
            try:
                message_id = response.json().get("messageId")
            except ValueError:
                message_id = None
            return SendResult(
                email=message.to_email, status=SendStatus.SENT, provider_message_id=message_id
            )

        body = response.text[:300]

        # These two abort the batch: every remaining send would fail identically,
        # and continuing would just take longer to reach the same conclusion.
        if response.status_code == 401:
            raise EmailAuthError(f"Brevo rejected the API key: {body}", context={"status": 401})
        if response.status_code == 402:
            raise EmailQuotaExceeded(f"Brevo credits exhausted: {body}", context={"status": 402})

        try:
            code = str(response.json().get("code", ""))
        except ValueError:
            code = ""

        if response.status_code == 400 and code in _RECIPIENT_ERRORS:
            # This address is bad; the account is fine. Record and move on.
            return SendResult(
                email=message.to_email,
                status=SendStatus.FAILED,
                error_code=code,
                error_message=_recipient_message(code),
            )

        if response.status_code == 429 or response.status_code >= 500:
            raise EmailProviderError(
                f"Brevo returned HTTP {response.status_code}: {body}",
                context={"status": response.status_code},
            )

        return SendResult(
            email=message.to_email,
            status=SendStatus.FAILED,
            error_code=code or str(response.status_code),
            error_message=body,
        )


def _recipient_message(code: str) -> str:
    """A reason the operator can act on, shown in the failures table."""
    return {
        "invalid_email": "The address isn't valid.",
        "invalid_parameter": "The address was rejected as invalid.",
        "blocked_domain": "That domain is blocked.",
        "blocked_contact": "This contact previously unsubscribed or bounced.",
    }.get(code, "The email provider rejected this address.")
