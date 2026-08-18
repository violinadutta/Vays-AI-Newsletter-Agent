"""Source selection, and the fallback order between them.

One place knows which concrete sources exist — the same shape as
``modules.ai.factory`` and ``modules.email.factory``, for the same reason:
adding a source should mean adding a class and one line here, not editing the
caller.
"""

from __future__ import annotations

from config import get_logger
from core.exceptions import DiscoveryError
from core.models import DiscoveredPost
from modules.discovery.base import DiscoverySource
from modules.discovery.feed import FeedSource
from modules.discovery.wordpress import WordPressSource

log = get_logger(__name__)


def build_sources(site_url: str) -> list[DiscoverySource]:
    """Sources to try, in order of preference.

    The WordPress API comes first because it supplies a stable post ID, which is
    what makes de-duplication exact. The feed is the fallback: it works when
    ``/wp-json`` is disabled by a security plugin — a common WordPress hardening
    step — but identifies posts only by URL.
    """
    base = site_url.rstrip("/")
    return [WordPressSource(base), FeedSource(f"{base}/feed/")]


def discover_posts(site_url: str, limit: int) -> list[DiscoveredPost]:
    """Fetch posts from the first source that answers.

    Each source is closed whether or not it succeeded — a leaked httpx client in
    a process that runs every few hours for weeks is a slow resource leak that
    would be attributed to something else entirely.

    Raises:
        DiscoveryError: Every source failed. Carries the last error, so the log
            names the actual reason rather than "all sources failed".
    """
    sources = build_sources(site_url)
    last_error: DiscoveryError | None = None

    try:
        for source in sources:
            try:
                posts = source.fetch(limit)
            except DiscoveryError as exc:
                log.warning(
                    "discovery.source_failed",
                    source=source.name,
                    reason=exc.message[:200],
                )
                last_error = exc
                continue

            if posts:
                return posts

            # An empty result is not a failure — a site may genuinely have no
            # posts. But it is worth trying the next source before concluding
            # that, because a disabled API can return 200 with an empty array.
            log.info("discovery.source_empty", source=source.name)
    finally:
        for source in sources:
            source.close()

    if last_error is not None:
        raise last_error
    return []
