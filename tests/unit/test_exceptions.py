"""Tests for the exception hierarchy.

The contract these enforce: **every** error the application raises deliberately
carries a message that is safe and useful to show a non-technical marketing
executive (NFR-U2). A stack trace reaching Priya is a product defect, not a
cosmetic one.
"""

from __future__ import annotations

import inspect

import pytest

from core import exceptions as exc_module
from core.exceptions import (
    EmailAuthError,
    EmailQuotaExceeded,
    FetchError,
    InvalidJSONResponse,
    LLMUnavailableError,
    NewsletterAppError,
    PartialSendFailure,
)

ALL_ERROR_CLASSES = [
    obj
    for _, obj in inspect.getmembers(exc_module, inspect.isclass)
    if issubclass(obj, NewsletterAppError) and obj is not NewsletterAppError
]


class TestContract:
    def test_the_hierarchy_is_not_empty(self) -> None:
        assert len(ALL_ERROR_CLASSES) >= 15

    @pytest.mark.parametrize("error_cls", ALL_ERROR_CLASSES, ids=lambda c: c.__name__)
    def test_every_error_has_a_usable_user_message(self, error_cls: type) -> None:
        message = error_cls.default_user_message
        assert message, f"{error_cls.__name__} has no user message"
        assert len(message) > 20, f"{error_cls.__name__}'s message is too terse to act on"
        assert message[0].isupper(), "user-facing text should read as a sentence"

    @pytest.mark.parametrize("error_cls", ALL_ERROR_CLASSES, ids=lambda c: c.__name__)
    def test_user_messages_leak_no_implementation_detail(self, error_cls: type) -> None:
        """Priya must never see a module path, an exception class, or a status code."""
        message = error_cls.default_user_message.lower()
        for leak in (
            "traceback",
            "exception",
            "none",
            "null",
            "httpx",
            "sqlalchemy",
            "pydantic",
            "modules.",
            "core.",
            "0x",
            "errno",
        ):
            assert leak not in message, f"{error_cls.__name__} leaks {leak!r}"

    @pytest.mark.parametrize("error_cls", ALL_ERROR_CLASSES, ids=lambda c: c.__name__)
    def test_every_error_is_catchable_as_the_base_type(self, error_cls: type) -> None:
        """The UI catches exactly one type; anything else is an unhandled bug."""
        assert issubclass(error_cls, NewsletterAppError)


class TestRetryability:
    """Retrying the wrong thing wastes 3x the timeout and hides the real cause."""

    @pytest.mark.parametrize("error_cls", [FetchError, LLMUnavailableError, InvalidJSONResponse])
    def test_transient_failures_are_retryable(self, error_cls: type) -> None:
        assert error_cls.retryable is True

    @pytest.mark.parametrize("error_cls", [EmailAuthError, EmailQuotaExceeded])
    def test_permanent_failures_are_not_retryable(self, error_cls: type) -> None:
        """A rejected API key will still be rejected on attempt three."""
        assert error_cls.retryable is False

    def test_base_default_is_not_retryable(self) -> None:
        """Fail closed: opting in to retries should be a deliberate act."""
        assert NewsletterAppError.retryable is False


class TestInstanceBehaviour:
    def test_technical_and_user_messages_are_independent(self) -> None:
        error = LLMUnavailableError("ConnectionError: tunnel host did not resolve")

        assert "ConnectionError" in error.message
        assert "ConnectionError" not in error.user_message

    def test_user_message_can_be_overridden_per_instance(self) -> None:
        error = FetchError("HTTP 403", user_message="That site blocked us.")

        assert error.user_message == "That site blocked us."

    def test_context_is_carried_for_structured_logging(self) -> None:
        error = FetchError("timeout", context={"url": "https://dell.com", "attempt": 3})

        assert error.context["url"] == "https://dell.com"
        assert error.context["attempt"] == 3

    def test_context_defaults_to_empty_dict_not_none(self) -> None:
        """So callers can always do ``**error.context`` without a guard."""
        assert FetchError("x").context == {}

    def test_str_returns_the_technical_message(self) -> None:
        assert str(FetchError("connection reset")) == "connection reset"


class TestPartialSendFailure:
    """Partial failure is a first-class outcome: 485 of 487 delivered is a
    successful campaign with a follow-up action, not a failed one."""

    def test_carries_the_counts_needed_for_the_retry_ui(self) -> None:
        error = PartialSendFailure("2 of 487 failed", sent=485, failed=2)

        assert error.sent == 485
        assert error.failed == 2

    def test_is_still_a_normal_application_error(self) -> None:
        assert isinstance(PartialSendFailure("x", sent=1, failed=1), NewsletterAppError)
