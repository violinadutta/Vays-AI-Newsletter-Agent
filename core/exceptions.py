"""Application exception hierarchy.

Design (TRD §7):

1.  **Every** error raised by this application inherits :class:`NewsletterAppError`.
    The UI catches that one type and can trust that a safe, human-readable message
    is attached. Anything else reaching the UI is a genuine bug and is reported
    with a correlation reference instead of a stack trace.

2.  Each exception carries two messages:

    * ``message``      — technical, goes to the logs.
    * ``user_message`` — what Priya sees. It answers *what happened, why, and what
      to do next*, in that order. Never a stack trace, never a library name.

3.  ``retryable`` tells the retry layer whether attempting again could possibly
    help. Retrying a 401 just wastes 3 × the timeout and hides the real problem,
    so authentication failures are explicitly *not* retryable.

Adding a new error type means adding a class here — never raising a bare
``Exception`` or ``ValueError`` from application code.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AIError",
    "AllExtractorsFailed",
    "ConfigurationError",
    "ContentTooShortError",
    "EmailAuthError",
    "EmailError",
    "EmailProviderError",
    "EmailQuotaExceeded",
    "DiscoveryError",
    "ExtractionError",
    "FetchError",
    "InvalidCSVError",
    "InvalidEmailError",
    "InvalidJSONResponse",
    "InvalidStateTransition",
    "InvalidURLError",
    "LLMRateLimitedError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "NewsletterAppError",
    "PartialSendFailure",
    "PersistenceError",
    "PromptNotFoundError",
    "TemplateError",
    "ValidationError",
]


class NewsletterAppError(Exception):
    """Base class for every error this application raises deliberately.

    Args:
        message: Technical description for logs and developers.
        user_message: Safe, actionable text for the UI. Falls back to the
            class-level ``default_user_message`` when not supplied.
        context: Structured data attached to the log record (never rendered in
            the UI). Must not contain secrets — the logging layer redacts known
            secret-shaped keys, but do not rely on that as the only defence.
    """

    default_user_message = "Something went wrong. Check the Logs page for details."
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        user_message: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.user_message = user_message or self.default_user_message
        self.context: dict[str, Any] = context or {}

    def __str__(self) -> str:
        return self.message


# ─────────────────────────────────────────────────────────────────────────────
#  Configuration — fatal, raised at startup before the UI renders
# ─────────────────────────────────────────────────────────────────────────────
class ConfigurationError(NewsletterAppError):
    """Settings are missing or invalid.

    Raised at import time on purpose. A misconfigured application must fail
    immediately and loudly rather than appear to work and fail confusingly an
    hour later during a live campaign.
    """

    default_user_message = (
        "The application is not configured correctly. Check your .env file "
        "against .env.example, then restart."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Validation — bad user input
# ─────────────────────────────────────────────────────────────────────────────
class ValidationError(NewsletterAppError):
    """User-supplied input failed validation."""

    default_user_message = "That input isn't valid. Please check it and try again."


class InvalidURLError(ValidationError):
    default_user_message = "That doesn't look like a valid web address. URLs start with https://"


class InvalidEmailError(ValidationError):
    default_user_message = "That email address isn't valid."


class InvalidCSVError(ValidationError):
    default_user_message = (
        "The recipient file couldn't be read. It needs a column named 'email'. "
        "Download the sample file to see the expected format."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Discovery
# ─────────────────────────────────────────────────────────────────────────────
class DiscoveryError(NewsletterAppError):
    """A discovery source could not be read.

    Retryable by default: the overwhelmingly common cause is a transient network
    problem or a site briefly down, and a discovery run that fails is simply one
    the scheduler repeats. It must never take the application down — nobody is
    watching when it fires.
    """

    retryable = True
    default_user_message = (
        "Couldn't check the blog for new posts. The next scheduled run will try again."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Extraction
# ─────────────────────────────────────────────────────────────────────────────
class ExtractionError(NewsletterAppError):
    default_user_message = "Couldn't read that article."


class FetchError(ExtractionError):
    """The page could not be retrieved (network, DNS, HTTP status, timeout)."""

    retryable = True
    default_user_message = (
        "Couldn't reach that page. Check the link is correct and that the site is online."
    )


class ContentTooShortError(ExtractionError):
    """Extraction succeeded but returned too little text to be usable.

    Not an error in a cascade — the orchestrator treats it as a signal to try
    the next extractor tier.
    """

    default_user_message = (
        "That page didn't contain enough article text to work with. "
        "It may be a landing page rather than an article."
    )


class AllExtractorsFailed(ExtractionError):
    default_user_message = (
        "Couldn't read this article — the site may block automated readers. "
        "Use 'Paste text manually' to continue."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  AI
# ─────────────────────────────────────────────────────────────────────────────
class AIError(NewsletterAppError):
    default_user_message = "The AI service had a problem. Try again in a moment."


class LLMUnavailableError(AIError):
    """The LLM endpoint is unreachable, or the circuit breaker is open.

    The message names what to check rather than what threw, because the person
    reading it is a marketing executive, not a developer.
    """

    retryable = True
    default_user_message = (
        "The AI service isn't responding. Your articles are saved — nothing was lost.\n\n"
        "Check your internet connection, then use Settings → AI Service → "
        "Test Connection. If that fails, the API key may need renewing."
    )


class LLMTimeoutError(AIError):
    retryable = True
    default_user_message = (
        "The AI service took too long to respond. Try again, or choose a shorter newsletter length."
    )


class InvalidJSONResponse(AIError):
    """The model's output failed schema or business validation after repair attempts."""

    retryable = True
    default_user_message = (
        "The AI returned an unexpected response. Click Retry — this usually "
        "resolves on the second attempt."
    )


