# CLAUDE.md — Project Memory

> This file is the persistent memory for the **AI Newsletter Generation & Distribution Platform**
> built for Vays Infotech. Read this file first in every session. Update it whenever a
> decision is made, reversed, or a milestone is completed.

---

## 1. Project Identity

| Field | Value |
|---|---|
| Project name | AI Newsletter Generation & Distribution Platform (working name: `vays-newsletter-ai`) |
| Client / context | Vays Infotech — internship deliverable |
| Owner | Sidhant (sidhant@deephorizon.io) |
| Repo root | `c:\Users\MY PC\OneDrive\Desktop\vays-ai-new` |
| Started | 2026-08-05 |
| Current phase | **M1–M9 complete. Feature-complete and handover-ready.** |
| Code written | ~11k lines across `core/ config/ modules/ services/ ui/`; **835 tests, 92% coverage** |
| Runs with | `run.bat` → login → 6 pages, live Groq generation, real Gmail sending |
| Not yet done | Visual email-client verification (M5, needs a human with Outlook/Gmail) · handover walkthrough with Vays |

---

## 2. One-Paragraph Problem Statement

Marketing teams at Vays Infotech manually read OEM partner blogs, summarize them, rewrite
them into customer-facing newsletters, format them as HTML, and send them out. The process is
slow, inconsistent in tone, hard to scale across many OEM partners, and delays campaigns.
This platform automates the pipeline: **paste blog URLs → extract → clean → LLM generates
structured newsletter content → human edits → render HTML → send bulk campaign → log and
archive.**

---

## 3. Hard Constraints (non-negotiable)

1. **The LLM must be open source / open weights.** No proprietary model APIs for generation.
2. **ZERO LOCAL INFERENCE.** No model, no weights, no inference runtime ever runs on the
   developer machine (Windows 11, 8 GB RAM, no GPU). Enforced by
   `scripts/check_no_local_inference.py` in CI — torch, transformers, llama-cpp, ollama, vllm
   and friends are a **build failure**, not a code-review comment.
3. **Groq is the LLM host** (D-21). Open-weight models over an ordinary API — no notebook, no tunnel, no session expiry.
4. **Deliverable must be handover-ready**: another developer must be able to clone, configure,
   and run it on a different Windows PC from the README alone.
5. **No code until the PRD and TRD are explicitly approved by the owner.**
6. **Code is delivered module by module**, each with explanation, error handling, logging,
   type hints, docs, and unit-test suggestions. Wait for approval between modules.

---

## 4. Approved Technology Stack

> Status: **LOCKED** as of 2026-08-05. Full decision register with rationale in
> `docs/09_FINAL_DECISIONS.md` (D-1 … D-20). Changing a locked decision requires an ADR.

| Layer | Choice | Why (short) |
|---|---|---|
| Frontend | **Streamlit** (multipage, `st.navigation`) | Required by brief; fastest path to a polished internal tool in pure Python |
| Backend | **Pure Python service layer** inside the same process (no separate API server in v1) | Avoids a second deployable; service layer is framework-agnostic so a FastAPI shell can be added later without rewrites |
| LLM (model) | **`openai/gpt-oss-120b`** on Groq (Apache 2.0 open-weight); `gpt-oss-20b` fallback | Groq serves **only** open-source models. These two support `strict` schema enforcement, which is what keeps D-3 a guarantee |
| LLM (serving) | **Groq API** (`api.groq.com`), OpenAI-compatible, `strict: true` structured outputs | Guarantees schema-valid JSON at the decoding layer; no infrastructure to run |
| LLM (transport) | HTTPS → `LLMProvider` abstraction: **Groq \| Hosted \| Mock** (no local adapter) | The abstraction makes the host swappable in one config line — already proven once by the Colab→Groq move. Every adapter is remote or fixture-based |
| Prompt mgmt | **Versioned YAML files in `prompts/`** + Jinja2 rendering + registry loader | Git-native, reviewable in PRs, zero infra. Langfuse considered and deferred |
| Database | **SQLite + SQLAlchemy 2.x + Alembic** | Zero-install on Windows; SQLAlchemy keeps the Postgres migration path open |
| Auth | **Own module** — `bcrypt` + ~80 lines in `core/auth.py` | No unvetted third-party package on the security boundary; fully auditable at handover; SSO is the documented upgrade path |
| Extraction | **Trafilatura** (primary) → **Newspaper4k** (fallback) → raw BS4 (last resort) | Highest F1 in published benchmarks; actively maintained |
| Templating | **Hand-authored table HTML + Jinja2** (D-23; MJML dropped) | Node is not installed, so `.mjml` sources could not be compiled — an uncompilable source of truth is worse than none. Removes the last Node dependency at any stage |
| Email | **Brevo API** (primary adapter), **SMTP** adapter for fallback/testing | 300 emails/day permanent free tier; combines transactional + marketing |
| Logging | **structlog** → JSON lines + rotating file + SQLite audit table | Machine-readable logs for the Logs page; audit trail separate from debug logs |
| Config | **pydantic-settings** + `.env` + `settings.yaml` | Fail-fast validation at startup beats runtime `KeyError` |
| Testing | **pytest** + `pytest-cov` + `responses`/`respx` + fake LLM provider | Test the pipeline without a GPU or network |
| Deployment | **Local Windows** (v1) → Docker Compose (v2) documented | Matches the actual handover requirement |
| Monitoring | Health-check panel + campaign metrics table in-app | No external observability stack for an internal tool |

