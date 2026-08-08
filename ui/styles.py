"""Global styling — one injected stylesheet for the whole app.

A single block, injected once, rather than per-page CSS: Streamlit re-executes
the script on every interaction, and scattered ``st.markdown`` style blocks
produce inconsistent spacing and duplicated rules that are impossible to reason
about later.

Tokens mirror ``docs/05_UI_SPEC.md`` §1.1.
"""

from __future__ import annotations

import streamlit as st

BRAND_PRIMARY = "#0B5FFF"
BRAND_PRIMARY_DARK = "#0847C4"

STATUS_COLORS: dict[str, str] = {
    "DRAFT": "#6B7280",
    "READY": "#0B5FFF",
    "SENDING": "#D97706",
    "SENT": "#059669",
    "PARTIAL_FAILURE": "#D97706",
    "FAILED": "#DC2626",
    "ARCHIVED": "#8A94A3",
}

_CSS = """
<style>
  :root {
    --brand-primary: #0B5FFF;
    --text-secondary: #5A6472;
    --border-default: #E3E6EB;
  }
  .block-container { padding-top: 2.5rem; max-width: 1200px; }
  h1 { font-size: 1.75rem; font-weight: 600; letter-spacing: -0.01em; }
  h2 { font-size: 1.375rem; font-weight: 600; }
  h3 { font-size: 1.0625rem; font-weight: 600; }

  /* Status chips always carry a text label as well as a colour — status must
     never be conveyed by colour alone (accessibility, UI spec §10). */
  .status-chip {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
    color: #fff; white-space: nowrap;
  }
  .health-dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 6px;
  }
  .muted { color: var(--text-secondary); font-size: 0.875rem; }
  .card {
    border: 1px solid var(--border-default); border-radius: 10px;
    padding: 1rem 1.25rem; background: #fff;
  }
  [data-testid="stSidebarNav"] { padding-top: 0.5rem; }
</style>
"""


def inject() -> None:
    """Inject the global stylesheet. Safe to call on every rerun."""
    st.markdown(_CSS, unsafe_allow_html=True)


def status_chip(status: str) -> str:
    """Return HTML for a status chip carrying both colour and label."""
    color = STATUS_COLORS.get(status, "#6B7280")
    label = status.replace("_", " ")
    return f'<span class="status-chip" style="background:{color}">{label}</span>'


def health_dot(healthy: bool, label: str) -> str:
    """Return HTML for a health indicator with an explicit text state."""
    color = "#059669" if healthy else "#DC2626"
    state = "online" if healthy else "offline"
    return (
        f'<span class="health-dot" style="background:{color}"></span>'
        f'<span class="muted">{label} &mdash; {state}</span>'
    )