class LLMRateLimitedError(AIError):
    """The provider's rate limit was hit and did not clear within the retry window.

    Distinct from :class:`LLMUnavailableError` because nothing is broken — the
    quota simply needs a moment, and telling the user to check their connection
    would send them hunting for a problem that does not exist.
    """

    retryable = True
    default_user_message = (
        "The AI service is busy right now (rate limit reached). Wait about a "
        "minute and try again, or use fewer articles per newsletter."
    )


class PromptNotFoundError(AIError):
    """A prompt name or version does not exist in the registry. A deployment bug."""

    default_user_message = (
        "An internal prompt file is missing. This is a configuration problem — "
        "please report it with the reference code shown in the Logs page."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Templating
# ─────────────────────────────────────────────────────────────────────────────
class TemplateError(NewsletterAppError):
    default_user_message = "Couldn't build the email layout. Try selecting a different template."


# ─────────────────────────────────────────────────────────────────────────────
#  Email
# ─────────────────────────────────────────────────────────────────────────────
class EmailError(NewsletterAppError):
    default_user_message = "Couldn't send the email."


class EmailProviderError(EmailError):
    retryable = True
    default_user_message = "The email service rejected the request. See the Logs page for details."


class EmailAuthError(EmailError):
    """Credentials rejected. Deliberately NOT retryable — retrying a bad key
    wastes time and obscures the real cause."""

    retryable = False
    default_user_message = (
        "The email service rejected our credentials. Check the API key in Settings → Email."
    )


class EmailQuotaExceeded(EmailError):
    retryable = False
    default_user_message = (
        "The daily email limit has been reached. Emails already sent were delivered "
        "successfully. Resume tomorrow, or upgrade the email plan."
    )


class PartialSendFailure(EmailError):
    """Some recipients succeeded and some failed.

    A first-class outcome, not an exception in the usual sense — it carries the
    per-recipient report so the caller can offer 'retry failed only'. Batch
    operations never raise merely because *some* items failed; they raise this
    only when the caller has explicitly asked for all-or-nothing semantics.
    """

    retryable = False
    default_user_message = "Some emails couldn't be delivered. See the list below."

    def __init__(
        self,
        message: str,
        *,
        sent: int,
        failed: int,
        user_message: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, user_message=user_message, context=context)
        self.sent = sent
        self.failed = failed


# ─────────────────────────────────────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────────────────────────────────────
class PersistenceError(NewsletterAppError):
    default_user_message = "Couldn't save your work. Please try again."


class InvalidStateTransition(PersistenceError):
    """An illegal campaign state change was attempted.

    Also the double-send guard: the SENDING transition is a conditional UPDATE,
    so a duplicate Streamlit rerun that fires the send handler twice matches zero
    rows the second time and lands here instead of sending twice.
    """

    default_user_message = (
        "That action isn't available for this campaign right now. "
        "Refresh the page to see its current status."
    )
