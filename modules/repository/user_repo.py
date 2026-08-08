"""User persistence.

This layer stores and retrieves ``password_hash`` but never hashes or verifies —
that belongs to ``core.auth`` (M1.4). Keeping the crypto in one auditable place
matters more here than convenience.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.enums import UserRole
from modules.repository.orm_models import UserORM


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        username: str,
        display_name: str,
        email: str,
        password_hash: str,
        role: UserRole = UserRole.EDITOR,
    ) -> UserORM:
        user = UserORM(
            username=username.strip().lower(),
            display_name=display_name,
            email=email.strip().lower(),
            password_hash=password_hash,
            role=role,
        )
        self.session.add(user)
        self.session.flush()
        return user

    def get(self, username: str) -> UserORM | None:
        return self.session.get(UserORM, username.strip().lower())

    def get_active(self, username: str) -> UserORM | None:
        """Fetch a user only if their account is enabled.

        Deactivation must take effect immediately, so the authentication path
        uses this rather than :meth:`get` — a disabled account should fail login
        the same way a wrong password does.
        """
        user = self.get(username)
        return user if user is not None and user.is_active else None

    def list_all(self) -> list[UserORM]:
        return list(self.session.execute(select(UserORM).order_by(UserORM.username)).scalars())

    def set_password(self, username: str, password_hash: str) -> None:
        user = self.get(username)
        if user is not None:
            user.password_hash = password_hash
            self.session.flush()

    def set_active(self, username: str, *, active: bool) -> None:
        user = self.get(username)
        if user is not None:
            user.is_active = active
            self.session.flush()

    def record_login(self, username: str) -> None:
        user = self.get(username)
        if user is not None:
            user.last_login_at = datetime.now(UTC)
            self.session.flush()

    def count(self) -> int:
        return self.session.execute(select(func.count()).select_from(UserORM)).scalar_one()
