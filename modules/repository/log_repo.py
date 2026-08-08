"""Application-log persistence — powers the in-app Logs page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select
from sqlalchemy.orm import Session

from config.constants import LOG_RETENTION_DAYS
from modules.repository.orm_models import AppLogORM


class LogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def write(
        self,
        *,
        level: str,
        logger: str,
        event: str,
        message: str | None = None,
        campaign_id: int | None = None,
        correlation_id: str | None = None,
        context: dict[str, Any] | None = None,
        exception: str | None = None,
    ) -> None:
        self.session.add(
            AppLogORM(
                level=level.upper(),
                logger=logger,
                event=event,
                message=message,
                campaign_id=campaign_id,
                correlation_id=correlation_id,
                context=context,
                exception=exception,
            )
        )

    def search(
        self,
        *,
        levels: list[str] | None = None,
        search: str | None = None,
        campaign_id: int | None = None,
        correlation_id: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AppLogORM]:
        conditions: list[ColumnElement[bool]] = []
        if levels:
            conditions.append(AppLogORM.level.in_([lvl.upper() for lvl in levels]))
        if search:
            pattern = f"%{search.lower()}%"
            conditions.append(
                or_(
                    func.lower(AppLogORM.event).like(pattern),
                    func.lower(AppLogORM.message).like(pattern),
                )
            )
        if campaign_id is not None:
            conditions.append(AppLogORM.campaign_id == campaign_id)
        if correlation_id:
            conditions.append(AppLogORM.correlation_id == correlation_id)
        if since is not None:
            conditions.append(AppLogORM.ts >= since)

        return list(
            self.session.execute(
                select(AppLogORM)
                .where(*conditions)
                .order_by(AppLogORM.ts.desc(), AppLogORM.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )

    def count(self) -> int:
        return self.session.execute(select(func.count()).select_from(AppLogORM)).scalar_one()

    def prune(self, retention_days: int = LOG_RETENTION_DAYS) -> int:
        """Delete log rows older than the retention window.

        Called at startup. Unbounded log growth in the same SQLite file that
        holds campaign history would eventually slow every query in the app, not
        just the Logs page.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        result = cast(
            "CursorResult[Any]",
            self.session.execute(delete(AppLogORM).where(AppLogORM.ts < cutoff)),
        )
        return int(result.rowcount or 0)
