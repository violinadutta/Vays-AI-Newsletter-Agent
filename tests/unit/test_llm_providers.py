"""LLM provider tests.

The first class is a **shared contract suite**: it runs against every
implementation, so a provider that violates the interface fails CI rather than
failing on demo day. That matters more than usual here — Vays will add their own
provider after handover, and this suite is what tells them whether it is correct.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.exceptions import (
    ConfigurationError,
    InvalidJSONResponse,
    LLMRateLimitedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from core.models import GenerationParams, Message
from core.schemas import ARTICLE_SUMMARY_SCHEMA, NEWSLETTER_SCHEMA
from modules.ai.base import LLMProvider
from modules.ai.circuit_breaker import CircuitBreaker
from modules.ai.factory import available_providers, create_provider
from modules.ai.groq_provider import (
    DEFAULT_MODEL,
    STRICT_SCHEMA_MODELS,
    supports_strict_schema,
)
from modules.ai.mock_provider import MockProvider
from modules.ai.openai_compatible import MAX_RETRY_AFTER_S, OpenAICompatibleProvider

BASE_URL = "https://api.groq.com/openai"
MESSAGES = [
    Message(role="system", content="You are a copywriter."),
    Message(role="user", content="Summarise this article about Dell servers."),
]


def completion(payload: dict[str, Any], finish_reason: str = "stop") -> dict[str, Any]:
    """A well-formed OpenAI-protocol response body."""
    return {
        "model": "openai/gpt-oss-120b",
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(payload)},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 340},
    }


def make_http_provider(**overrides: Any) -> OpenAICompatibleProvider:
    defaults: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "test-key",
        "model": "openai/gpt-oss-120b",
        "name": "groq",
        "timeout_s": 5,
        "max_retries": 1,
    }
    return OpenAICompatibleProvider(**{**defaults, **overrides})


# ─────────────────────────────────────────────────────────────────────────────
#  Shared contract — every provider must satisfy this
# ─────────────────────────────────────────────────────────────────────────────
class TestProviderContract:
    """Runs against every implementation, including any Vays adds later."""

    @pytest.fixture(params=["mock", "http"])
    def provider(self, request: pytest.FixtureRequest) -> LLMProvider:
        if request.param == "mock":
            return MockProvider()
        return make_http_provider()

    def test_declares_a_name(self, provider: LLMProvider) -> None:
        """Recorded on every campaign for provenance."""
        assert provider.name
        assert provider.name != "unknown"

    def test_declares_guided_json_support(self, provider: LLMProvider) -> None:
        assert isinstance(provider.supports_guided_json, bool)

    def test_health_check_never_raises(self, provider: LLMProvider) -> None:
        """A health probe that throws makes the Dashboard crash instead of
        reporting the outage it exists to report."""
        with respx.mock:
            respx.get(f"{BASE_URL}/v1/models").mock(side_effect=httpx.ConnectError("down"))
            status = provider.health_check()

        assert isinstance(status.healthy, bool)

    def test_close_is_safe_to_call(self, provider: LLMProvider) -> None:
        provider.close()

    def test_loads_no_model_weights(self, provider: LLMProvider) -> None:
        """D-12, asserted rather than assumed: no provider may pull an ML runtime
        into the process."""
        import sys

        banned = {"torch", "transformers", "vllm", "llama_cpp", "onnxruntime"}
        assert not banned & {m.split(".")[0] for m in sys.modules}


# ─────────────────────────────────────────────────────────────────────────────
#  MockProvider
# ─────────────────────────────────────────────────────────────────────────────
class TestMockProvider:
    def test_returns_a_schema_valid_newsletter(self) -> None:
        from core.models import NewsletterContent

        response = MockProvider().generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA, prompt_name="newsletter_compose"
        )

        NewsletterContent.model_validate(response.payload)

    def test_returns_a_schema_valid_summary(self) -> None:
        from core.models import ArticleSummary

        response = MockProvider().generate(
            MESSAGES, json_schema=ARTICLE_SUMMARY_SCHEMA, prompt_name="article_summary"
        )

        ArticleSummary.model_validate(response.payload)

    def test_is_deterministic_across_calls(self) -> None:
        """UI work and tests both need the same draft on every rerun."""
        first = MockProvider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)
        second = MockProvider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert first.payload == second.payload

    def test_different_inputs_give_different_output(self) -> None:
        """Three articles must not produce three identical summaries."""
        provider = MockProvider()
        payloads = [
            provider.generate(
                [Message(role="user", content=f"Article {i}")],
                json_schema=ARTICLE_SUMMARY_SCHEMA,
                prompt_name="article_summary",
            ).payload
            for i in range(3)
        ]

        assert len({json.dumps(p, sort_keys=True) for p in payloads}) > 1

    def test_synthesises_a_response_for_an_unknown_schema(self) -> None:
        """So a new prompt added in M4 can be developed offline, with no API key
        and no network."""
        schema = {
            "type": "object",
            "properties": {
                "headline": {"type": "string", "minLength": 20, "maxLength": 60},
                "score": {"type": "integer", "minimum": 1, "maximum": 10},
                "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                "mood": {"type": "string", "enum": ["good", "bad"]},
            },
            "required": ["headline", "score", "tags", "mood"],
        }

        payload = MockProvider().generate(MESSAGES, json_schema=schema).payload

        assert 20 <= len(payload["headline"]) <= 60
        assert 1 <= payload["score"] <= 10
        assert len(payload["tags"]) >= 2
        assert payload["mood"] in ("good", "bad")

    def test_requires_a_schema(self) -> None:
        with pytest.raises(InvalidJSONResponse):
            MockProvider().generate(MESSAGES)

    def test_records_calls_for_assertions(self) -> None:
        provider = MockProvider()
        provider.generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA, prompt_name="x")

        assert len(provider.calls) == 1
        assert provider.calls[0]["prompt"] == "x"

    def test_can_be_configured_to_fail(self) -> None:
        """Drives the recovery UI and circuit-breaker paths without a real outage."""
        provider = MockProvider(fail_with=LLMUnavailableError("simulated"))

        with pytest.raises(LLMUnavailableError):
            provider.generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)
        assert provider.health_check().healthy is False


# ─────────────────────────────────────────────────────────────────────────────
#  OpenAI-compatible transport
# ─────────────────────────────────────────────────────────────────────────────
class TestOpenAICompatibleProvider:
    @respx.mock
    def test_successful_generation(self) -> None:
        payload = {"subject": "Dell's new servers", "keywords": ["a", "b", "c"]}
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion(payload))
        )

        response = make_http_provider().generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA, prompt_name="p", prompt_version="1.0.0"
        )

        assert response.payload == payload
        assert response.provider == "groq"
        assert response.input_tokens == 1200
        assert response.prompt_version == "1.0.0"

    @respx.mock
    def test_the_schema_is_sent_as_guided_decoding(self) -> None:
        """Without this the schema is a suggestion; with it the decoder cannot
        emit anything else."""
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion({"ok": True}))
        )

        make_http_provider().generate(MESSAGES, json_schema=ARTICLE_SUMMARY_SCHEMA)

        sent = json.loads(route.calls[0].request.content)
        assert sent["response_format"]["type"] == "json_schema"
        assert sent["response_format"]["json_schema"]["strict"] is True
        assert sent["response_format"]["json_schema"]["schema"] == ARTICLE_SUMMARY_SCHEMA

    @respx.mock
    def test_sampling_parameters_are_forwarded(self) -> None:
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion({"ok": True}))
        )

        make_http_provider().generate(
            MESSAGES,
            json_schema=NEWSLETTER_SCHEMA,
            params=GenerationParams(temperature=0.3, top_p=0.8, max_tokens=512),
        )

        sent = json.loads(route.calls[0].request.content)
        assert sent["temperature"] == 0.3
        assert sent["max_tokens"] == 512


class TestErrorMapping:
    """'HTTP 401' means nothing to Priya. 'The key changes when the notebook
    restarts — copy the new one' tells her exactly what to do."""

    @respx.mock
    def test_401_explains_the_rotating_api_key(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )

        with pytest.raises(LLMUnavailableError) as exc_info:
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert "LLM_API_KEY" in exc_info.value.user_message

    @respx.mock
    def test_401_is_not_retried(self) -> None:
        """Retrying a rejected key wastes three timeouts and hides the cause."""
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(401))

        with pytest.raises(LLMUnavailableError):
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert route.call_count == 1

    @respx.mock
    def test_404_names_the_model_and_the_endpoint(self) -> None:
        """A 404 from an OpenAI-compatible API almost always means a typo'd model
        name, not a dead host — so the message names both."""
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(404))

        with pytest.raises(LLMUnavailableError) as exc_info:
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert "openai/gpt-oss-120b" in exc_info.value.user_message
        assert "endpoint URL" in exc_info.value.user_message

    @respx.mock
    def test_413_suggests_fewer_articles(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(413))

        with pytest.raises(LLMUnavailableError) as exc_info:
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert "fewer articles" in exc_info.value.user_message

    @respx.mock
    def test_cuda_out_of_memory_suggests_a_smaller_model(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="CUDA out of memory. Tried to allocate 2GB")
        )

        with pytest.raises(LLMUnavailableError) as exc_info:
            make_http_provider(max_retries=0).generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert "smaller model" in exc_info.value.user_message

    @respx.mock
    def test_timeouts_are_retried_then_reported(self) -> None:
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            side_effect=httpx.TimeoutException("too slow")
        )

        with pytest.raises(LLMTimeoutError):
            make_http_provider(max_retries=2).generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert route.call_count == 3

    @respx.mock
    def test_server_errors_are_retried(self) -> None:
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(503))

        with pytest.raises(LLMUnavailableError):
            make_http_provider(max_retries=2).generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert route.call_count == 3

    @respx.mock
    def test_a_transient_failure_then_success(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=completion({"ok": True})),
            ]
        )

        response = make_http_provider(max_retries=2).generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA
        )

        assert response.payload == {"ok": True}

    @respx.mock
    def test_truncated_output_reports_the_token_budget_not_a_parse_error(self) -> None:
        """A `length` finish is always malformed JSON. Saying 'unparseable' would
        send the user hunting for the wrong problem."""
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"subject": "trunca'}, "finish_reason": "length"}
                    ]
                },
            )
        )

        with pytest.raises(InvalidJSONResponse) as exc_info:
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert "ran out of room" in exc_info.value.user_message

    @respx.mock
    def test_malformed_json_is_reported(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "not json at all"}, "finish_reason": "stop"}
                    ]
                },
            )
        )

        with pytest.raises(InvalidJSONResponse):
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

    @respx.mock
    def test_an_unexpected_response_shape_is_reported(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"unexpected": "shape"})
        )

        with pytest.raises(InvalidJSONResponse):
            make_http_provider().generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)


