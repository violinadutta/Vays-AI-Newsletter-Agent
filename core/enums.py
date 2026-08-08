"""Domain enumerations and the campaign state machine.

``StrEnum`` throughout so members serialise to plain strings in JSON, the
database and prompt templates without any conversion layer — and so a value read
back from SQLite compares equal to the enum member.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ArticleStatus",
    "Audience",
    "CampaignStatus",
    "Category",
    "EditableField",
    "ExtractorTier",
    "LengthPreset",
    "SendStatus",
    "SuppressionReason",
    "Tone",
    "UserRole",
]


# ─────────────────────────────────────────────────────────────────────────────
#  Campaign lifecycle
# ─────────────────────────────────────────────────────────────────────────────
class CampaignStatus(StrEnum):
    """Where a campaign is in its life.

    ``PARTIAL_FAILURE`` is deliberately a *terminal-ish* state rather than
    ``FAILED``: 485 of 487 delivered is a successful campaign with a follow-up
    action, and calling it a failure would misrepresent it in every report.
    """

    DRAFT = "DRAFT"
    READY = "READY"
    SENDING = "SENDING"
    SENT = "SENT"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


#: Legal transitions. Anything absent raises ``InvalidStateTransition``.
#:
#: The security-relevant entry is ``SENDING``: it is reachable only from DRAFT or
#: READY, and the repository performs the change as a conditional UPDATE. A
#: duplicate Streamlit rerun that fires the send handler twice therefore matches
#: zero rows on the second attempt — which is what makes double-sending a
#: customer newsletter impossible rather than merely unlikely (risk R-9).
_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    # DRAFT -> SENDING is legal on purpose: the Send button lives on the Preview
    # screen, and forcing an explicit "mark as ready" click first would add a step
    # with no user value. READY means "validated and has recipients", not a
    # mandatory gate. This set must stay identical to SENDABLE_STATES below —
    # they are the same rule expressed twice, and a test asserts they agree.
    CampaignStatus.DRAFT: frozenset(
        {CampaignStatus.READY, CampaignStatus.SENDING, CampaignStatus.ARCHIVED}
    ),
    CampaignStatus.READY: frozenset(
        {CampaignStatus.DRAFT, CampaignStatus.SENDING, CampaignStatus.ARCHIVED}
    ),
    # No ARCHIVED here: a campaign that is mid-send must reach a settled state
    # first, or its send records would be orphaned by an archive action.
    CampaignStatus.SENDING: frozenset(
        {CampaignStatus.SENT, CampaignStatus.PARTIAL_FAILURE, CampaignStatus.FAILED}
    ),
    # Retrying failed recipients re-enters SENDING from a settled state.
    CampaignStatus.PARTIAL_FAILURE: frozenset({CampaignStatus.SENDING, CampaignStatus.ARCHIVED}),
    CampaignStatus.FAILED: frozenset({CampaignStatus.SENDING, CampaignStatus.ARCHIVED}),
    CampaignStatus.SENT: frozenset({CampaignStatus.ARCHIVED}),
    CampaignStatus.ARCHIVED: frozenset(),
}

#: States from which a send may begin. Used by the repository's guarded UPDATE.
SENDABLE_STATES: frozenset[CampaignStatus] = frozenset({CampaignStatus.DRAFT, CampaignStatus.READY})

#: States whose content may still be edited.
EDITABLE_STATES: frozenset[CampaignStatus] = frozenset({CampaignStatus.DRAFT, CampaignStatus.READY})


def can_transition(current: CampaignStatus, target: CampaignStatus) -> bool:
    """Whether ``current -> target`` is a legal campaign transition."""
    return target in _TRANSITIONS[current]


def allowed_transitions(current: CampaignStatus) -> frozenset[CampaignStatus]:
    """The set of states reachable from ``current``."""
    return _TRANSITIONS[current]


def is_terminal(status: CampaignStatus) -> bool:
    """Whether no further transition is possible."""
    return not _TRANSITIONS[status]


# ─────────────────────────────────────────────────────────────────────────────
#  Generation options (surfaced as UI selectors)
# ─────────────────────────────────────────────────────────────────────────────
class Tone(StrEnum):
    """Voice preset. Each maps to a prompt fragment in ``prompts/_shared/tone/``."""

    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    TECHNICAL = "technical"
    EXECUTIVE = "executive"
    ENTHUSIASTIC = "enthusiastic"


class LengthPreset(StrEnum):
    """Target newsletter body length. Word counts live in ``config.constants``."""

    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class Audience(StrEnum):
    """Who the newsletter is written for. Maps to ``prompts/_shared/audience/``."""

    ENTERPRISE_IT = "enterprise_it"
    SMB = "smb"
    CHANNEL_PARTNER = "channel_partner"
    C_SUITE = "c_suite"


#: Human-readable labels for the UI and for interpolation into prompts.
#: Kept beside the enum so a new member cannot be added without a label.
AUDIENCE_LABELS: dict[Audience, str] = {
    Audience.ENTERPRISE_IT: "enterprise IT",
    Audience.SMB: "small and mid-sized business",
    Audience.CHANNEL_PARTNER: "channel partner",
    Audience.C_SUITE: "C-suite executive",
}

TONE_LABELS: dict[Tone, str] = {
    Tone.PROFESSIONAL: "Professional",
    Tone.FRIENDLY: "Friendly",
    Tone.TECHNICAL: "Technical",
    Tone.EXECUTIVE: "Executive",
    Tone.ENTHUSIASTIC: "Enthusiastic",
}


class Category(StrEnum):
    """Content category.

    A closed set on purpose: it is emitted by the model under guided decoding, so
    an enum here means the category *cannot* be hallucinated — the decoder can
    only choose one of these eight strings.
    """

    PRODUCT_LAUNCH = "Product Launch"
    SECURITY = "Security"
    CLOUD = "Cloud"
    AI_ML = "AI/ML"
    NETWORKING = "Networking"
    INFRASTRUCTURE = "Infrastructure"
    PARTNERSHIP = "Partnership"
    INDUSTRY_NEWS = "Industry News"


class EditableField(StrEnum):
    """Fields the user may edit or regenerate individually (FR-3.8, FR-4.1)."""

    TITLE = "title"
    SUMMARY = "summary"
    NEWSLETTER = "newsletter"
    SUBJECT = "subject"
    PREVIEW_TEXT = "preview_text"
    CTA = "cta"
    KEYWORDS = "keywords"
    CATEGORY = "category"
    TONE = "tone"


# ─────────────────────────────────────────────────────────────────────────────
#  Pipeline states
# ─────────────────────────────────────────────────────────────────────────────
class ArticleStatus(StrEnum):
    EXTRACTED = "EXTRACTED"
    FAILED = "FAILED"


class ExtractorTier(StrEnum):
    """Which tier of the extraction cascade produced the text.

    Recorded per article so the success rate of each tier is measurable rather
    than assumed — the cascade's whole justification is that different libraries
    fail on different pages, and that claim should be checkable.
    """

    TRAFILATURA = "trafilatura"
    NEWSPAPER4K = "newspaper4k"
    FALLBACK = "fallback"
    MANUAL = "manual"


class SendStatus(StrEnum):
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    BOUNCED = "BOUNCED"
    SUPPRESSED = "SUPPRESSED"


class SuppressionReason(StrEnum):
    UNSUBSCRIBED = "UNSUBSCRIBED"
    HARD_BOUNCE = "HARD_BOUNCE"
    COMPLAINT = "COMPLAINT"
    MANUAL = "MANUAL"


class UserRole(StrEnum):
    """Role separation (FR-8.7). Only ``APPROVER`` and ``ADMIN`` may send."""

    EDITOR = "editor"
    APPROVER = "approver"
    ADMIN = "admin"


#: Roles permitted to dispatch a campaign to real recipients.
SENDING_ROLES: frozenset[UserRole] = frozenset({UserRole.APPROVER, UserRole.ADMIN})
