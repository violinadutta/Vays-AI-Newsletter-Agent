"""Campaign Preview — edit the draft, review it, and send.

Two columns: editing on the left, the rendered email on the right. Editing
without seeing the result is where formatting mistakes come from.
"""

from __future__ import annotations

import streamlit as st

from config import get_logger, get_settings
from config.constants import CTA_MAX_LENGTH, PREVIEW_TEXT_MAX_LENGTH, SUBJECT_MAX_LENGTH
from core.enums import EditableField
from core.exceptions import NewsletterAppError
from core.models import ContentPatch
from ui import components, state

log = get_logger(__name__)


def render() -> None:
    campaign_id = state.get(state.DRAFT_CAMPAIGN_ID)
    if not campaign_id:
        st.title("Campaign Preview")
        components.empty_state(
            "No draft open",
            "Generate a newsletter first, or open one from Campaign History.",
        )
        return

    from services.campaign_service import CampaignService

    service = CampaignService()
    try:
        content = service.get_content(campaign_id)
    except NewsletterAppError as exc:
        st.title("Campaign Preview")
        components.error_panel(exc)
        state.clear(state.DRAFT_CAMPAIGN_ID)
        return

    header, status_col = st.columns([5, 1])
    with header:
        st.title("Campaign Preview")
    with status_col:
        from modules.repository.campaign_repo import CampaignRepository
        from modules.repository.database import unit_of_work

        with unit_of_work() as session:
            row = CampaignRepository(session).get(campaign_id)
            components.status_chip(str(row.status) if row else "DRAFT")

    editor, preview = st.columns([1, 1], gap="large")
    with editor:
        patch = _editor(campaign_id, content)
    with preview:
        _preview(campaign_id, content)

    if patch:
        try:
            service.update_content(campaign_id, patch)
        except NewsletterAppError as exc:
            components.error_panel(exc)
        else:
            st.rerun()

    st.divider()
    _recipients(campaign_id)
    st.divider()
    _send(campaign_id, content)


# ── editing ──────────────────────────────────────────────────────────────────
def _editor(campaign_id: int, content: object) -> ContentPatch | None:
    """The editable fields. Returns a patch when something changed."""
    st.subheader("Edit")

    with st.form("content_editor"):
        subject = st.text_input("Subject line", value=content.subject)
        components.char_counter(subject, SUBJECT_MAX_LENGTH)

        preview_text = st.text_input("Preview text", value=content.preview_text)
        components.char_counter(preview_text, PREVIEW_TEXT_MAX_LENGTH)

        title = st.text_input("Title", value=content.title)
        summary = st.text_area("Executive summary", value=content.summary, height=110)
        newsletter = st.text_area("Newsletter body", value=content.newsletter, height=320)
        st.caption(f"{len(newsletter.split()):,} words")

        cta_col, url_col = st.columns(2)
        with cta_col:
            cta = st.text_input("CTA text", value=content.cta)
            components.char_counter(cta, CTA_MAX_LENGTH)
        with url_col:
            cta_url = st.text_input("CTA link", value=state.get(state.CTA_URL, "") or "")

        keywords = st.text_input("Keywords (comma separated)", value=", ".join(content.keywords))

        if st.form_submit_button("Save changes", type="primary", width="stretch"):
            state.set_value(state.CTA_URL, cta_url)
            return ContentPatch(
                subject=subject,
                preview_text=preview_text,
                title=title,
                summary=summary,
                newsletter=newsletter,
                cta=cta,
                cta_url=cta_url or None,
                keywords=[k.strip().lower() for k in keywords.split(",") if k.strip()],
            )

    _regenerate_controls(campaign_id)
    return None


def _regenerate_controls(campaign_id: int) -> None:
    """Per-field regeneration (FR-3.8).

    Outside the form, because a form batches its widgets until submit and these
    need to act immediately.
    """
    st.markdown("**Regenerate a single field**")
    field_col, instruction_col, button_col = st.columns([2, 3, 1])

    with field_col:
        field = st.selectbox(
            "Field",
            [
                EditableField.SUBJECT,
                EditableField.PREVIEW_TEXT,
                EditableField.CTA,
                EditableField.TITLE,
                EditableField.SUMMARY,
                EditableField.NEWSLETTER,
            ],
            format_func=lambda f: f.value.replace("_", " ").title(),
            label_visibility="collapsed",
        )
    with instruction_col:
        instruction = st.text_input(
            "Instruction",
            placeholder="optional — e.g. make it more urgent",
            label_visibility="collapsed",
        )
    with button_col:
        go = st.button("↻", help="Regenerate this field", width="stretch")

    if go:
        _regenerate(campaign_id, field, instruction or None)

    if st.button("Suggest 3 subject lines"):
        _subject_variants(campaign_id)

    variants = state.get(state.SUBJECT_VARIANTS)
    if variants:
        st.markdown("**Subject line options**")
        for index, variant in enumerate(variants):
            cols = st.columns([5, 1])
            with cols[0]:
                st.markdown(
                    f"{variant}  \n<span class='muted'>{len(variant)}/60</span>",
                    unsafe_allow_html=True,
                )
            with cols[1]:
                if st.button("Use", key=f"use_variant_{index}"):
                    _apply_subject(campaign_id, variant)


