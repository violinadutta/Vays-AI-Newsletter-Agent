"""The LUNA palette, and the two properties a recolour must not break.

**Contrast**, because a palette that looks right and reads badly is a
regression nobody notices until someone squints at a status chip; and
**semantic distinctness**, because a campaign that FAILED and one that SENT must
never be two shades of the same blue.

Ratios are computed rather than eyeballed — WCAG contrast is arithmetic, and
guessing it from a swatch is how inaccessible palettes ship.
"""

from __future__ import annotations

import pytest

from ui import styles


def _luminance(hex_colour: str) -> float:
    """Relative luminance, per WCAG 2.1."""
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


AA_NORMAL = 4.5

PALETTE = (
    styles.LUNA_LIGHTEST,
    styles.LUNA_CYAN,
    styles.LUNA_BLUE,
    styles.LUNA_DARK,
    styles.LUNA_DARKEST,
)


class TestThePalette:
    def test_the_five_luna_tones_are_present(self) -> None:
        assert PALETTE == ("#A7EBF2", "#54ACBF", "#26658C", "#023859", "#011C40")

    def test_the_primary_matches_the_streamlit_theme(self) -> None:
        """Two files carry this colour. If they disagree, buttons and links
        drift apart and nobody can see why."""
        from pathlib import Path

        from config.constants import PROJECT_ROOT

        config = Path(PROJECT_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")

        assert f'primaryColor = "{styles.BRAND_PRIMARY}"' in config
        assert f'backgroundColor = "{styles.LUNA_DARKEST}"' in config
        assert f'secondaryBackgroundColor = "{styles.LUNA_DARK}"' in config


class TestReadability:
    @pytest.mark.parametrize(
        ("name", "colour"),
        [
            ("body text", styles.TEXT_PRIMARY),
            ("muted text", styles.TEXT_MUTED),
            ("headings", styles.LUNA_LIGHTEST),
            ("links", styles.LUNA_CYAN),
        ],
    )
    def test_text_is_legible_on_the_page_background(self, name: str, colour: str) -> None:
        ratio = contrast(colour, styles.LUNA_DARKEST)

        assert ratio >= AA_NORMAL, f"{name} {colour} is only {ratio:.1f}:1 on the background"

    def test_body_text_is_legible_on_cards_too(self) -> None:
        """Cards use the second-darkest tone, so text has to clear both."""
        assert contrast(styles.TEXT_PRIMARY, styles.LUNA_DARK) >= AA_NORMAL

    @pytest.mark.parametrize("status", sorted(styles.STATUS_COLORS))
    def test_every_status_chip_is_readable(self, status: str) -> None:
        """Chips carry dark label text on a light fill. ARCHIVED failed this on
        the first pass at 3.4:1 — which is exactly why it is asserted."""
        ratio = contrast(styles.LUNA_DARKEST, styles.STATUS_COLORS[status])

        assert ratio >= AA_NORMAL, f"{status} chip is only {ratio:.1f}:1"

    def test_muted_text_is_dimmer_than_body_text(self) -> None:
        """Otherwise the hierarchy the two exist to express is not there."""
        assert _luminance(styles.TEXT_MUTED) < _luminance(styles.TEXT_PRIMARY)


class TestStatusStaysMeaningful:
    def test_every_campaign_state_has_a_colour(self) -> None:
        """A state added without one falls back to grey and silently stops
        being distinguishable."""
        from core.enums import CampaignStatus

        for status in CampaignStatus:
            assert str(status) in styles.STATUS_COLORS, f"{status} has no chip colour"

    def test_success_and_failure_are_not_the_same_hue(self) -> None:
        """The whole reason status colours are not taken from a blue palette."""
        assert styles.STATUS_COLORS["SENT"] != styles.STATUS_COLORS["FAILED"]
        assert contrast(styles.STATUS_COLORS["SENT"], styles.STATUS_COLORS["FAILED"]) > 1.2

    def test_approved_and_rejected_are_distinguishable(self) -> None:
        assert styles.STATUS_COLORS["APPROVED"] != styles.STATUS_COLORS["REJECTED"]

    def test_a_chip_carries_its_label_not_only_a_colour(self) -> None:
        """Colour alone excludes anyone who cannot distinguish it (UI spec §10)."""
        html = styles.status_chip("AWAITING_APPROVAL")

        assert "AWAITING APPROVAL" in html

    def test_health_dots_state_online_or_offline_in_words(self) -> None:
        assert "online" in styles.health_dot(healthy=True, label="AI service")
        assert "offline" in styles.health_dot(healthy=False, label="AI service")
