"""Runtime-adjustable settings persistence.

Only non-secret values live here. API keys and passwords stay in ``.env`` (D-19)
— storing them in the database would put them in every backup of the file and in
any query output, for no benefit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.repository.orm_models import SettingORM


class SettingsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str, default: Any = None) -> Any:
        row = self.session.get(SettingORM, key)
        return default if row is None else row.value

    def set(self, key: str, value: Any, *, updated_by: str | None = None) -> None:
        """Upsert a setting."""
        row = self.session.get(SettingORM, key)
        if row is None:
            self.session.add(SettingORM(key=key, value=value, updated_by=updated_by))
        else:
            row.value = value
            row.updated_by = updated_by
            row.updated_at = datetime.now(UTC)
        self.session.flush()

    def all(self) -> dict[str, Any]:
        rows = self.session.execute(select(SettingORM)).scalars()
        return {row.key: row.value for row in rows}

    def delete(self, key: str) -> None:
        row = self.session.get(SettingORM, key)
        if row is not None:
            self.session.delete(row)
            self.session.flush()
