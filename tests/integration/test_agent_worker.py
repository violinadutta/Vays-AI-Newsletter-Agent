"""The worker process and the heartbeat that proves it is alive.

**A scheduler that dies silently is this system's worst failure.** Nothing
breaks, no error appears, and the first symptom is somebody noticing weeks later
that no newsletters went out. These tests cover the two defences: every pass
catches its own failures, and every pass leaves a timestamp behind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import agent_worker
from services import agent_status

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _env(db_session, set_env, monkeypatch, tmp_path) -> None:  # noqa: ANN001, ARG001
    """An enabled agent, fully isolated from the network and the real filesystem.

    Both parts are load-bearing. Without the ``discover_posts`` stub this file
    fetches the live blog, extracts real articles and spends Groq tokens on every
    run; without the empty recipients folder it reads the developer's real
    ``data/recipients/``. Either makes the suite depend on the outside world,
    which is how a test passes on one machine and fails in CI.
    """
    from config.settings import reset_settings_cache
    from services import agent_service
    from tests.conftest import MINIMAL_ENV

    monkeypatch.setattr(agent_service, "discover_posts", lambda *_a, **_k: [])

    set_env(
        **MINIMAL_ENV,
        AGENT_ENABLED="true",
        AGENT_APPROVAL_EMAIL="management@vaysinfotech.com",
        AGENT_DISCOVERY_INTERVAL_HOURS="6",
        AGENT_RECIPIENTS_DIR=str(tmp_path / "no-recipients"),
    )
    reset_settings_cache()


# ─────────────────────────────────────────────────────────────────────────────
#  Nothing kills the worker
# ─────────────────────────────────────────────────────────────────────────────
class TestFailuresAreContained:
    def test_a_crashing_discovery_does_not_propagate(self, monkeypatch) -> None:  # noqa: ANN001
        """APScheduler swallows a raised job, leaving the worker looking healthy
        while doing nothing. So the job catches its own failures."""
        from services import agent_service

        def boom(*_a: object, **_k: object) -> None:
            msg = "the database went away"
            raise RuntimeError(msg)

        monkeypatch.setattr(agent_service.AgentService, "run_once", boom)

        agent_worker.run_discovery()  # must not raise

        assert "database went away" in (agent_status.current().last_error or "")

    def test_a_crashing_dispatch_does_not_propagate(self, monkeypatch) -> None:  # noqa: ANN001
        from services import dispatch_service

        def boom(*_a: object, **_k: object) -> None:
            msg = "the mail server refused everything"
            raise RuntimeError(msg)

        monkeypatch.setattr(dispatch_service.DispatchService, "dispatch_due", boom)

        agent_worker.run_dispatch()  # must not raise

        assert "refused everything" in (agent_status.current().last_error or "")

    def test_a_skipped_run_is_recorded_as_the_reason_not_a_crash(self) -> None:
        """No recipients is an operational state, not a failure of the worker."""
        agent_worker.run_discovery()

        status = agent_status.current()
        assert status.last_discovery is not None
        assert "Recipients page" in (status.last_error or "")


# ─────────────────────────────────────────────────────────────────────────────
#  The heartbeat
# ─────────────────────────────────────────────────────────────────────────────
class TestHeartbeat:
    def test_a_discovery_pass_checks_in(self) -> None:
        agent_worker.run_discovery()

        assert agent_status.current().last_discovery is not None

    def test_a_dispatch_pass_checks_in(self) -> None:
        agent_worker.run_dispatch()

        assert agent_status.current().last_dispatch is not None

    def test_a_successful_run_clears_a_stale_error(self, monkeypatch) -> None:  # noqa: ANN001
        """An error the agent has since recovered from would send someone
        chasing a problem that no longer exists."""
        from services import agent_service
        from services.agent_service import AgentRunReport

        agent_status.record(agent_status.LAST_ERROR, "something old")
        monkeypatch.setattr(
            agent_service.AgentService, "run_once", lambda _self: AgentRunReport(new_posts=0)
        )

        agent_worker.run_discovery()

        assert not agent_status.current().last_error

    def test_the_next_check_is_derived_from_the_interval(self) -> None:
        agent_worker.run_discovery()

        status = agent_status.current()
        expected = status.last_discovery + timedelta(hours=6)
        assert status.next_discovery == expected


class TestStatusReporting:
    def test_a_disabled_agent_reports_off(self, set_env) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache

        set_env(AGENT_ENABLED="false")
        reset_settings_cache()

        state, _ = agent_status.current().headline
        assert state == "off"

    def test_a_missing_approval_address_reports_blocked(self, set_env) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache

        set_env(AGENT_APPROVAL_EMAIL="")
        reset_settings_cache()

        state, _ = agent_status.current().headline
        assert state == "blocked"

    def test_an_enabled_agent_that_never_ran_says_so(self) -> None:
        state, explanation = agent_status.current().headline

        assert state == "never run"
        assert "has not run yet" in explanation

    def test_a_recent_check_in_reports_running(self) -> None:
        agent_worker.run_discovery()

        state, _ = agent_status.current().headline
        assert state == "running"

    def test_a_long_silence_reports_stalled(self) -> None:
        """The failure worth reporting: the worker is not coming back on its own."""
        agent_status.record(
            agent_status.LAST_DISCOVERY, (datetime.now(UTC) - timedelta(days=3)).isoformat()
        )

        status = agent_status.current()
        assert status.is_stale
        assert status.headline[0] == "stalled"

    def test_one_missed_interval_is_not_yet_stalled(self) -> None:
        """A restart or a slow run should not raise an alarm."""
        agent_status.record(
            agent_status.LAST_DISCOVERY, (datetime.now(UTC) - timedelta(hours=7)).isoformat()
        )

        assert not agent_status.current().is_stale

    def test_a_disabled_agent_is_never_stale(self) -> None:
        """Off is a decision, not a fault."""
        from config.settings import reset_settings_cache

        agent_status.record(
            agent_status.LAST_DISCOVERY, (datetime.now(UTC) - timedelta(days=30)).isoformat()
        )
        import os

        os.environ["AGENT_ENABLED"] = "false"
        reset_settings_cache()

        assert not agent_status.current().is_stale

    def test_status_survives_an_unreadable_heartbeat(self) -> None:
        """A status panel must never be the thing that breaks the dashboard."""
        agent_status.record(agent_status.LAST_DISCOVERY, "not-a-timestamp")

        assert agent_status.current().last_discovery is None


class TestWorkerConfiguration:
    def test_both_jobs_are_scheduled(self) -> None:
        scheduler = agent_worker.build_scheduler()

        assert {job.id for job in scheduler.get_jobs()} == {"discovery", "dispatch"}

    def test_dispatch_runs_far_more_often_than_discovery(self) -> None:
        """Dispatch is a cheap query that finds nothing almost every time; the
        cost of checking rarely is a campaign going out an hour late."""
        assert agent_worker.DISPATCH_INTERVAL_MINUTES <= 15

    def test_missed_runs_coalesce(self) -> None:
        """A laptop that slept through four intervals should do one catch-up
        run, not four in a row against a rate-limited API."""
        scheduler = agent_worker.build_scheduler()

        assert all(job.coalesce for job in scheduler.get_jobs())

    def test_one_pass_runs_both_jobs(self) -> None:
        assert agent_worker.run_once() == 0
        status = agent_status.current()
        assert status.last_discovery is not None
        assert status.last_dispatch is not None


class TestHeartbeatKeysAreNotSettings:
    """The worker writes state into the settings table. That must not look like
    a configuration problem every time the app starts."""

    @staticmethod
    def _warned_keys() -> list[str]:
        """Keys `apply_saved` complained about.

        ``structlog.testing.capture_logs`` rather than pytest's ``caplog``: the
        app routes structlog through a ProcessorFormatter, so the event name
        lives on the record's *dict* and never reaches ``caplog.text``.
        """
        import structlog

        from services.settings_service import SettingsService

        with structlog.testing.capture_logs() as entries:
            SettingsService().apply_saved()

        return [
            str(e.get("key")) for e in entries if e.get("event") == "settings.unknown_key_ignored"
        ]

    def test_runtime_keys_do_not_warn_as_unknown(self) -> None:
        """A warning on every startup trains the reader to ignore the one that
        matters — a genuinely unknown key, which means a renamed setting."""
        agent_status.record(agent_status.LAST_DISCOVERY)
        agent_status.record(agent_status.LAST_DISPATCH)

        assert self._warned_keys() == []

    def test_a_genuinely_unknown_key_still_warns(self) -> None:
        from modules.repository.database import unit_of_work
        from modules.repository.settings_repo import SettingsRepository

        with unit_of_work() as session:
            SettingsRepository(session).set("agent.renamed_field", "x")

        assert "agent.renamed_field" in self._warned_keys()
