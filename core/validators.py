"""Input validation — including the SSRF guard.

The URL validator here is the highest-security-value code in the project. The
application fetches arbitrary URLs supplied by a user, which is the textbook
setup for **Server-Side Request Forgery**: a user pastes
``http://169.254.169.254/latest/meta-data/`` and the server obligingly fetches
cloud credentials, or ``http://localhost:8501/`` and reaches an internal service
that assumed it was unreachable from outside.

The defence is an allow-by-exclusion check on the *resolved IP addresses*, not on
the hostname string. Blocklisting the text ``localhost`` is trivially bypassed by
``127.0.0.1``, ``0x7f.1``, ``2130706433``, a DNS name that resolves to a private
address, or an IPv6-mapped form. Resolving first and inspecting every returned
address defeats all of those at once.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from core.exceptions import InvalidEmailError, InvalidURLError

__all__ = [
    "as_ip_literal",
    "is_public_address",
    "normalise_url",
    "resolve_public_addresses",
    "validate_email_address",
    "validate_url",
]

#: Only these schemes are ever fetched. `file://`, `gopher://`, `ftp://` and
#: friends are classic SSRF escalation vectors.
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Blogs are served on standard web ports. Permitting arbitrary ports would let a
#: crafted URL probe internal services (databases, admin panels) that happen to be
#: reachable from the host. `None` means "scheme default".
ALLOWED_PORTS: Final[frozenset[int | None]] = frozenset({None, 80, 443})

#: Rejected before DNS is even attempted — cheap, and gives a clearer message.
BLOCKED_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

#: Internal TLDs that should never be fetched from the public web.
BLOCKED_SUFFIXES: Final[tuple[str, ...]] = (
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".lan",
)

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}$"
)

#: RFC 5321 limits: 64 for the local part, 254 for the whole address.
_EMAIL_MAX_LENGTH: Final[int] = 254
_EMAIL_LOCAL_MAX_LENGTH: Final[int] = 64


# ─────────────────────────────────────────────────────────────────────────────
#  IP address classification
# ─────────────────────────────────────────────────────────────────────────────
def is_public_address(address: str) -> bool:
    """Whether ``address`` is a globally routable IP.

    Rejects loopback, private (RFC 1918), link-local (including the
    ``169.254.169.254`` cloud metadata endpoint), unique-local IPv6, multicast,
    reserved and unspecified ranges.

    IPv4-mapped and 6to4 IPv6 addresses are unwrapped before classification,
    because ``::ffff:127.0.0.1`` is loopback wearing a disguise and Python's
    ``is_global`` does not see through it.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour

    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def as_ip_literal(hostname: str) -> str | None:
    """Return the canonical IP if ``hostname`` is an address literal, else ``None``.

    ``ipaddress`` accepts only canonical forms, but the OS resolver also accepts
    legacy IPv4 notations that ``inet_aton`` understands — ``127.1``,
    ``2130706433``, ``0x7f000001`` are all loopback. Treating those as ordinary
    hostnames would send them down the DNS path, and with DNS checking disabled
    they would sail straight through. Every one of them is a documented SSRF
    filter bypass, so both parsers are tried here.
    """
    candidate = hostname.strip("[]")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    try:
        return socket.inet_ntoa(socket.inet_aton(candidate))
    except OSError:
        return None


def resolve_public_addresses(hostname: str, port: int) -> list[str]:
    """Resolve ``hostname`` and return its addresses, or raise if any is private.

    **Every** returned address must be public. A hostname with one public and one
    private address is rejected outright: an attacker who controls DNS can
    otherwise have the safe address checked and the unsafe one connected to.

    Raises:
        InvalidURLError: On resolution failure, or if any address is not public.
    """
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidURLError(
            f"DNS resolution failed for {hostname!r}: {exc}",
            user_message=(
                f"Couldn't find the website '{hostname}'. Check the address is spelled correctly."
            ),
            context={"hostname": hostname},
        ) from exc

    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise InvalidURLError(
            f"{hostname!r} resolved to no addresses",
            user_message=f"Couldn't find the website '{hostname}'.",
        )

    private = [addr for addr in addresses if not is_public_address(addr)]
    if private:
        raise InvalidURLError(
            f"SSRF blocked: {hostname!r} resolves to non-public address(es) {private}",
            user_message=(
                "Only public websites can be used. That address points to an "
                "internal or private network."
            ),
            context={"hostname": hostname, "blocked_addresses": private},
        )
    return addresses


