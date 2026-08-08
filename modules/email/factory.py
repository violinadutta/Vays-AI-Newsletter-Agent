"""Email provider selection.

Adding a provider means adding a class and one line here — no call site changes
(NFR-M5). The console default is a safety property, not a convenience: a fresh
checkout must not be able to email a real customer.
"""

from __future__ import annotations

from config import get_logger, get_settings
from core.exceptions import ConfigurationError
from modules.email.base import EmailProvider
from modules.email.brevo_provider import BrevoEmailProvider
from modules.email.console_provider import ConsoleEmailProvider
from modules.email.smtp_provider import SMTPEmailProvider

log = get_logger(__name__)

_PROVIDERS: dict[str, type[EmailProvider]] = {
    "brevo": BrevoEmailProvider,
    "smtp": SMTPEmailProvider,
    "console": ConsoleEmailProvider,
}


def create_email_provider(name: str | None = None) -> EmailProvider:
    """Build the configured email provider.

    Raises:
        ConfigurationError: Unknown provider, or a real one missing its
            credentials — caught at startup rather than half-way through a send.
    """
    settings = get_settings().email
    key = (name or settings.provider).lower()

    provider_cls = _PROVIDERS.get(key)
    if provider_cls is None:
        raise ConfigurationError(
            f"unknown email provider {key!r}; valid values are {sorted(_PROVIDERS)}",
            user_message=(
                f"EMAIL_PROVIDER is set to '{key}', which isn't valid. "
                f"Use one of: {', '.join(sorted(_PROVIDERS))}."
            ),
        )

    if key == "brevo" and not settings.brevo_api_key.get_secret_value():
        raise ConfigurationError(
            "EMAIL_PROVIDER=brevo requires BREVO_API_KEY",
            user_message=(
                "Sending through Brevo needs an API key. Add BREVO_API_KEY to your "
                ".env file, or use EMAIL_PROVIDER=console to write .eml files instead."
            ),
        )
    if key == "smtp" and not settings.smtp_host:
        raise ConfigurationError(
            "EMAIL_PROVIDER=smtp requires SMTP_HOST",
            user_message=(
                "Sending over SMTP needs a server address. Set SMTP_HOST in your "
                ".env file, or use EMAIL_PROVIDER=console."
            ),
        )

    log.info("email.provider_selected", provider=key)
    return provider_cls()


def available_email_providers() -> list[str]:
    return sorted(_PROVIDERS)
