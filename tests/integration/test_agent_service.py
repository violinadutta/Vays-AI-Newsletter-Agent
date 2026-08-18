"""The agent pipeline: discover → extract → generate → recipients → approval.

The test that matters most is ``test_the_agent_never_sends_anything``. Everything
else here is about failing safely when nobody is watching.

Real services are replaced by stubs so each failure mode can be provoked
directly; the integration between them is what is under test, not extraction or
the LLM, both of which have their own suites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.enums import CampaignStatus, PostState
from core.exceptions import AIError, DiscoveryError
from core.models import (
    Article,
    Category,
    DiscoveredPost,
    IngestionResult,
    NewsletterContent,
    NewsletterDraft,
    Tone,
)
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.discovered_repo import DiscoveredPostRepository
from services.agent_service import AgentService
from services.recipient_source import RecipientSource

pytestmark = pytest.mark.integration

CSV = b"email,name,company\npriya@example.com,Priya,Acme\nrahul@example.com,Rahul,Beta\n"


# ─────────────────────────────────────────────────────────────────────────────
#  Doubles
# ─────────────────────────────────────────────────────────────────────────────
def a_post(external_id: str = "12694", url: str | None = None) -> DiscoveredPost:
    return DiscoveredPost(
        url=url or f"https://vaysinfotech.com/post-{external_id}/",
        title=f"A post about industrial security ({external_id})",
        external_id=external_id,
        source="wordpress-api",
    )


class StubIngestion:
    """Stands in for IngestionService. Fails on demand, per URL."""

    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        self.fail_urls = fail_urls or set()
        self.seen: list[str] = []

    def ingest_urls(self, urls: list[str], **_kwargs: object) -> IngestionResult:
        self.seen.extend(urls)
        url = urls[0]
        if url in self.fail_urls:
            return IngestionResult(articles=[], failures={url: "the site refused the request"})

        with unit_of_work() as session:
            from core.models import CleanedArticle
            from modules.repository.article_repo import ArticleRepository

            row = ArticleRepository(session).create(
                CleanedArticle(
                    url=url,
                    title="Total Cost of Ownership of a Rugged Firewall",
                    cleaned_text="A rugged firewall costs more than its price tag. " * 30,
                    word_count=270,
                    token_estimate=360,
                    extractor="trafilatura",
                    language="en",
                ),
                raw_text="raw",
            )
            article = Article(
                id=int(row.id),
                url=url,
                title=row.title,
                raw_text=row.raw_text,
                cleaned_text=row.cleaned_text,
                word_count=row.word_count,
                token_estimate=row.token_estimate,
                extractor=row.extractor_used,
            )
        return IngestionResult(articles=[article])


class StubGeneration:
    """Stands in for GenerationService, creating a real campaign row."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, request: object, **_kwargs: object) -> NewsletterDraft:
        self.calls += 1
        if self.fail:
            raise AIError("the model returned nothing usable")

        content = NewsletterContent(
            title="What a rugged industrial firewall actually costs",
            summary="The sticker price is the smallest part of a rugged firewall's cost.",
            newsletter=(
                "A rugged firewall sits where the plant network meets everything else.\n\n"
                "Power, mounting and commissioning hours outweigh the hardware over five years."
            ),
            subject="What a rugged firewall actually costs",
            preview_text="Total cost of ownership, not sticker price",
            cta="Read the breakdown",
            keywords=["ot", "firewall", "security"],
            category=Category.SECURITY,
            tone=Tone.PROFESSIONAL,
        )
        with unit_of_work() as session:
            campaign = CampaignRepository(session).create(name="Agent draft", content=content)
            campaign_id = int(campaign.id)
        return NewsletterDraft(
            campaign_id=campaign_id,
            content=content,
            options=request.options,  # type: ignore[attr-defined]
            section_summaries=[],
            source_urls=[],
            model="stub",
            provider="stub",
            prompt_version="1.1.0",
            generation_ms=10,
        )


@pytest.fixture(autouse=True)
def _agent_env(db_session, set_env, tmp_path: Path) -> None:  # noqa: ANN001, ARG001
    """An enabled agent with a recipient CSV present."""
    from tests.conftest import MINIMAL_ENV

    folder = tmp_path / "recipients"
    folder.mkdir()
    (folder / "list.csv").write_bytes(CSV)

    set_env(
        **MINIMAL_ENV,
        AGENT_ENABLED="true",
        AGENT_APPROVAL_EMAIL="management@vaysinfotech.com",
        AGENT_RECIPIENTS_DIR=str(folder),
        AGENT_MAX_POSTS_PER_RUN="3",
    )


