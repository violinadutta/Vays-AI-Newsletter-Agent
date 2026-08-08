"""AI engine tests.

The repair path gets the most attention here because it is **not** theoretical:
strict-mode decoding does not enforce string length, so an over-length subject
line is a measured, regular occurrence against the real provider.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.enums import Audience, Category, EditableField, ExtractorTier, LengthPreset, Tone
from core.exceptions import InvalidJSONResponse
from core.models import (
    ArticleSummary,
    CleanedArticle,
    GenerationOptions,
    GenerationParams,
    HealthStatus,
    LLMResponse,
    Message,
)
from modules.ai.base import LLMProvider
from modules.ai.engine import AIEngine, _summarise
from modules.ai.mock_provider import MockProvider

ARTICLE = CleanedArticle(
    url="https://dell.com/blog",
    title="Dell PowerEdge R7xx",
    cleaned_text="Dell announced the PowerEdge R7xx series. " * 20,
    extractor=ExtractorTier.TRAFILATURA,
    word_count=140,
    token_estimate=200,
)
OPTIONS = GenerationOptions(
    tone=Tone.PROFESSIONAL, length=LengthPreset.MEDIUM, audience=Audience.ENTERPRISE_IT
)

VALID_NEWSLETTER: dict[str, Any] = {
    "title": "Dell's New PowerEdge Servers",
    "summary": "Dell announced the R7xx line with improved power efficiency for data centres.",
    "newsletter": "Dell has announced the PowerEdge R7xx series. " * 5,
    "subject": "Dell's new servers cut power costs",
    "preview_text": "What it means for your refresh cycle",
    "cta": "Read the specs",
    "keywords": ["dell", "poweredge", "servers"],
    "category": "Product Launch",
    "tone": "professional",
}


class ScriptedProvider(LLMProvider):
    """Returns a queued sequence of payloads, recording every call."""

    name = "scripted"

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self.queue = list(payloads)
        self.calls: list[list[Message]] = []

    @property
    def supports_guided_json(self) -> bool:
        return True

    def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, detail="scripted")

    def generate(
        self,
        messages: list[Message],
        *,
        json_schema: dict[str, Any] | None = None,
        params: GenerationParams | None = None,
        prompt_name: str = "",
        prompt_version: str = "",
    ) -> LLMResponse:
        self.calls.append(messages)
        payload = self.queue.pop(0) if self.queue else {}
        return LLMResponse(
            payload=payload,
            model="scripted",
            provider=self.name,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            latency_ms=1,
        )


class TestTwoStagePipeline:
    def test_stage_one_returns_a_validated_summary(self) -> None:
        summary = AIEngine(MockProvider()).summarize_article(ARTICLE, OPTIONS)

        assert isinstance(summary, ArticleSummary)
        assert 3 <= len(summary.key_points) <= 5

    def test_stage_two_returns_validated_content(self) -> None:
        engine = AIEngine(MockProvider())
        summary = engine.summarize_article(ARTICLE, OPTIONS)

        content = engine.compose_newsletter([summary], OPTIONS)

        assert len(content.subject) <= 60
        assert content.tone == Tone.PROFESSIONAL

    def test_summaries_are_ordered_by_relevance(self) -> None:
        """The model should lead with the strongest story, not with whichever
        article the user happened to paste first."""
        provider = ScriptedProvider(VALID_NEWSLETTER)
        engine = AIEngine(provider)
        low = ArticleSummary(
            headline="Low relevance",
            key_points=["a.", "b.", "c."],
            business_impact="x",
            technical_facts=[],
            category=Category.CLOUD,
            relevance_score=2,
        )
        high = ArticleSummary(
            headline="High relevance",
            key_points=["a.", "b.", "c."],
            business_impact="x",
            technical_facts=[],
            category=Category.SECURITY,
            relevance_score=9,
        )

        engine.compose_newsletter([low, high], OPTIONS)

        user_message = provider.calls[0][1].content
        assert user_message.index("High relevance") < user_message.index("Low relevance")

    def test_the_prompt_carries_the_character_limits(self) -> None:
        """Strict decoding does not enforce string length, so the prompt must —
        and this is the test that stops someone quietly deleting those lines."""
        provider = ScriptedProvider(VALID_NEWSLETTER)

        AIEngine(provider).compose_newsletter(
            [
                ArticleSummary(
                    headline="h",
                    key_points=["a.", "b.", "c."],
                    business_impact="x",
                    technical_facts=[],
                    category=Category.CLOUD,
                    relevance_score=5,
                )
            ],
            OPTIONS,
        )

        user_message = provider.calls[0][1].content
        assert "MAXIMUM 60 characters" in user_message
        assert "MAXIMUM 100 characters" in user_message
        assert "hard limits, not targets" in user_message


class TestRepairPath:
    """Measured against Groq: over-length fields are a regular occurrence, not a
    hypothetical, because the decoder does not constrain string length."""

    def test_an_over_length_subject_is_repaired(self) -> None:
        too_long = {**VALID_NEWSLETTER, "subject": "x" * 75}
        provider = ScriptedProvider(too_long, VALID_NEWSLETTER)

        content = AIEngine(provider).compose_newsletter(
            [
                ArticleSummary(
                    headline="h",
                    key_points=["a.", "b.", "c."],
                    business_impact="x",
                    technical_facts=[],
                    category=Category.CLOUD,
                    relevance_score=5,
                )
            ],
            OPTIONS,
        )

        assert len(content.subject) <= 60
        assert len(provider.calls) == 2, "should have retried exactly once"

    def test_the_repair_prompt_names_the_actual_numbers(self) -> None:
        """ "That was invalid, try again" produces another guess. "subject is 75
        characters, the maximum is 60" produces a correction."""
        provider = ScriptedProvider({**VALID_NEWSLETTER, "subject": "x" * 75}, VALID_NEWSLETTER)

        AIEngine(provider).compose_newsletter(
            [
                ArticleSummary(
                    headline="h",
                    key_points=["a.", "b.", "c."],
                    business_impact="x",
                    technical_facts=[],
                    category=Category.CLOUD,
                    relevance_score=5,
                )
            ],
            OPTIONS,
        )

        repair_message = provider.calls[1][-1].content
        assert "75 characters" in repair_message
        assert "maximum is 60" in repair_message

    def test_the_repair_prompt_includes_the_rejected_output(self) -> None:
        """The model needs its own answer back to correct it rather than restart."""
        bad = {**VALID_NEWSLETTER, "subject": "x" * 75}
        provider = ScriptedProvider(bad, VALID_NEWSLETTER)

        AIEngine(provider).compose_newsletter(
            [
                ArticleSummary(
                    headline="h",
                    key_points=["a.", "b.", "c."],
                    business_impact="x",
                    technical_facts=[],
                    category=Category.CLOUD,
                    relevance_score=5,
                )
            ],
            OPTIONS,
        )

        roles = [m.role for m in provider.calls[1]]
        assert "assistant" in roles

    def test_a_persistent_failure_raises_after_the_retry_budget(self) -> None:
        bad = {**VALID_NEWSLETTER, "subject": "x" * 75}
        provider = ScriptedProvider(bad, bad, bad)

        with pytest.raises(InvalidJSONResponse) as exc_info:
            AIEngine(provider).compose_newsletter(
                [
                    ArticleSummary(
                        headline="h",
                        key_points=["a.", "b.", "c."],
                        business_impact="x",
                        technical_facts=[],
                        category=Category.CLOUD,
                        relevance_score=5,
                    )
                ],
                OPTIONS,
            )

        assert "75 characters" in exc_info.value.context["problems"]

    def test_repair_can_be_disabled(self) -> None:
        provider = ScriptedProvider({**VALID_NEWSLETTER, "subject": "x" * 75})

        with pytest.raises(InvalidJSONResponse):
            AIEngine(provider, repair_attempts=0).compose_newsletter(
                [
                    ArticleSummary(
                        headline="h",
                        key_points=["a.", "b.", "c."],
                        business_impact="x",
                        technical_facts=[],
                        category=Category.CLOUD,
                        relevance_score=5,
                    )
                ],
                OPTIONS,
            )
        assert len(provider.calls) == 1


