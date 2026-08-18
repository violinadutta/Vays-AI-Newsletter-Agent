"""What a discovery source is, and the contract every one of them keeps.

A source answers one question: *which posts exist on this site right now?* It
does **not** decide which are new — that is the repository's job, because
"newness" is a property of our database, not of the site.

Keeping those apart is what lets the WordPress and feed sources stay simple
enough to be obviously correct, and what makes de-duplication testable without a
network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import DiscoveredPost


class DiscoverySource(ABC):
    """Something that can list the posts published on a site."""

    #: Recorded on each post so a fallback from one source to another is visible
    #: in the logs rather than inferred from a change in the data.
    name: str = "unknown"

    @abstractmethod
    def fetch(self, limit: int) -> list[DiscoveredPost]:
        """Return up to ``limit`` posts, newest first.

        **Must not raise for an empty result.** A site with no posts yet is a
        valid state, not an error, and treating it as one would fill the log
        with failures on a quiet week.

        Raises:
            DiscoveryError: The source is unreachable or returned something
                unparseable. The caller decides whether to try another source.
        """

    def close(self) -> None:  # noqa: B027 - intentionally optional
        """Release any held resources. Concrete and empty so a source holding
        none is not forced to write a stub."""
