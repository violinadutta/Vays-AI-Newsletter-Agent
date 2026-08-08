"""Article persistence."""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.enums import ArticleStatus, ExtractorTier
from core.models import CleanedArticle
from modules.repository.orm_models import ArticleORM


def url_fingerprint(url: str) -> str:
    """Stable hash of a URL, used for de-duplication.

    Hashed rather than indexed directly because URLs are long, and a 64-character
    fixed-width index is far cheaper than one over a 2 KB column.
    """
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()


class ArticleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, article: CleanedArticle, raw_text: str) -> ArticleORM:
        row = ArticleORM(
            url=article.url,
            url_hash=url_fingerprint(article.url) if article.url else None,
            title=article.title,
            author=article.author,
            published_at=article.published_at,
            raw_text=raw_text,
            cleaned_text=article.cleaned_text,
            word_count=article.word_count,
            token_estimate=article.token_estimate,
            language=article.language,
            was_truncated=article.was_truncated,
            extractor_used=article.extractor,
            status=ArticleStatus.EXTRACTED,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_failure(self, url: str, reason: str) -> ArticleORM:
        """Persist a failed extraction.

        Failures are stored, not discarded: the Logs page and the per-tier
        success-rate metric both need them, and a user asking "why didn't that
        one work?" deserves an answer.
        """
        row = ArticleORM(
            url=url,
            url_hash=url_fingerprint(url),
            title="(extraction failed)",
            raw_text="",
            cleaned_text="",
            extractor_used=ExtractorTier.FALLBACK,
            status=ArticleStatus.FAILED,
            error_message=reason,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(self, article_id: int) -> ArticleORM | None:
        return self.session.get(ArticleORM, article_id)

    def get_many(self, article_ids: list[int]) -> list[ArticleORM]:
        """Fetch several articles, preserving the caller's ordering.

        Order matters: it is the order the stories appear in the newsletter, and
        SQL makes no ordering guarantee for an ``IN`` clause.
        """
        rows = self.session.execute(
            select(ArticleORM).where(ArticleORM.id.in_(article_ids))
        ).scalars()
        by_id = {row.id: row for row in rows}
        return [by_id[i] for i in article_ids if i in by_id]

    def find_by_url(self, url: str) -> ArticleORM | None:
        return (
            self.session.execute(
                select(ArticleORM)
                .where(ArticleORM.url_hash == url_fingerprint(url))
                .order_by(ArticleORM.created_at.desc())
            )
            .scalars()
            .first()
        )

    def count(self) -> int:
        from sqlalchemy import func

        return self.session.execute(select(func.count()).select_from(ArticleORM)).scalar_one()
