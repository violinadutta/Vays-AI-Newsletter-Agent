"""LLM access. Every provider is remote or fixture-based — nothing loads a model.

The seam Vays uses at handover: ``create_provider()`` reads ``LLM_PROVIDER`` and
returns something satisfying ``LLMProvider``. Switching to their own model is two
``.env`` values (``LLM_BASE_URL``, ``LLM_API_KEY``) and no code change.

That seam has already paid for itself once: moving the whole project off
Colab-hosted vLLM and onto Groq cost one small subclass and a config default.
"""

from modules.ai.base import LLMProvider
from modules.ai.circuit_breaker import CircuitBreaker, CircuitState
from modules.ai.factory import available_providers, create_provider
from modules.ai.groq_provider import (
    STRICT_SCHEMA_MODELS,
    GroqProvider,
    HostedProvider,
    supports_strict_schema,
)
from modules.ai.mock_provider import FailingMockProvider, MockProvider
from modules.ai.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "STRICT_SCHEMA_MODELS",
    "CircuitBreaker",
    "CircuitState",
    "FailingMockProvider",
    "GroqProvider",
    "HostedProvider",
    "LLMProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "available_providers",
    "create_provider",
    "supports_strict_schema",
]
