"""Tier 1 — Trafilatura.

The primary extractor because published benchmarks put it at the top for
precision (F1 ~0.94, precision ~0.98) and it is actively maintained. Precision
matters more than recall here: a summary built from navigation menus and cookie
banners is worse than one built from slightly less article text.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

import trafilatura
from trafilatura.settings import use_config

from config import get_logger
from core.enums import ExtractorTier
from core.models import ExtractedArticle

log = get_logger(__name__)

# Trafilatura's own network timeout must be disabled: we already fetched the
# HTML ourselves through the SSRF-checked fetcher, and letting the library make
# its own requests would bypass every protection in fetcher.py.
_CONFIG = use_config()
_CONFIG.set("DEFAULT", "EXTRACTION_TIMEOUT", "0")


class TrafilaturaExtractor:
    """Extract article text and metadata with Trafilatura."""

    tier = ExtractorTier.TRAFILATURA

    def extract(self, html: str, url: str | None) -> ExtractedArticle | None:
        try:
            text = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                favor_precision=True,
                config=_CONFIG,
            )
        except Exception as exc:  # noqa: BLE001 - a tier failure is not an app error
            log.warning("extract.tier_error", tier=self.tier.value, error=str(exc))
            return None

        if not text or not text.strip():
            return None

        title, author, published = self._metadata(html, url)

        return ExtractedArticle(
            url=url,
            title=title or "(untitled)",
            text=text.strip(),
            author=author,
            published_at=published,
            extractor=self.tier,
        )

    @staticmethod
    def _metadata(html: str, url: str | None) -> tuple[str | None, str | None, datetime | None]:
        """Best-effort metadata. Never fatal — a missing byline is not a failure."""
        try:
            meta = trafilatura.extract_metadata(html, default_url=url)
        except Exception:  # noqa: BLE001
            return None, None, None
        if meta is None:
            return None, None, None

        published: datetime | None = None
        if meta.date:
            with contextlib.suppress(ValueError, TypeError):
                published = datetime.fromisoformat(str(meta.date))

        return meta.title, meta.author, published
