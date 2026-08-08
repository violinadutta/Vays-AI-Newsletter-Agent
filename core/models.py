"""Domain data-transfer objects.

Every value that crosses a module boundary is one of these — never a raw dict.
That is what makes the boundaries checkable by mypy, self-documenting to the next
developer, and impossible to typo silently.

``extra="forbid"`` is set on all of them deliberately: an unexpected key means
either a typo or a contract drift between two modules, and both should be loud.

The ``ArticleSummary`` and ``NewsletterContent`` models do double duty — they
validate LLM output *and* generate the JSON Schemas that constrain generation
(``core.schemas``). One definition, so the prompt contract and the runtime
validation cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.constants import (
    CTA_MAX_LENGTH,
    KEYWORDS_MAX,
    KEYWORDS_MIN,
    PREVIEW_TEXT_MAX_LENGTH,
    SUBJECT_MAX_LENGTH,
    TITLE_MAX_LENGTH,
)
from core.enums import (
    ArticleStatus,
    Audience,
    CampaignStatus,
    Category,
    ExtractorTier,
    LengthPreset,
    SendStatus,
    Tone,
)

T = TypeVar("T")

_STRICT = ConfigDict(extra="forbid")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ─────────────────────────────────────────────────────────────────────────────
#  Articles
# ─────────────────────────────────────────────────────────────────────────────
class ExtractedArticle(BaseModel):
    """Raw output of one extractor tier, before cleaning."""

    model_config = _STRICT

    url: str | None = Field(None, description="None for manually pasted text")
    title: str
    text: str
    author: str | None = None
    published_at: datetime | None = None
    extractor: ExtractorTier
    extraction_ms: int = 0

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class CleanedArticle(BaseModel):
    """Normalised, budgeted article text — the input to stage-1 summarisation."""

    model_config = _STRICT

    url: str | None = None
    title: str
    cleaned_text: str
    author: str | None = None
    published_at: datetime | None = None
    extractor: ExtractorTier
    word_count: int
    token_estimate: int
    language: str | None = None
    was_truncated: bool = False


class Article(CleanedArticle):
    """A persisted article."""

    model_config = _STRICT

    id: int
    raw_text: str
    status: ArticleStatus = ArticleStatus.EXTRACTED
    error_message: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class IngestionResult(BaseModel):
    """Outcome of a URL batch. One failure never aborts the batch (FR-1.8)."""

    model_config = _STRICT

    articles: list[Article] = Field(default_factory=list)
    failures: dict[str, str] = Field(
        default_factory=dict, description="url -> user-facing failure reason"
    )
    duplicates_removed: int = 0

    @property
    def succeeded(self) -> int:
        return len(self.articles)

    @property
    def any_succeeded(self) -> bool:
        return bool(self.articles)


# ─────────────────────────────────────────────────────────────────────────────
#  LLM transport
# ─────────────────────────────────────────────────────────────────────────────
class Message(BaseModel):
    model_config = _STRICT

    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class GenerationParams(BaseModel):
    """Sampling parameters for one call. Recorded on the campaign for reproducibility."""

    model_config = _STRICT

    temperature: float = Field(0.7, ge=0.0, le=2.0)
    top_p: float = Field(0.9, ge=0.0, le=1.0)
    max_tokens: int = Field(2048, ge=64, le=8192)


class LLMResponse(BaseModel):
    """One model call plus the provenance needed to reproduce it later."""

    model_config = _STRICT

    payload: dict = Field(description="Parsed JSON from the model")
    model: str
    provider: str
    prompt_name: str
    prompt_version: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None


class HealthStatus(BaseModel):
    model_config = _STRICT

    healthy: bool
    detail: str = ""
    latency_ms: int | None = None
    checked_at: datetime = Field(default_factory=_utcnow)


# ─────────────────────────────────────────────────────────────────────────────
#  AI output — these define the prompt contract (see core.schemas)
# ─────────────────────────────────────────────────────────────────────────────
class ArticleSummary(BaseModel):
    """Stage-1 output: faithful extraction, one per article.

    ``technical_facts`` exists as a hallucination control: forcing the model to
    enumerate verifiable details separately makes a fabrication conspicuous to
    the human reviewer instead of buried in prose.
    """

    model_config = _STRICT

    headline: str = Field(max_length=80, description="Clear factual headline")
    key_points: list[str] = Field(
        min_length=3, max_length=5, description="Concrete points, each a complete sentence"
    )
    business_impact: str = Field(description="One sentence: why the reader should care")
    technical_facts: list[str] = Field(
        default_factory=list,
        description="Specific verifiable details stated in the article; empty if none",
    )
    category: Category
    relevance_score: int = Field(ge=1, le=10, description="Relevance to the chosen audience")


class NewsletterContent(BaseModel):
    """Stage-2 output: the nine fields of a newsletter.

    Length limits are enforced at *generation* time by guided decoding, so the
    model cannot emit an over-length subject line — these constraints are the
    contract, not a post-hoc check.
    """

    model_config = _STRICT

    title: str = Field(min_length=10, max_length=TITLE_MAX_LENGTH)
    summary: str = Field(min_length=50, max_length=600, description="Executive summary")
    newsletter: str = Field(min_length=100, description="The newsletter body")
    subject: str = Field(min_length=10, max_length=SUBJECT_MAX_LENGTH)
    preview_text: str = Field(min_length=10, max_length=PREVIEW_TEXT_MAX_LENGTH)
    cta: str = Field(min_length=2, max_length=CTA_MAX_LENGTH)
    keywords: list[str] = Field(min_length=KEYWORDS_MIN, max_length=KEYWORDS_MAX)
    category: Category
    tone: Tone

    @field_validator("keywords")
    @classmethod
    def _normalise_keywords(cls, value: list[str]) -> list[str]:
        """Lower-case, trim, and de-duplicate while preserving order."""
        seen: set[str] = set()
        out: list[str] = []
        for keyword in value:
            cleaned = keyword.strip().lower()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
        return out


class GenerationOptions(BaseModel):
    """User-chosen style controls, persisted with the campaign."""

    model_config = _STRICT

    tone: Tone = Tone.PROFESSIONAL
    length: LengthPreset = LengthPreset.MEDIUM
    audience: Audience = Audience.ENTERPRISE_IT


class GenerationRequest(BaseModel):
    model_config = _STRICT

    article_ids: list[int] = Field(min_length=1)
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    campaign_name: str | None = None


class NewsletterDraft(BaseModel):
    """A generated draft plus everything needed to audit how it was produced."""

    model_config = _STRICT

    campaign_id: int
    content: NewsletterContent
    options: GenerationOptions
    section_summaries: list[ArticleSummary] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    model: str = ""
    provider: str = ""
    prompt_version: str = ""
    generation_ms: int = 0


# ─────────────────────────────────────────────────────────────────────────────
#  Campaigns
# ─────────────────────────────────────────────────────────────────────────────
class ContentPatch(BaseModel):
    """A partial edit. Unset fields are left untouched.

    ``exclude_unset`` on serialisation is what distinguishes "clear this field"
    from "don't change it" — a distinction a plain dict of ``None``s loses.
    """

    model_config = _STRICT

    title: str | None = None
    summary: str | None = None
    newsletter: str | None = None
    subject: str | None = None
    preview_text: str | None = None
    cta: str | None = None
    cta_url: str | None = None
    keywords: list[str] | None = None
    category: Category | None = None
    tone: Tone | None = None
    template_id: str | None = None


class CampaignSummary(BaseModel):
    """Row shape for the History table — deliberately not the full campaign."""

    model_config = _STRICT

    id: int
    name: str
    status: CampaignStatus
    subject: str | None = None
    recipient_count: int = 0
    sent_count: int = 0
    failed_count: int = 0
    created_at: datetime
    sent_at: datetime | None = None

    @property
    def success_rate(self) -> float | None:
        attempted = self.sent_count + self.failed_count
        return None if attempted == 0 else self.sent_count / attempted


class CampaignFilter(BaseModel):
    model_config = _STRICT

    search: str | None = None
    statuses: list[CampaignStatus] | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=200)


class Page(BaseModel, Generic[T]):
    """A slice of results plus the counts the UI needs to render pagination."""

    model_config = _STRICT

    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return max(1, -(-self.total // self.page_size))  # ceiling division


# ─────────────────────────────────────────────────────────────────────────────
#  Branding, rendering and delivery
# ─────────────────────────────────────────────────────────────────────────────
class BrandConfig(BaseModel):
    model_config = _STRICT

    name: str
    primary_color: str = Field(pattern="^#[0-9A-Fa-f]{6}$")
    logo_path: str | None = None
    website: str | None = None
    address: str = Field(description="Postal address — legally required in every email")
    unsubscribe_base_url: str


class RenderedEmail(BaseModel):
    model_config = _STRICT

    html: str
    text: str = Field(description="Plain-text alternative part; never empty")
    subject: str
    preview_text: str
    template_id: str


class Recipient(BaseModel):
    model_config = _STRICT

    email: str
    name: str | None = None
    company: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class RecipientValidation(BaseModel):
    """The four counts shown before a send is enabled (US-E1).

    Suppressed addresses are reported rather than silently dropped: the operator
    should know that unsubscribed contacts were excluded.
    """

    model_config = _STRICT

    valid: list[Recipient] = Field(default_factory=list)
    invalid: dict[str, str] = Field(default_factory=dict, description="raw row -> reason")
    duplicates: list[str] = Field(default_factory=list)
    suppressed: list[str] = Field(default_factory=list)

    @property
    def sendable_count(self) -> int:
        return len(self.valid)


class EmailMessage(BaseModel):
    model_config = _STRICT

    to_email: str
    to_name: str | None = None
    subject: str
    html: str
    text: str
    headers: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class SendResult(BaseModel):
    model_config = _STRICT

    email: str
    status: SendStatus
    provider_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == SendStatus.SENT


class CampaignSendReport(BaseModel):
    """Returned by a send. Partial failure is data, not an exception."""

    model_config = _STRICT

    campaign_id: int
    attempted: int
    sent: int
    failed: int
    duration_s: float
    failures: list[SendResult] = Field(default_factory=list)

    @property
    def fully_successful(self) -> bool:
        return self.failed == 0 and self.attempted > 0
