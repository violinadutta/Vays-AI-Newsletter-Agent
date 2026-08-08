"""Client for the OpenAI ``/v1/chat/completions`` protocol.

**This class is the handover seam.** Groq and any other endpoint are the same
code with a different base URL, because vLLM, TGI, Ollama, LM Studio and every
commercial provider speak this wire format. Repointing the application at Vays'
own LLM is two ``.env`` values and no code change (D-21).

Implemented on ``httpx`` directly rather than the OpenAI SDK. The SDK's value is
streaming, its own retry layer and typed errors — we need none of those (we have
tenacity and a circuit breaker) and it would obscure the error mapping that turns
a 401 into "check LLM_API_KEY in your .env". One fewer dependency on the path that
matters most at handover.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import get_logger
from config.constants import HEALTH_CHECK_CACHE_S
from core.exceptions import (
    InvalidJSONResponse,
    LLMRateLimitedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from core.models import GenerationParams, HealthStatus, LLMResponse, Message
from modules.ai.base import LLMProvider
from modules.ai.circuit_breaker import CircuitBreaker

log = get_logger(__name__)


class _RetryableStatus(Exception):
    """Internal marker: an HTTP status worth another attempt."""

    def __init__(self, message: str, *, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


#: Markers vLLM emits when the GPU runs out of memory. Matched against the
#: response body because the status code alone (500) cannot distinguish a
#: deterministic OOM from a transient server hiccup.
_OOM_MARKERS = ("out of memory", "cuda error", "no available memory", "kv cache")


def _is_out_of_memory(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _OOM_MARKERS)


#: Groq's signal that constrained generation could not produce a valid object —
#: in practice, almost always because ``max_tokens`` cut it off mid-JSON.
_JSON_FAILURE_MARKERS = (
    "json_validate_failed",
    "failed to validate json",
    "failed to generate json",
)


def _is_json_validate_failure(body: str) -> bool:
    lowered = body.lower()
    return any(marker in lowered for marker in _JSON_FAILURE_MARKERS)


#: Cap on how long we will honour a ``Retry-After``. A provider asking us to wait
#: ten minutes should surface as an error the user can act on, not a frozen UI.
MAX_RETRY_AFTER_S = 30


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse ``Retry-After``, which may be seconds or an HTTP date."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(when.tzinfo or UTC)).total_seconds())


class OpenAICompatibleProvider(LLMProvider):
    """Talks to any server implementing the OpenAI chat-completions API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        name: str,
        timeout_s: int = 120,
        max_retries: int = 3,
        default_params: GenerationParams | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        client: httpx.Client | None = None,
        supports_guided_json: bool = True,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._defaults = default_params or GenerationParams()
        self._breaker = circuit_breaker or CircuitBreaker()
        self._supports_guided_json = supports_guided_json
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        self._health_cache: tuple[float, HealthStatus] | None = None

    @property
    def supports_guided_json(self) -> bool:
        return self._supports_guided_json

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._breaker

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ── health ───────────────────────────────────────────────────────────────
    def health_check(self, *, use_cache: bool = True) -> HealthStatus:
        """Probe ``/v1/models``.

        Cached for 30 seconds: Streamlit re-executes the whole script on every
        interaction, so an uncached probe would flood the endpoint with requests
        from one user clicking around.
        """
        now = time.monotonic()
        if use_cache and self._health_cache and now - self._health_cache[0] < HEALTH_CHECK_CACHE_S:
            return self._health_cache[1]

        started = time.monotonic()
        try:
            response = self._client.get(f"{self.base_url}/v1/models", timeout=10.0)
            latency = int((time.monotonic() - started) * 1000)

            if response.status_code == 401:
                status = HealthStatus(
                    healthy=False,
                    detail="The API key was rejected. Check LLM_API_KEY / GROQ_API_KEY.",
                    latency_ms=latency,
                )
            elif response.status_code >= 400:
                status = HealthStatus(
                    healthy=False,
                    detail=f"The AI service returned HTTP {response.status_code}.",
                    latency_ms=latency,
                )
            else:
                status = HealthStatus(healthy=True, detail=self.model, latency_ms=latency)
        except httpx.HTTPError as exc:
            status = HealthStatus(
                healthy=False,
                detail=f"Could not reach the AI service ({type(exc).__name__}).",
            )

        self._health_cache = (now, status)
        return status

    # ── generation ───────────────────────────────────────────────────────────
    def generate(
        self,
        messages: list[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        params: GenerationParams | None = None,
        prompt_name: str = "",
        prompt_version: str = "",
    ) -> LLMResponse:
        if not self._breaker.allows_request():
            raise LLMUnavailableError(
                f"circuit open for {self.name}: {self._breaker.last_error}",
                context={"provider": self.name, "circuit": "open"},
            )

        effective = params or self._defaults
        payload = self._build_payload(messages, json_schema, effective)
        started = time.monotonic()

        try:
            data = self._post(payload)
        except (LLMUnavailableError, LLMTimeoutError) as exc:
            self._breaker.record_failure(exc.message)
            raise

        self._breaker.record_success()
        latency = int((time.monotonic() - started) * 1000)
        return self._parse(data, latency, prompt_name, prompt_version)

    def _build_payload(
        self,
        messages: list[Message],
        json_schema: dict[str, Any] | None,
        params: GenerationParams,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "temperature": params.temperature,
            "top_p": params.top_p,
            "max_tokens": params.max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": json_schema,
                    # `strict` is only honoured by models that support constrained
                    # decoding. Claiming it on a model that does not gets the
                    # request rejected outright, so it tracks the provider's
                    # actual capability rather than our preference.
                    "strict": self._supports_guided_json,
                },
            }
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send the request, retrying only what is worth retrying."""

        @retry(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(multiplier=2, min=2, max=16),
            retry=retry_if_exception_type(
                (httpx.TimeoutException, httpx.TransportError, _RetryableStatus)
            ),
            reraise=True,
        )
        def _attempt() -> httpx.Response:
            response = self._client.post(f"{self.base_url}/v1/chat/completions", json=payload)

            if response.status_code == 429:
                # A rate limit is a "wait", not a "broken" — and the server says
                # how long. Obeying Retry-After beats guessing with exponential
                # backoff, which either waits too long or retries too early and
                # burns another request against the same quota.
                delay = _retry_after_seconds(response)
                if delay is not None:
                    log.info("llm.rate_limited", provider=self.name, retry_after_s=delay)
                    time.sleep(min(delay, MAX_RETRY_AFTER_S))
                raise _RetryableStatus("HTTP 429 (rate limited)", rate_limited=True)

            if response.status_code >= 500:
                # Not every 5xx is transient. An out-of-memory is deterministic:
                # the same request fails the same way, so retrying costs three
                # timeouts and then reports the wrong cause.
                if _is_out_of_memory(response.text):
                    return response
                raise _RetryableStatus(f"HTTP {response.status_code}")

            return response

        try:
            response = _attempt()
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"{self.name} timed out after {self._timeout}s",
                context={"provider": self.name, "timeout_s": self._timeout},
            ) from exc
        except _RetryableStatus as exc:
            if exc.rate_limited:
                # Nothing is broken — the quota needs a moment. Saying
                # "unreachable" would send the user hunting for a network fault.
                raise LLMRateLimitedError(
                    f"{self.name} rate limit not cleared after {self._max_retries} retries",
                    context={"provider": self.name},
                ) from exc
            raise LLMUnavailableError(
                f"{self.name} unreachable: {exc}",
                context={"provider": self.name, "error": str(exc)},
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"{self.name} unreachable: {exc}",
                context={"provider": self.name, "error": str(exc)},
            ) from exc

        self._raise_for_status(response)
        return dict(response.json())

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return

        body = response.text[:500]
        if response.status_code == 401:
            raise LLMUnavailableError(
                f"{self.name} rejected the API key (401)",
                user_message=(
                    "The AI service rejected our API key. Check LLM_API_KEY in your "
                    ".env file, or update it in Settings → AI Service."
                ),
                context={"provider": self.name, "status": 401},
            )
        if response.status_code == 404:
            raise LLMUnavailableError(
                f"{self.name} returned 404 — wrong endpoint or unknown model {self.model!r}",
                user_message=(
                    f"The AI service doesn't recognise the model '{self.model}', or the "
                    "endpoint URL is wrong. Check both in Settings → AI Service."
                ),
                context={"provider": self.name, "status": 404, "model": self.model},
            )
        if response.status_code == 400 and _is_json_validate_failure(body):
            # Groq reports a truncated response as a 400 `json_validate_failed`
            # rather than `finish_reason: length`, so the token-budget branch in
            # _parse() never sees it. Measured: a 2,200-token article under
            # max_tokens=1024 fails exactly this way. Without this branch the
            # user gets "the AI service had a problem" and no idea what to do.
            raise InvalidJSONResponse(
                f"{self.name} could not complete a schema-valid response "
                f"(likely truncated by max_tokens): {body}",
                user_message=(
                    "The AI couldn't finish a complete response. Try a shorter "
                    "newsletter length, or fewer articles at once."
                ),
                context={"provider": self.name, "status": 400, "reason": "json_validate_failed"},
            )
        if response.status_code == 413:
            raise LLMUnavailableError(
                f"{self.name} rejected the request as too large",
                user_message=(
                    "That request was too large for the AI service. Use fewer articles, "
                    "or a shorter newsletter length."
                ),
                context={"provider": self.name, "status": 413},
            )
        if _is_out_of_memory(body):
            raise LLMUnavailableError(
                f"{self.name} ran out of GPU memory: {body}",
                user_message=(
                    "The AI service ran out of memory. Restart the notebook with a "
                    "smaller model, or choose a shorter newsletter length."
                ),
                context={"provider": self.name, "status": response.status_code},
            )
        raise LLMUnavailableError(
            f"{self.name} returned HTTP {response.status_code}: {body}",
            context={"provider": self.name, "status": response.status_code},
        )

    def _parse(
        self, data: dict[str, Any], latency_ms: int, prompt_name: str, prompt_version: str
    ) -> LLMResponse:
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise InvalidJSONResponse(
                f"unexpected response shape from {self.name}: {str(data)[:300]}",
                context={"provider": self.name},
            ) from exc

        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            # Truncated output is always malformed JSON, and the useful message
            # is about the token budget, not about parsing.
            raise InvalidJSONResponse(
                f"{self.name} hit the token limit before finishing",
                user_message=(
                    "The AI ran out of room before finishing. Try a shorter "
                    "newsletter length, or fewer articles."
                ),
                context={"provider": self.name, "finish_reason": finish_reason},
            )

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise InvalidJSONResponse(
                f"{self.name} returned unparseable JSON: {content[:300]}",
                context={"provider": self.name, "guided": self._supports_guided_json},
            ) from exc

        if not isinstance(payload, dict):
            raise InvalidJSONResponse(
                f"{self.name} returned {type(payload).__name__}, expected an object",
                context={"provider": self.name},
            )

        usage = data.get("usage") or {}
        return LLMResponse(
            payload=payload,
            model=data.get("model", self.model),
            provider=self.name,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            finish_reason=finish_reason,
        )