---

## 5. Architecture in One Picture

```
Streamlit UI (pages/)
        │  calls only
        ▼
Service layer  (services/)          ← orchestration, transactions, no UI imports
        │
        ├── scraper/     Trafilatura → Newspaper4k → BS4
        ├── cleaner/     normalize, dedupe, truncate to token budget
        ├── ai/          LLMProvider (Groq | Hosted | Mock) + PromptRegistry
        │                ↑ all remote or fixture — nothing loads a model here
        ├── newsletter/  merge LLM JSON + user edits → NewsletterDraft
        ├── template/    MJML-compiled Jinja2 → inlined HTML + plain-text
        ├── email/       EmailProvider (Brevo | SMTP) + batching + retry
        ├── campaign/    lifecycle state machine + history
        └── repository/  SQLAlchemy models + Alembic migrations
```

Everything crossing a boundary is a **Pydantic model**, never a raw dict.

---

## 6. Key Decisions & Their Rationale

- **D-1 — No separate FastAPI backend in v1.** A single Streamlit process with a clean service
  layer is simpler to hand over and deploy on a Windows PC. The service layer has zero Streamlit
  imports, so wrapping it in FastAPI later is additive, not a rewrite.
- **D-2 — "Open source LLM" is a constraint on the *model*, not the *host*.** This is what makes
  Groq legitimate: it serves **only** open-source models, so the constraint is satisfied more
  cleanly than by self-hosting.
- **D-3 — Guided JSON decoding over prompt-and-pray.** Groq's `strict: true` constrains generation
  to the schema, so `json.loads` cannot fail. A repair-retry path exists for backends that do not
  support it (`supports_guided_json` is a provider property).
- **D-4 — Two-stage generation, not one mega-prompt.** Stage 1 = per-article extraction/summary;
  Stage 2 = cross-article newsletter composition. Smaller prompts = better quality on a mid-size
  model and cheaper retries.
- **D-5 — Human-in-the-loop is mandatory, not optional.** Nothing is sent that a human has not
  approved. This is the core mitigation for hallucination risk.
- **D-6 — Prompts live in Git as YAML with semantic versions.** Every campaign row records the
  prompt version and model that produced it, so any output is reproducible.
- **D-12 — No local inference adapter.** The `LocalProvider` (Ollama) from the first draft is
  deleted. Offline development uses `MockProvider` fixtures, which are deterministic and therefore
  better for that job anyway.
- **D-13 — The zero-local-inference rule is machine-enforced**, not documented. CI fails on any
  ML-runtime dependency or import.
- **D-14 — Token counting is a pure-Python heuristic.** This is what keeps `torch` (~2 GB) out of
  `requirements.txt`.
- **D-15 — Own auth module instead of `streamlit-authenticator`.** No unvetted package on the
  security boundary.
