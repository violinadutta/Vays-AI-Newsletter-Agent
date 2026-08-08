"""Fixture-backed provider — no model, no weights, no GPU, no network (D-12).

This replaced the Ollama adapter from the first draft, and it is better for the
job it actually does. Development and CI need *deterministic* output: a test that
depends on a small model's phrasing is a flaky test, and UI work needs the same
draft on every rerun.

It works for any schema. Curated fixtures give realistic copy for the two real
prompts; anything else is synthesised from the JSON Schema itself, so a new
prompt added in M4 can be developed entirely offline — no API key, no quota, no
network.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from config import get_logger
from core.exceptions import InvalidJSONResponse, LLMUnavailableError
from core.models import GenerationParams, HealthStatus, LLMResponse, Message
from modules.ai.base import LLMProvider

log = get_logger(__name__)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


class MockProvider(LLMProvider):
    """Returns deterministic, schema-valid responses without any inference."""

    name = "mock"

    def __init__(
        self,
        *,
        fixture_dir: Path | None = None,
        latency_ms: int = 0,
        fail_with: Exception | None = None,
    ) -> None:
        """
        Args:
            fixture_dir: Override the bundled fixtures.
            latency_ms: Simulated delay, for exercising progress indicators.
            fail_with: Raise this on every call — lets tests drive the failure
                paths (circuit breaker, recovery UI) without a real outage.
        """
        self._fixture_dir = fixture_dir or FIXTURE_DIR
        self._latency_ms = latency_ms
        self._fail_with = fail_with
        self.calls: list[dict[str, Any]] = []

    @property
    def supports_guided_json(self) -> bool:
        # True, and honestly so: the output is constructed from the schema, so it
        # conforms by construction. The engine's repair path is never exercised
        # here — which is why that path has its own explicit tests.
        return True

    def health_check(self) -> HealthStatus:
        if self._fail_with is not None:
            return HealthStatus(healthy=False, detail="Mock provider configured to fail")
        return HealthStatus(healthy=True, detail="mock (no inference)", latency_ms=0)

    def generate(
        self,
        messages: list[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        params: GenerationParams | None = None,
        prompt_name: str = "",
        prompt_version: str = "",
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "schema": json_schema, "prompt": prompt_name})
        if self._fail_with is not None:
            raise self._fail_with
        if self._latency_ms:
            time.sleep(self._latency_ms / 1000)

        if json_schema is None:
            raise InvalidJSONResponse(
                "MockProvider requires a schema — it builds its response from one",
                user_message="Internal error: the AI request was missing its output format.",
            )

        seed = self._seed(messages)
        payload = self._load_fixture(prompt_name, seed) or self._synthesise(json_schema, seed)

        return LLMResponse(
            payload=payload,
            model="mock-model",
            provider=self.name,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            latency_ms=self._latency_ms,
            input_tokens=sum(len(m.content) // 4 for m in messages),
            output_tokens=len(json.dumps(payload)) // 4,
            finish_reason="stop",
        )

    # ── internals ────────────────────────────────────────────────────────────
    @staticmethod
    def _seed(messages: list[Message]) -> int:
        """Derive a stable seed from the input.

        Same input, same output, every time — across processes and machines.
        Python's ``hash()`` is randomised per process and would break that.
        """
        joined = "".join(m.content for m in messages).encode("utf-8")
        return int(hashlib.sha256(joined).hexdigest()[:8], 16)

    def _load_fixture(self, prompt_name: str, seed: int) -> dict[str, Any] | None:
        if not prompt_name:
            return None
        path = self._fixture_dir / f"{prompt_name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("mock.fixture_unreadable", path=str(path))
            return None
        # A fixture file may hold one object or a list of variants; a list keeps
        # multi-article tests from producing three identical summaries.
        if isinstance(data, list):
            return dict(data[seed % len(data)])
        return dict(data)

    def _synthesise(self, schema: dict[str, Any], seed: int) -> dict[str, Any]:
        """Build a minimal object satisfying ``schema``."""
        result: dict[str, Any] = {}
        for key, spec in (schema.get("properties") or {}).items():
            result[key] = self._value_for(key, spec, seed)
        return result

    def _value_for(self, key: str, spec: dict[str, Any], seed: int) -> Any:
        if "enum" in spec:
            options = spec["enum"]
            return options[seed % len(options)]

        kind = spec.get("type")
        if kind == "string":
            return self._string_for(key, spec, seed)
        if kind == "integer":
            return max(int(spec.get("minimum", 1)), min(int(spec.get("maximum", 5)), 5))
        if kind == "number":
            return float(spec.get("minimum", 1.0))
        if kind == "boolean":
            return True
        if kind == "array":
            count = int(spec.get("minItems", 1))
            item_spec = spec.get("items", {"type": "string"})
            return [self._value_for(key, item_spec, seed + i) for i in range(count)]
        if kind == "object":
            return self._synthesise(spec, seed)
        return f"mock {key}"

    @staticmethod
    def _string_for(key: str, spec: dict[str, Any], seed: int) -> str:
        base = f"Mock {key.replace('_', ' ')} {seed % 1000}"
        minimum = int(spec.get("minLength", 0))
        maximum = int(spec.get("maxLength", 10_000))

        if len(base) < minimum:
            filler = " Generated placeholder text for offline development."
            while len(base) < minimum:
                base += filler
        return base[:maximum]


class FailingMockProvider(MockProvider):
    """Always unreachable. For driving the recovery UI and circuit-breaker tests."""

    name = "mock-failing"

    def __init__(self) -> None:
        super().__init__(
            fail_with=LLMUnavailableError(
                "simulated outage",
                context={"provider": "mock-failing"},
            )
        )
