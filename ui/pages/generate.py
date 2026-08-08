"""Generate Newsletter — paste URLs, extract, generate a draft.

A three-step vertical flow rather than tabs: the steps are sequential, and tabs
invite the user to skip ahead into a state that cannot work yet.
"""

from __future__ import annotations

import streamlit as st

from config import get_logger
from core.enums import Audience, LengthPreset, Tone
from core.exceptions import NewsletterAppError
from core.models import GenerationOptions, GenerationRequest
from ui import components, state

log = get_logger(__name__)

TONE_LABELS = {
    Tone.PROFESSIONAL: "Professional",
    Tone.FRIENDLY: "Friendly",
    Tone.TECHNICAL: "Technical",
    Tone.EXECUTIVE: "Executive",
    Tone.ENTHUSIASTIC: "Enthusiastic",
}
LENGTH_LABELS = {
    LengthPreset.SHORT: "Short (~150 words)",
    LengthPreset.MEDIUM: "Medium (~300 words)",
    LengthPreset.LONG: "Long (~500 words)",
}
AUDIENCE_LABELS = {
    Audience.ENTERPRISE_IT: "Enterprise IT",
    Audience.SMB: "Small / mid-sized business",
    Audience.CHANNEL_PARTNER: "Channel partner",
    Audience.C_SUITE: "C-suite",
}


def render() -> None:
    st.title("Generate Newsletter")
    st.caption("Paste OEM blog URLs. The AI drafts; you edit and approve.")

    # A draft finished on the previous run. Say so here rather than silently
    # switching pages — an unexplained page change reads as a glitch.
    draft_id = state.get(state.DRAFT_CAMPAIGN_ID)
    if draft_id:
        st.success(f"Draft #{draft_id} is ready. Open **Campaign Preview** to edit and send it.")

    _step_source()
    st.divider()
    _step_style()
    st.divider()
    _step_generate()


# ── 1. sources ───────────────────────────────────────────────────────────────
def _step_source() -> None:
    st.subheader("1 · Source articles")

    urls_text = st.text_area(
        "Blog URLs, one per line (max 10)",
        height=130,
        placeholder="https://www.dell.com/en-us/blog/...\nhttps://blogs.cisco.com/...",
        key="generate_urls",
    )
    urls = [line.strip() for line in urls_text.splitlines() if line.strip()]

    left, right = st.columns([3, 1])
    with left:
        st.caption(f"{len(urls)} URL{'s' if len(urls) != 1 else ''} · maximum 10")
    with right:
        extract = st.button(
            "Extract →", type="primary", width="stretch", disabled=not urls or len(urls) > 10
        )

    with st.expander("Paste article text manually"):
        _manual_paste()

    if extract:
        _extract(urls)

    _show_extracted()


def _extract(urls: list[str]) -> None:
    from services.ingestion_service import IngestionService

    with st.status("Reading articles…", expanded=True) as status:
        try:
            result = IngestionService().ingest_urls(
                urls,
                on_progress=lambda message: status.update(label=f"Reading articles… {message}"),
            )
        except NewsletterAppError as exc:
            status.update(label="Couldn't read the articles", state="error")
            components.error_panel(exc)
            return

        for url, reason in result.failures.items():
            st.warning(f"**{url}**\n\n{reason}")
        if result.duplicates_removed:
            st.caption(f"{result.duplicates_removed} duplicate URL(s) removed.")

        if result.any_succeeded:
            existing = state.get(state.ARTICLE_IDS, []) or []
            state.set_value(state.ARTICLE_IDS, existing + [a.id for a in result.articles])
            status.update(
                label=f"{result.succeeded} article(s) ready", state="complete", expanded=False
            )
        else:
            status.update(label="No articles could be read", state="error")