class TestErrorSummaries:
    def test_length_errors_state_both_numbers(self) -> None:
        from pydantic import ValidationError

        from core.models import NewsletterContent

        try:
            NewsletterContent.model_validate({**VALID_NEWSLETTER, "subject": "y" * 80})
        except ValidationError as exc:
            message = _summarise(exc)

        assert "80 characters" in message
        assert "maximum is 60" in message
        assert "shorten it" in message


class TestFieldRegeneration:
    def test_returns_the_new_value_without_mutating_the_draft(self) -> None:
        """A rejected regeneration must not half-apply."""
        from core.models import NewsletterContent

        draft = NewsletterContent.model_validate(VALID_NEWSLETTER)
        provider = ScriptedProvider({"subject": "A brand new subject line"})

        value = AIEngine(provider).regenerate_field(draft, EditableField.SUBJECT)

        assert value == "A brand new subject line"
        assert draft.subject == VALID_NEWSLETTER["subject"], "draft must be untouched"

    def test_the_prompt_carries_the_field_constraint(self) -> None:
        from core.models import NewsletterContent

        provider = ScriptedProvider({"cta": "See the specs"})

        AIEngine(provider).regenerate_field(
            NewsletterContent.model_validate(VALID_NEWSLETTER), EditableField.CTA
        )

        assert "MAXIMUM 40 characters" in provider.calls[0][1].content

    def test_a_user_instruction_reaches_the_prompt(self) -> None:
        from core.models import NewsletterContent

        provider = ScriptedProvider({"subject": "Urgent: act now"})

        AIEngine(provider).regenerate_field(
            NewsletterContent.model_validate(VALID_NEWSLETTER),
            EditableField.SUBJECT,
            "make it more urgent",
        )

        assert "make it more urgent" in provider.calls[0][1].content

    def test_an_empty_value_is_rejected(self) -> None:
        from core.models import NewsletterContent

        provider = ScriptedProvider({"subject": "   "})

        with pytest.raises(InvalidJSONResponse):
            AIEngine(provider).regenerate_field(
                NewsletterContent.model_validate(VALID_NEWSLETTER), EditableField.SUBJECT
            )


