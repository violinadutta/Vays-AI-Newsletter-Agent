"""Tests for the guided-decoding JSON Schemas.

These guard the contract between the prompt and the code. If a schema stops
matching its Pydantic model, the decoder constrains generation to one shape while
validation expects another — and the failure appears as an unexplained
"the AI returned an unexpected response" during a live campaign.
"""

from __future__ import annotations

from typing import Any

import pydantic
import pytest

from core.models import ArticleSummary, NewsletterContent
from core.schemas import (
    ARTICLE_SUMMARY_SCHEMA,
    NEWSLETTER_SCHEMA,
    SUBJECT_VARIANTS_SCHEMA,
    single_field_schema,
    to_strict_schema,
)

ALL_SCHEMAS = [ARTICLE_SUMMARY_SCHEMA, NEWSLETTER_SCHEMA, SUBJECT_VARIANTS_SCHEMA]


def _walk(node: Any):  # noqa: ANN202
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


class TestStrictness:
    @pytest.mark.parametrize("schema", ALL_SCHEMAS)
    def test_no_dangling_refs(self, schema: dict) -> None:
        """Everything is inlined, so any grammar backend can consume it."""
        assert not any(isinstance(n, dict) and "$ref" in n for n in _walk(schema))

    @pytest.mark.parametrize("schema", ALL_SCHEMAS)
    def test_no_defs_section(self, schema: dict) -> None:
        assert "$defs" not in schema

    @pytest.mark.parametrize("schema", ALL_SCHEMAS)
    def test_every_object_forbids_extra_properties(self, schema: dict) -> None:
        """Must agree with ``extra="forbid"`` on the models. If the decoder allows
        a key the validator rejects, valid generations get thrown away."""
        for node in _walk(schema):
            if isinstance(node, dict) and (node.get("type") == "object" or "properties" in node):
                assert node.get("additionalProperties") is False

    @pytest.mark.parametrize("schema", ALL_SCHEMAS)
    def test_root_is_an_object_with_required_fields(self, schema: dict) -> None:
        assert schema["type"] == "object"
        assert schema["required"]


class TestNewsletterSchema:
    def test_covers_every_field_of_the_contract(self) -> None:
        assert set(NEWSLETTER_SCHEMA["properties"]) == set(NewsletterContent.model_fields)

    def test_all_nine_fields_are_required(self) -> None:
        """A partially-populated newsletter is not useful; the model must fill
        every field or the generation is a failure."""
        assert len(NEWSLETTER_SCHEMA["required"]) == 9

    @pytest.mark.parametrize("field", ["subject", "preview_text", "cta", "title", "summary"])
    def test_string_length_constraints_are_stripped(self, field: str) -> None:
        """Verified against Groq on 2026-08-07: a schema carrying ``maxLength``
        is refused outright with ``json_validate_failed``.

        This is a correction to an earlier assumption. The 60-character subject
        limit is **not** enforced at generation time — it is stated in the prompt
        and enforced by ``NewsletterContent``, with the engine's repair-retry
        covering an overshoot. Structure is guaranteed; string length is not.
        """
        spec = NEWSLETTER_SCHEMA["properties"][field]

        assert "maxLength" not in spec
        assert "minLength" not in spec

    def test_the_length_limits_still_exist_on_the_model(self) -> None:
        """Stripping them from the wire schema must not lose them — the model is
        where the limit is actually enforced now."""
        from core.models import NewsletterContent

        with pytest.raises(pydantic.ValidationError):
            NewsletterContent.model_validate(
                {
                    "title": "A perfectly reasonable title",
                    "summary": "s" * 60,
                    "newsletter": "n" * 120,
                    "subject": "x" * 61,  # one over
                    "preview_text": "A reasonable preview",
                    "cta": "Read more",
                    "keywords": ["a", "b", "c"],
                    "category": "Product Launch",
                    "tone": "professional",
                }
            )

    def test_array_bounds_are_kept(self) -> None:
        """``minItems``/``maxItems`` *are* supported by strict mode, so the
        keyword-count constraint is still enforced by the decoder."""
        keywords = NEWSLETTER_SCHEMA["properties"]["keywords"]

        assert keywords["minItems"] == 3
        assert keywords["maxItems"] == 8

    def test_category_is_a_closed_enum(self) -> None:
        """The model selects from eight values; it cannot invent a ninth."""
        assert len(NEWSLETTER_SCHEMA["properties"]["category"]["enum"]) == 8

    def test_tone_is_a_closed_enum(self) -> None:
        assert len(NEWSLETTER_SCHEMA["properties"]["tone"]["enum"]) == 5

    def test_every_property_is_required(self) -> None:
        """Strict mode rejects a schema whose ``required`` omits any property —
        Pydantic leaves out anything with a default, which failed the first real
        request with *"`required` must include every key in properties"*."""
        assert set(NEWSLETTER_SCHEMA["required"]) == set(NEWSLETTER_SCHEMA["properties"])


