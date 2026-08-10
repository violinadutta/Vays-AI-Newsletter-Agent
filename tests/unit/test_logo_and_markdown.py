"""The logo actually reaching the inbox, and markdown never doing so.

Both bugs here were found in a real delivered email, not by a test — which is
why each test is named after the symptom the recipient saw.
"""

from __future__ import annotations

from email import policy
from email.message import EmailMessage as MIMEMessage
from email.parser import BytesParser
from pathlib import Path

import pytest

from core.models import EmailMessage, InlineImage
from modules.email.base import build_mime_body
from modules.template.brand import LOGO_CID, logo_file, resolve_logo_url
from modules.template.renderer import _split_paragraphs, strip_markdown

# A one-pixel PNG. Enough to prove the MIME plumbing without a fixture file.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


# ─────────────────────────────────────────────────────────────────────────────
#  Logo resolution
# ─────────────────────────────────────────────────────────────────────────────
class TestLogoUrl:
    def test_a_filesystem_path_becomes_a_cid_reference(self, tmp_path: Path) -> None:
        """The original bug: `assets/logo.png` went straight into `<img src>`,
        which resolves to nothing in every mail client on earth."""
        logo = tmp_path / "logo.png"
        logo.write_bytes(PNG)

        assert resolve_logo_url(str(logo)) == f"cid:{LOGO_CID}"

    def test_a_hosted_url_is_left_alone(self) -> None:
        """A hosted asset is the better option when one exists, and must not be
        rewritten into a cid that nothing will attach."""
        url = "https://vaysinfotech.com/logo.png"

        assert resolve_logo_url(url) == url

    def test_a_missing_file_yields_no_logo_rather_than_a_broken_image(self) -> None:
        """A cid: reference with nothing attached renders as a broken-image icon
        in the customer's inbox — worse than the text fallback."""
        assert resolve_logo_url("assets/does-not-exist.png") is None

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_no_logo_configured_is_not_an_error(self, value: str | None) -> None:
        assert resolve_logo_url(value) is None

    def test_a_relative_path_resolves_from_the_project_root(self) -> None:
        """Not the working directory — the app must behave the same however it
        was launched, which on Windows is easy to get wrong via a shortcut."""
        assert logo_file("templates/email/modern.html") is not None

    def test_a_url_is_not_treated_as_a_file(self) -> None:
        assert logo_file("https://vaysinfotech.com/logo.png") is None


# ─────────────────────────────────────────────────────────────────────────────
#  MIME embedding
# ─────────────────────────────────────────────────────────────────────────────
def _build(images: list[InlineImage]) -> MIMEMessage:
    mime = MIMEMessage()
    mime["From"] = "newsletter@vays.com"
    mime["To"] = "priya@example.com"
    mime["Subject"] = "Test"
    build_mime_body(
        mime,
        EmailMessage(
            to_email="priya@example.com",
            subject="Test",
            html=f'<p><img src="cid:{LOGO_CID}" /></p>',
            text="Test",
            inline_images=images,
        ),
    )
    return mime


