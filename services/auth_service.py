"""Login flow: user lookup, lockout, and session issuance.

Kept separate from :mod:`core.auth` so the cryptography stays pure and testable,
while the parts that need a database and a clock live here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from config import get_logger, get_settings
from core.auth import hash_password, sign_session_token, validate_password_strength, verify_password
from core.enums import SENDING_ROLES, UserRole
from core.exceptions import ValidationError
from modules.repository.database import unit_of_work
from modules.repository.user_repo import UserRepository

log = get_logger(__name__)

#: Failed attempts before a username is locked, and for how long.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


@dataclass
class AuthenticatedUser:
    """The identity the UI holds for the duration of a session."""

    username: str
    display_name: str
    role: UserRole
    token: str

    @property
    def can_send(self) -> bool:
        """Whether this user may dispatch a campaign to real recipients."""
        return self.role in SENDING_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0
    history: list[float] = field(default_factory=list)


class AuthService:
    """Authentication use cases.

    Lockout state is in-process, which is correct for the single-process
    deployment this ships as. If the app is ever run behind more than one worker,
    this moves to the database — noted here rather than discovered later.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, _Attempts] = {}

    # ── login ────────────────────────────────────────────────────────────────
    def authenticate(self, username: str, password: str) -> AuthenticatedUser:
        """Verify credentials and issue a session token.

        Raises:
            ValidationError: If the account is locked, or the credentials are
                wrong. The message is identical for "no such user", "wrong
                password" and "deactivated account" — telling an attacker which
                usernames exist is free reconnaissance.
        """
        key = username.strip().lower()
        self._raise_if_locked(key)

        with unit_of_work() as session:
            repo = UserRepository(session)
            user = repo.get_active(key)

            if user is None or not verify_password(password, user.password_hash):
                self._record_failure(key)
                log.warning("auth.failed", username=key, attempts=self._attempts[key].count)
                raise ValidationError(
                    f"authentication failed for {key!r}",
                    user_message="Incorrect username or password.",
                )

            repo.record_login(key)
            identity = AuthenticatedUser(
                username=user.username,
                display_name=user.display_name,
                role=UserRole(user.role),
                token=sign_session_token(
                    user.username,
                    str(user.role),
                    get_settings().app.secret_key.get_secret_value(),
                ),
            )

        self._attempts.pop(key, None)
        log.info("auth.success", username=key, role=str(identity.role))
        return identity

    def _raise_if_locked(self, key: str) -> None:
        record = self._attempts.get(key)
        if record is None or record.locked_until <= time.time():
            return
        remaining = int(record.locked_until - time.time()) + 1
        raise ValidationError(
            f"{key!r} is locked out for {remaining}s",
            user_message=(
                f"Too many failed attempts. Please wait {remaining} seconds and try again."
            ),
            context={"username": key, "remaining_s": remaining},
        )

    def _record_failure(self, key: str) -> None:
        record = self._attempts.setdefault(key, _Attempts())
        record.count += 1
        record.history.append(time.time())
        if record.count >= MAX_ATTEMPTS:
            record.locked_until = time.time() + LOCKOUT_SECONDS
            record.count = 0
            log.warning("auth.locked_out", username=key, seconds=LOCKOUT_SECONDS)

    # ── user management ──────────────────────────────────────────────────────
    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        email: str,
        password: str,
        role: UserRole = UserRole.EDITOR,
    ) -> str:
        """Create a user. Returns the created username.

        Raises:
            ValidationError: If the password is too weak or the username is taken.
        """
        validate_password_strength(password)
        key = username.strip().lower()
        if not key:
            raise ValidationError("empty username", user_message="Username is required.")

        with unit_of_work() as session:
            repo = UserRepository(session)
            if repo.get(key) is not None:
                raise ValidationError(
                    f"username {key!r} already exists",
                    user_message=f"The username '{key}' is already taken.",
                )
            repo.create(
                username=key,
                display_name=display_name or key,
                email=email,
                password_hash=hash_password(password),
                role=role,
            )

        log.info("auth.user_created", username=key, role=str(role))
        return key

    def set_password(self, username: str, password: str) -> None:
        validate_password_strength(password)
        with unit_of_work() as session:
            UserRepository(session).set_password(username, hash_password(password))
        # Clear any lockout: an admin resetting the password should not leave the
        # user locked out by the failed attempts that prompted the reset.
        self._attempts.pop(username.strip().lower(), None)
        log.info("auth.password_changed", username=username.strip().lower())

    def set_active(self, username: str, *, active: bool) -> None:
        with unit_of_work() as session:
            UserRepository(session).set_active(username, active=active)
        log.info("auth.user_activation_changed", username=username, active=active)

    def has_any_users(self) -> bool:
        """Whether any account exists — drives the first-run setup prompt."""
        with unit_of_work() as session:
            return UserRepository(session).count() > 0
