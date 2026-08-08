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