def _regenerate(campaign_id: int, field: EditableField, instruction: str | None) -> None:
    from services.generation_service import GenerationService

    with st.spinner(f"Rewriting the {field.value.replace('_', ' ')}…"):
        try:
            GenerationService().regenerate_field(campaign_id, field, instruction)
        except NewsletterAppError as exc:
            components.error_panel(exc)
            return
    st.rerun()


def _subject_variants(campaign_id: int) -> None:
    from services.generation_service import GenerationService

    with st.spinner("Writing three options…"):
        try:
            variants = GenerationService().generate_subject_variants(campaign_id)
        except NewsletterAppError as exc:
            components.error_panel(exc)
            return
    state.set_value(state.SUBJECT_VARIANTS, variants)
    st.rerun()


def _apply_subject(campaign_id: int, subject: str) -> None:
    from services.campaign_service import CampaignService

    try:
        CampaignService().update_content(campaign_id, ContentPatch(subject=subject))
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return
    state.clear(state.SUBJECT_VARIANTS)
    st.rerun()


# ── preview ──────────────────────────────────────────────────────────────────
def _preview(campaign_id: int, content: object) -> None:
    from services.delivery_service import DeliveryService

    st.subheader("Preview")

    controls, width_control = st.columns([2, 2])
    with controls:
        renderer_templates = DeliveryService()._renderer.list_templates()  # noqa: SLF001
        template_id = st.selectbox(
            "Template",
            renderer_templates or ["modern"],
            index=0,
            label_visibility="collapsed",
        )
        state.set_value(state.TEMPLATE_ID, template_id)
    with width_control:
        width = st.radio(
            "Width", ["Desktop", "Mobile"], horizontal=True, label_visibility="collapsed"
        )

    try:
        rendered = DeliveryService().render(
            content,
            template_id,
            cta_url=state.get(state.CTA_URL) or None,
            source_urls=state.get(state.SOURCE_URLS) or [],
        )
    except NewsletterAppError as exc:
        components.error_panel(exc)
        return

    # Rendered inside a sandboxed iframe: the email contains content from the
    # open web, and this page is authenticated (security control S-4).
    st.components.v1.html(
        rendered.html, height=720, width=380 if width == "Mobile" else None, scrolling=True
    )

    with st.expander("Plain-text part"):
        st.text(rendered.text)

    st.download_button(
        "Download HTML",
        rendered.html,
        file_name=f"newsletter-{campaign_id}.html",
        mime="text/html",
    )

    urls = state.get(state.SOURCE_URLS) or []
    if urls:
        components.source_panel(urls)


# ── recipients ───────────────────────────────────────────────────────────────
def _recipients(campaign_id: int) -> None:
    from services.delivery_service import DeliveryService

    st.subheader("Recipients")
    service = DeliveryService()

    upload = st.file_uploader(
        "Recipient list (CSV)",
        type=["csv"],
        help="Needs an 'email' column. 'name' and 'company' are optional.",
    )
    st.download_button(
        "Download a sample CSV",
        "email,name,company\npriya@example.com,Priya Sharma,Acme Ltd\n",
        file_name="recipients-sample.csv",
        mime="text/csv",
    )

    if upload is not None:
        try:
            validation = service.validate_recipients(upload.getvalue())
        except NewsletterAppError as exc:
            components.error_panel(exc)
            return

        counts = st.columns(4)
        counts[0].metric("Valid", f"{validation.sendable_count:,}")
        counts[1].metric("Invalid", f"{len(validation.invalid):,}")
        counts[2].metric("Duplicates", f"{len(validation.duplicates):,}")
        counts[3].metric("Suppressed", f"{len(validation.suppressed):,}")

        if validation.invalid:
            with st.expander(f"{len(validation.invalid)} row(s) skipped"):
                for row, reason in validation.invalid.items():
                    st.markdown(f"- `{row}` — {reason}")
        if validation.suppressed:
            with st.expander(f"{len(validation.suppressed)} suppressed contact(s) excluded"):
                st.caption("These people unsubscribed or hard-bounced previously.")
                for address in validation.suppressed:
                    st.markdown(f"- {address}")

        if validation.sendable_count and st.button(
            f"Attach {validation.sendable_count:,} recipients", type="primary"
        ):
            service.save_recipients(campaign_id, validation.valid)
            state.set_value(state.RECIPIENTS, validation.sendable_count)
            st.rerun()

    from services.campaign_service import CampaignService

    attached = CampaignService().recipient_count(campaign_id)
    if attached:
        st.success(f"{attached:,} recipients attached to this campaign.")


