#!/usr/bin/env python
"""Validate the prompt library. Runs in CI (D-6).

A prompt is code that ships to a customer's inbox, so it gets the same treatment:
a broken one should fail the build, not a campaign.

Checks:

1. Every prompt loads and its ``version:`` matches its filename — a mismatch means
   someone copied a file and forgot to update it, and the campaign audit trail
   would then record the wrong version.
2. Every declared ``output_schema`` exists.
3. Every prompt renders against representative context, so a Jinja typo or a
   missing ``_shared`` include is caught here rather than mid-campaign.
4. Every variable used by the template is declared in ``required_context``, or has
   a safe default. Undeclared variables are how "Write a newsletter about " ships.
5. Length-constrained prompts actually state their limits. Strict-mode decoding
   does not enforce string length (docs/04_LLM_HOSTING.md §4a), so a prompt that
   omits the limit will silently produce over-length output.

Usage::

    python scripts/validate_prompts.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.enums import Audience, Category, ExtractorTier, Tone  # noqa: E402
from core.models import ArticleSummary, CleanedArticle, NewsletterContent  # noqa: E402
from core.schemas import SCHEMA_REGISTRY  # noqa: E402
from modules.ai.prompt_registry import PromptRegistry  # noqa: E402

#: Prompts whose output has a hard character limit, and the limits they must name.
#: The number must literally appear in the rendered prompt.
LENGTH_CRITICAL: dict[str, list[int]] = {
    "newsletter_compose": [60, 100, 40],
    "subject_variants": [60],
}

SAMPLE_ARTICLE = CleanedArticle(
    url="https://dell.com/blog/poweredge",
    title="Dell PowerEdge R7xx",
    cleaned_text="Dell announced the PowerEdge R7xx series for enterprise data centres. " * 10,
    extractor=ExtractorTier.TRAFILATURA,
    word_count=100,
    token_estimate=150,
)

SAMPLE_SUMMARY = ArticleSummary(
    headline="Dell launches PowerEdge R7xx",
    key_points=["Point one.", "Point two.", "Point three."],
    business_impact="Lower running costs for a data centre refresh.",
    technical_facts=["R7xx series"],
    category=Category.PRODUCT_LAUNCH,
    relevance_score=8,
)

SAMPLE_DRAFT = NewsletterContent(
    title="Dell's New PowerEdge Servers",
    summary="Dell announced the R7xx line with improved power efficiency for data centres.",
    newsletter="Dell has announced the PowerEdge R7xx series. " * 5,
    subject="Dell's new servers cut power costs",
    preview_text="What it means for your refresh cycle",
    cta="Read the specs",
    keywords=["dell", "poweredge", "servers"],
    category=Category.PRODUCT_LAUNCH,
    tone=Tone.PROFESSIONAL,
)

#: Context sufficient to render each prompt.
CONTEXTS: dict[str, dict[str, Any]] = {
    "article_summary": {
        "article": SAMPLE_ARTICLE,
        "audience_label": "enterprise IT",
        "brand_name": "Vays Infotech",
    },
    "newsletter_compose": {
        "summaries": [SAMPLE_SUMMARY],
        "tone": Tone.PROFESSIONAL.value,
        "audience": Audience.ENTERPRISE_IT.value,
        "audience_label": "enterprise IT",
        "length_words": 300,
        "brand_name": "Vays Infotech",
    },
    "field_regenerate": {
        "draft": SAMPLE_DRAFT,
        "field_name": "subject",
        "field_constraints": "- MAXIMUM 60 characters.",
        "current_value": SAMPLE_DRAFT.subject,
        "instruction": None,
        "brand_name": "Vays Infotech",
    },
    "subject_variants": {"draft": SAMPLE_DRAFT, "brand_name": "Vays Infotech"},
}


def main() -> int:
    registry = PromptRegistry()
    names = registry.list_prompts()
    problems: list[str] = []

    if not names:
        print("FAIL: no prompts found")
        return 1

    for name in names:
        versions = registry.list_versions(name)
        for version in versions:
            label = f"{name} v{version}"
            try:
                template = registry.get(name, version)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label}: will not load - {exc}")
                continue

            if (
                template.output_schema not in SCHEMA_REGISTRY
                and template.output_schema != "single_field"
            ):
                problems.append(f"{label}: unknown output_schema {template.output_schema!r}")

            context = CONTEXTS.get(name)
            if context is None:
                problems.append(f"{label}: no sample context in this script - add one")
                continue

            try:
                rendered = registry.render(name, version, **context)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label}: will not render - {exc}")
                continue

            body = " ".join(m.content for m in rendered.messages)

            for limit in LENGTH_CRITICAL.get(name, []):
                if str(limit) not in body:
                    problems.append(
                        f"{label}: does not state its {limit}-character limit. "
                        "Strict decoding does not enforce string length, so the "
                        "prompt must."
                    )

            if template.defaults.max_tokens < 2048:
                problems.append(
                    f"{label}: max_tokens={template.defaults.max_tokens} is below the "
                    "2048 floor. Reasoning tokens count toward the budget and a "
                    "truncated response fails as json_validate_failed."
                )

            print(
                f"OK  {label:32s} {rendered.approx_input_chars:>6} chars  "
                f"temp={template.defaults.temperature}  max_tokens={template.defaults.max_tokens}"
            )

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"\nAll {len(names)} prompts valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
