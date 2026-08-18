"""SQLAlchemy ORM models — the nine tables from TRD §6.

Every column type here is Postgres-compatible, and there is no SQLite-specific
SQL anywhere, so migrating is a ``DATABASE_URL`` change plus ``alembic upgrade
head``. ``JSON`` is SQLAlchemy's generic type, which maps to ``JSONB`` on
Postgres automatically.

Two schema decisions worth defending, because both look redundant at first:

**Both ``ai_*`` and final content columns.** The product needs exactly one
comparison — what the AI wrote versus what shipped — to compute the edit-ratio
quality metric and show the diff. A general revision-history table would be more
flexible and less useful; this is the smallest design that answers the actual
question.

**Recipients are snapshotted per campaign, not joined from a shared contacts
table.** "Who received campaign 42" must stay true even after the master list
changes. A join would let history silently mutate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.enums import (
    ArticleStatus,
    CampaignStatus,
    ExtractorTier,
    PostState,
    SendStatus,
    SuppressionReason,
    UserRole,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every table."""


# ─────────────────────────────────────────────────────────────────────────────
class ArticleORM(Base):
    """Extracted source content."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str | None] = mapped_column(String(2048))
    #: sha256 of the normalised URL — indexed for de-duplication lookups. The raw
    #: URL is too long to index efficiently and varies by query-string order.
    url_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    cleaned_text: Mapped[str] = mapped_column(Text, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String(8))
    was_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    extractor_used: Mapped[ExtractorTier] = mapped_column(String(32), nullable=False)
    extraction_ms: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[ArticleStatus] = mapped_column(
        String(16), nullable=False, default=ArticleStatus.EXTRACTED
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )


# ─────────────────────────────────────────────────────────────────────────────
class CampaignORM(Base):
    """The central aggregate."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        String(24), nullable=False, default=CampaignStatus.DRAFT, index=True
    )

    # ── AI original: an immutable audit record, never overwritten by edits ──
    ai_title: Mapped[str | None] = mapped_column(Text)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_newsletter: Mapped[str | None] = mapped_column(Text)
    ai_subject: Mapped[str | None] = mapped_column(Text)
    ai_preview_text: Mapped[str | None] = mapped_column(Text)
    ai_cta: Mapped[str | None] = mapped_column(Text)
    ai_keywords: Mapped[list[str] | None] = mapped_column(JSON)
    ai_category: Mapped[str | None] = mapped_column(String(64))
    ai_tone: Mapped[str | None] = mapped_column(String(32))

    # ── Final content: what actually ships. Starts as a copy of ai_*. ──
    title: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    newsletter: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    preview_text: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(Text)
    cta_url: Mapped[str | None] = mapped_column(String(2048))
    keywords: Mapped[list[str] | None] = mapped_column(JSON)
    category: Mapped[str | None] = mapped_column(String(64))
    tone: Mapped[str | None] = mapped_column(String(32))

    # ── Provenance: what produced this, so any output is reproducible ──
    model_name: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(32))
    generation_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    generation_ms: Mapped[int] = mapped_column(Integer, default=0)
    regeneration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Rendering ──
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, default="modern")
    rendered_html: Mapped[str | None] = mapped_column(Text)
    rendered_text: Mapped[str | None] = mapped_column(Text)

    # ── Delivery rollup, denormalised so History lists without N+1 queries ──
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When a human approved this campaign. **The send gate is computed from
    #: this**, so it must be immutable once set — ``updated_at`` cannot serve the
    #: purpose because it moves on every write, which would silently slide the
    #: scheduled send forward every time anything touched the row.
    #:
    #: **Always written as UTC.** SQLite stores no timezone, so an aware value in
    #: any other zone comes back as its wall-clock reading labelled UTC — 08:00
    #: IST would return as 08:00 UTC, five and a half hours adrift, and the
    #: campaign would go out on the wrong day. Convert before assigning.
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(64))

    articles: Mapped[list[CampaignArticleORM]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="CampaignArticleORM.position",
    )
    recipients: Mapped[list[RecipientORM]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class CampaignArticleORM(Base):
    """Which articles fed which campaign, and the stage-1 summary each produced."""

    __tablename__ = "campaign_articles"

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    #: RESTRICT, not CASCADE: an article may feed several campaigns, and deleting
    #: it would silently strip a sent campaign of its provenance.
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="RESTRICT"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    section_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    campaign: Mapped[CampaignORM] = relationship(back_populates="articles")
    article: Mapped[ArticleORM] = relationship()


# ─────────────────────────────────────────────────────────────────────────────
class RecipientORM(Base):
    """A per-campaign snapshot of one recipient."""

    __tablename__ = "recipients"
    __table_args__ = (
        # De-duplication enforced by the database, not only by application code —
        # a retry or a double upload must not create a second row.
        UniqueConstraint("campaign_id", "email", name="uq_recipient_per_campaign"),
        Index("ix_recipients_campaign", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    company: Mapped[str | None] = mapped_column(String(255))
    extra: Mapped[dict[str, str] | None] = mapped_column(JSON)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    campaign: Mapped[CampaignORM] = relationship(back_populates="recipients")


class SendRecordORM(Base):
    """Per-recipient delivery outcome."""

    __tablename__ = "send_records"
    __table_args__ = (Index("ix_send_campaign_status", "campaign_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SendStatus] = mapped_column(
        String(16), nullable=False, default=SendStatus.QUEUED
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    batch_number: Mapped[int | None] = mapped_column(Integer)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SuppressionORM(Base):
    """Global do-not-send list. Checked before every send, without exception."""

    __tablename__ = "suppressions"

    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    reason: Mapped[SuppressionReason] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# ─────────────────────────────────────────────────────────────────────────────
class AppLogORM(Base):
    """Structured logs, queryable by the in-app Logs page.

    Separate from ``logs/app.jsonl``: the file is the full forensic trail at
    DEBUG, this table is the INFO+ subset a non-technical user can search.
    """

    __tablename__ = "app_logs"
    __table_args__ = (
        Index("ix_logs_ts", "ts"),
        Index("ix_logs_level", "level"),
        Index("ix_logs_corr", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    logger: Mapped[str] = mapped_column(String(128), nullable=False)
    event: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    campaign_id: Mapped[int | None] = mapped_column(Integer)
    correlation_id: Mapped[str | None] = mapped_column(String(32))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    exception: Mapped[str | None] = mapped_column(Text)


class SettingORM(Base):
    """Runtime-adjustable non-secret settings.

    Secrets never land here — they live only in ``.env`` (D-19). ``is_secret``
    marks a key whose *value* is a reference, so the UI knows to mask it.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(64))


class UserORM(Base):
    """Application user. Passwords are bcrypt hashes — never reversible."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(String(16), nullable=False, default=UserRole.EDITOR)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# ─────────────────────────────────────────────────────────────────────────────
class DiscoveredPostORM(Base):
    """A blog post found by automatic discovery — the agent's memory.

    **This table is what makes the agent idempotent.** Without it, every
    scheduled run would re-process the same posts and mail customers about the
    same article repeatedly. Every discovery run consults it before doing any
    work, and nothing else in the pipeline is responsible for that check.

    ``dedupe_key`` is the WordPress post ID where the source provides one, and
    the normalised URL where it does not (a bare RSS feed). One column with a
    unique constraint rather than two nullable ones, because "unique across
    either of two columns" is not something a database can enforce — and an
    unenforceable invariant is one that eventually breaks.
    """

    __tablename__ = "discovered_posts"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Stable identity. Unique — the constraint, not application logic, is what
    #: guarantees a post is processed once even if two runs overlap.
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    external_id: Mapped[str | None] = mapped_column(String(64))

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255))
    categories: Mapped[list[str] | None] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

    state: Mapped[PostState] = mapped_column(
        String(16), nullable=False, default=PostState.DISCOVERED, index=True
    )
    #: Bounded retries. A post whose site permanently blocks extraction must stop
    #: being attempted every few hours forever.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    #: Set once generation succeeds. From that point the campaign's own status
    #: governs, and this row is only the provenance link back to the source post.
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"))
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


# ─────────────────────────────────────────────────────────────────────────────
class ApprovalTokenORM(Base):
    """A single-use, expiring capability to review one campaign.

    **Only the hash is stored.** The token itself exists in exactly one place —
    the link in the approval email — and is never written down here, for the same
    reason passwords are not: a database read, a backup, or a screenshot of a
    query must not yield a working credential.

    The token does not by itself approve anything. It opens the review page,
    which still requires a login and an approver role. It is a pointer with an
    expiry, not an authorisation.
    """

    __tablename__ = "approval_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: sha256 of the raw token. Unique so a collision cannot silently grant
    #: access to the wrong campaign.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set when a decision is recorded. Non-null means spent — a replayed link
    #: is refused even before the expiry is checked.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# ─────────────────────────────────────────────────────────────────────────────
class SubscriberORM(Base):
    """The master mailing list — who receives newsletters, standing.

    **Distinct from ``recipients``, deliberately.** That table is a per-campaign
    *snapshot*: "who received campaign 42" must stay true even after this list
    changes. This one is the living list that snapshot is taken from. Merging
    them would let history mutate every time someone is added or removed.

    Also distinct from ``suppressions``: that is a legal do-not-send record and
    outranks this list. Someone unsubscribed stays unsubscribed even if they are
    active here, which is checked at send time by the existing validator.

    ``email`` is the primary key, so an import cannot create a duplicate no
    matter how many times the same CSV is uploaded. That is the append-safety
    property the whole feature rests on.
    """

    __tablename__ = "subscribers"

    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    company: Mapped[str | None] = mapped_column(String(255))

    #: Removing someone deactivates rather than deletes. A hard delete would let
    #: the next CSV import silently resurrect them, which is exactly the mistake
    #: the suppression list exists to prevent one level up.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    #: How they arrived — "csv:list.csv", "manual", "bootstrap". Answers "why is
    #: this person on the list?" months later.
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    added_by: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
