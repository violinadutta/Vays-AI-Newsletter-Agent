"""Reusable UI widgets.

One module rather than a package of one-function files: each of these is small,
and a component library where every piece is used once is just indirection.
Everything here is used in at least two pages.

The rules they encode (from ``docs/05_UI_SPEC.md``):

* Status is never colour-only — every chip carries its label, so it survives
  colour-blindness and a monochrome print.
* Errors state what happened, why, and what to do — never a stack trace.
* Long operations report a named stage, not an anonymous spinner.
"""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st

from core.exceptions import NewsletterAppError
from core.models import HealthStatus
from ui import styles


def status_chip(status: str) -> None:
    """Coloured status pill with its label."""
    st.markdown(styles.status_chip(status), unsafe_allow_html=True)


def health_row(label: str, status: HealthStatus) -> None:
    """One line of the health panel: dot, label, state, and the detail."""
    st.markdown(styles.health_dot(status.healthy, label), unsafe_allow_html=True)
    detail = status.detail or ("available" if status.healthy else "unavailable")
    if status.latency_ms is not None:
        detail = f"{detail} · {status.latency_ms} ms"
    st.markdown(f'<span class="muted">&nbsp;&nbsp;&nbsp;{detail}</span>', unsafe_allow_html=True)


def metric_card(label: str, value: str, delta: str | None = None) -> None:
    st.metric(label, value, delta)


def error_panel(exc: Exception, *, title: str = "Something went wrong") -> None:
    """Render an error as instructions.

    A ``NewsletterAppError`` already carries a user-facing message; anything else
    is a genuine bug and gets a reference the user can quote, with the detail
    tucked behind an expander rather than shown as a wall of traceback.
    """
    if isinstance(exc, NewsletterAppError):
        st.error(exc.user_message)
        if exc.context:
            with st.expander("Technical detail"):
                st.json(exc.context)
                st.code(exc.message)
    else:
        st.error(f"{title}. Check the Logs page for details.")
        with st.expander("Technical detail"):
            st.code(f"{type(exc).__name__}: {exc}")


def empty_state(
    title: str, body: str, *, cta: str | None = None, on_click: Callable[[], None] | None = None
) -> None:
    """A deliberate empty state.

    A blank page reads as a bug. This reads as a next step.
    """
    st.markdown(
        f'<div class="card" style="text-align:center;padding:2.5rem 1.5rem;">'
        f'<h3 style="margin-top:0;">{title}</h3>'
        f'<p class="muted">{body}</p></div>',
        unsafe_allow_html=True,
    )
    if cta and on_click:
        _, middle, _ = st.columns([1, 1, 1])
        with middle:
            if st.button(cta, type="primary", width="stretch"):
                on_click()


def char_counter(value: str, limit: int, label: str = "") -> None:
    """Live character count that warns before it truncates in an inbox.

    Over the limit is a warning, not a block: an inbox-truncation risk is the
    user's call to take, and blocking the send would be the app overruling a
    human on a judgement call it is not qualified to make.
    """
    count = len(value)
    if count > limit:
        colour, note = "#DC2626", " — will be truncated in most inboxes"
    elif count > limit * 0.9:
        colour, note = "#D97706", ""
    else:
        colour, note = "#8A94A3", ""
    st.markdown(
        f'<span style="color:{colour};font-size:12px;">{label}{count}/{limit}{note}</span>',
        unsafe_allow_html=True,
    )


def confirm_send(recipient_count: int, subject: str, sender: str) -> bool:
    """The send confirmation.

    Deliberate friction: the checkbox costs three seconds, and one careless send
    to a real customer list costs considerably more. Cancel holds the default
    position; the primary action is disabled until the box is ticked.
    """
    st.warning(f"**{recipient_count:,} recipients** will receive this. This cannot be undone.")
    st.markdown(f"**Subject:** {subject}")
    st.markdown(f"**From:** {sender}")

    checked = st.checkbox("I have checked the facts, links and recipient list")
    left, right = st.columns(2)
    with left:
        cancelled = st.button("Cancel", width="stretch")
    with right:
        confirmed = st.button(
            f"Send to {recipient_count:,} recipients",
            type="primary",
            width="stretch",
            disabled=not checked,
        )
    return bool(confirmed and not cancelled)


def article_row(article: object, index: int, on_remove: Callable[[int], None]) -> None:
    """One extracted article, with a preview so the user can verify it.

    Showing the extracted text is the trust mechanism: without it the user is
    asked to believe the right page was read, which is exactly the kind of
    unverifiable claim that makes people distrust AI tooling.
    """
    title = getattr(article, "title", "(untitled)")
    words = getattr(article, "word_count", 0)
    tier = getattr(getattr(article, "extractor", None), "value", "?")
    text = getattr(article, "cleaned_text", "")

    with st.container(border=True):
        head, meta, remove = st.columns([6, 2, 1])
        with head:
            st.markdown(f"**{title}**")
        with meta:
            st.markdown(
                f'<span class="muted">{words:,} words · {tier}</span>', unsafe_allow_html=True
            )
        with remove:
            if st.button("✕", key=f"remove_{index}", help="Remove this article"):
                on_remove(index)
        with st.expander("Preview extracted text"):
            url = getattr(article, "url", None)
            if url:
                st.caption(url)
            st.text(text[:1500] + ("…" if len(text) > 1500 else ""))


def provenance_card(campaign: object) -> None:
    """Which model and prompt produced this. Makes any output reproducible."""
    with st.container(border=True):
        st.markdown("**How this was generated**")
        st.markdown(
            f'<span class="muted">Model: {getattr(campaign, "model_name", None) or "—"}<br>'
            f"Provider: {getattr(campaign, 'provider', None) or '—'}<br>"
            f"Prompt version: {getattr(campaign, 'prompt_version', None) or '—'}<br>"
            f"Regenerations: {getattr(campaign, 'regeneration_count', 0)}</span>",
            unsafe_allow_html=True,
        )


def source_panel(urls: list[str]) -> None:
    """Provenance links plus the fact-check reminder.

    The warning is deliberate friction against the hallucination risk that no
    amount of prompt engineering removes.
    """
    with st.container(border=True):
        st.markdown("**Sources**")
        for index, url in enumerate(urls, start=1):
            st.markdown(f"{index}. [{url}]({url})")
        st.warning(
            "Verify every product name, version, date and figure against the sources "
            "before sending.",
            icon="⚠️",
        )
