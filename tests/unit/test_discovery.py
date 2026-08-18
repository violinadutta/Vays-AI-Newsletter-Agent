"""Discovery sources: parsing, fallback, and refusing to fail silently.

The failure this file most guards against is a **silent empty result**. A
scheduled job that reports "no new posts" when the API is actually broken looks
identical to a quiet week, and nobody investigates it. Every source therefore
raises on a malformed response rather than returning ``[]``.

All tests are offline — real HTTP is exercised separately and marked ``network``.
"""

from __future__ import annotations

import httpx
import pytest

from core.exceptions import DiscoveryError
from modules.discovery.factory import discover_posts
from modules.discovery.feed import FeedSource
from modules.discovery.wordpress import WordPressSource

WP_JSON = [
    {
        "id": 12694,
        "date_gmt": "2026-08-07T05:10:39",
        "link": "https://vaysinfotech.com/ai-security-new-cybersecurity-category/",
        "title": {"rendered": "AI Security Is Becoming a New Category"},
        "categories": [1, 7],
        "author": 2,
    },
    {
        "id": 12663,
        "date_gmt": "2026-07-24T09:56:58",
        "link": "https://vaysinfotech.com/amd-advancing-ai-2026/",
        "title": {"rendered": "AMD&#8217;s rack-scale AI proposition"},
        "categories": [1],
        "author": 2,
    },
]

RSS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>AI Security Is Becoming a New Category</title>
      <link>https://vaysinfotech.com/ai-security-new-cybersecurity-category/</link>
      <pubDate>Fri, 07 Aug 2026 05:10:39 +0000</pubDate>
      <category>Security</category>
      <dc:creator>Vays</dc:creator>
    </item>
    <item>
      <title>Rugged firewall TCO</title>
      <link>https://vaysinfotech.com/rugged-industrial-firewall-tco/</link>
      <pubDate>Wed, 02 Jul 2026 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:vaysinfotech.com,2026:post-1</id>
    <title>An Atom post</title>
    <link href="https://vaysinfotech.com/atom-post/"/>
    <published>2026-08-01T10:00:00Z</published>
    <category term="Cloud"/>
  </entry>
</feed>
"""


def client_returning(*responses: httpx.Response) -> httpx.Client:
    """An httpx client that replays the given responses in order."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if queue else httpx.Response(500)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ─────────────────────────────────────────────────────────────────────────────
