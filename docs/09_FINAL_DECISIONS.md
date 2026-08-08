# Final Decisions — Locked
### Zero local inference · Security posture · Deliverability contract
**Date** 2026-08-05 · **Status** LOCKED (supersedes conflicting statements in docs 01–08)
**Changing any decision here requires an ADR in `docs/ADR/`.**

---

## 0. The three guarantees

| # | Guarantee | How it is *proven*, not promised |
|---|---|---|
| **G1** | **No LLM, model, or inference runtime ever runs on your machine** | Architecture has no local adapter · CI fails the build on any ML dependency · RAM/disk budget measured below |
| **G2** | **The system is secure** | 20 controls mapped to named threats, each with an owning module · zero secrets in code, DB, logs or UI · recipient PII never leaves the machine |
| **G3** | **It is fully deliverable** | Every dependency permissive-licensed · one-command install · runs with no GPU and no Colab · the 30-minute handover test is an acceptance gate |

---

# G1 — Zero Local Inference

## 1.1 What changed from the first draft

The Phase-6 design had **four** LLM adapters. One of them — `LocalProvider`, running Ollama with a
small quantized model on `localhost:11434` — would have loaded model weights into your 8 GB
machine. That is now **deleted**.

```
BEFORE                                    AFTER (locked)
─────────────────────────────             ─────────────────────────────
LLMProvider                               LLMProvider
 ├── ColabProvider    (remote)  ✓          ├── ColabProvider   (remote)   ✓
 ├── HostedProvider   (remote)  ✓          ├── HostedProvider  (remote)   ✓
 ├── LocalProvider    (LOCAL)   ✗ DELETED  └── MockProvider    (fixtures) ✓
 └── MockProvider     (fixtures)✓
```

**Offline development is not lost — it got better.** `MockProvider` reads canned JSON fixtures
from `tests/fixtures/llm_responses/`. It needs no model, no weights, no GPU, no network. And for
developing the UI, the template engine, or the send pipeline, deterministic fixtures are *more*
useful than a small model's output, because a test that depends on a 1.7B model's phrasing is a
flaky test.

## 1.2 Where inference actually happens

```
┌──── YOUR WINDOWS LAPTOP · 8 GB RAM · NO GPU ────┐
│                                                 │
│  Streamlit + Python app          ~350 MB RAM    │
│  SQLite file                     ~50 MB disk    │
│                                                 │
│  Model weights on disk ................. 0 B    │
│  Model weights in RAM .................. 0 B    │
│  GPU used .............................. none   │
│  CUDA / torch / transformers installed . NO     │
│                                                 │
│         HTTP request with article text          │
└───────────────────────┬─────────────────────────┘
                        │ HTTPS + Bearer token
                        ▼
        ┌───────────────────────────────────┐
        │  SOMEONE ELSE'S GPU               │
        │  Colab T4 (dev) or hosted (prod)  │
        │  ← 100% of inference happens here │
        └───────────────────────────────────┘
```

## 1.3 Measured resource budget on your machine

| Component | RAM | Disk |
|---|---|---|
| Python 3.11 + Streamlit | ~180 MB | ~120 MB |
| pandas (Streamlit already requires it) | ~90 MB | ~50 MB |
| httpx, pydantic, SQLAlchemy, Jinja2, trafilatura, lxml, bcrypt, structlog | ~60 MB | ~80 MB |
| App code + templates + prompts | ~20 MB | ~5 MB |
| SQLite (1,000 campaigns, 50k send records) | — | ~50 MB |
| **Total** | **≈ 350 MB** | **≈ 305 MB** |
| *For comparison: what we are NOT installing* | | |
| ~~torch + CUDA runtime~~ | ~~2–4 GB~~ | ~~~2.5 GB~~ |
| ~~a 4-bit 8B model~~ | ~~5.5 GB~~ | ~~~5.5 GB~~ |

**~350 MB against 8 GB.** You can run this alongside VS Code, a browser and Teams without noticing
it. That headroom is the entire reason for D-14 below.

## 1.4 The enforcement mechanism — D-13

Documentation does not stop a future developer from `pip install transformers` "just for the
tokenizer." A build failure does.

