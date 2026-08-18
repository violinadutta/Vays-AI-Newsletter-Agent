"""Discovery via the WordPress REST API.

**Why this rather than scraping the blog index.** `vaysinfotech.com` runs
WordPress, which exposes `/wp-json/wp/v2/posts` — a documented, stable JSON API
returning exactly the metadata this pipeline needs. Parsing the HTML listing
instead would mean a CSS-selector guess that breaks the next time the theme is
updated, and would still not yield a stable identifier.

The identifier matters more than it looks. A WordPress post ID never changes,
while a slug can be edited after publication — correcting a typo in a headline
rewrites the URL. De-duplicating on the URL alone would re-process that post and
send a second newsletter about it.

No API key, no rate limit for this volume, no scraping service. One GET.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from config import get_logger, get_settings
from core.exceptions import DiscoveryError
from core.models import DiscoveredPost
from modules.discovery.base import DiscoverySource

log = get_logger(__name__)

#: Only the fields used below. WordPress returns the full rendered post body by
#: default — 100 KB+ for a handful of posts — and we re-fetch the article through
#: the existing extractor anyway, so asking for it here would be wasted transfer.
_FIELDS = "id,date_gmt,link,title,categories,author"


class WordPressSource(DiscoverySource):
    """Lists published posts from a WordPress site's REST API."""

    name = "wordpress-api"

    def __init__(self, site_url: str, client: httpx.Client | None = None) -> None:
        self.site_url = site_url.rstrip("/")
        settings = get_settings().scraper
        self._client = client or httpx.Client(
            timeout=settings.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self, limit: int) -> list[DiscoveredPost]:
        """Return up to ``limit`` published posts, newest first.

        Raises:
            DiscoveryError: The endpoint is unreachable, returns a non-200, or
                returns something that is not a JSON array. The caller may then
                try the feed source instead.
        """
        url = f"{self.site_url}/wp-json/wp/v2/posts"
        params: dict[str, str | int] = {
            "per_page": max(1, min(limit, 100)),  # WordPress caps per_page at 100
            "status": "publish",
            "orderby": "date",
            "order": "desc",
            "_fields": _FIELDS,
        }

        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise DiscoveryError(
                f"WordPress API unreachable at {url}: {exc}",
                context={"url": url, "error": type(exc).__name__},
            ) from exc

        if response.status_code != 200:
            raise DiscoveryError(
                f"WordPress API returned {response.status_code} for {url}",
                context={"url": url, "status": response.status_code},
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise DiscoveryError(
                f"WordPress API returned non-JSON from {url}",
                context={"url": url},
            ) from exc

        if not isinstance(payload, list):
            # A WordPress error is a JSON *object* with a "code" key, not a list.
            # Treating it as an empty result would silently report "no new posts"
            # forever, which is the worst possible failure for a scheduled job.
            raise DiscoveryError(
                f"WordPress API returned {type(payload).__name__}, expected a list",
                context={"url": url, "code": str(payload.get("code", ""))[:80]}
                if isinstance(payload, dict)
                else {"url": url},
            )

        posts = [p for p in (self._to_post(item) for item in payload) if p is not None]
        log.info("discovery.fetched", source=self.name, found=len(posts), url=url)
        return posts

    def _to_post(self, item: Any) -> DiscoveredPost | None:  # noqa: ANN401 - arbitrary JSON
        """Convert one API item, or ``None`` if it is unusable.

        A single malformed entry must not lose the whole run — the others are
        still perfectly good, and a scheduled job that returns nothing because of
        one bad record is worse than one that returns most of them.
        """
        if not isinstance(item, dict):
            return None

        link = str(item.get("link") or "").strip()
        if not link:
            return None

        title = item.get("title")
        rendered = title.get("rendered") if isinstance(title, dict) else title
        clean_title = _unescape(str(rendered or "").strip()) or link

        return DiscoveredPost(
            url=link,
            title=clean_title[:512],
            external_id=str(item["id"]) if item.get("id") is not None else None,
            published_at=_parse_date(item.get("date_gmt")),
            author=str(item["author"]) if item.get("author") is not None else None,
            categories=[str(c) for c in item.get("categories") or []],
            source=self.name,
        )


def _parse_date(value: Any) -> datetime | None:  # noqa: ANN401
    """Parse WordPress's ``date_gmt``, which is ISO-8601 without a timezone.

    It is documented as UTC, so the timezone is attached explicitly here —
    leaving it naive would make every later comparison against an aware
    ``now(UTC)`` raise.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _unescape(text: str) -> str:
    """Decode HTML entities WordPress puts in rendered titles (``&#8217;``)."""
    import html

    return html.unescape(text)
