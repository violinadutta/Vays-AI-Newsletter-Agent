# Architecture

For the developer who has to change this. Explains the shape and, more usefully,
**why** it is that shape — the reasoning is what tells you whether your change fits.

---

## 1. The one rule

```
ui/  →  services/  →  modules/  →  core/
```

Dependencies point **downward only**. `core` imports nothing from the project;
`ui` is imported by nothing.

This is not a convention. `import-linter` enforces four contracts on every CI run:

| Contract | Effect |
|---|---|
| Layers point downward only | `modules` cannot import `services` |
| `core` is pure | No `streamlit`, `sqlalchemy`, `httpx`, `pandas` in `core` |
| Business logic never imports Streamlit | `services` and `modules` stay framework-free |
| No local inference runtime anywhere | `torch`, `vllm`, `ollama` … are a build failure |

Break one and the build fails. That is deliberate: an architecture rule nobody
enforces is a suggestion.

**Practical consequence:** the service layer can be wrapped in FastAPI later without
a rewrite, because it has zero knowledge that Streamlit exists.

---

## 2. Layer by layer

### `core/` — pure domain

No I/O, no frameworks, no network. Fully unit-testable with no fixtures.

| File | Contents |
|---|---|
| `models.py` | 25 Pydantic DTOs — everything crossing a boundary |
| `enums.py` | `CampaignStatus` + its transition table, `Tone`, `Category`, `EditableField` |
| `exceptions.py` | 23 exception types, each with `user_message` and `retryable` |
| `validators.py` | Email normalisation, **SSRF guard** |
| `auth.py` | bcrypt hashing, HMAC session tokens, password rules |
| `schemas.py` | JSON Schema generated *from* the Pydantic models |

**Why DTOs everywhere rather than dicts:** a dict has no contract. When the LLM
returns a field you did not expect, or a scraper drops one, a Pydantic model fails
at the boundary with a message naming the field. A dict fails three layers later
with `KeyError`.

**`schemas.py` generates the LLM's JSON Schema from the same models that validate
the response.** They cannot drift, because there is only one definition.

### `config/` — settings and logging

`settings.py` builds seven `BaseSettings` sections from `.env`. Two properties:

1. **Fail fast, and fail completely.** Every problem across all sections is
   reported at once, each named by its environment variable, so the whole file gets
   fixed in one pass.
2. **`validate_assignment=True`**, which is what makes runtime settings edits safe —
   an edit runs the same validators as startup, and a rejected value leaves the
   previous one in place.

`logging_config.py` wires structlog to three sinks: console (level configurable),
`logs/app.jsonl` (always DEBUG), and the `app_logs` table. Correlation IDs bind per
operation.

### `modules/` — adapters

Each subpackage owns one external concern behind an interface.

```
scraper/    Trafilatura → Newspaper4k → BeautifulSoup, tried in order
cleaner/    normalise, dedupe, truncate to a token budget
ai/         LLMProvider (Groq | Hosted | Mock) + PromptRegistry + circuit breaker
template/   Jinja2 → table HTML → premailer CSS inlining → plain-text part
email/      EmailProvider (Brevo | SMTP | Console) + batching + retry
repository/ SQLAlchemy ORM + 7 repositories + Alembic migrations
```

**Ports and adapters, applied only where volatility justified it** — the LLM host,
the email provider, the database. Not everywhere; an interface with one
implementation that will never have another is a cost with no return.

That judgement was validated once: replacing Colab with Groq cost **one class and a
config default**.

### `services/` — use cases

Orchestration, transaction boundaries, no UI knowledge.

| Service | Owns |
|---|---|
| `ingestion_service` | URLs → extracted, cleaned, persisted articles |
| `generation_service` | Articles → two-stage AI → persisted draft |
| `delivery_service` | Recipients + content → rendered, batched, sent, recorded |
| `campaign_service` | Lifecycle state machine, history |
| `auth_service` | Login, lockout, user management |
| `settings_service` | Runtime-editable settings, validated and applied live |
| `health_service` | Dependency checks for the Dashboard |

### `ui/` — Streamlit

`app.py` bootstraps once (`@st.cache_resource`), guards auth, and builds navigation.
Pages are functions. `ui/state.py` wraps `st.session_state` in typed accessors —
session state is an untyped bag, and the accessors are where the safety lives.

---

## 3. The pipeline

```
URLs
 │
 ├─ fetcher      SSRF guard → robots.txt → polite UA → redirect re-validation
 ├─ extractor    Trafilatura → Newspaper4k → BS4 (first success wins)
 ├─ cleaner      normalise, dedupe, truncate to 3000 tokens
 │
 ├─ STAGE 1  per article, ≤3 concurrent → ArticleSummary
 │           extractive: headline, key points, business impact, technical facts
 │
 ├─ STAGE 2  all summaries together → NewsletterContent (9 fields)
 │           generative: composes across articles
 │
 ├─ HUMAN REVIEW  ◀── nothing proceeds without this
 │
 ├─ renderer     Jinja2 → table HTML → inlined CSS → plain-text alternative
 └─ batcher      suppression check → batches → retry → per-recipient results
```

