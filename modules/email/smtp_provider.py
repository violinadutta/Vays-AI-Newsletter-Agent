"""SMTP provider — for Mailtrap sandbox testing and any company mail server.

Kept alongside Brevo because a sandbox that catches mail without delivering it is
the only safe way to test a real send path, and because Vays may prefer their own
mail server at handover. Same interface either way.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage as MIMEMessage
from email.utils import formatdate, make_msgid

from config import get_logger, get_settings
from core.enums import SendStatus
from core.exceptions import EmailAuthError
from core.models import EmailMessage, HealthStatus, SendResult
from modules.email.base import EmailProvider

log = get_logger(__name__)


class SMTPEmailProvider(EmailProvider):
    """Sends over SMTP with STARTTLS.

    A connection is opened per send rather than held open. Slower, and correct:
    a long-lived SMTP connection across a multi-minute batch gets dropped by
    servers and firewalls, and the failure surfaces as a confusing mid-batch
    error rather than a clean per-message one.
    """

    name = "smtp"

    def __init__(self) -> None:
        settings = get_settings().email
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username
        self._password = settings.smtp_password.get_secret_value()
        self._use_tls = settings.smtp_use_tls
        self._sender_name = settings.sender_name
        self._sender_address = settings.sender_address
        self._reply_to = settings.reply_to

    def verify_credentials(self) -> HealthStatus:
        if not self._host:
            return HealthStatus(healthy=False, detail="SMTP_HOST is not set.")
        try:
            with self._connect() as server:
                server.noop()
        except smtplib.SMTPAuthenticationError:
            return HealthStatus(healthy=False, detail="The SMTP server rejected the credentials.")
        except (smtplib.SMTPException, OSError) as exc:
            return HealthStatus(
                healthy=False,
                detail=f"Could not reach {self._host}:{self._port} ({type(exc).__name__}).",
            )
        return HealthStatus(healthy=True, detail=f"connected to {self._host}:{self._port}")

    def _connect(self) -> smtplib.SMTP:
        server = smtplib.SMTP(self._host, self._port, timeout=30)
        if self._use_tls:
            server.starttls(context=ssl.create_default_context())
        if self._username:
            server.login(self._username, self._password)
        return server

    def send(self, message: EmailMessage) -> SendResult:
        mime = MIMEMessage()
        mime["From"] = f"{self._sender_name} <{self._sender_address}>"
        mime["To"] = (
            f"{message.to_name} <{message.to_email}>" if message.to_name else message.to_email
        )
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=True)
        message_id = make_msgid()
        mime["Message-ID"] = message_id
        if self._reply_to:
            mime["Reply-To"] = self._reply_to
        for key, value in message.headers.items():
            mime[key] = value

        mime.set_content(message.text)
        mime.add_alternative(message.html, subtype="html")

        try:
            with self._connect() as server:
                server.send_message(mime)
        except smtplib.SMTPAuthenticationError as exc:
            # Account-level: every remaining send fails identically.
            raise EmailAuthError(
                f"SMTP authentication failed: {exc}", context={"host": self._host}
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(
                email=message.to_email,
                status=SendStatus.FAILED,
                error_code="recipient_refused",
                error_message=str(exc)[:200],
            )
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(
                email=message.to_email,
                status=SendStatus.FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc)[:200],
            )

        return SendResult(
            email=message.to_email, status=SendStatus.SENT, provider_message_id=message_id
        )
