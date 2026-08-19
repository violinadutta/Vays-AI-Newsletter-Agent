"""Email rendering tests.

The compliance and escaping cases are the ones that matter. A missing unsubscribe
link is a legal problem discovered by a recipient; an unescaped `<script>` from a
scraped article is a security problem discovered by nobody until it matters.
"""

from __future__ import annotations

import pytest

from core.enums import Category, Tone
from core.exceptions import TemplateError
from core.models import BrandConfig, NewsletterContent, Recipient
from modules.template.brand import _contrasting_text, resolve_brand
from modules.template.renderer import TemplateRenderer

ADDRESS = "Vays Infotech, 4th Floor, Tech Park, Pune 411045, India"
TEMPLATE_IDS = ["modern", "classic", "minimal"]


@pytest.fixture(autouse=True)
def _brand_env(set_env) -> None:  # noqa: ANN001
    from tests.conftest import MINIMAL_ENV

    set_env(
        **MINIMAL_ENV,
        BRAND_ADDRESS=ADDRESS,
        BRAND_NAME="Vays Infotech",
        UNSUBSCRIBE_BASE_URL="https://vaysinfotech.com/unsubscribe",
    )


@pytest.fixture
def content() -> NewsletterContent:
    return NewsletterContent(
        title="Dell's New PowerEdge Servers",
        summary="Dell refreshed its two-socket rack line with efficiency as the headline change.",
        newsletter=(
            "Dell has announced the PowerEdge R7xx series, a refresh of its "
            "mainstream two-socket rack line.\n\n"
            "Efficiency is the headline change: a redesigned airflow path and "
            "higher-efficiency power supplies."
        ),
        subject="Dell's new servers cut power costs",
        preview_text="What the refresh means for your cycle",
        cta="Talk to our team",
        keywords=["dell", "poweredge", "efficiency"],
        category=Category.PRODUCT_LAUNCH,
        tone=Tone.PROFESSIONAL,
    )


@pytest.fixture
def renderer() -> TemplateRenderer:
    return TemplateRenderer()