**Why two stages rather than one prompt (D-4):** smaller prompts produce better
output on a mid-size model, retries are cheaper, and the extractive first stage is a
hallucination control — the generative stage works from enumerated facts rather than
raw article text.

**Why ≤3 concurrent summaries:** the value is overlapping network latency. A wider
fan-out against a per-minute token ceiling earns 429s that cost more than the
parallelism saves.

---

## 4. Decisions that shape the code

Full register in [09_FINAL_DECISIONS.md](09_FINAL_DECISIONS.md); the ones you will
feel while editing:

| | Decision | Consequence for you |
|---|---|---|
| **D-1** | No separate API in v1 | Service layer is framework-free; keep it that way |
| **D-3** | Guided JSON decoding | `json.loads` cannot fail on gpt-oss models |
| **D-4** | Two-stage generation | Two prompts, two schemas |
| **D-5** | Human review is mandatory | Do not add an auto-send path |
| **D-6** | Prompts are versioned YAML | **Never edit a published version** |
| **D-12/13** | No local inference, machine-enforced | Adding `torch` fails the build |
| **D-19** | Secrets in `.env` only | The settings registry rejects `SecretStr` at import |
| **D-23** | Hand-authored table HTML | No MJML, no Node, edit the HTML directly |
| **D-24** | Two config layers | DB overrides `.env`; `.env` is the handover baseline |

---

## 5. Cross-cutting mechanics

### Errors

Every exception carries a `user_message` — plain English, no stack trace, no status
code. The UI renders that and nothing else. `retryable` distinguishes "try again"
from "this will always fail", which is what the retry logic keys on.

### Logging and correlation

Every operation binds a correlation ID. Three sinks, one call. The Logs page lets a
non-technical user click an ID and see every event from one operation — that is what
turns "it broke around 3pm" into a diagnosis.

### Resilience

- **Circuit breaker** on the LLM provider (CLOSED → OPEN → HALF_OPEN). Stops
  hammering a dead service.
- **Tenacity retries** with exponential backoff, honouring `Retry-After`.
- **Deterministic failures are not retried** — retrying a CUDA OOM or a schema
  rejection just burns three timeouts.

### Persistence

SQLite with `WAL`, `foreign_keys=ON`, `busy_timeout`. `unit_of_work()` is the single
transaction boundary; repositories never commit.

---

## 6. Extending it

### Add an LLM provider

1. Subclass `LLMProvider` in `modules/ai/` (or `OpenAICompatibleProvider` if it
   speaks that protocol — most do).
2. Add one line to `_PROVIDERS` in `modules/ai/factory.py`.
3. Add the literal to `LLMSettings.provider`.

No other file changes. That is the property the factory exists to preserve.

### Add an email provider

Subclass `EmailProvider`, add a line to `modules/email/factory.py`.

**Contract:** `send()` must **not** raise for a per-recipient failure — return
`SendResult(FAILED)`. A batch of 500 must not abort over one dead mailbox. Raise
only for conditions that make the rest of the batch pointless (401, 402).

### Add an email template

Copy `templates/email/modern.html`. Rules, all of which exist because Outlook uses
Word's rendering engine and Gmail strips `<style>`:

- Tables for layout, never divs; `role="presentation"`
- Fixed 600px width, explicit on every table
- No flexbox, grid, float, or `background-image`
- CTA is a padded table cell, not a `<button>`
- MSO conditional wrapper for Outlook
- Must include `{{ brand.address }}` and `{{ brand.unsubscribe_url }}` — the
  compliance assertion refuses to render without them

The parametrised template tests pick up a new file automatically.

### Add a prompt version

Copy `prompts/<name>/v1.1.0.yaml` to `v1.2.0.yaml`, edit, run
`scripts/validate_prompts.py`. `latest` resolves to the highest version. **Never
edit a published version** — campaigns record what produced them.

### Add a runtime-editable setting

One entry in `EDITABLE` in `services/settings_service.py`. Widget type, bounds and
the `.env` variable name are all derived from the Pydantic field.

Secrets are refused at import time. That is D-19 as a mechanism rather than a rule.

---

## 7. Testing

| Kind | Marker | Covers |
|---|---|---|
| Unit | none | Pure logic, no I/O |
| Integration | `@pytest.mark.integration` | Real temporary SQLite |
| End-to-end | `@pytest.mark.e2e` | Real Streamlit rendering via `AppTest` |
| Network | `@pytest.mark.network` | Skipped by default |

**Tests never read your `.env`.** `build_settings(env_file=None)` reads only real
environment variables, so a test cannot pass locally and fail in CI because of a
file on your machine.

**Naming:** tests are named after the failure they prevent, not the method they
call — `test_a_rejected_value_leaves_the_old_one_in_place`, not `test_set`. When one
fails, the name tells you what broke.

**Offline development:** `LLM_PROVIDER=mock` runs the entire pipeline from JSON
fixtures. No network, no key, no cost.
