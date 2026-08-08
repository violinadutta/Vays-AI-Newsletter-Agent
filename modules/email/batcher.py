"""Batching, pacing and retry — owned here, not by each provider.

Separating this from the providers is what keeps a second adapter cheap: a new
provider implements two methods and inherits all of the delivery robustness. It
also means the retry policy is identical across Brevo, SMTP and console, so a
bug found in one is fixed for all.

**Partial failure is the normal case, not an exception.** 485 of 487 delivered is
a successful campaign with a follow-up action. This class never raises because
some sends failed; it returns the per-recipient results and lets the caller
decide.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from config import get_logger, get_settings, mask_email
from core.enums import SendStatus
from core.exceptions import EmailAuthError, EmailProviderError, EmailQuotaExceeded
from core.models import EmailMessage, SendResult
from modules.email.base import EmailProvider

log = get_logger(__name__)

ProgressCallback = Callable[[int, int, int], None]
"""Called with (sent, failed, remaining) after each batch."""


class BatchSender:
    """Sends many messages with pacing, retry and live progress."""

    def __init__(
        self,
        provider: EmailProvider,
        *,
        batch_size: int | None = None,
        batch_delay_s: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings().email
        self.provider = provider
        self.batch_size = batch_size if batch_size is not None else settings.batch_size
        self.batch_delay_s = batch_delay_s if batch_delay_s is not None else settings.batch_delay_s
        self.max_retries = max_retries if max_retries is not None else settings.max_retries
        self._stop_requested = False

    def request_stop(self) -> None:
        """Ask the send to finish the current batch and stop.

        Deliberately not immediate. Aborting mid-batch would leave sends whose
        outcome we never recorded — the recipient may or may not have received
        the email, and neither answer can be proven afterwards.
        """
        self._stop_requested = True

    def send_many(
        self,
        messages: list[EmailMessage],
        *,
        on_progress: ProgressCallback | None = None,
    ) -> list[SendResult]:
        """Send every message, returning one result per recipient.

        Never raises for individual failures. Raises only when continuing is
        pointless — bad credentials or an exhausted quota — and even then the
        results gathered so far are attached to the exception's context so the
        caller can record what did go out.
        """
        results: list[SendResult] = []
        total = len(messages)
        sent = failed = 0

        for index in range(0, total, self.batch_size):
            if self._stop_requested:
                log.info("send.stopped_by_request", completed=len(results), total=total)
                break

            batch = messages[index : index + self.batch_size]
            batch_number = index // self.batch_size + 1

            for message in batch:
                result = self._send_one(message, results)
                results.append(result)
                if result.ok:
                    sent += 1
                else:
                    failed += 1

            if on_progress:
                on_progress(sent, failed, total - len(results))

            log.info(
                "send.batch_complete",
                batch=batch_number,
                sent=sent,
                failed=failed,
                remaining=total - len(results),
            )

            # Pace between batches, but not after the final one — a trailing sleep
            # is pure latency the user watches for no benefit.
            if self.batch_delay_s and index + self.batch_size < total:
                time.sleep(self.batch_delay_s)

        return results

    def _send_one(self, message: EmailMessage, so_far: list[SendResult]) -> SendResult:
        """Send one message, retrying transient provider faults."""
        attempt = 0
        last_error = ""

        while attempt <= self.max_retries:
            attempt += 1
            try:
                result = self.provider.send(message)
            except (EmailAuthError, EmailQuotaExceeded) as exc:
                # Account-level. Attach what has already been delivered so the
                # caller can persist it rather than losing the record.
                exc.context.update(
                    {
                        "sent_before_failure": sum(1 for r in so_far if r.ok),
                        "attempted": len(so_far),
                    }
                )
                # warning, not error: this is re-raised for the caller to handle,
                # and log.exception here would duplicate the traceback.
                log.warning("send.aborted", reason=type(exc).__name__, completed=len(so_far))
                raise
            except EmailProviderError as exc:
                last_error = exc.message
                if attempt <= self.max_retries:
                    delay = 2**attempt
                    log.warning(
                        "send.retrying",
                        to=mask_email(message.to_email),
                        attempt=attempt,
                        wait_s=delay,
                    )
                    time.sleep(delay)
                    continue
                break

            if result.ok or attempt > self.max_retries:
                return result.model_copy(update={"attempts": attempt})

            # A per-recipient failure. Retrying a rejected mailbox will not help,
            # so this is returned rather than retried.
            return result.model_copy(update={"attempts": attempt})

        return SendResult(
            email=message.to_email,
            status=SendStatus.FAILED,
            error_code="provider_error",
            error_message=last_error or "The email service kept failing.",
            attempts=attempt,
        )
