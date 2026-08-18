<div align="center">

# AI Newsletter Generation &amp; Distribution Platform

**Paste a blog URL, get a customer-ready newsletter. Or let the agent do it every month on its own.**

[![Python](https://img.shields.io/badge/Python-3.11%20–%203.14-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B?logo=streamlit&logoColor=white)](https://streamlit.io/)
[![LLM](https://img.shields.io/badge/LLM-gpt--oss--120b%20on%20Groq-023859)](https://groq.com/)
[![Tests](https://img.shields.io/badge/tests-1%2C168%20passing-0E8F6F)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-93%25-0E8F6F)](#testing)
[![License](https://img.shields.io/badge/license-MIT-54ACBF)](LICENSE)

</div>

---

## Overview

Marketing teams at **Vays Infotech** used to build partner newsletters by hand: read the OEM
blog posts, summarise them, rewrite them into customer-facing copy, format the HTML, and send.
The process was slow, inconsistent in tone, and hard to scale across many partners.

This platform automates that pipeline end to end:

```
discover → extract → clean → generate (LLM) → human approves → render → send
```

It runs two ways from the same codebase:

- **Manually** — a marketer pastes blog URLs, reviews and edits the generated copy, and sends.
- **Automatically** — a background agent watches the Vays blog, generates a newsletter from any
  new post, emails management a secure approval link, and sends to the full list on the
  **3rd Wednesday of each month**.

**Human approval is mandatory in both modes.** Nothing reaches a customer that a person has not
read and approved. That is the primary control against the model inventing a claim about an OEM
partner, and it cannot be switched off by configuration.

### Design constraints

Three rules shaped every decision in this repository:

| Constraint | How it is honoured |
|---|---|
| **The LLM must be open-source / open-weight** | `openai/gpt-oss-120b` — Apache 2.0 open weights, despite the name. Groq serves *only* open-source models, so using their API satisfies the constraint more cleanly than self-hosting. |
| **Zero local inference** | No model, no weights, no inference runtime ever runs on the developer machine. This is **machine-enforced**: `scripts/check_no_local_inference.py` fails the build if `torch`, `transformers`, `ollama`, `vllm` or similar appear in any requirements file or import. It runs first in CI. |
| **Handover-ready** | Another developer must clone, configure and run this on a different machine from the documentation alone. |

---

## Key Features

- **Three-tier article extraction** — Trafilatura → Newspaper4k → BeautifulSoup, with a polite
  user agent and `robots.txt` compliance. Measured on eight real OEM articles: **7/8 extracted,
  all at tier 1**; the miss was a non-article page, correctly refused with a manual-paste message.
- **Two-stage LLM generation** — per-article summarisation, then cross-article composition.
  Smaller prompts give better quality on a mid-size model and cheaper retries.
- **Schema-guaranteed JSON** — Groq's `strict: true` constrains decoding to the schema, so
  `json.loads` cannot fail. A repair-retry path covers backends that lack it.
- **Human-in-the-loop approval** — in-app, or by a secure emailed link (random, hashed at rest,
  single-use, expiring, campaign-scoped).
- **Autonomous monthly agent** — WordPress REST discovery with RSS fallback, duplicate-proof by a
  database unique constraint, scheduled sending in a configurable timezone.
- **Four independent send guards** — suppression list, double-send guard, retry-skips-delivered,
  and render-time compliance. Each has a test named after the failure it prevents.
- **Master mailing list** — CSV import that appends, paste-a-list, and removal that deactivates
  rather than deletes so campaign history stays intact.
- **Live-editable settings** — 43 runtime settings changeable in the UI without a restart, with
  secrets structurally excluded from that registry.
- **Full observability** — structured JSON logs to console, file and a searchable in-app Logs
  page, threaded by correlation ID.
- **Three email templates** with CSS inlining, a plain-text alternative, `List-Unsubscribe`
  headers, and CID-embedded logos that actually render in Gmail and Outlook.

---

## System Architecture

A **modular monolith**: one process, four layers, and a hard rule that imports point downward
only. The rule is not a convention — `import-linter` enforces it as one of six build gates.

```
┌─────────────────────────────────────────────────────────────┐
│  ui/          Streamlit — 8 pages, components, styles       │
│               decides what to show; no business rules       │
├─────────────────────────────────────────────────────────────┤
│  services/    orchestration, transactions, workflow         │
│               ZERO Streamlit imports — enforced             │
├─────────────────────────────────────────────────────────────┤
│  modules/     capabilities: discovery · scraper · cleaner   │
│               ai · template · email · repository · tunnel   │
│               each hides one external thing behind an       │
│               interface. Every swap happens here.           │
├─────────────────────────────────────────────────────────────┤
│  core/ config/  models · enums · schemas · validators       │
│                 exceptions · auth · settings · logging      │
│                 imports nothing above. Pure, no I/O.        │
└─────────────────────────────────────────────────────────────┘
```

Everything crossing a boundary is a **Pydantic model**, never a raw dict.

Because `services/` has no Streamlit import, the entire business layer is callable from a script,
a test, a scheduler, or a future HTTP API. `agent_worker.py` is the proof — it drives the whole
pipeline with no UI present.

### Two processes

This is the most important operational fact in the project.

| Process | Started by | Responsibility |
|---|---|---|
| **Dashboard** | `run.bat` | Serves the browser UI, manual generation and sending, settings, the approval review page. **Runs no schedule and discovers nothing.** |
| **Agent worker** | `run_agent.bat` | APScheduler. Discovery job every N hours, dispatch job every 5 minutes. **If it is not running, nothing is automatic.** |

They never call each other. They coordinate purely through database rows, which is why SQLite
runs in WAL mode. *"Why isn't the agent doing anything?"* is almost always that the worker was
never started.

### The automated flow

```
 agent worker                          human                        agent worker
 ───────────                           ─────                        ───────────
 discovery job (every 6h)
   │
   ├─ in-flight campaigns < max?  ──no──→ hold, do nothing
   │  yes
   ├─ discover posts (WP REST → RSS)
   ├─ skip anything already seen (UNIQUE constraint)
   ├─ extract → clean → generate
   └─ status = AWAITING_APPROVAL
         └─ email approval link ────────→ clicks Approve
                                            │
                                     status = APPROVED
                                     approved_at = now (UTC)
                                            │
                                            └──→ dispatch job (every 5 min)
                                                   │
                                                   ├─ now ≥ 3rd Wednesday 11:00?
                                                   │    no → wait
                                                   │    yes
                                                   ├─ resolve recipients
                                                   ├─ four send guards
                                                   └─ batched send → SENT
```

Approving early is always safe: approval never sends, it only makes a campaign *eligible*.

---

## Technology Stack

| Layer | Choice | Why |
|---|---|---|
| **UI** | Streamlit (multipage `st.navigation`) | Fastest path to a polished internal tool in pure Python |
| **Backend** | Pure Python service layer, same process | Avoids a second deployable; framework-agnostic so a FastAPI shell can be added later without a rewrite |
| **LLM model** | `openai/gpt-oss-120b` (Apache 2.0), `gpt-oss-20b` fallback | Open weights; both support `strict` schema enforcement |
| **LLM host** | Groq API (OpenAI-compatible) | Serves only open-source models; no infrastructure to run |
| **HTTP** | `httpx` | Used directly rather than the OpenAI SDK — the retry, timeout and error-mapping behaviour is ours |
| **Database** | SQLite + SQLAlchemy 2.x + Alembic | Zero-install on Windows; keeps the Postgres path open |
| **Scheduler** | APScheduler (`BlockingScheduler`) | In-process, no Redis or Celery |
| **Auth** | Own module — `bcrypt` + ~160 auditable lines | No unvetted third-party package on the security boundary |
| **Extraction** | Trafilatura → Newspaper4k → BeautifulSoup | Highest published F1, actively maintained |
| **Templating** | Hand-authored table HTML + Jinja2 + `premailer` | Node is not installed, so MJML was dropped — an uncompilable source of truth is worse than none |
| **Email** | Brevo API, SMTP, or Console | 300/day free tier; SMTP for Gmail; console writes `.eml` and sends nothing |
| **Logging** | `structlog` → JSON lines + rotating file + SQLite table | Machine-readable; the Logs page reads the table |
| **Config** | `pydantic-settings` + `.env` + a settings table | Fail-fast validation at startup beats a runtime `KeyError` |
| **Testing** | `pytest`, `pytest-cov`, `responses`, fake LLM provider | Tests the whole pipeline with no GPU and no network |

Every runtime dependency is permissively licensed (MIT / BSD / Apache-2.0). `html2text` was
dropped specifically to keep GPL-3.0 out of the distribution.

---

## Project Structure

```
vays-ai-new/
├── app.py                     # Streamlit entry point — bootstrap, auth gate, navigation
├── agent_worker.py            # SEPARATE PROCESS — APScheduler, discovery + dispatch jobs
├── run.bat                    # Start the dashboard (preflight-checks the 4 usual failures)
├── run_agent.bat              # Start the agent worker
├── run_tunnel.bat             # Start ngrok for off-machine approval links
├── install_agent_task.bat     # Register the worker as a Windows scheduled task
│
├── core/                      # Contracts and pure logic — imports nothing above it
│   ├── models.py              #   14 Pydantic models; every boundary object
│   ├── enums.py               #   CampaignStatus, PostState, roles, state machine
│   ├── schemas.py             #   Pydantic → Groq strict JSON schema
│   ├── validators.py          #   URL/email validation + the SSRF guard
│   ├── exceptions.py          #   30+ typed errors, each with a safe user_message
│   └── auth.py                #   bcrypt hashing, HMAC session tokens
│
├── config/
│   ├── settings.py            #   8 settings sections; the send-schedule calculation
│   ├── logging_config.py      #   structlog setup, redaction, correlation IDs
│   └── constants.py           #   paths; creates runtime dirs on startup
│
├── modules/                   # Capabilities — every external dependency hides here
│   ├── discovery/             #   WordPress REST → RSS fallback
│   ├── scraper/               #   3-tier extractor cascade + polite fetcher
│   ├── cleaner/               #   normalise, dedupe, token-budget truncation
│   ├── ai/                    #   LLMProvider (Groq | Hosted | Mock), engine, prompts,
│   │                          #   circuit breaker — THE SWAP POINT for the LLM
│   ├── template/              #   Jinja2 rendering, CSS inlining, brand/logo resolution
│   ├── email/                 #   EmailProvider (Brevo | SMTP | Console), batching, MIME
│   ├── repository/            #   12 ORM models, unit-of-work, per-table repositories
│   └── tunnel.py              #   ngrok detection and public-URL resolution
│
├── services/                  # Orchestration — no Streamlit imports, ever
│   ├── agent_service.py       #   the automation orchestrator
│   ├── approval_service.py    #   token issue/check/approve/reject + notifier
│   ├── dispatch_service.py    #   the send gate: APPROVED and past the schedule
│   ├── generation_service.py  #   two-stage generation (100% test coverage)
│   ├── delivery_service.py    #   the four send guards, then batching
│   ├── ingestion_service.py   #   URLs → extracted, cleaned, persisted articles
│   ├── subscriber_service.py  #   the master mailing list
│   ├── settings_service.py    #   43 live-editable settings; refuses secrets
│   └── auth_service.py        #   login, lockout, user management
│
├── ui/
│   ├── pages/                 #   dashboard, generate, preview, approvals,
│   │                          #   recipients, history, settings, logs
│   ├── components.py          #   shared widgets
│   ├── session.py             #   cookie-backed session persistence
│   ├── state.py               #   typed session_state access
│   └── styles.py              #   the LUNA palette
│
├── prompts/                   # Versioned YAML prompts (never edited in place)
│   ├── article_summary/       #   stage 1
│   ├── newsletter_compose/    #   stage 2
│   ├── field_regenerate/      #   regenerate one field
│   ├── subject_variants/      #   alternative subject lines
│   └── _shared/               #   voice, length rules, persona, untrusted-input rules
│
├── templates/
│   ├── email/                 #   classic · modern · minimal (the newsletter templates)
│   └── internal/              #   approval_request.html — kept SEPARATE on purpose
│
├── migrations/versions/       # 5 Alembic revisions
├── scripts/                   # create_user, check_no_local_inference, validate_prompts
├── tests/                     # unit · integration · e2e (Streamlit AppTest)
├── docs/                      # PRD, TRD, architecture, runbook, ADRs, HANDOVER.pdf
├── assets/                    # logo files
└── .streamlit/config.toml     # theme
```

> **`templates/internal/` exists for a reason.** `approval_request.html` was originally placed in
> `templates/email/`, where it sorted before `classic`, silently became the *default* newsletter
> template, and broke the Preview page. Non-newsletter templates stay out of that directory.

---

## Prerequisites

| Requirement | Detail |
|---|---|
| **Python** | 3.11 – 3.14. Developed and verified on **3.14.3** (Windows 11, AMD64). |
| **OS** | Windows 10/11 primary. The code is `pathlib`-based and CRLF-safe; Linux works and is covered by CI, but the `.bat` launchers are Windows-only. |
| **RAM** | Any. All inference is remote — this runs on an 8 GB laptop with no GPU. |
| **Groq API key** | Free, no credit card — [console.groq.com/keys](https://console.groq.com/keys) |
| **Mail provider** | Optional to start. Gmail App Password or a Brevo key when you want to send for real. |
| **Network** | Outbound HTTPS to `api.groq.com`, the blog being watched, and your mail provider. |
| **Not required** | Node.js, Docker, Redis, Celery, Kubernetes, a GPU, or any paid service. |

---

## Installation

```bash
# 1 — clone
git clone https://github.com/violinadutta/Vays-AI-Newsletter-Agent.git
cd Vays-AI-Newsletter-Agent

# 2 — virtual environment
py -3.12 -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux

# 3 — dependencies (the lockfile pins all 132 packages for a reproducible install)
pip install -r requirements.lock.txt
# for development work as well:
pip install -r requirements-dev.txt

# 4 — configuration
copy .env.example .env           # Windows   (cp on macOS/Linux)
python -c "import secrets; print(secrets.token_urlsafe(48))"
#   paste the output into APP_SECRET_KEY= in .env, then add GROQ_API_KEY=

# 5 — database (12 tables, 5 migrations)
alembic upgrade head

# 6 — create the first login. There is no default account and no default password.
python -m scripts.create_user
```

`requirements.txt` holds loose version ranges and is what you edit when adding a dependency.
`requirements.lock.txt` is the pinned set — use it for installs.

---

## Environment Variables

All configuration lives in `.env`, which is **git-ignored**. Copy `.env.example` — it documents
every variable inline and shows exactly where to obtain each credential.

**Required to start:**

| Variable | Where to get it |
|---|---|
| `APP_SECRET_KEY` | Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Signs session tokens; changing it logs everyone out. |
| `GROQ_API_KEY` | Free at [console.groq.com/keys](https://console.groq.com/keys). Not needed if `LLM_PROVIDER=mock`. |

**Required to send real email:**

| Variable | Notes |
|---|---|
| `EMAIL_PROVIDER` | `console` (default, writes `.eml` and sends nothing) · `smtp` · `brevo` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | Gmail: `smtp.gmail.com`, `587`, your full address, and a **16-character App Password** — not the account password. Create one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) with 2-Step Verification enabled. |
| `BREVO_API_KEY` | [app.brevo.com/settings/keys/api](https://app.brevo.com/settings/keys/api) — 300 emails/day free |
| `BRAND_ADDRESS`, `UNSUBSCRIBE_BASE_URL` | **Legally required.** Rendering refuses without both. |

**Key optional groups** (all documented in `.env.example`):

- **LLM** — `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`
- **Agent** — `AGENT_ENABLED`, `AGENT_BLOG_URL`, `AGENT_SEND_*`, `AGENT_MAX_IN_FLIGHT`,
  `AGENT_APPROVAL_EMAIL`, `AGENT_APP_BASE_URL`
- **Brand** — `BRAND_NAME`, `BRAND_PRIMARY_COLOR`, `BRAND_LOGO_PATH`, `BRAND_WEBSITE`
- **Scraper** — `SCRAPER_MAX_INPUT_TOKENS`, `SCRAPER_RESPECT_ROBOTS`, timeouts

> ⚠️ **`LLM_MAX_TOKENS` must be ≥ 2048.** `gpt-oss` emits internal reasoning tokens that count
> against the budget but never appear in the response. A smaller budget truncates mid-JSON, and
> Groq reports that as a `400 json_validate_failed` rather than `finish_reason: length` — so the
> failure looks like a schema problem when it is a budget problem.

### Two configuration layers

`.env` is the bottom layer and the source of truth for a fresh install. On top of it, **43
settings are editable in the UI** (Settings page, admin only) and take effect immediately without
a restart; "Revert" restores the file value.

**Secrets cannot reach that layer, and this is enforced by a mechanism rather than a rule.**
`SettingsService._validate_registry()` runs at import time and raises if any registered field is
a `SecretStr` — so adding an API key to the editable registry fails the build, not code review.

---

## Running the Project

### The dashboard

```bash
run.bat                                   # Windows
# streamlit run app.py                    # any platform
```

Opens on **http://localhost:8501**. `run.bat` preflight-checks the four things that actually go
wrong on a fresh machine — missing venv, missing `.env`, unapplied migrations, and **no user
accounts** — and names the fix for each. A login screen with no accounts is a dead end.

### The agent worker (optional, separate process)

```bash
run_agent.bat                             # runs continuously on its schedule
python agent_worker.py --once             # one discovery + dispatch cycle, then exit
```

Requires `AGENT_ENABLED=true`. **The dashboard does not run the agent** — this is a separate
process with its own clock, so that a browser tab closing cannot stop the automation.

To survive a reboot, register it as a Windows scheduled task:

```bash
install_agent_task.bat
```

### Public approval links (optional)

```bash
run_tunnel.bat                            # ngrok, for approving from a phone
```

Or skip ngrok entirely in production by setting one variable:

```env
AGENT_APP_BASE_URL=https://newsletter.example.com
```

Every generated link routes through `resolve_base_url()`, so that single variable is the whole
migration from localhost to a real domain.

### Fully offline (no API key, no network)

```env
LLM_PROVIDER=mock
EMAIL_PROVIDER=console
```

The complete pipeline runs against deterministic fixtures and writes `.eml` files to
`data/outbox/`. This is the recommended mode for development and demos.

---

## Usage

1. **Sign in** with the account created by `scripts/create_user.py`.
2. **Add recipients** — *Recipients* page. Upload a CSV (it **appends**, it does not replace),
   paste a list, or remove individuals. Removal deactivates rather than deletes, so past campaign
   history still resolves.
3. **Generate** — *Generate Newsletter*. Paste one or more blog URLs, choose tone, length and
   audience, then generate. The pipeline extracts, cleans, summarises each article, and composes
   the newsletter.
4. **Review and edit** — *Campaign Preview*. Edit any field, regenerate a single field with an
   instruction ("make it more urgent"), request alternative subject lines, and preview the
   rendered HTML. Source URLs are shown beside each block so every claim is traceable.
5. **Send** — confirm, and the four guards run before anything leaves.
6. **Track** — *Campaign History* shows per-recipient delivery status and supports retrying only
   the failures.

### Letting the agent do it

Set `AGENT_ENABLED=true`, configure `AGENT_APPROVAL_EMAIL`, and start `run_agent.bat`. Then:

1. The agent finds a new blog post and generates a newsletter.
2. Management receives an approval email with a secure link.
3. On approval, the campaign becomes eligible — **it does not send yet**.
4. On the 3rd Wednesday at 11:00 (IST by default), the dispatch job sends it to the full list.

**`AGENT_MAX_IN_FLIGHT=1` is what limits this to one newsletter per month**, not the discovery
interval. See [Troubleshooting](#troubleshooting).

---

## API Documentation

This project **does not expose an HTTP API**. It is a single Streamlit application with an
internal Python service layer, which was a deliberate decision (D-1) to avoid a second deployable.

The service layer is the de-facto internal API and has zero Streamlit imports, so it can be
called from a script, a test, or a future FastAPI shell without modification:

```python
from services.generation_service import GenerationService
from services.delivery_service import DeliveryService
from services.agent_service import AgentService

draft = GenerationService().generate(request)  # runs the full two-stage pipeline
AgentService().run()  # one discovery cycle
```

The only externally reachable URL is the **approval review page**, served by the Streamlit app:

| Route | Method | Purpose |
|---|---|---|
| `/approvals?token=<token>` | GET | Renders the campaign for review. The token is random, sha256-hashed at rest, single-use, expiring (72 h default), and scoped to one campaign. All four failure modes are reported identically. |

Adding a REST API later is additive rather than a rewrite — the layer rule that makes that true
is enforced by `import-linter` today.

---

## AI/ML Components

### Model

| | |
|---|---|
| **Model** | `openai/gpt-oss-120b` — **Apache 2.0 open weights**, despite the name |
| **Fallback** | `openai/gpt-oss-20b` — smaller, faster, lighter on rate limits |
| **Host** | Groq API, OpenAI-compatible, `https://api.groq.com` |
| **Structured output** | `strict: true` — decoding is constrained to the JSON schema |
| **Hardware required** | **None.** No GPU, no local weights, no inference runtime. |

> `openai/gpt-oss-120b` is OpenAI's *open-weight* release, not a proprietary hosted model. Groq
> serves only open-source models. This satisfies the open-source requirement on the model licence
> rather than on who runs the GPU.

### There is no training, no dataset, and no model file

Inference is remote and stateless. Nothing is fine-tuned, no dataset is required, and no model
artefact is stored in this repository. That is the point of the zero-local-inference rule — and
`scripts/check_no_local_inference.py` fails the build if anyone reintroduces an ML runtime.

### The generation pipeline

**Stage 1 — `article_summary`**, once per article: key points, technical facts, category, and a
relevance score. Extractive before generative, which reduces hallucination.

**Stage 2 — `newsletter_compose`**, once for all articles: subject, preview text, body blocks
and CTA, grounded in the stage-1 output.

Plus `field_regenerate` (regenerate one field with an instruction) and `subject_variants`
(alternative subject lines from different angles).

### Prompts

Prompts are **versioned YAML in `prompts/`, never edited in place** — every campaign records the
prompt version and model that produced it, so any output is reproducible. To change a prompt,
copy `v1.1.0.yaml` to `v1.2.0.yaml` and edit that.

Shared fragments in `prompts/_shared/` (`human_voice.md`, `length_rules.md`,
`system_persona.md`, `untrusted_input_rules.md`) are included by every prompt.

Two lessons that are baked into the current prompts:

- **State limits as hard limits.** "approximately 60 characters" overshot every time;
  `MAXIMUM 60 characters (this is a hard limit, not a target)` landed in range on the first try.
- **A prompt instruction is not a guarantee.** v1.0.0 asked for "a short bold heading"; the field
  is plain text, so the model emitted `**Heading**` and the asterisks reached customers. Fixed at
  both ends — v1.1.0 bans markdown explicitly, *and* the renderer converts a whitelist of
  markdown to real HTML as a safety net. Each paragraph is **escaped first**, then our own tags
  are inserted, so the safety net cannot become an injection hole.

### Reliability

- **Circuit breaker** — opens after repeated failures so a dead endpoint fails fast rather than
  hanging every request.
- **Typed errors** — `LLMRateLimitedError` is distinct from `LLMUnavailableError`, so "busy" and
  "broken" are handled differently. `Retry-After` is honoured.
- **Measured cost**: one article ≈ 3,450 tokens, ~5.3 s end to end. The Groq free tier is
  ~8–12k tokens **per minute**, which is the binding constraint at higher volume.

### Swapping the model or host

```env
LLM_MODEL=openai/gpt-oss-20b        # smaller and cheaper
LLM_PROVIDER=hosted                 # any OpenAI-compatible endpoint
LLM_BASE_URL=https://your-endpoint
```

Only the `gpt-oss` family supports `strict` schema enforcement. Other open models still work —
`supports_guided_json` becomes `False` and a repair-retry loop takes over — but JSON validity
becomes probabilistic rather than guaranteed.

For a non-OpenAI-compatible host, implement `LLMProvider` in `modules/ai/` and register it in
`modules/ai/factory.py`. **This has been done for real:** the original Colab-based host was
replaced by Groq at a cost of one class and one config default, with nothing outside
`modules/ai/` changed.

---

## Configuration

| What | Where | Restart needed? |
|---|---|---|
| Secrets (API keys, passwords, `APP_SECRET_KEY`) | `.env` **only** | Yes |
| 43 operational settings | Settings page (admin) or `.env` | **No** |
| Prompts | `prompts/**/*.yaml` (new version file) | No |
| Email templates | `templates/email/*.html` | No |
| Theme | `.streamlit/config.toml` + `ui/styles.py` | Yes |
| Database schema | `migrations/versions/` via Alembic | Yes |

Settings changed in the UI are validated with the same validators as startup — a rejected value
leaves the previous one in place, so the app is never left holding a config it could not have
booted with.

---

## Testing

**1,168 tests · 93% coverage · six gates green** on Windows and Ubuntu, Python 3.11 and 3.12.

```bash
# the full gate suite, in the order CI runs it
python scripts/check_no_local_inference.py     # runs FIRST — if this fails, nothing else matters
ruff format --check . && ruff check .
mypy core config modules services
lint-imports                                    # the layer rule — 4 contracts
pytest

# faster loops
pytest tests/unit -q            # pure logic, no I/O
pytest tests/integration -q     # real SQLite, stubbed network
pytest tests/e2e -q             # Streamlit AppTest — renders every page
pytest --cov --cov-report=html  # → htmlcov/index.html
```

No test requires a GPU, a network connection, or an API key — the mock provider and fixtures
cover the whole pipeline.

**A coverage percentage hides a zero.** `generation_service.py` — the orchestrator every
generation flows through — once had **0% coverage** while the suite reported 90% overall. It is
now 100%. Before trusting a module, check it has a caller and a test, not just a green number.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| **The agent isn't doing anything** | The worker process was never started. `run.bat` starts the **dashboard only**. | Run `run_agent.bat`. Check the dashboard's "worker last seen" heartbeat, and that `AGENT_ENABLED=true`. |
| Campaign stuck in `AWAITING_APPROVAL` | Working as designed — nobody has approved. | Approve via the email link or the Approvals page. |
| Approved but not sending | It is not the 3rd Wednesday yet. | Check "next send" on the dashboard. This is normal. |
| **Several newsletters sent at once** | `AGENT_MAX_IN_FLIGHT` above 1. A per-run cap alone is not enough: with frequent discovery, a backlog queues campaigns that all become due on the same day. | Set `AGENT_MAX_IN_FLIGHT=1`. |
| Approval link 404s or is stale | ngrok restarted and rotated its URL. | Set `AGENT_APP_BASE_URL` to a stable domain. |
| Approval link says invalid | Token used, expired, or for another campaign — all reported identically by design. | Approve from the Approvals page. |
| `json_validate_failed` from Groq | `LLM_MAX_TOKENS` below 2048. | Raise it to at least 2048. |
| Generation slow, or HTTP 429 | Groq free-tier tokens-per-minute ceiling. | `Retry-After` is honoured automatically. Reduce `SCRAPER_MAX_INPUT_TOKENS` or switch to `gpt-oss-20b`. |
| Nothing arrives in an inbox | `EMAIL_PROVIDER=console` — it sends nothing. | The emails are in `data/outbox/` as `.eml`. Set `smtp` or `brevo` to send. |
| Send refuses outright | Guard 4 — missing unsubscribe URL or postal address. | Set `BRAND_ADDRESS` and `UNSUBSCRIBE_BASE_URL`. |
| Logo missing in email | `BRAND_LOGO_PATH` points at a missing file. | Fix the path. It degrades to a text fallback by design and never blocks a send. |
| Sidebar says AI service offline | Groq unreachable, bad key, or the circuit breaker is open. | Settings → Test Connection shows the real error. |
| Refresh logs you out | Cookie blocked, or `APP_SECRET_KEY` changed. | Changing the key invalidates every session — expected. |
| App won't start | Missing `.env`, unapplied migrations, or no accounts. | `run.bat` names which one. |

---

## Deployment

**Current deployment is local Windows** — `run.bat` for the dashboard, `run_agent.bat` for the
worker, optionally registered as a scheduled task so it survives a reboot.

For a server deployment:

1. Set `APP_ENV=prod` and generate a fresh `APP_SECRET_KEY`.
2. Set `AGENT_APP_BASE_URL` to the real domain so approval links resolve.
3. Put the Streamlit app behind a reverse proxy with TLS.
4. Run the agent worker as a service (`systemd`, or a Windows scheduled task).
5. Consider Postgres — change `DATABASE_URL`, `pip install psycopg[binary]`, then
   `alembic upgrade head`. No raw SQL and no SQLite-only types are used, so the ORM layer carries
   over; the pragmas in `modules/repository/database.py` are SQLite-specific and worth reading
   first.
6. Back up `data/newsletter.db` — it is a single file, so copying it is the whole procedure.

Docker is not used. There is no Dockerfile in this repository, and adding one is optional rather
than required.

---

## Security

- **Secrets belong in environment variables and are never committed.** `.env` is git-ignored;
  only `.env.example`, containing placeholders, is tracked. Any file matching `.env.*` is ignored
  unless explicitly allowed.
- **Secrets are structurally excluded from the editable settings registry** by an import-time
  check, so a future edit cannot accidentally expose one through the UI.
- **Logs are redacted** — API keys stripped, email addresses partially masked.
- **Passwords** are bcrypt-hashed with per-password salts. **There is no default account and no
  default password** anywhere in the codebase.
- **Sessions** use HMAC-signed tokens with a 12-hour expiry. A restored session is only honoured
  if the signature verifies, the token has not expired, **and the account still exists and is
  active** — the last being the check a token cannot make about itself. Roles are read from the
  database, so a stale token cannot preserve a revoked privilege.
- **SSRF guard** on every URL — loopback, link-local and RFC-1918 ranges are refused, so a pasted
  metadata-service URL cannot make the server fetch its own credentials.
- **Scraped content is treated as untrusted input**, with explicit prompt rules to that effect.
- **Approval tokens** are random, hashed at rest, single-use, expiring and campaign-scoped.

> **Known limitation.** The session cookie cannot be `HttpOnly`, because Streamlit can only set
> cookies from the browser side. This is acceptable for a LAN-internal tool. **If this
> application is ever exposed publicly, revisit it** — the mitigation is a server-side session
> store or terminating authentication at a reverse proxy.

If you find a security issue, please report it privately rather than opening a public issue.

---

## Documentation

The complete technical handover is **[`docs/HANDOVER.pdf`](docs/HANDOVER.pdf)** — 41 pages
covering architecture, a full module and function reference, eleven diagrams, the swap guide,
the operations runbook and the decision register.

| Document | Contents |
|---|---|
| `docs/HANDOVER.pdf` | **The complete handover pack — start here** |
| `docs/SETUP_GUIDE.md` | Fresh-machine install in detail |
| `docs/RUNBOOK.md` | Operations |
| `docs/ARCHITECTURE.md` | Module contracts and interfaces |
| `docs/SWAP_THE_LLM.md` | Changing the model or host |
| `docs/PROMPT_GUIDE.md` | Writing and versioning prompts |
| `docs/PUBLIC_ACCESS.md` | ngrok and public hosting |
| `docs/KNOWN_ISSUES.md` | Live issue list |
| `docs/09_FINAL_DECISIONS.md` | Decision register, security control matrix, deliverability contract |
| `docs/adr/` | ADRs for the decisions that were *reversed* |
| `CLAUDE.md` | The running engineering log |

---

## Project Status

| Milestone | Status |
|---|---|
| M1 Foundation · M2 Scraper · M3 LLM · M4 Prompts | ✅ Complete, verified live |
| M5 Template engine | 🟡 Code complete — **awaiting visual verification in real Outlook/Gmail** |
| M6 Email + campaigns · M7 UI · M8 Auth/settings · M9 Handover | ✅ Complete |
| M10 Autonomous agent | ✅ Complete |

**Known gaps:** the M5 email-client check needs a human with Outlook and Gmail; the recipient
list currently holds test addresses and must be replaced before go-live; the agent ships disabled
(`AGENT_ENABLED=false`) by design.

---

## License

Released under the **MIT License** — see [LICENSE](LICENSE).

Built as an internal deliverable for **Vays Infotech**. Every runtime dependency is
permissively licensed (MIT / BSD / Apache-2.0); `html2text` was dropped specifically to keep
GPL-3.0 out of the distribution.

Generated newsletters are original summaries that link back to the source article, with an
attribution block in every template.
