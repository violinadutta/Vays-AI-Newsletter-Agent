"""The orchestrator that turns persisted articles into a persisted draft.

This service had **zero** test coverage until M9, despite being the module every
generation flows through. The tests below are chosen for what they protect: the
fail-fast health check, partial tolerance in stage 1, and the provenance record
that makes a campaign reproducible (D-6).

A stub engine is used rather than ``MockProvider`` so each failure mode can be
provoked directly — "the second article fails to summarise" is awkward to arrange
through a fixture provider and trivial with a stub.
"""

from __future__ import annotations

import pytest

from core.enums import Category, EditableField, Tone
from core.exceptions import AIError, LLMUnavailableError, ValidationError
from core.models import (
    ArticleSummary,
    CleanedArticle,
    GenerationOptions,
    GenerationRequest,
    HealthStatus,
    NewsletterContent,
)
from modules.repository.article_repo import ArticleRepository
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from services.generation_service import GenerationService

pytestmark = pytest.mark.integration


# ─────────────────────────────────────────────────────────────────────────────
#  Doubles
# ─────────────────────────────────────────────────────────────────────────────
class StubProvider:
    name = "stub"
    model = "stub-model-v1"
    supports_guided_json = True

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy
        self.health_checks = 0

    def health_check(self) -> HealthStatus:
        self.health_checks += 1
        return HealthStatus(
            healthy=self._healthy, detail="ok" if self._healthy else "connection refused"
        )


class StubRegistry:
    def __init__(self, version: str = "1.1.0") -> None:
        self.version = version

    def resolve_version(self, _name: str, _version: str = "latest") -> str:
        return self.version


def _summary(headline: str = "A headline about servers") -> ArticleSummary:
    return ArticleSummary(
        headline=headline,
        key_points=[
            "A redesigned airflow path moves more air per watt.",
            "Higher-efficiency power supplies are standard.",
            "Dell rates it at 20% lower draw at equal load.",
        ],
        business_impact="Lower cooling cost on an existing rack.",
        technical_facts=["two-socket rack line"],
        category=Category.INFRASTRUCTURE,
        relevance_score=8,
    )


def _content(subject: str = "Dell cuts rack power by a fifth") -> NewsletterContent:
    return NewsletterContent(
        title="OEM infrastructure round-up for this quarter",
        summary="Dell refreshed PowerEdge with efficiency as the headline change this quarter.",
        newsletter=(
            "Dell's new PowerEdge servers move more air through each watt.\n\n"
            "For an existing rack that means a lower bill and less heat to reject."
        ),
        subject=subject,
        preview_text="What the refresh means for your cycle",
        cta="See the specs",
        keywords=["dell", "poweredge", "efficiency"],
        category=Category.INFRASTRUCTURE,
        tone=Tone.PROFESSIONAL,
    )


