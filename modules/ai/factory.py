"""Provider selection.

The one place that knows which concrete provider exists. Everything else depends
on the ``LLMProvider`` interface, which is what makes the handover swap a
configuration change rather than a code change.

Adding a fourth provider means adding a class and one line here. No other file
changes — that is the property this factory exists to preserve.
"""

from __future__ import annotations

from config import get_logger, get_settings
from core.exceptions import ConfigurationError
from modules.ai.base import LLMProvider
from modules.ai.groq_provider import GroqProvider, HostedProvider
from modules.ai.mock_provider import MockProvider

log = get_logger(__name__)

#: There is deliberately no "local" entry — no model ever runs on this machine
#: (D-12). ``LLMSettings.provider`` rejects the value at config level too, so the
#: constraint cannot be reintroduced by a typo.
_PROVIDERS: dict[str, type[LLMProvider]] = {
    "groq": GroqProvider,
    "hosted": HostedProvider,
    "mock": MockProvider,
}


def create_provider(name: str | None = None) -> LLMProvider:
    """Build the configured provider.

    Args:
        name: Override the configured provider. For tests.

    Raises:
        ConfigurationError: If the name is unknown, or a remote provider is
            selected without an endpoint — caught at startup rather than on the
            first generation attempt.
    """
    settings = get_settings().llm
    key = (name or settings.provider).lower()

    provider_cls = _PROVIDERS.get(key)
    if provider_cls is None:
        raise ConfigurationError(
            f"unknown LLM provider {key!r}; valid values are {sorted(_PROVIDERS)}",
            user_message=(
                f"LLM_PROVIDER is set to '{key}', which isn't valid. "
                f"Use one of: {', '.join(sorted(_PROVIDERS))}."
            ),
        )

    if key != "mock" and not settings.api_key.get_secret_value():
        hint = (
            "Create a free key at console.groq.com and put it in GROQ_API_KEY"
            if key == "groq"
            else "Set LLM_API_KEY"
        )
        raise ConfigurationError(
            f"provider {key!r} requires an API key",
            user_message=(
                f"The AI service needs an API key. {hint} in your .env file, "
                "or set LLM_PROVIDER=mock to work offline."
            ),
        )

    log.info("llm.provider_selected", provider=key, model=settings.model)
    return provider_cls()


def available_providers() -> list[str]:
    """Provider names, for the Settings page selector."""
    return sorted(_PROVIDERS)
