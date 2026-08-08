"""Shared pytest fixtures.

The central concern here is **isolation from the developer's own machine**. Two
things would otherwise leak in and make tests pass locally but fail in CI (or the
reverse, which is worse because it hides a real bug):

1. The project's ``.env`` file. Solved by building settings with ``env_file=None``
   so only explicitly-set environment variables are read.
2. Environment variables left over from a previous test or from the shell.
   Solved by the autouse ``_isolated_env`` fixture, which strips every variable
   this application recognises before each test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from config.logging_config import reset_logging
from config.settings import Settings, build_settings, reset_settings_cache

#: Prefixes of every environment variable this application reads.
_APP_ENV_PREFIXES = (
    "APP_",
    "LOG_",
    "LLM_",
    "EMAIL_",
    "BREVO_",
    "SMTP_",
    "SCRAPER_",
    "DATABASE_",
    "BRAND_",
    "UNSUBSCRIBE_",
)

#: The smallest environment that produces valid settings.
MINIMAL_ENV: dict[str, str] = {
    "APP_SECRET_KEY": "t" * 48,
    "APP_ENV": "local",
    "LLM_PROVIDER": "mock",
    "EMAIL_PROVIDER": "console",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every application environment variable and clear cached state.

    Autouse: isolation should be the default, not something each test remembers
    to opt into.
    """
    for key in list(os.environ):
        if key.startswith(_APP_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    reset_settings_cache()
    reset_logging()
    yield
    reset_settings_cache()
    reset_logging()


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - pytest fixture factory
    """Return a helper that sets environment variables for one test."""

    def _set(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)

    return _set


@pytest.fixture
def minimal_settings(set_env) -> Settings:  # noqa: ANN001 - fixture injection
    """A valid :class:`Settings` built from the smallest working environment."""
    set_env(**MINIMAL_ENV)
    return build_settings(env_file=None)


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    """A session against a throwaway file-backed SQLite database.

    A file rather than ``:memory:`` on purpose: the foreign-key and WAL pragmas
    are applied per connection, and an in-memory database gets a fresh empty
    schema on every new connection — which would quietly make the cascade-delete
    tests pass for the wrong reason.
    """
    from modules.repository.database import get_session, init_database, reset_engine_cache
    from modules.repository.orm_models import Base

    reset_engine_cache()
    # init_database installs the engine globally, so unit_of_work() inside the
    # code under test uses this temporary database rather than the real one.
    engine = init_database(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = get_session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        reset_engine_cache()


@pytest.fixture
def log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the rotating file handler to a temp path.

    Tests must never append to the real ``logs/app.jsonl`` — it would pollute the
    developer's log history and make assertions depend on prior runs.
    """
    target = tmp_path / "app.jsonl"
    monkeypatch.setattr("config.logging_config.LOG_FILE", target)
    return target
