"""The page a *recipient* lands on: Like, or Unsubscribe.

The only screen in this application reached by someone who is not signed in, and
the only one served before the auth gate. That shapes everything here — no
navigation, no sidebar, nothing about campaigns or internal state, and no
response that reveals whether an address is on the list.

**Nothing happens until a button is pressed**, and that is a correctness
requirement rather than politeness. Gmail and Outlook prefetch links in messages
with security scanners; a page that acted on load would be fired by a robot,
silently unsubscribing people and recording likes nobody clicked. The scanner
fetches this page and reads it; only a human presses the button.

Which action this is comes from inside the signed token, not from the URL, so a
Like link cannot be edited into an Unsubscribe one.
"""

from __future__ import annotations

import streamlit as st

from core.enums import EmailAction
from services.engagement_service import TOKEN_PARAM, EngagementService

_DONE_KEY = "engagement_done"


def is_recipient_link() -> bool:
    """Whether this request is a recipient arriving from an email link.

    Called by ``app.py`` before the auth gate, so it must be cheap and must not
    touch the database — an unauthenticated request should not be able to make
    the app do work by guessing a parameter.
    """
    return bool(st.query_params.get(TOKEN_PARAM))


def render() -> None:
    """Serve the recipient page for whatever action the token names."""
    token = st.query_params.get(TOKEN_PARAM)
    if not token:
        st.error("This link is not valid. Please use the link from the email.")
        return

    service = EngagementService()

    # Shown before anything is known about the token, so a failed link and a
    # valid one look the same until the moment they must not.
    check = service.inspect(token)
    if not check.valid or check.action is None:
        st.title("Link not valid")
        st.error(check.reason or "This link is not valid.")
        st.caption("If you are trying to unsubscribe, reply to the email and we will remove you.")
        return

    unsubscribing = check.action is EmailAction.UNSUBSCRIBED
    st.title("Unsubscribe" if unsubscribing else "Thanks for the feedback")

    done_key = f"{_DONE_KEY}:{check.action}"
    if st.session_state.get(done_key):
        # Already handled in this browser session. Show the outcome rather than
        # the button, so a refresh does not present it as undone.
        st.success(st.session_state[done_key])
        _footer(unsubscribing=unsubscribing)
        return

    if check.already_done:
        st.info(
            "You are already unsubscribed. No further newsletters will be sent."
            if unsubscribing
            else "You have already liked this newsletter. Thanks again."
        )
        _footer(unsubscribing=unsubscribing)
        return

    _confirm(service, token, check.email, unsubscribing=unsubscribing, done_key=done_key)


def _confirm(
    service: EngagementService,
    token: str,
    email: str,
    *,
    unsubscribing: bool,
    done_key: str,
) -> None:
    """The confirmation step. The button is the only thing that writes."""
    if unsubscribing:
        st.write(f"Please confirm you want to stop receiving newsletters at **{email}**.")
        label = "Yes, unsubscribe me"
    else:
        st.write("Glad this was useful — confirm below and we will note it.")
        label = "Confirm"

    if st.button(label, type="primary"):
        result = service.apply(token)
        if not result.ok:
            st.error(result.message)
            return
        st.session_state[done_key] = result.message
        st.rerun()

    if unsubscribing:
        st.caption("If you clicked by mistake, just close this page — nothing has changed.")


def _footer(*, unsubscribing: bool) -> None:
    if unsubscribing:
        st.caption("Changed your mind? Reply to any earlier newsletter and we will add you back.")
