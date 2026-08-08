# Technical Requirements Document (TRD)
### AI Newsletter Generation & Distribution Platform
**Version** 1.0 · **Date** 2026-08-05 · **Status** Draft — awaiting approval
**Companion documents:** `01_PRD.md`, `03_RESEARCH_AND_DECISIONS.md`, `04_COLAB_LLM_ARCHITECTURE.md`, `05_UI_SPEC.md`, `06_BACKEND_ARCHITECTURE.md`, `07_PROMPT_ENGINEERING.md`

---

> **⚠ SUPERSEDED IN PART (2026-08-07, D-21).** This document was written when the LLM
> was to be self-hosted on Google Colab. **Colab has been dropped entirely** — it failed
> twice on real hardware and its 3-hour sessions, rotating tunnel URL and ToS conflict
> made it unsuitable regardless. The LLM is now **Groq** (open-weight models over an
> ordinary API). Any mention below of Colab, Cloudflare Tunnel, vLLM, or Qwen3-on-a-T4
> is historical. See `docs/04_LLM_HOSTING.md` for what is actually built.

## 1. High-Level Architecture

### 1.1 Architectural style

**Modular monolith with a strict layered boundary and pluggable adapters.**

Not microservices. Not a framework-coupled script. The reasoning:

- **Team size is one.** Distributed systems buy independent deployability at the cost of
  operational complexity. There is no second team to deploy independently.
- **Handover is a first-class requirement.** One process, one command, one config file is the
  most handover-friendly shape that still has real internal structure.
- **The volatile parts are the integrations, not the core.** The LLM host will change. The email
  provider may change. The database may become Postgres. Those are exactly the boundaries that
  get an adapter interface (Ports & Adapters / Hexagonal), while the stable core stays plain Python.

