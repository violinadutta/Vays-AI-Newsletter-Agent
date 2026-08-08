"""Tier 2 — Newspaper4k.

Lower raw accuracy than Trafilatura, but it fails on *different* pages and
returns better metadata (byline, publish date, top image). That difference is
the entire justification for a cascade: one extractor's blind spots are not the
other's.

Note this is ``newspaper4k``, the maintained fork — **not** ``newspaper3k``,
which most tutorials still recommend and which has had no release since 2018.
"""

from __future__ import annotations

from config import get_logger
from core.enums import ExtractorTier
from core.models import ExtractedArticle

log = get_logger(__name__)


class NewspaperExtractor:
    """Extract with Newspaper4k, from already-fetched HTML."""

    tier = ExtractorTier.NEWSPAPER4K

    def extract(self, html: str, url: str | None) -> ExtractedArticle | None:
        # Imported lazily: `newspaper` emits an "nltk is not installed" warning at
        # import time, and there is no reason for that to appear when the app
        # starts — this tier is only reached when tier 1 has already failed.
        try:
            from newspaper import Article
        except ImportError:  # pragma: no cover - dependency is declared
            log.warning("extract.tier_unavailable", tier=self.tier.value)
            return None

        try:
            article = Article(url or "https://example.com/", language="en")
            # download() with input_html avoids any network access — we already
            # have the bytes, and letting it fetch would bypass the SSRF guard.
            article.download(input_html=html)
            article.parse()
            # NOTE: article.nlp() is deliberately never called. It downloads NLTK
            # corpora on first use, which would break offline development and add
            # a runtime download to a handover install.
        except Exception as exc:  # noqa: BLE001
            log.warning("extract.tier_error", tier=self.tier.value, error=str(exc))
            return None

        text = (article.text or "").strip()
        if not text:
            return None

        return ExtractedArticle(
            url=url,
            title=(article.title or "").strip() or "(untitled)",
            text=text,
            author=", ".join(article.authors) if article.authors else None,
            published_at=article.publish_date,
            extractor=self.tier,
        )
