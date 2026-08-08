# Research Findings, Stack Selection & LLM Choice
### Phases 3, 4 and 5
**Date** 2026-08-05 · **Status** Draft — awaiting approval
**Method:** web research conducted 2026-08-05 against current documentation, benchmarks and vendor pricing. Sources listed in §7. Where a claim is uncertain, it is marked **[verify at implementation]** rather than stated as fact.

---

> **⚠ SUPERSEDED IN PART (2026-08-07, D-21).** This document was written when the LLM
> was to be self-hosted on Google Colab. **Colab has been dropped entirely** — it failed
> twice on real hardware and its 3-hour sessions, rotating tunnel URL and ToS conflict
> made it unsuitable regardless. The LLM is now **Groq** (open-weight models over an
> ordinary API). Any mention below of Colab, Cloudflare Tunnel, vLLM, or Qwen3-on-a-T4
> is historical. See `docs/04_LLM_HOSTING.md` for what is actually built.

# PHASE 3 — Research Findings

## 3.1 AI newsletter generation systems — what the field has learned

Three findings shaped this architecture more than anything else.

**Finding 1 — Fully autonomous generation fails commercially, not technically.**
Tools that generate and send without review consistently produce content that is *plausible and
generic*. The industry pattern that works is **AI-assisted drafting with a human editor**: the AI
removes the blank page and the formatting labour; the human supplies judgment and brand voice.
→ *Design consequence:* the human approval gate is a feature (PRD §1), and edit-ratio is our
primary quality metric — not throughput.

**Finding 2 — Two-stage generation beats one large prompt.**
Systems that ask a mid-size model to "read these 4 articles and write a newsletter" in a single
call produce shallow output and frequently drop articles. Systems that **summarize each article
independently, then compose from the summaries** produce measurably better structure and are
cheaper to retry.
→ *Design consequence:* the two-stage pipeline in TRD §3.1. Stage 1 is a map step, which also
means it parallelizes and scales to more articles later.

**Finding 3 — Structured output is the reliability bottleneck.**
Free-form generation parsed with regex or `json.loads` is the single largest source of production
failures in LLM apps. Constrained/guided decoding — where a grammar mask makes invalid tokens
unselectable — moves JSON validity from ~90% to effectively 100%.
→ *Design consequence:* guided decoding is mandatory, not optional (see §3.5).

## 3.2 Content marketing automation — the surrounding practice

- **Provenance drives trust.** Users abandon AI tools they can't audit. Showing "this paragraph
  came from *this* article" measurably increases willingness to ship the output. → FR-3.11.
- **Subject line and preview text carry disproportionate value.** They are the highest-leverage
  fields and the ones humans most often rewrite. → dedicated regeneration and variant generation
  (FR-3.8, FR-3.10), character counters (FR-4.3).
- **Templates beat freeform layout.** Marketing teams want consistency, not a page builder.
  → 2–3 fixed templates with brand tokens, not a drag-and-drop editor.
- **Deliverability is a product feature.** Missing `List-Unsubscribe`, no plain-text part, or an
  unverified sender domain will quietly destroy a campaign regardless of copy quality. → NFR-C1.

## 3.3 Blog extraction — benchmark evidence

Published evaluations (Trafilatura's own evaluation suite; the ScrapingHub article-extraction
benchmark; independent multi-language comparisons) consistently rank:

| Library | Reported F1 | Precision | Maintained | Verdict |
|---|---|---|---|---|
| **Trafilatura** | **0.937–0.945** | **0.925–0.978 (highest)** | ✅ actively | **Primary** |
| readability-lxml | 0.943 | 0.912 | ✅ | Viable alternative; highest recall |
| **newspaper4k** | ~0.62–0.75 | lower | ✅ (maintained fork) | **Fallback** — but gives good metadata |
| newspaper3k | ~0.617 | lower | ❌ **abandoned since 2018** | **Do not use** |
| boilerpy3 / readabilipy | — | — | partial | errors on malformed HTML |

Two additional facts that matter:
1. `newspaper3k` — the library most tutorials still recommend — has had no release since 2018 and
   is effectively abandoned. `newspaper4k` is the API-compatible maintained fork. Using the former
   would be an avoidable technical debt on day one.
2. Several libraries **error outright** on malformed HTML rather than degrading. This is precisely
   why a single extractor is insufficient.

