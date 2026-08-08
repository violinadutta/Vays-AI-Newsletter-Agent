"""Circuit breaker tests.

The scenario these encode: a Colab session expires mid-batch. Without a breaker,
six articles each discover the dead endpoint separately — six timeouts plus
retries, roughly twelve minutes to learn one fact.
"""

from __future__ import annotations

import threading
import time

import pytest

from modules.ai.circuit_breaker import CircuitBreaker, CircuitState


@pytest.fixture
def breaker() -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=3, reset_timeout_s=60)


class TestClosedState:
    def test_starts_closed(self, breaker: CircuitBreaker) -> None:
        assert breaker.state is CircuitState.CLOSED
        assert breaker.allows_request()

    def test_failures_below_the_threshold_do_not_trip_it(self, breaker: CircuitBreaker) -> None:
        breaker.record_failure("blip")
        breaker.record_failure("blip")

        assert breaker.allows_request()

    def test_a_success_resets_the_count(self, breaker: CircuitBreaker) -> None:
        """Consecutive failures, not cumulative — an endpoint that works between
        occasional blips should never trip."""
        breaker.record_failure("blip")
        breaker.record_failure("blip")
        breaker.record_success()
        breaker.record_failure("blip")
        breaker.record_failure("blip")

        assert breaker.allows_request()


class TestOpenState:
    def test_trips_at_the_threshold(self, breaker: CircuitBreaker) -> None:
        for _ in range(3):
            breaker.record_failure("connection refused")

        assert breaker.state is CircuitState.OPEN
        assert not breaker.allows_request()

    def test_the_tripping_error_is_retained_for_the_message(self, breaker: CircuitBreaker) -> None:
        """The UI needs to say *why*, not just that the circuit is open."""
        for _ in range(3):
            breaker.record_failure("tunnel host did not resolve")

        assert "tunnel host" in breaker.last_error

    def test_it_fails_fast_rather_than_waiting(self, breaker: CircuitBreaker) -> None:
        """The point of the whole class: no 120-second timeout per article once
        we already know the endpoint is dead."""
        for _ in range(3):
            breaker.record_failure("down")

        started = time.monotonic()
        allowed = breaker.allows_request()

        assert allowed is False
        assert time.monotonic() - started < 0.01


class TestHalfOpen:
    def test_becomes_half_open_after_the_reset_window(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=0)
        breaker.record_failure("down")

        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker.allows_request()

    def test_a_successful_probe_closes_it(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=0)
        breaker.record_failure("down")
        breaker.record_success()

        assert breaker.state is CircuitState.CLOSED

    def test_a_failed_probe_reopens_immediately(self) -> None:
        """No second chance: the probe *was* the test.

        A long reset window is used so the reopened circuit stays open for the
        assertion — with a zero window it would flip straight back to HALF_OPEN
        and the test would prove nothing.
        """
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=600)
        breaker.record_failure("down")
        breaker._state = CircuitState.HALF_OPEN  # noqa: SLF001 - simulate the window elapsing

        breaker.record_failure("still down")

        assert breaker.state is CircuitState.OPEN
        assert not breaker.allows_request()


class TestManualReset:
    def test_reset_closes_the_circuit(self, breaker: CircuitBreaker) -> None:
        """Used when the user pastes a new endpoint URL in Settings — they have
        just told us the problem is fixed, so making them wait out the reset
        window is pointless friction."""
        for _ in range(3):
            breaker.record_failure("down")

        breaker.reset()

        assert breaker.allows_request()
        assert breaker.last_error == ""


class TestThreadSafety:
    def test_concurrent_failures_are_counted_exactly_once_each(self) -> None:
        """Extraction and generation both run on worker threads; two threads
        failing at once must not double-count towards the threshold."""
        breaker = CircuitBreaker(failure_threshold=50, reset_timeout_s=60)

        def hammer() -> None:
            for _ in range(10):
                breaker.record_failure("concurrent")

        threads = [threading.Thread(target=hammer) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 5 threads x 10 failures = exactly the 50-failure threshold.
        assert breaker.state is CircuitState.OPEN

    def test_concurrent_reads_do_not_deadlock(self, breaker: CircuitBreaker) -> None:
        errors: list[Exception] = []

        def read() -> None:
            try:
                for _ in range(100):
                    breaker.allows_request()
                    breaker.record_success()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=read) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors
        assert all(not t.is_alive() for t in threads)
