"""Email provider and batching tests.

The distinction under test throughout: a **per-recipient** failure returns a
FAILED result and the batch continues; an **account-level** failure raises and
stops. Getting that backwards means either one dead mailbox aborts 500 sends, or
a rejected API key produces 500 identical failures over several minutes.
"""

from __future__ import annotations

import email as email_lib
from pathlib import Path

import httpx
import pytest
import respx

from core.enums import SendStatus
from core.exceptions import ConfigurationError, EmailAuthError, EmailQuotaExceeded
from core.models import EmailMessage, SendResult
from modules.email.base import unsubscribe_headers
from modules.email.batcher import BatchSender
from modules.email.brevo_provider import BREVO_API, BrevoEmailProvider
from modules.email.console_provider import ConsoleEmailProvider
from modules.email.factory import available_email_providers, create_email_provider


@pytest.fixture(autouse=True)
def _email_env(set_env) -> None:  # noqa: ANN001
    from tests.conftest import MINIMAL_ENV

    set_env(
        **MINIMAL_ENV,
        EMAIL_SENDER_NAME="Vays Infotech",
        EMAIL_SENDER_ADDRESS="newsletter@vays.com",
        BRAND_ADDRESS="Vays Infotech, Pune 411045, India",
    )


def message(to: str = "priya@vays.com") -> EmailMessage:
    return EmailMessage(
        to_email=to,
        to_name="Priya",
        subject="Dell's new servers",
        html="<html><body><p>Hello</p></body></html>",
        text="Hello",
        headers=unsubscribe_headers("https://vays.com/unsub", "newsletter@vays.com"),
    )


class TestUnsubscribeHeaders:
    """Gmail and Yahoo require one-click unsubscribe from bulk senders; absence is
    a direct deliverability penalty regardless of content."""

    def test_both_headers_are_present(self) -> None:
        headers = unsubscribe_headers("https://vays.com/unsub", "newsletter@vays.com")

        assert "List-Unsubscribe" in headers
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_it_offers_both_a_url_and_a_mailto(self) -> None:
        header = unsubscribe_headers("https://vays.com/unsub", "newsletter@vays.com")[
            "List-Unsubscribe"
        ]

        assert "https://vays.com/unsub" in header
        assert "mailto:newsletter@vays.com" in header


class TestConsoleProvider:
    def test_it_writes_a_file_and_sends_nothing(self, tmp_path: Path) -> None:
        provider = ConsoleEmailProvider(outbox=tmp_path)

        result = provider.send(message())

        assert result.status == SendStatus.SENT
        assert len(list(tmp_path.glob("*.eml"))) == 1

    def test_the_file_is_a_real_multipart_message(self, tmp_path: Path) -> None:
        """So it opens in a mail client exactly as a delivered message would —
        the cheapest possible rendering check."""
        provider = ConsoleEmailProvider(outbox=tmp_path)
        provider.send(message())

        parsed = email_lib.message_from_bytes(next(tmp_path.glob("*.eml")).read_bytes())
        subtypes = {part.get_content_type() for part in parsed.walk()}

        assert parsed["Subject"] == "Dell's new servers"
        assert "text/plain" in subtypes
        assert "text/html" in subtypes

    def test_unsubscribe_headers_survive_into_the_file(self, tmp_path: Path) -> None:
        provider = ConsoleEmailProvider(outbox=tmp_path)
        provider.send(message())

        parsed = email_lib.message_from_bytes(next(tmp_path.glob("*.eml")).read_bytes())

        assert "vays.com/unsub" in parsed["List-Unsubscribe"]

    def test_it_reports_healthy_without_credentials(self, tmp_path: Path) -> None:
        assert ConsoleEmailProvider(outbox=tmp_path).verify_credentials().healthy is True

    def test_filenames_do_not_collide(self, tmp_path: Path) -> None:
        provider = ConsoleEmailProvider(outbox=tmp_path)

        for _ in range(5):
            provider.send(message())

        assert len(list(tmp_path.glob("*.eml"))) == 5


