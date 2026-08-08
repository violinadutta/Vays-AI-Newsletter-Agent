"""Persistence adapter — SQLAlchemy ORM models and repositories.

Repositories expose intention-revealing methods (``get_recent``, ``mark_sent``)
rather than a generic ``query()``, so SQLAlchemy never leaks into the service
layer. That is what keeps the Postgres migration path open: there is no raw SQL
anywhere, and no caller depends on the ORM's API.

**Transactions are owned by services, not repositories.** A repository that
commits on its own makes multi-step use cases impossible to make atomic — see
:func:`modules.repository.database.unit_of_work`.
"""

from modules.repository.database import (
    UnitOfWork,
    create_all,
    get_engine,
    get_session,
    init_database,
    reset_engine_cache,
    unit_of_work,
)

__all__ = [
    "UnitOfWork",
    "create_all",
    "get_engine",
    "get_session",
    "init_database",
    "reset_engine_cache",
    "unit_of_work",
]
