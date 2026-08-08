"""Tests for structured logging.

Three of these are **regression tests for bugs found during M1.1 verification**.
Each is marked ``REGRESSION`` with the symptom, because a fix without a test is
just a fix that the next person will undo.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pytest

from config.logging_config import (
    _console_colors_supported,
    bind_correlation_id,
    clear_context,
    configure_logging,
    get_logger,
    mask_email,
    new_correlation_id,
)


def _read_records(path: Path) -> list[dict]:
    logging.getLogger().handlers[-1].flush()
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ─────────────────────────────────────────────────────────────────────────────
#  REGRESSION: colorama
# ─────────────────────────────────────────────────────────────────────────────
class TestConsoleColourSupport:
    """REGRESSION — structlog raised ``SystemError: ConsoleRenderer with
    colors=True on Windows requires the colorama package installed``, killing the
    app at startup on the one platform we deploy to."""

    def test_windows_without_colorama_disables_colour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        # A None entry makes `import colorama` raise ImportError.
        monkeypatch.setitem(sys.modules, "colorama", None)

        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[method-assign]

        assert _console_colors_supported(stream) is False

    def test_windows_with_colorama_allows_colour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "colorama", object())

        stream = io.StringIO()
        stream.isatty = lambda: True  # type: ignore[method-assign]

        assert _console_colors_supported(stream) is True

    def test_non_tty_never_uses_colour(self) -> None:
        """Redirected output must not be polluted with ANSI escape codes."""
        assert _console_colors_supported(io.StringIO()) is False

    def test_none_stream_is_safe(self) -> None:
        """``sys.stdout`` is ``None`` under pythonw.exe — must not raise."""
        assert _console_colors_supported(None) is False

    def test_stream_without_isatty_is_safe(self) -> None:
        """Streamlit and some process supervisors replace sys.stdout with a
        wrapper that does not implement the full TextIO interface."""

        class Wrapper:
            def write(self, _text: str) -> int:
                return 0

        assert _console_colors_supported(Wrapper()) is False  # type: ignore[arg-type]

    def test_closed_stream_is_safe(self) -> None:
        """A closed file raises ValueError from isatty(). Colour is cosmetic and
        must never be the reason the application fails to start."""
        stream = io.StringIO()
        stream.close()

        assert _console_colors_supported(stream) is False

    def test_configure_logging_does_not_raise_on_windows(
        self, monkeypatch: pytest.MonkeyPatch, log_file: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setitem(sys.modules, "colorama", None)
        configure_logging(level="INFO")  # the original crash


# ─────────────────────────────────────────────────────────────────────────────
#  REGRESSION: Windows encoding
# ─────────────────────────────────────────────────────────────────────────────
class TestUnicodeHandling:
    """REGRESSION — two separate defects with the same symptom (mangled text):

    1. Windows consoles default to cp1252, so an em dash printed as ``\\ufffd``.
    2. ``JSONRenderer`` defaults to ``ensure_ascii=True``, so the log *file*
       stored ``Dell\\u2019s`` instead of ``Dell's``.

    Scraped OEM headlines are full of smart quotes and em dashes, so this was
    not an edge case — it was every other article.
    """

    SMART = "Dell’s PowerEdge — what’s changed · café"

    def test_non_ascii_survives_file_round_trip(self, log_file: Path) -> None:
        configure_logging(level="INFO")
        get_logger("t").info("article.extracted", title=self.SMART)

        records = _read_records(log_file)
        assert records[-1]["title"] == self.SMART

    def test_log_file_stores_readable_utf8_not_escapes(self, log_file: Path) -> None:
        """The Logs page search and `grep` both break on ``\\u2019`` soup."""
        configure_logging(level="INFO")
        get_logger("t").info("article.extracted", title=self.SMART)

        raw = log_file.read_text(encoding="utf-8")
        assert self.SMART in raw
        assert "\\u2019" not in raw


# ─────────────────────────────────────────────────────────────────────────────
#  Security: redaction (NFR-S5)
# ─────────────────────────────────────────────────────────────────────────────
class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "apikey",
            "API_KEY",
            "llm_api_key",
            "token",
            "access_token",
            "password",
            "passwd",
            "secret",
            "client_secret",
            "authorization",
            "bearer",
            "credential",
        ],
    )
    def test_secret_shaped_keys_are_redacted(self, log_file: Path, key: str) -> None:
        configure_logging(level="INFO")
        get_logger("t").warning("llm.failed", **{key: "sk-live-DO-NOT-LEAK-12345"})

        assert "sk-live-DO-NOT-LEAK-12345" not in log_file.read_text(encoding="utf-8")

    def test_ordinary_fields_are_not_redacted(self, log_file: Path) -> None:
        """Over-redaction would make the logs useless, which is its own failure."""
        configure_logging(level="INFO")
        get_logger("t").info("article.extracted", url="https://dell.com/blog", word_count=1240)

        record = _read_records(log_file)[-1]
        assert record["url"] == "https://dell.com/blog"
        assert record["word_count"] == 1240


class TestMaskEmail:
    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("priya.sharma@vays.com", "p***a@vays.com"),
            ("ab@vays.com", "a***@vays.com"),
            ("a@vays.com", "a***@vays.com"),
        ],
    )
    def test_masks_local_part(self, address: str, expected: str) -> None:
        assert mask_email(address) == expected

    def test_malformed_address_is_fully_redacted(self) -> None:
        """Fail closed: if it cannot be parsed, do not print it."""
        assert "@" not in mask_email("not-an-email")

    def test_domain_is_preserved_for_diagnostics(self) -> None:
        assert mask_email("priya@vays.com").endswith("@vays.com")


# ─────────────────────────────────────────────────────────────────────────────
#  Correlation IDs (TRD §8.2)
# ─────────────────────────────────────────────────────────────────────────────
class TestCorrelationIds:
    def test_every_record_in_a_bound_block_shares_the_id(self, log_file: Path) -> None:
        configure_logging(level="INFO")
        cid = bind_correlation_id()

        log = get_logger("t")
        log.info("step.one")
        log.info("step.two")
        log.warning("step.three")

        records = _read_records(log_file)
        assert {r["correlation_id"] for r in records} == {cid}

    def test_clear_context_stops_propagation(self, log_file: Path) -> None:
        configure_logging(level="INFO")
        bind_correlation_id()
        clear_context()
        get_logger("t").info("unrelated.event")

        assert "correlation_id" not in _read_records(log_file)[-1]

    def test_ids_are_short_and_unique(self) -> None:
        ids = {new_correlation_id() for _ in range(500)}
        assert len(ids) == 500, "collision in correlation ids"
        assert all(len(i) == 8 for i in ids), "ids must stay short enough to quote by phone"


# ─────────────────────────────────────────────────────────────────────────────
#  REGRESSION: Streamlit reruns
# ─────────────────────────────────────────────────────────────────────────────
class TestIdempotency:
    """Streamlit re-executes the script on every interaction. Without the guard,
    each rerun would add another handler pair and log lines would multiply until
    the file rotated."""

    def test_repeated_configuration_adds_no_duplicate_handlers(self, log_file: Path) -> None:
        configure_logging(level="INFO")
        first = len(logging.getLogger().handlers)

        for _ in range(10):
            configure_logging(level="INFO")

        assert len(logging.getLogger().handlers) == first

    def test_a_single_event_is_written_once(self, log_file: Path) -> None:
        for _ in range(5):
            configure_logging(level="INFO")
        get_logger("t").info("only.once")

        assert len(_read_records(log_file)) == 1
