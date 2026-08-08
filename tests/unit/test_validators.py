"""Tests for input validation.

The SSRF suite is the most security-critical set of tests in the project. If it
regresses, a marketing executive pasting a URL can make the server fetch cloud
credentials or reach an internal admin panel. Every case below is a real,
documented bypass technique — string blocklists defeat none of them, which is
why the implementation resolves DNS and inspects the resulting addresses.
"""

from __future__ import annotations

import socket

import pytest

from core.exceptions import InvalidEmailError, InvalidURLError
from core.validators import (
    is_public_address,
    normalise_url,
    validate_email_address,
    validate_url,
)


@pytest.fixture
def resolves_to(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - fixture factory
    """Force DNS resolution to return chosen addresses, with no network access."""

    def _install(*addresses: str) -> None:
        def fake_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN202, ARG001
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port or 443))
                for addr in addresses
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    return _install


# ─────────────────────────────────────────────────────────────────────────────
#  SSRF
# ─────────────────────────────────────────────────────────────────────────────
class TestSSRFLiteralAddresses:
    """A bare IP literal never reaches DNS, so it is checked directly."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            # Legacy IPv4 notations the OS resolver accepts but `ipaddress`
            # rejects. Each is a documented SSRF filter bypass.
            "http://127.1/admin",  # shorthand loopback
            "http://2130706433/admin",  # decimal loopback
            "http://0x7f000001/admin",  # hex loopback
            "http://127.0.1/admin",  # three-part shorthand
            "https://0.0.0.0/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/router",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
            "http://[fc00::1]/",  # unique-local IPv6
            "http://[fe80::1]/",  # link-local IPv6
        ],
    )
    def test_non_public_literals_are_blocked(self, url: str) -> None:
        with pytest.raises(InvalidURLError) as exc_info:
            validate_url(url, resolve_dns=False)

        assert "internal or private" in exc_info.value.user_message

    def test_public_literal_is_allowed(self) -> None:
        assert validate_url("https://93.184.216.34/page", resolve_dns=False)


class TestSSRFHostnames:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:80/",
            "http://LOCALHOST/",
            "http://myapp.local/",
            "http://db.internal/",
            "http://metadata.google.internal/",
            "http://server.corp/",
        ],
    )
    def test_internal_hostnames_are_blocked(self, url: str) -> None:
        with pytest.raises(InvalidURLError):
            validate_url(url, resolve_dns=False)

    def test_public_name_resolving_to_private_is_blocked(self, resolves_to) -> None:  # noqa: ANN001
        """The classic bypass: an attacker-controlled DNS record for a
        perfectly innocent-looking hostname."""
        resolves_to("10.0.0.5")

        with pytest.raises(InvalidURLError) as exc_info:
            validate_url("https://totally-normal-blog.com/post")

        assert "10.0.0.5" in exc_info.value.context["blocked_addresses"]

    def test_decimal_ip_notation_is_caught_by_dns(self, resolves_to) -> None:  # noqa: ANN001
        """``http://2130706433/`` is 127.0.0.1 in decimal. It parses as a
        hostname, so only the resolution check catches it."""
        resolves_to("127.0.0.1")

        with pytest.raises(InvalidURLError):
            validate_url("http://2130706433/")

    def test_mixed_public_and_private_results_are_blocked(self, resolves_to) -> None:  # noqa: ANN001
        """If any address is private, reject. Otherwise an attacker who controls
        DNS gets the safe address validated and the unsafe one connected to."""
        resolves_to("93.184.216.34", "127.0.0.1")

        with pytest.raises(InvalidURLError):
            validate_url("https://mixed.example.com/")

    def test_fully_public_resolution_is_allowed(self, resolves_to) -> None:  # noqa: ANN001
        resolves_to("93.184.216.34", "93.184.216.35")

        assert validate_url("https://dell.com/blog") == "https://dell.com/blog"

    def test_dns_failure_is_a_clear_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", boom)

        with pytest.raises(InvalidURLError) as exc_info:
            validate_url("https://does-not-exist-xyz.com/")

        assert "spelled correctly" in exc_info.value.user_message


class TestURLSurface:
    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "ftp://host/x", "gopher://host/", "data:text/html,x"]
    )
    def test_only_http_schemes_are_accepted(self, url: str) -> None:
        with pytest.raises(InvalidURLError):
            validate_url(url, resolve_dns=False)

    def test_embedded_credentials_are_rejected(self) -> None:
        """A credential-leak and SSRF vector that no blog URL ever needs."""
        with pytest.raises(InvalidURLError) as exc_info:
            validate_url("https://user:pass@dell.com/blog", resolve_dns=False)

        assert "username or password" in exc_info.value.user_message

    @pytest.mark.parametrize("port", [22, 3306, 5432, 6379, 8501, 11434])
    def test_non_web_ports_are_rejected(self, port: int) -> None:
        """Defence in depth: blocks probing internal services that happen to be
        reachable, even on an otherwise public host."""
        with pytest.raises(InvalidURLError):
            validate_url(f"http://example.com:{port}/", resolve_dns=False)

    @pytest.mark.parametrize("port", [80, 443])
    def test_standard_web_ports_are_allowed(self, port: int) -> None:
        assert validate_url(f"http://93.184.216.34:{port}/", resolve_dns=False)

    def test_malformed_port_is_rejected(self) -> None:
        with pytest.raises(InvalidURLError):
            validate_url("http://example.com:notaport/", resolve_dns=False)

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(InvalidURLError):
            validate_url("   ", resolve_dns=False)


class TestNormalisation:
    def test_scheme_is_added_when_missing(self) -> None:
        assert normalise_url("www.dell.com/blog").startswith("https://")

    def test_fragment_is_stripped(self) -> None:
        """Fragments are never sent to a server, so keeping them would only
        break de-duplication of the same article pasted twice."""
        assert normalise_url("https://dell.com/blog#section-2") == "https://dell.com/blog"

    def test_query_string_is_preserved(self) -> None:
        assert normalise_url("https://dell.com/b?id=7") == "https://dell.com/b?id=7"

    def test_host_is_lowercased_but_path_is_not(self) -> None:
        """Hosts are case-insensitive; paths are not, and lowercasing one would
        404 on any case-sensitive server."""
        assert normalise_url("https://DELL.com/Blog/Post") == "https://dell.com/Blog/Post"

    def test_whitespace_is_trimmed(self) -> None:
        assert normalise_url("  https://dell.com/x  ") == "https://dell.com/x"


class TestIsPublicAddress:
    @pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34", "2606:4700::1"])
    def test_public(self, addr: str) -> None:
        assert is_public_address(addr) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "10.1.1.1",
            "192.168.0.1",
            "172.20.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "::ffff:10.0.0.1",
        ],
    )
    def test_not_public(self, addr: str) -> None:
        assert is_public_address(addr) is False

    def test_garbage_is_not_public(self) -> None:
        """Fail closed on anything unparseable."""
        assert is_public_address("not-an-ip") is False


# ─────────────────────────────────────────────────────────────────────────────
#  Email
# ─────────────────────────────────────────────────────────────────────────────
class TestEmailValidation:
    @pytest.mark.parametrize(
        "address",
        [
            "priya@vays.com",
            "first.last@sub.domain.co.uk",
            "user+tag@example.io",
            "a_b-c@example.com",
            "x@e.co",
        ],
    )
    def test_valid_addresses(self, address: str) -> None:
        assert validate_email_address(address) == address.lower()

    @pytest.mark.parametrize(
        "address",
        [
            "",
            "  ",
            "no-at-sign",
            "@example.com",
            "user@",
            "user@@example.com",
            "user@example",
            "user name@example.com",
            "user@exam ple.com",
            "user@.com",
        ],
    )
    def test_invalid_addresses(self, address: str) -> None:
        with pytest.raises(InvalidEmailError):
            validate_email_address(address)

    def test_address_is_lowercased(self) -> None:
        """Suppression lookups and the per-campaign uniqueness constraint both
        depend on this: someone who unsubscribed as Priya@Vays.com must not
        receive mail as priya@vays.com."""
        assert validate_email_address("  Priya@Vays.COM ") == "priya@vays.com"

    def test_overlong_address_is_rejected(self) -> None:
        with pytest.raises(InvalidEmailError):
            validate_email_address("a" * 250 + "@example.com")

    def test_overlong_local_part_is_rejected(self) -> None:
        with pytest.raises(InvalidEmailError):
            validate_email_address("a" * 65 + "@example.com")