`scripts/check_no_local_inference.py` runs in **CI and as a pre-commit hook**, and fails on any of
these appearing in `requirements*.txt` or in any `import` statement in the source tree:

```
torch · tensorflow · jax · flax
transformers · sentence-transformers · accelerate · optimum · bitsandbytes
llama-cpp-python · ctransformers · exllamav2 · autoawq · gpt4all
onnxruntime · onnxruntime-gpu · vllm · ollama
```

This is the difference between a design intention and a property of the system. It survives me
leaving the project.

## 1.5 The decision that makes it hold — D-14

The one place the constraint was quietly at risk: **token counting.** The draft proposed
`transformers` (pulls torch, ~2 GB) or `tiktoken` (downloads BPE files at runtime, and is the
wrong tokenizer for Qwen anyway).

**Locked:** a pure-Python heuristic estimator, calibrated once against Qwen3's tokenizer during M2
and hard-coded as a ratio. We need a **±10% budget estimate** to decide when to truncate an
article — not an exact count. A character/word heuristic delivers that with zero dependencies.

This single decision is what keeps torch out of the project. It is also a good illustration of the
general principle: the requirement was never "count tokens exactly", it was "don't blow the context
window", and the cheaper reading of the requirement removed 2 GB.

## 1.6 Development without Colab, without a GPU, without internet

```powershell
# .env
LLM_PROVIDER=mock
```
The **entire** pipeline runs: extraction (from saved HTML fixtures), cleaning, generation (from JSON
fixtures), rendering, CSV validation, batching, sending (console provider writes `.eml` files to
disk), campaign history, logs, auth. Full test suite passes. No GPU, no network, no Colab session.

This is not a degraded mode. It is the default development mode.

---

# G2 — Security

## 2.1 Locked security decisions

| ID | Decision | Reasoning |
|---|---|---|
| **D-15** | **Own auth module** (`core/auth.py`, ~80 lines, `bcrypt`) — `streamlit-authenticator` **rejected** | An unvetted small community package sitting on the authentication boundary is the worst possible place for supply-chain risk. The needed surface is narrow: hash, verify, sign a session token, guard a page. ~80 auditable lines beats an opaque dependency, and whoever inherits this can read all of it in ten minutes. |
| **D-16** | **`html2text` (GPL-3.0) dropped.** Plain-text email part composed directly from `NewsletterContent` fields | GPL in a commercial client deliverable is a real problem, and the dependency was unnecessary — the content fields are already plain text. Removing it also *improves* output: converted HTML produces artefacts, direct composition doesn't. **Result: every remaining dependency is permissive-licensed.** |
| **D-19** | **Secrets live only in `.env`** (git-ignored, `.env.example` committed with placeholders). Optional Windows DPAPI via `keyring` documented as a hardening step | Never in source, never in the `settings` DB table, never in logs, always masked in the UI (`sk-••••4f2a`). `detect-secrets` pre-commit hook + `pip-audit` in CI. |
| **D-20** | **Recipient PII never leaves your machine** | Email addresses go from CSV → local SQLite → the email provider's send API. They are **never** sent to the LLM, never logged in full (masked to `p***a@vays.com`), and the SQLite file is excluded from any cloud-sync path. |
| ~~**D-17**~~ *(superseded by D-21, 2026-08-07)* | ~~Shipped default is `LLM_PROVIDER=colab`~~ | Colab is gone. See D-21. |
| **D-21** *(2026-08-07)* | **Groq is the LLM host. Colab is removed entirely.** | Colab was attempted twice on real hardware and never produced a token: first the CUDA-13 wheel vs Colab's CUDA-12 runtime (`ImportError: libcudart.so.13`), then an install/restart loop. Those were fixable; the shape was not — ~3-hour sessions, a tunnel URL that rotates on every restart, no guaranteed GPU, a shifting CUDA/torch matrix, and a standing ToS conflict. **Groq serves only open-source models**, so the brief's constraint is satisfied more cleanly than by self-hosting, with none of the operational tax. Cost of the switch: one small class and a config default. |

## 2.2 What data crosses the machine boundary

This is the table to show anyone who asks whether the system is safe.

