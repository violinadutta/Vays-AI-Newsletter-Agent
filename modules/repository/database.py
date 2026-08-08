"""Database engine, session management and the unit-of-work boundary.

Three SQLite-specific decisions are made here, each of which is a silent
correctness bug if omitted:

1. **``PRAGMA foreign_keys=ON``** — SQLite ignores foreign keys by default. Without
   this, ``ON DELETE CASCADE`` does nothing and deleting a campaign leaves its
   recipients and send records orphaned in the file forever.
2. **``PRAGMA journal_mode=WAL``** — the default rollback journal blocks readers
   while a write is in progress. Streamlit reruns constantly, so several threads
   read while a send loop writes; without WAL that surfaces as intermittent
   ``database is locked`` errors under exactly the load we expect.
3. **``check_same_thread=False``** — Streamlit serves each session on its own
   thread, and SQLite's default refuses a connection used across threads.

All three are applied per-connection through an event hook, so they hold for
pooled connections too, not just the first one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from config import get_logger, get_settings
from core.exceptions import PersistenceError

log = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _configure_sqlite(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """Apply the per-connection PRAGMAs SQLite needs to behave correctly."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # Wait rather than fail immediately when another thread holds the write
        # lock. A send loop writing 50 rows should not make a page render fail.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def init_database(url: str | None = None) -> Engine:
    """Create the process-wide engine and session factory, and install them.

    Called once at application startup, and by the test suite with a temporary
    database. Installing the engine globally — rather than only returning it — is
    what lets :func:`unit_of_work` work without every caller threading an engine
    through; otherwise tests silently fall back to the real configured database.

    Args:
        url: Database URL. Defaults to the configured ``DATABASE_URL``.
    """
    global _engine, _session_factory

    _engine = _build_engine(url)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    log.info("database.connected", dialect=_engine.dialect.name)
    return _engine


def get_engine(url: str | None = None) -> Engine:
    """Return the process-wide engine, initialising it on first use.

    Args:
        url: Force a specific URL, replacing any installed engine. For tests and
            for the startup call; ordinary code passes nothing.
    """
    if _engine is not None and url is None:
        return _engine
    return init_database(url)


def _build_engine(url: str | None) -> Engine:
    database_url = url or get_settings().database.url

    kwargs: dict[str, Any] = {"echo": False, "future": True}
    if _is_sqlite(database_url):
        kwargs["connect_args"] = {"check_same_thread": False}
        # A file-backed SQLite database needs its directory to exist first;
        # SQLite will not create intermediate directories and fails with an
        # unhelpful "unable to open database file".
        if ":memory:" not in database_url:
            path = Path(database_url.split("///", 1)[-1])
            path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, **kwargs)

    if _is_sqlite(database_url):
        event.listen(engine, "connect", _configure_sqlite)

    return engine


def get_session() -> Session:
    """Return a new session. The caller is responsible for closing it.

    Prefer :func:`unit_of_work`, which handles commit, rollback and close.
    """
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None  # noqa: S101 - set by get_engine above
    return _session_factory()


@contextmanager
def unit_of_work() -> Iterator[Session]:
    """Transaction boundary: commit on success, roll back on any exception.

    Services own this — repositories never commit, so a multi-step use case
    (create the campaign *and* link its articles) is atomic or does not happen.

    Example:
        >>> with unit_of_work() as session:
        ...     campaign = CampaignRepository(session).create(...)
        ...     ArticleRepository(session).link(campaign.id, article_ids)
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class UnitOfWork:
    """Class form of :func:`unit_of_work`, for callers that need the session
    across several statements without nesting a ``with`` block.

    Example:
        >>> with UnitOfWork() as uow:
        ...     CampaignRepository(uow.session).create(...)
    """

    def __init__(self) -> None:
        self.session: Session

    def __enter__(self) -> UnitOfWork:
        self.session = get_session()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create every table directly from the ORM metadata.

    Used by the test suite. **Production uses Alembic** (``alembic upgrade head``)
    so that schema changes to a database holding real campaign history are
    versioned and reversible.
    """
    from modules.repository.orm_models import Base

    target = engine or get_engine()
    try:
        Base.metadata.create_all(target)
    except Exception as exc:  # pragma: no cover - environment failure
        raise PersistenceError(
            f"could not create database schema: {exc}",
            user_message="Couldn't set up the database. Check file permissions on data/.",
        ) from exc


def reset_engine_cache() -> None:
    """Dispose of the cached engine. For tests only."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
