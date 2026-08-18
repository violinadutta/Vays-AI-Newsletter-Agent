"""The automation agent: discover a post, draft a newsletter, ask a human.

This is an **orchestrator, not an implementation**. Every step delegates to the
service the manual UI already uses — ``IngestionService`` extracts,
``GenerationService`` writes, ``DeliveryService`` validates recipients. The agent
contributes scheduling, persistence of what it has already seen, and the decision
points between stages. Nothing about extraction, generation or sending changes
because it was started by a timer rather than a button.

**It stops at ``AWAITING_APPROVAL``.** No path here sends anything to a customer.
The campaign becomes eligible for delivery only after a human approves it and the
configured send time arrives — enforced in the state machine (an unapproved
campaign is absent from ``SENDABLE_STATES``) rather than by a check in this file.

Failure is expected and never fatal. Nobody is watching when this runs, so a
single bad post must not stop the run, and a failed run must not stop the
scheduler. Every stage records what happened against the post so the next run can
pick up where this one left off, and a post that keeps failing is eventually
abandoned rather than retried forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import bind_correlation_id, get_logger, get_settings
from core.enums import CampaignStatus, PostState
from core.exceptions import InvalidCSVError, NewsletterAppError
from core.models import GenerationOptions, GenerationRequest
from modules.discovery import discover_posts
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.discovered_repo import DiscoveredPostRepository
from services.recipient_source import RecipientList, RecipientSource

log = get_logger(__name__)


@dataclass
class AgentRunReport:
    """What one discovery run did. Returned rather than logged only, so the
    dashboard and the tests can both assert on it."""

    discovered: int = 0
    new_posts: int = 0
    processed: int = 0
    drafted: list[int] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped_reason: str | None = None
    recipients: int = 0
    #: Non-zero when drafting was held back because a newsletter is still in
    #: flight. Not a failure — the normal state for most runs of the month.
    holding: int = 0

    @property
    def ok(self) -> bool:
        return self.skipped_reason is None


class AgentService:
    """Runs one pass of the automated pipeline."""

    def __init__(
        self,
        recipients: RecipientSource | None = None,
        generation: object | None = None,
        ingestion: object | None = None,
    ) -> None:
        # Injected for tests; built lazily in `run_once` otherwise, because
        # constructing GenerationService builds an LLM provider and the agent may
        # be constructed only to report status.
        self._recipients = recipients or RecipientSource()
        self._generation = generation
        self._ingestion = ingestion

    # ── the run ──────────────────────────────────────────────────────────────
    def run_once(self) -> AgentRunReport:
        """Discover, draft, and submit for approval. Never raises.

        Returns a report describing what happened. A refusal to run (agent
        disabled, no approval address, no recipient list) is reported as
        ``skipped_reason`` rather than an exception — the scheduler's job is to
        call this again later, not to handle errors.
        """
        correlation_id = bind_correlation_id()
        report = AgentRunReport()

        blocked = self._preflight()
        if blocked is not None:
            report.skipped_reason = blocked
            log.warning("agent.run_skipped", reason=blocked, correlation_id=correlation_id)
            return report

        # Discovery is cheap and idempotent, so it runs before the recipient
        # check: knowing a post exists is worth recording even on a run that
        # cannot proceed to drafting.
        try:
            report.new_posts = self._discover(report)
        except NewsletterAppError as exc:
            report.skipped_reason = exc.user_message
            log.warning("agent.discovery_failed", reason=exc.message[:300])
            return report

        # Recipients are checked *before* generation rather than after: a draft
        # nobody can be sent is an expensive thing to produce, and Groq's free
        # tier is the constraint that makes "expensive" literal.
        try:
            recipients = self._recipients.load()
            report.recipients = recipients.sendable_count
        except InvalidCSVError as exc:
            report.skipped_reason = exc.user_message
            # error, not exception: a missing recipient file is an expected
            # operational state with a known cause. A stack trace here would be
            # noise on the Logs page for something that needs a file, not a fix.
            log.error(  # noqa: TRY400
                "agent.recipients_unavailable", reason=exc.message[:300]
            )
            return report

        report.processed = self._process_pending(recipients, report)
        log.info(
            "agent.run_complete",
            discovered=report.discovered,
            new=report.new_posts,
            drafted=len(report.drafted),
            failed=len(report.failed),
            correlation_id=correlation_id,
        )
        return report

    # ── preflight ────────────────────────────────────────────────────────────
    @staticmethod
    def _preflight() -> str | None:
        """Reasons not to run at all. ``None`` means proceed."""
        settings = get_settings().agent

        if not settings.enabled:
            return "The automation agent is turned off (Settings → Agent)."

        if not settings.approval_email.strip():
            # Refused rather than defaulted: drafting newsletters nobody is asked
            # to approve produces a silent backlog that looks like nothing is
            # happening at all.
            return (
                "No approval address is configured, so there is nobody to send the "
                "draft to. Set it in Settings → Agent."
            )
        return None

    # ── stages ───────────────────────────────────────────────────────────────
    def _discover(self, report: AgentRunReport) -> int:
        """Find posts and record the ones not seen before."""
        settings = get_settings().agent
        # Ask for more than we will process: the extra entries are what let the
        # duplicate guard notice a post published between runs, without them
        # having to be handled immediately.
        posts = discover_posts(settings.blog_url, limit=max(settings.max_posts_per_run * 3, 10))
        report.discovered = len(posts)

        with unit_of_work() as session:
            created = DiscoveredPostRepository(session).record_new(posts)
            for row in created:
                log.info("agent.post_discovered", url=row.url, title=row.title[:120])
            return len(created)

    def _process_pending(self, recipients: RecipientList, report: AgentRunReport) -> int:
        """Move pending posts through extraction, generation and approval.

        Drafting stops while a newsletter is still in flight. Discovery has
        already recorded whatever is new by this point, so nothing is lost —
        posts simply wait for the next cycle, which is what "one newsletter a
        month" requires once discovery runs more often than that.
        """
        settings = get_settings().agent

        in_flight = self._in_flight()
        if in_flight >= settings.max_in_flight:
            report.holding = in_flight
            log.info(
                "agent.holding",
                in_flight=in_flight,
                limit=settings.max_in_flight,
                note="a newsletter is still awaiting approval or waiting to send",
            )
            return 0

        # Never start more than the remaining headroom, however many posts are
        # queued behind it.
        allowed = min(settings.max_posts_per_run, settings.max_in_flight - in_flight)

        with unit_of_work() as session:
            pending = DiscoveredPostRepository(session).pending(
                limit=allowed, max_attempts=settings.max_attempts
            )
            # Read the identifying fields out now: the ORM rows are bound to this
            # session, and each post below opens its own transaction so that one
            # failure cannot roll back a campaign already committed.
            work = [(row.id, row.url, row.title) for row in pending]

        for post_id, url, title in work:
            try:
                campaign_id = self._draft_one(post_id, url, recipients)
            except NewsletterAppError as exc:
                self._record_failure(post_id, exc.message)
                report.failed.append((url, exc.user_message))
                log.warning("agent.post_failed", url=url, reason=exc.message[:300])
            except Exception as exc:  # noqa: BLE001 - one bad post must not end the run
                self._record_failure(post_id, f"{type(exc).__name__}: {exc}")
                report.failed.append((url, "Unexpected error — see the Logs page."))
                log.exception("agent.post_error", url=url)
            else:
                report.drafted.append(campaign_id)
                log.info("agent.draft_ready", campaign_id=campaign_id, title=title[:120])

        return len(work)

    def _draft_one(self, post_id: int, url: str, recipients: RecipientList) -> int:
        """Extract, generate, attach recipients, and submit for approval.

        Returns the campaign id. Each stage records its outcome against the post
        before the next begins, so a failure halfway leaves an accurate record
        rather than a post that claims to be untouched.
        """
        article_id = self._extract(post_id, url)
        campaign_id = self._generate(post_id, article_id)
        self._attach_recipients(campaign_id, recipients)
        self._submit_for_approval(post_id, campaign_id)
        self._request_approval(campaign_id, recipients.sendable_count)
        return campaign_id

    @staticmethod
    def _request_approval(campaign_id: int, recipient_count: int) -> None:
        """Issue a review link and email it to management.

        A failure here is logged but **not** raised. The campaign is already
        parked at ``AWAITING_APPROVAL`` and appears on the Approvals page, so an
        undelivered email costs a notification, not the draft — and raising would
        mark a post FAILED and re-generate it on the next run, paying the token
        cost twice for a campaign that already exists.
        """
        from services.approval_service import ApprovalNotifier, ApprovalService

        try:
            link = ApprovalService().issue(campaign_id)
            sent = ApprovalNotifier().send(campaign_id, recipient_count, link)
        except Exception:  # noqa: BLE001 - notification must not undo the draft
            log.exception("agent.approval_request_failed", campaign_id=campaign_id)
            return

        if not sent:
            log.warning(
                "agent.approval_email_not_sent",
                campaign_id=campaign_id,
                note="the campaign is still visible on the Approvals page",
            )

    def _extract(self, post_id: int, url: str) -> int:
        from services.ingestion_service import IngestionService

        service = self._ingestion or IngestionService()
        result = service.ingest_urls([url])  # type: ignore[attr-defined]

        if not result.articles:
            reason = next(iter(result.failures.values()), "extraction produced no article")
            raise NewsletterAppError(
                f"extraction failed for {url}: {reason}",
                user_message=f"Couldn't read {url}. {reason}",
            )

        article_id = int(result.articles[0].id)
        with unit_of_work() as session:
            DiscoveredPostRepository(session).mark(
                post_id, PostState.EXTRACTED, article_id=article_id
            )
        return article_id

    def _generate(self, post_id: int, article_id: int) -> int:
        from services.generation_service import GenerationService

        service = self._generation or GenerationService()
        draft = service.generate(  # type: ignore[attr-defined]
            GenerationRequest(article_ids=[article_id], options=GenerationOptions())
        )
        campaign_id = int(draft.campaign_id)

        with unit_of_work() as session:
            DiscoveredPostRepository(session).mark(
                post_id, PostState.GENERATED, campaign_id=campaign_id
            )
        return campaign_id

    @staticmethod
    def _attach_recipients(campaign_id: int, recipients: RecipientList) -> None:
        from services.delivery_service import DeliveryService

        DeliveryService().save_recipients(campaign_id, recipients.validation.valid)

    @staticmethod
    def _submit_for_approval(post_id: int, campaign_id: int) -> None:
        """Park the campaign where only a human can move it forward.

        ``AWAITING_APPROVAL`` is absent from ``SENDABLE_STATES``, so from this
        moment the existing guarded UPDATE refuses to send it. That is the whole
        human-in-the-loop guarantee, and it lives in the state machine rather
        than here.
        """
        with unit_of_work() as session:
            CampaignRepository(session).transition_or_raise(
                campaign_id, CampaignStatus.AWAITING_APPROVAL
            )
            DiscoveredPostRepository(session).mark(
                post_id, PostState.GENERATED, campaign_id=campaign_id
            )
        log.info("agent.awaiting_approval", campaign_id=campaign_id)

    @staticmethod
    def _in_flight() -> int:
        """Campaigns already written and not yet sent.

        Counts ``AWAITING_APPROVAL`` (a human has not decided) and ``APPROVED``
        (decided, waiting for the send time). Anything sent, rejected or archived
        is finished with and does not hold the queue.
        """
        from sqlalchemy import func, select

        from modules.repository.orm_models import CampaignORM

        with unit_of_work() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(CampaignORM)
                    .where(
                        CampaignORM.status.in_(
                            (CampaignStatus.AWAITING_APPROVAL, CampaignStatus.APPROVED)
                        )
                    )
                )
                or 0
            )

    @staticmethod
    def _record_failure(post_id: int, reason: str) -> None:
        """Persist a failure so the next run can retry, and eventually stop.

        Its own transaction: the failure record must survive even though the
        work that produced it was rolled back.
        """
        try:
            with unit_of_work() as session:
                DiscoveredPostRepository(session).mark(post_id, PostState.FAILED, error=reason)
        except Exception:  # noqa: BLE001 - recording a failure must not raise a new one
            log.exception("agent.failure_record_failed", post_id=post_id)
