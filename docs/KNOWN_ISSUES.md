# Known Issues &amp; Limitations

Written to be read *before* you hit these, not after. Everything here is known and
deliberate or known and unfixed — nothing is hidden.

Ordered by how likely you are to meet it.

---

## 1. Will affect you

### 1.1 Groq's free tier is the binding constraint · **open**

Roughly **8–12k tokens per minute**. One article costs ~3,450 tokens end to end, so
three long articles can exceed the ceiling before composition even starts.

**Symptoms:** generation becomes slow; "The AI service is busy"; 4 calls on one large
article measured at ~68 seconds against ~5 seconds for a small one — that gap is
rate limiting, not model speed.

**Mitigations already in place:** input budget capped at 3000 tokens/article,
`Retry-After` honoured, stage-1 concurrency limited to 3, and `LLMRateLimitedError`
distinguishes "busy" from "broken" so the UI can say which.

**If it bites:** two or three articles per campaign, or switch to
`openai/gpt-oss-20b` (Settings → AI Service, no restart), or a paid Groq tier for
roughly 10× the limits.

---

### 1.2 Email rendering is verified structurally, not visually · **open**

The 24 template tests assert *properties of the HTML*: tables not divs, CSS inlined,
`[if mso]` wrapper present, no flexbox or grid, unsubscribe link and postal address
present.

**None of that proves it looks right in Outlook.** Outlook on Windows renders with
Microsoft Word's engine, and things that pass every test above can still break —
tables ignoring `max-width`, padding dropped, the CTA rendering as bare underlined
text, hairline gaps between rows.

Proper verification needs Litmus or Email on Acid (paid) or the actual clients
installed. Neither was available.