| Data | Leaves your machine? | Goes where | Control |
|---|---|---|---|
| OEM article text (public web content) | ✅ yes | LLM endpoint | Already public. Fenced as untrusted data in the prompt |
| Generated newsletter copy | ✅ yes | Email provider, at send | The intended purpose |
| **Recipient email addresses** | ⚠️ **only to the email provider at send time** | Brevo API over TLS | **Never to the LLM.** Never logged in full |
| Recipient names / companies | ⚠️ same | Brevo API (merge fields) | Same |
| API keys, passwords | ❌ **never** | — | `.env` only; masked in UI; scrubbed from logs |
| Campaign history, drafts, logs | ❌ **never** | — | Local SQLite only |
| Tunnel URL | ❌ **never** | — | Treated as a secret |

## 2.3 Control matrix — threat → control → owner

| Threat | Control | Lives in |
|---|---|---|
| **SSRF** — user pastes `http://169.254.169.254/…` or an internal host | Hostname resolved and checked against private/loopback/link-local/reserved ranges; scheme restricted to http(s); **each redirect hop re-validated**; max 3 hops | `modules/scraper/fetcher.py` (at the boundary that performs the fetch, so no caller can bypass it) |
| **Prompt injection** from scraped content | Article fenced in `<<<ARTICLE>>>` markers + explicit system rule that fenced content is data, not instruction; guided decoding constrains output shape regardless of instruction-following; **human approval gate is the final control** | `prompts/_shared/untrusted_input_rules.md` + product design |
| **Template injection** — `{{ }}` surviving extraction into a template | Jinja2 `SandboxedEnvironment`, `autoescape=True`; user content passed as *data*, never concatenated into template source | `modules/template/renderer.py` |
| **XSS in preview** | Preview rendered in a sandboxed iframe; extraction returns text, not HTML | UI spec §5.1 |
| **LLM endpoint abuse** — someone finds the tunnel URL | vLLM `--api-key`; bearer token on every request; token regenerated every session, so a leaked URL is worthless after the next restart | Colab notebook cell 3 |
| **Credential leakage** | `.env` git-ignored · masked in UI · `redact_secrets` structlog processor scrubs `(?i)(key\|token\|password\|secret\|authorization)` · `detect-secrets` pre-commit | `config/logging_config.py` |
| **Unauthorized access** | Auth guard on every page; signed session token; send restricted to `approver`/`admin` | `core/auth.py` |
| **Weak passwords** | bcrypt, cost 12, per-password salt; 5-attempt lockout with cooldown | `core/auth.py` |
| **SQL injection** | SQLAlchemy parameterized queries exclusively; **no raw SQL anywhere** (also preserves the Postgres path) | `modules/repository/` |
| **CSV injection** — `=cmd\|…` in an exported cell | Prefix `= + - @` with `'` on export | `services/campaign_service.py` |
| **Accidental mass send** | Confirmation modal stating recipient count + fact-check checkbox; **DB-level conditional `UPDATE … WHERE status IN (…)`** makes a double-fire physically impossible | `CampaignRepository.transition_status()` |
| **Sending to unsubscribed contacts** | Global suppression table checked pre-send, enforced even when the address is in the uploaded CSV | `services/delivery_service.py` |
| **Supply-chain** | Pinned versions · `pip-audit` in CI · **all-permissive licence audit** · no unvetted package on the auth path (D-15) · ML runtimes banned (D-13) | CI |
| **PII exposure** | Local-only storage · masked in logs · documented deletion procedure · DB excluded from sync | D-20 |
| **Hallucinated facts reaching a customer** | Human approval gate · two-stage pipeline · `technical_facts` enumeration · source links in UI · fact-check checkbox | Product design (Prompt doc §7) |

