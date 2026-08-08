"""Extraction strategy contract.

Strategies return ``None`` rather than raising when they cannot handle a page.
In a cascade, "this tier didn't work" is ordinary control flow, not an error —
reserving exceptions for genuine failures keeps the orchestrator readable and
keeps the logs honest (a Trafilatura miss is a WARNING, not an ERROR).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.enums import ExtractorTier
from core.models import ExtractedArticle


@runtime_checkable
class ExtractorStrategy(Protocol):
    """One tier of the extraction cascade."""

    tier: ExtractorTier

    def extract(self, html: str, url: str | None) -> ExtractedArticle | None:
        """Pull an article out of ``html``.

        Args:
            html: The raw page source.
            url: The source URL, used for metadata resolution. May be ``None``
                for pasted HTML.

        Returns:
            The extracted article, or ``None`` if this strategy could not find
            usable content. Must not raise for ordinary "bad page" conditions.
        """
        ...
