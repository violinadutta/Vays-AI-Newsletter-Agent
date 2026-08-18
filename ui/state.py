"""Typed access to Streamlit session state.

Every key is declared here once. Nothing else in the UI touches
``st.session_state`` with a raw string, because the classic Streamlit bug is a
typo silently creating a *new* key: the write succeeds, the read returns the
default, and nothing reports an error.

**Never put user-specific data in ``@st.cache_data``.** Session state is
per-user; the cache is shared across every session in the process. Confusing the
two leaks one marketing executive's draft into another's browser — a security
bug, not just a correctness one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import streamlit as st

if TYPE_CHECKING:
    from services.auth_service import AuthenticatedUser

# ── keys ─────────────────────────────────────────────────────────────────────
USER: Final = "auth.user"
LOGIN_ERROR: Final = "auth.login_error"

ARTICLE_IDS: Final = "generate.article_ids"
GENERATION_OPTIONS: Final = "generate.options"

DRAFT_CAMPAIGN_ID: Final = "preview.campaign_id"
SOURCE_URLS: Final = "preview.source_urls"
RECIPIENTS: Final = "preview.recipients"
TEMPLATE_ID: Final = "preview.template_id"
CTA_URL: Final = "preview.cta_url"
CONFIRM_SEND: Final = "preview.confirm_send"
SEND_REPORT: Final = "preview.send_report"
SUBJECT_VARIANTS: Final = "preview.subject_variants"

HISTORY_CAMPAIGN_ID: Final = "history.campaign_id"
HEALTH_CACHE: Final = "ops.health"

#: Cleared on logout. Anything holding campaign content belongs in this list, or
#: the next person to log in on the same browser inherits the previous user's work.
_SESSION_KEYS: Final = (
    USER,
    LOGIN_ERROR,
    ARTICLE_IDS,
    GENERATION_OPTIONS,
    DRAFT_CAMPAIGN_ID,
    SOURCE_URLS,
    RECIPIENTS,
    TEMPLATE_ID,
    CTA_URL,
    CONFIRM_SEND,
    SEND_REPORT,
    SUBJECT_VARIANTS,
    HISTORY_CAMPAIGN_ID,
    HEALTH_CACHE,
)


def get(key: str, default: Any = None) -> Any:
    return st.session_state.get(key, default)


def set_value(key: str, value: Any) -> None:
    st.session_state[key] = value


def clear(key: str) -> None:
    st.session_state.pop(key, None)


# ── authentication ───────────────────────────────────────────────────────────
def current_user() -> AuthenticatedUser | None:
    """The logged-in user, or ``None``."""
    return st.session_state.get(USER)


def set_current_user(user: AuthenticatedUser) -> None:
    st.session_state[USER] = user


def is_authenticated() -> bool:
    return st.session_state.get(USER) is not None


def logout() -> None:
    """Clear every session key.

    Wholesale rather than selective: forgetting one key on a shared machine
    means the next user sees the previous user's draft.
    """
    for key in _SESSION_KEYS:
        st.session_state.pop(key, None)


@st.cache_resource(show_spinner=False)
def health_service() -> object:
    """The shared health checker.

    ``HealthService`` caches results for 30 seconds **on the instance**. A new
    instance per rerun therefore caches nothing, and Streamlit reruns on every
    interaction — so the sidebar alone would probe Groq on each click. One
    shared object makes the cache real.
    """
    from services.health_service import HealthService

    return HealthService()