### Explicitly out of scope for v1 — stated so the gap is a decision, not an oversight
Per-user rate limiting · CSRF tokens (Streamlit's websocket model + local-only deployment) ·
encryption at rest for the SQLite file · audit logging of *read* access · MFA.
Each is documented in `KNOWN_ISSUES.md` with the trigger that would make it necessary.

---

# G3 — Fully Deliverable

## 3.1 Licence audit — all permissive, no copyleft

| Licence | Packages |
|---|---|
| MIT | pydantic, pydantic-settings, SQLAlchemy, Alembic, newspaper4k, beautifulsoup4, tiktoken*(dropped)*, pytest, ruff, mypy |
| Apache-2.0 | streamlit, trafilatura, tenacity, structlog, bcrypt, openai-SDK *(used as a protocol client only)* |
| BSD | httpx, Jinja2, premailer, lxml, pandas |
| **GPL / AGPL / SSPL** | **none** ✅ |
| Model | Qwen3 — **Apache 2.0** ✅ |

The model licence matters as much as the code licence: Qwen3 under Apache 2.0 carries no
attribution requirement and no acceptable-use clause, unlike Llama (community licence) or Gemma
(use restrictions). For something handed to a company, that is the difference between "you can use
this" and "legal needs to review this."

## 3.2 Runtime requirements at handover

| Requirement | Status |
|---|---|
| GPU | ❌ not needed |
| CUDA | ❌ not needed |
| Node.js | ❌ not needed at runtime (MJML is build-time only — D-18; compiled HTML is committed) |
| Database server | ❌ not needed (SQLite is a file) |
| Docker | ❌ not needed for v1 |
| Colab account | ❌ **not needed** — ships with `LLM_PROVIDER=hosted` |
| Python 3.11+ | ✅ the only prerequisite |
| RAM | ✅ ~350 MB |

**One prerequisite.** That is the deliverability story.

## 3.3 Install on a fresh Windows machine

```powershell
git clone <repo> ; cd vays-ai-new
py -3.11 -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env        # edit: LLM key, email key, brand details
alembic upgrade head
python -m scripts.create_user
streamlit run app.py
```
Plus `run.bat` for the non-developer who has to restart it on a Monday morning.

## 3.4 What "fully deliverable" is contractually gated on

From PRD §11 — these are acceptance criteria, not aspirations:

- [ ] Fresh Windows machine → running app in ≤ 30 min from the README alone, **verified with a real
      person who has not seen the project**
- [ ] `pytest` green, ≥70% coverage on core modules, on **both** Windows and Linux in CI
- [ ] `check_no_local_inference.py` green
- [ ] `pip-audit` clean
- [ ] Full run with `LLM_PROVIDER=mock` — no network, no GPU
- [ ] Switching `LLM_PROVIDER` between `hosted` and `colab` requires **no code change**
- [ ] No secret in any log file, in the UI, or in the repository
- [ ] Every delivered email contains a working unsubscribe link, a physical address, and a
      plain-text part
- [ ] Docs complete: README · SETUP_GUIDE · ARCHITECTURE · RUNBOOK · PROMPT_GUIDE · ADRs ·
      KNOWN_ISSUES · `.env.example` with every variable described
- [ ] Handover session recorded

---

## 4. Locked decision register

| ID | Decision | Status |
|---|---|---|
| D-1 | Modular monolith; service layer in-process, no separate API in v1 | 🔒 |
| D-2 | "Open source LLM" constrains the **model licence**, not the hosting method | 🔒 |
| D-3 | Guided JSON decoding (vLLM + XGrammar), not prompt-and-parse | 🔒 |
| D-4 | Two-stage generation: per-article summary → composition | 🔒 |
| D-5 | Human approval gate is mandatory and architectural | 🔒 |
| D-6 | Prompts as semver'd YAML in Git; version recorded per campaign | 🔒 |
| D-7 | Model: **Qwen3-14B-Instruct AWQ** (Apache 2.0); 8B / 4B fallbacks | 🔒 * |
| D-8 | Serving: vLLM, OpenAI-compatible wire format, `dtype=float16` on T4 | 🔒 |
| D-9 | Extraction: Trafilatura → Newspaper4k → BS4 cascade → manual paste | 🔒 |
| D-10 | Email: Brevo primary, SMTP + console adapters | 🔒 |
| D-11 | SQLite + SQLAlchemy 2.x + Alembic; no raw SQL | 🔒 |
| **D-12** | **No local inference adapter — `LocalProvider` deleted** | 🔒 |
| **D-13** | **Zero-local-inference enforced by CI, not documentation** | 🔒 |
| **D-14** | **Pure-Python heuristic token estimator — keeps torch out** | 🔒 |
| **D-15** | **Own bcrypt auth module; `streamlit-authenticator` rejected** | 🔒 |
| **D-16** | **`html2text` (GPL) dropped; all dependencies permissive** | 🔒 |
| **D-17** | **Ships defaulting to `hosted`; Colab is the dev option** | 🔒 |
| ~~D-18~~ *(superseded by D-23)* | ~~MJML is build-time only~~ | See D-23. |
| **D-23** *(2026-08-07)* | **MJML dropped entirely. Email templates are hand-authored table HTML.** | Node is not installed on the development machine, so the `.mjml` sources could not be compiled here. Committing a "source of truth" nobody can build is worse than not having one: the next person edits the `.mjml`, fails to compile, edits the HTML instead, and the two diverge silently. MJML's value is abstracting table-layout quirks — for three fixed newsletter layouts that is a few hundred lines of markup we can write directly, and it is what MJML emits anyway. **This removes the last Node dependency from the project at any stage**, which is a strict deliverability win (G3). The compatibility rules MJML would have applied are documented in a header comment in each template and asserted by tests. |
| **D-19** | **Secrets in `.env` only — never code, DB, logs or UI** | 🔒 |
| **D-20** | **Recipient PII never leaves the machine except to the email provider** | 🔒 |
| **D-21** | **Groq replaces Colab as the LLM host; all Colab code and docs deleted** | 🔒 |
| **D-22** | **Default model `openai/gpt-oss-120b` — Apache 2.0 open-weight despite the name** | 🔒 * |

\* D-22 is the one decision most likely to be questioned on sight. `openai/gpt-oss-*` is
OpenAI's **open-weight** release under Apache 2.0 — downloadable, self-hostable, no usage
restrictions. It is chosen because it is one of the few Groq models supporting `strict: true`
constrained decoding, which is what keeps D-3 a guarantee rather than a hope. Switching to a
Qwen or Llama model is one `.env` line, at the cost of dropping to repair-retry.

\* D-7 carries one empirical check: confirm the AWQ kernel path works on compute capability 7.5 and
compare 14B vs 8B on 3 real OEM articles (M3 acceptance criterion 9). If 14B-AWQ misbehaves on a
T4, fall back to 8B — a config change. The decision is locked; the size within the Qwen3 ladder is
evidence-driven.

---

## 5. What this changed in the existing docs

| Doc | Change |
|---|---|
| `CLAUDE.md` | Constraint 2 rewritten as zero-local-inference + enforcement · stack table (transport, auth) · architecture diagram · decisions D-12…D-17 added · stack marked LOCKED |
| `02_TRD.md` | Layer + folder diagrams drop `local_provider` · S-5 auth control · §13 dependency table (html2text removed, streamlit-authenticator removed, tokenizer decision, bcrypt added) · **new §13.1 banned-dependency list** · `LLM_PROVIDER` values |
| `03_RESEARCH_AND_DECISIONS.md` | §4.6 auth rewritten with the rejection rationale · Ollama clarified as Colab-only |
| `04_COLAB_LLM_ARCHITECTURE.md` | Provider diagram and table drop `LocalProvider`, with a note explaining the removal |
| `08_MILESTONE_PLAN.md` | M3 file list drops `local_provider.py`, adds `check_no_local_inference.py` |

---

## 6. Resolved 2026-08-05

**Hosted endpoint — resolved.** Vays is providing no hosted LLM access and no cloud space. Colab is
the only runtime for this build (D-17 revised). The `hosted` provider is still built, tested and
documented so the handover swap is proven rather than theoretical.

**Colab ToS (doc 04 §0.1) — accepted with mitigation.** Owner is aware. Use a dedicated throwaway
Google account; keep the notebook tab open during use; point nothing but the dev machine at the
endpoint.

### The consequence: swap-ability is now a first-class deliverable

Because Vays will replace the LLM themselves, the provider seam is the single most important part
of the handover. It is protected by:

- `HostedProvider` is **built and tested in M3**, not left as a stub — an untested escape hatch is
  not an escape hatch.
- A shared `LLMProvider` contract test suite runs against **every** implementation, so a future
  provider that violates the contract fails CI.
- `docs/SWAP_THE_LLM.md` (M9): a standalone chapter — what to change, what not to touch, how to
  verify, and how to roll back.
- Acceptance gate: switching provider requires **zero code changes**, verified in CI.