class TestTemplateDiscovery:
    def test_all_three_templates_ship(self, renderer: TemplateRenderer) -> None:
        assert set(renderer.list_templates()) == set(TEMPLATE_IDS)

    def test_an_unknown_template_lists_the_real_ones(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        with pytest.raises(TemplateError) as exc_info:
            renderer.render(content, "nonexistent")

        assert "modern" in exc_info.value.user_message


class TestLegalCompliance:
    """Enforced at render time because forgetting is invisible until it is a
    compliance problem, and a template can be edited by anyone."""

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_every_template_carries_an_unsubscribe_link(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        """The unsubscribe link is now per recipient, so at render time it is
        still a placeholder — one campaign render serves every address.

        Asserting the placeholder alone would be a weaker test than the one it
        replaces, so this also runs the per-recipient pass and checks a real URL
        comes out. Both halves must hold: present in the template, and resolved
        before it could reach anyone.
        """
        from core.models import Recipient
        from modules.template.renderer import UNSUBSCRIBE_TOKEN, apply_merge_tokens

        rendered = renderer.render(content, template_id)

        assert UNSUBSCRIBE_TOKEN in rendered.html
        assert UNSUBSCRIBE_TOKEN in rendered.text

        links = {
            "like_url": "https://vaysinfotech.com/?t=like",
            "unsubscribe_url": "https://vaysinfotech.com/?t=unsub",
        }
        recipient = Recipient(email="dana@client.com")
        for part in (rendered.html, rendered.text):
            personalised = apply_merge_tokens(part, recipient, links)
            assert links["unsubscribe_url"] in personalised
            assert UNSUBSCRIBE_TOKEN not in personalised

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_every_template_offers_a_like_link(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        rendered = renderer.render(content, template_id)

        assert "{{like_url}}" in rendered.html

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_every_template_carries_the_postal_address(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        assert ADDRESS in renderer.render(content, template_id).html

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_every_template_produces_a_plain_text_part(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        """A missing text/plain alternative is a well-known spam signal."""
        text = renderer.render(content, template_id).text

        assert len(text.strip()) > 100
        assert "<" not in text.replace("<https", "")  # no markup leaked in

    def test_rendering_without_a_postal_address_is_refused(
        self, renderer: TemplateRenderer, content: NewsletterContent, set_env
    ) -> None:  # noqa: ANN001
        set_env(BRAND_ADDRESS="")

        with pytest.raises(TemplateError) as exc_info:
            renderer.render(content, "modern", brand=None)

        assert "required in every marketing email by law" in exc_info.value.user_message

    def test_rendering_without_an_unsubscribe_url_is_refused(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        brand = BrandConfig(
            name="Vays", primary_color="#0B5FFF", address=ADDRESS, unsubscribe_base_url="  "
        )

        with pytest.raises(TemplateError) as exc_info:
            renderer.render(content, "modern", brand=brand)

        assert "unsubscribe" in exc_info.value.user_message.lower()


class TestEscaping:
    """Article text comes from the open web and passes through an LLM."""

    def test_html_in_content_is_escaped(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        hostile = content.model_copy(
            update={"newsletter": "Before <script>alert('xss')</script> after."}
        )

        html = renderer.render(hostile, "modern").html

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_a_jinja_expression_in_content_is_not_evaluated(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        """Template injection (S-3): content is data, never template source."""
        hostile = content.model_copy(
            update={"newsletter": "Total: {{ 7 * 6 }} and {% raw %}{% endraw %}"}
        )

        html = renderer.render(hostile, "modern").html

        assert "42" not in html

    def test_quotes_in_the_title_do_not_break_the_markup(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        hostile = content.model_copy(update={"title": 'Dell\'s "big" <update>'})

        html = renderer.render(hostile, "modern").html

        assert "<update>" not in html


class TestMergeTokens:
    def test_tokens_are_substituted_for_a_recipient(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        personalised = content.model_copy(
            update={"newsletter": "Hi {{name}} at {{company}}, here is the news."}
        )

        rendered = renderer.render(
            personalised,
            "modern",
            recipient=Recipient(email="p@vays.com", name="Priya", company="Acme Ltd"),
        )

        assert "Priya" in rendered.html
        assert "Acme Ltd" in rendered.html
        assert "{{name}}" not in rendered.html

    def test_the_preview_path_shows_placeholders_not_blanks(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        """So the user can see where personalisation lands rather than a gap."""
        personalised = content.model_copy(update={"newsletter": "Hi {{name}}, news."})

        html = renderer.render(personalised, "modern").html

        assert "Hi there," in html

    def test_tokens_are_substituted_in_the_subject(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        personalised = content.model_copy(update={"subject": "{{name}}, your update"})

        rendered = renderer.render(
            personalised, "modern", recipient=Recipient(email="p@v.com", name="Priya")
        )

        assert rendered.subject == "Priya, your update"

    def test_an_unknown_token_is_left_alone(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        """A stray `{{` in article text must reach the recipient as typed, not
        vanish because we guessed it was a merge field."""
        odd = content.model_copy(update={"newsletter": "The syntax is {{unknown}} here."})

        html = renderer.render(odd, "modern").html

        assert "unknown" in html


class TestEmailClientCompatibility:
    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_css_is_inlined(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        """Gmail strips <style> blocks in several contexts and Outlook uses the
        Word engine, so a stylesheet that is not inlined simply does not apply."""
        html = renderer.render(content, template_id).html

        assert html.count('style="') > 10

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_layout_uses_tables_not_divs(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        """Outlook's Word engine does not support div-based layout."""
        html = renderer.render(content, template_id).html

        assert '<table role="presentation"' in html

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_no_unsupported_css_features(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        """flexbox, grid and float are unsupported or unreliable across clients."""
        html = renderer.render(content, template_id).html.lower()

        for banned in ("display:flex", "display: flex", "display:grid", "position:absolute"):
            assert banned not in html

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_a_preheader_is_present(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        """It is what the inbox list shows beside the subject line."""
        html = renderer.render(content, template_id).html

        assert content.preview_text in html

    @pytest.mark.parametrize("template_id", TEMPLATE_IDS)
    def test_outlook_conditional_wrapper_is_present(
        self, renderer: TemplateRenderer, content: NewsletterContent, template_id: str
    ) -> None:
        html = renderer.render(content, template_id).html

        assert "[if mso]" in html


class TestPlainText:
    def test_it_contains_the_content_not_markup(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        text = renderer.render(content, "modern").text

        assert content.title in text
        assert "Efficiency is the headline change" in text
        assert "redesigned airflow path" in text
        assert "<table" not in text

    def test_the_cta_url_matches_the_html(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        """Computed once for both parts — separately is how the button and the
        text link end up pointing somewhere different."""
        rendered = renderer.render(content, "modern", cta_url="https://vays.com/contact")

        assert "https://vays.com/contact" in rendered.text
        assert "https://vays.com/contact" in rendered.html

    def test_sources_are_attributed(
        self, renderer: TemplateRenderer, content: NewsletterContent
    ) -> None:
        """Copyright control C-6: always link back to the original."""
        rendered = renderer.render(
            content, "modern", source_urls=["https://dell.com/blog/poweredge"]
        )

        assert "https://dell.com/blog/poweredge" in rendered.text
        assert "https://dell.com/blog/poweredge" in rendered.html


class TestBrand:
    @pytest.mark.parametrize(
        ("colour", "expected"),
        [("#0B5FFF", "#FFFFFF"), ("#FFFFFF", "#14181F"), ("#F5E663", "#14181F")],
    )
    def test_button_text_contrasts_with_the_brand_colour(self, colour: str, expected: str) -> None:
        """The brand colour is configurable, so a pale one would otherwise give
        white-on-pale button text — invisible, and found by a recipient."""
        assert _contrasting_text(colour) == expected

    def test_an_invalid_colour_is_rejected(self) -> None:
        brand = BrandConfig(
            name="V", primary_color="#0B5FFF", address=ADDRESS, unsubscribe_base_url="https://u"
        )
        bad = brand.model_copy(update={"primary_color": "blue"})

        with pytest.raises(TemplateError):
            resolve_brand(bad)