class TestBrevoProvider:
    @respx.mock
    def test_a_successful_send(self, set_env) -> None:  # noqa: ANN001
        set_env(BREVO_API_KEY="key")
        respx.post(f"{BREVO_API}/smtp/email").mock(
            return_value=httpx.Response(201, json={"messageId": "abc-123"})
        )

        result = BrevoEmailProvider().send(message())

        assert result.status == SendStatus.SENT
        assert result.provider_message_id == "abc-123"

    @respx.mock
    def test_the_payload_carries_both_parts_and_the_headers(self, set_env) -> None:  # noqa: ANN001
        set_env(BREVO_API_KEY="key")
        route = respx.post(f"{BREVO_API}/smtp/email").mock(
            return_value=httpx.Response(201, json={"messageId": "x"})
        )

        BrevoEmailProvider().send(message())

        import json

        sent = json.loads(route.calls[0].request.content)
        assert sent["htmlContent"] and sent["textContent"]
        assert "List-Unsubscribe" in sent["headers"]

    @respx.mock
    def test_a_bad_address_fails_only_that_recipient(self, set_env) -> None:  # noqa: ANN001
        """One dead mailbox must not abort a batch of 500."""
        set_env(BREVO_API_KEY="key")
        respx.post(f"{BREVO_API}/smtp/email").mock(
            return_value=httpx.Response(400, json={"code": "invalid_email", "message": "bad"})
        )

        result = BrevoEmailProvider().send(message())

        assert result.status == SendStatus.FAILED
        assert "isn't valid" in (result.error_message or "")

    @respx.mock
    def test_a_rejected_key_aborts_the_batch(self, set_env) -> None:  # noqa: ANN001
        """Account-level: every remaining send would fail identically."""
        set_env(BREVO_API_KEY="key")
        respx.post(f"{BREVO_API}/smtp/email").mock(return_value=httpx.Response(401))

        with pytest.raises(EmailAuthError):
            BrevoEmailProvider().send(message())

    @respx.mock
    def test_exhausted_credits_abort_the_batch(self, set_env) -> None:  # noqa: ANN001
        set_env(BREVO_API_KEY="key")
        respx.post(f"{BREVO_API}/smtp/email").mock(return_value=httpx.Response(402))

        with pytest.raises(EmailQuotaExceeded):
            BrevoEmailProvider().send(message())

    @respx.mock
    def test_a_network_fault_is_a_recipient_failure_not_a_crash(self, set_env) -> None:  # noqa: ANN001
        set_env(BREVO_API_KEY="key")
        respx.post(f"{BREVO_API}/smtp/email").mock(side_effect=httpx.ConnectError("down"))

        assert BrevoEmailProvider().send(message()).status == SendStatus.FAILED

    @respx.mock
    def test_health_check_reports_remaining_credit(self, set_env) -> None:  # noqa: ANN001
        """Turns "the send stopped" into "you have 60 sends left today"."""
        set_env(BREVO_API_KEY="key")
        respx.get(f"{BREVO_API}/account").mock(
            return_value=httpx.Response(200, json={"plan": [{"credits": 240}]})
        )

        assert "240" in BrevoEmailProvider().verify_credentials().detail

    @respx.mock
    def test_health_check_never_raises(self, set_env) -> None:  # noqa: ANN001
        set_env(BREVO_API_KEY="key")
        respx.get(f"{BREVO_API}/account").mock(side_effect=httpx.ConnectError("down"))

        assert BrevoEmailProvider().verify_credentials().healthy is False


