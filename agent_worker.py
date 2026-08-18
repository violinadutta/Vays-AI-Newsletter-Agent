"""The automation worker — a separate process that runs on a timer.

**Why a second process rather than a thread inside Streamlit.** Streamlit is
request-driven: it re-executes the script on every interaction and stops running
when the last browser session closes. A scheduler living inside it would be
duplicated on each rerun and would die the moment somebody shut their tab —
which is precisely when unattended automation is supposed to be working.

So this is its own process, started by ``run_agent.bat`` and left running. It
shares the database, the settings and every service with the dashboard; the only
thing it does not share is a browser.

Two jobs:

* **discovery** — every ``AGENT_DISCOVERY_INTERVAL_HOURS``: find new posts,
  draft newsletters, request approval.
* **dispatch** — every 5 minutes: send any approved campaign whose configured
  send time has arrived. Frequent and cheap, because it does nothing at all
  unless something is both approved and due.

Nothing here raises. A job that throws would be swallowed by APScheduler and the
worker would sit there looking healthy, so every pass catches its own failures
and records them where the dashboard can see them.

Run it with::

    run_agent.bat
    .venv\\Scripts\\python.exe agent_worker.py --once     # a single pass, for testing
"""

from __future__ import annotations

import argparse
import signal
import sys
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler

from config import configure_logging, get_logger, get_settings
from core.exceptions import ConfigurationError

#: Dispatch runs far more often than discovery. It is a cheap query that finds
#: nothing almost every time, and the cost of checking too rarely is a campaign
#: going out an hour late.
DISPATCH_INTERVAL_MINUTES = 5

log = get_logger(__name__)


def run_discovery() -> None:
    """One discovery pass: new posts → drafts → approval requests."""
    from services import agent_status
    from services.agent_service import AgentService

    try:
        report = AgentService().run_once()
    except Exception as exc:  # noqa: BLE001 - a scheduled job must never die
        log.exception("worker.discovery_crashed")
        agent_status.record(agent_status.LAST_ERROR, f"Discovery failed: {exc}"[:500])
        return

    agent_status.record(agent_status.LAST_DISCOVERY)
    if report.skipped_reason:
        agent_status.record(agent_status.LAST_ERROR, report.skipped_reason[:500])
        log.info("worker.discovery_skipped", reason=report.skipped_reason)
        return

    agent_status.record(agent_status.LAST_ERROR, "")
    log.info(
        "worker.discovery_done",
        new=report.new_posts,
        drafted=len(report.drafted),
        failed=len(report.failed),
    )


def run_dispatch() -> None:
    """One dispatch pass: send anything approved whose time has come."""
    from services import agent_status
    from services.dispatch_service import DispatchService

    try:
        report = DispatchService().dispatch_due()
    except Exception as exc:  # noqa: BLE001 - same reason
        log.exception("worker.dispatch_crashed")
        agent_status.record(agent_status.LAST_ERROR, f"Dispatch failed: {exc}"[:500])
        return

    agent_status.record(agent_status.LAST_DISPATCH)
    if report.sent or report.failed:
        log.info("worker.dispatch_done", sent=len(report.sent), failed=len(report.failed))


def run_once() -> int:
    """Both passes, once, then exit. For testing and for Task Scheduler."""
    run_discovery()
    run_dispatch()
    return 0


def build_scheduler() -> BlockingScheduler:
    settings = get_settings().agent
    scheduler = BlockingScheduler(timezone=settings.timezone)

    scheduler.add_job(
        run_discovery,
        "interval",
        hours=settings.discovery_interval_hours,
        id="discovery",
        # If the machine slept through a run, do one now rather than several.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_dispatch,
        "interval",
        minutes=DISPATCH_INTERVAL_MINUTES,
        id="dispatch",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=600,
    )
    return scheduler


def main() -> int:
    parser = argparse.ArgumentParser(description="Vays newsletter automation worker.")
    parser.add_argument("--once", action="store_true", help="Run one pass of each job and exit.")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"\n  Configuration problem:\n\n{exc.user_message}\n")  # noqa: T201
        return 1

    configure_logging(settings.logging.log_level)

    from modules.repository.database import init_database
    from modules.repository.log_handler import attach_db_log_handler
    from services import agent_status
    from services.settings_service import SettingsService

    init_database()
    SettingsService().apply_saved()
    attach_db_log_handler()

    agent = get_settings().agent
    if not agent.enabled:
        print(  # noqa: T201
            "\n  The agent is turned off (AGENT_ENABLED=false).\n"
            "  Turn it on in Settings -> Agent, or in .env, then start this again.\n"
        )
        return 1
    if not agent.approval_email.strip():
        print(  # noqa: T201
            "\n  No approval address is configured.\n"
            "  Set AGENT_APPROVAL_EMAIL, or Settings -> Agent, before starting the worker.\n"
        )
        return 1

    if args.once:
        print("  Running one pass...\n")  # noqa: T201
        return run_once()

    agent_status.record(agent_status.WORKER_STARTED)
    scheduler = build_scheduler()

    def stop(_signum: int, _frame: FrameType | None) -> None:
        log.info("worker.stopping")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(  # noqa: T201
        f"\n  Vays newsletter agent\n"
        f"  ---------------------\n"
        f"  Watching   : {agent.blog_url}\n"
        f"  Discovery  : every {agent.discovery_interval_hours}h\n"
        f"  Sends at   : {agent.send_time} ({agent.timezone}), once approved\n"
        f"  Approvals  : {agent.approval_email}\n\n"
        f"  Leave this window open. Press Ctrl+C to stop.\n"
    )
    log.info(
        "worker.started",
        interval_hours=agent.discovery_interval_hours,
        send_time=agent.send_time,
    )

    # The first discovery runs immediately rather than after a full interval:
    # starting the worker and seeing nothing happen for six hours reads as
    # broken, and is the first thing anyone would report.
    run_discovery()
    run_dispatch()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("worker.stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
