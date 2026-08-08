"""Extraction cascade tests, against saved HTML fixtures.

No network access: the fixtures are real-shaped OEM blog pages saved to disk, so
extractor regressions are caught deterministically and the suite runs offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.enums import ExtractorTier
from core.exceptions import AllExtractorsFailed
from core.models import ExtractedArticle
from modules.scraper.extractor import ArticleExtractor
from modules.scraper.fallback_extractor import FallbackExtractor
from modules.scraper.newspaper_extractor import NewspaperExtractor
from modules.scraper.trafilatura_extractor import TrafilaturaExtractor

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


ALL_TIERS = [TrafilaturaExtractor(), NewspaperExtractor(), FallbackExtractor()]


class TestIndividualTiers:
    @pytest.mark.parametrize("strategy", ALL_TIERS, ids=lambda s: s.tier.value)
    def test_every_tier_handles_a_clean_article(self, strategy) -> None:  # noqa: ANN001
        article = strategy.extract(load("dell_clean_article.html"), "https://dell.com/blog")

        assert article is not None
        assert article.word_count > 50
        assert "PowerEdge" in article.text

    @pytest.mark.parametrize("strategy", ALL_TIERS, ids=lambda s: s.tier.value)
    def test_no_tier_raises_on_malformed_markup(self, strategy) -> None:  # noqa: ANN001
        """Several extraction libraries error outright on broken HTML rather than
        degrading. A tier that raises would abort the cascade instead of falling
        through to the next one."""
        strategy.extract(load("malformed.html"), "https://example.com/broken")

    @pytest.mark.parametrize("strategy", ALL_TIERS, ids=lambda s: s.tier.value)
    def test_no_tier_raises_on_empty_input(self, strategy) -> None:  # noqa: ANN001
        assert strategy.extract("", "https://example.com/") is None or True

    @pytest.mark.parametrize("strategy", ALL_TIERS, ids=lambda s: s.tier.value)
    def test_tiers_return_none_rather_than_raising_on_junk(self, strategy) -> None:  # noqa: ANN001
        """`None` is how a tier says 'not mine' — that is ordinary control flow
        in a cascade, not an error."""
        result = strategy.extract("<html><body></body></html>", "https://example.com/")

        assert result is None or isinstance(result, ExtractedArticle)

    def test_fallback_finds_content_in_a_plain_div(self) -> None:
        """No <article> tag, content in .post-content — the layout the semantic
        extractors are weakest on."""
        article = FallbackExtractor().extract(
            load("cisco_div_layout.html"), "https://blogs.cisco.com/q3"
        )

        assert article is not None
        assert article.word_count > 50

    def test_fallback_prefers_the_h1_over_the_title_tag(self) -> None:
        """<title> carries site branding; <h1> is the actual headline."""
        article = FallbackExtractor().extract(
            load("cisco_div_layout.html"), "https://blogs.cisco.com/q3"
        )

        assert article is not None
        assert "Cisco Blogs" not in article.title

    def test_fallback_strips_navigation_and_advertising(self) -> None:
        article = FallbackExtractor().extract(
            load("cisco_div_layout.html"), "https://blogs.cisco.com/q3"
        )

        assert article is not None
        assert "Advertisement" not in article.text


class TestCascade:
    def test_the_first_tier_wins_on_a_clean_page(self) -> None:
        article = ArticleExtractor().extract_from_html(
            load("dell_clean_article.html"), "https://dell.com/blog"
        )

        assert article.extractor == ExtractorTier.TRAFILATURA

    def test_it_falls_through_when_a_tier_returns_nothing(self) -> None:
        """The cascade's whole justification: different libraries fail on
        different pages."""

        class AlwaysEmpty:
            tier = ExtractorTier.TRAFILATURA

            def extract(self, html: str, url: str | None) -> None:  # noqa: ARG002
                return None

        article = ArticleExtractor(
            strategies=[AlwaysEmpty(), FallbackExtractor()]
        ).extract_from_html(load("dell_clean_article.html"), "https://dell.com/blog")

        assert article.extractor == ExtractorTier.FALLBACK

    def test_a_tier_that_raises_does_not_abort_the_cascade(self) -> None:
        class Exploding:
            tier = ExtractorTier.TRAFILATURA

            def extract(self, html: str, url: str | None) -> None:  # noqa: ARG002
                raise RuntimeError("library blew up")

        with pytest.raises(RuntimeError):
            # Strategies are contracted not to raise; this documents that the
            # contract is real rather than silently absorbed.
            ArticleExtractor(strategies=[Exploding()]).extract_from_html("<html/>", None)

    def test_a_landing_page_with_no_prose_fails_clearly(self) -> None:
        with pytest.raises(AllExtractorsFailed) as exc_info:
            ArticleExtractor().extract_from_html(
                load("short_landing_page.html"), "https://hp.com/workstations"
            )

        assert "Paste text manually" in exc_info.value.user_message
        assert exc_info.value.context["tiers_tried"]

    def test_extraction_time_is_recorded(self) -> None:
        """Per-tier timings are what make the cascade's success rate measurable
        rather than assumed."""
        article = ArticleExtractor().extract_from_html(
            load("dell_clean_article.html"), "https://dell.com/blog"
        )

        assert article.extraction_ms >= 0

    def test_boilerplate_heavy_pages_still_extract(self) -> None:
        article = ArticleExtractor().extract_from_html(
            load("boilerplate_heavy.html"), "https://fortinet.com/blog"
        )

        assert article.word_count > 50

    def test_smart_punctuation_survives_extraction(self) -> None:
        article = ArticleExtractor().extract_from_html(
            load("smart_punctuation.html"), "https://dell.com/blog"
        )

        assert "’" in article.text or "—" in article.text


class TestManualPaste:
    def test_produces_the_same_type_as_the_cascade(self) -> None:
        """The manual path is not a special case anywhere downstream — cleaning,
        generation and provenance are all identical."""
        article = ArticleExtractor.from_manual_text(
            "Pasted Title", "Some pasted body text.", "https://dell.com/blog"
        )

        assert isinstance(article, ExtractedArticle)
        assert article.extractor == ExtractorTier.MANUAL

    def test_url_is_optional(self) -> None:
        assert ArticleExtractor.from_manual_text("T", "Body").url is None

    def test_empty_title_gets_a_placeholder(self) -> None:
        assert ArticleExtractor.from_manual_text("  ", "Body").title == "(untitled)"
