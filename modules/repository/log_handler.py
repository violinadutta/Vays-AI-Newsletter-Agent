"""Logging handler that mirrors INFO+ records into the ``app_logs`` table.

Lives here rather than in ``config.logging_config`` to avoid an import cycle:
the repository layer imports ``config`` for settings and logging, so config
cannot import the repository at module scope. Attaching this from ``app.py``
after the database is initialised keeps the dependency pointing one way.

**A logging failure must never break the thing being logged.** Every write is
wrapped, and a failure disables the handler rather than raising on every
subsequent log line — an unreachable database would otherwise turn one problem
into a storm.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from modules.repository.database import unit_of_work
from modules.repository.log_repo import LogRepository

#: Fields structlog/stdlib put on the record that are not user context.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "color_message",
        "event",
        "timestamp",
        "level",
        "logger",
        "correlation_id",
        "campaign_id",
    }
)


class DatabaseLogHandler(logging.Handler):
    """Writes records to the ``app_logs`` table so the Logs page can query them."""

    def __init__(self, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._lock = threading.Lock()
        self._disabled = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._disabled:
            return
        try:
            self._write(record)
        except Exception:  # noqa: BLE001 - logging must never raise into the caller
            with self._lock:
                if not self._disabled:
                    self._disabled = True
                    # Printed rather than logged: logging is what just failed.
                    print(  # noqa: T201
                        "[log] database logging disabled after a write failure; "
                        "file logging continues in logs/app.jsonl"
                    )

    def _write(self, record: logging.LogRecord) -> None:
        event, context, correlation_id, campaign_id = _unpack(record)

        with unit_of_work() as session:
            LogRepository(session).write(
                level=record.levelname,
                logger=record.name,
                event=event,
                message=context.pop("message", None)
                if isinstance(context.get("message"), str)
                else None,
                campaign_id=campaign_id,
                correlation_id=correlation_id,
                context=context or None,
                exception=self.format(record) if record.exc_info else None,
            )


def _unpack(record: logging.LogRecord) -> tuple[str, dict[str, Any], str | None, int | None]:
    """Split a record into event name, context, and the two indexed fields.

    structlog routes records through ``ProcessorFormatter``, which leaves the
    whole **event dict** on ``record.msg`` rather than a string. Storing that
    verbatim would put ``{'env': 'local', 'event': 'app.started', ...}`` in the
    ``event`` column — unreadable in the Logs page and useless to search.
    """
    payload = getattr(record, "msg", "")

    if isinstance(payload, dict):
        event = str(payload.get("event", "") or "")
        context = {key: _stringify(value) for key, value in payload.items() if key not in _RESERVED}
        correlation_id = payload.get("correlation_id")
        campaign_id = _as_int(payload.get("campaign_id"))
    else:
        event = str(payload)
        context = {
            key: _stringify(value)
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        correlation_id = getattr(record, "correlation_id", None)
        campaign_id = _as_int(getattr(record, "campaign_id", None))

    return event, context, (str(correlation_id) if correlation_id else None), campaign_id


def _stringify(value: Any) -> Any:
    """Keep JSON-serialisable values; stringify anything else."""
    if isinstance(value, str | int | float | bool | type(None) | list | dict):
        return value
    return str(value)


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def attach_db_log_handler(level: int = logging.INFO) -> DatabaseLogHandler | None:
    """Add the database handler to the root logger, once.

    Returns the handler, or ``None`` if one is already attached.
    """
    root = logging.getLogger()
    if any(isinstance(h, DatabaseLogHandler) for h in root.handlers):
        return None

    handler = DatabaseLogHandler(level)
    root.addHandler(handler)
    return handler