@pytest.fixture
def agent(monkeypatch) -> AgentService:  # noqa: ANN001
    """An agent wired to stubs, discovering one post."""
    from services import agent_service

    monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [a_post()])
    return AgentService(
        recipients=RecipientSource(), generation=StubGeneration(), ingestion=StubIngestion()
    )


def campaign_status(campaign_id: int) -> CampaignStatus:
    with unit_of_work() as session:
        return CampaignRepository(session).get(campaign_id).status


# ─────────────────────────────────────────────────────────────────────────────
#  The guarantee
# ─────────────────────────────────────────────────────────────────────────────
class TestTheAgentNeverSends:
    def test_a_drafted_campaign_stops_at_awaiting_approval(self, agent: AgentService) -> None:
        """The whole human-in-the-loop design in one assertion."""
        report = agent.run_once()

        assert len(report.drafted) == 1
        assert campaign_status(report.drafted[0]) == CampaignStatus.AWAITING_APPROVAL

    def test_the_agent_never_sends_anything(self, agent: AgentService) -> None:
        """No send record exists after a full run. If this ever fails, the agent
        has mailed customers without a human deciding."""
        from sqlalchemy import func, select

        from modules.repository.orm_models import SendRecordORM

        agent.run_once()

        with unit_of_work() as session:
            sends = session.scalar(select(func.count()).select_from(SendRecordORM))
        assert sends == 0

    def test_a_drafted_campaign_cannot_begin_sending(self, agent: AgentService) -> None:
        """Not merely "the agent does not call send" — the campaign is refused by
        the guarded UPDATE even if something else tries."""
        report = agent.run_once()

        with unit_of_work() as session:
            assert CampaignRepository(session).begin_send(report.drafted[0]) is False


# ─────────────────────────────────────────────────────────────────────────────
#  Refusing to run
# ─────────────────────────────────────────────────────────────────────────────
class TestPreflight:
    def test_a_disabled_agent_does_nothing(self, agent: AgentService, set_env) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache

        set_env(AGENT_ENABLED="false")
        reset_settings_cache()

        report = agent.run_once()

        assert not report.ok
        assert "turned off" in report.skipped_reason
        assert report.drafted == []

    def test_no_approval_address_stops_the_run(self, agent: AgentService, set_env) -> None:  # noqa: ANN001
        """Drafting newsletters nobody is asked to approve is a silent backlog
        that looks exactly like nothing happening."""
        from config.settings import reset_settings_cache

        set_env(AGENT_APPROVAL_EMAIL="")
        reset_settings_cache()

        report = agent.run_once()

        assert not report.ok
        assert "approval address" in report.skipped_reason

    def test_a_missing_recipient_file_stops_before_generation(
        self, agent: AgentService, set_env, tmp_path: Path
    ) -> None:  # noqa: ANN001
        """Checked before the LLM runs: a draft that cannot be sent is expensive
        to produce, and Groq's free tier makes "expensive" literal."""
        from config.settings import reset_settings_cache

        set_env(AGENT_RECIPIENTS_DIR=str(tmp_path / "nothing-here"))
        reset_settings_cache()

        report = agent.run_once()

        assert not report.ok
        assert "Recipients page" in report.skipped_reason
        assert agent._generation.calls == 0  # noqa: SLF001 - the point of the test

    def test_discovery_still_records_posts_when_recipients_are_missing(
        self, agent: AgentService, set_env, tmp_path: Path
    ) -> None:  # noqa: ANN001
        """Discovery is cheap and idempotent — knowing a post exists is worth
        keeping even on a run that cannot proceed."""
        from config.settings import reset_settings_cache

        set_env(AGENT_RECIPIENTS_DIR=str(tmp_path / "nothing-here"))
        reset_settings_cache()

        agent.run_once()

        with unit_of_work() as session:
            assert DiscoveredPostRepository(session).count() == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Failing safely