class TestCircuitIntegration:
    @respx.mock
    def test_repeated_failures_open_the_circuit(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(503))
        provider = make_http_provider(
            max_retries=0, circuit_breaker=CircuitBreaker(failure_threshold=2, reset_timeout_s=60)
        )

        for _ in range(2):
            with pytest.raises(LLMUnavailableError):
                provider.generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert not provider.circuit_breaker.allows_request()

    @respx.mock
    def test_an_open_circuit_fails_without_a_request(self) -> None:
        """The saving: no 120-second timeout per article once we already know."""
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=httpx.Response(503))
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=600)
        provider = make_http_provider(max_retries=0, circuit_breaker=breaker)

        with pytest.raises(LLMUnavailableError):
            provider.generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)
        calls_after_first = route.call_count

        with pytest.raises(LLMUnavailableError):
            provider.generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert route.call_count == calls_after_first

    @respx.mock
    def test_success_keeps_the_circuit_closed(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion({"ok": True}))
        )
        provider = make_http_provider()

        provider.generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert provider.circuit_breaker.allows_request()


class TestHealthCheck:
    @respx.mock
    def test_healthy_endpoint(self) -> None:
        respx.get(f"{BASE_URL}/v1/models").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        )

        status = make_http_provider().health_check()

        assert status.healthy is True
        assert status.latency_ms is not None

    @respx.mock
    def test_401_is_reported_as_a_key_problem(self) -> None:
        respx.get(f"{BASE_URL}/v1/models").mock(return_value=httpx.Response(401))

        status = make_http_provider().health_check()

        assert status.healthy is False
        assert "API key" in status.detail

    @respx.mock
    def test_unreachable_endpoint(self) -> None:
        respx.get(f"{BASE_URL}/v1/models").mock(side_effect=httpx.ConnectError("no route"))

        assert make_http_provider().health_check().healthy is False

    @respx.mock
    def test_results_are_cached(self) -> None:
        """Streamlit reruns constantly; an uncached probe would flood the
        endpoint from a single user clicking around."""
        route = respx.get(f"{BASE_URL}/v1/models").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        provider = make_http_provider()

        for _ in range(5):
            provider.health_check()

        assert route.call_count == 1

    @respx.mock
    def test_the_cache_can_be_bypassed(self) -> None:
        """Settings → Test Connection must give a real answer, not a stale one."""
        route = respx.get(f"{BASE_URL}/v1/models").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        provider = make_http_provider()

        provider.health_check()
        provider.health_check(use_cache=False)

        assert route.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
