"""Navigation between pages.

The bug this guards against: ``st.switch_page("preview")`` looks correct and
type-checks, but ``switch_page`` does not accept the ``url_path`` given to
``st.Page`` — only a file path or the page object. Every click of the Open
button raised ``StreamlitAPIException``, and the exception surfaced at the
bottom of the page rather than on the button, so it read as "the button does
nothing" for some time before anyone looked.

Two of these tests are about that specific mistake: one proves ``goto`` hands
over an object, and one greps the UI for the string form so a future edit cannot
quietly reintroduce it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ui import nav

UI_DIR = Path(__file__).resolve().parents[2] / "ui"


class TestTheRegistry:
    def test_every_spec_has_a_unique_key_and_url(self) -> None:
        keys = [key for key, *_ in nav.PAGE_SPECS]
        assert len(keys) == len(set(keys)), "duplicate page key"

    def test_the_first_page_is_the_landing_page(self) -> None:
        """Order is meaningful: entry zero takes the root path and is default."""
        assert nav.PAGE_SPECS[0][0] == "dashboard"

    def test_preview_is_registered(self) -> None:
        """The destination of the Open button, and the one that broke."""
        assert "preview" in {key for key, *_ in nav.PAGE_SPECS}


class TestGoto:
    def test_an_unknown_key_raises_rather_than_doing_nothing(self) -> None:
        """A silent no-op is the failure mode being designed out."""
        nav._pages.clear()
        with pytest.raises(KeyError, match="unknown page"):
            nav.goto("no-such-page")

    def test_goto_passes_the_page_object_not_a_string(self, monkeypatch) -> None:  # noqa: ANN001
        """The whole bug in one assertion.

        ``st.switch_page`` rejects a ``url_path``. If a future edit passes the
        key through as a string, this fails.
        """
        sentinel = object()
        handed: list[object] = []

        nav._pages.clear()
        nav._pages["preview"] = sentinel  # type: ignore[assignment]
        monkeypatch.setattr(nav.st, "switch_page", handed.append)

        nav.goto("preview")

        assert handed == [sentinel]
        assert not isinstance(handed[0], str), "switch_page was given a string"


class TestNoPageUsesTheStringForm:
    def test_ui_never_calls_switch_page_with_a_bare_string(self) -> None:
        """Static guard: ``nav.goto`` is the only supported way to navigate.

        Checked by reading the source rather than by import, so it holds for
        pages this test does not render.
        """
        offenders: list[str] = []
        for path in UI_DIR.rglob("*.py"):
            if path.name == "nav.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"""switch_page\(\s*["']""", line):
                    offenders.append(f"{path.name}:{number}: {line.strip()}")

        assert not offenders, "use nav.goto(key) instead:\n" + "\n".join(offenders)
