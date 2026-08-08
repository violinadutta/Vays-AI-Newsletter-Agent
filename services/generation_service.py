"""Generation use case: articles in, persisted draft out.

Orchestration only — the prompts live in ``prompts/``, the model call lives in
``modules/ai``, and persistence lives in ``modules/repository``. This module's
job is the *order* of those things, and the transaction boundary around them.

Two behaviours here are product requirements, not implementation detail:

* **Stage 1 runs per article, with bounded concurrency.** One article failing to
  summarise must not lose the others (the same rule as ingestion, FR-1.8).
* **Provenance is recorded on every campaign** — model, provider, prompt version,
  parameters, timings. Without it "why did it write that?" is unanswerable three
  weeks later, and the prompt library's versioning would be decorative.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from config import bind_correlation_id, get_logger, get_settings
from core.enums import EditableField
from core.exceptions import AIError, LLMUnavailableError, NewsletterAppError, ValidationError
from core.models import (
    ArticleSummary,
    CleanedArticle,
    GenerationOptions,
    GenerationRequest,
    NewsletterContent,
    NewsletterDraft,
)
from modules.ai.engine import AIEngine
from modules.ai.factory import create_provider
from modules.repository.article_repo import ArticleRepository
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

#: Stage-1 concurrency. Deliberately small: the value is overlapping network
#: latency, and a provider with a per-minute token ceiling punishes a wide fan-out
#: with 429s that cost more than the parallelism saves.
MAX_PARALLEL_SUMMARIES = 3


class GenerationService:
    """Turns persisted articles into a persisted newsletter draft."""

    def __init__(self, engine: AIEngine | None = None) -> None:
        settings = get_settings()
        self._engine = engine or AIEngine(create_provider(), brand_name=settings.brand.name)

    # ── the main use case ────────────────────────────────────────────────────
    def generate(
        self, request: GenerationRequest, *, on_progress: ProgressCallback | None = None
    ) -> NewsletterDraft:
        """Run the two-stage pipeline and persist the result.

        Raises:
            ValidationError: No usable articles were supplied.
            LLMUnavailableError: The provider is unreachable. Articles are already
                persisted, so nothing is lost (NFR-R1).
            InvalidJSONResponse: Output failed validation after the repair attempt.
        """
        correlation_id = bind_correlation_id()
        started = time.monotonic()

        articles = self._load(request.article_ids)
        if not articles:
            raise ValidationError(
                f"none of {request.article_ids} could be loaded",
                user_message="Those articles are no longer available. Extract them again.",
            )

        self._require_healthy_provider()

        summaries = self._summarise_all(articles, request.options, on_progress)
        if not summaries:
            raise AIError(
                "every article failed to summarise",
                user_message=(
                    "The AI couldn't summarise any of these articles. Try again, or "
                    "use fewer articles."
                ),
                context={"article_count": len(articles)},
            )

        if on_progress:
            on_progress("Writing the newsletter")
        content = self._engine.compose_newsletter(summaries, request.options)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        campaign_id = self._persist(request, articles, summaries, content, elapsed_ms)

        log.info(
            "generation.complete",
            campaign_id=campaign_id,
            articles=len(articles),
            summarised=len(summaries),
            duration_ms=elapsed_ms,
            correlation_id=correlation_id,
        )
        return NewsletterDraft(
            campaign_id=campaign_id,
            content=content,
            options=request.options,
            section_summaries=summaries,
            source_urls=[a.url for a in articles if a.url],
            model=self._engine.provider.name,
            provider=self._engine.provider.name,
            prompt_version=self._prompt_version(),
            generation_ms=elapsed_ms,
        )

    # ── single-field operations (FR-3.8, FR-3.10) ────────────────────────────
    def regenerate_field(
        self, campaign_id: int, field: EditableField, instruction: str | None = None
    ) -> str:
        """Regenerate one field and persist it.

        The regeneration counter is incremented on the campaign — it is one of the
        quality signals in PRD §7.1, and it only means something if it is counted
        every time rather than when someone remembers to.
        """
        bind_correlation_id()
        content = self._load_content(campaign_id)
        value = self._engine.regenerate_field(content, field, instruction)

        from core.models import ContentPatch

        with unit_of_work() as session:
            repo = CampaignRepository(session)
            repo.update_content(campaign_id, ContentPatch(**{field.value: value}))
            repo.increment_regeneration(campaign_id)

        return value

    def generate_subject_variants(self, campaign_id: int, count: int = 3) -> list[str]:
        """Alternative subject lines for the user to choose from."""
        bind_correlation_id()
        variants = self._engine.generate_subject_variants(self._load_content(campaign_id))
        return variants[:count]

    # ── internals ────────────────────────────────────────────────────────────
    def _require_healthy_provider(self) -> None:
        """Fail before stage 1 rather than part-way through it.

        A dead provider discovered on article three has already cost two calls and
        left the user watching a progress bar that will not finish.
        """
        health = self._engine.provider.health_check()
        if not health.healthy:
            raise LLMUnavailableError(
                f"provider {self._engine.provider.name} is unhealthy: {health.detail}",
                context={"provider": self._engine.provider.name, "detail": health.detail},
            )

    def _summarise_all(
        self,
        articles: list[CleanedArticle],
        options: GenerationOptions,
        on_progress: ProgressCallback | None,
    ) -> list[ArticleSummary]:
        """Stage 1, in parallel, tolerating individual failures."""
        summaries: list[ArticleSummary] = []
        total = len(articles)

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SUMMARIES) as pool:
            futures = {
                pool.submit(self._engine.summarize_article, article, options): article
                for article in articles
            }
            for done, future in enumerate(futures, start=1):
                article = futures[future]
                try:
                    summaries.append(future.result())
                except NewsletterAppError as exc:
                    log.warning("generation.summary_failed", url=article.url, error=exc.message)
                if on_progress:
                    on_progress(f"Summarised {done}/{total}")

        return summaries

    @staticmethod
    def _load(article_ids: list[int]) -> list[CleanedArticle]:
        with unit_of_work() as session:
            rows = ArticleRepository(session).get_many(article_ids)
            return [
                CleanedArticle(
                    url=row.url,
                    title=row.title,
                    cleaned_text=row.cleaned_text,
                    author=row.author,
                    published_at=row.published_at,
                    extractor=row.extractor_used,
                    word_count=row.word_count,
                    token_estimate=row.token_estimate,
                    language=row.language,
                    was_truncated=row.was_truncated,
                )
                for row in rows
            ]

    @staticmethod
    def _load_content(campaign_id: int) -> NewsletterContent:
        with unit_of_work() as session:
            campaign = CampaignRepository(session).get(campaign_id)
            if campaign is None:
                raise ValidationError(
                    f"campaign {campaign_id} not found",
                    user_message="That campaign no longer exists.",
                )
            return NewsletterContent(
                title=campaign.title or "",
                summary=campaign.summary or "",
                newsletter=campaign.newsletter or "",
                subject=campaign.subject or "",
                preview_text=campaign.preview_text or "",
                cta=campaign.cta or "",
                keywords=campaign.keywords or [],
                category=campaign.category,
                tone=campaign.tone,
            )

    def _persist(
        self,
        request: GenerationRequest,
        articles: list[CleanedArticle],
        summaries: list[ArticleSummary],
        content: NewsletterContent,
        elapsed_ms: int,
    ) -> int:
        provider = self._engine.provider
        with unit_of_work() as session:
            repo = CampaignRepository(session)
            campaign = repo.create(
                name=request.campaign_name or content.title[:120],
                content=content,
                provenance={
                    "model_name": getattr(provider, "model", provider.name),
                    "provider": provider.name,
                    "prompt_version": self._prompt_version(),
                    "generation_ms": elapsed_ms,
                    "generation_params": {
                        "tone": request.options.tone.value,
                        "length": request.options.length.value,
                        "audience": request.options.audience.value,
                        "guided_json": provider.supports_guided_json,
                    },
                },
            )
            repo.link_articles(
                campaign.id,
                request.article_ids,
                [s.model_dump(mode="json") for s in summaries],
            )
            return int(campaign.id)

    def _prompt_version(self) -> str:
        """The composition prompt's version — what a campaign is reproduced from."""
        return self._engine.registry.resolve_version("newsletter_compose")
