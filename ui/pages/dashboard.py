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
            name_col, status_col, stats_col, open_col = st.columns([4, 2, 2, 1])
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
                if st.button("Open", key=f"open_{campaign.id}"):
                    state.set_value(state.DRAFT_CAMPAIGN_ID, campaign.id)
                    st.rerun()


def _health() -> None:
    """The panel that answers "is anything broken right now?".

    Deliberately prominent: the most common real failure is an unreachable AI
    service, and the user needs to see that *before* investing effort in a draft.
    """
    from services.health_service import HealthService

    st.subheader("System health")
    force = st.button("Run health check")
    health = HealthService().check(force=force)

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
