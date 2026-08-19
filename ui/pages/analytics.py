"""Delivery analytics — the record of every email that was actually sent.

Five columns, as specified: recipient name, email address, the newsletter
heading, delivery status, and the time of delivery. Everything else on the page
exists to make those five findable in a list that grows by one row per recipient
per send.

Read-only by design. There is no retry or resend here even though the data is
right in front of you — those live on Campaign History, where the campaign
context makes the consequences obvious. A reporting screen that can send email
is one people hesitate to open.

Specification: ``docs/05_UI_SPEC.md``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import streamlit as st

from core.enums import SendStatus
from services.analytics_service import AnalyticsService
from ui import components

#: Rows per page. High enough that most sends fit on one page, low enough that
#: the browser is not asked to lay out thousands of rows on every rerun.
PAGE_SIZE = 200

#: (label, days). "All time" is last because the default view is deliberately
#: recent — an operator opening this page is nearly always asking about the send
#: that just went out.
WINDOWS: tuple[tuple[str, int | None], ...] = (
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("All time", None),
)

_PAGE_KEY = "analytics_page"
_FILTER_KEY = "analytics_filter_signature"


def render() -> None:
    """Entry point for the Analytics page."""
    st.title("Delivery Analytics")
    st.caption("Every email this platform has sent, and what happened to it.")

    service = AnalyticsService()

    try:
        campaigns = service.campaigns()
    except Exception as exc:  # noqa: BLE001 - surfaced as a panel, not a traceback
        components.error_panel(exc, title="Could not load delivery history")
        return

    if not campaigns:
        components.empty_state(
            "No emails sent yet",
            "Once a campaign is sent, every recipient appears here with its "
            "delivery status and timestamp.",
        )
        return

    window_label, days = _window_picker()
    _summary(service, days)
    st.divider()

    statuses, campaign_id, search = _filters(campaigns)
    _table(service, statuses, campaign_id, search, days, window_label)


def _window_picker() -> tuple[str, int | None]:
    labels = [label for label, _ in WINDOWS]
    # Index 1 = last 30 days: long enough to include the previous monthly send,
    # which is the comparison an operator actually wants.
    choice = st.radio("Period", labels, index=1, horizontal=True, label_visibility="collapsed")
    return choice, dict(WINDOWS)[choice]


def _summary(service: AnalyticsService, days: int | None) -> None:
    """Headline tiles."""
    try:
        summary = service.summary(days=days)
    except Exception as exc:  # noqa: BLE001
        components.error_panel(exc, title="Could not summarise deliveries")
        return

    resolved = summary.delivered + summary.failed
    tiles = (
        ("Emails sent", f"{summary.total:,}"),
        ("Delivered", f"{summary.delivered:,}"),
        ("Failed", f"{summary.failed:,}"),
        # Blank rather than "0.0%" when nothing has resolved yet: a zero here
        # reads as total failure when it actually means "no data".
        ("Delivery rate", f"{summary.delivery_rate:.1f}%" if resolved else "—"),
    )
    for column, (label, value) in zip(st.columns(4), tiles, strict=True):
        with column:
            components.metric_card(label, value)

    if summary.pending:
        st.caption(f"{summary.pending:,} still queued — not counted in the delivery rate.")


def _filters(campaigns: list[tuple[int, str]]) -> tuple[list[SendStatus] | None, int | None, str]:
    """Status, campaign and text filters, returned ready for the service."""
    status_col, campaign_col, search_col = st.columns([1.1, 1.6, 1.3])

    with status_col:
        chosen = st.multiselect(
            "Status",
            [str(status) for status in SendStatus],
            default=[],
            placeholder="All statuses",
        )

    with campaign_col:
        labels = ["All newsletters"] + [f"#{cid} · {heading}" for cid, heading in campaigns]
        picked = st.selectbox("Newsletter", labels, index=0)
        campaign_id = None if picked == labels[0] else campaigns[labels.index(picked) - 1][0]

    with search_col:
        search = st.text_input("Search", placeholder="name or email address")

    statuses = [SendStatus(value) for value in chosen] if chosen else None

    # Any filter change invalidates the current page number. Without this,
    # narrowing a 5-page result while on page 4 shows an empty table, which
    # reads as "no results" rather than "you are past the end".
    signature = (tuple(chosen), campaign_id, search.strip().lower())
    if st.session_state.get(_FILTER_KEY) != signature:
        st.session_state[_FILTER_KEY] = signature
        st.session_state[_PAGE_KEY] = 0

    return statuses, campaign_id, search.strip()


def _table(
    service: AnalyticsService,
    statuses: list[SendStatus] | None,
    campaign_id: int | None,
    search: str,
    days: int | None,
    window_label: str,
) -> None:
    """The five-column delivery table, paged, with an export."""
    try:
        total = service.count(statuses=statuses, campaign_id=campaign_id, search=search, days=days)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(int(st.session_state.get(_PAGE_KEY, 0)), pages - 1)
        records = service.records(
            statuses=statuses,
            campaign_id=campaign_id,
            search=search,
            days=days,
            limit=PAGE_SIZE,
            offset=page * PAGE_SIZE,
        )
    except Exception as exc:  # noqa: BLE001
        components.error_panel(exc, title="Could not load delivery records")
        return

    if not records:
        components.empty_state(
            "Nothing matches these filters",
            f"No deliveries in {window_label.lower()} match. Widen the period or clear a filter.",
        )
        return

    st.markdown(f"**{total:,} delivery record(s)** · {window_label.lower()}")

    st.dataframe(
        [_row(record) for record in records],
        width="stretch",
        hide_index=True,
        column_config={
            "Recipient": st.column_config.TextColumn(width="medium"),
            "Email address": st.column_config.TextColumn(width="medium"),
            "Newsletter": st.column_config.TextColumn(width="large"),
            "Status": st.column_config.TextColumn(width="small"),
            "Delivered at": st.column_config.TextColumn(width="medium"),
        },
    )

    if any(record.is_estimated_time for record in records):
        st.caption(
            "The tilde marks the time the send was *attempted*. Anything that never left "
            "has no delivery time, so the attempt is shown instead of an empty cell."
        )

    _pagination(page, pages, total)

    # Exports the whole filtered result, not just the rows on screen — someone
    # downloading a report wants the full set.
    everything = service.records(
        statuses=statuses, campaign_id=campaign_id, search=search, days=days, limit=10_000
    )
    st.download_button(
        "Download CSV",
        _csv(everything),
        file_name=f"delivery-report-{datetime.now().strftime('%Y-%m-%d')}.csv",
        mime="text/csv",
        help=f"All {total:,} matching records, not only this page.",
    )


def _row(record: object) -> dict[str, str]:
    """One display row. Shared with the CSV so the two cannot drift apart."""
    return {
        "Recipient": record.recipient_name,  # type: ignore[attr-defined]
        "Email address": record.email,  # type: ignore[attr-defined]
        "Newsletter": record.newsletter,  # type: ignore[attr-defined]
        "Status": record.status,  # type: ignore[attr-defined]
        "Delivered at": _timestamp(record),
    }


def _timestamp(record: object) -> str:
    moment: datetime | None = record.delivered_at  # type: ignore[attr-defined]
    if moment is None:
        return "—"
    stamp = moment.strftime("%d %b %Y, %H:%M")
    return f"~ {stamp}" if record.is_estimated_time else stamp  # type: ignore[attr-defined]


def _pagination(page: int, pages: int, total: int) -> None:
    if pages <= 1:
        return
    previous_col, label_col, next_col = st.columns([1, 2, 1])
    with previous_col:
        if st.button("Previous", disabled=page == 0, width="stretch"):
            st.session_state[_PAGE_KEY] = page - 1
            st.rerun()
    with label_col:
        first = page * PAGE_SIZE + 1
        last = min(first + PAGE_SIZE - 1, total)
        st.markdown(
            f'<p style="text-align:center" class="muted">{first:,}-{last:,} of {total:,}</p>',
            unsafe_allow_html=True,
        )
    with next_col:
        if st.button("Next", disabled=page >= pages - 1, width="stretch"):
            st.session_state[_PAGE_KEY] = page + 1
            st.rerun()


def _csv(records: list[object]) -> str:
    """The same five columns as the table, plus the failure reason.

    The error column is exported but not displayed: it is long, it is empty for
    every successful row, and on screen it would push the five columns that
    matter off the edge. In a spreadsheet it costs nothing and is the first thing
    wanted when investigating a failure.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["Recipient", "Email address", "Newsletter", "Status", "Delivered at", "Error"])
    for record in records:
        writer.writerow([*_row(record).values(), getattr(record, "error", None) or ""])
    return buffer.getvalue()
