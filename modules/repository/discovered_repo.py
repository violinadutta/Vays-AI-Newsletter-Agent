"""Persistence for automatically discovered posts — the agent's duplicate guard.

**This is the single place that decides whether a post is new.** Discovery
sources only report what exists on the site; without this record every scheduled
run would re-process the same articles and mail customers about them again.

The guarantee rests on a unique constraint rather than on a read-then-write
check. Two overlapping runs both seeing "not present" and both inserting is a
classic race, and no amount of application-level care prevents it — the database
does.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.enums import RETRYABLE_POST_STATES, PostState
from core.models import DiscoveredPost
from modules.repository.orm_models import DiscoveredPostORM


def dedupe_key(post: DiscoveredPost) -> str:
    """The identity under which a post is remembered.

    Prefers the source's own identifier — a WordPress post ID is stable across
    an edit that changes the slug, and matching on URL alone would treat a
    corrected headline as a brand-new article and send a second newsletter about
    it.

    Falls back to a hash of the normalised URL when the source has no ID (a bare
    RSS feed). Hashed rather than stored raw because a URL can exceed a
    practical index width.
    """
    if post.external_id:
        return f"{post.source}:{post.external_id}"
    return f"url:{hashlib.sha256(normalise_url(post.url).encode()).hexdigest()}"


def normalise_url(url: str) -> str:
    """Strip the parts of a URL that do not change which article it points at.

    Query strings and fragments on a blog post are almost always tracking
    parameters (``?utm_source=…``), and treating two such URLs as different
    posts would re-process the same article.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


class DiscoveredPostRepository:
    """Reads and writes the agent's record of what it has already seen."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ── the duplicate guard ──────────────────────────────────────────────────
    def record_new(self, posts: list[DiscoveredPost]) -> list[DiscoveredPostORM]:
        """Insert the posts not seen before. Returns only the newly created rows.

        A post already present is skipped silently — that is the normal case on
        every run after the first, not a condition worth logging as unusual.

        Each insert is flushed inside a savepoint so that a concurrent run
        winning the race on one post does not roll back the others. Without the
        savepoint, one ``IntegrityError`` would poison the whole session.
        """
        created: list[DiscoveredPostORM] = []

        for post in posts:
            key = dedupe_key(post)
            if self.get_by_key(key) is not None:
                continue

            row = DiscoveredPostORM(
                dedupe_key=key,
                external_id=post.external_id,
                url=post.url,
                title=post.title,
                author=post.author,
                categories=post.categories or None,
                published_at=post.published_at,
                source=post.source,
                state=PostState.DISCOVERED,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(row)
                    self.session.flush()
            except IntegrityError:
                # Another run inserted it between the check and the flush. That
                # is exactly what the unique constraint is for, and the correct
                # response is to leave it to whoever won.
                continue
            created.append(row)

        return created

    def is_known(self, post: DiscoveredPost) -> bool:
        """Whether this post has been seen before, under either identity form."""
        return self.get_by_key(dedupe_key(post)) is not None

    def get_by_key(self, key: str) -> DiscoveredPostORM | None:
        return self.session.execute(
            select(DiscoveredPostORM).where(DiscoveredPostORM.dedupe_key == key)
        ).scalar_one_or_none()

    def get(self, post_id: int) -> DiscoveredPostORM | None:
        return self.session.get(DiscoveredPostORM, post_id)

    # ── work queue ───────────────────────────────────────────────────────────
    def pending(self, limit: int = 10, max_attempts: int = 3) -> list[DiscoveredPostORM]:
        """Posts the agent should try to move forward, oldest first.

        Oldest first so a backlog drains in publication order rather than
        newest-wins, which would leave old posts permanently starved.

        ``max_attempts`` bounds retries: a post on a site that blocks extraction
        must eventually stop consuming a slot on every run.
        """
        return list(
            self.session.execute(
                select(DiscoveredPostORM)
                .where(
                    DiscoveredPostORM.state.in_(tuple(RETRYABLE_POST_STATES)),
                    DiscoveredPostORM.attempts < max_attempts,
                )
                .order_by(DiscoveredPostORM.published_at.asc().nulls_last())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    # ── state ────────────────────────────────────────────────────────────────
    def mark(
        self,
        post_id: int,
        state: PostState,
        *,
        article_id: int | None = None,
        campaign_id: int | None = None,
        error: str | None = None,
    ) -> None:
        """Advance a post's state, optionally linking what it produced.

        ``attempts`` is incremented only on failure. Counting successful steps
        would exhaust the retry budget during normal progress.
        """
        row = self.session.get(DiscoveredPostORM, post_id)
        if row is None:
            return

        row.state = state
        row.updated_at = datetime.now(UTC)
        if article_id is not None:
            row.article_id = article_id
        if campaign_id is not None:
            row.campaign_id = campaign_id
        if state is PostState.FAILED:
            row.attempts += 1
            row.last_error = (error or "")[:2000] or None
        elif error is None:
            row.last_error = None
        self.session.flush()

    # ── reporting ────────────────────────────────────────────────────────────
    def state_counts(self) -> dict[str, int]:
        """Rows per state, for the dashboard panel."""
        rows = self.session.execute(
            select(DiscoveredPostORM.state, func.count()).group_by(DiscoveredPostORM.state)
        ).all()
        return {str(state): int(count) for state, count in rows}

    def recent(self, limit: int = 10) -> list[DiscoveredPostORM]:
        return list(
            self.session.execute(
                select(DiscoveredPostORM)
                .order_by(DiscoveredPostORM.discovered_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

    def count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(DiscoveredPostORM)) or 0)
