"""Campaign History — the institutional archive.

The point of this page is that "what did we send, to whom, and where did it come
from" has an answer. Without it the platform saves time and loses memory, which
was one of the original complaints (PRD §1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st

from core.enums import CampaignStatus
from core.exceptions import NewsletterAppError
from core.models import CampaignFilter
from ui import components, state, styles

DATE_RANGES = {
    "Last 7 days": 7,
    "Last 30 days": 30,
    "Last 90 days": 90,
    "All time": None,
}


def render() -> None:
    from services.campaign_service import CampaignService

    st.title("Campaign History")

    if state.get(state.HISTORY_CAMPAIGN_ID):
        _detail(state.get(state.HISTORY_CAMPAIGN_ID))
        return

    service = CampaignService()
    filters = _filters()
    page = service.list_campaigns(filters)

    if page.total == 0:
        components.empty_state(
            "Nothing matches these filters",
            "Try a wider date range, or clear the status filter.",
        )
        return

    st.caption(
        f"Showing {len(page.items)} of {page.total:,} campaigns · "
        f"page {page.page} of {page.total_pages}"
    )

    for campaign in page.items:
        _row(campaign)

    _pagination(page, filters)


def _filters() -> CampaignFilter:
    search_col, status_col, date_col = st.columns([3, 2, 2])

    with search_col:
        search = st.text_input("Search", placeholder="Campaign name or subject…")
    with status_col:
        statuses = st.multiselect(
            "Status", list(CampaignStatus), format_func=lambda s: s.value.replace("_", " ")
        )
    with date_col:
        window = st.selectbox("Date range", list(DATE_RANGES), index=1)

    days = DATE_RANGES[window]
    return CampaignFilter(
        search=search or None,
        statuses=statuses or None,
        created_after=datetime.now(UTC) - timedelta(days=days) if days else None,
        page=st.session_state.get("history_page", 1),
        page_size=20,
    )


def _row(campaign: object) -> None:
    with st.container(border=True):
        name, status, sent, rate, actions = st.columns([4, 2, 2, 2, 2])

        with name:
            st.markdown(f"**{campaign.name}**")
            st.markdown(
                f'<span class="muted">{campaign.created_at:%d %b %Y, %H:%M}</span>',
                unsafe_allow_html=True,
            )
        with status:
            st.markdown(styles.status_chip(str(campaign.status)), unsafe_allow_html=True)
        with sent:
            st.markdown(
                f'<span class="muted">{campaign.recipient_count:,} recipients</span>',
                unsafe_allow_html=True,
            )
        with rate:
            value = f"{campaign.success_rate:.0%}" if campaign.success_rate is not None else "—"
            st.markdown(f'<span class="muted">{value} delivered</span>', unsafe_allow_html=True)
        with actions:
            view, duplicate = st.columns(2)
            with view:
                if st.button("View", key=f"view_{campaign.id}", width="stretch"):
                    state.set_value(state.HISTORY_CAMPAIGN_ID, campaign.id)
                    st.rerun()
            with duplicate:
                if st.button(
                    "Copy",
                    key=f"dup_{campaign.id}",
                    width="stretch",
                    help="Duplicate as a new draft",
                ):
                    _duplicate(campaign.id)


def _duplicate(campaign_id: int) -> None:
    from services.campaign_service import CampaignService

    try:
        new_id = CampaignService().duplicate(campaign_id)
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return
    state.set_value(state.DRAFT_CAMPAIGN_ID, new_id)
    st.success(f"Copied to draft #{new_id}. Open **Campaign Preview** to edit it.")


def _pagination(page: object, filters: CampaignFilter) -> None:
    if page.total_pages <= 1:
        return

    previous, indicator, following = st.columns([1, 2, 1])
    with previous:
        if st.button("← Previous", disabled=filters.page <= 1, width="stretch"):
            st.session_state["history_page"] = filters.page - 1
            st.rerun()
    with indicator:
        st.markdown(
            f'<div style="text-align:center" class="muted">Page {page.page} of {page.total_pages}</div>',
            unsafe_allow_html=True,
        )
    with following:
        if st.button("Next →", disabled=filters.page >= page.total_pages, width="stretch"):
            st.session_state["history_page"] = filters.page + 1
            st.rerun()


def _detail(campaign_id: int) -> None:
    """Full campaign record: content, provenance, delivery, failures."""
    from modules.repository.campaign_repo import CampaignRepository
    from modules.repository.database import unit_of_work
    from services.campaign_service import CampaignService

    if st.button("← Back to history"):
        state.clear(state.HISTORY_CAMPAIGN_ID)
        st.rerun()

    with unit_of_work() as session:
        row = CampaignRepository(session).get(campaign_id)

    if row is None:
        components.error_panel(
            NewsletterAppError("campaign gone", user_message="That campaign no longer exists.")
        )
        return

    st.subheader(row.name)
    st.markdown(styles.status_chip(str(row.status)), unsafe_allow_html=True)

    content_col, meta_col = st.columns([3, 2], gap="large")

    with content_col:
        st.markdown(f"**Subject:** {row.subject or '—'}")
        st.markdown(f"**Preview text:** {row.preview_text or '—'}")
        if row.rendered_html:
            st.components.v1.html(row.rendered_html, height=560, scrolling=True)
        else:
            st.markdown(row.newsletter or "_No content_")

    with meta_col:
        components.provenance_card(row)

        with st.container(border=True):
            st.markdown("**Delivery**")
            st.markdown(
                f'<span class="muted">Recipients: {row.recipient_count:,}<br>'
                f"Delivered: {row.sent_count:,}<br>"
                f"Failed: {row.failed_count:,}<br>"
                f"Sent: {row.sent_at:%d %b %Y, %H:%M}</span>"
                if row.sent_at
                else f'<span class="muted">Recipients: {row.recipient_count:,}<br>Not yet sent</span>',
                unsafe_allow_html=True,
            )

        if st.button("Duplicate as new draft", width="stretch"):
            _duplicate(campaign_id)

    failures = CampaignService().failures(campaign_id)
    if failures:
        st.divider()
        st.markdown(f"**{len(failures)} failed recipient(s)**")
        st.dataframe(
            [{"Email": email, "Reason": reason} for email, reason in failures],
            width="stretch",
            hide_index=True,
        )
