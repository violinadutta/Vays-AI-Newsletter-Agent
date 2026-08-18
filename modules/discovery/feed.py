"""Discovery via an RSS/Atom feed — the fallback source.

Used when the WordPress API is unavailable: the site moves off WordPress, the
REST API is disabled by a security plugin (common), or a WAF blocks `/wp-json`
while leaving `/feed/` alone.

Parsed with the standard library's ``xml.etree`` rather than ``feedparser``,
which would be a new dependency for one function. Feeds are small and the two
formats' differences are three tag names.

**A feed has no stable post identifier**, so ``external_id`` is left ``None`` and
de-duplication falls back to the URL. That is weaker — an edited slug looks like
a new post — which is precisely why this is the fallback and not the default.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from config import get_logger, get_settings
from core.exceptions import DiscoveryError
from core.models import DiscoveredPost
from modules.discovery.base import DiscoverySource

log = get_logger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"

#: A feed listing recent posts is a few tens of kilobytes. Anything vastly larger
#: is a misconfiguration or an attempt to exhaust memory, and either way is not
#: something to hand to a parser.
MAX_FEED_BYTES = 5 * 1024 * 1024

_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def _reject_doctype(payload: bytes, url: str) -> None:
    """Refuse any XML carrying a document type declaration.

    Entity-expansion attacks need an internal DTD subset to define the entities.
    No legitimate RSS or Atom feed has a DOCTYPE, so refusing them outright costs
    nothing and removes the attack class entirely — cheaper and more certain than
    reasoning about expansion limits.
    """
    if _DOCTYPE.search(payload[:4096]):
        raise DiscoveryError(
            f"feed at {url} contains a DOCTYPE declaration and was refused",
            context={"url": url, "reason": "doctype"},
        )


class FeedSource(DiscoverySource):
    """Lists posts from an RSS 2.0 or Atom feed."""

    name = "rss-feed"

    def __init__(self, feed_url: str, client: httpx.Client | None = None) -> None:
        self.feed_url = feed_url
        settings = get_settings().scraper
        self._client = client or httpx.Client(
            timeout=settings.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(self, limit: int) -> list[DiscoveredPost]:
        """Return up to ``limit`` entries, in feed order (newest first).

        Raises:
            DiscoveryError: The feed is unreachable, returns a non-200, or is not
                well-formed XML.
        """
        try:
            response = self._client.get(self.feed_url)
        except httpx.HTTPError as exc:
            raise DiscoveryError(
                f"feed unreachable at {self.feed_url}: {exc}",
                context={"url": self.feed_url, "error": type(exc).__name__},
            ) from exc

        if response.status_code != 200:
            raise DiscoveryError(
                f"feed returned {response.status_code} for {self.feed_url}",
                context={"url": self.feed_url, "status": response.status_code},
            )

        if len(response.content) > MAX_FEED_BYTES:
            raise DiscoveryError(
                f"feed at {self.feed_url} is {len(response.content):,} bytes, over the "
                f"{MAX_FEED_BYTES:,} limit",
                context={"url": self.feed_url, "bytes": len(response.content)},
            )

        _reject_doctype(response.content, self.feed_url)

        try:
            # S314: `xml.etree` does not resolve external entities, so XXE does
            # not apply. The remaining risk is entity-expansion DoS ("billion
            # laughs"), which requires an internal DTD subset — refused outright
            # by `_reject_doctype` above, and bounded again by the size cap. That
            # closes the hole without adding `defusedxml` for one call.
            root = ElementTree.fromstring(response.content)  # noqa: S314
        except ElementTree.ParseError as exc:
            raise DiscoveryError(
                f"feed at {self.feed_url} is not well-formed XML: {exc}",
                context={"url": self.feed_url},
            ) from exc

        posts = [
            post
            for post in (self._from_rss(item) for item in root.iter("item"))
            if post is not None
        ]
        if not posts:
            posts = [
                post
                for post in (self._from_atom(entry) for entry in root.iter(f"{_ATOM}entry"))
                if post is not None
            ]

        log.info("discovery.fetched", source=self.name, found=len(posts), url=self.feed_url)
        return posts[:limit]

    def _from_rss(self, item: ElementTree.Element) -> DiscoveredPost | None:
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        if not link:
            return None
        return DiscoveredPost(
            url=link,
            title=(title or link)[:512],
            external_id=None,  # RSS <guid> is not reliably a stable id
            published_at=_parse_rfc822(item.findtext("pubDate")),
            author=(item.findtext("{http://purl.org/dc/elements/1.1/}creator") or "").strip()
            or None,
            categories=[
                (c.text or "").strip() for c in item.iter("category") if (c.text or "").strip()
            ],
            source=self.name,
        )

    def _from_atom(self, entry: ElementTree.Element) -> DiscoveredPost | None:
        link_el = entry.find(f"{_ATOM}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        if not link:
            return None
        return DiscoveredPost(
            url=link.strip(),
            title=(title or link)[:512],
            external_id=(entry.findtext(f"{_ATOM}id") or "").strip() or None,
            published_at=_parse_iso(entry.findtext(f"{_ATOM}published")),
            author=None,
            categories=[c.get("term", "") for c in entry.iter(f"{_ATOM}category") if c.get("term")],
            source=self.name,
        )


def _parse_rfc822(value: str | None) -> datetime | None:
    """RSS dates are RFC 822 (``Fri, 07 Aug 2026 05:10:39 +0000``)."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_iso(value: str | None) -> datetime | None:
    """Atom dates are ISO-8601."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(re.sub(r"Z$", "+00:00", value.strip()))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