**Decision:** three-tier cascade — Trafilatura (best precision) → Newspaper4k (better metadata,
different failure modes) → BeautifulSoup heuristic (never fails, lower quality) → manual paste.
Different libraries fail on *different* pages; cascading converts a ~90% success rate into ~95%+.

**Deliberately not chosen:** Playwright/Selenium for JS-rendered pages. It adds ~400 MB of browser
binaries and significant complexity to solve a minority case that the manual-paste fallback already
covers. Revisit only if real OEM targets prove to be JS-heavy.

## 3.4 Email campaign systems — provider landscape (2026)

| Provider | Free tier | Notes |
|---|---|---|
| **Brevo** | **300/day permanent (~9,000/mo)** | Most generous permanent free tier; combines transactional API + marketing UI; non-technical staff can manage lists |
| Resend | 3,000/mo permanent | Excellent DX, clean API; lower monthly ceiling |
| Mailgun | 100/day (~3,000/mo) | Solid, developer-oriented |
| Mailtrap | 4,000/mo (150/day cap) | **Best sandbox** — catches mail without delivering it |
| Amazon SES | ~3,000/mo for 12 months | Cheapest at scale; heaviest setup |
| **SendGrid** | ❌ **free plan retired (2025)** | Most tutorials still recommend it — outdated advice |

**Decision:** **Brevo** as the primary provider (highest permanent free ceiling, and marketing
teams can self-serve lists), **SMTP adapter** for Mailtrap sandbox testing and for any future
company mail server, **console adapter** for offline development. Behind one `EmailProvider`
interface, so this is a low-consequence decision that can be reversed in an afternoon.

## 3.5 Prompt engineering — current best practice

| Practice | Applied as |
|---|---|
| Separate system persona from task instruction | `_shared/system_persona.yaml` + per-task prompt files |
| Delimit untrusted input explicitly | Article text wrapped in `<<<ARTICLE>>>` fences with a stated rule that its contents are data, never instructions (prompt-injection control S-8) |
| Constrain output with a grammar, don't ask nicely | vLLM `guided_json` with the **XGrammar** backend — JIT-compiled, adaptive token-mask caching, the default in vLLM v0.7+ and also used by SGLang, TensorRT-LLM and MLC-LLM |
| Few-shot exemplars beat adjectives | 1–2 real approved newsletters as exemplars per tone preset, rather than "write professionally" |
| Version prompts like code | Semantic-versioned YAML in Git; the version is recorded on every campaign row |
| Task decomposition | Two-stage pipeline (§3.1, Finding 2) |
| Explicit negative constraints | "Do not invent version numbers, prices, dates, or statistics not present in the source" — the primary hallucination control |
| Temperature by field | Summary 0.3 (faithful) · newsletter body 0.7 (engaging) · subject variants 0.9 (diverse) |

## 3.6 Open-source LLM deployment — serving engines

| Engine | Throughput | Guided JSON | VRAM efficiency | T4 (sm_75) support | Setup on Colab |
|---|---|---|---|---|---|
| **vLLM** | **Best** (PagedAttention + continuous batching) | ✅ **native, XGrammar** | Best | ✅ with `dtype=float16` | Moderate (~3–5 min install) |
| Ollama | Good | Partial (`format: json`, weaker guarantees) | Good (GGUF) | ✅ | **Easiest** (one command) |
| llama.cpp server | Good | ✅ GBNF grammars | Best on low VRAM | ✅ | Moderate |
| TGI (HuggingFace) | Very good | ✅ | Good | ✅ | Heavier |
| Raw `transformers` + FastAPI | Poor | ❌ manual | Poor | ✅ | Easy but slow |

**Critical T4 constraint discovered in research:** the Tesla T4 in Colab's free tier is Turing
(compute capability **7.5**). This means:
- ✅ `float16` works.
- ❌ `bfloat16` requires CC ≥ 8.0 — vLLM will raise `ValueError` unless `dtype="float16"` is set
  explicitly. **This is the single most common Colab+vLLM failure and must be in the notebook.**
- ❌ Standard FP8 requires CC ≥ 8.0.
- ⚠️ Marlin kernels (the fast quantized path) require sm_80 — on T4 the AWQ/GPTQ path uses older,
  slower kernels. Functional, but do not expect Ampere-class throughput.

**Decision:** **vLLM** primary (only engine with first-class guided JSON + best throughput),
**Ollama** documented as a fallback **running on Colab, never on the developer machine** (D-12),
for when vLLM installation on Colab misbehaves — a real risk,
since Colab's preinstalled CUDA/torch versions shift without notice.

