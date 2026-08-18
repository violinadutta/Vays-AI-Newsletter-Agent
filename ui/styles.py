"""Global styling — one injected stylesheet for the whole app.

A single block, injected once, rather than per-page CSS: Streamlit re-executes
the script on every interaction, and scattered ``st.markdown`` style blocks
produce inconsistent spacing and duplicated rules that are impossible to reason
about later.

**Colour comes from the LUNA palette** (see ``.streamlit/config.toml``, which
carries the same values for Streamlit's own widgets). Layout, spacing and type
scale are unchanged — this file recolours, it does not restyle.

Status colours are deliberately **not** drawn from the palette. A newsletter
that FAILED and one that SENT must not be two shades of the same blue; the
palette owns the chrome, meaning owns the semantics. Each chip still carries a
text label as well, because status must never rest on colour alone
(``docs/05_UI_SPEC.md`` §10).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from config.constants import ASSETS_DIR

# ── LUNA ─────────────────────────────────────────────────────────────────────
LUNA_LIGHTEST = "#A7EBF2"
LUNA_CYAN = "#54ACBF"
LUNA_BLUE = "#26658C"
LUNA_DARK = "#023859"
LUNA_DARKEST = "#011C40"

#: Interactive elements. Matches ``primaryColor`` in .streamlit/config.toml —
#: the two must agree or buttons and links drift apart.
BRAND_PRIMARY = LUNA_CYAN
BRAND_PRIMARY_DARK = LUNA_BLUE

#: Body text: the lightest palette tone lifted towards white. #A7EBF2 itself is
#: correct for accents and tiring for paragraphs.
TEXT_PRIMARY = "#E6F3F7"
#: Secondary text. Dimmer than body, still ~7:1 on the dark background.
TEXT_MUTED = "#8FBDD1"
BORDER_DEFAULT = "#0C4470"

#: Semantic, not decorative — brightened for legibility on a dark ground rather
#: than reused from the palette.
STATUS_COLORS: dict[str, str] = {
    "DRAFT": "#7B8794",
    "READY": LUNA_CYAN,
    "AWAITING_APPROVAL": "#E0A22C",
    "APPROVED": "#10B981",
    "REJECTED": "#8A94A3",
    "SENDING": "#E0A22C",
    "SENT": "#10B981",
    "PARTIAL_FAILURE": "#E0A22C",
    "FAILED": "#F0616D",
    "ARCHIVED": "#89A0B0",
}

_CSS = f"""
<style>
  :root {{
    --luna-lightest: {LUNA_LIGHTEST};
    --luna-cyan: {LUNA_CYAN};
    --luna-blue: {LUNA_BLUE};
    --luna-dark: {LUNA_DARK};
    --luna-darkest: {LUNA_DARKEST};

    --brand-primary: {BRAND_PRIMARY};
    --text-secondary: {TEXT_MUTED};
    --border-default: {BORDER_DEFAULT};
  }}
  .block-container {{ padding-top: 2.5rem; max-width: 1200px; }}
  h1 {{ font-size: 1.75rem; font-weight: 600; letter-spacing: -0.01em; }}
  h2 {{ font-size: 1.375rem; font-weight: 600; }}
  h3 {{ font-size: 1.0625rem; font-weight: 600; }}

  /* Headings take the lightest tone so they lift off the navy ground. */
  h1, h2, h3 {{ color: {LUNA_LIGHTEST}; }}

  /* Status chips always carry a text label as well as a colour — status must
     never be conveyed by colour alone (accessibility, UI spec §10). Dark text
     on the light chips, because white on amber fails contrast. */
  .status-chip {{
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
    color: {LUNA_DARKEST}; white-space: nowrap;
  }}
  .health-dot {{
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 6px;
  }}
  .muted {{ color: var(--text-secondary); font-size: 0.875rem; }}
  .card {{
    border: 1px solid var(--border-default); border-radius: 10px;
    padding: 1rem 1.25rem; background: {LUNA_DARK};
  }}

  /* Streamlit's bordered containers are used throughout the app as cards. */
  [data-testid="stVerticalBlockBorderWrapper"] {{
    border-color: {BORDER_DEFAULT} !important;
    border-radius: 10px;
  }}

  [data-testid="stSidebarNav"] {{ padding-top: 0.5rem; }}
  [data-testid="stSidebar"] {{ border-right: 1px solid {BORDER_DEFAULT}; }}

  /* Links and the sidebar rules pick up the palette rather than the default
     blue Streamlit ships. */
  a, a:visited {{ color: {LUNA_CYAN}; }}
  a:hover {{ color: {LUNA_LIGHTEST}; }}
  hr, [data-testid="stDivider"] {{ border-color: {BORDER_DEFAULT}; }}

  /* Tab underline: the default red is the last thing that looks off-brand. */
  .stTabs [aria-selected="true"] {{ color: {LUNA_LIGHTEST} !important; }}

  code {{ color: {LUNA_LIGHTEST}; background: rgba(38, 101, 140, 0.35); }}
</style>
"""


def inject() -> None:
    """Inject the global stylesheet. Safe to call on every rerun."""
    st.markdown(_CSS, unsafe_allow_html=True)


def status_chip(status: str) -> str:
    """Return HTML for a status chip carrying both colour and label."""
    color = STATUS_COLORS.get(status, "#7B8794")
    label = status.replace("_", " ")
    return f'<span class="status-chip" style="background:{color}">{label}</span>'


def health_dot(healthy: bool, label: str) -> str:
    """Return HTML for a health indicator with an explicit text state."""
    color = "#10B981" if healthy else "#F0616D"
    state = "online" if healthy else "offline"
    return (
        f'<span class="health-dot" style="background:{color}"></span>'
        f'<span class="muted">{label} &mdash; {state}</span>'
    )


#: Tried in order. The dark-UI variant first: the original wordmark is near-black
#: and disappears against the navy sidebar, which is the same trap the email
#: header hit when the logo sat on the brand-blue band.
LOGO_CANDIDATES = ("logo-dark-ui.png", "image001.png", "logo.png")


def dashboard_logo(assets_dir: Path | None = None) -> Path | None:
    """The logo file to show in the top-left, or ``None`` if none is present.

    Separate from the rendering call so it can be tested without importing
    ``app``, which executes the whole application at module scope.
    """
    folder = assets_dir or ASSETS_DIR
    for name in LOGO_CANDIDATES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None
