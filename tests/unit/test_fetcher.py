"""Fetcher tests using respx — no real network access.

The redirect tests are the security-critical ones. Automatic redirect following
is an SSRF hole: a public URL passes validation, then 302s to
``http://169.254.169.254/`` which never gets checked. This fetcher follows hops
by hand precisely so each one is re-validated.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from core.exceptions import FetchError, InvalidURLError
from modules.scraper.fetcher import ArticleFetcher

PAGE = "<html><body><article><p>Real article content goes here.</p></article></body></html>"
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture
def fetcher(set_env) -> ArticleFetcher:  # noqa: ANN001
    from tests.conftest import MINIMAL_ENV

    set_env(**{**MINIMAL_ENV, "SCRAPER_RESPECT_ROBOTS": "false", "SCRAPER_MAX_RETRIES": "1"})
    with ArticleFetcher() as instance:
        yield instance


class TestSuccessfulFetch:
    @respx.mock
    def test_returns_page_html(self, fetcher: ArticleFetcher) -> None:
        respx.get("https://dell.com/blog").mock(
            return_value=httpx.Response(200, text=PAGE, headers=HTML_HEADERS)
        )

        result = fetcher.fetch("https://dell.com/blog")

        assert "Real article content" in result.html
        assert result.status_code == 200
        assert result.elapsed_ms >= 0


class TestRedirects:
    @respx.mock
    def test_follows_a_public_redirect(self, fetcher: ArticleFetcher) -> None:
        respx.get("https://dell.com/old").mock(
            return_value=httpx.Response(301, headers={"location": "https://dell.com/new"})
        )
        respx.get("https://dell.com/new").mock(
            return_value=httpx.Response(200, text=PAGE, headers=HTML_HEADERS)
        )

        result = fetcher.fetch("https://dell.com/old")

        assert result.url == "https://dell.com/new"
        assert result.redirected_from == "https://dell.com/old"

    @respx.mock
    def test_a_redirect_to_a_private_address_is_blocked(self, fetcher: ArticleFetcher) -> None:
        """The SSRF hole that automatic redirect following leaves open."""
        respx.get("https://dell.com/blog").mock(
            return_value=httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        )

        with pytest.raises(InvalidURLError) as exc_info:
            fetcher.fetch("https://dell.com/blog")

        assert "internal or private" in exc_info.value.user_message

    @respx.mock
    def test_a_redirect_to_localhost_is_blocked(self, fetcher: ArticleFetcher) -> None:
        respx.get("https://dell.com/blog").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:80/admin"})
        )

        with pytest.raises(InvalidURLError):
            fetcher.fetch("https://dell.com/blog")

    @respx.mock
    def test_a_redirect_loop_terminates(self, fetcher: ArticleFetcher) -> None:
        respx.get("https://dell.com/loop").mock(
            return_value=httpx.Response(302, headers={"location": "https://dell.com/loop"})
        )

        with pytest.raises(FetchError) as exc_info:
            fetcher.fetch("https://dell.com/loop")

        assert "redirects too many times" in exc_info.value.user_message

    @respx.mock
    def test_a_redirect_without_a_location_header_is_an_error(
        self, fetcher: ArticleFetcher
    ) -> None:
        respx.get("https://dell.com/blog").mock(return_value=httpx.Response(302))

        with pytest.raises(FetchError):
            fetcher.fetch("https://dell.com/blog")


class TestHTTPErrors:
    @respx.mock
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (403, "blocked automated access"),
            (404, "doesn't exist"),
            (429, "rate-limiting"),
            (503, "having problems"),
        ],
    )
    def test_status_codes_map_to_actionable_messages(
        self, fetcher: ArticleFetcher, status: int, expected: str
    ) -> None:
        """'HTTP 403' means nothing to a marketing executive. 'That site blocked
        automated access — paste the text instead' tells them what to do."""
        respx.get("https://dell.com/blog").mock(return_value=httpx.Response(status))

        with pytest.raises(FetchError) as exc_info:
            fetcher.fetch("https://dell.com/blog")

        assert expected in exc_info.value.user_message

    @respx.mock
    def test_timeouts_are_retried_then_reported(self, fetcher: ArticleFetcher) -> None:
        route = respx.get("https://dell.com/blog").mock(
            side_effect=httpx.TimeoutException("too slow")
        )

        with pytest.raises(FetchError) as exc_info:
            fetcher.fetch("https://dell.com/blog")

        assert route.call_count > 1, "timeouts are transient and should be retried"
        assert "took too long" in exc_info.value.user_message

    @respx.mock
    def test_client_errors_are_not_retried(self, fetcher: ArticleFetcher) -> None:
        """Retrying a 404 wastes three timeouts and hides the real problem."""
        route = respx.get("https://dell.com/blog").mock(return_value=httpx.Response(404))

        with pytest.raises(FetchError):
            fetcher.fetch("https://dell.com/blog")

        assert route.call_count == 1


