"""Tests for the login flow: lookup, lockout, and role permissions."""

from __future__ import annotations

import time

import pytest
from sqlalchemy.orm import Session

from core.enums import UserRole
from core.exceptions import ValidationError
from services.auth_service import LOCKOUT_SECONDS, MAX_ATTEMPTS, AuthService

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"


@pytest.fixture
def auth(db_session: Session, set_env) -> AuthService:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)
    service = AuthService()
    service.create_user(
        username="priya",
        display_name="Priya Sharma",
        email="priya@vays.com",
        password=PASSWORD,
        role=UserRole.EDITOR,
    )
    return service


class TestAuthentication:
    def test_valid_credentials_return_an_identity(self, auth: AuthService) -> None:
        user = auth.authenticate("priya", PASSWORD)

        assert user.username == "priya"
        assert user.role == UserRole.EDITOR
        assert user.token

    def test_username_is_case_insensitive(self, auth: AuthService) -> None:
        assert auth.authenticate("  PRIYA  ", PASSWORD).username == "priya"

    def test_wrong_password_is_rejected(self, auth: AuthService) -> None:
        with pytest.raises(ValidationError):
            auth.authenticate("priya", "wrong password entirely")

    def test_unknown_and_wrong_password_give_identical_messages(self, auth: AuthService) -> None:
        """Telling an attacker which usernames exist is free reconnaissance."""
        with pytest.raises(ValidationError) as unknown:
            auth.authenticate("nobody", PASSWORD)
        with pytest.raises(ValidationError) as wrong:
            auth.authenticate("priya", "wrong password entirely")

        assert unknown.value.user_message == wrong.value.user_message

    def test_a_deactivated_account_cannot_log_in(self, auth: AuthService) -> None:
        auth.set_active("priya", active=False)

        with pytest.raises(ValidationError):
            auth.authenticate("priya", PASSWORD)


class TestLockout:
    def test_locks_out_after_repeated_failures(self, auth: AuthService) -> None:
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(ValidationError):
                auth.authenticate("priya", "wrong")

        with pytest.raises(ValidationError) as exc_info:
            auth.authenticate("priya", PASSWORD)  # correct password, still locked

        assert "Too many failed attempts" in exc_info.value.user_message

    def test_lockout_reports_the_remaining_time(self, auth: AuthService) -> None:
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(ValidationError):
                auth.authenticate("priya", "wrong")

        with pytest.raises(ValidationError) as exc_info:
            auth.authenticate("priya", PASSWORD)

        assert 0 < exc_info.value.context["remaining_s"] <= LOCKOUT_SECONDS + 1

    def test_a_successful_login_clears_the_failure_count(self, auth: AuthService) -> None:
        for _ in range(MAX_ATTEMPTS - 1):
            with pytest.raises(ValidationError):
                auth.authenticate("priya", "wrong")

        auth.authenticate("priya", PASSWORD)

        # The counter reset, so the next failure starts from one rather than
        # tipping straight into a lockout.
        with pytest.raises(ValidationError):
            auth.authenticate("priya", "wrong")
        auth.authenticate("priya", PASSWORD)

    def test_lockout_expires(self, auth: AuthService, monkeypatch: pytest.MonkeyPatch) -> None:
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(ValidationError):
                auth.authenticate("priya", "wrong")

        future = time.time() + LOCKOUT_SECONDS + 1
        monkeypatch.setattr(time, "time", lambda: future)

        assert auth.authenticate("priya", PASSWORD).username == "priya"

    def test_a_password_reset_clears_the_lockout(self, auth: AuthService) -> None:
        """An admin resetting the password should not leave the user locked out
        by the very failures that prompted the reset."""
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(ValidationError):
                auth.authenticate("priya", "wrong")

        auth.set_password("priya", "a brand new passphrase")

        assert auth.authenticate("priya", "a brand new passphrase").username == "priya"

    def test_lockout_is_per_username(self, auth: AuthService) -> None:
        """One user's failures must not lock out everyone else."""
        auth.create_user(
            username="rahul",
            display_name="Rahul",
            email="r@vays.com",
            password=PASSWORD,
            role=UserRole.APPROVER,
        )
        for _ in range(MAX_ATTEMPTS):
            with pytest.raises(ValidationError):
                auth.authenticate("priya", "wrong")

        assert auth.authenticate("rahul", PASSWORD).username == "rahul"


class TestUserManagement:
    def test_duplicate_usernames_are_refused(self, auth: AuthService) -> None:
        with pytest.raises(ValidationError) as exc_info:
            auth.create_user(
                username="PRIYA", display_name="Impostor", email="x@y.com", password=PASSWORD
            )

        assert "already taken" in exc_info.value.user_message

    def test_weak_passwords_are_refused(self, auth: AuthService) -> None:
        with pytest.raises(ValidationError):
            auth.create_user(
                username="weak", display_name="W", email="w@vays.com", password="short"
            )

    def test_has_any_users(self, auth: AuthService) -> None:
        assert auth.has_any_users() is True


class TestRolePermissions:
    def test_editors_cannot_send(self, auth: AuthService) -> None:
        assert auth.authenticate("priya", PASSWORD).can_send is False

    def test_approvers_can_send(self, auth: AuthService) -> None:
        auth.create_user(
            username="rahul",
            display_name="Rahul",
            email="r@vays.com",
            password=PASSWORD,
            role=UserRole.APPROVER,
        )

        assert auth.authenticate("rahul", PASSWORD).can_send is True

    def test_admins_can_send_and_administer(self, auth: AuthService) -> None:
        auth.create_user(
            username="sid",
            display_name="Sidhant",
            email="s@vays.com",
            password=PASSWORD,
            role=UserRole.ADMIN,
        )
        user = auth.authenticate("sid", PASSWORD)

        assert user.can_send is True
        assert user.is_admin is True