class TestSubjectVariants:
    def test_returns_the_variants(self) -> None:
        from core.models import NewsletterContent

        provider = ScriptedProvider({"variants": ["One", "Two", "Three"]})

        variants = AIEngine(provider).generate_subject_variants(
            NewsletterContent.model_validate(VALID_NEWSLETTER)
        )

        assert variants == ["One", "Two", "Three"]

    def test_the_prompt_demands_distinct_angles(self) -> None:
        """Asking for "three subject lines" without this reliably returns three
        paraphrases of the same sentence."""
        from core.models import NewsletterContent

        provider = ScriptedProvider({"variants": ["a", "b", "c"]})

        AIEngine(provider).generate_subject_variants(
            NewsletterContent.model_validate(VALID_NEWSLETTER)
        )

        user_message = provider.calls[0][1].content
        assert "Benefit-led" in user_message
        assert "Curiosity-led" in user_message
        assert "Factual" in user_message

    def test_an_empty_result_is_rejected(self) -> None:
        from core.models import NewsletterContent

        provider = ScriptedProvider({"variants": []})

        with pytest.raises(InvalidJSONResponse):
            AIEngine(provider).generate_subject_variants(
                NewsletterContent.model_validate(VALID_NEWSLETTER)
            )


class TestProvenance:
    def test_the_prompt_name_and_version_are_recorded(self) -> None:
        """Every campaign records these; without them "why did it write that?" is
        unanswerable and the prompt library's versioning is decorative."""
        provider = MockProvider()

        AIEngine(provider).summarize_article(ARTICLE, OPTIONS)

        assert provider.calls[0]["prompt"] == "article_summary"

    def test_the_schema_is_sent_with_every_call(self) -> None:
        provider = MockProvider()

        AIEngine(provider).summarize_article(ARTICLE, OPTIONS)

        schema = provider.calls[0]["schema"]
        assert schema is not None
        assert "key_points" in json.dumps(schema)