class TestContentTypes:
    @respx.mock
    def test_pdfs_are_rejected_with_a_useful_message(self, fetcher: ArticleFetcher) -> None:
        """Feeding PDF bytes to an HTML parser produces garbage, not an error —
        which is worse, because the user would see a nonsense draft."""
        respx.get("https://dell.com/spec.pdf").mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
            )
        )

        with pytest.raises(FetchError) as exc_info:
            fetcher.fetch("https://dell.com/spec.pdf")

        assert "PDF" in exc_info.value.user_message

    @respx.mock
    def test_oversized_responses_are_rejected(self, fetcher: ArticleFetcher) -> None:
        respx.get("https://dell.com/huge").mock(
            return_value=httpx.Response(200, text="x" * (11 * 1024 * 1024), headers=HTML_HEADERS)
        )

        with pytest.raises(FetchError) as exc_info:
            fetcher.fetch("https://dell.com/huge")

        assert "unusually large" in exc_info.value.user_message


class TestSSRFAtTheFetchBoundary:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/admin",
            "http://169.254.169.254/",
            "http://10.0.0.1/",
            "file:///etc/passwd",
            "http://127.1/",
        ],
    )
    def test_unsafe_urls_never_reach_the_network(self, fetcher: ArticleFetcher, url: str) -> None:
        """Validation happens before a connection is opened — and it lives at
        this boundary so no future caller can bypass it."""
        with respx.mock:
            with pytest.raises(InvalidURLError):
                fetcher.fetch(url)
            assert not respx.calls


class TestRobots:
    @respx.mock
    def test_a_disallowed_path_is_refused(self, set_env) -> None:  # noqa: ANN001
        from tests.conftest import MINIMAL_ENV

        set_env(**{**MINIMAL_ENV, "SCRAPER_RESPECT_ROBOTS": "true"})
        respx.get("https://dell.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/")
        )

        with ArticleFetcher() as fetcher, pytest.raises(FetchError) as exc_info:
            fetcher.fetch("https://dell.com/private/post")

        assert "Paste text manually" in exc_info.value.user_message

    @respx.mock
    def test_a_missing_robots_file_means_allowed(self, set_env) -> None:  # noqa: ANN001
        """Failing open is deliberate: a transient robots.txt error must not
        block a public page the user could open in their browser."""
        from tests.conftest import MINIMAL_ENV

        set_env(**{**MINIMAL_ENV, "SCRAPER_RESPECT_ROBOTS": "true"})
        respx.get("https://dell.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://dell.com/blog").mock(
            return_value=httpx.Response(200, text=PAGE, headers=HTML_HEADERS)
        )

        with ArticleFetcher() as fetcher:
            assert fetcher.fetch("https://dell.com/blog").status_code == 200

    @respx.mock
    def test_robots_is_fetched_once_per_host(self, set_env) -> None:  # noqa: ANN001
        from tests.conftest import MINIMAL_ENV

        set_env(**{**MINIMAL_ENV, "SCRAPER_RESPECT_ROBOTS": "true"})
        robots = respx.get("https://dell.com/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
        )
        respx.get(url__regex=r"https://dell\.com/post\d").mock(
            return_value=httpx.Response(200, text=PAGE, headers=HTML_HEADERS)
        )

        with ArticleFetcher() as fetcher:
            for i in range(3):
                fetcher.fetch(f"https://dell.com/post{i}")

        assert robots.call_count == 1
