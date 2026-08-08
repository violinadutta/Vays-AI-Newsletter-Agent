"""Circuit breaker for the LLM endpoint.

The problem it solves is specific and real: when the LLM endpoint is down — a
revoked key, a network fault, an outage — a 6-article batch would otherwise
discover that six separate times, each after a full 120-second timeout plus three
retries. Twelve minutes to learn one fact.

With it, the first failure costs the timeout and every subsequent call fails
instantly with the same explanation, until the reset window elapses and one probe
is allowed through.
"""

from __future__ import annotations

import threading
import time
from enum import StrEnum

from config import get_logger

log = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing fast
    HALF_OPEN = "half_open"  # one probe allowed through


class CircuitBreaker:
    """Trips after ``failure_threshold`` consecutive failures.

    Thread-safe: extraction and generation both run on worker threads, and two
    threads failing simultaneously must not double-count towards the threshold.
    """

    def __init__(self, failure_threshold: int = 3, reset_timeout_s: int = 60) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at = 0.0
        self._state = CircuitState.CLOSED
        self._last_error = ""

    @property
    def state(self) -> CircuitState:
        """Current state, accounting for an elapsed reset window."""
        with self._lock:
            return self._current_state()

    def _current_state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and (
            time.monotonic() - self._opened_at >= self.reset_timeout_s
        ):
            self._state = CircuitState.HALF_OPEN
            log.info("circuit.half_open", after_s=self.reset_timeout_s)
        return self._state

    @property
    def last_error(self) -> str:
        """The failure that tripped the circuit, for the user-facing message."""
        with self._lock:
            return self._last_error

    def allows_request(self) -> bool:
        """Whether a call may proceed now."""
        with self._lock:
            return self._current_state() is not CircuitState.OPEN

    def record_success(self) -> None:
        """Reset the breaker. A single success closes it from HALF_OPEN."""
        with self._lock:
            if self._state is not CircuitState.CLOSED:
                log.info("circuit.closed", after_failures=self._failures)
            self._failures = 0
            self._state = CircuitState.CLOSED
            self._last_error = ""

    def record_failure(self, error: str = "") -> None:
        """Count a failure and trip the circuit if the threshold is reached.

        A failure in HALF_OPEN re-opens immediately: the probe was the test, and
        it failed, so there is nothing to gain from letting more calls through.
        """
        with self._lock:
            self._last_error = error or self._last_error

            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                log.warning("circuit.reopened", error=error)
                return

            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                log.warning(
                    "circuit.opened",
                    failures=self._failures,
                    reset_in_s=self.reset_timeout_s,
                    error=error,
                )

    def reset(self) -> None:
        """Force the circuit closed.

        Used when the user updates the endpoint URL in Settings: they have just
        told us the problem is fixed, and making them wait out the reset window
        would be pointless friction.
        """
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED
            self._last_error = ""