## 3.7 Streamlit architecture — production realities

Research surfaced four constraints that must be designed around, not discovered later:

1. **Single process, one thread per session.** Horizontal scaling requires **sticky sessions**; a
   load balancer that round-robins users breaks session state outright. → documented in TRD §10.2.
2. **Memory is not isolated between sessions.** One session's leak degrades everyone. → keep the
   app process lean; never load a model in-process (already true — inference is remote).
3. **`st.session_state` persists across pages** in a multipage app; `@st.cache_data`/`cache_resource`
   persist across *sessions*. Conflating the two causes user-data leakage between users — a real
   security bug, not just a correctness one. → typed accessors in `ui/state.py`, and a rule:
   **never cache anything user-specific.**
4. **The rerun model re-executes the whole script on every interaction.** Any side effect not
   guarded will fire repeatedly. → this is the direct cause of double-send risk R-9, mitigated by
   the DB-level conditional transition (TRD §3.2).

## 3.8 Modular Python applications

Standard, well-supported practice applied here: layered architecture with unidirectional
dependencies; Ports & Adapters for volatile integrations; Pydantic DTOs at every boundary;
dependency injection by constructor (no global singletons except settings); `import-linter` in CI
to make the layer rule enforceable rather than aspirational.

---

# PHASE 4 — Technology Stack Selection

Each decision below states the alternatives considered, the choice, and the reasoning. Where the
decision is low-stakes because an adapter isolates it, that is stated — knowing which decisions are
cheap to reverse is as important as making them well.

### 4.1 Frontend — **Streamlit**

| Option | Pros | Cons |
|---|---|---|
| **Streamlit** ✅ | Pure Python; fastest path to a working internal tool; rich widgets; specified in the brief | Rerun model needs discipline; limited layout control; not for public-facing apps |
| Gradio | Great for ML demos | Weaker for multi-page CRUD apps |
| React + FastAPI | Full design control | Two stacks, two deploys, ~3× the work — not justifiable for a solo internship deliverable |
| Flask + Jinja | Simple, one language | Hand-building every widget and all state management |

**Choice: Streamlit.** Required by the brief, and genuinely the right call: the entire value here
is in the pipeline, not in bespoke UI. Polish is achieved through custom CSS, a disciplined
component library, and consistent states (UI spec, doc 05) — not by changing framework.

### 4.2 Backend — **Service layer inside the Streamlit process (no separate API in v1)**

The alternative — FastAPI backend + Streamlit frontend — is more "correct" on an architecture
diagram and worse in practice here: two processes to start, two to deploy, two to debug, on a
handover target that is a single Windows PC. The mitigation for the theoretical objection is
concrete: **`services/` contains zero Streamlit imports**, so adding a FastAPI shell later is
additive. The signatures in TRD §5.1 were written with that mapping in mind.

### 4.3 LLM serving — **vLLM + XGrammar guided JSON**
See §3.6. The deciding factor is guided decoding: it eliminates a whole error class rather than
mitigating it. *Cheap to reverse* — the OpenAI-compatible wire format means Ollama or llama.cpp
can be substituted by changing a base URL.

### 4.4 Prompt management — **Versioned YAML files in Git**

| Option | Verdict |
|---|---|
| **YAML files in `prompts/`** ✅ | Git-native (diff, review, blame, revert); zero infra; version travels with the code; prompt changes go through PR review — appropriate when prompt quality is the product |
| Langfuse | Genuinely better for *teams where non-engineers iterate on prompts*, with built-in tracing and A/B testing. Requires a hosted service or self-hosted Docker + Postgres |
| Hardcoded strings | Unversioned, unreviewable, untestable. No |
| DB-stored prompts | Editable at runtime, but changes bypass code review and aren't reproducible from a Git checkout |

**Choice: YAML in Git.** For a solo developer shipping a handover-ready artifact, adding a
Postgres-backed service to manage six prompt files is infrastructure that outweighs its benefit.
**Documented upgrade trigger:** if marketing staff need to edit prompts without a developer, adopt
Langfuse — the `PromptRegistry` interface is designed so its loader is the only thing that changes.

### 4.5 Database — **SQLite + SQLAlchemy 2.x + Alembic**
Zero installation on Windows (decisive for handover), the file *is* the backup, and ACID
transactions are sufficient at this scale. WAL mode enabled for concurrent readers. SQLAlchemy —
not raw `sqlite3` — specifically so the Postgres migration is a connection-string change.
*Postgres was not chosen for v1* because requiring a database server install would materially
damage the ≤30-minute handover target.

