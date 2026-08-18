"""Automatic discovery of newly published blog posts (the agent's first tool).

Sources are tried in order — WordPress REST API first for its stable post IDs,
RSS feed as the fallback. Deciding which posts are *new* deliberately lives in
the repository, not here.
"""

from modules.discovery.base import DiscoverySource
from modules.discovery.factory import build_sources, discover_posts
from modules.discovery.feed import FeedSource
from modules.discovery.wordpress import WordPressSource

__all__ = [
    "DiscoverySource",
    "FeedSource",
    "WordPressSource",
    "build_sources",
    "discover_posts",
]