#  Groq specifics: rate limits and strict schema enforcement
# ─────────────────────────────────────────────────────────────────────────────
class TestRateLimiting:
    """Groq's free tier is generous on requests (~30/min) and tight on tokens
    (~8–12k/min), so 429 is an expected condition, not an exotic one."""

    @respx.mock
    def test_a_rate_limit_is_retried_then_reported_as_such(self) -> None:
        """Not as "unreachable" — nothing is broken, and telling the user to
        check their connection sends them hunting for a phantom fault."""
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(429, headers={"retry-after": "0"})
        )

        with pytest.raises(LLMRateLimitedError) as exc_info:
            make_http_provider(max_retries=1).generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert "busy" in exc_info.value.user_message
        assert exc_info.value.retryable is True

    @respx.mock
    def test_a_rate_limit_that_clears_succeeds(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "0"}),
                httpx.Response(200, json=completion({"ok": True})),
            ]
        )

        response = make_http_provider(max_retries=2).generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA
        )

        assert response.payload == {"ok": True}

    @respx.mock
    def test_retry_after_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The server says how long to wait. Guessing with exponential backoff
        either waits too long or retries too early and burns another request
        against the same quota."""
        slept: list[float] = []
        monkeypatch.setattr("modules.ai.openai_compatible.time.sleep", slept.append)

        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "7"}),
                httpx.Response(200, json=completion({"ok": True})),
            ]
        )

        make_http_provider(max_retries=2).generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert 7 in slept

    @respx.mock
    def test_an_absurd_retry_after_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A provider asking for ten minutes should surface as an error the user
        can act on, not a frozen UI."""
        slept: list[float] = []
        monkeypatch.setattr("modules.ai.openai_compatible.time.sleep", slept.append)

        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "600"}),
                httpx.Response(200, json=completion({"ok": True})),
            ]
        )

        make_http_provider(max_retries=2).generate(MESSAGES, json_schema=NEWSLETTER_SCHEMA)

        assert max(slept) <= MAX_RETRY_AFTER_S

    @respx.mock
    def test_a_missing_retry_after_still_retries(self) -> None:
        respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json=completion({"ok": True})),
            ]
        )

        assert make_http_provider(max_retries=2).generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA
        ).payload == {"ok": True}


