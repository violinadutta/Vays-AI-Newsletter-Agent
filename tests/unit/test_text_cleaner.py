"""Tests for text normalisation.

Includes property-based tests: text cleaning is exactly the kind of pure function
that breaks on inputs nobody thought to write a case for.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.enums import ExtractorTier
from core.models import ExtractedArticle
from modules.cleaner.text_cleaner import TextCleaner


@pytest.fixture
def cleaner() -> TextCleaner:
    return TextCleaner()


class TestNormalisation:
    def test_smart_quotes_and_dashes_survive(self, cleaner: TextCleaner) -> None:
        """These must be preserved, not stripped — they are ordinary punctuation
        in OEM copy and mangling them shows up in the customer's inbox."""
        text = "Dell’s new servers — announced today — use café cooling."

        assert "’" in cleaner.clean_text(text)
        assert "café" in cleaner.clean_text(text)

    def test_non_breaking_spaces_become_ordinary_spaces(self, cleaner: TextCleaner) -> None:
        """NFKC folds them. CMS platforms scatter them through copy, and left in
        they break word counting and wrap oddly in email."""
        assert " " not in cleaner.clean_text("Dell PowerEdge servers")

    def test_zero_width_characters_are_removed(self, cleaner: TextCleaner) -> None:
        assert cleaner.clean_text("Dell​Power‌Edge") == "DellPowerEdge"

    def test_windows_line_endings_are_normalised(self, cleaner: TextCleaner) -> None:
        assert "\r" not in cleaner.clean_text("line one\r\nline two\r\n")

    def test_excess_blank_lines_collapse(self, cleaner: TextCleaner) -> None:
        assert "\n\n\n" not in cleaner.clean_text("one\n\n\n\n\ntwo")

    def test_repeated_spaces_collapse(self, cleaner: TextCleaner) -> None:
        assert cleaner.clean_text("Dell     PowerEdge") == "Dell PowerEdge"


class TestBoilerplateRemoval:
    @pytest.mark.parametrize(
        "line",
        [
            "Share this on LinkedIn",
            "Read more",
            "Related Articles",
            "Subscribe to our newsletter",
            "Accept all cookies",
            "Cookie Policy",
            "Advertisement",
            "5 min read",
            "Tags: dell, servers",
            "All rights reserved",
            "Follow us",
        ],
    )
    def test_boilerplate_lines_are_dropped(self, cleaner: TextCleaner, line: str) -> None:
        cleaned = cleaner.clean_text(f"Real article content here.\n{line}\nMore real content.")

        assert line not in cleaned
        assert "Real article content here." in cleaned

    def test_a_sentence_merely_containing_a_pattern_survives(self, cleaner: TextCleaner) -> None:
        """The patterns match whole lines only. Deleting any sentence containing
        'read more' would silently eat article prose."""
        text = "Customers can read more about the specification in the datasheet."

        assert text in cleaner.clean_text(text)

    def test_duplicate_paragraphs_are_removed(self, cleaner: TextCleaner) -> None:
        """Scraped pages repeat pull-quotes. Left in, the model reads the
        repetition as emphasis and over-weights that point."""
        para = "Dell has announced the PowerEdge R7xx series for enterprise data centres today."
        cleaned = cleaner.clean_text(f"{para}\n\nSomething else entirely here.\n\n{para}")

        assert cleaned.count(para) == 1

    def test_short_repeated_lines_are_kept(self, cleaner: TextCleaner) -> None:
        """Headings legitimately repeat; only substantial paragraphs are deduped."""
        assert cleaner.clean_text("Overview\n\nBody text.\n\nOverview").count("Overview") == 2