#  WordPress REST API
# ─────────────────────────────────────────────────────────────────────────────
class TestWordPressSource:
    def test_it_parses_posts(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = WordPressSource(
            "https://vaysinfotech.com", client=client_returning(httpx.Response(200, json=WP_JSON))
        )

        posts = source.fetch(10)

        assert len(posts) == 2
        assert posts[0].url.endswith("/ai-security-new-cybersecurity-category/")
        assert posts[0].source == "wordpress-api"

    def test_the_post_id_is_captured(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """The whole reason this source is preferred over the feed: a stable id
        survives a slug edit, so a retitled post is not treated as new."""
        source = WordPressSource(
            "https://vaysinfotech.com", client=client_returning(httpx.Response(200, json=WP_JSON))
        )

        assert [p.external_id for p in source.fetch(10)] == ["12694", "12663"]

    def test_html_entities_in_titles_are_decoded(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """WordPress renders `'` as `&#8217;`. Left encoded it would reach the
        approval email and the newsletter looking broken."""
        source = WordPressSource(
            "https://vaysinfotech.com", client=client_returning(httpx.Response(200, json=WP_JSON))
        )

        title = source.fetch(10)[1].title

        assert "’s rack-scale" in title
        assert "&#8217;" not in title

    def test_dates_are_timezone_aware(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """`date_gmt` is documented UTC but serialised without an offset. Left
        naive, every later comparison against an aware now() would raise."""
        published = (
            WordPressSource(
                "https://vaysinfotech.com",
                client=client_returning(httpx.Response(200, json=WP_JSON)),
            )
            .fetch(10)[0]
            .published_at
        )

        assert published is not None
        assert published.tzinfo is not None

    def test_a_non_200_raises_rather_than_reporting_no_posts(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """The silent-empty failure: "no new posts" and "the API is down" must
        never look the same to a scheduled job."""
        source = WordPressSource(
            "https://vaysinfotech.com", client=client_returning(httpx.Response(503))
        )

        with pytest.raises(DiscoveryError):
            source.fetch(10)

    def test_a_wordpress_error_object_raises(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """A disabled REST API returns 200 with a JSON *object*, not a list.
        Treating that as an empty result would report "nothing new" forever."""
        source = WordPressSource(
            "https://vaysinfotech.com",
            client=client_returning(
                httpx.Response(200, json={"code": "rest_no_route", "message": "No route"})
            ),
        )

        with pytest.raises(DiscoveryError, match="expected a list"):
            source.fetch(10)

    def test_non_json_raises(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = WordPressSource(
            "https://vaysinfotech.com",
            client=client_returning(httpx.Response(200, text="<html>login</html>")),
        )

        with pytest.raises(DiscoveryError):
            source.fetch(10)

    def test_one_malformed_entry_does_not_lose_the_others(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        payload = [{"id": 1, "link": "", "title": {"rendered": "no link"}}, WP_JSON[0]]
        source = WordPressSource(
            "https://vaysinfotech.com", client=client_returning(httpx.Response(200, json=payload))
        )

        assert len(source.fetch(10)) == 1

    def test_an_empty_site_is_not_an_error(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = WordPressSource(
            "https://vaysinfotech.com", client=client_returning(httpx.Response(200, json=[]))
        )

        assert source.fetch(10) == []

    def test_a_network_failure_raises_discovery_error(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        source = WordPressSource(
            "https://vaysinfotech.com", client=httpx.Client(transport=httpx.MockTransport(boom))
        )

        with pytest.raises(DiscoveryError):
            source.fetch(10)


# ─────────────────────────────────────────────────────────────────────────────
#  Feed fallback
# ─────────────────────────────────────────────────────────────────────────────
class TestFeedSource:
    def test_it_parses_rss(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=RSS_XML)),
        )

        posts = source.fetch(10)

        assert len(posts) == 2
        assert posts[0].categories == ["Security"]
        assert posts[0].author == "Vays"
        assert posts[0].published_at is not None

    def test_it_parses_atom(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=ATOM_XML)),
        )

        posts = source.fetch(10)

        assert len(posts) == 1
        assert posts[0].url == "https://vaysinfotech.com/atom-post/"

    def test_rss_gives_no_stable_id(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """Documented weakness, asserted so it is a known property rather than a
        surprise: URL de-duplication is the fallback for a reason."""
        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=RSS_XML)),
        )

        assert all(p.external_id is None for p in source.fetch(10))

    def test_a_doctype_is_refused(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        """Entity-expansion attacks need an internal DTD subset. No real feed has
        a DOCTYPE, so refusing them removes the attack class outright."""
        payload = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaa">]><rss></rss>'
        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=payload)),
        )

        with pytest.raises(DiscoveryError, match="DOCTYPE"):
            source.fetch(10)

    def test_an_oversized_feed_is_refused_before_parsing(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        from modules.discovery.feed import MAX_FEED_BYTES

        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=b"x" * (MAX_FEED_BYTES + 1))),
        )

        with pytest.raises(DiscoveryError, match="over the"):
            source.fetch(10)

    def test_malformed_xml_raises(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=b"<rss><channel>")),
        )

        with pytest.raises(DiscoveryError, match="well-formed"):
            source.fetch(10)

    def test_the_limit_is_respected(self, minimal_settings) -> None:  # noqa: ANN001, ARG002
        source = FeedSource(
            "https://vaysinfotech.com/feed/",
            client=client_returning(httpx.Response(200, content=RSS_XML)),
        )

        assert len(source.fetch(1)) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Fallback between sources
# ─────────────────────────────────────────────────────────────────────────────
class TestFallback:
    def test_the_feed_is_used_when_the_api_is_disabled(self, minimal_settings, monkeypatch) -> None:  # noqa: ANN001, ARG002
        """The realistic failure: a security plugin blocks /wp-json but leaves
        /feed/ working. Discovery must keep running."""
        from modules.discovery import factory

        def sources(_site: str) -> list[object]:
            return [
                WordPressSource("https://x", client=client_returning(httpx.Response(403))),
                FeedSource(
                    "https://x/feed/", client=client_returning(httpx.Response(200, content=RSS_XML))
                ),
            ]

        monkeypatch.setattr(factory, "build_sources", sources)

        posts = discover_posts("https://x", limit=10)

        assert len(posts) == 2
        assert posts[0].source == "rss-feed"

    def test_every_source_failing_raises_the_last_reason(
        self, minimal_settings, monkeypatch
    ) -> None:  # noqa: ANN001, ARG002
        """ "All sources failed" is useless in a log; the actual status code is
        what tells you whether to look at the network or the plugin."""
        from modules.discovery import factory

        def sources(_site: str) -> list[object]:
            return [
                WordPressSource("https://x", client=client_returning(httpx.Response(403))),
                FeedSource("https://x/feed/", client=client_returning(httpx.Response(418))),
            ]

        monkeypatch.setattr(factory, "build_sources", sources)

        with pytest.raises(DiscoveryError, match="418"):
            discover_posts("https://x", limit=10)
