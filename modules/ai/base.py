"""LLM provider contract.

Three members, not twelve. A narrow interface is what makes ``MockProvider``
possible, and ``MockProvider`` is what makes the entire application testable on
an 8 GB laptop with no GPU and no network.

**No implementation of this ever loads model weights** (D-12). Every provider is
either a remote HTTP client or a fixture reader — enforced by
``scripts/check_no_local_inference.py`` and an import-linter contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from core.models import GenerationParams, HealthStatus, LLMResponse, Message


class LLMProvider(ABC):
    """A source of structured completions."""

    #: Stable identifier recorded on every campaign for provenance.
    name: str = "unknown"

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """Cheap liveness probe. Must never raise — return an unhealthy status."""

    @abstractmethod
    def generate(
        self,
        messages: list[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        params: GenerationParams | None = None,
        prompt_name: str = "",
        prompt_version: str = "",
    ) -> LLMResponse:
        """Produce one structured completion.

        Args:
            messages: The conversation, system message first.
            json_schema: A JSON Schema the output must conform to. When
                :attr:`supports_guided_json` is true this constrains generation
                itself, so the response cannot be malformed.
            params: Sampling parameters. Defaults are used when omitted.
            prompt_name: Recorded for provenance.
            prompt_version: Recorded for provenance.

        Returns:
            The parsed payload plus the metadata needed to reproduce the call.

        Raises:
            LLMUnavailableError: Endpoint unreachable, or the circuit is open.
            LLMTimeoutError: The request exceeded its deadline.
            InvalidJSONResponse: The response could not be parsed or validated.
        """

    @property
    @abstractmethod
    def supports_guided_json(self) -> bool:
        """Whether the backend constrains generation to the schema.

        When false, the engine must apply its repair-retry path — the difference
        between "JSON is guaranteed" and "JSON is usually fine".
        """

    def close(self) -> None:  # noqa: B027 - intentionally optional, not abstract
        """Release any held resources.

        A concrete no-op rather than an abstract method: ``MockProvider`` holds
        nothing to release, and forcing every provider — including any Vays adds
        later — to write an empty stub would be noise, not safety.
        """
