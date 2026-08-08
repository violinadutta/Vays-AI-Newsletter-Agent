"""Groq — the LLM host (D-21).

Groq runs **only open-source models**; there are no proprietary models on the
platform. So the brief's core constraint is satisfied by construction, without
the ToS grey area, session expiry or tunnel rotation that made Colab unworkable.

This is a thin subclass of :class:`OpenAICompatibleProvider` because Groq speaks
the OpenAI chat-completions protocol. That the switch cost one small class is the
return on designing the provider seam up front — the same seam Vays will use
again to point this at their own LLM.

Two Groq-specific behaviours are handled here:

**Strict structured outputs.** ``strict: true`` uses constrained decoding and
guarantees the response matches the schema (D-3). It is supported on a subset of
models — see :data:`STRICT_SCHEMA_MODELS`. On any other model the schema is a
strong suggestion, ``supports_guided_json`` reports ``False``, and the engine's
repair-retry path covers the gap.

**Rate limits.** The free tier is generous on requests (~30/min) and tight on
tokens (~8–12k/min). A 429 therefore means "wait", not "broken", and Groq says
how long in the ``Retry-After`` header — which is worth obeying rather than
guessing at a backoff.
"""

from __future__ import annotations

import httpx

from config import get_logger, get_settings
from core.models import GenerationParams
from modules.ai.circuit_breaker import CircuitBreaker
from modules.ai.openai_compatible import OpenAICompatibleProvider

log = get_logger(__name__)

#: Groq's default endpoint. Overridable via ``LLM_BASE_URL`` for a proxy or a
#: future region-specific host.
GROQ_BASE_URL = "https://api.groq.com/openai"

#: Models on which ``strict: true`` constrained decoding is available, so
#: malformed JSON is impossible rather than unlikely.
#:
#: ``openai/gpt-oss-*`` is **open-weight under Apache 2.0** despite the vendor
#: prefix in its name. It satisfies the open-source-model constraint; the name
#: just reads proprietary. Recorded here because that will be asked at handover.
STRICT_SCHEMA_MODELS: frozenset[str] = frozenset(
    {
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "moonshotai/kimi-k2-instruct-0905",
    }
)

#: Sensible default: the largest model with strict schema support.
DEFAULT_MODEL = "openai/gpt-oss-120b"


def supports_strict_schema(model: str) -> bool:
    """Whether ``model`` enforces a JSON schema at the decoding layer."""
    return model.strip() in STRICT_SCHEMA_MODELS


class GroqProvider(OpenAICompatibleProvider):
    """Open-weight models on Groq's inference API."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        llm = get_settings().llm
        model = llm.model or DEFAULT_MODEL
        strict = supports_strict_schema(model)

        if not strict:
            # Worth a warning rather than silence: the difference between
            # "schema-valid by construction" and "usually schema-valid" is the
            # difference between D-3 holding and not.
            log.warning(
                "groq.no_strict_schema",
                model=model,
                supported=sorted(STRICT_SCHEMA_MODELS),
                impact="JSON validity is not guaranteed; the repair path will be used",
            )

        super().__init__(
            base_url=llm.base_url or GROQ_BASE_URL,
            api_key=llm.api_key.get_secret_value(),
            model=model,
            name="groq",
            timeout_s=llm.timeout_s,
            max_retries=llm.max_retries,
            default_params=GenerationParams(temperature=llm.temperature, max_tokens=llm.max_tokens),
            circuit_breaker=CircuitBreaker(
                failure_threshold=llm.circuit_failure_threshold,
                reset_timeout_s=llm.circuit_reset_s,
            ),
            client=client,
            supports_guided_json=strict,
        )


class HostedProvider(OpenAICompatibleProvider):
    """Any other OpenAI-compatible endpoint — the handover path (D-17).

    Vays points the application at their own LLM by setting ``LLM_PROVIDER=hosted``
    plus a base URL and key. Identical transport to :class:`GroqProvider`; only
    the label and defaults differ. If these two ever need to diverge in
    behaviour, the abstraction has failed.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        llm = get_settings().llm
        super().__init__(
            base_url=llm.base_url,
            api_key=llm.api_key.get_secret_value(),
            model=llm.model,
            name="hosted",
            timeout_s=llm.timeout_s,
            max_retries=llm.max_retries,
            default_params=GenerationParams(temperature=llm.temperature, max_tokens=llm.max_tokens),
            circuit_breaker=CircuitBreaker(
                failure_threshold=llm.circuit_failure_threshold,
                reset_timeout_s=llm.circuit_reset_s,
            ),
            client=client,
            # Unknown endpoint, unknown capability. Assume no hard constraint so
            # the repair path stays armed — failing safe, not optimistically.
            supports_guided_json=False,
        )
