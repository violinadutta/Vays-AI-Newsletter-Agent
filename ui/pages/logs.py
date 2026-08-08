"""Logs — how a non-technical user turns "it broke" into a diagnosis.

The correlation-ID filter is the feature that earns this page its place: clicking
one reconstructs every event from a single operation, which is the difference
between "generation failed around 3pm" and knowing exactly which call failed and
why.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import streamlit as st

from config.constants import LOG_RETENTION_DAYS
from ui import components

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
WINDOWS = {"Last hour": 1, "Last 24 hours": 24, "Last 7 days": 168, "All": None}
LEVEL_COLOURS = {"ERROR": "#DC2626", "CRITICAL": "#DC2626", "WARNING": "#D97706"}


def render() -> None:
    from modules.repository.database import unit_of_work
    from modules.repository.log_repo import LogRepository

    st.title("Logs")

    correlation = st.session_state.get("log_correlation")
    if correlation:
        st.info(f"Showing one operation · correlation `{correlation}`")
        if st.button("← Show all logs"):
            st.session_state.pop("log_correlation", None)
            st.rerun()

    level_col, window_col, search_col = st.columns([2, 2, 3])
    with level_col:
        levels = st.multiselect("Level", LEVELS, default=["INFO", "WARNING", "ERROR", "CRITICAL"])
    with window_col:
        window = st.selectbox("Time range", list(WINDOWS), index=1)
    with search_col:
        search = st.text_input("Search", placeholder="event or message…")

    hours = WINDOWS[window]
    since = datetime.now(UTC) - timedelta(hours=hours) if hours else None

    with unit_of_work() as session:
        repo = LogRepository(session)
        entries = repo.search(
            levels=levels or None,
            search=search or None,
            correlation_id=correlation,
            since=since,
            limit=250,
        )
        total = repo.count()

    if not entries:
        components.empty_state(
            "No log entries match these filters",
            "Widen the time range, or include more levels.",
        )
        return

    st.caption(
        f"Showing {len(entries)} of {total:,} entries · kept for {LOG_RETENTION_DAYS} days · "
        "the full trail is in logs/app.jsonl"
    )

    for entry in entries:
        _row(entry)

    st.divider()
    st.download_button(
        "Export these entries (CSV)",
        _to_csv(entries),
        file_name="logs.csv",
        mime="text/csv",
        help="Attach this to a bug report.",
    )


def _row(entry: object) -> None:
    colour = LEVEL_COLOURS.get(entry.level, "#8A94A3")
    with st.container(border=True):
        time_col, level_col, event_col, action_col = st.columns([2, 1, 5, 2])

        with time_col:
            st.markdown(
                f'<span class="muted">{entry.ts:%d %b %H:%M:%S}</span>', unsafe_allow_html=True
            )
        with level_col:
            st.markdown(
                f'<span style="color:{colour};font-weight:600;font-size:12px;">{entry.level}</span>',
                unsafe_allow_html=True,
            )
        with event_col:
            st.markdown(f"`{entry.event}`")
            if entry.message:
                st.markdown(f'<span class="muted">{entry.message}</span>', unsafe_allow_html=True)
        with action_col:
            if entry.correlation_id and st.button(
                entry.correlation_id,
                key=f"corr_{entry.id}",
                help="Show every event from this operation",
            ):
                st.session_state["log_correlation"] = entry.correlation_id
                st.rerun()

        if entry.context or entry.exception:
            with st.expander("Detail"):
                if entry.context:
                    st.json(entry.context)
                if entry.exception:
                    st.code(entry.exception)


def _to_csv(entries: list) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["timestamp", "level", "logger", "event", "message", "correlation_id"])
    for entry in entries:
        writer.writerow(
            [
                entry.ts.isoformat(),
                entry.level,
                entry.logger,
                entry.event,
                entry.message or "",
                entry.correlation_id or "",
            ]
        )
    return buffer.getvalue()
