"""Ingestion service tests — the full URL-to-persisted-article path.

The behaviour under test that matters most is partial failure: pasting five URLs
where one site blocks scrapers must yield four articles and one clear
explanation, never a dead end (FR-1.8).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from core.enums import ArticleStatus, ExtractorTier
from core.exceptions import ValidationError
from modules.repository.article_repo import ArticleRepository
from services.ingestion_service import IngestionService

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "html"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


def page(name: str = "dell_clean_article.html") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def service(db_session: Session, set_env) -> IngestionService:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**{**MINIMAL_ENV, "SCRAPER_RESPECT_ROBOTS": "false", "SCRAPER_MAX_RETRIES": "0"})
    return IngestionService()


class TestBatchIngestion:
    @respx.mock
    def test_ingests_multiple_urls(self, service: IngestionService) -> None:
        for i in range(3):
            respx.get(f"https://dell.com/post{i}").mock(
                return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
            )

        result = service.ingest_urls([f"https://dell.com/post{i}" for i in range(3)])

        assert result.succeeded == 3
        assert not result.failures
        assert all(a.id > 0 for a in result.articles)

    @respx.mock
    def test_source_order_is_preserved(self, service: IngestionService) -> None:
        """Order is the story order in the newsletter, and concurrent extraction
        would otherwise return them in completion order."""
        for name in ("charlie", "alpha", "bravo"):
            respx.get(f"https://dell.com/{name}").mock(
                return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
            )

        result = service.ingest_urls(
            ["https://dell.com/charlie", "https://dell.com/alpha", "https://dell.com/bravo"]
        )

        assert [a.url for a in result.articles] == [
            "https://dell.com/charlie",
            "https://dell.com/alpha",
            "https://dell.com/bravo",
        ]

    @respx.mock
    def test_one_blocked_site_does_not_abort_the_batch(self, service: IngestionService) -> None:
        """FR-1.8. The single most important behaviour in this service."""
        respx.get("https://dell.com/ok").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )
        respx.get("https://blocked.com/post").mock(return_value=httpx.Response(403))
        respx.get("https://cisco.com/ok").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )

        result = service.ingest_urls(
            ["https://dell.com/ok", "https://blocked.com/post", "https://cisco.com/ok"]
        )

        assert result.succeeded == 2
        assert "https://blocked.com/post" in result.failures
        assert "blocked automated access" in result.failures["https://blocked.com/post"]

    @respx.mock
    def test_failure_reasons_are_user_facing_not_technical(self, service: IngestionService) -> None:
        respx.get("https://dell.com/gone").mock(return_value=httpx.Response(404))

        result = service.ingest_urls(["https://dell.com/gone"])
        message = result.failures["https://dell.com/gone"]

        assert "doesn't exist" in message
        assert "404" not in message
        assert "http" not in message.lower().replace("https://", "")

    @respx.mock
    def test_unsafe_urls_are_reported_not_fetched(self, service: IngestionService) -> None:
        result = service.ingest_urls(["http://169.254.169.254/latest/meta-data/"])

        assert result.succeeded == 0
        assert not respx.calls
        assert "internal or private" in next(iter(result.failures.values()))

    @respx.mock
    def test_a_page_with_no_article_text_reports_the_manual_fallback(
        self, service: IngestionService
    ) -> None:
        respx.get("https://hp.com/landing").mock(
            return_value=httpx.Response(
                200, text=page("short_landing_page.html"), headers=HTML_HEADERS
            )
        )

        result = service.ingest_urls(["https://hp.com/landing"])

        assert "Paste text manually" in result.failures["https://hp.com/landing"]

    @respx.mock
    def test_progress_is_reported_per_url(self, service: IngestionService) -> None:
        """A 6-URL batch takes long enough that silence reads as a hang."""
        for i in range(3):
            respx.get(f"https://dell.com/p{i}").mock(
                return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
            )
        updates: list[str] = []

        service.ingest_urls(
            [f"https://dell.com/p{i}" for i in range(3)], on_progress=updates.append
        )

        assert len(updates) == 3
        assert "3/3" in updates[-1]


class TestDeduplication:
    @respx.mock
    def test_duplicate_urls_are_fetched_once(self, service: IngestionService) -> None:
        route = respx.get("https://dell.com/post").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )

        result = service.ingest_urls(["https://dell.com/post", "https://dell.com/post"])

        assert route.call_count == 1
        assert result.duplicates_removed == 1

    @respx.mock
    def test_urls_differing_only_by_fragment_or_case_are_one_article(
        self, service: IngestionService
    ) -> None:
        """Normalising before de-duplication is what makes this work — otherwise
        the same article is fetched, summarised and billed twice."""
        route = respx.get("https://dell.com/post").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )

        result = service.ingest_urls(
            ["https://dell.com/post", "https://DELL.com/post#section-2", "dell.com/post"]
        )

        assert route.call_count == 1
        assert result.duplicates_removed == 2

    def test_blank_entries_are_ignored(self, service: IngestionService) -> None:
        with pytest.raises(ValidationError):
            service.ingest_urls(["", "   ", "\n"])


class TestBatchValidation:
    def test_an_empty_batch_is_rejected(self, service: IngestionService) -> None:
        with pytest.raises(ValidationError) as exc_info:
            service.ingest_urls([])

        assert "at least one" in exc_info.value.user_message

    def test_too_many_urls_is_rejected_with_the_count(self, service: IngestionService) -> None:
        with pytest.raises(ValidationError) as exc_info:
            service.ingest_urls([f"https://dell.com/p{i}" for i in range(11)])

        assert "11" in exc_info.value.user_message


class TestPersistence:
    @respx.mock
    def test_articles_are_persisted_before_any_llm_call(
        self, service: IngestionService, db_session: Session
    ) -> None:
        """NFR-R1: an expired Colab session must cost the regeneration, never the
        extraction work."""
        respx.get("https://dell.com/post").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )

        service.ingest_urls(["https://dell.com/post"])

        assert ArticleRepository(db_session).count() == 1

    @respx.mock
    def test_the_stored_article_carries_its_provenance(
        self, service: IngestionService, db_session: Session
    ) -> None:
        respx.get("https://dell.com/post").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )

        article = service.ingest_urls(["https://dell.com/post"]).articles[0]
        row = ArticleRepository(db_session).get(article.id)

        assert row is not None
        assert row.extractor_used == ExtractorTier.TRAFILATURA
        assert row.status == ArticleStatus.EXTRACTED
        assert row.word_count > 0
        assert row.raw_text  # the pre-cleaning text is kept for auditing

    @respx.mock
    def test_a_stored_article_can_be_read_back(self, service: IngestionService) -> None:
        respx.get("https://dell.com/post").mock(
            return_value=httpx.Response(200, text=page(), headers=HTML_HEADERS)
        )

        created = service.ingest_urls(["https://dell.com/post"]).articles[0]

        assert service.get_article(created.id) is not None
        assert service.get_article(999_999) is None


class TestManualPaste:
    def test_pasted_text_enters_the_same_pipeline(self, service: IngestionService) -> None:
        body = "Dell has announced new PowerEdge servers for enterprise data centres. " * 20

        article = service.ingest_manual("Dell PowerEdge Launch", body, "https://dell.com/blog")

        assert article.id > 0
        assert article.extractor == ExtractorTier.MANUAL
        assert article.word_count > 50

    def test_a_source_url_is_optional(self, service: IngestionService) -> None:
        body = "Dell has announced new PowerEdge servers for enterprise data centres. " * 20

        assert service.ingest_manual("Title", body).url is None

    def test_a_stub_of_text_is_rejected_with_guidance(self, service: IngestionService) -> None:
        with pytest.raises(ValidationError) as exc_info:
            service.ingest_manual("Title", "Too short.")

        assert "full article text" in exc_info.value.user_message