- **D-16 — `html2text` (GPL-3.0) dropped.** The plain-text email part is composed directly from
  the content fields. Every remaining dependency is permissive-licensed.
- **D-17 (SUPERSEDED by D-21)** — was "ship defaulting to Colab".
- **D-24 (2026-08-08) — Configuration is two layers; `.env` stays the bottom one.**
  Runtime-editable settings live in the `settings` table and override `.env`; "Revert"
  restores the file value. `.env` remains the source of truth for a fresh install, so a
  handover is still configured by a file in Git. **Secrets are excluded by a mechanism**,
  not a rule: the editable registry is checked at import time and refuses any `SecretStr`
  field, which makes D-19 unbreakable by a future edit.
- **D-21 (2026-08-07) — Groq replaces Colab entirely.** Colab was attempted twice on real
  hardware and never generated a token: the CUDA-13 vs CUDA-12 wheel mismatch, then an
  install/restart loop. Beyond those fixable faults, the shape was wrong — 3-hour sessions, a
  rotating tunnel URL, no guaranteed GPU, and a standing ToS conflict. Groq serves only
  open-source models over an ordinary API. **The switch cost one small class and a config
  default**, which is the clearest possible validation of the `LLMProvider` seam.
  All Colab code, notebooks and docs are deleted; `LLM_PROVIDER=colab` now fails at startup.

> Full register, including the security control matrix and the deliverability contract:
> `docs/09_FINAL_DECISIONS.md`.

---

## 7. Live Risk Register (top items)

| ID | Risk | Mitigation |
|---|---|---|
| ~~C-1~~ | ~~Colab ToS grey area~~ | **CLOSED by D-21.** Groq is ordinary API use |
| ~~C-2~~ | ~~Colab sessions die, tunnel URL rotates~~ | **CLOSED by D-21.** No sessions, no tunnel |
| **C-8** | **Groq free-tier token limit (~8–12k TPM)** — the binding constraint. Three 6k-token articles would 429 before composition started | Input budget cut 6000 → 3000/article; `Retry-After` honoured; `LLMRateLimitedError` distinguishes "busy" from "broken". Paid tier is ~10× if it bites |
| C-9 | `openai/gpt-oss-120b` **looks** proprietary and is not (Apache 2.0 open-weight) | Documented in `04_LLM_HOSTING.md` §4 and `.env.example`. Switching to a Qwen/Llama model is one line, at the cost of strict schema enforcement |
| C-3 | LLM hallucinates facts / fabricates OEM claims | Mandatory human review; source-URL provenance shown next to each block; grounding instructions in prompt; extractive summary stage before generative stage |
| C-4 | Blog sites block scraping or are JS-rendered | 3-tier extractor cascade, polite UA + robots.txt check, manual paste-text fallback |
| C-5 | Emails land in spam / no unsubscribe | List-Unsubscribe header, plain-text alternative, verified sender domain (SPF/DKIM/DMARC), suppression list |
| C-6 | Copyright — republishing OEM blog content | Generate original summaries, always link back to source, attribution block in template |
| C-7 | 8 GB dev laptop RAM | All inference remote; no local model loading; keep Streamlit process lean |

---

## 8. Working Agreements (how to behave in this repo)

- Do **not** generate application code until §9 records PRD + TRD approval.
- Generate **one module at a time**; stop and wait after each.
- Every module: explanation → folder placement → code → error handling → logging → docstrings →
  suggested unit tests.
- Prefer maintainability and clarity over cleverness. SOLID where practical, not dogmatically.
- Never invent an API surface; if a library detail is uncertain, verify it before writing code.
- Windows-first: forward-slash paths via `pathlib`, no POSIX-only shell assumptions, CRLF-safe.
- Secrets never enter Git. `.env` is git-ignored; `.env.example` is committed.

---

## 9. Approval Log

