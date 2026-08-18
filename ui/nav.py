"""The page list, and the only supported way to navigate between pages.

``st.switch_page`` does **not** accept the ``url_path`` given to ``st.Page``.
It takes either a path to a file on disk or the ``st.Page`` object itself, and
because every page here is a *function* rather than a file, only the object form
can work. Passing ``"preview"`` raised::

    StreamlitAPIException: Could not find page: preview. Must be the file path
    relative to the main script ... Only the main app file and files in the
    pages/ directory are supported.

The message points at a file layout this app deliberately does not use, which
makes it easy to misread as "add a pages/ directory". The real fix is to hold on
to the objects, so this module builds them once per run and hands them out by
key. A caller names a destination; it cannot name a string that only looks like
one.

Kept out of ``app.py`` so that a page can navigate without importing the app —
``app.py`` executes ``main()`` at module scope, so importing it from a page
would re-enter the whole application.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:  # pragma: no cover - typing only
    from streamlit.navigation.page import StreamlitPage

#: key → (module attribute, title, icon, url_path). The key is what callers use;
#: it happens to match ``url_path`` but the two are not interchangeable, which is
#: the whole point of this module.
PAGE_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("dashboard", "dashboard", "Dashboard", ":material/dashboard:"),
    ("generate", "generate", "Generate Newsletter", ":material/auto_awesome:"),
    ("preview", "preview", "Campaign Preview", ":material/preview:"),
    ("approvals", "approvals", "Approvals", ":material/how_to_reg:"),
    ("recipients", "recipients", "Recipients", ":material/group:"),
    ("history", "history", "Campaign History", ":material/history:"),
    ("settings", "settings_page", "Settings", ":material/settings:"),
    ("logs", "logs", "Logs", ":material/receipt_long:"),
)

#: Rebuilt on every script run by :func:`build`. Streamlit expects fresh
#: ``st.Page`` objects per run, so this is a per-run lookup table and not a
#: cache that outlives one.
_pages: dict[str, StreamlitPage] = {}


def build() -> list[StreamlitPage]:
    """Create the pages and return them in sidebar order.

    Called once per run from ``app.py``. Imports the page modules here rather
    than at module scope so that a page module can import *this* one for
    :func:`goto` without a circular import.
    """
    from importlib import import_module

    _pages.clear()
    built: list[StreamlitPage] = []
    for index, (key, module_attr, title, icon) in enumerate(PAGE_SPECS):
        # import_module rather than getattr on the package: ui/pages/__init__.py
        # deliberately re-exports nothing, so the submodule has to be imported
        # by name or the attribute simply is not there.
        render = import_module(f"ui.pages.{module_attr}").render
        # The first entry is the landing page and takes the root path. Every
        # other page needs an explicit `url_path`: Streamlit otherwise infers it
        # from the callable's name, and all eight expose `render()` — which
        # collides into a single "/render" and raises StreamlitAPIException.
        page = (
            st.Page(render, title=title, icon=icon, default=True)
            if index == 0
            else st.Page(render, title=title, icon=icon, url_path=key)
        )
        _pages[key] = page
        built.append(page)
    return built


def goto(key: str) -> None:
    """Navigate to the page registered under ``key``.

    Raises ``KeyError`` for an unknown key rather than falling back to a string,
    because a silent no-op is exactly the failure this module exists to remove:
    the Open button appeared to do nothing for a while before the underlying
    exception was noticed.
    """
    page = _pages.get(key)
    if page is None:  # pragma: no cover - only reachable if build() never ran
        msg = f"unknown page {key!r}; known pages: {sorted(_pages) or 'none built yet'}"
        raise KeyError(msg)
    st.switch_page(page)
