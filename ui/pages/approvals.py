"""Approvals — where a human decides whether an automated draft goes out.

This page is the human in "human-in-the-loop". Everything upstream is
automation; nothing downstream happens without a decision made here.

**The review link does not approve anything.** Arriving with a token shows the
campaign; approving still requires being signed in with an approver or admin
role. The token is a pointer with an expiry — treating it as authorisation would
mean a link preview fetcher or a corporate mail scanner could dispatch a campaign
to customers, and those follow every link in every message.

Editing before approving reuses the existing Preview page rather than
duplicating an editor here.
"""

from __future__ import annotations

import streamlit as st

from config import get_settings
from core.enums import SENDING_ROLES, CampaignStatus
from core.exceptions import NewsletterAppError
from services.approval_service import TOKEN_PARAM, ApprovalService
from ui import components, state


def render() -> None:
    st.title("Approvals")

    service = ApprovalService()
    user = state.current_user()
    token = st.query_params.get(TOKEN_PARAM)

    focused = _resolve_token(service, token) if token else None
    pending = service.pending_campaigns()

    if not pending:
        components.empty_state(
            "Nothing is waiting for approval",
            "Automatically generated newsletters appear here before they are sent. "
            "When the agent drafts one, you will get an email with a review link.",
        )
        _agent_status_note()
        return

    st.caption(
        f"{len(pending)} campaign{'s' if len(pending) != 1 else ''} waiting on a decision. "
        "Nothing is sent until you approve it."
    )

    if focused is not None and focused in pending:
        # Put the campaign the link pointed at first, so a reviewer arriving
        # from an email does not have to hunt for it.
        pending = [focused, *[c for c in pending if c != focused]]

    for campaign_id in pending:
        _campaign_card(service, campaign_id, user, highlighted=campaign_id == focused)


def _resolve_token(service: ApprovalService, token: str) -> int | None:
    """Validate a review link and report the outcome. Never spends the token."""
    check = service.check(token)
    if check.rejected:
        st.warning(check.reason, icon="⚠️")
        return None

    st.success("Review link verified. The campaign is shown first below.", icon="✅")
    return check.campaign_id


def _campaign_card(
    service: ApprovalService, campaign_id: int, user: object, *, highlighted: bool
) -> None:
    from services.campaign_service import CampaignService

    campaigns = CampaignService()
    try:
        content = campaigns.get_content(campaign_id)
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return

    recipient_count = campaigns.recipient_count(campaign_id)
    source = _source_post(campaign_id)
    can_decide = bool(user and user.role in SENDING_ROLES)

    with st.container(border=True):
        if highlighted:
            st.markdown("**From your review link**")

        st.markdown(f"### {content.subject}")
        st.markdown(f'<span class="muted">{content.preview_text}</span>', unsafe_allow_html=True)

        left, right = st.columns([3, 2])
        with left:
            if source is not None:
                st.markdown(f"**Source:** [{source['title']}]({source['url']})")
            st.markdown(f"**Call to action:** {content.cta}")
            st.markdown(f"**Recipients:** {recipient_count:,}")
        with right:
            settings = get_settings().agent
            st.markdown(f"**Sends:** {settings.describe_schedule()}")
            st.markdown(f"**Status:** {CampaignStatus.AWAITING_APPROVAL}")

        with st.expander("Read the newsletter"):
            st.markdown(f"**{content.title}**")
            st.markdown(content.summary)
            st.divider()
            for paragraph in content.newsletter.split("\n\n"):
                st.markdown(paragraph)

        st.warning(
            "Verify every product name, version number and statistic against the "
            "source before approving. The AI can produce plausible details that "
            "are not in the article.",
            icon="🔍",
        )

        if not can_decide:
            st.info(
                "Sign in with an approver or admin account to decide on this campaign.",
                icon="🔒",
            )
            return

        edit, reject, approve = st.columns([1, 1, 2])
        with edit:
            if st.button("Edit", key=f"edit_{campaign_id}", width="stretch"):
                # Reuses the existing editor rather than duplicating one here.
                state.set_value(state.DRAFT_CAMPAIGN_ID, campaign_id)
                st.switch_page("preview")
        with reject:
            if st.button("Reject", key=f"reject_{campaign_id}", width="stretch"):
                _decide(service, campaign_id, user, approve=False)
        with approve:
            if st.button("Approve", key=f"approve_{campaign_id}", type="primary", width="stretch"):
                _decide(service, campaign_id, user, approve=True)


def _decide(service: ApprovalService, campaign_id: int, user: object, *, approve: bool) -> None:
    action = service.approve if approve else service.reject
    try:
        action(campaign_id, by=user.username, role=user.role)  # type: ignore[union-attr]
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return

    if approve:
        settings = get_settings().agent
        from services.dispatch_service import DispatchService

        due = next(
            (d for d in DispatchService.approved_campaigns() if d.campaign_id == campaign_id),
            None,
        )
        when = due.due_at.strftime("%d %b %Y at %H:%M") if due else settings.describe_schedule()
        st.success(f"Approved. It will be sent on {when}.", icon="✅")
    else:
        st.info("Rejected. This campaign will not be sent.", icon="🚫")

    # The link is spent now, so drop it from the URL — a refresh should not
    # re-present a token that no longer works.
    st.query_params.clear()
    st.rerun()


def _source_post(campaign_id: int) -> dict[str, str] | None:
    """The blog post this campaign came from, for provenance."""
    from sqlalchemy import select

    from modules.repository.database import unit_of_work
    from modules.repository.orm_models import DiscoveredPostORM

    with unit_of_work() as session:
        row = (
            session.execute(
                select(DiscoveredPostORM).where(DiscoveredPostORM.campaign_id == campaign_id)
            )
            .scalars()
            .first()
        )
        return {"title": row.title, "url": row.url} if row else None


def _agent_status_note() -> None:
    settings = get_settings().agent
    if not settings.enabled:
        st.info(
            "The automation agent is currently turned off, so no new drafts are "
            "being produced. Turn it on in Settings → Agent.",
            icon="⏸️",
        )
    elif not settings.approval_email.strip():
        st.warning(
            "No approval address is configured, so the agent will not run. "
            "Set it in Settings → Agent.",
            icon="⚠️",
        )
