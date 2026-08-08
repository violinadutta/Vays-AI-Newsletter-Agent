"""Email provider contract, and the headers every send must carry.

Two members only. Batching, pacing and retry deliberately live in
:mod:`modules.email.batcher` rather than in each provider — otherwise every new
adapter has to reimplement the delivery robustness, and they will each get it
slightly wrong.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import EmailMessage, HealthStatus, SendResult


class EmailProvider(ABC):
    """Something that can deliver one message."""

    #: Recorded on send records for provenance.
    name: str = "unknown"

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult:
        """Deliver one message.

        **Must not raise for an ordinary delivery failure.** A rejected mailbox is
        a per-recipient outcome, not an exception — a batch of 500 must not abort
        because one address is dead. Return a ``SendResult`` with ``FAILED``.

        Raising is reserved for conditions that make the *rest of the batch*
        pointless: bad credentials, exhausted quota.

        Raises:
            EmailAuthError: Credentials rejected — every subsequent send will fail.
            EmailQuotaExceeded: The account's limit is spent.
        """

    @abstractmethod
    def verify_credentials(self) -> HealthStatus:
        """Cheap check that this provider could send. Must never raise."""

    def close(self) -> None:  # noqa: B027 - intentionally optional
        """Release any held resources.

        Deliberately concrete and empty rather than abstract: a provider holding
        no resources (console, SMTP) should not be forced to write a stub.
        """


def unsubscribe_headers(unsubscribe_url: str, sender_address: str) -> dict[str, str]:
    """Build the ``List-Unsubscribe`` headers.

    These are not decoration. Gmail and Yahoo require one-click unsubscribe for
    bulk senders, and their absence is a direct, measurable deliverability
    penalty — the message is more likely to be filtered regardless of content.

    ``List-Unsubscribe-Post`` is what makes the client's own "unsubscribe" button
    work without the recipient visiting a page, which is the behaviour the
    mailbox providers actually score.
    """
    mailto = f"mailto:{sender_address}?subject=unsubscribe"
    return {
        "List-Unsubscribe": f"<{mailto}>, <{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
