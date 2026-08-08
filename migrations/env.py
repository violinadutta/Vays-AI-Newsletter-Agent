"""Alembic migration environment.

Deliberately does **not** import ``config.settings``: migrations must run with
minimal configuration. Requiring ``APP_SECRET_KEY`` to be valid before you can
create a table would make the very first setup step on a new machine fail for an
unrelated reason.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from modules.repository.orm_models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Alembic compares this against the live database to autogenerate migrations.
target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the database URL from the environment, or .env, or the default."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    dotenv = Path(__file__).resolve().parents[1] / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("DATABASE_URL=") and not stripped.startswith("#"):
                return stripped.split("=", 1)[1].strip()

    return "sqlite:///./data/app.db"


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — for reviewing a change before applying it."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    if url.startswith("sqlite") and ":memory:" not in url:
        Path(url.split("///", 1)[-1]).parent.mkdir(parents=True, exist_ok=True)

    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most column properties. Batch mode rewrites the
            # table instead, so migrations that are routine on Postgres do not
            # become impossible on the database we actually ship with.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