class TestArticleSummarySchema:
    def test_covers_every_field(self) -> None:
        assert set(ARTICLE_SUMMARY_SCHEMA["properties"]) == set(ArticleSummary.model_fields)

    def test_key_points_are_bounded(self) -> None:
        points = ARTICLE_SUMMARY_SCHEMA["properties"]["key_points"]
        assert (points["minItems"], points["maxItems"]) == (3, 5)

    def test_relevance_score_is_bounded(self) -> None:
        score = ARTICLE_SUMMARY_SCHEMA["properties"]["relevance_score"]
        assert (score["minimum"], score["maximum"]) == (1, 10)


class TestRoundTrip:
    """The real contract: anything the schema permits, the model must accept."""

    def test_a_conforming_newsletter_validates(self) -> None:
        payload = {
            "title": "Dell's New PowerEdge Servers",
            "summary": "Dell announced the R7xx line with improved power efficiency "
            "for enterprise data centres this quarter.",
            "newsletter": "Dell has announced the PowerEdge R7xx series. " * 5,
            "subject": "Dell's new servers cut power costs",
            "preview_text": "Plus what it means for your refresh cycle",
            "cta": "Read the specs",
            "keywords": ["dell", "poweredge", "servers"],
            "category": "Product Launch",
            "tone": "professional",
        }
        assert NewsletterContent.model_validate(payload).subject == payload["subject"]

    def test_a_conforming_summary_validates(self) -> None:
        payload = {
            "headline": "Dell launches PowerEdge R7xx",
            "key_points": ["Point one here.", "Point two here.", "Point three here."],
            "business_impact": "Lower running costs for data centre refreshes.",
            "technical_facts": ["R7xx series"],
            "category": "Product Launch",
            "relevance_score": 8,
        }
        assert ArticleSummary.model_validate(payload).relevance_score == 8

    def test_extra_keys_are_rejected_by_the_model(self) -> None:
        """Mirrors ``additionalProperties: false`` — both sides must agree."""
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            ArticleSummary.model_validate(
                {
                    "headline": "x",
                    "key_points": ["a.", "b.", "c."],
                    "business_impact": "y",
                    "technical_facts": [],
                    "category": "Security",
                    "relevance_score": 5,
                    "unexpected": "field",
                }
            )


class TestKeywordNormalisation:
    def test_keywords_are_lowercased_trimmed_and_deduplicated(self) -> None:
        content = NewsletterContent.model_validate(
            {
                "title": "A perfectly reasonable title",
                "summary": "s" * 60,
                "newsletter": "n" * 120,
                "subject": "A reasonable subject",
                "preview_text": "A reasonable preview",
                "cta": "Read more",
                "keywords": ["  Dell ", "dell", "PowerEdge", "poweredge", "servers"],
                "category": "Product Launch",
                "tone": "professional",
            }
        )
        assert content.keywords == ["dell", "poweredge", "servers"]


class TestSingleFieldSchema:
    def test_constrains_generation_to_exactly_one_property(self) -> None:
        """Stops the model from helpfully rewriting the body and discarding the
        user's edits when they only asked for a new subject line."""
        schema = single_field_schema("subject", description="A subject line", max_length=60)

        assert list(schema["properties"]) == ["subject"]
        assert schema["required"] == ["subject"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["subject"]["maxLength"] == 60

    def test_max_length_is_optional(self) -> None:
        assert (
            "maxLength"
            not in single_field_schema("newsletter", description="Body")["properties"]["newsletter"]
        )


class TestGeneratedFromModels:
    def test_schemas_are_derived_not_handwritten(self) -> None:
        """Single source of truth: changing a model constraint must change the
        schema automatically, or the two will drift."""
        assert to_strict_schema(NewsletterContent) == NEWSLETTER_SCHEMA
        assert to_strict_schema(ArticleSummary) == ARTICLE_SUMMARY_SCHEMA
