"""Password hashing and session tokens.

This module exists instead of a third-party auth package (D-15). The reasoning:
authentication is the one boundary where an unvetted dependency has the worst
blast radius, and the functionality actually needed is small — hash, verify, sign
a token, check a token. All of it is below, and all of it is auditable by whoever
inherits this project.

Pure computation only: no database, no I/O, no framework. The login *flow* —
looking a user up, counting failed attempts — lives in
:mod:`services.auth_service`, so this file stays trivially testable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

import bcrypt

from core.exceptions import ValidationError

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "generate_password",
    "hash_password",
    "sign_session_token",
    "validate_password_strength",
    "verify_password",
    "verify_session_token",
]

#: bcrypt work factor. 12 is ~250ms on current hardware — slow enough to make
#: offline cracking expensive, fast enough that a login does not feel broken.
BCRYPT_ROUNDS = 12

MIN_PASSWORD_LENGTH = 10

#: Session lifetime. Long enough for a working day; short enough that a forgotten
#: session on a shared machine expires on its own.
SESSION_TTL_SECONDS = 12 * 60 * 60


def _prehash(password: str) -> bytes:
    """Reduce a password to a fixed 44-byte value before bcrypt sees it.

    bcrypt only considers the first 72 bytes of its input. Historically it
    truncated silently — so ``correct-horse-battery-staple-...`` and the same
    string with a different 73rd character were the *same password* — and newer
    releases raise instead. SHA-256 then base64 removes the limit entirely and is
    the standard remedy.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Hash a password for storage. The result is safe to put in the database."""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash.

    Returns ``False`` rather than raising on a malformed or corrupt hash: a
    damaged row must fail closed, not crash the login page for everyone.
    """
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str) -> None:
    """Reject passwords that are too weak to store.

    Deliberately minimal — length is the only requirement that reliably
    correlates with strength. Composition rules ("one uppercase, one symbol")
    mostly produce ``Password1!`` and a sticky note.

    Raises:
        ValidationError: If the password is too short or entirely whitespace.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"password shorter than {MIN_PASSWORD_LENGTH} characters",
            user_message=(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters. "
                "A short phrase you can remember works well."
            ),
        )
    if not password.strip():
        raise ValidationError(
            "password is only whitespace",
            user_message="Password cannot be blank.",
        )


def generate_password(length: int = 16) -> str:
    """Generate a random password, for admin-initiated resets."""
    return secrets.token_urlsafe(length)


# ─────────────────────────────────────────────────────────────────────────────
#  Session tokens
# ─────────────────────────────────────────────────────────────────────────────
def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def sign_session_token(username: str, role: str, secret: str, *, now: float | None = None) -> str:
    """Create a signed, expiring session token.

    The payload is readable but not forgeable: an attacker can see the username,
    but cannot change it to ``admin`` without the signing secret.
    """
    issued = now if now is not None else time.time()
    payload = json.dumps(
        {"u": username, "r": role, "iat": int(issued), "exp": int(issued + SESSION_TTL_SECONDS)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload, secret)}"


def verify_session_token(
    token: str, secret: str, *, now: float | None = None
) -> dict[str, str | int] | None:
    """Validate a session token and return its payload, or ``None`` if invalid.

    Returns ``None`` for a tampered signature, a malformed token, or an expired
    one — the caller cannot distinguish them, and should not: all three mean
    "log in again".
    """
    try:
        encoded, signature = token.split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError):
        return None

    # Constant-time comparison: a timing-sensitive `==` leaks how much of a
    # forged signature was correct, which is enough to forge one byte at a time.
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

    current = now if now is not None else time.time()
    if not isinstance(data, dict) or data.get("exp", 0) < current:
        return None

    return data


# ─────────────────────────────────────────────────────────────────────────────
#  Recipient action tokens (like / unsubscribe links in an email)
# ─────────────────────────────────────────────────────────────────────────────
#: Recipient links must keep working long after the send. An unsubscribe link
#: that expires is a compliance problem — someone reading a six-month-old
#: newsletter still has the right to opt out — so these live far longer than a
#: session token. They are single-purpose and grant no access to the app.
RECIPIENT_TOKEN_TTL_SECONDS = 2 * 365 * 24 * 60 * 60


def sign_recipient_token(
    email: str, campaign_id: int, action: str, secret: str, *, now: float | None = None
) -> str:
    """Create a signed link token identifying one recipient, campaign and action.

    Stateless on purpose. A stored token would mean one database row per
    recipient per action per campaign — 600 rows for a 300-address send that may
    never be clicked — and a row that has to exist before the email goes out.
    Signing carries the identity in the URL instead, and the signature is what
    makes it unforgeable: a recipient can read their own address in the link but
    cannot change it to somebody else's and unsubscribe them.

    The action is inside the signed payload, so a "like" link cannot be edited
    into an "unsubscribe" one.
    """
    issued = now if now is not None else time.time()
    payload = json.dumps(
        {
            "e": email.strip().lower(),
            "c": int(campaign_id),
            "a": action,
            "iat": int(issued),
            "exp": int(issued + RECIPIENT_TOKEN_TTL_SECONDS),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload, secret)}"


def verify_recipient_token(
    token: str, secret: str, *, expected_action: str | None = None, now: float | None = None
) -> dict[str, str | int] | None:
    """Validate a recipient link token, or ``None`` if it is not usable.

    ``expected_action`` binds the token to the page handling it, so a link minted
    for one action cannot be replayed against another route.
    """
    try:
        encoded, signature = token.split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError):
        return None

    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or data.get("exp", 0) < (now if now is not None else time.time()):
        return None
    if expected_action is not None and data.get("a") != expected_action:
        return None
    return data
