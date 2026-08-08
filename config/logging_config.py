"""Structured logging setup.

Three sinks, each with a different job (TRD §8):

===============  ======  =========================================================
Sink             Level   Purpose
===============  ======  =========================================================
Console          INFO    developer feedback while running
``logs/app.jsonl``  DEBUG   full forensic trail, rotating 10 MB × 5
``app_logs`` table  INFO+   powers the in-app Logs page (added in M1.3)
===============  ======  =========================================================

Why JSON rather than pretty text: the Logs page has to filter by level, search by
campaign, and reconstruct a whole operation from a correlation ID. Parsing
formatted strings to do that is a mistake that is expensive to undo later.

**Correlation IDs** are the highest-value feature here. Every user-initiated
operation binds one for its lifetime, so when Priya reports "it failed around
3pm", the reference code shown in the error message retrieves the exact chain of
events. On a system with an external dependency that dies every three hours,
that is the difference between a diagnosis and a guess.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import uuid
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger

from config.constants import (
    LOG_FILE,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    ensure_runtime_dirs,
)

#: Keys whose values are redacted before a record reaches any sink.
#: This is defence in depth, not the primary control — the primary control is
#: simply never passing a secret to a logging call (NFR-S5).
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|apikey|token|password|passwd|secret|authorization|bearer|credential)"
)

_REDACTED = "***REDACTED***"

#: Guards against duplicate handlers. Streamlit re-executes scripts constantly;
#: without this, every rerun would add another handler and log lines would
#: multiply on each interaction.
_configured = False


def redact_secrets(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: replace secret-shaped values with a placeholder."""
    for key in list(event_dict):
        if _SECRET_KEY_PATTERN.search(key):
            event_dict[key] = _REDACTED
    return event_dict


def mask_email(address: str) -> str:
    """Mask an email address for logging (D-20).

    ``priya.sharma@vays.com`` becomes ``p***a@vays.com``.

    Full recipient addresses belong in the database, where they are needed — not
    scattered through log files that get exported, attached to bug reports and
    copied around.
    """
    if "@" not in address:
        return _REDACTED
    local, _, domain = address.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def new_correlation_id() -> str:
    """Return a short, human-quotable id for one user-initiated operation."""
    return uuid.uuid4().hex[:8]


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation id to the current context and return it.

    Every log line emitted afterwards on this thread carries it automatically.
    """
    cid = correlation_id or new_correlation_id()
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def clear_context() -> None:
    """Clear bound context variables. Call at the end of an operation."""
    structlog.contextvars.clear_contextvars()


def _console_colors_supported(stream: TextIO | None) -> bool:
    """Whether the console renderer may safely use ANSI colours.

    structlog raises ``SystemError`` if ``colors=True`` is requested on Windows
    without ``colorama`` installed — which would crash the app at startup on the
    exact platform we deploy to. Colour is cosmetic, so we detect rather than add
    a dependency for it: one fewer package to install on a handover machine.
    """
    if stream is None:
        return False  # pythonw.exe / no console attached
    try:
        if not stream.isatty():
            return False  # piped or redirected — ANSI codes would be noise
    except (AttributeError, ValueError):
        # Streamlit and some supervisors replace sys.stdout with a wrapper that
        # may lack isatty(); a closed stream raises ValueError. Either way,
        # colour is cosmetic and must never be the reason startup fails.
        return False
    if sys.platform != "win32":
        return True
    try:
        import colorama  # noqa: F401  (optional; present only if something else pulled it in)
    except ImportError:
        return False
    return True


_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    redact_secrets,
    structlog.processors.StackInfoRenderer(),
    structlog.processors.UnicodeDecoder(),
]


def configure_logging(level: str = "INFO", *, json_console: bool = False) -> None:
    """Configure structlog and the stdlib logging bridge.

    Idempotent: safe to call on every Streamlit rerun.

    Args:
        level: Console threshold. The file sink always records DEBUG, because the
            forensic trail is worth more than the disk space, and rotation caps
            the cost at 50 MB.
        json_console: Emit JSON to the console instead of the human-readable
            renderer. Useful when the app runs under a process supervisor.
    """
    global _configured
    if _configured:
        return

    ensure_runtime_dirs()

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Windows consoles default to a legacy ANSI codepage (cp1252), which mangles
    # or crashes on the non-ASCII characters that appear routinely in scraped
    # article titles and in our own error messages. Force UTF-8 on the stream.
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

    console_renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_console
        else structlog.dev.ConsoleRenderer(colors=_console_colors_supported(stream))
    )

    console_handler = logging.StreamHandler(stream)
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED_PROCESSORS,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                console_renderer,
            ],
        )
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",  # explicit: Windows would otherwise use the ANSI codepage
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED_PROCESSORS,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                # ensure_ascii=False keeps the log file readable and greppable:
                # scraped OEM headlines are full of smart quotes and em dashes,
                # and `’` soup defeats both the Logs page search and a human
                # reading the file. The handler is opened with encoding="utf-8".
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(logging.DEBUG)

    # Third-party libraries are chatty at DEBUG and drown our own events.
    for noisy in ("httpx", "httpcore", "urllib3", "trafilatura", "charset_normalizer", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def set_log_level(level: str) -> None:
    """Change the console log threshold on a running process.

    ``configure_logging`` is deliberately one-shot, so changing the log level
    from the Settings page needs its own path — otherwise the UI would report a
    successful save and nothing would change, which is worse than refusing the
    edit.

    Only the console threshold moves. The file sink stays at DEBUG: the forensic
    trail in ``logs/app.jsonl`` is the thing you want *after* something went
    wrong, and it is no use if it was quiet at the time.

    Args:
        level: One of DEBUG, INFO, WARNING, ERROR, CRITICAL. Unknown values fall
            back to INFO rather than raising — a log-level change must never be
            the thing that takes the app down.
    """
    resolved = getattr(logging, level.upper(), logging.INFO)
    for handler in logging.getLogger().handlers:
        # RotatingFileHandler subclasses StreamHandler, so the file sink has to
        # be excluded explicitly or it would be silenced along with the console.
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(resolved)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Use the module path as the name, e.g. ``get_logger(__name__)``.
    """
    return structlog.stdlib.get_logger(name)


def reset_logging() -> None:
    """Tear down logging configuration. For tests only."""
    global _configured
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()
    _configured = False
