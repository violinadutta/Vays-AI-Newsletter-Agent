"""Resolving the public URL that approval links are built from.

The failure this guards against is quiet and expensive: an approval email whose
button points at a tunnel that no longer exists. Nothing errors — the mail simply
arrives with a dead link, and the campaign waits forever.
"""

from __future__ import annotations

import httpx
import pytest

from modules.tunnel import detect_ngrok_url, local_url, resolve_base_url

TUNNELS = {
    "tunnels": [
        {"public_url": "http://ab12cd34.ngrok-free.app", "proto": "http"},
        {"public_url": "https://ab12cd34.ngrok-free.app", "proto": "https"},
    ]
}


@pytest.fixture
def ngrok(monkeypatch):  # noqa: ANN001, ANN201
    """Stand in for the ngrok agent's local API."""

    def _serve(payload: object, status: int = 200) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            if status != 200:
                return httpx.Response(status)
            return httpx.Response(200, json=payload)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(httpx, "get", lambda *_a, **_k: client.get("http://x"))

    return _serve


def unreachable(monkeypatch) -> None:  # noqa: ANN001
    def boom(*_a: object, **_k: object) -> httpx.Response:
        raise httpx.ConnectError("nothing listening on 4040")

    monkeypatch.setattr(httpx, "get", boom)


class TestDetection:
    def test_it_finds_the_public_url(self, ngrok) -> None:  # noqa: ANN001
        ngrok(TUNNELS)

        assert detect_ngrok_url() == "https://ab12cd34.ngrok-free.app"

    def test_https_is_preferred_over_http(self, ngrok) -> None:  # noqa: ANN001
        """The approval page collects a password. An http tunnel would carry it
        across the internet in clear text; ngrok terminates TLS for us."""
        ngrok(TUNNELS)

        assert detect_ngrok_url().startswith("https://")

    def test_ngrok_not_running_is_not_an_error(self, monkeypatch) -> None:  # noqa: ANN001
        """The common case — the tunnel window simply is not open."""
        unreachable(monkeypatch)

        assert detect_ngrok_url() is None

    def test_a_non_200_yields_nothing(self, ngrok) -> None:  # noqa: ANN001
        ngrok(None, status=502)

        assert detect_ngrok_url() is None

    def test_no_tunnels_yet_yields_nothing(self, ngrok) -> None:  # noqa: ANN001
        """ngrok answers before it has connected. Returning a half-started state
        would put an unusable host into an email."""
        ngrok({"tunnels": []})

        assert detect_ngrok_url() is None

    def test_an_unexpected_shape_yields_nothing(self, ngrok) -> None:  # noqa: ANN001
        ngrok({"something": "else"})

        assert detect_ngrok_url() is None


class TestResolution:
    def test_a_fixed_url_is_used_as_given(self, monkeypatch) -> None:  # noqa: ANN001
        """A configured address must never be second-guessed by a tunnel that
        happens to be running."""
        unreachable(monkeypatch)

        assert resolve_base_url("https://newsletter.vaysinfotech.com") == (
            "https://newsletter.vaysinfotech.com"
        )

    def test_a_trailing_slash_is_stripped(self, monkeypatch) -> None:  # noqa: ANN001
        unreachable(monkeypatch)

        assert resolve_base_url("http://192.168.1.40:8501/") == "http://192.168.1.40:8501"

    def test_auto_uses_the_live_tunnel(self, ngrok) -> None:  # noqa: ANN001
        """The point of the whole module: the address is read at the moment the
        email is composed, not trusted from configuration written days ago."""
        ngrok(TUNNELS)

        assert resolve_base_url("auto") == "https://ab12cd34.ngrok-free.app"

    def test_auto_falls_back_to_localhost(self, monkeypatch, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """A link that works on this machine beats a malformed one, and the log
        records which happened."""
        unreachable(monkeypatch)

        assert resolve_base_url("auto") == "http://localhost:8501"

    def test_an_empty_value_means_localhost(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """Blank is the "just me on this PC" case, not a configuration error."""
        assert resolve_base_url("") == local_url()

    def test_the_port_comes_from_configuration(self, set_env) -> None:  # noqa: ANN001
        """The port appears in run.bat, the tunnel script and the approval link.
        A literal repeated in three places is one somebody changes in two."""
        from config.settings import reset_settings_cache
        from tests.conftest import MINIMAL_ENV

        set_env(**MINIMAL_ENV, APP_PORT="9000")
        reset_settings_cache()

        assert local_url() == "http://localhost:9000"

    def test_a_production_domain_is_used_verbatim(self, monkeypatch) -> None:  # noqa: ANN001
        """The production switch: one env value, no code change, and a running
        tunnel must not override it."""
        ngrok_running = {"tunnels": [{"public_url": "https://random.ngrok-free.app"}]}

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ngrok_running)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(httpx, "get", lambda *_a, **_k: client.get("http://x"))

        assert resolve_base_url("https://newsletter.vaysinfotech.com") == (
            "https://newsletter.vaysinfotech.com"
        )

    @pytest.mark.parametrize("value", ["auto", "AUTO", " Auto "])
    def test_auto_is_recognised_however_it_is_typed(self, ngrok, value: str) -> None:  # noqa: ANN001
        ngrok(TUNNELS)

        assert resolve_base_url(value) == "https://ab12cd34.ngrok-free.app"
