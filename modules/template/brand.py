"""Brand assets and the legal furniture every marketing email must carry.

The address check here is not fussiness. CAN-SPAM and equivalent regimes require a
valid physical postal address in commercial email, and an unsubscribe mechanism.
Both are enforced at render time rather than left to a template author to
remember, because forgetting them is invisible until it is a compliance problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import get_settings
from config.constants import PROJECT_ROOT
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
        logo_url=resolve_logo_url(logo),
        website=website or None,
        address=address,
        unsubscribe_url=unsubscribe,
    )


#: Content-ID for the embedded logo. Referenced by the templates as
#: ``cid:vays-logo`` and attached by the delivery service.
LOGO_CID = "vays-logo"


def resolve_logo_url(logo_path: str | None) -> str | None:
    """Turn the configured logo setting into something an email client can load.

    Three cases, and only two of them used to work:

    * an ``http(s)`` URL — used as-is, which is what a hosted asset needs
    * a **local file path** — becomes ``cid:vays-logo``, and the delivery layer
      attaches the bytes as a related part. A filesystem path left in ``src``
      renders as a broken image in every mail client, which is what was
      happening: ``<img src="assets/logo.png">`` means nothing in an inbox.
    * empty or missing — ``None``, and the templates fall back to the brand name
      as text rather than showing a broken image icon

    A configured path that does not exist on disk returns ``None`` rather than
    raising: a missing logo is a cosmetic problem and must never be the reason a
    campaign cannot be sent.
    """
    if not logo_path or not logo_path.strip():
        return None

    candidate = logo_path.strip()
    if candidate.startswith(("http://", "https://", "cid:")):
        return candidate

    return f"cid:{LOGO_CID}" if logo_file(candidate) is not None else None


def logo_file(logo_path: str | None) -> Path | None:
    """Resolve the logo setting to a readable file, or ``None``.

    Relative paths are taken from the project root so the app behaves the same
    whichever directory it was launched from — a real difference on Windows,
    where a shortcut's "start in" folder is easy to get wrong.
    """
    if not logo_path or not logo_path.strip():
        return None
    if logo_path.startswith(("http://", "https://", "cid:")):
        return None

    candidate = Path(logo_path.strip())
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate if candidate.is_file() else None
