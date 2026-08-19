"""Newsletter content → email HTML and a plain-text alternative.

Three things here are load-bearing:

**The environment is sandboxed and autoescaping.** Article text arrives from the
open web and passes through an LLM; a ``{{ ... }}`` sequence surviving into a
template string would be a code-execution path (security control S-3). This is the
exact opposite of the *prompt* renderer, which deliberately disables escaping —
that one produces prompts, this one produces markup.

**User content is data, never template source.** Merge tokens like ``{{name}}``
that a user typed into the newsletter body are substituted by literal string
replacement against a whitelist, *after* Jinja has finished. Re-rendering user
content through Jinja would reintroduce the injection hole autoescape closes.

**The plain-text part is composed from the content fields, not converted from
HTML** (D-16). It drops the GPL dependency and produces better output: converted
HTML leaves artefacts, while the fields are already clean prose.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import premailer
from jinja2 import FileSystemLoader, StrictUndefined, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from markupsafe import Markup, escape

from config import get_logger
from config.constants import EMAIL_TEMPLATES_DIR
from core.exceptions import TemplateError
from core.models import BrandConfig, NewsletterContent, Recipient, RenderedEmail
from modules.template.brand import ResolvedBrand, resolve_brand

log = get_logger(__name__)

# premailer parses CSS with cssutils, which warns about vendor-specific properties
# it does not recognise (`-ms-interpolation-mode`, `mso-hide`). Those properties are
# in the templates on purpose — they are what makes Outlook and older clients
# behave — so the warnings are noise that would bury real problems in the log.
logging.getLogger("CSSUTILS").setLevel(logging.ERROR)

#: Merge tokens a user may type into the newsletter body. Anything outside this
#: set is left alone rather than guessed at — a stray ``{{`` in article text must
#: reach the recipient as-is, not vanish.
MERGE_TOKENS = ("name", "company", "email", "like_url", "unsubscribe_url")

#: The two tokens that must resolve to a real URL before an email is sent. They
#: are per-*recipient* — each carries a signed identity — so they cannot be
#: filled in at render time, which happens once for the whole campaign.
LINK_TOKENS = ("like_url", "unsubscribe_url")

#: What the template writes where the unsubscribe link goes. The compliance
#: check accepts this in place of a literal URL, because substitution is
#: guaranteed before send and asserted at the boundary in DeliveryService.
UNSUBSCRIBE_TOKEN = "{{unsubscribe_url}}"  # noqa: S105 - a template placeholder, not a secret

_MERGE_PATTERN = re.compile(r"\{\{\s*(" + "|".join(MERGE_TOKENS) + r")\s*\}\}")

#: Blocks that carry no meaning in a text-only reading.
_HTML_TAG = re.compile(r"<[^>]+>")


class TemplateRenderer:
    """Renders a draft into an email a mail client will display correctly."""

    def __init__(self, templates_dir: Path | None = None) -> None:
        self.root = templates_dir or EMAIL_TEMPLATES_DIR
        self._env = SandboxedEnvironment(
            loader=FileSystemLoader(self.root),
            # Sandboxed + autoescape: article text is untrusted, and it is
            # interpolated into markup here.
            autoescape=select_autoescape(default_for_string=True, default=True),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ── discovery ────────────────────────────────────────────────────────────
    def list_templates(self) -> list[str]:
        """Template ids available to the user."""
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.html"))

    # ── rendering ────────────────────────────────────────────────────────────
    def render(
        self,
        content: NewsletterContent,
        template_id: str = "modern",
        *,
        brand: BrandConfig | None = None,
        recipient: Recipient | None = None,
        cta_url: str | None = None,
        source_urls: list[str] | None = None,
    ) -> RenderedEmail:
        """Build the HTML and plain-text parts of one email.

        Args:
            content: The approved newsletter fields.
            template_id: Which layout to use.
            brand: Override the configured brand.
            recipient: Supplies merge values. ``None`` renders placeholders, which
                is what the in-app preview wants.
            cta_url: Where the call-to-action points.
            source_urls: Attribution links (copyright control C-6).

        Raises:
            TemplateError: Unknown template, missing legal requirements, or a
                template that fails to render.
        """
        resolved = resolve_brand(brand)
        template_file = f"{template_id}.html"
        if not (self.root / template_file).exists():
            raise TemplateError(
                f"no email template named {template_id!r} in {self.root}",
                user_message=(
                    f"The '{template_id}' template isn't available. "
                    f"Choose one of: {', '.join(self.list_templates()) or 'none'}."
                ),
                context={"template_id": template_id, "available": self.list_templates()},
            )

        # Resolved once, used for both parts. Computing it separately per part is
        # how the HTML button and the text link end up pointing somewhere different.
        effective_cta_url = cta_url or resolved.website or "#"

        try:
            html = self._env.get_template(template_file).render(
                content=content,
                brand=resolved,
                cta_url=effective_cta_url,
                paragraphs=_split_paragraphs(content.newsletter),
                source_urls=source_urls or [],
                preview_text=content.preview_text,
            )
        except Exception as exc:  # noqa: BLE001 - any template fault is one error to the user
            raise TemplateError(
                f"template {template_id!r} failed to render: {exc}",
                context={"template_id": template_id},
            ) from exc

        html = self._inline_css(html, template_id)
        html = apply_merge_tokens(html, recipient)

        text = apply_merge_tokens(
            build_plain_text(content, resolved, cta_url=effective_cta_url, source_urls=source_urls),
            recipient,
        )

        self._assert_compliant(html, text, resolved, template_id)

        log.info(
            "email.rendered",
            template=template_id,
            html_bytes=len(html),
            text_bytes=len(text),
            personalised=recipient is not None,
        )
        return RenderedEmail(
            html=html,
            text=text,
            subject=apply_merge_tokens(content.subject, recipient),
            preview_text=content.preview_text,
            template_id=template_id,
        )

    def _inline_css(self, html: str, template_id: str) -> str:
        """Move ``<style>`` rules onto elements.

        Gmail strips ``<style>`` blocks in several contexts and Outlook uses the
        Word rendering engine, so a stylesheet that is not inlined simply does not
        apply. Failure here is non-fatal: unstyled but correct beats no email.
        """
        try:
            return str(
                premailer.transform(
                    html,
                    keep_style_tags=True,  # belt and braces for clients that do read them
                    strip_important=False,
                    disable_validation=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("email.css_inline_failed", template=template_id, error=str(exc))
            return html

    @staticmethod
    def _assert_compliant(html: str, text: str, brand: ResolvedBrand, template_id: str) -> None:
        """Refuse to emit an email that would be unlawful or unusable.

        A template can be edited by anyone; these checks make a mistake in one
        fail loudly here rather than silently in a customer's inbox.
        """

        # Either a literal URL or the per-recipient token counts. The token is
        # not a weaker guarantee: DeliveryService substitutes it and then refuses
        # to send anything with an unresolved token left in it, so the only way
        # to reach an inbox is with a real URL in place.
        def has_unsubscribe(part: str) -> bool:
            return brand.unsubscribe_url in part or UNSUBSCRIBE_TOKEN in part

        problems: list[str] = []
        if not has_unsubscribe(html):
            problems.append("no unsubscribe link in the HTML part")
        if not has_unsubscribe(text):
            problems.append("no unsubscribe link in the plain-text part")
        if brand.address not in html:
            problems.append("no postal address in the HTML part")
        if not text.strip():
            problems.append("the plain-text part is empty")

        if problems:
            raise TemplateError(
                f"template {template_id!r} produced a non-compliant email: {'; '.join(problems)}",
                user_message=(
                    "This email is missing something legally required (an unsubscribe "
                    "link or postal address). Check Settings → Branding."
                ),
                context={"template_id": template_id, "problems": problems},
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Plain text
# ─────────────────────────────────────────────────────────────────────────────
def build_plain_text(
    content: NewsletterContent,
    brand: ResolvedBrand,
    *,
    cta_url: str | None = None,
    source_urls: list[str] | None = None,
) -> str:
    """Compose the plain-text alternative from the content fields (D-16).

    Not converted from the HTML. ``html2text`` was dropped for its GPL licence,
    and the direct route is better anyway: these fields are already clean prose,
    whereas conversion leaves table artefacts and stray whitespace.

    A plain-text part is not optional — its absence is a well-known spam signal.
    """
    lines: list[str] = [content.title, "=" * min(len(content.title), 70), ""]

    if content.summary:
        lines += [content.summary, ""]

    lines += [strip_markdown(_strip_markup(content.newsletter)).strip(), ""]

    if content.cta:
        lines += [f"{content.cta}: {cta_url or brand.website or ''}".strip(), ""]

    if source_urls:
        lines += ["Sources:", *[f"  - {url}" for url in source_urls], ""]

    lines += [
        "-" * 70,
        brand.name,
        brand.address,
    ]
    if brand.website:
        lines.append(brand.website)
    # Not Jinja here -- this builder is plain string work, so the tokens are
    # written directly. In the HTML templates the same two need {% raw %} or
    # Jinja consumes them before the per-recipient pass ever sees them.
    lines += ["", "Was this useful? {{like_url}}", f"Unsubscribe: {UNSUBSCRIBE_TOKEN}"]

    return "\n".join(lines)


def _strip_markup(value: str) -> str:
    """Remove any stray HTML from user-edited content for the text part."""
    return _HTML_TAG.sub("", value)


#: Markdown emphasis the model emits despite being told not to. `**bold**` is by
#: far the most common, because "write a bold heading" has no other expression in
#: plain text. Single `*` is deliberately absent: it is ambiguous against a
#: literal asterisk or a multiplication sign, and guessing wrong mangles copy.
_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_MD_BOLD_ALT = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.DOTALL)
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.+?)\s*$", re.MULTILINE)


def _split_paragraphs(body: str) -> list[Markup]:
    """Split the newsletter body into paragraphs, converting markdown to HTML.

    Done here rather than in the template so all three layouts share one
    definition of what a paragraph is.

    **Why markdown conversion belongs here.** The model is instructed to emit
    plain prose, but an instruction is not a guarantee: asked for a "bold
    heading" with no markup available, it reaches for ``**Heading**``, and those
    asterisks then appear verbatim in the customer's inbox. Prompt wording is
    the fix for the common case; this is the safety net for the rest, and a
    safety net is warranted because the failure is visible to the recipient.

    **The order is the security property.** Each paragraph is escaped *first*,
    which neutralises any HTML in scraped article text, and only then are our
    own tags inserted into the escaped string. Converting before escaping — or
    marking raw model output safe — would reopen exactly the injection hole
    :class:`~jinja2.sandbox.SandboxedEnvironment` and autoescape exist to close.
    """
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", body or "") if chunk.strip()]
    return [_render_paragraph(chunk) for chunk in chunks]


def _render_paragraph(chunk: str) -> Markup:
    """Escape one paragraph, then convert a whitelist of markdown to HTML."""
    safe = str(escape(chunk))

    # Headings and bullets first: both are line-anchored, and inline emphasis
    # inside them is still picked up by the bold pass afterwards.
    safe = _MD_HEADING.sub(r"<strong>\1</strong>", safe)
    safe = _MD_BULLET.sub(r"&bull;&nbsp;\1", safe)
    safe = _MD_BOLD.sub(r"<strong>\1</strong>", safe)
    safe = _MD_BOLD_ALT.sub(r"<strong>\1</strong>", safe)

    # A single newline inside a paragraph is meaningful — it is what separates a
    # heading line from the copy beneath it. HTML collapses it, so the two would
    # run together on one line.
    safe = safe.replace("\n", "<br />")
    return Markup(safe)  # noqa: S704 - every insertion above is ours, post-escape


def strip_markdown(value: str) -> str:
    """Remove markdown markers for the plain-text part.

    The text alternative has no bold, so ``**Dell**`` must become ``Dell`` and
    not ``**Dell**``. Bullets keep a marker, because a list that reads as running
    prose is worse than one with dashes.
    """
    value = _MD_HEADING.sub(r"\1", value)
    value = _MD_BOLD.sub(r"\1", value)
    value = _MD_BOLD_ALT.sub(r"\1", value)
    return _MD_BULLET.sub(r"- \1", value)


def apply_merge_tokens(
    value: str, recipient: Recipient | None, links: dict[str, str] | None = None
) -> str:
    """Substitute ``{{name}}``-style tokens by literal replacement.

    Deliberately not Jinja. This runs over content a user typed and an LLM wrote;
    handing that to a template engine is the injection hole the sandbox exists to
    close. A whitelist plus ``str.replace`` cannot execute anything.

    With no recipient (the preview path) tokens resolve to neutral placeholders
    rather than empty strings, so the user can see where personalisation lands.
    """
    links = links or {}
    values = {
        "name": (recipient.name if recipient and recipient.name else "there"),
        "company": (recipient.company if recipient and recipient.company else "your team"),
        "email": (recipient.email if recipient else "you@example.com"),
    }

    def substitute(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in LINK_TOKENS:
            # Left untouched unless the caller supplies a real URL. This runs
            # twice: once at render time for the whole campaign, and again per
            # recipient at send time. The links are per-recipient, so replacing
            # them in the first pass — even with a placeholder — would consume
            # them before the pass that can actually fill them, and every
            # recipient would get the same dead href.
            return links.get(token, match.group(0))
        return values[token]

    return _MERGE_PATTERN.sub(substitute, value)