class TestStrictSchemaEnforcement:
    """``strict: true`` is what makes malformed JSON impossible rather than
    unlikely (D-3), and Groq supports it only on specific models."""

    def test_the_default_model_supports_it(self) -> None:
        assert supports_strict_schema(DEFAULT_MODEL)

    @pytest.mark.parametrize("model", sorted(STRICT_SCHEMA_MODELS))
    def test_listed_models_support_it(self, model: str) -> None:
        assert supports_strict_schema(model)

    @pytest.mark.parametrize("model", ["llama-3.3-70b-versatile", "qwen-2.5-32b", "gemma2-9b-it"])
    def test_other_models_do_not(self, model: str) -> None:
        assert not supports_strict_schema(model)

    @respx.mock
    def test_strict_is_sent_when_the_provider_supports_it(self) -> None:
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion({"ok": True}))
        )

        make_http_provider(supports_guided_json=True).generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA
        )

        sent = json.loads(route.calls[0].request.content)
        assert sent["response_format"]["json_schema"]["strict"] is True

    @respx.mock
    def test_strict_is_not_claimed_when_unsupported(self) -> None:
        """Claiming `strict` on a model that cannot honour it gets the request
        rejected outright, so the flag tracks capability, not preference."""
        route = respx.post(f"{BASE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion({"ok": True}))
        )

        make_http_provider(supports_guided_json=False).generate(
            MESSAGES, json_schema=NEWSLETTER_SCHEMA
        )

        sent = json.loads(route.calls[0].request.content)
        assert sent["response_format"]["json_schema"]["strict"] is False