| Date | Item | Status | Notes |
|---|---|---|---|
| 2026-08-05 | PRD (`docs/01_PRD.md`) | ✅ **Approved** | — |
| 2026-08-05 | TRD (`docs/02_TRD.md`) | ✅ **Approved** | — |
| 2026-08-05 | Stack selection (`docs/03_RESEARCH_AND_DECISIONS.md`) | ✅ **Approved** | — |
| 2026-08-05 | Decision register (`docs/09_FINAL_DECISIONS.md`) | ✅ **Approved** | Superseded on 2026-08-07 by D-21 (Groq) |
| 2026-08-06 | **M1.1 — Foundation: config, logging, exceptions, dep guard** | ✅ **Complete & verified** | 144 tests, 99% cov; ruff/mypy/import-linter/guard all green on Python 3.14.3 |

| 2026-08-06 | **M1.2 — Core domain: enums, models, validators, schemas** | ✅ **Complete & verified** | 297 tests, 99% cov; all 6 gates green. SSRF guard implemented |
| 2026-08-06 | **M1.3 — Repository layer, ORM, Alembic** | ✅ **Complete & verified** | 337 tests, 94% cov; 9 tables migrated; FK/WAL pragmas verified live; double-send guard proven |
| 2026-08-06 | **M1.4 — App shell, auth, nav guard** | ✅ **Complete & verified** | 388 tests, 95% cov; app boots and serves; 7 AppTest e2e tests pass |
| 2026-08-06 | **MILESTONE M1 COMPLETE** | ✅ | Runnable app: `streamlit run app.py` → login → 6-page shell |
| 2026-08-06 | **M2 — Scraper + Cleaner** | ✅ **Complete & verified** | 504 tests, 94% cov. **Live measurement on 8 discovered OEM articles: 7/8 (88%) extracted, all tier-1.** The one failure was an author-archive page, correctly refused with the manual-paste message |

| 2026-08-06 | **M3 (code) — LLM providers, circuit breaker, mock provider** | ✅ **Complete & verified** | 560 tests, 92% cov. Full two-stage pipeline runs on `LLM_PROVIDER=mock` with no GPU/network |
| 2026-08-07 | **D-21 — Colab dropped; Groq adopted** | ✅ **Complete** | Colab failed twice on real hardware (CUDA-13 wheel mismatch, then an install/restart loop). All Colab code/docs deleted. `GroqProvider` + rate-limit handling added. **Switch cost: one class + a config default** |

| 2026-08-07 | **M4 — Prompt registry + AI engine** | ✅ **Complete & verified live** | 634 tests, 89% cov. All 4 prompts run end to end against Groq on a real 1,105-word AWS article |

| 2026-08-07 | **M5 — Template engine** | 🟡 **Code complete; needs a client check** | 676 tests, 89% cov. 3 templates render, CSS inlines, compliance enforced. **MJML dropped (D-23).** Previews in `data/exports/preview_*.html` — Outlook/Gmail verification is the owner's step |

| 2026-08-07 | **M6 — Email engine + campaign manager** | ✅ **Complete & verified** | 732 tests, 88% cov. 3 providers, batching/retry, suppression list, double-send guard proven, retry-failed skips delivered |

| 2026-08-07 | **M7 — Streamlit UI (6 pages)** | ✅ **Complete & verified** | 758 tests, 89% cov. App boots headless (HTTP 200) and all 6 pages render through `AppTest`. DB log sink added so the Logs page has data |

| 2026-08-08 | **M8 — Auth, settings & observability** | ✅ **Complete & verified live** | 795 tests, 90% cov. Done-criterion run end to end: a second account logs in, repoints the LLM endpoint, tests it, survives a restart, and reverts — no file, no terminal |

### M9 — the handover pack (2026-08-10)

835 tests, 92% coverage, six gates green. Docs written: `README.md`,
`SETUP_GUIDE`, `RUNBOOK`, `ARCHITECTURE`, `SWAP_THE_LLM`, `PROMPT_GUIDE`,
`KNOWN_ISSUES`, and four ADRs covering the decisions that were *reversed*
(Colab→Groq, MJML dropped, two-layer config, CID logo) — those are the ones a
future developer would otherwise re-litigate.

