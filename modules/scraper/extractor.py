"""The extraction cascade — the only scraper class services talk to.

Different libraries fail on different pages. Trying three in order converts a
~85% single-extractor success rate into ~95%+, and the tier that succeeded is
recorded on every article so that claim stays measurable rather than assumed.
"""

from __future__ import annotations

import time

from config import get_logger, get_settings
from core.enums import ExtractorTier
from core.exceptions import AllExtractorsFailed
from core.models import ExtractedArticle
from modules.scraper.base import ExtractorStrategy
from modules.scraper.fallback_extractor import FallbackExtractor
from modules.scraper.fetcher import ArticleFetcher, FetchResult
from modules.scraper.newspaper_extractor import NewspaperExtractor
from modules.scraper.trafilatura_extractor import TrafilaturaExtractor

log = get_logger(__name__)


class ArticleExtractor:
    """Runs the extraction tiers in order and returns the first usable result."""

    def __init__(
        self,
        strategies: list[ExtractorStrategy] | None = None,
        fetcher: ArticleFetcher | None = None,
    ) -> None:
        self._strategies: list[ExtractorStrategy] = strategies or [
            TrafilaturaExtractor(),
            NewspaperExtractor(),
            FallbackExtractor(),
        ]
        self._fetcher = fetcher
        self._min_words = get_settings().scraper.min_word_count

    def extract(self, url: str) -> ExtractedArticle:
        """Fetch and extract an article.

        Raises:
            InvalidURLError: The URL is unsafe or malformed.
            FetchError: The page could not be retrieved.
            AllExtractorsFailed: Every tier returned too little usable text.
        """
        fetcher = self._fetcher or ArticleFetcher()
        try:
            result = fetcher.fetch(url)
        finally:
            if self._fetcher is None:
                fetcher.close()
        return self.extract_from_html(result.html, result.url, fetch=result)

    def extract_from_html(
        self, html: str, url: str | None, fetch: FetchResult | None = None
    ) -> ExtractedArticle:
        """Run the cascade over already-fetched HTML.

        The best *rejected* result is kept: if every tier falls short of the word
        threshold, the longest one is still returned when it is at least half the
        minimum. A 120-word article the user can read and decide about beats an
        error message, and the UI shows the word count either way.
        """
        started = time.monotonic()
        best: ExtractedArticle | None = None

        for strategy in self._strategies:
            article = strategy.extract(html, url)
            if article is None:
                log.debug("extract.tier_empty", tier=strategy.tier.value, url=url)
                continue

            words = article.word_count
            if words >= self._min_words:
                elapsed = int((time.monotonic() - started) * 1000)
                log.info(
                    "article.extracted",
                    url=url,
                    tier=strategy.tier.value,
                    word_count=words,
                    duration_ms=elapsed,
                )
                return article.model_copy(update={"extraction_ms": elapsed})

            log.warning(
                "extract.too_short",
                tier=strategy.tier.value,
                url=url,
                word_count=words,
                minimum=self._min_words,
            )
            if best is None or words > best.word_count:
                best = article

        if best is not None and best.word_count >= self._min_words // 2:
            elapsed = int((time.monotonic() - started) * 1000)
            log.info(
                "article.extracted_short",
                url=url,
                tier=best.extractor.value,
                word_count=best.word_count,
            )
            return best.model_copy(update={"extraction_ms": elapsed})

        raise AllExtractorsFailed(
            f"all extractors failed for {url!r} (best was {best.word_count if best else 0} words)",
            context={
                "url": url,
                "best_word_count": best.word_count if best else 0,
                "tiers_tried": [s.tier.value for s in self._strategies],
                "status": fetch.status_code if fetch else None,
            },
        )

    @staticmethod
    def from_manual_text(title: str, text: str, url: str | None = None) -> ExtractedArticle:
        """Build an article from text the user pasted in (FR-1.7).

        Returns the same type as the cascade, so every downstream stage —
        cleaning, tokenising, generation, provenance — is identical. The manual
        path is not a special case anywhere but here.
        """
        return ExtractedArticle(
            url=url,
            title=title.strip() or "(untitled)",
            text=text.strip(),
            extractor=ExtractorTier.MANUAL,
        )
