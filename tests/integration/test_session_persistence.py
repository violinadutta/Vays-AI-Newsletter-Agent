"""Staying signed in across a refresh — without weakening the auth gate.

Persistence is only acceptable if a stored cookie is exactly as hard to forge as
a login. Every test here is one of the ways that could go wrong: a tampered
token, an expired one, or a cookie belonging to somebody who has since been
deactivated.

The last is the one a token cannot answer by itself, and the one most likely to
be forgotten — which is why it is asserted directly.
"""

from __future__ import annotations

import time

import pytest

from core.auth import SESSION_TTL_SECONDS, sign_session_token
from core.enums import UserRole
from services.auth_service import AuthService
from ui import session as session_store

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)


@pytest.fixture
def user() -> str:
    AuthService().create_user(
        username="priya",
        display_name="Priya Sharma",
        email="priya@vays.com",
        password=PASSWORD,
        role=UserRole.ADMIN,
    )
    return "priya"


@pytest.fixture
def cookie(monkeypatch):  # noqa: ANN001, ANN201
    """Stand in for the browser, so no component or browser is needed."""
    jar: dict[str, str] = {}

    monkeypatch.setattr(session_store, "stored_token", lambda: jar.get(session_store.COOKIE_NAME))
    monkeypatch.setattr(session_store, "forget", lambda: jar.pop(session_store.COOKIE_NAME, None))

    def put(token: str) -> None:
        jar[session_store.COOKIE_NAME] = token

    put.jar = jar  # type: ignore[attr-defined]
    return put


def secret() -> str:
    from config import get_settings

    return get_settings().app.secret_key.get_secret_value()


class TestRestoringASession:
    def test_a_valid_token_signs_the_user_back_in(self, user: str, cookie) -> None:  # noqa: ANN001
        """The whole feature: refresh should not mean log in again."""
        cookie(sign_session_token(user, str(UserRole.ADMIN), secret()))

        restored = session_store.restore()

        assert restored is not None
        assert restored.username == user
        assert restored.role is UserRole.ADMIN

    def test_no_cookie_means_no_session(self, user: str, cookie) -> None:  # noqa: ANN001, ARG002
        assert session_store.restore() is None

    def test_the_display_name_comes_from_the_database(self, user: str, cookie) -> None:  # noqa: ANN001
        """Not from the token: a renamed user should show their current name."""
        cookie(sign_session_token(user, str(UserRole.ADMIN), secret()))

        assert session_store.restore().display_name == "Priya Sharma"


class TestTheCookieIsNotATrustBoundary:
    def test_a_tampered_token_is_refused(self, user: str, cookie) -> None:  # noqa: ANN001
        """Editing the cookie must be exactly as useless as guessing a password."""
        token = sign_session_token(user, str(UserRole.ADMIN), secret())
        cookie(token[:-4] + "AAAA")

        assert session_store.restore() is None

    def test_a_token_signed_with_another_key_is_refused(self, user: str, cookie) -> None:  # noqa: ANN001
        """Someone who can write cookies but does not have APP_SECRET_KEY gets
        nowhere — which is the entire security property."""
        cookie(sign_session_token(user, str(UserRole.ADMIN), "a" * 48))

        assert session_store.restore() is None

    def test_an_expired_token_is_refused(self, user: str, cookie) -> None:  # noqa: ANN001
        issued_long_ago = time.time() - SESSION_TTL_SECONDS - 60
        cookie(sign_session_token(user, str(UserRole.ADMIN), secret(), now=issued_long_ago))

        assert session_store.restore() is None

    def test_garbage_is_refused_without_raising(self, user: str, cookie) -> None:  # noqa: ANN001, ARG002
        for value in ("", "not-a-token", "a.b.c", "x" * 500):
            cookie(value)
            assert session_store.restore() is None

    def test_a_role_claimed_in_the_token_does_not_grant_it(self, user: str, cookie) -> None:  # noqa: ANN001
        """The token is signed, so its role cannot be edited — but the role that
        is *honoured* comes from the database regardless, so a stale token
        cannot preserve a privilege that has since been revoked."""
        AuthService().create_user(
            username="ed",
            display_name="Ed",
            email="ed@vays.com",
            password=PASSWORD,
            role=UserRole.EDITOR,
        )
        cookie(sign_session_token("ed", str(UserRole.ADMIN), secret()))

        assert session_store.restore().role is UserRole.EDITOR


class TestDeactivationEndsTheSession:
    def test_a_deactivated_user_cannot_restore(self, user: str, cookie) -> None:  # noqa: ANN001
        """The check a token cannot make for itself. Without it, deactivating
        somebody would leave them signed in for up to twelve more hours."""
        cookie(sign_session_token(user, str(UserRole.ADMIN), secret()))
        AuthService().set_active(user, active=False)

        assert session_store.restore() is None

    def test_a_deleted_user_cannot_restore(self, user: str, cookie) -> None:  # noqa: ANN001
        from modules.repository.database import unit_of_work
        from modules.repository.orm_models import UserORM

        cookie(sign_session_token(user, str(UserRole.ADMIN), secret()))
        with unit_of_work() as session:
            session.delete(session.get(UserORM, user))

        assert session_store.restore() is None

    def test_a_refused_cookie_is_cleared(self, user: str, cookie) -> None:  # noqa: ANN001
        """A cookie that will never work again should stop being presented."""
        cookie(sign_session_token(user, str(UserRole.ADMIN), "wrong" * 10))

        session_store.restore()

        assert session_store.stored_token() is None
