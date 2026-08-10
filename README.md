<div align="center">
  <img src="assets/image001.png" alt="Vays Infotech" width="220" />

# AI Newsletter Generation &amp; Distribution Platform

**Paste OEM blog URLs → get a reviewed, on-brand newsletter in your customers' inboxes.**

</div>

---

## What this is

Marketing at Vays Infotech used to read OEM partner blogs by hand, summarise them,
rewrite them into customer-facing copy, wrestle the result into HTML, and send it.
Slow, inconsistent in tone, and hard to scale across partners.

This automates the pipeline end to end:

```
 paste URLs → extract → clean → AI writes → YOU EDIT → render → send → log
                                             ▲
                                    nothing sends without
                                    a human approving it
```

The human review step is not a convenience. It is the control that stops a
hallucinated product name reaching a customer, and it cannot be skipped.

**Status:** feature-complete. 835 tests, 90% coverage, verified against the live
Groq API and a real Gmail send.

---

## Quick start

Already set up? Double-click **`run.bat`**.

First time on this machine? **[docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)** takes you
from a fresh clone to a sent test newsletter in about 20 minutes. The short version:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
copy .env.example .env
notepad .env                       # paste your Groq API key into GROQ_API_KEY
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe scripts\create_user.py
run.bat
```

Opens at **http://localhost:8501**.

> **A free Groq key is all you need to run this.** No GPU, no model download, no
> cloud account. See [§ The AI](#the-ai).

---

## The six pages

| Page | What it is for |
|---|---|
| **Dashboard** | Is anything broken right now? Live health of the AI service, database and email provider, plus recent campaigns. |
| **Generate** | Paste 1–10 OEM blog URLs, pick tone / length / audience, generate. Sites that block scraping fall back to manual paste. |
| **Preview** | Edit all nine fields, regenerate any single one, see the real email render, upload recipients, send. |
| **History** | Every campaign with delivery stats, filterable. Retry failed recipients without re-mailing the ones who got it. |
| **Settings** | Change the model, endpoint, branding, batch sizes — no restart, no file editing. Manage accounts. |
| **Logs** | Filter by level and time, then click a correlation ID to reconstruct every event from one operation. |

---

## How it is built

```
Streamlit UI (ui/pages/)
        │  calls only ↓
Service layer (services/)        ← orchestration, transactions, zero UI imports
        │
        ├── scraper/     Trafilatura → Newspaper4k → BeautifulSoup cascade
        ├── cleaner/     normalise, dedupe, truncate to a token budget
        ├── ai/          LLMProvider (Groq | Hosted | Mock) + versioned prompts
        ├── template/    Jinja2 → table HTML → inlined CSS → plain-text part
        ├── email/       EmailProvider (Brevo | SMTP | Console) + batching + retry
        └── repository/  SQLAlchemy models + Alembic migrations