### 4.6 Authentication — **own module: `bcrypt` + ~80 lines** *(revised — D-15)*
The first draft proposed `streamlit-authenticator`. **Rejected on review.** It is a small
community package, and it would sit directly on the authentication boundary — the one place where
an unvetted transitive dependency has the worst blast radius. The functionality needed is narrow:
hash a password, verify it, sign a session token, guard a page. That is ~80 lines against the
`bcrypt` library (single-purpose, widely audited) and it is fully auditable by whoever inherits
this project. Fewer dependencies on the security path is the more defensible choice, and it costs
roughly half a day. OAuth/SSO remains the documented upgrade path.

### 4.7 Templating — **MJML (build-time) → Jinja2 (runtime)**
HTML email is a hostile target: Outlook uses the Word rendering engine, Gmail strips `<style>`
blocks, and mobile clients vary. MJML exists precisely to abstract this and is the industry
standard for cross-client responsive email. Its cost is a Node dependency — unacceptable at
runtime on a Windows handover machine. **Resolution:** compile `.mjml` → `.html` once at
development time, commit the output, let Jinja2 inject content at runtime, and inline CSS with
`premailer`. Full compatibility, zero runtime Node.

### 4.8 Email delivery — **Brevo API + SMTP adapter**
See §3.4. *Cheap to reverse* — `EmailProvider` interface.

### 4.9 Logging — **structlog → JSON lines + SQLite table**
See TRD §8. The non-obvious requirement is that **non-technical users need a Logs page**, which
means logs must be queryable — that rules out plain formatted text files as the only sink.

### 4.10 Configuration — **pydantic-settings + `.env`**
Validation at startup converts a class of silent runtime failures into one clear error message
before the UI loads. On a project whose most likely failure mode is "the endpoint URL is stale",
that matters.

### 4.11 Deployment — **Local Windows (v1), Docker (v2 documented)**
Matches the stated requirement (`easy to deploy on another Windows PC`). Docker is designed for but
not built, so no decision blocks it.

### 4.12 Testing — **pytest + respx + mock providers**
The mock LLM provider is the keystone: it makes the entire application testable on an 8 GB laptop
with no GPU and no network.

### 4.13 Monitoring — **In-app health panel + campaign metrics**
Prometheus/Grafana for a single-process internal tool used by five people would be theatre. The
real operational question is *"is the Colab tunnel alive right now?"* — answered by a health
indicator on the Dashboard and a Test Connection button in Settings.

---

# PHASE 5 — Open Source LLM Selection

## 5.1 The hard constraint that decides this

The runtime target is a **Colab free-tier Tesla T4: 15–16 GB VRAM, compute capability 7.5,
float16 only**. Model selection is therefore not "which model is best" but **"which model is best
among those that fit in ~15 GB with room for a KV cache"**.

Approximate VRAM for weights:

| Params | FP16 | 4-bit (AWQ/GPTQ) |
|---|---|---|
| 4B | ~8 GB | ~3 GB |
| 8B | ~16 GB ❌ (no KV cache room) | ~5.5 GB |
| 14B | ~28 GB ❌ | **~9 GB ✅** |
| 32B | ~64 GB ❌ | ~19 GB ❌ |

## 5.2 Candidate comparison

### Kimi K3 (Moonshot AI) — **rejected on hardware grounds**
Released July 2026 with open weights under a modified-MIT licence. It is a **2.8-trillion-parameter
MoE** (~104B active per token) with a 1M-token context. Reported footprint: **~1.4 TB even at 4-bit
MXFP4**, requiring multi-accelerator server nodes.

It is arguably the most capable open-weight model available, and it is **~93× larger than the
available VRAM**. It cannot be self-hosted here under any configuration. It remains relevant only
as a *hosted* endpoint — worth revisiting if the company later funds inference credits and quality
becomes the binding constraint rather than cost.

### Llama 4 (Meta) — **not selected**
Strong models (Scout leads on long context; Maverick posts the highest MMLU among open models at
~85.5%). Two disqualifiers for this project: the **Llama Community Licence is not OSI-approved**
and carries acceptable-use and attribution conditions — a real consideration for a commercial
deliverable at a client company — and the Llama 4 sizes are MoE models far too large for a T4.

