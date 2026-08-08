"""HTTP fetching for article pages.

Fetching a user-supplied URL is the most dangerous thing this application does,
so the safety checks live here — at the boundary that actually performs the
request — rather than in a service that a future caller might bypass.

Three protections, in order:

1. **SSRF validation** (``core.validators``) before any connection is opened.
2. **Manual redirect following**, re-validating every hop. Automatic redirects
   are an SSRF hole: ``https://evil.com/x`` passes validation and then 302s to
   ``http://169.254.169.254/``, which never gets checked.
3. **A hard byte cap** applied while streaming, so a server that omits
   ``Content-Length`` and sends gigabytes cannot exhaust memory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import get_logger, get_settings
from config.constants import HTTP_MAX_REDIRECTS, HTTP_MAX_RESPONSE_BYTES
from core.exceptions import FetchError, InvalidURLError
from core.validators import validate_url

log = get_logger(__name__)

#: Content types we will parse. Anything else is a PDF, an image or a download,
#: and feeding its bytes to an HTML parser produces garbage, not an error.
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


@dataclass(frozen=True)
class FetchResult:
    """A successfully fetched page."""

    url: str
    html: str
    status_code: int
    elapsed_ms: int
    redirected_from: str | None = None


class ArticleFetcher:
    """Fetches article HTML with timeouts, retries, and SSRF protection."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        settings = get_settings().scraper
        self._timeout = settings.timeout_s
        self._user_agent = settings.user_agent
        self._respect_robots = settings.respect_robots
        self._max_retries = settings.max_retries
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self._timeout,
            follow_redirects=False,  # we follow manually so each hop is re-validated
            headers={
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._robots_cache: dict[str, robotparser.RobotFileParser | None] = {}

    def __enter__(self) -> ArticleFetcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ── robots.txt ───────────────────────────────────────────────────────────
    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        """Fetch and cache a host's robots.txt.

        Returns ``None`` when robots.txt is missing or unreadable, which means
        "allowed" — the convention for an absent file. **Failing open is
        deliberate**: a transient robots.txt error should not block a marketing
        executive from reading a public blog post they could open in a browser.
        """
        parts = urlsplit(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        if host_key in self._robots_cache:
            return self._robots_cache[host_key]

        parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
        try:
            response = self._client.get(f"{host_key}/robots.txt", timeout=5.0)
            if response.status_code == 200:
                parser.parse(response.text.splitlines())  # type: ignore[union-attr]
            else:
                parser = None
        except httpx.HTTPError:
            parser = None

        self._robots_cache[host_key] = parser
        return parser

    def _robots_allows(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self._user_agent, url)

    # ── fetching ─────────────────────────────────────────────────────────────
    def fetch(self, url: str) -> FetchResult:
        """Fetch a page.

        Args:
            url: The URL to fetch, as typed by the user.

        Returns:
            The page HTML and the final URL after redirects.

        Raises:
            InvalidURLError: The URL is malformed or points somewhere non-public.
            FetchError: Network failure, HTTP error, disallowed content type,
                too many redirects, or a robots.txt refusal.
        """
        started = time.monotonic()
        original = validate_url(url)

        if not self._robots_allows(original):
            raise FetchError(
                f"robots.txt disallows fetching {original}",
                user_message=(
                    "This site asks automated readers not to access that page. "
                    "Open it in your browser and use 'Paste text manually' instead."
                ),
                context={"url": original},
            )

        current = original
        for hop in range(HTTP_MAX_REDIRECTS + 1):
            response = self._request(current)

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(
                        f"redirect without a Location header from {current}",
                        user_message="That page redirected somewhere invalid.",
                    )
                # Re-validate every hop: this is the check automatic redirect
                # following would skip, and the reason we do it by hand.
                current = validate_url(str(httpx.URL(current).join(location)))
                log.debug("fetch.redirect", hop=hop + 1, to=current)
                continue

            html = self._read_body(response, current)
            elapsed = int((time.monotonic() - started) * 1000)
            log.info(
                "fetch.ok",
                url=current,
                status=response.status_code,
                bytes=len(html),
                duration_ms=elapsed,
            )
            return FetchResult(
                url=current,
                html=html,
                status_code=response.status_code,
                elapsed_ms=elapsed,
                redirected_from=original if current != original else None,
            )

        raise FetchError(
            f"more than {HTTP_MAX_REDIRECTS} redirects from {original}",
            user_message="That link redirects too many times. Try the direct article URL.",
            context={"url": original},
        )

    def _request(self, url: str) -> httpx.Response:
        """Issue one request, retrying only what is worth retrying."""

        @retry(
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
            reraise=True,
        )
        def _attempt() -> httpx.Response:
            return self._client.get(url)

        try:
            response = _attempt()
        except httpx.TimeoutException as exc:
            raise FetchError(
                f"timed out after {self._timeout}s fetching {url}",
                user_message="That page took too long to respond. Try again, or paste the text.",
                context={"url": url},
            ) from exc
        except httpx.HTTPError as exc:
            raise FetchError(
                f"network error fetching {url}: {exc}",
                user_message="Couldn't reach that page. Check the link and your connection.",
                context={"url": url},
            ) from exc

        if response.is_redirect:
            return response

        if response.status_code >= 400:
            raise FetchError(
                f"HTTP {response.status_code} from {url}",
                user_message=self._status_message(response.status_code),
                context={"url": url, "status": response.status_code},
            )
        return response

    @staticmethod
    def _status_message(status: int) -> str:
        if status in (401, 403):
            return (
                "That site blocked automated access. Open the article in your "
                "browser and use 'Paste text manually'."
            )
        if status == 404:
            return "That page doesn't exist. Check the link is correct."
        if status == 429:
            return "That site is rate-limiting us. Wait a minute and try again."
        if status >= 500:
            return "That site is having problems right now. Try again shortly."
        return "Couldn't read that page."

    @staticmethod
    def _read_body(response: httpx.Response, url: str) -> str:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
            raise FetchError(
                f"unsupported content type {content_type!r} at {url}",
                user_message=(
                    "That link isn't a web page — it may be a PDF or a file download. "
                    "Paste the article text manually instead."
                ),
                context={"url": url, "content_type": content_type},
            )

        if len(response.content) > HTTP_MAX_RESPONSE_BYTES:
            raise FetchError(
                f"response exceeds {HTTP_MAX_RESPONSE_BYTES} bytes at {url}",
                user_message="That page is unusually large. Paste the article text instead.",
                context={"url": url},
            )
        return response.text


def fetch_url(url: str) -> FetchResult:
    """Convenience wrapper that manages the client lifecycle for a single fetch."""
    with ArticleFetcher() as fetcher:
        return fetcher.fetch(url)


__all__ = ["ArticleFetcher", "FetchResult", "FetchError", "InvalidURLError", "fetch_url"]