class TestTitleCleaning:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Dell's New Servers | Dell Technologies", "Dell's New Servers"),
            ("Q3 Threat Landscape - Cisco Blogs", "Q3 Threat Landscape"),
            ("Security Update – Fortinet", "Security Update"),
        ],
    )
    def test_site_branding_is_stripped(self, cleaner: TextCleaner, raw: str, expected: str) -> None:
        """Left in, the branding leaks into the generated headline and the
        newsletter reads like a scraped page."""
        assert cleaner.clean_title(raw) == expected

    def test_a_headline_containing_a_dash_is_not_truncated(self, cleaner: TextCleaner) -> None:
        """The separator heuristic must not eat real headline content."""
        raw = "Why hybrid cloud - and not public cloud - won the enterprise argument."

        assert cleaner.clean_title(raw) == raw

    def test_empty_title_gets_a_placeholder(self, cleaner: TextCleaner) -> None:
        assert cleaner.clean_title("   ") == "(untitled)"


class TestLanguageDetection:
    def test_detects_english(self, cleaner: TextCleaner) -> None:
        text = (
            "Dell Technologies has announced a refresh of its mainstream rack server line "
            "aimed at enterprise data centres and their operators this quarter."
        )
        assert cleaner.detect_language(text) == "en"

    def test_short_text_returns_none_rather_than_guessing(self, cleaner: TextCleaner) -> None:
        assert cleaner.detect_language("Hi") is None


class TestCleanArticle:
    def test_produces_a_populated_cleaned_article(self, cleaner: TextCleaner) -> None:
        article = ExtractedArticle(
            url="https://dell.com/blog",
            title="Dell's Servers | Dell",
            text="Dell has announced new servers. " * 60,
            extractor=ExtractorTier.TRAFILATURA,
        )
        cleaned = cleaner.clean(article, max_tokens=6000)

        assert cleaned.title == "Dell's Servers"
        assert cleaned.word_count > 0
        assert cleaned.token_estimate > 0
        assert cleaned.was_truncated is False
        assert cleaned.extractor == ExtractorTier.TRAFILATURA

    def test_oversized_articles_are_flagged_as_truncated(self, cleaner: TextCleaner) -> None:
        # Each paragraph must be distinct, or de-duplication collapses them and
        # the article is no longer oversized — which is the cleaner working
        # correctly, but makes for a useless truncation test.
        article = ExtractedArticle(
            title="Long",
            text="\n\n".join(
                f"Paragraph {i} discusses a distinct aspect of the announcement. " * 20
                for i in range(80)
            ),
            extractor=ExtractorTier.TRAFILATURA,
        )
        cleaned = cleaner.clean(article, max_tokens=500)

        assert cleaned.was_truncated is True


class TestProperties:
    """Cleaning is a pure function, so properties are cheap to assert."""

    @given(st.text(max_size=2000))
    def test_never_raises_on_arbitrary_input(self, text: str) -> None:
        TextCleaner().clean_text(text)

    @given(st.text(max_size=2000))
    def test_normalisation_invariants_hold_for_any_input(self, text: str) -> None:
        """Whatever goes in, these are always true of what comes out.

        Note this does *not* assert the output is shorter: NFKC decomposition can
        legitimately lengthen text — ``¯`` becomes space + combining macron, and
        ``ﬁ`` becomes two characters. A naive "output ≤ input" property looks
        obvious and is wrong.
        """
        cleaned = TextCleaner().clean_text(text)

        assert "\r" not in cleaned
        assert "\n\n\n" not in cleaned
        assert "​" not in cleaned  # zero-width space
        assert "  " not in cleaned.replace("\n", " ") or "\n" in cleaned

    @given(st.text(max_size=2000))
    def test_is_idempotent(self, text: str) -> None:
        """Cleaning twice must equal cleaning once, or repeated processing of a
        saved draft would drift."""
        cleaner = TextCleaner()
        once = cleaner.clean_text(text)

        assert cleaner.clean_text(once) == once

    @given(st.text(max_size=500))
    def test_output_has_no_leading_or_trailing_whitespace(self, text: str) -> None:
        cleaned = TextCleaner().clean_text(text)

        assert cleaned == cleaned.strip()
