"""System health — what the Dashboard and Settings pages report.

The user needs to know the LLM is reachable *before* investing effort in a draft,
not after clicking Generate — whether the cause is a revoked key, an exhausted
quota or an outage. That is the whole reason this service exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from config import get_logger, get_settings
from config.constants import HEALTH_CHECK_CACHE_S
from core.models import HealthStatus
from modules.ai.factory import create_provider
from modules.repository.database import unit_of_work
from modules.repository.log_repo import LogRepository

log = get_logger(__name__)


@dataclass(frozen=True)
class SystemHealth:
    llm: HealthStatus
    database: HealthStatus
    email: HealthStatus

    @property
    def all_healthy(self) -> bool:
        return self.llm.healthy and self.database.healthy and self.email.healthy

    @property
    def can_generate(self) -> bool:
        return self.llm.healthy and self.database.healthy


class HealthService:
    """Probes each dependency, with caching to survive Streamlit's rerun model."""

    def __init__(self) -> None:
        self._cache: tuple[float, SystemHealth] | None = None

    def check(self, *, force: bool = False) -> SystemHealth:
        """Return current system health.

        Cached for 30 seconds unless ``force`` is set — the Settings page's
        "Test connection" button always forces, because the user has just changed
        something and expects a real answer.
        """
        now = time.monotonic()
        if not force and self._cache and now - self._cache[0] < HEALTH_CHECK_CACHE_S:
            return self._cache[1]

        health = SystemHealth(
            llm=self._check_llm(),
            database=self._check_database(),
            email=self._check_email(),
        )
        self._cache = (now, health)
        return health

    @staticmethod
    def _check_llm() -> HealthStatus:
        try:
            provider = create_provider()
        except Exception as exc:  # noqa: BLE001 - a health probe must never raise
            return HealthStatus(healthy=False, detail=f"Not configured: {exc}")
        try:
            return provider.health_check()
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(healthy=False, detail=f"Probe failed: {type(exc).__name__}")
        finally:
            provider.close()

    @staticmethod
    def _check_database() -> HealthStatus:
        started = time.monotonic()
        try:
            with unit_of_work() as session:
                count = LogRepository(session).count()
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(healthy=False, detail=f"Unavailable: {type(exc).__name__}")
        return HealthStatus(
            healthy=True,
            detail=f"{count} log entries",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _check_email() -> HealthStatus:
        # The real credential check arrives with the email providers in M6.
        # Reporting the configured provider is still the answer to "why didn't
        # that send?", so it is worth surfacing now rather than showing nothing.
        provider = get_settings().email.provider
        if provider == "console":
            return HealthStatus(healthy=True, detail="console — writes .eml files, sends nothing")
        return HealthStatus(healthy=True, detail=f"{provider} (not yet verified)")