# ─────────────────────────────────────────────────────────────────────────────
#  URLs
# ─────────────────────────────────────────────────────────────────────────────
def normalise_url(raw: str) -> str:
    """Trim, add a scheme if missing, and strip the fragment.

    Users paste ``www.dell.com/blog`` and ``https://dell.com/blog#section-2``.
    Both should reach the same place, and the fragment is never sent to a server
    anyway, so keeping it would only break de-duplication.
    """
    text = raw.strip()
    if not text:
        raise InvalidURLError("empty URL", user_message="Please enter a web address.")
    if "://" not in text:
        text = f"https://{text}"

    parts = urlsplit(text)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def validate_url(raw: str, *, resolve_dns: bool = True) -> str:
    """Validate a user-supplied URL and return its normalised form.

    Args:
        raw: The URL as typed or pasted.
        resolve_dns: Perform the DNS-resolution SSRF check. Only set ``False`` in
            tests that must not touch the network — never in application code.

    Returns:
        The normalised, safe-to-fetch URL.

    Raises:
        InvalidURLError: If the URL is malformed, uses a disallowed scheme or
            port, carries credentials, or points anywhere non-public.
    """
    url = normalise_url(raw)
    parts = urlsplit(url)

    if parts.scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError(
            f"disallowed scheme {parts.scheme!r}",
            user_message="Only http:// and https:// web addresses are supported.",
            context={"scheme": parts.scheme},
        )

    # user:password@host is an SSRF and credential-leak vector, and no blog needs it.
    if parts.username or parts.password:
        raise InvalidURLError(
            "URL contains embedded credentials",
            user_message="Web addresses with a username or password aren't allowed.",
        )

    hostname = parts.hostname
    if not hostname:
        raise InvalidURLError(
            f"no hostname in {url!r}",
            user_message="That doesn't look like a complete web address.",
        )

    try:
        port = parts.port
    except ValueError as exc:  # non-numeric or out-of-range port
        raise InvalidURLError(
            f"invalid port in {url!r}",
            user_message="That web address has an invalid port number.",
        ) from exc

    if port not in ALLOWED_PORTS:
        raise InvalidURLError(
            f"disallowed port {port}",
            user_message=("Only standard web addresses (ports 80 and 443) are supported."),
            context={"port": port},
        )

    lowered = hostname.lower().rstrip(".")
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_SUFFIXES):
        raise InvalidURLError(
            f"SSRF blocked: internal hostname {hostname!r}",
            user_message="Only public websites can be used, not internal addresses.",
            context={"hostname": hostname},
        )

    # A bare IP literal skips DNS, so check it directly — in every notation the
    # OS resolver would accept, not just the canonical one.
    literal = as_ip_literal(lowered)
    if literal is not None:
        if not is_public_address(literal):
            raise InvalidURLError(
                f"SSRF blocked: non-public IP {hostname!r} ({literal})",
                user_message=(
                    "Only public websites can be used. That address points to an "
                    "internal or private network."
                ),
                context={"hostname": hostname, "resolved_literal": literal},
            )
    elif resolve_dns:
        resolve_public_addresses(lowered, port or (443 if parts.scheme == "https" else 80))

    return url


# ─────────────────────────────────────────────────────────────────────────────
#  Email addresses
# ─────────────────────────────────────────────────────────────────────────────
def validate_email_address(raw: str) -> str:
    """Validate and normalise a recipient email address.

    Returns the address lower-cased and trimmed, which is what makes the
    per-campaign uniqueness constraint and the suppression-list lookup work — a
    contact who unsubscribed as ``Priya@Vays.com`` must not receive mail as
    ``priya@vays.com``.

    Raises:
        InvalidEmailError: If the address is not syntactically valid.
    """
    address = raw.strip()
    if not address:
        raise InvalidEmailError("empty email address", user_message="Email address is missing.")

    if len(address) > _EMAIL_MAX_LENGTH:
        raise InvalidEmailError(
            f"address exceeds {_EMAIL_MAX_LENGTH} characters",
            user_message="That email address is too long to be valid.",
        )

    local, _, _domain = address.partition("@")
    if len(local) > _EMAIL_LOCAL_MAX_LENGTH:
        raise InvalidEmailError(
            "local part too long",
            user_message="That email address is not valid.",
        )

    if not _EMAIL_RE.match(address):
        raise InvalidEmailError(
            f"malformed address {address!r}",
            user_message="That doesn't look like a valid email address.",
            context={"length": len(address)},
        )

    return address.lower()