# ─────────────────────────────────────────────────────────────────────────────
class TestFailureHandling:
    def test_a_discovery_failure_does_not_raise(self, monkeypatch) -> None:  # noqa: ANN001
        """A scheduled job that raises kills the worker. Nobody is watching."""
        from services import agent_service

        def boom(*_a: object, **_k: object) -> list[DiscoveredPost]:
            raise DiscoveryError("the site is down")

        monkeypatch.setattr(agent_service, "discover_posts", boom)
        report = AgentService(generation=StubGeneration(), ingestion=StubIngestion()).run_once()

        assert not report.ok
        assert report.drafted == []

    def test_an_extraction_failure_marks_the_post_for_retry(self, monkeypatch) -> None:  # noqa: ANN001
        from services import agent_service

        post = a_post()
        monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [post])
        agent = AgentService(
            generation=StubGeneration(), ingestion=StubIngestion(fail_urls={post.url})
        )

        report = agent.run_once()

        assert report.drafted == []
        assert len(report.failed) == 1
        with unit_of_work() as session:
            [row] = DiscoveredPostRepository(session).recent()
            assert row.state == PostState.FAILED
            assert row.attempts == 1
            assert "refused" in row.last_error

    def test_an_llm_failure_marks_the_post_for_retry(self, monkeypatch) -> None:  # noqa: ANN001
        from services import agent_service

        monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [a_post()])
        agent = AgentService(generation=StubGeneration(fail=True), ingestion=StubIngestion())

        report = agent.run_once()

        assert report.drafted == []
        with unit_of_work() as session:
            [row] = DiscoveredPostRepository(session).recent()
            assert row.state == PostState.FAILED
            # Extraction succeeded before generation failed, so the article link
            # survives and the retry does not re-extract from scratch.
            assert row.article_id is not None

    def test_one_bad_post_does_not_stop_the_others(self, monkeypatch, set_env) -> None:  # noqa: ANN001
        """The property that keeps an unattended run useful: a single awkward
        article must not throw away the work already paid for.

        Needs headroom: the in-flight cap is 1 in production, which by design
        means one draft per run. What is under test here is batch tolerance, so
        the cap is raised rather than the assertion lowered.
        """
        from config.settings import reset_settings_cache
        from services import agent_service

        set_env(AGENT_MAX_IN_FLIGHT="10", AGENT_MAX_POSTS_PER_RUN="3")
        reset_settings_cache()
        good, bad = a_post("1"), a_post("2")
        monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [good, bad])
        agent = AgentService(
            generation=StubGeneration(), ingestion=StubIngestion(fail_urls={bad.url})
        )

        report = agent.run_once()

        assert len(report.drafted) == 1
        assert len(report.failed) == 1

    def test_an_unexpected_error_is_contained(self, monkeypatch) -> None:  # noqa: ANN001
        """Anything not a domain error must still not end the run."""
        from services import agent_service

        class Exploding(StubGeneration):
            def generate(self, request: object, **kwargs: object) -> NewsletterDraft:
                msg = "something nobody anticipated"
                raise RuntimeError(msg)

        monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [a_post()])

        report = AgentService(generation=Exploding(), ingestion=StubIngestion()).run_once()

        assert not report.drafted
        assert len(report.failed) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Idempotence and limits
# ─────────────────────────────────────────────────────────────────────────────
class TestRepeatRuns:
    def test_the_same_post_is_not_drafted_twice(self, agent: AgentService) -> None:
        """The failure that would mail customers about one article repeatedly."""
        first = agent.run_once()

        second = agent.run_once()

        assert len(first.drafted) == 1
        assert second.new_posts == 0
        assert second.drafted == []

    def test_posts_per_run_is_capped(self, monkeypatch, set_env) -> None:  # noqa: ANN001
        """The Groq token ceiling is the real constraint behind this cap."""
        from config.settings import reset_settings_cache
        from services import agent_service

        # In-flight raised so the per-run cap is the limit actually being tested.
        set_env(AGENT_MAX_POSTS_PER_RUN="2", AGENT_MAX_IN_FLIGHT="10")
        reset_settings_cache()
        monkeypatch.setattr(
            agent_service, "discover_posts", lambda *_a, **_k: [a_post(str(i)) for i in range(5)]
        )

        report = AgentService(generation=StubGeneration(), ingestion=StubIngestion()).run_once()

        assert len(report.drafted) == 2
        assert report.new_posts == 5  # all recorded, only two processed


class TestRecipients:
    def test_recipients_are_attached_to_the_campaign(self, agent: AgentService) -> None:
        report = agent.run_once()

        from services.campaign_service import CampaignService

        assert CampaignService().recipient_count(report.drafted[0]) == 2

    def test_the_recipient_count_is_reported(self, agent: AgentService) -> None:
        """It goes into the approval email, so management knows the blast radius
        before deciding."""
        assert agent.run_once().recipients == 2

    def test_the_newest_csv_wins(self, agent: AgentService, set_env, tmp_path: Path) -> None:  # noqa: ANN001
        """Marketing drops in a fresh export; requiring them to delete the old one
        is a step they will forget, and the failure would be silent."""
        import os
        import time

        from config.settings import reset_settings_cache

        folder = tmp_path / "many"
        folder.mkdir()
        (folder / "old.csv").write_bytes(b"email\nold@example.com\n")
        time.sleep(0.01)
        newer = folder / "new.csv"
        newer.write_bytes(b"email\na@example.com\nb@example.com\nc@example.com\n")
        os.utime(newer, (time.time() + 10, time.time() + 10))

        set_env(AGENT_RECIPIENTS_DIR=str(folder))
        reset_settings_cache()

        assert agent.run_once().recipients == 3


