"""Whether the agent is actually running, and what it last did.

**A scheduler that dies silently is the worst failure this system has.** Nothing
breaks, no error appears, and the first symptom is somebody noticing weeks later
that no newsletters went out. So each pass writes a heartbeat, and the dashboard
reports the *age* of it rather than merely what it says.

Stored in the existing ``settings`` table rather than a new one: these are a
handful of scalar values, and a table per fact is how a schema sprawls. They are
written under an ``agent.runtime.*`` prefix, which the editable-settings registry
does not know about — so they never appear on the Settings page as though
somebody could type them in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from config import get_logger, get_settings
from modules.repository.database import unit_of_work
from modules.repository.settings_repo import SettingsRepository

log = get_logger(__name__)

LAST_DISCOVERY = "agent.runtime.last_discovery_at"
LAST_DISPATCH = "agent.runtime.last_dispatch_at"
LAST_ERROR = "agent.runtime.last_error"
WORKER_STARTED = "agent.runtime.worker_started_at"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_text(value: object) -> str | None:
    """A stored heartbeat value as text, or ``None`` if it is blank or absent.

    The settings column is JSON, so anything could be in there — including the
    empty string a cleared error leaves behind, which must read as "no error"
    rather than as an error with no message.
    """
    return value if isinstance(value, str) and value.strip() else None


def record(key: str, value: str | None = None) -> None:
    """Write one heartbeat value. Never raises — a failure to record must not
    take down the pass it was recording."""
    try:
        with unit_of_work() as session:
            SettingsRepository(session).set(key, value if value is not None else _now())
    except Exception:  # noqa: BLE001
        log.exception("agent.heartbeat_failed", key=key)


@dataclass(frozen=True)
class AgentStatus:
    """A snapshot of the agent, for the dashboard."""

    enabled: bool
    configured: bool
    last_discovery: datetime | None
    last_dispatch: datetime | None
    worker_started: datetime | None
    last_error: str | None
    interval_hours: int
    send_time: str
    timezone: str
    schedule_label: str

    @property
    def next_discovery(self) -> datetime | None:
        if self.last_discovery is None:
            return None
        return self.last_discovery + timedelta(hours=self.interval_hours)

    @property
    def worker_seen(self) -> bool:
        """Whether a worker has ever run. Distinct from "running now"."""
        return self.last_discovery is not None or self.worker_started is not None

    @property
    def is_stale(self) -> bool:
        """Whether the worker has missed its slot by a wide margin.

        Two intervals plus an hour of slack. One missed interval could be a
        restart or a slow run; two means it is not coming back on its own, which
        is the thing worth reporting rather than a graph nobody reads.
        """
        if not self.enabled or self.last_discovery is None:
            return False
        deadline = self.last_discovery + timedelta(hours=self.interval_hours * 2 + 1)
        return datetime.now(UTC) > deadline

    @property
    def headline(self) -> tuple[str, str]:
        """A (state, explanation) pair the dashboard can render directly."""
        if not self.enabled:
            return "off", "Automation is turned off. Nothing is discovered or sent automatically."
        if not self.configured:
            return "blocked", "No approval address is set, so the agent will not run."
        if not self.worker_seen:
            return "never run", "The agent is on, but the worker process has not run yet."
        if self.is_stale:
            return "stalled", (
                "The worker has not checked in for longer than expected. "
                "It may have stopped — restart it with run_agent.bat."
            )
        return "running", "The agent is on and checking in as expected."


def current() -> AgentStatus:
    """Read the agent's current state. Never raises."""
    settings = get_settings().agent

    values: dict[str, object] = {}
    try:
        with unit_of_work() as session:
            stored = SettingsRepository(session).all()
        values = {k: v for k, v in stored.items() if k.startswith("agent.runtime.")}
    except Exception:  # noqa: BLE001 - a status panel must not break the dashboard
        log.exception("agent.status_read_failed")

    return AgentStatus(
        enabled=settings.enabled,
        configured=bool(settings.approval_email.strip()),
        last_discovery=_parse(values.get(LAST_DISCOVERY)),
        last_dispatch=_parse(values.get(LAST_DISPATCH)),
        worker_started=_parse(values.get(WORKER_STARTED)),
        last_error=_as_text(values.get(LAST_ERROR)),
        interval_hours=settings.discovery_interval_hours,
        send_time=settings.send_time,
        timezone=settings.timezone,
        schedule_label=settings.describe_schedule(),
    )
