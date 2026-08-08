"""Ingestion use case: URLs in, persisted articles out.

Two behaviours here are product requirements rather than implementation details:

* **A failure never aborts the batch** (FR-1.8). Pasting five URLs where one site
  blocks scrapers should yield four articles and one clear explanation, not a
  dead end.
* **Articles are persisted before any LLM call.** An expired Colab session then
  costs the regeneration, never the extraction work (NFR-R1).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from config import bind_correlation_id, get_logger, get_settings
from config.constants import MAX_URLS_PER_BATCH
from core.exceptions import NewsletterAppError, ValidationError
from core.models import Article, CleanedArticle, ExtractedArticle, IngestionResult
from core.validators import normalise_url
from modules.cleaner.text_cleaner import TextCleaner
from modules.repository.article_repo import ArticleRepository
from modules.repository.database import unit_of_work
from modules.scraper.extractor import ArticleExtractor
from modules.scraper.fetcher import ArticleFetcher

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]


class IngestionService:
    """Fetch, extract, clean and persist articles."""

    def __init__(
        self,
        extractor: ArticleExtractor | None = None,
        cleaner: TextCleaner | None = None,
    ) -> None:
        self._extractor = extractor or ArticleExtractor()
        self._cleaner = cleaner or TextCleaner()
        self._settings = get_settings().scraper

    # ── batch ingestion ──────────────────────────────────────────────────────
    def ingest_urls(
        self, urls: list[str], *, on_progress: ProgressCallback | None = None
    ) -> IngestionResult:
        """Ingest a batch of URLs.

        Args:
            urls: Raw URLs as typed by the user.
            on_progress: Called with a human-readable status after each URL.

        Returns:
            The articles that succeeded and a per-URL reason for those that did
            not. Never raises for individual failures.

        Raises:
            ValidationError: If the batch itself is unusable (empty, or too big).
        """
        correlation_id = bind_correlation_id()
        unique, duplicates = self._deduplicate(urls)

        if not unique:
            raise ValidationError(
                "no URLs supplied",
                user_message="Paste at least one blog URL to get started.",
            )
        if len(unique) > MAX_URLS_PER_BATCH:
            raise ValidationError(
                f"{len(unique)} URLs exceeds the limit of {MAX_URLS_PER_BATCH}",
                user_message=(
                    f"You can process up to {MAX_URLS_PER_BATCH} articles at once. "
                    f"You pasted {len(unique)}."
                ),
            )

        started = time.monotonic()
        log.info("ingestion.started", count=len(unique), correlation_id=correlation_id)

        extracted: dict[str, ExtractedArticle] = {}
        failures: dict[str, str] = {}

        # One shared fetcher across the batch: connection pooling and the
        # robots.txt cache both matter when several URLs share a host.
        with (
            ArticleFetcher() as fetcher,
            ThreadPoolExecutor(max_workers=self._settings.max_concurrent) as pool,
        ):
            futures = {pool.submit(self._extract_one, url, fetcher): url for url in unique}
            for done, future in enumerate(futures, start=1):
                url = futures[future]
                try:
                    extracted[url] = future.result()
                except NewsletterAppError as exc:
                    failures[url] = exc.user_message
                    log.warning("ingestion.url_failed", url=url, error=exc.message)
                except Exception as exc:  # noqa: BLE001 - one bad URL must not kill the batch
                    failures[url] = "Something unexpected went wrong reading this page."
                    log.exception("ingestion.url_error", url=url, error=str(exc))
                if on_progress:
                    on_progress(f"{done}/{len(unique)} processed")

        # Persist in one transaction, in the order the user pasted them — that
        # ordering becomes the story order in the newsletter.
        articles: list[Article] = []
        with unit_of_work() as session:
            repo = ArticleRepository(session)
            for url in unique:
                if url not in extracted:
                    continue
                cleaned = self._clean(extracted[url])
                row = repo.create(cleaned, raw_text=extracted[url].text)
                session.flush()
                articles.append(self._to_model(row, cleaned))

        log.info(
            "ingestion.finished",
            succeeded=len(articles),
            failed=len(failures),
            duration_ms=int((time.monotonic() - started) * 1000),
            correlation_id=correlation_id,
        )
        return IngestionResult(articles=articles, failures=failures, duplicates_removed=duplicates)

    # ── manual paste (FR-1.7) ────────────────────────────────────────────────
    def ingest_manual(self, title: str, text: str, source_url: str | None = None) -> Article:
        """Ingest text the user pasted by hand.

        Raises:
            ValidationError: If the pasted text is too short to be an article.
        """
        if len(text.split()) < 50:
            raise ValidationError(
                "pasted text too short",
                user_message=(
                    "That's too short to work with. Paste the full article text — "
                    "at least a few paragraphs."
                ),
            )

        article = ArticleExtractor.from_manual_text(title, text, source_url)
        cleaned = self._clean(article)

        with unit_of_work() as session:
            row = ArticleRepository(session).create(cleaned, raw_text=text)
            session.flush()
            result = self._to_model(row, cleaned)

        log.info("article.manual", words=cleaned.word_count, url=source_url)
        return result

    def get_article(self, article_id: int) -> Article | None:
        with unit_of_work() as session:
            row = ArticleRepository(session).get(article_id)
            return None if row is None else self._from_row(row)

    # ── internals ────────────────────────────────────────────────────────────
    def _extract_one(self, url: str, fetcher: ArticleFetcher) -> ExtractedArticle:
        result = fetcher.fetch(url)
        return self._extractor.extract_from_html(result.html, result.url, fetch=result)

    def _clean(self, article: ExtractedArticle) -> CleanedArticle:
        return self._cleaner.clean(article, max_tokens=self._settings.max_input_tokens)

    @staticmethod
    def _deduplicate(urls: list[str]) -> tuple[list[str], int]:
        """Normalise and de-duplicate, preserving the order the user pasted.

        Normalising first means ``dell.com/blog`` and ``https://DELL.com/blog#x``
        are recognised as the same article rather than fetched twice.
        """
        seen: set[str] = set()
        unique: list[str] = []
        duplicates = 0

        for raw in urls:
            if not raw.strip():
                continue
            try:
                candidate = normalise_url(raw)
            except NewsletterAppError:
                candidate = raw.strip()  # keep it; validation reports it properly later
            if candidate in seen:
                duplicates += 1
                continue
            seen.add(candidate)
            unique.append(candidate)

        return unique, duplicates

    @staticmethod
    def _to_model(row: object, cleaned: CleanedArticle) -> Article:
        return Article(
            id=row.id,  # type: ignore[attr-defined]
            raw_text=row.raw_text,  # type: ignore[attr-defined]
            created_at=row.created_at,  # type: ignore[attr-defined]
            **cleaned.model_dump(),
        )

    @staticmethod
    def _from_row(row: object) -> Article:
        return Article(
            id=row.id,  # type: ignore[attr-defined]
            url=row.url,  # type: ignore[attr-defined]
            title=row.title,  # type: ignore[attr-defined]
            cleaned_text=row.cleaned_text,  # type: ignore[attr-defined]
            raw_text=row.raw_text,  # type: ignore[attr-defined]
            author=row.author,  # type: ignore[attr-defined]
            published_at=row.published_at,  # type: ignore[attr-defined]
            extractor=row.extractor_used,  # type: ignore[attr-defined]
            word_count=row.word_count,  # type: ignore[attr-defined]
            token_estimate=row.token_estimate,  # type: ignore[attr-defined]
            language=row.language,  # type: ignore[attr-defined]
            was_truncated=row.was_truncated,  # type: ignore[attr-defined]
            status=row.status,  # type: ignore[attr-defined]
            error_message=row.error_message,  # type: ignore[attr-defined]
            created_at=row.created_at,  # type: ignore[attr-defined]
        )
