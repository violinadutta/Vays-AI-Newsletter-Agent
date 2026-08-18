"""Dashboard — answers three questions in under a minute.

*What does this do? What do I do first? Is anything broken right now?*
(PRD §12.3). Everything on this page exists to answer one of those.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st

from core.enums import CampaignStatus
from core.models import CampaignFilter
from ui import components, state, styles


def render() -> None:
    from services.campaign_service import CampaignService

    header, action = st.columns([4, 1])
    with header:
        st.title("Dashboard")
    with action:
        st.markdown("&nbsp;", unsafe_allow_html=True)

    service = CampaignService()
    recent = service.recent(limit=5)

    if not recent:
        _first_run()
        return

    _metrics(service)
    st.divider()

    _agent_panel()
    st.divider()

    left, right = st.columns([3, 2], gap="large")
    with left:
        _recent(recent)
    with right:
        _health()


def _first_run() -> None:
    components.empty_state(
        "No campaigns yet",
        "Your first newsletter takes about fifteen minutes. Paste a few OEM blog "
        "URLs and the AI will draft it for you to edit.",
    )
    st.divider()
    with st.container(border=True):
        st.markdown("**Getting started**")
        st.markdown(
            "1. **Generate Newsletter** — paste OEM blog URLs and pick a tone\n"
            "2. **Campaign Preview** — edit anything the AI wrote, then preview it\n"
            "3. Upload recipients and send\n\n"
            "Nothing is sent without your explicit confirmation."
        )
    _agent_panel()
    _health()


def _metrics(service: object) -> None:
    from modules.repository.database import unit_of_work
    from modules.repository.send_repo import SendRepository

    month_start = datetime.now(UTC) - timedelta(days=30)
    page = service.list_campaigns(CampaignFilter(created_after=month_start, page_size=200))
    campaigns = page.items

    sent_campaigns = [c for c in campaigns if c.status == CampaignStatus.SENT]
    attempted = sum(c.sent_count + c.failed_count for c in campaigns)
    delivered = sum(c.sent_count for c in campaigns)

    with unit_of_work() as session:
        total_sent = SendRepository(session).total_sent()

    columns = st.columns(4)
    with columns[0]:
        components.metric_card("Campaigns (30 days)", f"{len(campaigns):,}")
    with columns[1]:
        components.metric_card("Sent (30 days)", f"{len(sent_campaigns):,}")
    with columns[2]:
        rate = f"{delivered / attempted:.1%}" if attempted else "—"
        components.metric_card("Delivery rate", rate)
    with columns[3]:
        components.metric_card("Emails delivered (all time)", f"{total_sent:,}")


def _recent(recent: list) -> None:
    st.subheader("Recent campaigns")

    for campaign in recent:
        with st.container(border=True):
            # Widened from 1 to 2: at one ninth of the row the button was
            # narrower than the word inside it and rendered one letter per line.
            name_col, status_col, stats_col, open_col = st.columns([4, 2, 2, 2])
            with name_col:
                st.markdown(f"**{campaign.name}**")
                st.markdown(
                    f'<span class="muted">{campaign.created_at:%d %b %Y, %H:%M}</span>',
                    unsafe_allow_html=True,
                )
            with status_col:
                st.markdown(styles.status_chip(str(campaign.status)), unsafe_allow_html=True)
            with stats_col:
                if campaign.success_rate is not None:
                    st.markdown(
                        f'<span class="muted">{campaign.sent_count:,} sent<br>'
                        f"{campaign.success_rate:.0%} delivered</span>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown('<span class="muted">not sent</span>', unsafe_allow_html=True)
            with open_col:
                if st.button("Open", key=f"open_{campaign.id}", width="stretch"):
                    # `st.rerun()` alone re-rendered the Dashboard, so the button
                    # appeared to do nothing: the campaign id it sets is read by
                    # the Preview page, which was never navigated to.
                    state.set_value(state.DRAFT_CAMPAIGN_ID, campaign.id)
                    st.switch_page("preview")


def _health() -> None:
    """The panel that answers "is anything broken right now?".

    Deliberately prominent: the most common real failure is an unreachable AI
    service, and the user needs to see that *before* investing effort in a draft.
    """
    from services.health_service import HealthService

    st.subheader("System health")
    force = st.button("Run health check")

    try:
        health = HealthService().check(force=force)
    except Exception:  # noqa: BLE001 - the panel that reports faults must survive one
        # A probe that raises would otherwise blank the entire Dashboard — the
        # page whose job is to tell you something is wrong becoming the thing
        # that is wrong.
        st.error(
            "The health check itself failed. The Logs page has the detail; "
            "the rest of the app is unaffected."
        )
        return

    with st.container(border=True):
        components.health_row("AI service", health.llm)
        st.divider()
        components.health_row("Email", health.email)
        st.divider()
        components.health_row("Database", health.database)

    if not health.can_generate:
        st.error(
            "Generation is unavailable. Open **Settings → AI Service** and use "
            "Test Connection to find out why."
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Automation
# ─────────────────────────────────────────────────────────────────────────────
#: The words the workflow uses, in the order it uses them. Rendering the raw
#: enum would show SENT before AWAITING_APPROVAL and read as nonsense.
_PIPELINE = (
    ("Awaiting approval", CampaignStatus.AWAITING_APPROVAL),
    ("Approved", CampaignStatus.APPROVED),
    ("Sending", CampaignStatus.SENDING),
    ("Sent", CampaignStatus.SENT),
    ("Rejected", CampaignStatus.REJECTED),
)

_STATE_ICON = {"off": "⏸️", "blocked": "⚠️", "never run": "⚠️", "stalled": "🔴", "running": "🟢"}


def _agent_panel() -> None:
    """Is the automation working, and what is it waiting on?

    Reports the **age** of the last check-in rather than only its timestamp: a
    scheduler that died silently is this system's worst failure mode, and
    "6 hours ago" answers the question that a date does not.
    """
    from services import agent_status
    from services.dispatch_service import DispatchService

    status = agent_status.current()
    state_name, explanation = status.headline

    with st.container(border=True):
        st.markdown(f"{_STATE_ICON.get(state_name, '•')} **Automation — {state_name}**")
        st.caption(explanation)

        if not status.enabled:
            return

        cols = st.columns(4)
        with cols[0]:
            st.markdown("**Last check**")
            st.markdown(_ago(status.last_discovery))
        with cols[1]:
            st.markdown("**Next check**")
            st.markdown(_when(status.next_discovery))
        with cols[2]:
            st.markdown("**Schedule**")
            st.markdown(status.schedule_label)
        with cols[3]:
            st.markdown("**Next send**")
            st.markdown(_next_send(DispatchService))

        _pipeline_counts()

        if status.last_error:
            st.warning(status.last_error, icon="⚠️")


def _pipeline_counts() -> None:
    """Where campaigns are in the automated workflow."""
    from modules.repository.database import unit_of_work
    from modules.repository.discovered_repo import DiscoveredPostRepository
    from services.campaign_service import CampaignService

    counts = CampaignService().status_counts()
    with unit_of_work() as session:
        posts = DiscoveredPostRepository(session).state_counts()

    parts = [f"{sum(posts.values())} posts seen"]
    parts += [f"{counts.get(str(status), 0)} {label.lower()}" for label, status in _PIPELINE]
    st.markdown(f'<span class="muted">{" · ".join(parts)}</span>', unsafe_allow_html=True)


def _next_send(dispatch: object) -> str:
    try:
        due = dispatch.next_due()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a status panel must not break the page
        return "—"
    if due is None:
        return "nothing approved"
    return "due now" if due.is_due else due.due_at.strftime("%d %b %H:%M")


def _ago(moment: datetime | None) -> str:
    """Elapsed time in the units a person would use."""
    if moment is None:
        return "never"
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def _when(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    if moment <= datetime.now(UTC):
        return "due now"
    return moment.astimezone().strftime("%H:%M")
