"""The AI engine — two-stage generation with validated, repairable output.

**Why two stages** (D-4): asking a model to "read these four articles and write a
newsletter" in one call produces shallow copy and drops articles. Summarising each
article independently, then composing from the summaries, produces better
structure, parallelises, and lets one failed article retry without redoing the
batch.

**The validation ladder.** Cheapest check first:

1. Constrained decoding — the provider guarantees *structure* (key set, types,
   enums, array bounds).
2. ``json.loads`` — cannot fail after (1) on a strict provider; if it does, that
   is a server bug and is logged loudly.
3. Pydantic — catches what the decoder cannot. **Notably string lengths**, which
   strict mode does not enforce (see ``docs/04_LLM_HOSTING.md`` §4a). This is not
   a theoretical rung: measured against Groq, an over-length subject line is a
   real and regular occurrence.
4. **Repair retry** — hand the model its own output plus the specific validation
   error and ask for a correction. One attempt, then fail.

The repair path exists because of a measured finding, not a hypothetical one.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from config import get_logger
from config.constants import (
    CTA_MAX_LENGTH,
    LENGTH_PRESET_WORDS,
    PREVIEW_TEXT_MAX_LENGTH,
    SUBJECT_MAX_LENGTH,
    TITLE_MAX_LENGTH,
)
from core.enums import AUDIENCE_LABELS, EditableField
from core.exceptions import InvalidJSONResponse
from core.models import (
    ArticleSummary,
    CleanedArticle,
    GenerationOptions,
    LLMResponse,
    Message,
    NewsletterContent,
)
from core.schemas import get_schema, single_field_schema
from modules.ai.base import LLMProvider
from modules.ai.prompt_registry import LATEST, PromptRegistry, RenderedPrompt, get_registry

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

#: Per-field constraints injected into the regeneration prompt. The decoder does
#: not enforce these, so the prompt must state them and Pydantic must check them.
FIELD_CONSTRAINTS: dict[EditableField, str] = {
    EditableField.SUBJECT: f"- The subject line: MAXIMUM {SUBJECT_MAX_LENGTH} characters.",
    EditableField.PREVIEW_TEXT: (
        f"- The preview text: MAXIMUM {PREVIEW_TEXT_MAX_LENGTH} characters."
    ),
    EditableField.CTA: f"- The CTA: MAXIMUM {CTA_MAX_LENGTH} characters, 2-5 words.",
    EditableField.TITLE: f"- The title: MAXIMUM {TITLE_MAX_LENGTH} characters.",
    EditableField.SUMMARY: "- The summary: between 50 and 600 characters.",
    EditableField.NEWSLETTER: "- The newsletter body: keep it close to its current length.",
    EditableField.KEYWORDS: "- Keywords: 3 to 8 lowercase terms, comma-separated in one string.",
    EditableField.CATEGORY: "- Category: one of the allowed values only.",
    EditableField.TONE: "- Tone: one of the allowed values only.",
}


class AIEngine:
    """Orchestrates prompts, the provider, and output validation."""

    def __init__(
        self,
        provider: LLMProvider,
        registry: PromptRegistry | None = None,
        *,
        brand_name: str = "Vays Infotech",
        repair_attempts: int = 1,
    ) -> None:
        self.provider = provider
        self.registry = registry or get_registry()
        self.brand_name = brand_name
        self.repair_attempts = repair_attempts

    # ── stage 1 ──────────────────────────────────────────────────────────────
    def summarize_article(
        self,
        article: CleanedArticle,
        options: GenerationOptions,
        *,
        version: str = LATEST,
    ) -> ArticleSummary:
        """Extract structured facts from one article. Faithful, not persuasive."""
        prompt = self.registry.render(
            "article_summary",
            version,
            article=article,
            audience_label=AUDIENCE_LABELS[options.audience],
            brand_name=self.brand_name,
        )
        return self._generate_validated(prompt, ArticleSummary)

    # ── stage 2 ──────────────────────────────────────────────────────────────
    def compose_newsletter(
        self,
        summaries: list[ArticleSummary],
        options: GenerationOptions,
        *,
        version: str = LATEST,
    ) -> NewsletterContent:
        """Compose the newsletter from stage-1 summaries.

        Summaries are ordered by relevance so the model leads with the strongest
        story rather than whichever article the user happened to paste first.
        """
        ordered = sorted(summaries, key=lambda s: s.relevance_score, reverse=True)
        prompt = self.registry.render(
            "newsletter_compose",
            version,
            summaries=ordered,
            tone=options.tone.value,
            audience=options.audience.value,
            audience_label=AUDIENCE_LABELS[options.audience],
            length_words=LENGTH_PRESET_WORDS[options.length.value],
            brand_name=self.brand_name,
        )
        return self._generate_validated(prompt, NewsletterContent)

    # ── single-field regeneration (FR-3.8) ───────────────────────────────────
    def regenerate_field(
        self,
        draft: NewsletterContent,
        field: EditableField,
        instruction: str | None = None,
        *,
        version: str = LATEST,
    ) -> str:
        """Regenerate one field, leaving every other field untouched.

        Returns the new value as a string. The caller applies it — this method
        deliberately does not mutate the draft, so a rejected regeneration cannot
        half-apply.
        """
        current = getattr(draft, field.value)
        current_text = ", ".join(current) if isinstance(current, list) else str(current)

        prompt = self.registry.render(
            "field_regenerate",
            version,
            draft=draft,
            field_name=field.value.replace("_", " "),
            field_constraints=FIELD_CONSTRAINTS.get(field, ""),
            current_value=current_text,
            instruction=instruction,
            brand_name=self.brand_name,
        )
        schema = single_field_schema(
            field.value, description=f"The regenerated {field.value.replace('_', ' ')}"
        )
        response = self._call(prompt, schema)

        value = response.payload.get(field.value)
        if not isinstance(value, str) or not value.strip():
            raise InvalidJSONResponse(
                f"regeneration of {field.value} returned {value!r}",
                context={"field": field.value},
            )

        log.info(
            "field.regenerated",
            field=field.value,
            was_chars=len(current_text),
            now_chars=len(value),
        )
        return value.strip()

    # ── subject variants (FR-3.10) ───────────────────────────────────────────
    def generate_subject_variants(
        self, draft: NewsletterContent, *, version: str = LATEST
    ) -> list[str]:
        """Three subject lines from distinct angles."""
        prompt = self.registry.render(
            "subject_variants", version, draft=draft, brand_name=self.brand_name
        )
        response = self._call(prompt, get_schema("subject_variants"))

        variants = response.payload.get("variants")
        if not isinstance(variants, list) or not variants:
            raise InvalidJSONResponse(
                f"subject_variants returned {variants!r}", context={"prompt": prompt.name}
            )
        return [str(v).strip() for v in variants if str(v).strip()]

    # ── internals ────────────────────────────────────────────────────────────
    def _call(self, prompt: RenderedPrompt, schema: dict[str, Any]) -> LLMResponse:
        return self.provider.generate(
            prompt.messages,
            json_schema=schema,
            params=prompt.params,
            prompt_name=prompt.name,
            prompt_version=prompt.version,
        )

    def _generate_validated(self, prompt: RenderedPrompt, model: type[T]) -> T:
        """Generate, validate, and repair once if validation fails.

        Raises:
            InvalidJSONResponse: If the output still fails validation after the
                repair attempt.
        """
        schema = get_schema(prompt.output_schema)
        started = time.monotonic()

        response = self._call(prompt, schema)
        try:
            validated = model.model_validate(response.payload)
        except ValidationError as first_error:
            log.warning(
                "generation.validation_failed",
                prompt=prompt.name,
                version=prompt.version,
                problems=_summarise(first_error),
                will_repair=self.repair_attempts > 0,
            )
            validated = self._repair(prompt, schema, model, response, first_error)

        log.info(
            "generation.ok",
            prompt=prompt.name,
            version=prompt.version,
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return validated

    def _repair(
        self,
        prompt: RenderedPrompt,
        schema: dict[str, Any],
        model: type[T],
        failed: LLMResponse,
        error: ValidationError,
    ) -> T:
        """Show the model its own output and the specific fault, and ask again.

        Naming the exact problem matters. "That was invalid, try again" produces
        another guess; "subject is 74 characters, the limit is 60" produces a
        correction.
        """
        last_error = error

        for attempt in range(1, self.repair_attempts + 1):
            repair_messages = [
                *prompt.messages,
                Message(role="assistant", content=str(failed.payload)),
                Message(
                    role="user",
                    content=(
                        "That response was rejected:\n"
                        f"{_summarise(last_error)}\n\n"
                        "Return the complete object again with ONLY those problems fixed. "
                        "Keep every other field exactly as it was."
                    ),
                ),
            ]
            retried = self.provider.generate(
                repair_messages,
                json_schema=schema,
                params=prompt.params,
                prompt_name=prompt.name,
                prompt_version=prompt.version,
            )
            try:
                repaired = model.model_validate(retried.payload)
            except ValidationError as exc:
                last_error = exc
                log.warning(
                    "generation.repair_failed",
                    prompt=prompt.name,
                    attempt=attempt,
                    problems=_summarise(exc),
                )
                continue

            log.info("generation.repaired", prompt=prompt.name, attempt=attempt)
            return repaired

        raise InvalidJSONResponse(
            f"{prompt.name} v{prompt.version} failed validation after "
            f"{self.repair_attempts} repair attempt(s): {_summarise(last_error)}",
            context={
                "prompt": prompt.name,
                "version": prompt.version,
                "problems": _summarise(last_error),
            },
        )


def _summarise(error: ValidationError) -> str:
    """Render a Pydantic error as something a model can act on.

    Length violations are stated with the actual and permitted counts, because
    "string too long" alone does not tell the model how much to cut.
    """
    parts: list[str] = []
    for item in error.errors():
        field = ".".join(str(loc) for loc in item["loc"]) or "(root)"
        ctx = item.get("ctx") or {}
        if "max_length" in ctx:
            actual = len(str(item.get("input", "")))
            parts.append(
                f"{field} is {actual} characters, the maximum is {ctx['max_length']} - shorten it"
            )
        elif "min_length" in ctx:
            actual = len(str(item.get("input", "")))
            parts.append(
                f"{field} is {actual} characters, the minimum is {ctx['min_length']} - expand it"
            )
        else:
            parts.append(f"{field}: {item['msg']}")
    return "; ".join(parts)