### Gemma 3 (Google) — **not selected as primary; best small fallback**
Excellent quality-per-parameter, particularly at 1B–4B where it is the recommended edge/mobile
choice (Gemma 3 4B runs in ~4.2 GB). The Gemma licence includes use restrictions and is not
Apache 2.0. Gemma 3 12B is a legitimate alternative to Qwen3-14B **[verify current benchmarks at
implementation]**, but Qwen wins on licence clarity and structured-output support.

### Mistral — **not selected**
Reliable, efficient, permissively licensed in its open releases, strong at ~80+ languages. Mistral
7B / Small are solid choices. It is simply outperformed at equal size by current Qwen3 releases,
and its newest flagships have moved toward restrictive licensing.

### Qwen3 (Alibaba) — **SELECTED**
- **Apache 2.0** across the dense lineup — the cleanest possible commercial licence, no attribution
  or acceptable-use conditions. For an internship deliverable handed to a company, this alone is
  significant.
- **Full dense size ladder: 0.6B / 1.7B / 4B / 8B / 14B / 32B** — the only major family offering
  a graceful degradation path across VRAM budgets. If the T4 is unavailable and only a smaller
  runtime is allocated, dropping from 14B → 8B → 4B is a one-line config change with no other
  code impact.
- **Native structured output and tool calling**, 128K context on dense models.
- Efficiency gains are real: Qwen3-8B reportedly matches Qwen2.5-14B on benchmarks.
- Consistently ranked at or near the top of open-source leaderboards through 2026, with the Qwen3
  family widely described as the best overall local LLM family for balance of quality, sizes,
  tooling and licence.

## 5.3 Task-specific fit assessment

| Requirement | Why Qwen3-14B fits |
|---|---|
| **Summarization** | Faithful extraction is the least model-demanding task in the pipeline; even 4B handles it. Run at temp 0.3. |
| **Humanization / marketing copy** | The hardest requirement. Mid-size open models trend generic. Mitigated by few-shot exemplars from real approved newsletters, tone presets, and temp 0.7 — **and by the human editor, which is why the approval gate is architectural, not cosmetic.** This is the area where a quality shortfall is most likely; the edit-ratio metric is designed to detect it. |
| **Newsletter generation (structure)** | Strong instruction-following; the two-stage pipeline keeps each prompt small |
| **JSON generation** | Native structured output **plus** vLLM+XGrammar guided decoding — validity is guaranteed at the decoding layer regardless of model behaviour |
| **Context** | 128K far exceeds the ~6K input budget for 3–5 articles |
| **Licence** | Apache 2.0 — no conditions |

## 5.4 Final recommendation

| Tier | Model | VRAM | When |
|---|---|---|---|
| **Primary** | **Qwen3-14B-Instruct, AWQ 4-bit** | ~9 GB + KV cache | Default. Best quality that fits a T4 with headroom |
| Fallback A | **Qwen3-8B-Instruct, AWQ 4-bit** | ~5.5 GB | If 14B is unstable, or T4 kernel performance on sm_75 proves too slow |
| Fallback B | **Qwen3-4B-Instruct, FP16** | ~8 GB | Minimal VRAM allocation; also the fastest iteration loop during development |
| Escape hatch | Any open-weight model on a hosted endpoint | 0 GB local | Colab unavailable — model stays open-source, hosting does not |

**[verify at implementation]** — exact Hugging Face repository IDs and the availability of an
official AWQ build for the chosen size must be confirmed against the Hub before the notebook is
written; quantized community builds vary in quality. Confirm also that the AWQ kernel path works
on sm_75 for the specific build chosen — research surfaced user reports of AWQ failures on
compute-capability-7.5 cards, so this needs an empirical check early in Milestone M3, not an
assumption.

## 5.5 Tradeoffs accepted, stated plainly

| Tradeoff | Accepted because |
|---|---|
| 4-bit quantization degrades quality (Qwen3-8B MMLU 74.7 → 69.3 at 4-bit in one published study; 8-bit is near-lossless) | 4-bit is the only way a 14B fits. A quantized 14B is expected to beat an unquantized 4B on copy quality. **This assumption is testable and should be tested in M3** with a side-by-side on real OEM articles. |
| T4 throughput is modest; ~60–90 s for a 3-article newsletter | Acceptable for a human-in-the-loop tool where the user then spends 10 minutes editing |
| A 14B open model will not match a frontier proprietary model on prose | The brief mandates open source; the human editor closes the gap; edit-ratio measures it honestly |
| Kimi K3 — the strongest open model — is unusable | 1.4 TB vs 15 GB. Not a close call |