**`generation_service.py` had 0% coverage** — the orchestrator every generation
flows through, entirely untested, while the suite reported 90% overall. Now
**100%**, via a stub engine rather than `MockProvider` so each failure mode can
be provoked directly. **Fourth instance of the dead-code/untested-core pattern**
(`app_logs`, `settings`, `set_password`, now this). A coverage percentage hides
a zero.

`run.bat` checks the four things that actually go wrong on a fresh machine —
missing venv, missing `.env`, unapplied migrations, **no user accounts** — and
names the fix for each. A login screen with no accounts is a dead end, and
that check is the difference between a 2-minute start and an hour lost.

CI runs all six gates on Windows + Ubuntu × Python 3.11/3.12. Three details
worth keeping: the zero-local-inference guard runs **first** (if someone adds
torch, nothing else matters); `pip-audit` is `continue-on-error` and therefore
advisory — a green tick does not mean clean; and a dedicated job greps **full
git history** for `gsk_`/`xkeysib-` keys, because a secret deleted in a later
commit is still leaked. Verified: history is clean and `.env` was never tracked.

Every internal doc link is checked by a script — all resolve. The test suite was
also run under CI conditions (no `.env`, env vars only) to prove the workflow
config is right rather than plausible.

### Post-M8 fixes from the first real Gmail send (2026-08-08)

The first live send exposed three things no test could have caught, because all
three are only visible in a received email.

**1. The logo never rendered, and could not have.** `resolve_brand` put
`logo_path` (`assets/logo.png`, a *filesystem* path) straight into `<img src>`.
No mail client can resolve that. The `assets/` directory did not exist either,
and `minimal.html` had no logo block at all.
Fixed with **CID embedding** — the only approach that works without web hosting,
since Gmail and Outlook both strip `data:` URIs. `resolve_logo_url()` now returns
`cid:vays-logo` for a local file and passes an `http(s)` URL through unchanged,
so the hosted path stays open for Vays. `build_mime_body()` in `modules/email/base.py`
attaches the bytes as `multipart/related` under the HTML alternative — attaching
at the top level instead makes it an ordinary attachment and the `cid:` resolves
to nothing. Shared by the console and SMTP providers so the `.eml` preview and
the real send cannot drift.
A missing logo file degrades to the text fallback; it must never block a send.
Also: `modern.html` was putting the logo on the `#0B5FFF` band, which would have
made a dark logo unreadable. Logos now get a white plate with the brand colour
as a rule beneath.

**2. Asterisks in the delivered copy — my prompt caused it.**
`newsletter_compose` v1.0.0 line 53 said *"Give each story a short bold heading"*.
The field is plain text, so the model reached for `**Heading**` and the markers
reached the customer. Fixed at both ends:
`v1.1.0` bans markdown explicitly and asks for lead-in sentences instead, and
`_split_paragraphs` now converts a whitelist of markdown to real HTML as a safety
net — a prompt instruction is not a guarantee, and this failure is visible to the
recipient. **Order is the security property**: each paragraph is escaped *first*,
then our own tags are inserted into the escaped string, so this cannot become an
injection hole. Single `*` is deliberately unsupported (ambiguous against `5 * 3`).

**3. The copy read as machine-written.** Added `_shared/human_voice.md` — a
concrete list of the tells (uniform sentence length, signposting, rule-of-three,
decorative hedging, "delve/leverage/seamless") rather than a vague "be natural".
Temperature raised 0.7 → 0.85; the banned-construction list is what keeps the
extra variance from wandering.

**Measured A/B on the same input, live against Groq:**

| | v1.0.0 | v1.1.0 |
|---|---|---|
| Asterisks in body | **10** | **0** |
| Opening | "Dell's latest PowerEdge R7xx servers redesign…" | "Dell's new PowerEdge R7xx servers move more air through each watt." |
| Contractions | none | yes |

Prompts are versioned, not edited (D-6): v1.0.0 still resolves for any campaign
that recorded it. `field_regenerate` got a matching v1.1.0, or regenerating the
body would have reverted the voice and reintroduced the asterisks.

⚠ **Process note.** The `.eml` generation script assumed `EMAIL_PROVIDER=console`
without asserting it. `.env` had been switched to `smtp`, so three `send_test()`
calls went to Gmail for real. The address was `recipient@example.com` (RFC 2606,
undeliverable) so nobody received anything, but it consumed quota and generated
bounces. **Any script that can send now asserts the provider first.**