class TestOneNewsletterAtATime:
    """ "One a month" is not a per-run cap.

    Discovery runs every few hours. A cap of one *per run* would still draft one
    per run and queue thirty in a month, all of which then send together on the
    same day — which is exactly what happened on the first live run. The limit
    that makes it monthly is how many may be **in flight** at once.
    """

    def test_a_second_run_drafts_nothing_while_one_awaits_approval(
        self, monkeypatch, set_env
    ) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache
        from services import agent_service

        set_env(AGENT_MAX_IN_FLIGHT="1", AGENT_MAX_POSTS_PER_RUN="1")
        reset_settings_cache()
        monkeypatch.setattr(
            agent_service, "discover_posts", lambda *_a, **_k: [a_post("1"), a_post("2")]
        )
        agent = AgentService(generation=StubGeneration(), ingestion=StubIngestion())

        first = agent.run_once()
        second = agent.run_once()

        assert len(first.drafted) == 1
        assert second.drafted == []
        assert second.holding == 1

    def test_holding_is_not_a_failure(self, monkeypatch, set_env) -> None:  # noqa: ANN001
        """Most runs of the month do nothing. That is the design, not an error,
        and must not show up on the dashboard as one."""
        from config.settings import reset_settings_cache
        from services import agent_service

        set_env(AGENT_MAX_IN_FLIGHT="1")
        reset_settings_cache()
        monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [a_post()])
        agent = AgentService(generation=StubGeneration(), ingestion=StubIngestion())
        agent.run_once()

        report = agent.run_once()

        assert report.ok
        assert report.failed == []

    def test_new_posts_are_still_recorded_while_holding(self, monkeypatch, set_env) -> None:  # noqa: ANN001
        """The user's requirement in one test: it keeps checking the blog and
        saves what it finds for next time, even though it is not drafting."""
        from config.settings import reset_settings_cache
        from services import agent_service

        set_env(AGENT_MAX_IN_FLIGHT="1")
        reset_settings_cache()
        posts = [a_post("1")]
        monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: list(posts))
        agent = AgentService(generation=StubGeneration(), ingestion=StubIngestion())
        agent.run_once()

        posts.append(a_post("2"))  # a new post is published
        report = agent.run_once()

        assert report.new_posts == 1
        assert report.drafted == []
        with unit_of_work() as session:
            assert DiscoveredPostRepository(session).count() == 2

    def test_the_next_one_is_drafted_once_the_previous_has_sent(self, monkeypatch, set_env) -> None:  # noqa: ANN001
        """The queue moves on after a send, so next month gets a newsletter."""
        from config.settings import reset_settings_cache
        from services import agent_service

        set_env(AGENT_MAX_IN_FLIGHT="1")
        reset_settings_cache()
        monkeypatch.setattr(
            agent_service, "discover_posts", lambda *_a, **_k: [a_post("1"), a_post("2")]
        )
        agent = AgentService(generation=StubGeneration(), ingestion=StubIngestion())
        first = agent.run_once()

        # Simulate the first going out.
        with unit_of_work() as session:
            repo = CampaignRepository(session)
            repo.transition_or_raise(first.drafted[0], CampaignStatus.APPROVED)
            repo.transition_or_raise(first.drafted[0], CampaignStatus.SENDING)
            repo.transition_or_raise(first.drafted[0], CampaignStatus.SENT)

        second = agent.run_once()

        assert len(second.drafted) == 1

    def test_an_approved_but_unsent_campaign_still_holds_the_queue(
        self, monkeypatch, set_env
    ) -> None:  # noqa: ANN001
        """Approved is not sent. It is waiting for the third Wednesday, and a
        second newsletter drafted meanwhile would go out on the same day."""
        from config.settings import reset_settings_cache
        from services import agent_service

        set_env(AGENT_MAX_IN_FLIGHT="1")
        reset_settings_cache()
        monkeypatch.setattr(
            agent_service, "discover_posts", lambda *_a, **_k: [a_post("1"), a_post("2")]
        )
        agent = AgentService(generation=StubGeneration(), ingestion=StubIngestion())
        first = agent.run_once()
        with unit_of_work() as session:
            CampaignRepository(session).transition_or_raise(
                first.drafted[0], CampaignStatus.APPROVED
            )

        assert agent.run_once().drafted == []
