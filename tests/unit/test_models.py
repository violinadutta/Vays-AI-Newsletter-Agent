"""Tests for the domain DTOs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.enums import CampaignStatus, ExtractorTier, SendStatus
from core.models import (
    CampaignSendReport,
    CampaignSummary,
    ContentPatch,
    ExtractedArticle,
    IngestionResult,
    Page,
    RecipientValidation,
    SendResult,
)


class TestExtractedArticle:
    def test_word_count_is_derived(self) -> None:
        article = ExtractedArticle(
            url="https://dell.com/b",
            title="T",
            text="one two three",
            extractor=ExtractorTier.TRAFILATURA,
        )
        assert article.word_count == 3

    def test_url_is_optional_for_manual_paste(self) -> None:
        article = ExtractedArticle(title="T", text="x", extractor=ExtractorTier.MANUAL)
        assert article.url is None

    def test_unknown_field_is_rejected(self) -> None:
        """extra='forbid' turns a contract drift between modules into a loud
        failure instead of a silently ignored value."""
        with pytest.raises(ValidationError):
            ExtractedArticle(title="T", text="x", extractor=ExtractorTier.MANUAL, typo_field=1)


class TestIngestionResult:
    def test_partial_success_is_representable(self) -> None:
        """One bad URL in a batch of three must not abort the batch (FR-1.8)."""
        result = IngestionResult(failures={"https://blocked.com": "Site blocked us"})

        assert result.succeeded == 0
        assert result.any_succeeded is False
        assert "https://blocked.com" in result.failures

    def test_defaults_are_independent_between_instances(self) -> None:
        """A shared mutable default would leak one batch's failures into the next."""
        first, second = IngestionResult(), IngestionResult()
        first.failures["a"] = "b"

        assert second.failures == {}


class TestCampaignSummary:
    def _summary(self, sent: int, failed: int) -> CampaignSummary:
        return CampaignSummary(
            id=1,
            name="C",
            status=CampaignStatus.SENT,
            sent_count=sent,
            failed_count=failed,
            created_at=datetime.now(UTC),
        )

    def test_success_rate(self) -> None:
        assert self._summary(485, 15).success_rate == pytest.approx(0.97)

    def test_success_rate_is_none_before_any_send(self) -> None:
        """Returning 0.0 would render as '0% success' on a campaign that has not
        been sent yet — misleading in the History table."""
        assert self._summary(0, 0).success_rate is None


class TestPage:
    def test_total_pages_rounds_up(self) -> None:
        assert Page[int](items=[], total=21, page=1, page_size=20).total_pages == 2

    def test_exact_multiple(self) -> None:
        assert Page[int](items=[], total=40, page=1, page_size=20).total_pages == 2

    def test_empty_result_still_has_one_page(self) -> None:
        """Zero pages would break the pagination control."""
        assert Page[int](items=[], total=0, page=1, page_size=20).total_pages == 1


class TestContentPatch:
    def test_unset_fields_are_distinguishable_from_explicit_none(self) -> None:
        """'Leave this alone' and 'clear this' are different instructions, and a
        plain dict of Nones cannot express the difference."""
        patch = ContentPatch(subject="New subject")

        assert patch.model_dump(exclude_unset=True) == {"subject": "New subject"}

    def test_empty_patch_changes_nothing(self) -> None:
        assert ContentPatch().model_dump(exclude_unset=True) == {}


class TestSendResults:
    def test_ok_is_true_only_for_sent(self) -> None:
        assert SendResult(email="a@b.com", status=SendStatus.SENT).ok is True
        assert SendResult(email="a@b.com", status=SendStatus.FAILED).ok is False
        assert SendResult(email="a@b.com", status=SendStatus.SUPPRESSED).ok is False

    def test_partial_failure_is_not_fully_successful(self) -> None:
        report = CampaignSendReport(
            campaign_id=1, attempted=487, sent=485, failed=2, duration_s=222.0
        )
        assert report.fully_successful is False

    def test_a_clean_send_is_fully_successful(self) -> None:
        report = CampaignSendReport(
            campaign_id=1, attempted=487, sent=487, failed=0, duration_s=210.0
        )
        assert report.fully_successful is True

    def test_zero_attempted_is_not_success(self) -> None:
        """Sending to nobody is a configuration mistake, not a triumph."""
        report = CampaignSendReport(campaign_id=1, attempted=0, sent=0, failed=0, duration_s=0.0)
        assert report.fully_successful is False


class TestRecipientValidation:
    def test_reports_all_four_categories(self) -> None:
        """Suppressed contacts are surfaced, not silently dropped — the operator
        should know unsubscribed people were excluded."""
        validation = RecipientValidation(
            invalid={"bad row": "no @"},
            duplicates=["dup@vays.com"],
            suppressed=["gone@vays.com"],
        )

        assert validation.sendable_count == 0
        assert validation.invalid and validation.duplicates and validation.suppressed
