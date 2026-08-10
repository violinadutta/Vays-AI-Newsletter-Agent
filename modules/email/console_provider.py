"""Console provider — writes ``.eml`` files and sends nothing.

The default provider (see ``.env.example``), so that a fresh checkout **cannot**
email a real customer. That is not a convenience; it is the difference between a
mistake during development and an apology to 500 recipients.

The files are real RFC 5322 messages with both MIME parts, so they open in
Outlook, Thunderbird or Apple Mail exactly as a delivered message would. That
makes them the cheapest possible way to check rendering — no provider account, no
inbox, no send.
"""

from __future__ import annotations

import re
import uuid
from email.message import EmailMessage as MIMEMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from config import get_logger, get_settings
from config.constants import OUTBOX_DIR
from core.enums import SendStatus
from core.models import EmailMessage, HealthStatus, SendResult
from modules.email.base import EmailProvider, build_mime_body

log = get_logger(__name__)

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


class ConsoleEmailProvider(EmailProvider):
    """Writes each message to ``data/outbox/`` as a ``.eml`` file."""

    name = "console"

    def __init__(self, outbox: Path | None = None) -> None:
        self.outbox = outbox or OUTBOX_DIR
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.sent: list[EmailMessage] = []

    def verify_credentials(self) -> HealthStatus:
        return HealthStatus(
            healthy=True, detail=f"console — writes .eml to {self.outbox}, sends nothing"
        )

    def send(self, message: EmailMessage) -> SendResult:
        settings = get_settings().email
        mime = MIMEMessage()
        mime["From"] = f"{settings.sender_name} <{settings.sender_address}>"
        mime["To"] = (
            f"{message.to_name} <{message.to_email}>" if message.to_name else message.to_email
        )
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=True)
        mime["Message-ID"] = make_msgid(domain="vays.local")
        if settings.reply_to:
            mime["Reply-To"] = settings.reply_to
        for key, value in message.headers.items():
            mime[key] = value

        build_mime_body(mime, message)

        path = self.outbox / self._filename(message)
        path.write_bytes(mime.as_bytes())
        self.sent.append(message)

        log.info("email.written", path=str(path), to=_mask(message.to_email))
        return SendResult(
            email=message.to_email,
            status=SendStatus.SENT,
            provider_message_id=f"console-{uuid.uuid4().hex[:12]}",
        )

    def _filename(self, message: EmailMessage) -> str:
        safe = _UNSAFE_FILENAME.sub("_", message.to_email)
        return f"{safe}-{uuid.uuid4().hex[:8]}.eml"


def _mask(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"