**What to do:** set `EMAIL_PROVIDER=console`, send a test, and open the `.eml` from
`data\outbox\` in real Outlook and Gmail before your first campaign. Sample files are
already there. Compare `modern`, `classic` and `minimal` and pick the one that holds
up.

---

### 1.3 Dark mode is unhandled · **open**

Gmail and Outlook dark modes may invert backgrounds. The logo sits on an explicit
white plate to mitigate this, but clients that force-invert can still make a
transparent-background logo look wrong.

**Workaround:** use a logo PNG with a baked-in white background rather than
transparency.

---

## 2. Might affect you

### 2.1 Some sites cannot be scraped · **by design**

Measured live on 8 real OEM articles: **7/8 extracted (88%), all tier-1**. The one
failure was an author-archive page, correctly refused.

Also observed and handled: Fortinet's `robots.txt` disallow, a Microsoft 403, an HPE
timeout. Each produces its intended actionable message.

JavaScript-rendered sites will fail — there is no headless browser, deliberately
(it would add ~300 MB and a browser runtime to a handover).

**Fallback:** extraction failure offers a **paste manually** box, and pasted text
enters the identical downstream pipeline. That is the design, not a workaround.

---

### 2.2 The AI can fabricate details · **mitigated, never eliminated**

An LLM can produce a plausible product name, version number or statistic that does
not exist. This is inherent to the technology.

**Controls:** an extractive summary stage before the generative one; a
`technical_facts` field that forces verifiable details to be enumerated separately
so a fabrication is conspicuous; source URLs shown beside each block; grounding
instructions in the prompt; and **mandatory human review**.

**The review step is the control.** Do not remove it. Check product names and
numbers against the source links before sending.

---

### 2.3 Recipient limits · **by design**

10,000 per campaign, enforced. Beyond that, split the list — the process is
single-threaded and a larger send would run for hours with no checkpoint.

Provider limits are lower and bind first: Brevo free is 300/day, Gmail ~500/day.

---

### 2.4 Only `gpt-oss` models guarantee schema-valid JSON · **by design**

`strict: true` constrained decoding is supported by `openai/gpt-oss-120b`,
`gpt-oss-20b` and `moonshotai/kimi-k2-instruct-0905`. On any other model the app
falls back to a repair-retry, which is slower and occasionally fails.

Settings → AI Service warns when the configured model lacks the capability.

**Also true and worth knowing:** `strict` mode enforces *structure* — keys, types,
enums, array bounds. It does **not** enforce string lengths; `minLength`/`maxLength`
are rejected by the API and stripped from the wire schema. Length limits are carried
by prompt wording, validated by Pydantic, and repaired on retry.

---

## 3. Architectural limits

### 3.1 Single process only · **by design, documented upgrade path**

- **Login lockout state is in memory.** Behind two workers, an attacker would get 5
  attempts per worker. Moving it to the database is a small change, flagged in
  `services/auth_service.py`.
- **SQLite** handles this workload comfortably (WAL mode, one writer). Concurrent
  campaigns from several users would need PostgreSQL — SQLAlchemy keeps that path
  open, and it is a connection-string change plus a migration run.
- **No background workers.** A send blocks the session that started it. A 500-address
  campaign takes a few minutes with the browser tab open.

### 3.2 No separate API · **D-1, deliberate**

The service layer has zero Streamlit imports, so wrapping it in FastAPI later is
additive rather than a rewrite. That was the point of the boundary.

### 3.3 `send_test(brand=...)` half-applies · **open, minor**

`DeliveryService.send_test()` passes a `brand` override to the renderer, but
`_build_message()` calls `resolve_brand()` with no argument and uses the *configured*
brand for the unsubscribe headers. Body honours the override; headers do not.

No production impact — nothing passes `brand`. It is a trap for whoever writes the
next test harness. Fix: thread the override through `_build_message`.

---

## 4. Security notes

| | |
|---|---|
| **Passwords** | bcrypt with SHA-256 pre-hash (72-byte limit). Minimum 10 characters |
| **Sessions** | HMAC-signed tokens, `APP_SECRET_KEY` must be 32+ characters |
| **SSRF** | URLs are DNS-resolved and IP-classified before fetching; redirects re-validated. Covers `inet_aton` legacy forms (`127.1`, decimal, hex) |
| **Template injection** | Sandboxed Jinja + autoescape. Content is escaped *before* our own markdown-to-HTML tags are inserted, so that conversion cannot become an injection hole |
| **Secrets** | `.env` only. The runtime-settings registry refuses `SecretStr` fields at import time — enforced by mechanism, not convention |
| **PII** | Recipient addresses never reach the LLM, are masked in logs, and stay in local SQLite |

### Not implemented

- **No HTTPS.** Streamlit serves plain HTTP. Fine on `localhost`; put it behind a
  reverse proxy with TLS before exposing it on a network.
- **No rate limiting on the login form** beyond the 5-attempt lockout.
- **No CSRF protection** — Streamlit's model makes this largely moot, but it is not
  a hardened public-facing app.
- **No audit trail of who edited what content** — campaigns record who sent, not who
  edited each field.

**Do not expose this to the internet without a TLS-terminating proxy and a review.**
It is designed as an internal tool on a trusted network.

---

## 5. Testing gaps

**835 tests, 90% coverage.** What that number does not cover:

- **`ui/` is excluded from coverage.** The 26 `AppTest` page tests prove every page
  renders and the critical controls behave, but not that it *looks* right.
- **No load testing.** The largest exercised send is a few hundred simulated
  recipients.
- **Live-API tests are not in CI.** CI runs `LLM_PROVIDER=mock`; Groq integration was
  verified manually and the results recorded in `CLAUDE.md`.
- **`pip-audit` is advisory in CI** (`continue-on-error`), so a CVE will not fail the
  build. Read the output rather than trusting the green tick.

---

## 6. Fixed — kept for context

| Was | Resolution |
|---|---|
| Colab hosting: CUDA wheel mismatch, restart loops, session expiry | **Replaced by Groq** (D-21). Cost one class and a config default |
| MJML templates could not be compiled (no Node) | **Hand-authored table HTML** (D-23) |
| Logo never rendered — a filesystem path in `<img src>` | **CID embedding** as `multipart/related` |
| Asterisks in delivered copy | Prompt asked for a "bold heading"; fixed in `v1.1.0` plus markdown-to-HTML conversion as a safety net |
| Copy read as machine-written | `_shared/human_voice.md`; measured 10 asterisks → 0, and a genuinely different register |
| `app_logs`, `settings` tables had no writer | Both wired in M7/M8 |

---

## Reporting something new

1. **Logs** → filter ERROR → click the correlation ID → **Export CSV**
2. Note what you were doing and what you expected
3. Include the Dashboard health state

See [RUNBOOK.md](RUNBOOK.md) § 7.