def _manual_paste() -> None:
    """The escape hatch for sites that block automated readers (FR-1.7)."""
    from services.ingestion_service import IngestionService

    title = st.text_input("Article title", key="manual_title")
    body = st.text_area("Article text", height=200, key="manual_body")

    if st.button("Add this article", disabled=not (title and body)):
        try:
            article = IngestionService().ingest_manual(title, body)
        except NewsletterAppError as exc:
            components.error_panel(exc)
            return
        state.set_value(state.ARTICLE_IDS, [*(state.get(state.ARTICLE_IDS, []) or []), article.id])
        st.success(f"Added “{article.title}” ({article.word_count:,} words).")
        st.rerun()


def _show_extracted() -> None:
    from services.ingestion_service import IngestionService

    article_ids = state.get(state.ARTICLE_IDS, []) or []
    if not article_ids:
        return

    service = IngestionService()
    articles = [a for a in (service.get_article(i) for i in article_ids) if a is not None]

    st.markdown(f"**{len(articles)} article(s) ready**")
    for index, article in enumerate(articles):
        components.article_row(article, index, _remove_article)


def _remove_article(index: int) -> None:
    ids = list(state.get(state.ARTICLE_IDS, []) or [])
    if 0 <= index < len(ids):
        ids.pop(index)
        state.set_value(state.ARTICLE_IDS, ids)
        st.rerun()


# ── 2. style ─────────────────────────────────────────────────────────────────
def _step_style() -> None:
    st.subheader("2 · Style")
    saved: GenerationOptions = state.get(state.GENERATION_OPTIONS) or GenerationOptions()

    left, middle, right = st.columns(3)
    with left:
        tone = st.selectbox(
            "Tone",
            list(Tone),
            index=list(Tone).index(saved.tone),
            format_func=lambda t: TONE_LABELS[t],
        )
    with middle:
        length = st.selectbox(
            "Length",
            list(LengthPreset),
            index=list(LengthPreset).index(saved.length),
            format_func=lambda x: LENGTH_LABELS[x],
        )
    with right:
        audience = st.selectbox(
            "Audience",
            list(Audience),
            index=list(Audience).index(saved.audience),
            format_func=lambda a: AUDIENCE_LABELS[a],
        )

    state.set_value(
        state.GENERATION_OPTIONS,
        GenerationOptions(tone=tone, length=length, audience=audience),
    )


# ── 3. generate ──────────────────────────────────────────────────────────────
def _step_generate() -> None:
    from services.health_service import HealthService

    st.subheader("3 · Generate")

    article_ids = state.get(state.ARTICLE_IDS, []) or []
    health = HealthService().check()

    left, right = st.columns([3, 1])
    with left:
        components.health_row("AI service", health.llm)
        if article_ids:
            st.caption(f"Usually takes 30–90 seconds for {len(article_ids)} article(s).")
    with right:
        blocked = not article_ids or not health.can_generate
        if st.button("Generate newsletter", type="primary", width="stretch", disabled=blocked):
            _generate(article_ids)

    if not article_ids:
        st.caption("Add at least one article above.")
    elif not health.can_generate:
        st.caption("The AI service isn't reachable — check Settings → AI Service.")


def _generate(article_ids: list[int]) -> None:
    """Run the pipeline with staged progress.

    Named stages, not a bare spinner: at 45 seconds an anonymous spinner reads as
    a hang, while "Summarising 2/3" reads as work.
    """
    from services.generation_service import GenerationService

    options = state.get(state.GENERATION_OPTIONS) or GenerationOptions()

    with st.status("Generating…", expanded=True) as status:
        try:
            draft = GenerationService().generate(
                GenerationRequest(article_ids=article_ids, options=options),
                on_progress=lambda message: status.update(label=f"Generating… {message}"),
            )
        except NewsletterAppError as exc:
            status.update(label="Generation failed", state="error")
            components.error_panel(exc)
            st.info(
                "Your articles are saved — nothing was lost. Fix the problem above and "
                "click Generate again."
            )
            return

        status.update(label="Draft ready", state="complete", expanded=False)

    state.set_value(state.DRAFT_CAMPAIGN_ID, draft.campaign_id)
    state.set_value(state.ARTICLE_IDS, [])
    state.set_value(state.SOURCE_URLS, draft.source_urls)
    st.rerun()
