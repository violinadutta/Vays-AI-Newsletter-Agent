"""Tests for password hashing and session tokens.

This is the module that replaced a third-party auth package (D-15). The
justification for owning ~150 lines of security-sensitive code is that it is
fully auditable — which is only true if it is also fully tested.
"""

from __future__ import annotations

import time

import pytest

from core.auth import (
    MIN_PASSWORD_LENGTH,
    SESSION_TTL_SECONDS,
    generate_password,
    hash_password,
    sign_session_token,
    validate_password_strength,
    verify_password,
    verify_session_token,
)
from core.exceptions import ValidationError

SECRET = "test-secret-key-at-least-32-characters-long"


class TestPasswordHashing:
    def test_correct_password_verifies(self) -> None:
        assert verify_password("correct horse battery", hash_password("correct horse battery"))

    def test_wrong_password_does_not_verify(self) -> None:
        assert not verify_password("wrong", hash_password("correct horse battery"))

    def test_hash_is_salted(self) -> None:
        """Identical passwords must not produce identical hashes, or a stolen
        database reveals which users share a password."""
        assert hash_password("same password") != hash_password("same password")

    def test_hash_does_not_contain_the_password(self) -> None:
        assert "hunter2hunter2" not in hash_password("hunter2hunter2")

    def test_passwords_longer_than_72_bytes_are_fully_significant(self) -> None:
        """bcrypt only reads the first 72 bytes. Without SHA-256 pre-hashing,
        these two passwords would be the *same* password — a silent and serious
        weakness for anyone using a long passphrase."""
        base = "a" * 72
        stored = hash_password(base + "FIRST")

        assert verify_password(base + "FIRST", stored)
        assert not verify_password(base + "SECOND", stored)

    def test_unicode_passwords_work(self) -> None:
        assert verify_password("paßwörd–ünicode", hash_password("paßwörd–ünicode"))

    @pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$2b$12$tooshort", "🙂"])
    def test_corrupt_hash_fails_closed(self, bad_hash: str) -> None:
        """A damaged row must fail the login, not crash the page for everyone."""
        assert verify_password("anything", bad_hash) is False


class TestPasswordStrength:
    def test_accepts_a_reasonable_passphrase(self) -> None:
        validate_password_strength("correct horse battery staple")

    def test_rejects_short_passwords(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_password_strength("a" * (MIN_PASSWORD_LENGTH - 1))

        assert str(MIN_PASSWORD_LENGTH) in exc_info.value.user_message

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError):
            validate_password_strength(" " * 20)

    def test_generated_passwords_pass_the_policy(self) -> None:
        validate_password_strength(generate_password())

    def test_generated_passwords_are_unique(self) -> None:
        assert len({generate_password() for _ in range(200)}) == 200


class TestSessionTokens:
    def test_round_trip(self) -> None:
        token = sign_session_token("priya", "editor", SECRET)
        payload = verify_session_token(token, SECRET)

        assert payload is not None
        assert payload["u"] == "priya"
        assert payload["r"] == "editor"

    def test_a_tampered_payload_is_rejected(self) -> None:
        """The payload is readable but not forgeable — the whole point of
        signing it. Escalating 'editor' to 'admin' must fail."""
        token = sign_session_token("priya", "editor", SECRET)
        encoded, signature = token.split(".", 1)
        forged = sign_session_token("priya", "admin", "attacker-secret").split(".")[0]

        assert verify_session_token(f"{forged}.{signature}", SECRET) is None

    def test_a_tampered_signature_is_rejected(self) -> None:
        token = sign_session_token("priya", "editor", SECRET)
        encoded, _ = token.split(".", 1)

        assert verify_session_token(f"{encoded}.{'0' * 64}", SECRET) is None

    def test_a_token_signed_with_another_secret_is_rejected(self) -> None:
        token = sign_session_token("priya", "admin", "some-other-secret")

        assert verify_session_token(token, SECRET) is None

    def test_expired_tokens_are_rejected(self) -> None:
        issued = time.time() - SESSION_TTL_SECONDS - 1
        token = sign_session_token("priya", "editor", SECRET, now=issued)

        assert verify_session_token(token, SECRET) is None

    def test_a_token_just_inside_its_lifetime_is_accepted(self) -> None:
        issued = time.time() - SESSION_TTL_SECONDS + 60
        token = sign_session_token("priya", "editor", SECRET, now=issued)

        assert verify_session_token(token, SECRET) is not None

    @pytest.mark.parametrize("token", ["", "no-dot", "a.b", "....", "!!!.???", "x" * 500])
    def test_malformed_tokens_return_none_rather_than_raising(self, token: str) -> None:
        """A malformed cookie must log the user out, not produce a 500."""
        assert verify_session_token(token, SECRET) is None