class TestInlineImages:
    def test_without_images_the_message_stays_a_plain_alternative(self) -> None:
        mime = _build([])

        assert mime.get_content_type() == "multipart/alternative"
        assert [p.get_content_type() for p in mime.iter_parts()] == ["text/plain", "text/html"]

    def test_an_image_is_attached_as_related_to_the_html_part(self) -> None:
        """`multipart/related` is what binds a `cid:` reference to the bytes.
        Attached at the top level instead, it would be an ordinary attachment and
        the reference would resolve to nothing."""
        mime = _build([InlineImage(content_id=LOGO_CID, data=PNG)])

        html_part = list(mime.iter_parts())[-1]
        assert html_part.get_content_type() == "multipart/related"
        assert [p.get_content_type() for p in html_part.iter_parts()] == ["text/html", "image/png"]

    def test_the_content_id_matches_the_cid_in_the_html(self) -> None:
        """Off-by-one on the angle brackets is the classic way to get a broken
        image that looks correctly configured."""
        mime = _build([InlineImage(content_id=LOGO_CID, data=PNG)])

        image = list(list(mime.iter_parts())[-1].iter_parts())[-1]
        assert image["Content-ID"] == f"<{LOGO_CID}>"
        assert image.get_content_disposition() == "inline"

    def test_the_plain_text_alternative_survives_embedding(self) -> None:
        """Losing the text part to gain a logo would trade a cosmetic win for a
        measurable spam-score penalty."""
        mime = _build([InlineImage(content_id=LOGO_CID, data=PNG)])

        assert mime.get_body(preferencelist=("plain",)) is not None

    def test_the_message_round_trips_through_a_parser(self) -> None:
        parsed = BytesParser(policy=policy.default).parsebytes(
            _build([InlineImage(content_id=LOGO_CID, data=PNG)]).as_bytes()
        )

        assert parsed.get_body(preferencelist=("html",)) is not None
        assert any(part.get_content_type() == "image/png" for part in parsed.walk())


# ─────────────────────────────────────────────────────────────────────────────
#  Markdown never reaching a recipient
# ─────────────────────────────────────────────────────────────────────────────
class TestMarkdownInBody:
    def test_bold_markers_become_real_bold_not_visible_asterisks(self) -> None:
        """The reported bug: `**Dell**` arrived in the inbox with the asterisks
        showing, because the prompt asked for a bold heading and plain text has
        no other way to express one."""
        [para] = _split_paragraphs("**Dell PowerEdge** refreshed the line.")

        assert "<strong>Dell PowerEdge</strong>" in str(para)
        assert "*" not in str(para)

    def test_a_hash_heading_becomes_bold(self) -> None:
        [para] = _split_paragraphs("## Cisco Catalyst")

        assert str(para) == "<strong>Cisco Catalyst</strong>"

    def test_bullets_become_visible_bullets(self) -> None:
        [para] = _split_paragraphs("- AI ops\n- No new hardware")

        assert str(para).count("&bull;") == 2
        assert not str(para).lstrip().startswith("-")

    def test_a_line_break_inside_a_paragraph_is_preserved(self) -> None:
        """Without this the heading line and the copy beneath it run together
        into one sentence, because HTML collapses a bare newline."""
        [para] = _split_paragraphs("**Dell**\nDell announced a refresh.")

        assert "<br />" in str(para)

    def test_a_literal_asterisk_is_not_mangled(self) -> None:
        """`5 * 3` and a footnote marker are not emphasis. Guessing wrong here
        corrupts copy, which is why single-asterisk emphasis is unsupported."""
        [para] = _split_paragraphs("Throughput rose 5 * 3 times in the benchmark.")

        assert "5 * 3" in str(para)

    def test_html_in_the_body_is_still_escaped(self) -> None:
        """The security property: escaping happens *before* our tags are
        inserted, so markdown support cannot become an injection hole."""
        [para] = _split_paragraphs("<script>alert(1)</script> and **bold**")

        rendered = str(para)
        assert "<script>" not in rendered
        assert "&lt;script&gt;" in rendered
        assert "<strong>bold</strong>" in rendered

    def test_an_injected_strong_tag_does_not_survive(self) -> None:
        [para] = _split_paragraphs("<strong onmouseover='x'>hi</strong>")

        assert "onmouseover" not in str(para) or "&lt;strong" in str(para)


class TestMarkdownInPlainText:
    def test_markers_are_removed_not_rendered(self) -> None:
        """The text alternative has no bold, so the markers are pure noise."""
        assert strip_markdown("**Dell** shipped it.") == "Dell shipped it."

    def test_a_heading_line_keeps_its_words(self) -> None:
        assert strip_markdown("## Cisco Catalyst") == "Cisco Catalyst"

    def test_bullets_keep_a_marker(self) -> None:
        """A list that reads as running prose is worse than one with dashes."""
        assert strip_markdown("* AI ops\n* No new hardware") == "- AI ops\n- No new hardware"
