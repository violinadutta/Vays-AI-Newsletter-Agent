"""Email delivery. Batching and retry live in one place, shared by every adapter."""

from modules.email.base import EmailProvider, unsubscribe_headers
from modules.email.batcher import BatchSender
from modules.email.brevo_provider import BrevoEmailProvider
from modules.email.console_provider import ConsoleEmailProvider
from modules.email.factory import available_email_providers, create_email_provider
from modules.email.smtp_provider import SMTPEmailProvider

__all__ = [
    "BatchSender",
    "BrevoEmailProvider",
    "ConsoleEmailProvider",
    "EmailProvider",
    "SMTPEmailProvider",
    "available_email_providers",
    "create_email_provider",
    "unsubscribe_headers",
]