```

Everything crossing a boundary is a **Pydantic model**, never a raw dict. The layer
rule is enforced by `import-linter` in CI, not by convention — `ui → services →
modules → core`, downward only.

Full detail in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

### Stack

| | |
|---|---|
| UI | Streamlit (multipage, `st.navigation`) |
| LLM | `openai/gpt-oss-120b` — Apache 2.0 open-weight — hosted on Groq |
| Data | SQLite + SQLAlchemy 2.x + Alembic |
| Email | Brevo API, SMTP, or Console (`.eml` files, sends nothing) |
| Config | pydantic-settings + `.env`, with runtime overrides in-app |
| Logs | structlog → JSON lines + rotating file + a queryable SQLite table |
| Tests | pytest + `responses`/`respx` + a fixture-backed mock provider |

---

## The AI

**The model is open-source; only the hosting is rented.** That distinction is the
core constraint of this project, and it is satisfied more cleanly by Groq than by
self-hosting, because Groq serves *only* open-weight models.

Three properties are worth knowing:

1. **No model ever runs on this machine.** Not a preference — a build failure.
   `scripts/check_no_local_inference.py` and an import-linter contract reject
   `torch`, `transformers`, `vllm`, `ollama` and friends in CI.
2. **Malformed JSON is impossible, not unlikely.** Groq's `strict: true` constrains
   generation to the schema at the decoding layer. A repair-retry path exists for
   backends that lack it.
3. **Every campaign records the model and prompt version that produced it**, so any
   output can be reproduced. Prompts are versioned YAML in `prompts/`, never edited
   in place.

**Vays swapping in their own LLM is a two-line change** — `LLM_BASE_URL` and
`LLM_API_KEY`, or the Settings page. Any OpenAI-compatible endpoint works, no code
changes. Walked through in **[docs/SWAP_THE_LLM.md](docs/SWAP_THE_LLM.md)**.

---

## Sending real email

Ships defaulting to `EMAIL_PROVIDER=console`, which writes `.eml` files to
`data/outbox/` and **sends nothing** — a fresh clone cannot accidentally mail
customers.

| Provider | Use it for |
|---|---|
| `console` | Development. Open a `.eml` in a mail client to check rendering. |
| `smtp` | Testing through Gmail or a corporate relay. |
| `brevo` | Real campaigns. 300/day free, custom domain authentication. |

**Gmail SMTP is fine for testing and wrong for campaigns** — ~500 recipients/day, the
From address is locked to the authenticated account, no SPF/DKIM for your own domain,
and Google's terms prohibit bulk marketing mail. Use Brevo for anything real.
See **[docs/RUNBOOK.md](docs/RUNBOOK.md)**.

### Four guards between "click send" and a customer's inbox

Each has a test named after the failure it prevents:

1. **Suppression list** — someone who unsubscribed stays unsubscribed, even if their
   address is in a freshly uploaded CSV.
2. **Double-send guard** — a conditional UPDATE claims the campaign, so a Streamlit
   rerun firing the handler twice matches zero rows the second time.
3. **Retry skips the delivered** — "retry failed only" must not re-mail everyone who
   succeeded.
4. **Render-time compliance** — no unsubscribe link or postal address means no email
   gets built at all.

---

## Development

```powershell
.venv\Scripts\python.exe -m pytest                      # 835 tests
.venv\Scripts\python.exe -m ruff check .                # lint
.venv\Scripts\python.exe -m mypy core config modules services
.venv\Scripts\lint-imports.exe                          # architecture contracts
.venv\Scripts\python.exe scripts\check_no_local_inference.py
.venv\Scripts\python.exe scripts\validate_prompts.py
```

All six run on every push via [GitHub Actions](.github/workflows/ci.yml), on Windows
and Linux, Python 3.11 and 3.12.

**Work offline** with `LLM_PROVIDER=mock` — deterministic JSON fixtures, no network,
no API key, no cost. The whole pipeline runs.

---

## Documentation

| Read this | When |
|---|---|
| **[SETUP_GUIDE](docs/SETUP_GUIDE.md)** | Getting it running on a new machine |
| **[RUNBOOK](docs/RUNBOOK.md)** | Operating it, and fixing it when it breaks |
| **[ARCHITECTURE](docs/ARCHITECTURE.md)** | Changing the code |
| **[SWAP_THE_LLM](docs/SWAP_THE_LLM.md)** | Moving off Groq to your own model |
| **[KNOWN_ISSUES](docs/KNOWN_ISSUES.md)** | Before you report a bug — and before you trust it with a big list |
| **[PROMPT_GUIDE](docs/PROMPT_GUIDE.md)** | Changing what the AI writes |
| **[FINAL_DECISIONS](docs/09_FINAL_DECISIONS.md)** | Asking "why is it like this?" |
| **[ADRs](docs/ADR/)** | Asking "why did this change?" |

Planning documents (PRD, TRD, research, UI spec, milestone plan) are `docs/01`–`08`.

---

## Known limitations

Read **[docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md)** in full before a large send. The
three that will affect you first:

- **Groq's free tier is ~8–12k tokens/minute.** Three long articles can hit it. The
  app honours `Retry-After` and tells you what happened, but generation gets slower.
- **Email-client rendering is verified structurally, not visually.** No Litmus
  account, so the templates are checked by assertion and by opening `.eml` files —
  not across 40 real clients.
- **Single-process deployment.** Login lockout state is in memory; behind two
  workers it would need to move to the database.

---

## Licence &amp; attribution

Internal deliverable for **Vays Infotech**. Every runtime dependency is
permissive-licensed (MIT / BSD / Apache-2.0) — `html2text` was dropped specifically
to avoid GPL-3.0 in the distribution.

Generated newsletters are original summaries that link back to the source article,
with an attribution block in every template.

<div align="center">
<sub>Built by Sidhant · <a href="CLAUDE.md">CLAUDE.md</a> is the running engineering log</sub>
</div>