### M8 — the settings table finally has a writer

`SettingsRepository` and `AuthService.set_password()` had existed since M1.3/M1.4
with **zero production callers** — the same dead-code shape as `app_logs` before
M7. Both are now wired. Third instance of this pattern; worth a grep for others
before M9.

**D-24 — configuration is two layers, and `.env` stays the bottom one.**
`services/settings_service.py` holds a registry of 28 runtime-editable settings.
A saved value overrides `.env`; "Revert" restores it. `.env` remains the source
of truth for a fresh install, so a handover is still configured by a file the
next developer reads in Git — but day-to-day changes don't need one.

Four things make it safe rather than merely convenient:

1. **Secrets cannot reach the registry.** `_validate_registry()` runs at import
   and raises if any registered field is a `SecretStr`. Adding `llm.api_key`
   fails the build, not review. That is D-19 as a mechanism instead of a
   convention.
2. **`validate_assignment=True`** on the settings sections, so an edit runs the
   same validators as startup. A rejected value leaves the old one in place —
   the app is never left holding a config it couldn't have booted with. Verified:
   `temperature=99` is refused and 0.7 survives.
3. **The stored value is the normalised one**, read back off the model after
   assignment. `https://host/v1/` is persisted as `https://host`. Storing the
   raw input would leave the database and the running process disagreeing about
   the endpoint — on this page of all pages.
4. **Live mutation, not a rebuild.** Every holder of `get_settings()` sees the
   change because the object is mutated in place. This only works because the
   factories call `get_settings()` per operation rather than caching a provider —
   an invariant of *other* modules, so
   `TestChangesReachTheRestOfTheApp` asserts it. If someone later caches a
   provider, those tests fail rather than the feature silently doing nothing.

**`configure_logging` is one-shot, so the log-level setting would have lied.**
It returns early if already configured, so saving a new level would have shown a
success toast and changed nothing. Added `set_log_level()`, which moves only the
console handler — the file sink stays at DEBUG, because the forensic trail is
what you want *after* something breaks and it is no use if it was quiet at the
time. `RotatingFileHandler` subclasses `StreamHandler`, so the file sink has to
be excluded explicitly or it gets silenced too.

**Startup order matters.** Overrides are applied *before* `configure_logging`,
because the log level is one of them. A saved value that no longer validates is
logged and skipped, never fatal: refusing to boot over a value that can only be
fixed in the UI you just prevented from loading is a trap with no exit.

