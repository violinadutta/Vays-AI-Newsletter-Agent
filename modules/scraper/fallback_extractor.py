"""Tier 3 — BeautifulSoup heuristic.

The last resort. Lower quality than the first two tiers, but it essentially
never fails: it strips the obvious non-content elements and then picks whichever
container holds the most paragraph text.

Its job is not to be good. Its job is to turn "we got nothing" into "we got
something the user can look at and decide about", which is a much better place
to be than an error message.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from config import get_logger
from core.enums import ExtractorTier
from core.models import ExtractedArticle

log = get_logger(__name__)

#: Elements that are never article content.
_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "iframe",
    "svg",
    "button",
    "figure",
)

#: Containers that usually *are* article content, in descending confidence.
_CANDIDATE_SELECTORS = (
    "article",
    "main",
    "[role=main]",
    ".post-content",
    ".article-body",
    ".entry-content",
    "#content",
)


class FallbackExtractor:
    """Heuristic extraction using BeautifulSoup."""

    tier = ExtractorTier.FALLBACK

    def extract(self, html: str, url: str | None) -> ExtractedArticle | None:
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001 - malformed markup falls back to the stdlib parser
            try:
                soup = BeautifulSoup(html, "html.parser")
            except Exception as exc:  # noqa: BLE001
                log.warning("extract.tier_error", tier=self.tier.value, error=str(exc))
                return None

        title = self._title(soup)

        for tag in soup(_STRIP_TAGS):
            tag.decompose()

        container = self._best_container(soup)
        if container is None:
            return None

        paragraphs = [
            text
            for para in container.find_all(["p", "h2", "h3", "li"])
            if len(text := para.get_text(" ", strip=True)) > 20
        ]
        text = "\n\n".join(paragraphs).strip()
        if not text:
            return None

        return ExtractedArticle(
            url=url,
            title=title or "(untitled)",
            text=text,
            extractor=self.tier,
        )

    @staticmethod
    def _title(soup: BeautifulSoup) -> str | None:
        """Prefer the on-page ``<h1>`` over ``<title>``.

        ``<title>`` usually carries site branding — "Article Name | Dell
        Technologies" — which then leaks into the generated newsletter headline.
        """
        h1 = soup.find("h1")
        if h1 is not None:
            heading = h1.get_text(" ", strip=True)
            if heading:
                return heading
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return None

    @staticmethod
    def _best_container(soup: BeautifulSoup) -> Tag | None:
        """Pick the element holding the most paragraph text."""
        best: Tag | None = None
        best_length = 0

        for selector in _CANDIDATE_SELECTORS:
            for candidate in soup.select(selector):
                length = sum(len(p.get_text(strip=True)) for p in candidate.find_all("p"))
                if length > best_length:
                    best, best_length = candidate, length

        # Nothing matched a known container: fall back to the densest <div>, then
        # to <body>. Crude, but it is the difference between a usable draft and
        # a dead end.
        if best is None:
            for div in soup.find_all("div"):
                length = sum(
                    len(p.get_text(strip=True)) for p in div.find_all("p", recursive=False)
                )
                if length > best_length:
                    best, best_length = div, length

        return best or soup.body
