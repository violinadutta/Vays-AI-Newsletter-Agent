"""Brand assets and the legal furniture every marketing email must carry.

The address check here is not fussiness. CAN-SPAM and equivalent regimes require a
valid physical postal address in commercial email, and an unsubscribe mechanism.
Both are enforced at render time rather than left to a template author to
remember, because forgetting them is invisible until it is a compliance problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import get_settings
from core.exceptions import TemplateError
from core.models import BrandConfig

_HEX_COLOUR = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class ResolvedBrand:
    """Brand values validated and ready for a template."""

    name: str
    primary_color: str
    text_on_primary: str
    logo_url: str | None
    website: str | None
    address: str
    unsubscribe_url: str

    @property
    def year(self) -> int:
        from datetime import UTC, datetime

        return datetime.now(UTC).year


def _contrasting_text(hex_colour: str) -> str:
    """Pick black or white text for a background, by relative luminance.

    A brand colour is configurable, so a pale one would otherwise produce white
    text on a pale button — invisible, and only discovered by a recipient.
    """
    red, green, blue = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#14181F" if luminance > 0.6 else "#FFFFFF"


def resolve_brand(override: BrandConfig | None = None) -> ResolvedBrand:
    """Load brand settings and verify an email may legally be built from them.

    Raises:
        TemplateError: If the postal address or unsubscribe URL is missing, or
            the brand colour is not a valid hex value.
    """
    settings = get_settings().brand
    name = override.name if override else settings.name
    colour = override.primary_color if override else settings.primary_color
    address = (override.address if override else settings.address or "").strip()
    unsubscribe = (
        override.unsubscribe_base_url if override else settings.unsubscribe_base_url
    ).strip()
    logo = override.logo_path if override else settings.logo_path
    website = override.website if override else settings.website

    if not _HEX_COLOUR.match(colour):
        raise TemplateError(
            f"brand colour {colour!r} is not a 6-digit hex value",
            user_message=(
                "The brand colour isn't valid. Set it in Settings → Branding, for example #0B5FFF."
            ),
        )

    if not address:
        raise TemplateError(
            "BRAND_ADDRESS is empty; marketing email requires a physical postal address",
            user_message=(
                "A postal address is required in every marketing email by law. "
                "Add one in Settings → Branding before sending."
            ),
        )

    if not unsubscribe:
        raise TemplateError(
            "no unsubscribe URL configured",
            user_message=(
                "An unsubscribe link is required in every marketing email. "
                "Set the unsubscribe URL in Settings → Branding."
            ),
        )

    return ResolvedBrand(
        name=name,
        primary_color=colour,
        text_on_primary=_contrasting_text(colour),
        logo_url=logo or None,
        website=website or None,
        address=address,
        unsubscribe_url=unsubscribe,
    )