---

## 6. Decisions Deferred (with triggers)

| Decision | Deferred until |
|---|---|
| Langfuse for prompt management | Marketing staff need to edit prompts without a developer |
| Postgres | >5 concurrent users, or lock contention appears |
| Background job queue (Celery/RQ) | Recipient lists routinely exceed ~5,000 |
| Playwright for JS-rendered sites | A real OEM target proves unscrapeable by all three tiers |
| FastAPI HTTP layer | A second client (mobile, CRM integration, scheduler) needs the same logic |
| Fine-tuning / LoRA on Vays' own newsletters | ≥50 approved newsletters exist as training data and edit-ratio stays above target |
| Open/click tracking | Basic delivery is proven stable in production |

---

## 7. Sources

- [Trafilatura — Evaluation](https://trafilatura.readthedocs.io/en/latest/evaluation.html)
- [ScrapingHub — Article extraction benchmark](https://github.com/scrapinghub/article-extraction-benchmark)
- [Trafilatura vs Readability vs Newspaper4k](https://www.contextractor.com/trafilatura-vs-readability-vs-newspaper/)
- [Text extraction comparison (multi-library)](https://github.com/tsolewski/Text_extraction_comparison_PL)
- [Best Open-Source LLMs 2026 — Qwen, GLM, DeepSeek & Llama compared](https://www.buildfastwithai.com/blogs/collection/open-source-llms)
- [Best Open-Source LLMs, July 2026 leaderboard](https://techsy.io/en/blog/best-open-source-llms-2026)
- [Open Source LLM comparison table (2026)](https://computingforgeeks.com/open-source-llm-comparison/)
- [Qwen3 full lineup guide 2026](https://baeseokjae.github.io/posts/qwen-3-full-lineup-guide-2026/)
- [Qwen/Qwen3-14B on Hugging Face](https://huggingface.co/Qwen/Qwen3-14B)
- [Qwen3 Technical Report](https://arxiv.org/pdf/2505.09388)
- [An Empirical Study of Qwen3 Quantization](https://arxiv.org/html/2505.02214v1)
- [Kimi K3 open weights — 2.8T parameters](https://www.explainx.ai/blog/kimi-k3-open-weights-2-8-trillion-parameters-july-2026)
- [Kimi K3 inference economics — the 1.4TB catch](https://www.techi.com/kimi-k3-open-weights-inference-economics/)
- [vLLM — Structured Outputs](https://docs.vllm.ai/en/v0.8.2/features/structured_outputs.html)
- [Structured decoding in vLLM — a gentle introduction (BentoML)](https://www.bentoml.com/blog/structured-decoding-in-vllm-a-gentle-introduction)
- [Guided decoding performance on vLLM and SGLang (SqueezeBits)](https://blog.squeezebits.com/guided-decoding-performance-vllm-sglang)
- [vLLM issue #1157 — bfloat16 requires compute capability ≥ 8.0 (T4 is 7.5)](https://github.com/vllm-project/vllm/issues/1157)
- [vLLM GPU compatibility matrix](https://www.speediyo.com/ai-infra/vllm-gpu-compute-capability-matrix)
- [Google Colab FAQ — prohibited activities](https://research.google.com/colaboratory/faq.html)
- [colab-llm — Ollama + Cloudflare tunnel notebook](https://github.com/enescingoz/colab-llm)
- [Google Colab GPU: free access, limits and alternatives](https://www.hivenet.com/post/google-colaboratory-gpu-complete-guide-to-free-cloud-gpu-access-and-limitations)
- [Best email API services 2026 (Brevo)](https://www.brevo.com/blog/best-email-api/)
- [SendGrid alternatives 2026 — the free tier is gone](https://dreamlit.ai/blog/best-sendgrid-alternatives)
- [Email API pricing comparison, July 2026](https://www.buildmvpfast.com/api-costs/email)
- [MJML — the responsive email framework](https://mjml.io/)
- [Streamlit — Session State concepts](https://docs.streamlit.io/develop/concepts/architecture/session-state)
- [Streamlit in production — caching, multi-user state (Devolute)](https://www.devolute.org/en/insights/open-source/streamlit-production-data-apps-python/)
- [Langfuse — prompt version control](https://langfuse.com/docs/prompt-management/features/prompt-version-control)
