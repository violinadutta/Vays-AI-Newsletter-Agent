"""Keeping a signed-in user signed in across a browser refresh.

``st.session_state`` lives for one Streamlit *session*, and a refresh starts a
new one — so without this, F5 logs you out.

**The cookie carries our own token, and only our own code trusts it.** The
component here is storage: it writes and reads an opaque string in the browser.
Every security property stays in ``core.auth`` — the HMAC signature, the 12-hour
expiry, the constant-time comparison. That distinction is what keeps this
compatible with D-15, which refused ``streamlit-authenticator`` because it would
have owned the authentication itself. A cookie jar owns nothing.

Three checks run before a restored session is honoured:

1. **The signature must verify** against ``APP_SECRET_KEY``. A forged or
   tampered cookie is indistinguishable from a missing one.
2. **The token must not have expired** — 12 hours, enforced inside the payload.
3. **The account must still exist and be active.** This is the one the token
   alone cannot answer: deactivating somebody has to log them out, and without
   this check their cookie would keep working until it expired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import streamlit as st

from config import get_logger, get_settings
from core.auth import SESSION_TTL_SECONDS, verify_session_token
from core.enums import UserRole

log = get_logger(__name__)

#: Deliberately generic. A name like "vays_admin_session" tells anyone reading a
#: cookie list what the application is and that it has an admin area.
COOKIE_NAME = "vays_session"

#: Must be stable: the component is keyed by it, and a changing key would mount
#: a fresh, empty cookie jar on every rerun.
_MANAGER_KEY = "session_cookie_manager"


def _manager() -> object:
    """The cookie component, created once per browser session.

    Held in ``session_state``, **not** ``@st.cache_resource``: the manager mounts
    a frontend widget, and Streamlit refuses widget calls inside a cached
    function. One instance per rerun would also be wrong — each mounts its own
    component and they race to read the same cookie.
    """
    import extra_streamlit_components as stx

    manager = st.session_state.get(_MANAGER_KEY)
    if manager is None:
        manager = stx.CookieManager(key=_MANAGER_KEY)
        st.session_state[_MANAGER_KEY] = manager
    return manager


def remember(username: str, role: UserRole, token: str) -> None:
    """Store the session token so a refresh does not sign the user out.

    Never raises. Failing to persist a session is an inconvenience; failing to
    log in because the cookie jar misbehaved is an outage.
    """
    try:
        _manager().set(  # type: ignore[attr-defined]
            COOKIE_NAME,
            token,
            expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_TTL_SECONDS),
            key=f"{_MANAGER_KEY}_set",
        )
        log.info("session.remembered", username=username, role=str(role))
    except Exception:  # noqa: BLE001 - persistence is best-effort
        log.exception("session.remember_failed", username=username)


def forget() -> None:
    """Clear the stored session. Called on sign-out.

    A sign-out that leaves the cookie in place is worse than no persistence at
    all — the next visitor to that browser would be signed in as the last user.
    """
    try:
        _manager().delete(COOKIE_NAME, key=f"{_MANAGER_KEY}_del")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - the cookie may simply not be there
        log.debug("session.forget_noop")


def stored_token() -> str | None:
    """The raw token from the browser, if any. Unverified."""
    try:
        # Read from st.context first: it is available on the very first run,
        # whereas the component needs a round trip before its jar is populated.
        # Without this the user sees one flash of the login screen on every
        # refresh, which reads exactly like the bug this module exists to fix.
        cookies = getattr(st.context, "cookies", None) or {}
        direct = cookies.get(COOKIE_NAME)
        if direct:
            return str(direct)
        value = _manager().get(COOKIE_NAME)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no cookie is the normal case
        return None
    return str(value) if value else None


def restore() -> object | None:
    """Rebuild the signed-in user from the stored token, or ``None``.

    Returns an ``AuthenticatedUser``. ``None`` means "show the login screen" and
    covers every failure identically — absent, forged, expired, or belonging to
    an account that has since been deactivated.
    """
    token = stored_token()
    if not token:
        return None

    payload = verify_session_token(token, get_settings().app.secret_key.get_secret_value())
    if payload is None:
        # Signature bad or expired. Clear it so the browser stops presenting a
        # cookie that will never work again.
        forget()
        return None

    username = str(payload.get("u", ""))
    if not username:
        forget()
        return None

    # The token says who they were when it was issued. Only the database knows
    # whether that is still true.
    from modules.repository.database import unit_of_work
    from modules.repository.user_repo import UserRepository
    from services.auth_service import AuthenticatedUser

    try:
        with unit_of_work() as session:
            row = UserRepository(session).get_active(username)
            if row is None:
                log.info("session.restore_refused", username=username, reason="inactive or removed")
                forget()
                return None
            user = AuthenticatedUser(
                username=row.username,
                display_name=row.display_name,
                role=UserRole(row.role),
                token=token,
            )
    except Exception:  # noqa: BLE001 - a broken lookup must show the login screen
        log.exception("session.restore_failed", username=username)
        return None

    log.info("session.restored", username=user.username)
    return user
