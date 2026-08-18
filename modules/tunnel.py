"""Finding the public URL when the dashboard is behind an ngrok tunnel.

**Why this exists.** The approval email carries a link to the review page. That
link is read on somebody else's machine — a phone, a manager's laptop — where
``http://localhost:8501`` means their own computer and resolves to nothing. A
tunnel gives the app a public address; this module works out what it currently
is.

**The trap it avoids is one this project has already been burned by.** On the
free tier ngrok issues a *new random hostname every restart*, which is precisely
the failure that made Colab unusable (D-21): a URL that rotates underneath a
configuration nobody remembers to update. Hard-coding today's tunnel into
``.env`` would produce approval emails whose links stop working the next time
the tunnel restarts, with no error anywhere — the mail simply arrives with a dead
button.

So ``AGENT_APP_BASE_URL=auto`` asks the locally running ngrok agent what its
address is *at the moment the email is composed*. ngrok exposes that on
``127.0.0.1:4040`` with no authentication, because it is bound to loopback.

A **reserved domain** (free tier now includes one) is still the better answer,
because it fixes old links as well as new ones. This is what makes the ephemeral
case survivable, not a reason to prefer it.
"""

from __future__ import annotations

import httpx

from config import get_logger, get_settings

log = get_logger(__name__)

#: The ngrok agent's local inspection API. Loopback-only and unauthenticated by
#: design — it is not reachable from outside the machine.
NGROK_API = "http://127.0.0.1:4040/api/tunnels"

#: Deliberately short. This runs while composing an approval email; if ngrok is
#: not running, failing fast and falling back beats holding up the send.
TIMEOUT_S = 2.0

#: The literal value meaning "ask the tunnel".
AUTO = "auto"


def detect_ngrok_url(timeout: float = TIMEOUT_S) -> str | None:
    """The current public HTTPS URL of a running ngrok tunnel, or ``None``.

    Never raises. Every failure mode here — ngrok not installed, not running,
    still starting, a changed API shape — means the same thing to the caller:
    fall back to the configured URL.
    """
    try:
        response = httpx.get(NGROK_API, timeout=timeout)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    if not isinstance(tunnels, list):
        return None

    urls = [
        str(t.get("public_url", ""))
        for t in tunnels
        if isinstance(t, dict) and str(t.get("public_url", "")).startswith("http")
    ]
    # Prefer https: the approval page collects a password, and ngrok terminates
    # TLS for us. An http tunnel would carry that in clear text across the
    # internet.
    https = [u for u in urls if u.startswith("https://")]
    candidates = https or urls
    if not candidates:
        return None

    chosen = candidates[0]
    log.info("tunnel.detected", url=chosen)
    return chosen


def resolve_base_url(configured: str) -> str:
    """The base URL to build approval links from.

    Args:
        configured: ``AGENT_APP_BASE_URL``. The literal ``auto`` means "ask the
            running ngrok agent"; anything else is used as given.

    Returns:
        An absolute URL with no trailing slash. Falls back to ``localhost`` when
        ``auto`` is set but no tunnel answers — a link that works on this machine
        is more useful than a malformed one, and the log says which happened.
    """
    cleaned = configured.strip()
    if not cleaned:
        return local_url()
    if cleaned.lower() != AUTO:
        return cleaned.rstrip("/")

    detected = detect_ngrok_url()
    if detected:
        return detected.rstrip("/")

    log.warning(
        "tunnel.not_found",
        note=(
            "AGENT_APP_BASE_URL=auto but no ngrok tunnel is running; approval "
            "links will point at localhost and only work on this machine"
        ),
    )
    return local_url()


def local_url() -> str:
    """This machine's own address, on the configured port."""
    return f"http://localhost:{get_settings().app.port}"