### 1.2 Layer diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│  PRESENTATION LAYER                        Streamlit                 │
│  app.py  ·  pages/*.py  ·  components/*.py                           │
│  Responsibility: render, capture input, display state.               │
│  Forbidden: business logic, direct DB access, direct HTTP calls.     │
└─────────────────────────────┬────────────────────────────────────────┘
                              │  Pydantic DTOs only
┌─────────────────────────────▼────────────────────────────────────────┐
│  SERVICE / ORCHESTRATION LAYER             services/                 │
│  IngestionService · GenerationService · CampaignService              │
│  DeliveryService · SettingsService                                   │
│  Responsibility: use-case orchestration, transactions, state machine │
│  Forbidden: any `import streamlit`.                                  │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────────┐
│  DOMAIN / CORE LAYER                       core/                     │
│  models (Pydantic) · enums · exceptions · validators · constants     │
│  Pure Python. No I/O. No third-party framework. Fully unit-testable. │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────────┐
│  ADAPTER LAYER (Ports & Adapters)          modules/                  │
│                                                                      │
│  scraper/     ArticleExtractor    ← Trafilatura│Newspaper4k│BS4      │
│  cleaner/     TextCleaner         (pure functions, no I/O)           │
│  ai/          LLMProvider  ◄── Colab │ HostedOpenWeight │ Mock         │
│               (ALL remote — no model ever loads in this process)      │
│               PromptRegistry (YAML, versioned)                       │
│  template/    TemplateRenderer    ← Jinja2 over MJML-compiled HTML   │
│  email/       EmailProvider ◄── Brevo │ SMTP │ Console(dev)          │
│  repository/  SQLAlchemy repositories                                │
└─────────────────────────────┬────────────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────────────┐
│  INFRASTRUCTURE                                                      │
│  SQLite (data/app.db) · logs/*.jsonl · prompts/*.yaml                │
│  templates/*.html · .env                                             │
└──────────────────────────────────────────────────────────────────────┘

                  EXTERNAL SYSTEMS
   OEM blog sites  ·  Colab-hosted vLLM (via Cloudflare Tunnel)  ·  Brevo API
```

**The one rule that keeps this maintainable:** dependencies point downward only. `pages/` may
import `services/`; `services/` may import `core/` and `modules/`; `core/` imports nothing from
the project. A CI lint check (`import-linter`) enforces this — an architecture rule that isn't
mechanically enforced is a suggestion.

### 1.3 Deployment topology (v1)

```
   Developer's Windows PC                        Google Colab (GPU runtime)
 ┌───────────────────────────┐                ┌────────────────────────────────┐
 │  streamlit run app.py     │                │  vLLM OpenAI-compatible server │
 │  localhost:8501           │  HTTPS + Bearer│  Qwen3-14B-AWQ  ·  XGrammar    │
 │                           │───────────────▶│  127.0.0.1:8000                │
 │  SQLite  data/app.db      │                │            ▲                   │
 │  Logs    logs/app.jsonl   │                │  cloudflared tunnel            │
 └───────────┬───────────────┘                └────────────────────────────────┘
             │
             ├──▶ OEM blog sites (HTTPS GET)
             └──▶ Brevo transactional email API (HTTPS POST)
```

---

## 2. Component Diagram

```
                              ┌────────────────────┐
                              │   Streamlit UI     │
                              │  6 pages + shared  │
                              │    components      │
                              └─────────┬──────────┘
                                        │
        ┌───────────────┬───────────────┼───────────────┬────────────────┐
        ▼               ▼               ▼               ▼                ▼
┌──────────────┐ ┌─────────────┐ ┌─────────────┐ ┌────────────┐ ┌──────────────┐
│ Ingestion    │ │ Generation  │ │  Campaign   │ │ Delivery   │ │  Settings    │
│  Service     │ │  Service    │ │  Service    │ │  Service   │ │  Service     │
└──────┬───────┘ └──────┬──────┘ └──────┬──────┘ └─────┬──────┘ └──────┬───────┘
       │                │               │              │               │
   ┌───┴────┐      ┌────┴────┐          │        ┌─────┴─────┐         │
   ▼        ▼      ▼         ▼          │        ▼           ▼         ▼
┌───────┐┌───────┐┌──────┐┌────────┐    │  ┌──────────┐┌─────────┐┌────────┐
│Scraper││Cleaner││ LLM  ││ Prompt │    │  │ Template ││  Email  ││ Config │
│       ││       ││Prov. ││Registry│    │  │ Renderer ││ Provider││        │
└───┬───┘└───────┘└──┬───┘└───┬────┘    │  └────┬─────┘└────┬────┘└────────┘
    │                │        │         │       │           │
    │            ┌───┴───┐    │         │       │           │
    │            ▼       ▼    ▼         ▼       ▼           ▼
    │      ┌─────────┐┌──────────┐ ┌──────────────────────────────┐
    │      │ Colab   ││ Hosted   │ │      Repository Layer        │
    │      │ Adapter ││ Adapter  │ │  Article │ Campaign │ Send   │
    │      └─────────┘└──────────┘ │  Recipient │ Log │ Setting   │
    │                              └──────────────┬───────────────┘
    ▼                                             ▼
 OEM Blogs                                 SQLite (SQLAlchemy)

  Cross-cutting (used by every component): Logger (structlog) · Config (pydantic-settings)
                                           Exceptions (core.exceptions) · Retry (tenacity)
```

### 2.1 Component responsibilities

| Component | Responsibility | Explicitly NOT responsible for |
|---|---|---|
| `IngestionService` | Orchestrate fetch → extract → clean for a URL batch; persist `Article` rows | Deciding *how* to extract; knowing about the LLM |
| `GenerationService` | Build the prompt context, call the provider, validate JSON, persist `NewsletterDraft` | HTTP mechanics; prompt text (owned by registry) |
| `CampaignService` | Campaign lifecycle state machine, drafts, history, duplication | Sending; rendering |
| `DeliveryService` | Recipient validation, batching, send loop, retry, per-recipient result | Template rendering; provider SDK specifics |
| `SettingsService` | Read/write settings, secret masking, connection tests | Where secrets are stored (config layer) |
| `ArticleExtractor` | Return a structured `ExtractedArticle` from a URL or raw HTML | Cleaning, tokenization |
| `TextCleaner` | Deterministic, pure text normalization + token-budget truncation | Network, persistence |
| `LLMProvider` | `generate(prompt, schema, params) -> dict`; health check | Prompt content; business validation |
| `PromptRegistry` | Load, version, and render prompt templates | Calling the model |
| `TemplateRenderer` | `render(draft, template_id, brand) -> (html, text)` | Sending |
| `EmailProvider` | `send(message) -> SendResult`; verify credentials | Batching, retry policy (owned by DeliveryService) |
| Repositories | Persistence and queries only | Business rules |

---

## 3. Sequence Diagrams

### 3.1 Primary flow — generate a newsletter

```
Priya    UI(Generate)   IngestionSvc   Scraper  Cleaner  GenerationSvc  PromptReg  LLMProvider  Repo
  │           │              │            │        │          │             │           │        │
  ├─paste URLs┤              │            │        │          │             │           │        │
  ├─click Generate──────────▶│            │        │          │             │           │        │
  │           │              │            │        │          │             │           │        │
  │           │  validate_urls() ─── invalid? ──▶ return per-URL errors, continue with valid
  │           │              │            │        │          │             │           │        │
  │           │              ├─for each URL (bounded concurrency = 4)       │           │        │
  │           │              ├─fetch+extract──────▶│        │          │             │           │
  │           │              │◀──ExtractedArticle──┤        │          │             │           │
  │           │              │   (tier 1 Trafilatura → tier 2 Newspaper4k → tier 3 BS4)         │
  │           │              ├─clean()────────────────────▶ │          │             │           │
  │           │              │◀──CleanedArticle─────────────┤          │             │           │
  │           │              ├─save Article ─────────────────────────────────────────────────▶  │
  │  ◀── progress: "3/3 articles extracted" ──────┤          │             │           │        │
  │           │              │            │        │          │             │           │        │
  │           │              └───────────────────────────────▶│  (hand off cleaned articles)    │
  │           │                                               │             │           │        │
  │           │              health_check() ──────────────────┼─────────────┼──────────▶│        │
  │           │              ◀─── unhealthy? → raise LLMUnavailableError, draft is already saved │
  │           │                                               │             │           │        │
  │           │   STAGE 1: per-article summarization (parallel, max 3)      │           │        │
  │           │                                               ├─get("article_summary", v)▶│      │
  │           │                                               │◀──rendered prompt────────┤       │
  │           │                                               ├─generate(prompt, ArticleSummary schema)▶│
  │           │                                               │◀──validated JSON────────────────┤ │
  │  ◀── progress: "Summarizing 2/3" ─────────────────────────┤             │           │        │
  │           │                                               │             │           │        │
  │           │   STAGE 2: newsletter composition (single call)             │           │        │
  │           │                                               ├─get("newsletter_compose", v)▶│   │
  │           │                                               ├─generate(prompt, Newsletter schema)▶│
  │           │                                               │◀──validated JSON────────────────┤ │
  │           │                                               │   on schema failure: 1 repair retry
  │           │                                               │   then 1 backoff retry, then fail
  │           │                                               ├─save NewsletterDraft + provenance─▶│
  │  ◀── redirect to Campaign Preview with draft_id ──────────┤             │           │        │
```

**Design notes**
- Articles are persisted **before** any LLM call, so an LLM outage never loses extraction work (NFR-R1).
- Two-stage generation: stage 1 is per-article and parallel; stage 2 is a single composition call.
  This keeps each prompt small enough for a 14B model to follow reliably, and lets a single failed
  article be retried without redoing the whole batch.
- Health check happens once, before stage 1, so the failure is fast and legible.

### 3.2 Send flow

```
Priya   UI(Preview)  CampaignSvc  TemplateRenderer  DeliverySvc  EmailProvider   Repo
  │          │            │              │               │             │          │
  ├─edit fields──────────▶│              │               │             │          │
  │          │            ├─save edits (autosave, debounced 2s)────────────────▶  │
  ├─upload CSV───────────▶│              │               │             │          │
  │          │            ├──────────────────────────────▶ validate_recipients()  │
  │  ◀── "487 valid · 11 invalid · 2 duplicates · 3 suppressed" ───────┤          │
  ├─click Preview────────▶│              │               │             │          │
  │          │            ├─render(draft, template, brand)▶            │          │
  │          │            │◀──(html, text)┤               │             │          │
  │  ◀── rendered preview ┤              │               │             │          │
  ├─Send Test ───────────────────────────────────────────▶│─send(1)───▶│          │
  │  ◀── "Test sent to priya@vays.com" ───────────────────┤             │          │
  ├─click Send Campaign──▶│              │               │             │          │
  │  ◀── CONFIRM MODAL: "487 recipients · subject · sender" ───────────┤          │
  ├─confirm──────────────▶│              │               │             │          │
  │          │            ├─transition DRAFT→SENDING (DB guard: fails if not DRAFT/READY)──▶│
  │          │            ├──────────────────────────────▶│             │          │
  │          │            │        for each batch of 50:  ├─send_batch─▶│          │
  │          │            │                               │◀──results───┤          │
  │          │            │                               ├─persist SendRecord[]──▶│
  │  ◀── progress bar: sent 150/487 · failed 2 ───────────┤             │          │
  │          │            │        transient failure → tenacity backoff (3 attempts)          │
  │          │            │        permanent failure  → record reason, continue               │
  │          │            ├─transition SENDING→SENT (or PARTIAL_FAILURE)──────────────────▶  │
  │  ◀── summary + failure table + "Retry failed only" ───┤             │          │
```

**Idempotency:** the `SENDING` transition is a conditional UPDATE (`WHERE status IN ('DRAFT','READY')`).
If Streamlit reruns and fires the handler twice, the second attempt affects 0 rows and is rejected.
This is the concrete mitigation for risk R-9 — double-sending a customer newsletter is unacceptable.

### 3.3 LLM failure and recovery

```
GenerationSvc      LLMProvider(Colab)      CircuitBreaker      UI
     ├─generate()─────────▶│                     │              │
     │                     ├─POST /v1/chat/completions          │
     │                     │   ✗ ConnectionError (tunnel dead)  │
     │                     ├─retry 1 (2s backoff) ✗             │
     │                     ├─retry 2 (4s backoff) ✗             │
     │                     ├─retry 3 (8s backoff) ✗             │
     │                     ├─record failure ────▶│              │
     │◀──LLMUnavailableError┤                    │ 3 failures   │
     │                                           │ → OPEN (60s) │
     ├─draft already persisted; no data lost     │              │
     ├───────────────────────────────────────────────────────▶  │
     │   UI shows: "AI service unreachable. Your articles are saved.
     │              Open Settings → update the Colab URL → Test Connection,
     │              then click Retry Generation."
     │                                           │              │
     │  (while OPEN, calls fail instantly — no 30s hang per attempt)
     │  after 60s → HALF_OPEN → next call probes; success → CLOSED
```

---

## 4. Folder Structure

```
vays-ai-new/
├── app.py                          # Streamlit entrypoint: auth gate, nav, global config
├── requirements.txt
├── requirements-dev.txt
├── pyproject.toml                  # ruff, mypy, pytest, import-linter config
├── .env.example                    # every variable documented; committed
├── .env                            # real secrets; git-ignored
├── .gitignore
├── README.md                       # installation → config → run → troubleshoot → handover
├── CLAUDE.md                       # project memory / architect context
│
├── config/
│   ├── __init__.py
│   ├── settings.py                 # pydantic-settings Settings (single source of truth)
│   ├── constants.py                # enums-adjacent constants, limits, timeouts
│   └── logging_config.py           # structlog setup, processors, handlers
│
├── core/                           # PURE — no I/O, no frameworks
│   ├── __init__.py
│   ├── models.py                   # Pydantic DTOs crossing every boundary
│   ├── enums.py                    # CampaignStatus, Tone, Length, Audience, Category
│   ├── exceptions.py               # exception hierarchy (§7)
│   ├── validators.py               # URL, email, CSV validators (incl. SSRF guard)
│   └── schemas.py                  # JSON Schemas handed to guided decoding
│
├── modules/                        # ADAPTERS — the replaceable parts
│   ├── scraper/
│   │   ├── base.py                 # ExtractorStrategy Protocol
│   │   ├── fetcher.py              # HTTP with timeout/retry/UA/robots
│   │   ├── trafilatura_extractor.py
│   │   ├── newspaper_extractor.py
│   │   ├── fallback_extractor.py   # BeautifulSoup heuristic
│   │   └── extractor.py            # ArticleExtractor — cascade orchestrator
│   ├── cleaner/
│   │   ├── text_cleaner.py         # normalize, strip boilerplate, dedupe
│   │   └── tokenizer.py            # token estimation + smart truncation
│   ├── ai/
│   │   ├── base.py                 # LLMProvider ABC + LLMResponse
│   │   ├── colab_provider.py       # OpenAI-compatible client → tunnel
│   │   ├── hosted_provider.py      # hosted open-weight fallback (same wire format)
│   │   ├── mock_provider.py        # deterministic JSON fixtures — offline dev + tests
│   │   │                           # (no model, no weights, no GPU, no network)
│   │   ├── factory.py              # provider selection from config
│   │   ├── circuit_breaker.py
│   │   ├── prompt_registry.py      # load/version/render YAML prompts
│   │   └── engine.py               # AIEngine: 2-stage pipeline + JSON validation/repair
│   ├── template/
│   │   ├── renderer.py             # Jinja2 render + CSS inlining + text alternative
│   │   └── brand.py                # brand asset resolution
│   ├── email/
│   │   ├── base.py                 # EmailProvider ABC + EmailMessage/SendResult
│   │   ├── brevo_provider.py
│   │   ├── smtp_provider.py
│   │   ├── console_provider.py     # dev: writes .eml to disk, sends nothing
│   │   ├── factory.py
│   │   └── batcher.py              # batching + rate limiting + backoff
│   └── repository/
│       ├── database.py             # engine, session factory, init
│       ├── orm_models.py           # SQLAlchemy declarative models
│       ├── article_repo.py
│       ├── campaign_repo.py
│       ├── recipient_repo.py
│       ├── send_repo.py
│       ├── log_repo.py
│       └── settings_repo.py
│
├── services/                       # USE CASES — orchestration, transactions
│   ├── ingestion_service.py
│   ├── generation_service.py
│   ├── campaign_service.py
│   ├── delivery_service.py
│   ├── settings_service.py
│   └── health_service.py
│
├── ui/
│   ├── pages/
│   │   ├── 1_Dashboard.py
│   │   ├── 2_Generate.py
│   │   ├── 3_Preview.py
│   │   ├── 4_History.py
│   │   ├── 5_Settings.py
│   │   └── 6_Logs.py
│   ├── components/                 # reusable widgets (status chip, url input, editor…)
│   ├── state.py                    # typed session-state keys + accessors
│   └── styles.py                   # injected CSS, theme constants
│
├── prompts/                        # VERSIONED, GIT-TRACKED
│   ├── article_summary/v1.0.0.yaml
│   ├── newsletter_compose/v1.0.0.yaml
│   ├── field_regenerate/v1.0.0.yaml
│   ├── subject_variants/v1.0.0.yaml
│   └── _shared/  (system persona, tone/audience fragments, few-shot exemplars)
│
├── templates/
│   ├── email/
│   │   ├── src/*.mjml              # authored source
│   │   ├── modern.html             # compiled output (committed)
│   │   ├── classic.html
│   │   └── minimal.html
│   └── partials/
│
├── notebooks/
│   └── colab_llm_server.ipynb      # the LLM host notebook
│
├── data/                           # git-ignored
│   ├── app.db
│   ├── uploads/
│   └── exports/
├── logs/                           # git-ignored
├── assets/                         # logo, placeholder images
│
├── tests/
│   ├── conftest.py                 # fixtures: temp DB, mock providers, sample HTML
│   ├── unit/
│   ├── integration/
│   ├── fixtures/                   # saved HTML pages, sample LLM responses, CSVs
│   └── e2e/
│
├── migrations/                     # Alembic
└── docs/                           # this document set + runbook + ADRs
```

**Why this layout:** a new developer can predict where anything lives from its category alone.
`modules/` = things that talk to the outside world and might be swapped. `services/` = the verbs
of the product. `core/` = the nouns. `ui/` = pixels. That mapping is the whole mental model.

---

## 5. API Design

### 5.1 Internal service API (v1 — Python interfaces, not HTTP)

There is no HTTP API in v1 (decision D-1). These are the **contracts** the UI calls. They are
designed so that a FastAPI layer can be laid over them later with a one-to-one mapping — every
signature takes and returns Pydantic models, so `@app.post()` decorators are all that's missing.

```python
# services/ingestion_service.py
class IngestionService:
    def ingest_urls(
        self, urls: list[str], *, on_progress: ProgressCB | None = None
    ) -> IngestionResult: ...
    def ingest_manual(self, title: str, text: str, source_url: str | None = None) -> Article: ...
    def get_article(self, article_id: int) -> Article: ...


# services/generation_service.py
class GenerationService:
    def generate(
        self, req: GenerationRequest, *, on_progress: ProgressCB | None = None
    ) -> NewsletterDraft: ...
    def regenerate_field(
        self, draft_id: int, field: EditableField, instruction: str | None = None
    ) -> FieldResult: ...
    def generate_subject_variants(self, draft_id: int, n: int = 3) -> list[str]: ...


# services/campaign_service.py
class CampaignService:
    def create_draft(self, draft: NewsletterDraft, name: str) -> Campaign: ...
    def update_content(self, campaign_id: int, patch: ContentPatch) -> Campaign: ...
    def get(self, campaign_id: int) -> CampaignDetail: ...
    def list(self, f: CampaignFilter) -> Page[CampaignSummary]: ...
    def duplicate(self, campaign_id: int) -> Campaign: ...
    def delete(self, campaign_id: int) -> None: ...
    def transition(self, campaign_id: int, to: CampaignStatus) -> Campaign: ...  # guarded


# services/delivery_service.py
class DeliveryService:
    def validate_recipients(self, csv_bytes: bytes) -> RecipientValidation: ...
    def render(self, campaign_id: int, template_id: str) -> RenderedEmail: ...
    def send_test(self, campaign_id: int, to: EmailStr, template_id: str) -> SendResult: ...
    def send_campaign(
        self, campaign_id: int, *, on_progress: ProgressCB | None = None
    ) -> CampaignSendReport: ...
    def retry_failed(self, campaign_id: int) -> CampaignSendReport: ...
```

### 5.2 External API consumed — LLM (OpenAI-compatible)

Choosing the OpenAI wire format is deliberate: it is the de-facto standard that vLLM, Ollama,
llama.cpp, Together, Groq, OpenRouter and HF all speak. Adopting it means switching providers is
a base-URL change, not a client rewrite.

**Request**
```http
POST {LLM_BASE_URL}/v1/chat/completions
Authorization: Bearer {LLM_API_KEY}
Content-Type: application/json

{
  "model": "Qwen/Qwen3-14B-AWQ",
  "messages": [
    {"role": "system", "content": "<persona + rules>"},
    {"role": "user",   "content": "<rendered prompt>"}
  ],
  "temperature": 0.7,
  "top_p": 0.9,
  "max_tokens": 2048,
  "response_format": {"type": "json_schema", "json_schema": {"name": "newsletter", "schema": { ... }, "strict": true}},
  "extra_body": {"guided_decoding_backend": "xgrammar"}
}
```

**Success response** — the assistant message content is a JSON string conforming to the schema.
**Errors handled:** `401` → bad token (config error, no retry) · `404` → wrong model name (no retry)
· `422` → schema rejected (repair path) · `429` → rate limit (backoff retry) · `5xx` → backoff retry
· `ConnectionError`/`Timeout` → backoff retry then circuit-breaker open.

**Health probe:** `GET {LLM_BASE_URL}/v1/models` — cheap, no generation, confirms both tunnel and
model load. Cached for 30 s to avoid probe storms from Streamlit reruns.

### 5.3 External API consumed — Email (Brevo)

```http
POST https://api.brevo.com/v3/smtp/email
api-key: {BREVO_API_KEY}

{
  "sender":   {"name": "Vays Infotech", "email": "newsletter@vaysinfotech.com"},
  "to":       [{"email": "user@example.com", "name": "User"}],
  "subject":  "…",
  "htmlContent": "…",
  "textContent": "…",
  "headers":  {"List-Unsubscribe": "<mailto:unsub@…>, <https://…/unsub?t=…>",
               "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
  "tags":     ["campaign-42"]
}
```
Handled: `201` success · `400` invalid payload (permanent) · `401` bad key (config error)
· `402` credit exhausted (halt campaign, surface clearly) · `429` rate limit (backoff)
· `5xx` (backoff).

### 5.4 Future HTTP API (v2 sketch, for handover planning)

`POST /api/v1/articles/extract` · `POST /api/v1/generate` · `GET|PATCH /api/v1/campaigns/{id}`
· `POST /api/v1/campaigns/{id}/send` · `GET /api/v1/health`. Documented now so the service
signatures in §5.1 are designed against it, not retrofitted to it.

---

## 6. Database Schema

**Engine:** SQLite (v1) via SQLAlchemy 2.x ORM; Alembic for migrations.
**Migration path:** every column type chosen is Postgres-compatible; `JSON` columns use SQLAlchemy's
generic `JSON` type which maps to `JSONB` on Postgres. No SQLite-specific SQL anywhere.

```sql
-- ─────────── articles: extracted source content ───────────
CREATE TABLE articles (
    id                INTEGER PRIMARY KEY,
    url               TEXT,                       -- NULL for manually pasted
    url_hash          TEXT,                       -- sha256(normalized url), for dedupe lookup
    title             TEXT NOT NULL,
    author            TEXT,
    published_at      DATETIME,
    raw_text          TEXT NOT NULL,
    cleaned_text      TEXT NOT NULL,
    word_count        INTEGER NOT NULL,
    token_estimate    INTEGER NOT NULL,
    language          TEXT,
    extractor_used    TEXT NOT NULL,              -- trafilatura | newspaper4k | fallback | manual
    extraction_ms     INTEGER,
    status            TEXT NOT NULL,              -- EXTRACTED | FAILED
    error_message     TEXT,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_articles_url_hash ON articles(url_hash);
CREATE INDEX ix_articles_created  ON articles(created_at DESC);

-- ─────────── campaigns: the central aggregate ───────────
CREATE TABLE campaigns (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'DRAFT',
        -- DRAFT | READY | SENDING | SENT | PARTIAL_FAILURE | FAILED | ARCHIVED

    -- AI original (immutable audit record — never overwritten by edits)
    ai_title          TEXT, ai_summary      TEXT, ai_newsletter TEXT,
    ai_subject        TEXT, ai_preview_text TEXT, ai_cta        TEXT,
    ai_keywords       JSON, ai_category     TEXT, ai_tone       TEXT,

    -- Final content (what actually ships; starts as a copy of ai_*)
    title             TEXT, summary      TEXT, newsletter TEXT,
    subject           TEXT, preview_text TEXT, cta        TEXT,
    keywords          JSON, category     TEXT, tone       TEXT,
    cta_url           TEXT,

    -- generation provenance (reproducibility)
    model_name        TEXT, prompt_version TEXT, provider TEXT,
    generation_params JSON,               -- temperature, top_p, max_tokens, tone/length/audience
    generation_ms     INTEGER,
    regeneration_count INTEGER NOT NULL DEFAULT 0,

    -- rendering
    template_id       TEXT DEFAULT 'modern',
    rendered_html     TEXT,
    rendered_text     TEXT,

    -- delivery rollup (denormalized for fast History listing)
    recipient_count   INTEGER DEFAULT 0,
    sent_count        INTEGER DEFAULT 0,
    failed_count      INTEGER DEFAULT 0,

    created_by        TEXT,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at           DATETIME
);
CREATE INDEX ix_campaigns_status  ON campaigns(status);
CREATE INDEX ix_campaigns_created ON campaigns(created_at DESC);

-- ─────────── link table: which articles fed which campaign ───────────
CREATE TABLE campaign_articles (
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    article_id   INTEGER NOT NULL REFERENCES articles(id)  ON DELETE RESTRICT,
    position     INTEGER NOT NULL,
    section_summary TEXT,               -- stage-1 output, kept for provenance display
    PRIMARY KEY (campaign_id, article_id)
);

-- ─────────── recipients (per campaign snapshot) ───────────
CREATE TABLE recipients (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    name         TEXT,
    company      TEXT,
    extra        JSON,                  -- arbitrary merge fields from the CSV
    is_valid     BOOLEAN NOT NULL DEFAULT 1,
    invalid_reason TEXT,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (campaign_id, email)         -- dedupe enforced at the DB, not just in code
);
CREATE INDEX ix_recipients_campaign ON recipients(campaign_id);

-- ─────────── per-recipient send outcome ───────────
CREATE TABLE send_records (
    id             INTEGER PRIMARY KEY,
    campaign_id    INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    recipient_id   INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
    status         TEXT NOT NULL,       -- QUEUED | SENT | FAILED | BOUNCED | SUPPRESSED
    provider_message_id TEXT,
    error_code     TEXT,
    error_message  TEXT,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    batch_number   INTEGER,
    sent_at        DATETIME,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX ix_send_campaign_status ON send_records(campaign_id, status);

-- ─────────── global suppression list (never send again) ───────────
CREATE TABLE suppressions (
    email      TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,           -- UNSUBSCRIBED | HARD_BOUNCE | COMPLAINT | MANUAL
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ─────────── structured application logs (UI-queryable) ───────────
CREATE TABLE app_logs (
    id          INTEGER PRIMARY KEY,
    ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level       TEXT NOT NULL,          -- DEBUG..CRITICAL
    logger      TEXT NOT NULL,
    event       TEXT NOT NULL,
    campaign_id INTEGER,
    correlation_id TEXT,
    context     JSON,
    exception   TEXT
);
CREATE INDEX ix_logs_ts    ON app_logs(ts DESC);
CREATE INDEX ix_logs_level ON app_logs(level);
CREATE INDEX ix_logs_corr  ON app_logs(correlation_id);

-- ─────────── key/value settings (non-secret) ───────────
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       JSON NOT NULL,
    is_secret   BOOLEAN NOT NULL DEFAULT 0,   -- secrets store a reference, never the value
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by  TEXT
);

-- ─────────── users ───────────
CREATE TABLE users (
    username      TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,        -- bcrypt
    role          TEXT NOT NULL DEFAULT 'editor',   -- editor | approver | admin
    is_active     BOOLEAN NOT NULL DEFAULT 1,
    last_login_at DATETIME,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 6.1 Two schema decisions worth defending

**Why store both `ai_*` and final columns rather than a separate revisions table?**
The product needs exactly one comparison — *what the AI wrote* vs *what shipped* — to compute the
edit-ratio quality metric (PRD §7.1) and to show the diff (FR-4.6). A full revision-history table
would be more general and less useful; this is the smallest design that answers the actual question.
If per-keystroke history is ever needed, it becomes an additive `campaign_revisions` table.

**Why snapshot recipients per campaign instead of a global contacts table?**
A campaign must be reproducible and auditable: "who received campaign 42" must stay true even if
the master list changes tomorrow. A shared contacts table with a join would let history silently
mutate. A global `contacts` table can be added later for list management, feeding *into* the
snapshot at send time.

### 6.2 Campaign state machine

```
   DRAFT ──edit/complete──▶ READY ──confirm send──▶ SENDING ──┬──all ok──▶ SENT
     ▲                        │                               ├──partial─▶ PARTIAL_FAILURE
     └───────edit again───────┘                               └──all fail▶ FAILED
                                                                     │
   any state ───archive───▶ ARCHIVED                    retry_failed ─┘
```
Illegal transitions raise `InvalidStateTransition`. Transitions are conditional UPDATEs, which is
what makes double-send impossible (§3.2).

---

## 7. Error Handling Strategy

### 7.1 Exception hierarchy

```python
NewsletterAppError                      # base — every app error inherits this
├── ConfigurationError                  # missing/invalid settings — fatal at startup
├── ValidationError                     # bad user input — 400-equivalent
│   ├── InvalidURLError
│   ├── InvalidEmailError
│   └── InvalidCSVError
├── ExtractionError
│   ├── FetchError                      # network/HTTP layer
│   ├── ContentTooShortError            # extracted but unusable
│   └── AllExtractorsFailed             # every cascade tier exhausted
├── AIError
│   ├── LLMUnavailableError             # unreachable / circuit open
│   ├── LLMTimeoutError
│   ├── InvalidJSONResponse             # schema violation after repair attempts
│   └── PromptNotFoundError
├── TemplateError
├── EmailError
│   ├── EmailProviderError              # provider rejected the request
│   ├── EmailAuthError                  # bad credentials
│   ├── EmailQuotaExceeded
│   └── PartialSendFailure              # carries the per-recipient report
└── PersistenceError
    └── InvalidStateTransition
```

### 7.2 Handling rules

| Rule | Detail |
|---|---|
| **Fail fast on config** | Invalid settings raise at import time, before the UI renders. A misconfigured app must not appear to work. |
| **Degrade, don't crash, on integrations** | LLM or email down → app still starts, shows a banner, disables the affected action with an explanatory tooltip. |
| **Retry only what's retryable** | Timeouts, `429`, `5xx`, connection resets → `tenacity` exponential backoff with jitter (3 attempts). `4xx` other than 429 → no retry; retrying a `401` just wastes time and hides the real problem. |
| **Circuit breaker on the LLM** | 3 consecutive failures → OPEN for 60 s → HALF_OPEN probe. Prevents a 6-URL batch from taking 6 × 90 s to discover the same dead tunnel. |
| **Partial failure is a first-class outcome** | Batch operations return a result object with successes *and* failures. They never raise on "some failed" — the caller decides. |
| **Translate at the boundary** | UI catches `NewsletterAppError` and maps it to a user message via a single `ERROR_MESSAGES` mapping. Any bare `Exception` reaching the UI logs a full traceback with a correlation ID and shows: *"Something went wrong. Reference: `a3f9c2`. Check the Logs page."* |
| **Never swallow** | No bare `except: pass`. Every caught exception is either handled, re-raised, or logged with context. Enforced by a ruff rule. |

### 7.3 User-facing message contract

Every error message answers three questions in this order: **what happened · why · what to do.**

| Internal exception | Shown to Priya |
|---|---|
| `AllExtractorsFailed` | "Couldn't read the article at `<url>` — the site may block automated access. Use **Paste manually** to continue." |
| `LLMUnavailableError` | "The AI service isn't responding. Your articles are saved. Go to **Settings → Test Connection** and update the endpoint URL if your Colab session restarted." |
| `InvalidJSONResponse` | "The AI returned an unexpected response. Click **Retry** — this usually resolves on the second attempt." |
| `EmailAuthError` | "Email credentials were rejected. Check the API key in **Settings → Email**." |
| `EmailQuotaExceeded` | "Daily email limit reached (300 on the free plan). 240 of 487 were sent. Resume tomorrow or upgrade the plan." |
| `PartialSendFailure` | "Sent to 485 of 487. 2 addresses failed — see the table below, then **Retry failed only**." |

---

## 8. Logging Strategy

### 8.1 Design

**`structlog` producing JSON lines**, with three sinks:

| Sink | Level | Purpose | Retention |
|---|---|---|---|
| Console | INFO (DEBUG in dev) | Developer feedback while running | ephemeral |
| `logs/app.jsonl` (rotating, 10 MB × 5) | DEBUG | Full forensic trail | ~50 MB |
| `app_logs` table | INFO+ | Powers the in-app Logs page for non-technical users | 90 days, pruned on startup |

Why JSON rather than pretty text: the Logs page needs to filter by level, search by campaign, and
correlate a whole request. Parsing formatted strings to do that is a mistake that's expensive to
undo later.

### 8.2 Correlation IDs

Every user-initiated operation generates a `correlation_id` (short uuid4) bound into the structlog
context for its lifetime. Every log line from that operation carries it. When Priya reports "it
failed around 3pm", the reference code shown in the error message retrieves the exact chain of
events. This is the single highest-value logging feature for a system with an unreliable
external dependency.

### 8.3 What is logged

```python
log.info(
    "article.extracted",
    url=url,
    extractor="trafilatura",
    word_count=1240,
    duration_ms=832,
    correlation_id=cid,
)

log.warning(
    "extractor.fallback",
    url=url,
    failed_tier="trafilatura",
    reason="content_too_short",
    word_count=87,
    correlation_id=cid,
)

log.info(
    "llm.request",
    provider="colab",
    model="Qwen3-14B-AWQ",
    prompt="newsletter_compose",
    prompt_version="1.0.0",
    input_tokens=3400,
    correlation_id=cid,
)

log.error(
    "llm.failed",
    provider="colab",
    attempt=3,
    error_type="ConnectionError",
    circuit_state="OPEN",
    correlation_id=cid,
)

log.info(
    "campaign.sent",
    campaign_id=42,
    attempted=487,
    sent=485,
    failed=2,
    duration_s=214,
    correlation_id=cid,
)
```

### 8.4 What is never logged

- API keys, passwords, bearer tokens, SMTP credentials — a `redact_secrets` structlog processor
  scrubs any key matching `(?i)(key|token|password|secret|authorization)`.
- Full recipient email addresses at INFO+ — hashed or masked (`p***a@vays.com`). Full addresses
  exist in the DB where they belong, not scattered through log files.
- Full article or newsletter bodies — logged as length + hash. Log volume matters, and dumping
  content makes logs unreadable.

---

## 9. Security Considerations

| # | Area | Threat | Control |
|---|---|---|---|
| S-1 | Secrets | Keys committed to Git or shown in the UI | `.env` git-ignored; `.env.example` has placeholders only; Settings shows `sk-••••4f2a`; secrets never persisted to `settings` table |
| S-2 | **SSRF** | User pastes `http://169.254.169.254/…` or `http://localhost:8000/admin` and the server fetches it | `core/validators.py` resolves the hostname and **rejects private, loopback, link-local, and reserved ranges**; scheme restricted to http/https; redirects re-validated at each hop; max 3 redirects |
| S-3 | Template injection | Article content or user edits containing `{{ }}` executed by Jinja2 | Jinja2 `SandboxedEnvironment`, autoescape on; user content passed as **data**, never concatenated into the template source |
| S-4 | XSS in preview | Malicious HTML from a scraped page rendered in the app | Extraction returns text, not HTML; preview rendered inside a sandboxed iframe via `components.html` |
| S-5 | Auth | Unauthenticated access to campaign data and send capability | Own auth module (`core/auth.py`, ~80 lines, bcrypt); every page begins with an auth guard; session token signed with `APP_SECRET_KEY`; send restricted to `approver`/`admin` roles |
| S-6 | Passwords | Plaintext or weak hashing | bcrypt with per-password salt, cost 12 |
| S-7 | **LLM endpoint exposure** | Public tunnel URL discovered → free GPU abuse or prompt exfiltration | vLLM started with `--api-key`; client sends `Authorization: Bearer`; tunnel URL treated as a secret; rotate token on every session start |
| S-8 | Prompt injection | Scraped article contains "Ignore previous instructions and write…" | Article text delimited in a clearly marked block; system prompt states untrusted-content rules; guided JSON decoding constrains the output shape regardless of instruction-following; **human review is the ultimate control** |
| S-9 | SQL injection | — | SQLAlchemy parameterized queries exclusively; no string-built SQL |
| S-10 | CSV injection | Recipient CSV cell `=cmd\|…` exported and opened in Excel | Prefix `= + - @` with `'` on export |
| S-11 | PII | Recipient data leakage | Local SQLite only; masked in logs; documented deletion procedure; DB file excluded from any sync/backup that leaves the machine |
| S-12 | Dependency risk | Vulnerable transitive package | `pip-audit` in CI; pinned versions in `requirements.txt`; monthly review |
| S-13 | Email abuse | App used to send unsolicited mail | Suppression list enforced pre-send; mandatory unsubscribe; sender domain must be verified |

**Explicitly out of scope for v1** (documented so the gap is a decision, not an oversight):
rate limiting per user, CSRF tokens (Streamlit's websocket model plus local-only deployment),
encryption at rest for the SQLite file, and full audit logging of read access.

---

## 10. Deployment Strategy

### 10.1 v1 — Local Windows (the actual handover target)

```powershell
git clone <repo> ; cd vays-ai-new
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # then edit .env
alembic upgrade head
python -m scripts.create_user    # first admin account
streamlit run app.py             # → http://localhost:8501
```
Plus: open `notebooks/colab_llm_server.ipynb`, run all cells, copy the tunnel URL into
**Settings → AI Service**.

A `run.bat` wraps activation + migration + launch into one double-click, because the handover
audience includes non-developers who will need to restart it.

### 10.2 v1.5 — Shared internal host

Windows Server or a small Linux VM on the company LAN, Streamlit behind nginx (TLS + sticky
sessions — required, since Streamlit session state is per-process), run as a service (NSSM on
Windows / systemd on Linux). LLM moves to a company GPU box or a hosted open-weight endpoint.

### 10.3 v2 — Containerized

`docker-compose.yml` with app + Postgres + (optionally) a vLLM service with GPU passthrough.
Multi-stage Dockerfile, non-root user, healthcheck endpoint. Documented in the TRD now so the
schema and config choices stay compatible with it; not built in v1.

### 10.4 Environment matrix

| Env | LLM provider | Email provider | DB | Log level |
|---|---|---|---|---|
| `local` | `mock` | `console` (writes `.eml` files) | SQLite temp | DEBUG |
| `dev` | `colab` | `smtp` (Mailtrap sandbox) | SQLite | DEBUG |
| `staging` | `hosted` | `brevo` (test list) | SQLite/Postgres | INFO |
| `prod` | `hosted` or company GPU | `brevo` | Postgres | INFO |

The `local` profile matters: it means the full app — including tests and UI development — runs
with **no GPU, no network, and no email account**. On an 8 GB laptop, that is the difference
between a productive day and a blocked one.

---

## 11. Configuration Management

### 11.1 Precedence

`CLI/env vars` → `.env` → `settings.yaml` (non-secret, version-controlled defaults) → code defaults.
Secrets exist **only** in env/`.env`. Runtime-adjustable non-secrets (brand colour, batch size,
default tone) live in the `settings` DB table so Priya can change them without a developer.

### 11.2 Implementation

```python
# config/settings.py
class LLMSettings(BaseSettings):
    provider: Literal["colab", "hosted", "local", "mock"] = "colab"
    base_url: HttpUrl = "http://localhost:8000"
    api_key: SecretStr = SecretStr("")
    model: str = "Qwen/Qwen3-14B-AWQ"
    timeout_s: int = 120
    max_retries: int = 3
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(2048, ge=256, le=8192)
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env")


class Settings(BaseSettings):
    app_env: Literal["local", "dev", "staging", "prod"] = "dev"
    llm: LLMSettings = LLMSettings()
    email: EmailSettings = EmailSettings()
    scraper: ScraperSettings = ScraperSettings()
    database: DatabaseSettings = DatabaseSettings()
    ...


@lru_cache
def get_settings() -> Settings: ...  # single instance, imported everywhere
```

Pydantic validation means a typo in `.env` produces *"LLM_TEMPERATURE: input should be ≤ 2.0"* at
startup, not a confusing model failure an hour later.

---

## 12. Environment Variables

```ini
# ─── Application ───────────────────────────────────────────
APP_ENV=dev                       # local | dev | staging | prod
APP_SECRET_KEY=                   # REQUIRED. random 32+ chars, signs session cookies
LOG_LEVEL=INFO

# ─── LLM ───────────────────────────────────────────────────
LLM_PROVIDER=colab                # colab | hosted | mock   (all remote or fixture-based)
LLM_BASE_URL=https://<random>.trycloudflare.com   # changes each Colab session
LLM_API_KEY=                      # bearer token; must match the notebook's --api-key
LLM_MODEL=Qwen/Qwen3-14B-AWQ
LLM_TIMEOUT_S=120
LLM_MAX_RETRIES=3
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=2048
LLM_CIRCUIT_FAILURE_THRESHOLD=3
LLM_CIRCUIT_RESET_S=60

# ─── Email ─────────────────────────────────────────────────
EMAIL_PROVIDER=brevo              # brevo | smtp | console
EMAIL_SENDER_NAME=Vays Infotech
EMAIL_SENDER_ADDRESS=newsletter@vaysinfotech.com
EMAIL_REPLY_TO=marketing@vaysinfotech.com
EMAIL_BATCH_SIZE=50
EMAIL_BATCH_DELAY_S=2
EMAIL_MAX_RETRIES=3
BREVO_API_KEY=
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_USE_TLS=true

# ─── Scraper ───────────────────────────────────────────────
SCRAPER_TIMEOUT_S=20
SCRAPER_MAX_RETRIES=2
SCRAPER_USER_AGENT=VaysNewsletterBot/1.0 (+https://vaysinfotech.com/bot)
SCRAPER_RESPECT_ROBOTS=true
SCRAPER_MAX_CONCURRENT=4
SCRAPER_MIN_WORD_COUNT=200
SCRAPER_MAX_INPUT_TOKENS=6000

# ─── Database ──────────────────────────────────────────────
DATABASE_URL=sqlite:///./data/app.db

# ─── Branding ──────────────────────────────────────────────
BRAND_NAME=Vays Infotech
BRAND_PRIMARY_COLOR=#0B5FFF
BRAND_LOGO_PATH=assets/logo.png
BRAND_WEBSITE=https://vaysinfotech.com
BRAND_ADDRESS=<physical address — legally required in every email>
UNSUBSCRIBE_BASE_URL=https://vaysinfotech.com/unsubscribe
```

`.env.example` ships with every variable, a one-line description, whether it is required, and a
safe default. Startup validation lists **all** missing required variables at once rather than
failing on the first — a small thing that saves real time during handover setup.

---

## 13. Third-Party Dependencies

| Package | Purpose | Licence | Risk / note |
|---|---|---|---|
| `streamlit` | UI framework | Apache-2.0 | Rerun model requires disciplined session state |
| `pydantic` / `pydantic-settings` | DTOs + config validation | MIT | Core to every boundary |
| `sqlalchemy` 2.x | ORM | MIT | Keeps the Postgres path open |
| `alembic` | Migrations | MIT | Essential once real campaign data exists |
| `trafilatura` | Primary extractor | Apache-2.0 | Best benchmark F1; actively maintained |
| `newspaper4k` | Fallback extractor | MIT | Maintained successor to abandoned `newspaper3k` |
| `beautifulsoup4` + `lxml` | Last-resort extractor | MIT / BSD | — |
| `httpx` | HTTP client | BSD | Async + sync, connection pooling, HTTP/2 |
| `openai` (SDK, pointed at vLLM) | OpenAI-compatible client | Apache-2.0 | Used as a *protocol client*, not for OpenAI models — no proprietary model dependency |
| `tenacity` | Retry/backoff | Apache-2.0 | — |
| `jinja2` | Templating | BSD | Sandboxed environment |
| `premailer` | CSS inlining for email | BSD | Required for Outlook/Gmail |
| ~~`html2text`~~ **removed** | HTML → plain-text alternative | ~~GPL-3.0~~ | **DROPPED (D-16).** GPL is unacceptable in a commercial deliverable, and the conversion is unnecessary: the plain-text part is composed directly from the `NewsletterContent` fields (which are already plain text). Zero dependency, better output. |
| `sib-api-v3-sdk` / direct `httpx` to Brevo | Email delivery | MIT | Direct httpx preferred — fewer transitive deps, full error visibility |
| `email-validator` | RFC-compliant address validation | Unlicense/CC0 | — |
| `structlog` | Structured logging | Apache-2.0/MIT | — |
| ~~`streamlit-authenticator`~~ **removed** | Auth widget | Apache-2.0 | **DROPPED (D-15).** Unvetted small third-party package sitting on the security boundary. Replaced by `bcrypt` + ~80 lines in `core/auth.py` — fully auditable, no supply-chain surface on the auth path. |
| `bcrypt` | Password hashing | Apache-2.0 | Single-purpose, widely audited |
| `pandas` | CSV handling, History tables | BSD | Heavy (~50 MB) but Streamlit already depends on it |
| **(none — pure-Python heuristic)** | Token counting | — | **D-14.** `transformers`/`tiktoken` were rejected: `transformers` drags in torch (~2 GB) and `tiktoken` downloads BPE files at runtime. We only need a ±10% budget estimate, which a character/word heuristic calibrated once against Qwen3's tokenizer delivers. **This is what keeps torch out of `requirements.txt` entirely.** |
| **Dev:** `pytest`, `pytest-cov`, `pytest-mock`, `respx`, `ruff`, `mypy`, `import-linter`, `pip-audit` | Quality gates | MIT/BSD | — |
| **Build-time only:** `mjml` (Node) | Compile MJML → HTML | MIT | **Not a runtime dependency** — compiled HTML is committed, so the deployed app needs no Node |

### 13.1 Banned dependencies — machine-enforced (D-13)

`scripts/check_no_local_inference.py` runs in CI and as a pre-commit hook. It fails the build if
any of the following appears in `requirements*.txt` or in an `import` statement anywhere in the
source tree:

```
torch · tensorflow · jax · transformers · sentence-transformers · accelerate
llama-cpp-python · ctransformers · onnxruntime · onnxruntime-gpu
vllm · ollama · gpt4all · exllamav2 · autoawq · bitsandbytes · optimum
```

This converts *"no LLM runs on your machine"* from a promise into a build failure. It is the only
form of guarantee that survives handover to a developer who wasn't in this conversation and who
might reasonably think adding `transformers` for "just a tokenizer" is harmless.

**MJML decision, stated plainly:** MJML is a Node tool and this is a Python project. Making Node a
runtime requirement on a Windows handover machine is a real cost. The resolution is to treat MJML
as a **build-time asset compiler**: author `.mjml`, compile once, commit the `.html`, and have
Jinja2 fill it at runtime. Full Outlook compatibility, zero Node at runtime.

---

## 14. Scalability Considerations

**Honest position:** v1 is designed for ~5 concurrent users and ~10k recipients per campaign.
That is the actual requirement. Building for more would be speculative work. What matters is that
the *ceilings are known* and the *escape hatches are pre-designed*.

| Dimension | v1 ceiling | First symptom | Escape hatch |
|---|---|---|---|
| Concurrent users | ~5 | Slow reruns; SQLite `database is locked` | WAL mode (already on) → Postgres (`DATABASE_URL` change only) |
| Recipients/campaign | ~10,000 | Send blocks the UI thread for minutes | Move `send_campaign` to a background thread with DB-backed progress → then Celery/RQ + Redis |
| Articles per generation | ~10 | Prompt exceeds context; quality drops | Map-reduce summarization (stage 1 already is the map step) |
| LLM throughput | 1 request at a time on a T4 | Queueing during concurrent generation | vLLM continuous batching already handles this; scale up GPU or move to a hosted endpoint |
| Campaign history | ~100k rows | History page slows | Already indexed + paginated; add archival partitioning |
| Log volume | 50 MB rotating | — | 90-day pruning on startup; ship to a log service if ever needed |

**Deliberate design choices that preserve the escape hatches:**
1. No raw SQL → Postgres migration is a URL change.
2. `send_campaign` accepts an `on_progress` callback → moving it to a worker doesn't change the signature.
3. Services never import Streamlit → they can run in a worker or behind FastAPI unchanged.
4. All external calls go through adapters → scaling any one integration is localized.

---

## 15. Testing Strategy

### 15.1 Pyramid

```
        ╱ E2E (5%) ╲          Streamlit AppTest: happy path, auth gate, send guard
      ╱ Integration ╲         Service + real SQLite + mocked HTTP/LLM/email
     ╱     (25%)     ╲
    ╱   Unit (70%)    ╲       Pure logic: cleaner, validators, schemas, state machine,
   ╱___________________╲      prompt rendering, batching, retry policy
```

### 15.2 Key techniques

| Technique | Why it matters here |
|---|---|
| **`MockLLMProvider` returning fixture JSON** | The entire pipeline is testable with no GPU, no Colab, no network. This is the single most important testing decision in the project. |
| **Saved HTML fixtures** of real OEM blog pages | Extractor regressions are caught deterministically; no live scraping in tests |
| **`respx`** to mock httpx | Simulate `429`, `500`, timeouts, malformed bodies without hitting anyone's API |
| **`console` email provider** writing `.eml` files | Full send-path testing that cannot accidentally email a real customer |
| **Property-based tests** (`hypothesis`) on the cleaner | Text normalization is exactly the kind of code that breaks on inputs nobody thought of |
| **Schema round-trip tests** | Every prompt's declared JSON Schema is validated against its fixture responses in CI, so a prompt edit that breaks the contract fails the build |
| **Golden-file tests** on rendered HTML | Template changes produce a visible diff rather than a silent layout break |

### 15.3 Critical test cases (non-negotiable)

1. Send is idempotent — invoking the handler twice sends exactly once.
2. An LLM outage mid-generation loses no extracted articles.
3. A malformed CSV row is skipped and reported, not fatal.
4. Suppressed addresses are never sent to, even if present in the uploaded CSV.
5. SSRF guard rejects `localhost`, `127.0.0.1`, `169.254.169.254`, `10.x`, `192.168.x`, and a
   public hostname that DNS-resolves to a private IP.
6. Secrets never appear in log output (assert against captured log records).
7. Illegal state transitions raise.
8. Every rendered email contains an unsubscribe link and a plain-text part.

### 15.4 Coverage targets

`core/` 90% · `modules/cleaner`, `modules/ai` 85% · `modules/scraper`, `modules/email` 75% ·
`services/` 80% · `ui/` not measured (covered by E2E smoke tests). Overall gate: **70%**.

---

## 16. CI/CD Recommendations

### 16.1 Pipeline (GitHub Actions)

```yaml
on: [push, pull_request]

lint:     ruff check . ; ruff format --check .
types:    mypy core/ services/ modules/
arch:     lint-imports              # enforces the layer rule from §1.2
test:     pytest --cov --cov-fail-under=70   (matrix: windows-latest, ubuntu-latest)
security: pip-audit ; detect-secrets scan
prompts:  python -m scripts.validate_prompts   # schema + version integrity
build:    docker build (v2 only)
```

Windows is in the test matrix deliberately: the deployment target is Windows, and path/encoding
bugs that only appear there are exactly the ones that break a handover.

### 16.2 Branching & release

`main` (always deployable) ← PR ← `feat/*` | `fix/*`. Conventional commits. Tags `v1.0.0` with a
CHANGELOG generated from commit history. Every PR must state which milestone it advances.

### 16.3 Pre-commit hooks

`ruff` (lint + format), `detect-secrets`, `end-of-file-fixer`, `check-yaml`. Catching a committed
API key at commit time is worth the 3 seconds it costs.

---

## 17. Handover Documentation Plan

The internship ends; the code stays. Handover artifacts are **acceptance criteria** (PRD §11), not
optional extras.

| Artifact | Audience | Contents |
|---|---|---|
| `README.md` | Everyone | What it is · prerequisites · install · configure · run · common tasks · troubleshooting matrix · FAQ |
| `docs/SETUP_GUIDE.md` | New developer | Step-by-step with screenshots, from a bare Windows machine to a first newsletter |
| `docs/ARCHITECTURE.md` | New developer | Condensed TRD: diagrams, layer rules, "where do I add X?" |
| `docs/RUNBOOK.md` | Operator | Restarting Colab · rotating the tunnel URL · email quota exhausted · DB backup/restore · log locations · per-domain scraper notes |
| `docs/ADR/*.md` | Future maintainer | One page per significant decision: context, options, choice, consequences. Explains *why*, which code never can |
| `docs/API_REFERENCE.md` | Developer | Service-layer signatures and DTOs |
| `docs/PROMPT_GUIDE.md` | Marketing + dev | How to edit prompts safely, how versioning works, how to test a change |
| `.env.example` | Everyone | Every variable, described, with safe defaults |
| `notebooks/colab_llm_server.ipynb` | Developer | Heavily commented, runs top to bottom |
| Handover session + recording | Successor | 60–90 min walkthrough: demo, architecture tour, known issues, roadmap |
| `docs/KNOWN_ISSUES.md` | Successor | Honest list of limitations, workarounds, and what I'd do differently |

**The handover test:** a developer who has never seen this project, given only the repository,
reaches a running app and a sent test newsletter in ≤ 30 minutes without asking a question.
If they can't, the documentation is incomplete — regardless of how good the code is.