class StubEngine:
    """Records calls and lets each stage be made to fail on demand."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        summary_failures: set[str] | None = None,
        prompt_version: str = "1.1.0",
    ) -> None:
        self.provider = StubProvider(healthy=healthy)
        self.registry = StubRegistry(prompt_version)
        self._summary_failures = summary_failures or set()
        self.composed_with: list[ArticleSummary] | None = None
        self.regenerate_calls: list[tuple[EditableField, str | None]] = []

    def summarize_article(
        self, article: CleanedArticle, _options: GenerationOptions
    ) -> ArticleSummary:
        if article.url in self._summary_failures:
            raise AIError(f"summarisation failed for {article.url}")
        return _summary(f"Summary of {article.title}"[:80])

    def compose_newsletter(
        self, summaries: list[ArticleSummary], _options: GenerationOptions, **_kwargs: object
    ) -> NewsletterContent:
        self.composed_with = summaries
        return _content()

    def regenerate_field(
        self, _content: NewsletterContent, field: EditableField, instruction: str | None
    ) -> str:
        self.regenerate_calls.append((field, instruction))
        return "Act now, view the specs"

    def generate_subject_variants(self, _content: NewsletterContent) -> list[str]:
        return ["Variant one about power", "Variant two about cost", "Variant three", "Extra"]


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)


def seed_articles(count: int = 2) -> list[int]:
    ids: list[int] = []
    with unit_of_work() as session:
        repo = ArticleRepository(session)
        for index in range(count):
            article = CleanedArticle(
                url=f"https://dell.com/blog/post-{index}",
                title=f"Article {index} about PowerEdge servers",
                cleaned_text="Dell announced a refresh of the rack line. " * 40,
                word_count=280,
                token_estimate=380,
                extractor="trafilatura",
                language="en",
            )
            ids.append(int(repo.create(article, raw_text="raw").id))
    return ids


# ─────────────────────────────────────────────────────────────────────────────
#  generate()
# ─────────────────────────────────────────────────────────────────────────────
class TestGenerate:
    def test_it_persists_a_campaign_and_returns_the_draft(self) -> None:
        ids = seed_articles(2)
        service = GenerationService(engine=StubEngine())

        draft = service.generate(GenerationRequest(article_ids=ids))

        assert draft.campaign_id > 0
        assert draft.content.subject == "Dell cuts rack power by a fifth"
        assert len(draft.section_summaries) == 2
        with unit_of_work() as session:
            assert CampaignRepository(session).get(draft.campaign_id) is not None

    def test_the_provider_is_checked_before_any_article_is_summarised(self) -> None:
        """A dead provider found on article three has already cost two calls and
        left the user watching a progress bar that will never finish."""
        ids = seed_articles(3)
        engine = StubEngine(healthy=False)
        service = GenerationService(engine=engine)

        with pytest.raises(LLMUnavailableError):
            service.generate(GenerationRequest(article_ids=ids))

        assert engine.composed_with is None, "composition ran despite an unhealthy provider"

    def test_unknown_article_ids_are_refused_with_a_recoverable_message(self) -> None:
        service = GenerationService(engine=StubEngine())

        with pytest.raises(ValidationError) as exc:
            service.generate(GenerationRequest(article_ids=[9999]))

        assert "Extract them again" in exc.value.user_message

    def test_one_failed_summary_does_not_lose_the_others(self) -> None:
        """Partial tolerance is the point of the per-article try/except: a single
        awkward article must not throw away work already paid for."""
        ids = seed_articles(3)
        engine = StubEngine(summary_failures={"https://dell.com/blog/post-1"})
        service = GenerationService(engine=engine)

        draft = service.generate(GenerationRequest(article_ids=ids))

        assert len(draft.section_summaries) == 2
        assert engine.composed_with is not None

    def test_every_summary_failing_is_reported_as_actionable(self) -> None:
        ids = seed_articles(2)
        engine = StubEngine(
            summary_failures={"https://dell.com/blog/post-0", "https://dell.com/blog/post-1"}
        )
        service = GenerationService(engine=engine)

        with pytest.raises(AIError) as exc:
            service.generate(GenerationRequest(article_ids=ids))

        assert "fewer articles" in exc.value.user_message

    def test_progress_is_reported_for_each_stage(self) -> None:
        """The Generate page shows these; silence reads as a hang."""
        ids = seed_articles(2)
        seen: list[str] = []

        GenerationService(engine=StubEngine()).generate(
            GenerationRequest(article_ids=ids), on_progress=seen.append
        )

        assert any("Summarised" in message for message in seen)
        assert any("Writing" in message for message in seen)

    def test_a_campaign_name_is_used_when_given(self) -> None:
        ids = seed_articles(1)

        draft = GenerationService(engine=StubEngine()).generate(
            GenerationRequest(article_ids=ids, campaign_name="August OEM round-up")
        )

        with unit_of_work() as session:
            assert CampaignRepository(session).get(draft.campaign_id).name == "August OEM round-up"


# ─────────────────────────────────────────────────────────────────────────────
#  Provenance — D-6
# ─────────────────────────────────────────────────────────────────────────────
class TestProvenance:
    def test_the_resolved_prompt_version_is_recorded_not_the_alias(self) -> None:
        """Storing "latest" would make the record worthless the moment a new
        prompt version ships — the campaign would claim to be reproducible and
        would not be."""
        ids = seed_articles(1)

        draft = GenerationService(engine=StubEngine(prompt_version="1.1.0")).generate(
            GenerationRequest(article_ids=ids)
        )

        assert draft.prompt_version == "1.1.0"

    def test_the_model_and_provider_are_stored_on_the_campaign(self) -> None:
        ids = seed_articles(1)

        draft = GenerationService(engine=StubEngine()).generate(GenerationRequest(article_ids=ids))

        with unit_of_work() as session:
            campaign = CampaignRepository(session).get(draft.campaign_id)
            assert campaign.model_name == "stub-model-v1"
            assert campaign.prompt_version == "1.1.0"

    def test_the_generation_options_are_recorded(self) -> None:
        """A campaign that cannot say which tone produced it cannot be repeated."""
        ids = seed_articles(1)
        options = GenerationOptions(tone=Tone.TECHNICAL)

        draft = GenerationService(engine=StubEngine()).generate(
            GenerationRequest(article_ids=ids, options=options)
        )

        assert draft.options.tone is Tone.TECHNICAL

    def test_source_urls_survive_onto_the_draft(self) -> None:
        """Attribution is a copyright control (C-6), not a nicety."""
        ids = seed_articles(2)

        draft = GenerationService(engine=StubEngine()).generate(GenerationRequest(article_ids=ids))

        assert len(draft.source_urls) == 2
        assert all(url.startswith("https://dell.com") for url in draft.source_urls)


# ─────────────────────────────────────────────────────────────────────────────
#  Single-field operations
# ─────────────────────────────────────────────────────────────────────────────
class TestRegenerateField:
    def _campaign(self) -> int:
        ids = seed_articles(1)
        return (
            GenerationService(engine=StubEngine())
            .generate(GenerationRequest(article_ids=ids))
            .campaign_id
        )

    def test_the_new_value_is_persisted(self) -> None:
        campaign_id = self._campaign()
        service = GenerationService(engine=StubEngine())

        value = service.regenerate_field(campaign_id, EditableField.CTA)

        assert value == "Act now, view the specs"
        with unit_of_work() as session:
            assert CampaignRepository(session).get(campaign_id).cta == value

    def test_the_regeneration_counter_is_incremented(self) -> None:
        """It is a quality signal in PRD §7.1, and only means something if it is
        counted every time rather than when someone remembers to."""
        campaign_id = self._campaign()
        service = GenerationService(engine=StubEngine())

        service.regenerate_field(campaign_id, EditableField.CTA)
        service.regenerate_field(campaign_id, EditableField.SUBJECT)

        with unit_of_work() as session:
            assert CampaignRepository(session).get(campaign_id).regeneration_count == 2

    def test_the_instruction_reaches_the_engine(self) -> None:
        campaign_id = self._campaign()
        engine = StubEngine()

        GenerationService(engine=engine).regenerate_field(
            campaign_id, EditableField.CTA, "make it more urgent"
        )

        assert engine.regenerate_calls == [(EditableField.CTA, "make it more urgent")]

    def test_a_missing_campaign_is_refused(self) -> None:
        service = GenerationService(engine=StubEngine())

        with pytest.raises(ValidationError) as exc:
            service.regenerate_field(9999, EditableField.CTA)

        assert "no longer exists" in exc.value.user_message


class TestSubjectVariants:
    def test_it_returns_the_requested_number(self) -> None:
        ids = seed_articles(1)
        service = GenerationService(engine=StubEngine())
        campaign_id = service.generate(GenerationRequest(article_ids=ids)).campaign_id

        assert len(service.generate_subject_variants(campaign_id, count=3)) == 3

    def test_a_missing_campaign_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            GenerationService(engine=StubEngine()).generate_subject_variants(9999)