# ─────────────────────────────────────────────────────────────────────────────
#  Factory
# ─────────────────────────────────────────────────────────────────────────────
class TestFactory:
    def test_mock_needs_no_credentials(self, set_env) -> None:  # noqa: ANN001
        from tests.conftest import MINIMAL_ENV

        set_env(**MINIMAL_ENV)

        assert create_provider().name == "mock"

    def test_a_remote_provider_without_a_key_fails_at_startup(self, set_env) -> None:  # noqa: ANN001
        """Caught before the UI renders, not on the first generation attempt."""
        from tests.conftest import MINIMAL_ENV

        set_env(**{**MINIMAL_ENV, "LLM_PROVIDER": "groq", "LLM_API_KEY": ""})

        with pytest.raises(ConfigurationError) as exc_info:
            create_provider()

        assert "console.groq.com" in exc_info.value.user_message

    def test_an_unknown_provider_lists_the_valid_ones(self, set_env) -> None:  # noqa: ANN001
        from tests.conftest import MINIMAL_ENV

        set_env(**MINIMAL_ENV)

        with pytest.raises(ConfigurationError) as exc_info:
            create_provider("gpt5")

        assert "mock" in exc_info.value.user_message

    def test_groq_and_hosted_are_the_same_transport(self, set_env) -> None:  # noqa: ANN001
        """The handover property: if these ever diverge in behaviour, the
        abstraction has failed."""
        from tests.conftest import MINIMAL_ENV

        set_env(**{**MINIMAL_ENV, "LLM_PROVIDER": "groq", "LLM_API_KEY": "k"})
        groq = create_provider("groq")
        hosted = create_provider("hosted")

        assert isinstance(groq, OpenAICompatibleProvider)
        assert isinstance(hosted, OpenAICompatibleProvider)
        assert groq.base_url == hosted.base_url
        assert groq.name != hosted.name

    def test_no_local_inference_provider_exists(self) -> None:
        """D-12 asserted at the factory as well as in config.

        What must never exist is an adapter that loads a model into this
        process — every entry here is remote or fixture-based.
        """
        assert "local" not in available_providers()
        assert set(available_providers()) == {"groq", "hosted", "mock"}
