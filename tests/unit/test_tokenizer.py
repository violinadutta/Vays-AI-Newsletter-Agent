"""Tests for token estimation and budget-aware truncation (D-14)."""

from __future__ import annotations

import pytest

from modules.cleaner.tokenizer import (
    TRUNCATION_MARKER,
    estimate_tokens,
    truncate_to_budget,
)


def make_article(paragraphs: int, words_each: int = 40) -> str:
    return "\n\n".join(
        f"Paragraph {i} " + " ".join(f"word{j}" for j in range(words_each))
        for i in range(paragraphs)
    )


class TestEstimation:
    def test_empty_text_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_estimate_grows_with_length(self) -> None:
        assert estimate_tokens("a" * 1000) > estimate_tokens("a" * 100)

    def test_estimate_is_within_a_sane_range_of_word_count(self) -> None:
        """English prose runs roughly 1.3 tokens per word. The heuristic only has
        to land in that neighbourhood — it decides when to truncate, not what to
        bill for."""
        text = " ".join("word" for _ in range(1000))
        assert 900 < estimate_tokens(text) < 2500

    def test_estimation_errs_upward(self) -> None:
        """Under-estimating costs a rejected request from the model;
        over-estimating costs a few words of an article."""
        text = "a" * 370  # 100 tokens at exactly 3.7 chars/token, before the safety factor
        assert estimate_tokens(text) > 100


class TestTruncation:
    def test_text_within_budget_is_untouched(self) -> None:
        text = make_article(3)
        result = truncate_to_budget(text, max_tokens=10_000)

        assert result.text == text
        assert result.was_truncated is False

    def test_oversized_text_is_trimmed(self) -> None:
        result = truncate_to_budget(make_article(60), max_tokens=300)

        assert result.was_truncated is True
        assert result.final_tokens <= 300 * 1.1

    def test_the_opening_is_preserved(self) -> None:
        """The lead carries what the article is about — losing it produces a
        summary of the middle."""
        result = truncate_to_budget(make_article(60), max_tokens=400)

        assert "Paragraph 0" in result.text

    def test_the_ending_is_preserved(self) -> None:
        """OEM blogs put availability, pricing and next steps at the end. A blind
        tail cut throws away exactly the part a newsletter needs."""
        result = truncate_to_budget(make_article(60), max_tokens=400)

        assert "Paragraph 59" in result.text

    def test_the_removal_is_marked(self) -> None:
        """The model should know the text is not contiguous rather than infer a
        non-existent link between two distant paragraphs."""
        result = truncate_to_budget(make_article(60), max_tokens=400)

        assert TRUNCATION_MARKER.strip() in result.text

    def test_paragraphs_are_kept_whole(self) -> None:
        """Cutting mid-sentence hands the model a fragment, and it tends to
        summarise the fragment."""
        result = truncate_to_budget(make_article(40), max_tokens=300)

        for chunk in result.text.split(TRUNCATION_MARKER):
            for para in chunk.strip().split("\n\n"):
                if para.strip():
                    assert para.strip().startswith("Paragraph")

    def test_no_paragraph_is_duplicated(self) -> None:
        """Head and tail selections can overlap on short inputs; the same
        paragraph appearing twice would read as emphasis to the model."""
        result = truncate_to_budget(make_article(6), max_tokens=120)
        paragraphs = [
            p.strip()
            for chunk in result.text.split(TRUNCATION_MARKER)
            for p in chunk.split("\n\n")
            if p.strip()
        ]

        assert len(paragraphs) == len(set(paragraphs))

    @pytest.mark.parametrize("budget", [0, -1])
    def test_non_positive_budget_returns_the_input(self, budget: int) -> None:
        text = make_article(2)
        assert truncate_to_budget(text, budget).text == text

    def test_absurdly_small_budget_yields_empty_rather_than_garbage(self) -> None:
        result = truncate_to_budget(make_article(20), max_tokens=1)

        assert result.was_truncated is True
        assert result.text == ""

    def test_empty_input(self) -> None:
        assert truncate_to_budget("", 100).text == ""