# ── sending ──────────────────────────────────────────────────────────────────
def _send(campaign_id: int, content: object) -> None:
    from services.campaign_service import CampaignService
    from services.delivery_service import DeliveryService

    st.subheader("Send")

    report = state.get(state.SEND_REPORT)
    if report:
        _show_report(campaign_id, report)
        return

    settings = get_settings()
    attached = CampaignService().recipient_count(campaign_id)
    template_id = state.get(state.TEMPLATE_ID, "modern")

    test_col, send_col = st.columns([2, 2])
    with test_col:
        test_address = st.text_input("Send a test to", placeholder="you@vays.com")
        if st.button("Send test", disabled=not test_address):
            try:
                result = DeliveryService().send_test(content, test_address, template_id)
            except NewsletterAppError as exc:
                components.error_panel(exc)
            else:
                if result.ok:
                    st.success(f"Test sent to {test_address}.")
                else:
                    st.error(result.error_message or "The test send failed.")

    with send_col:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if not attached:
            st.button("Send campaign", disabled=True, width="stretch")
            st.caption("Attach a recipient list first.")
        elif not state.get(state.CONFIRM_SEND):
            if st.button(
                f"Send campaign to {attached:,} recipients", type="primary", width="stretch"
            ):
                state.set_value(state.CONFIRM_SEND, True)
                st.rerun()

    if state.get(state.CONFIRM_SEND) and attached:
        st.divider()
        sender = f"{settings.email.sender_name} <{settings.email.sender_address}>"
        if components.confirm_send(attached, content.subject, sender):
            state.clear(state.CONFIRM_SEND)
            _dispatch(campaign_id, content, template_id)
        elif st.session_state.get("cancel_send"):
            state.clear(state.CONFIRM_SEND)


def _dispatch(campaign_id: int, content: object, template_id: str) -> None:
    from services.delivery_service import DeliveryService

    progress = st.progress(0.0, text="Starting…")
    status_line = st.empty()

    def on_progress(sent: int, failed: int, remaining: int) -> None:
        total = sent + failed + remaining
        progress.progress(
            (sent + failed) / total if total else 1.0,
            text=f"Sent {sent:,} · failed {failed:,} · {remaining:,} remaining",
        )

    try:
        report = DeliveryService().send_campaign(
            campaign_id,
            content,
            template_id,
            cta_url=state.get(state.CTA_URL) or None,
            source_urls=state.get(state.SOURCE_URLS) or [],
            on_progress=on_progress,
        )
    except NewsletterAppError as exc:
        progress.empty()
        components.error_panel(exc)
        return

    progress.empty()
    status_line.empty()
    state.set_value(state.SEND_REPORT, report)
    st.rerun()


def _show_report(campaign_id: int, report: object) -> None:
    if report.fully_successful:
        st.success(
            f"Campaign sent — {report.sent:,} of {report.attempted:,} delivered "
            f"in {report.duration_s:.0f}s."
        )
    else:
        st.warning(f"{report.sent:,} of {report.attempted:,} delivered. {report.failed:,} failed.")

    if report.failures:
        st.markdown("**Failed recipients**")
        st.dataframe(
            [
                {"Email": f.email, "Reason": f.error_message or f.error_code or "Unknown"}
                for f in report.failures
            ],
            width="stretch",
            hide_index=True,
        )
        if st.button("Retry failed only"):
            _retry(campaign_id)

    if st.button("Start a new newsletter"):
        state.clear(state.SEND_REPORT)
        state.clear(state.DRAFT_CAMPAIGN_ID)
        state.clear(state.SOURCE_URLS)
        st.rerun()


def _retry(campaign_id: int) -> None:
    from services.campaign_service import CampaignService
    from services.delivery_service import DeliveryService

    with st.spinner("Retrying the failed recipients…"):
        try:
            content = CampaignService().get_content(campaign_id)
            report = DeliveryService().retry_failed(
                campaign_id, content, state.get(state.TEMPLATE_ID, "modern")
            )
        except NewsletterAppError as exc:
            components.error_panel(exc)
            return
    state.set_value(state.SEND_REPORT, report)
    st.rerun()