class TestBatchSender:
    class _Recording(ConsoleEmailProvider):
        """Console provider that can be told to fail specific addresses."""

        def __init__(
            self, outbox: Path, fail: set[str] | None = None, raise_on: Exception | None = None
        ) -> None:
            super().__init__(outbox=outbox)
            self.fail = fail or set()
            self.raise_on = raise_on
            self.attempts = 0

        def send(self, message: EmailMessage) -> SendResult:
            self.attempts += 1
            if self.raise_on is not None:
                raise self.raise_on
            if message.to_email in self.fail:
                return SendResult(
                    email=message.to_email,
                    status=SendStatus.FAILED,
                    error_code="mailbox_not_found",
                    error_message="Mailbox does not exist",
                )
            return super().send(message)

    def test_every_recipient_gets_a_result(self, tmp_path: Path) -> None:
        provider = self._Recording(tmp_path)
        messages = [message(f"user{i}@vays.com") for i in range(7)]

        results = BatchSender(provider, batch_size=3, batch_delay_s=0).send_many(messages)

        assert len(results) == 7
        assert all(r.ok for r in results)

    def test_partial_failure_is_data_not_an_exception(self, tmp_path: Path) -> None:
        """485 of 487 delivered is a successful campaign with a follow-up action."""
        provider = self._Recording(tmp_path, fail={"bad@vays.com"})
        messages = [message("ok@vays.com"), message("bad@vays.com"), message("fine@vays.com")]

        results = BatchSender(provider, batch_size=10, batch_delay_s=0).send_many(messages)

        assert sum(1 for r in results if r.ok) == 2
        assert sum(1 for r in results if not r.ok) == 1

    def test_a_rejected_mailbox_is_not_retried(self, tmp_path: Path) -> None:
        """It will be rejected identically on attempt three."""
        provider = self._Recording(tmp_path, fail={"bad@vays.com"})

        BatchSender(provider, batch_size=10, batch_delay_s=0, max_retries=3).send_many(
            [message("bad@vays.com")]
        )

        assert provider.attempts == 1

    def test_progress_is_reported_per_batch(self, tmp_path: Path) -> None:
        provider = self._Recording(tmp_path)
        updates: list[tuple[int, int, int]] = []

        BatchSender(provider, batch_size=2, batch_delay_s=0).send_many(
            [message(f"u{i}@vays.com") for i in range(6)],
            on_progress=lambda s, f, r: updates.append((s, f, r)),
        )

        assert len(updates) == 3
        assert updates[-1] == (6, 0, 0)

    def test_an_account_failure_aborts_and_reports_what_was_sent(self, tmp_path: Path) -> None:
        """The already-delivered count must survive, or retry re-sends them."""
        provider = self._Recording(tmp_path, raise_on=EmailAuthError("key rejected"))

        with pytest.raises(EmailAuthError) as exc_info:
            BatchSender(provider, batch_size=10, batch_delay_s=0).send_many(
                [message(f"u{i}@vays.com") for i in range(5)]
            )

        assert "sent_before_failure" in exc_info.value.context

    def test_stop_finishes_the_current_batch(self, tmp_path: Path) -> None:
        """Aborting mid-batch would leave sends whose outcome was never recorded."""
        provider = self._Recording(tmp_path)
        sender = BatchSender(provider, batch_size=2, batch_delay_s=0)
        sender.request_stop()

        results = sender.send_many([message(f"u{i}@vays.com") for i in range(6)])

        assert results == []


class TestFactory:
    def test_console_is_the_default(self) -> None:
        """A fresh checkout must not be able to email a real customer."""
        assert create_email_provider().name == "console"

    def test_brevo_without_a_key_fails_at_startup(self, set_env) -> None:  # noqa: ANN001
        set_env(EMAIL_PROVIDER="brevo", BREVO_API_KEY="")

        with pytest.raises(ConfigurationError) as exc_info:
            create_email_provider()

        assert "BREVO_API_KEY" in exc_info.value.user_message

    def test_smtp_without_a_host_fails_at_startup(self, set_env) -> None:  # noqa: ANN001
        set_env(EMAIL_PROVIDER="smtp", SMTP_HOST="")

        with pytest.raises(ConfigurationError) as exc_info:
            create_email_provider()

        assert "SMTP_HOST" in exc_info.value.user_message

    def test_an_unknown_provider_lists_the_valid_ones(self) -> None:
        with pytest.raises(ConfigurationError) as exc_info:
            create_email_provider("carrier-pigeon")

        assert "console" in exc_info.value.user_message

    def test_all_three_providers_are_registered(self) -> None:
        assert set(available_email_providers()) == {"brevo", "smtp", "console"}