Also: password change for your own account (re-authenticates first, so it runs
the lockout counter and can't be used to brute-force from inside a session);
settings editing gated to admins; self-deactivation refused rather than warned
about — the only admin locking themselves out has no way back in.

**Measured live (2026-08-08):** endpoint change → normalised → persisted →
survived a simulated restart → bad value refused → reverted. Test Connection
against the real Groq endpoint: **healthy, 491 ms, `openai/gpt-oss-120b`**.

### M7 — UI notes worth keeping

- **`app_logs` had no writer.** The table and repository existed since M1.3, but nothing
  ever wrote to it, so the Logs page would have been permanently empty. Added
  `modules/repository/log_handler.py` (`DatabaseLogHandler`), attached from `app.py`
  *after* `init_database()`. It lives in the repository layer, not `config`, because
  `config` cannot import the repository without an import cycle.
  A write failure **disables the handler** rather than raising on every subsequent log
  line — an unreachable database would otherwise turn one problem into a storm.
- **structlog puts the whole event dict on `record.msg`.** The first version stored
  `{'env': 'dev', 'event': 'app.started', …}` in the `event` column, which would have made
  the page unreadable and broken search. `_unpack()` now splits event name, context, and the
  two indexed fields (`correlation_id`, `campaign_id`) apart.
- **The first `test_pages.py` was vacuous** — it asserted on pages it never rendered, so it
  would have passed against a UI that crashed on every click. Rewritten around
  `AppTest.from_function`, which extracts the function's *source* and re-executes it: a
  closure over an outer variable is silently lost, so the page name is passed via
  `args=(module_name,)`. 26 page tests now genuinely render.

### M6 — the four guards between "click send" and a customer's inbox

Each has a test named after the failure it prevents:

1. **Suppression list**, checked in bulk pre-send. An unsubscribed address stays
   unsubscribed even when it is in a freshly uploaded CSV.
2. **Double-send guard** — `begin_send()` conditional UPDATE. A Streamlit rerun firing
   the handler twice is refused, verified end to end through `DeliveryService`.
3. **Retry skips already-delivered recipients.** Without it, "retry failed only"
   re-mails everyone who succeeded — the duplicate-send bug one layer down.
4. **Render-time compliance** — no unsubscribe link or postal address means no email.

Also: a per-recipient failure returns `FAILED` and the batch continues; an
account-level failure (401 / 402) raises and stops, carrying `sent_before_failure`
so what already went out is still recorded and not re-sent on retry.

### M4 live result (2026-08-07)

Article: `aws.amazon.com/blogs/aws/top-announcements-of-the-aws-summit-in-new-york-2026/`

| Stage | Result |
|---|---|
| Stage 1 | AI/ML, relevance 9/10, 5 key points, 14 technical facts |
| Stage 2 | subject **52/60** · preview **62/100** · cta **17/40** · 246 words — first attempt |
| Subject variants | 3 genuinely distinct angles (benefit / curiosity / factual) |
| Field regeneration | `Explore the specs` → `Act now, view specs` — honoured "make it more urgent" |

**Three findings that only a live call could produce:**

1. **`max_tokens` must be ≥ 2048 for any structured output.** gpt-oss emits internal
   reasoning tokens that count toward the budget but never appear in the response. A
   budget sized to the visible output is cut off mid-JSON, and Groq reports that as a
   400 `json_validate_failed` — *not* `finish_reason: length`, so the token-budget
   error path never fired. Now mapped explicitly, and `validate_prompts.py` enforces
   the floor.
2. **`article_summary` at 1024 tokens failed on a real article** and passed on my
   synthetic one. Synthetic fixtures do not exercise token budgets.
3. **`PromptRegistry.render()` needed positional-only parameters.** A prompt variable
   named `name` collided with the method parameter — plausible in real use
   (`brand_name`, `field_name`) and baffling when it happens.

⚠ **Timing:** 4 calls on one large article took ~68 s, against ~5 s for a small one.
That gap is rate limiting, not model speed — the free-tier TPM ceiling is being hit.

### M3 VERIFIED LIVE (2026-08-07) — `LLM_PROVIDER=groq`

First real generation succeeded end to end. Two schema incompatibilities were found and
fixed **only because a real call was made** — both were invisible to the mock provider:

1. **`required` must list every property.** Pydantic omits fields with defaults, so
   `technical_facts` was optional and Groq refused the request. `_strictify` now marks
   every property required.
2. **`minLength`/`maxLength` are rejected by strict mode.** They are now stripped from the
   wire schema. **This corrects an earlier claim**: string lengths are *not* enforced at
   generation time. Structure is (keys, types, enums, array bounds); length is carried by
   the prompt, validated by Pydantic, and repaired on retry.

Measured, 1 article, `openai/gpt-oss-120b`:

| | |
|---|---|
| Health check | ~320 ms |
| Stage 1 (summary) | 1.7 s · 599 in / 603 out |
| Stage 2 (compose) | 3.6 s · 737 in / 1511 out · **valid on attempt 1** |
| Total | **3,450 tokens, 5.3 s** (Colab design budgeted 60–90 s) |
| Output | subject 56/60 · preview 95/100 · cta 21/40 · body 234 words |

**Prompt wording carries real weight.** Without the limits in the prompt, `subject`,
`preview_text` and `cta` overshot every time. With `MAXIMUM 60 characters (this is a hard
limit, not a target)` they landed in range first try. M4's prompts must state limits this
way — "approximately" does not work.

⚠ **Open: TPM headroom.** 3 articles ≈ 10,350 tokens against a free-tier ceiling of
~8–12k **per minute**. That is at or over the line. Mitigations if it bites: stagger the
stage-1 calls, drop to `gpt-oss-20b`, or add a card for ~10× limits.

### M2 live extraction result (2026-08-06)

Article URLs discovered from AWS, Cisco and Dell blog indexes, then extracted:

| Metric | Target (M2 done-criteria) | Actual |
|---|---|---|
| Tier-1 (Trafilatura) success | ≥ 85% | **88%** (100% of genuine articles) |
| Any-tier success | ≥ 95% | **88%** — the miss was a non-article page |

Also observed live and handled correctly: Fortinet's robots.txt disallow, a Microsoft
403 block, and an HPE timeout — each produced its intended actionable message.

### Verified environment (2026-08-06)

| | |
|---|---|
| Python | **3.14.3** (Windows 11, AMD64) — all 132 packages resolved **wheel-only**; `trafilatura`, `lxml`, `pyarrow`, `bcrypt`, `SQLAlchemy` exercised at runtime, not just imported |
| Pin | `requires-python = ">=3.11,<3.15"` |
| Lockfile | `requirements.lock.txt` (132 pinned packages) for byte-identical handover installs |
| Gates | `ruff` · `ruff format` · `mypy` · `lint-imports` (4/4 contracts) · `check_no_local_inference` · `pytest` |

### Bugs found and fixed during M1.1 verification (all have regression tests)

1. **Windows startup crash** — `structlog` `ConsoleRenderer(colors=True)` raises `SystemError`
   on Windows without `colorama`. Now detects support; colour never blocks startup.
2. **cp1252 mangling** — Windows console mangled em dashes/smart quotes from scraped titles.
   stdout forced to UTF-8.
3. **Escaped Unicode in logs** — `JSONRenderer` defaulted to `ensure_ascii=True`, storing
   `Dell’s`. Now `ensure_ascii=False` so the Logs page search and `grep` both work.
4. **Test isolation** — `get_settings()` read the developer's `.env`, so tests would pass
   locally and fail in CI. Added `build_settings(env_file=...)`; the suite passes `None`.

---

## 10. Milestone Progress

| # | Milestone | Status |
|---|---|---|
| M0 | Planning & documentation | ✅ Complete |
| M1 | Project skeleton, config, logging, DB | ✅ Complete |
| M2 | Scraper + Cleaner | ✅ Complete |
| M3 | LLM provider (Groq) | ✅ Complete — verified against the live API |
| M4 | Prompt registry + AI engine | ✅ Complete — verified live |
| M5 | Template engine (hand-authored HTML) | 🟡 Code complete; awaiting real email-client check |
| M6 | Email engine + campaign manager | ✅ Complete |
| M7 | Streamlit UI (6 pages) | ✅ Complete |
| M8 | Auth, settings, logs, health | ✅ Complete — verified live |
| M9 | Tests, README, handover pack | ✅ Complete |

---

## 11. Document Index

| Doc | Contents |
|---|---|
| `docs/01_PRD.md` | Phase 1 — Product Requirements Document |
| `docs/02_TRD.md` | Phase 2 — Technical Requirements Document |
| `docs/03_RESEARCH_AND_DECISIONS.md` | Phases 3–5 — research findings, stack comparison, LLM selection |
| `docs/04_LLM_HOSTING.md` | Phase 6 — Groq hosting, model choice, rate limits, the handover swap |
| `docs/05_UI_SPEC.md` | Phase 7 — wireframes, components, states for all 6 pages |
| `docs/06_BACKEND_ARCHITECTURE.md` | Phase 8 — module contracts, interfaces, SOLID mapping |
| `docs/07_PROMPT_ENGINEERING.md` | Phase 9 — prompt library, JSON schema, versioning |
| `docs/08_MILESTONE_PLAN.md` | Phase 10 — milestones, files, functions, tests, deliverables |
| `docs/09_FINAL_DECISIONS.md` | **Locked decision register (D-1…D-20), zero-local-inference guarantee, security control matrix, deliverability contract** |
| `docs/04_LLM_HOSTING.md` | **Groq setup, model choice, rate limits, failure handling, and the handover swap (D-21)** |
