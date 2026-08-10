# Runbook

Operating the platform, and fixing it when it misbehaves. For first-time
installation see [SETUP_GUIDE.md](SETUP_GUIDE.md).

---

## 1. Daily operation

### Starting and stopping

```powershell
run.bat                 # start — applies migrations, checks config, serves on :8501
Ctrl+C                  # stop
```

Migrations run on every start and are idempotent, so upgrading is `git pull` then
`run.bat`.

### The first thing to look at

**Dashboard.** It answers "is anything broken right now?" without you needing to
read a log: AI service, database and email provider each show a live status.

A red AI service means generation will fail — fix that before anyone wastes twenty
minutes writing a draft that cannot be composed.

### Running a campaign

1. **Generate** — paste 1–10 OEM blog URLs. Watch the per-URL status chips; a
   failure on one does not block the others.
2. **Preview** — read every field. Regenerate individual fields until the copy is
   right. **Check the facts against the source links shown beside each block.**
3. Upload the recipient CSV. It needs an `email` column; `name` and `company` are
   used for personalisation if present.
4. **Send test** to yourself first. Always.
5. **Send campaign.**

### The one rule

**Nothing sends that a human has not read.** The AI can produce a plausible product
name that does not exist. The review step is the control that catches it, and the
provenance links beside each block are there so checking is quick.

---

## 2. Configuring a real email provider

Ships as `EMAIL_PROVIDER=console`, which writes `.eml` files to `data\outbox\` and
sends nothing.

### Gmail (testing only)

Requires an **App Password** — Google removed plain-password SMTP in 2022.

1. Enable 2-Step Verification: [myaccount.google.com/security](https://myaccount.google.com/security)
2. Create an app password: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Copy the 16 characters, **remove the spaces**.

```ini
EMAIL_PROVIDER=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
SMTP_USE_TLS=true
EMAIL_SENDER_ADDRESS=you@gmail.com
```

`EMAIL_SENDER_ADDRESS` must match `SMTP_USERNAME` — Gmail rewrites the From header to
the authenticated account regardless.

Restart, then **Settings → Email → Test connection**.

> **Do not run real campaigns through Gmail.** ~500 recipients/day, no SPF/DKIM for
> your own domain, From locked to the Gmail account, and Google's terms prohibit
> bulk marketing mail. It is a pipeline test, not a sending platform.

### Brevo (real campaigns)

```ini
EMAIL_PROVIDER=brevo
BREVO_API_KEY=xkeysib-...
EMAIL_SENDER_ADDRESS=newsletter@vaysinfotech.com
```

300 emails/day free, permanently. Authenticate the `vaysinfotech.com` domain in
Brevo's dashboard (SPF, DKIM, DMARC) before the first campaign — without it,
deliverability is poor no matter how good the HTML is.

### Which settings need a restart

| | |
|---|---|
| **No restart** — Settings page, applies live | provider, host, port, username, sender, batch size, brand, model, endpoint, log level |
| **Restart required** — `.env` only | `SMTP_PASSWORD`, `BREVO_API_KEY`, `GROQ_API_KEY`, `APP_SECRET_KEY`, `DATABASE_URL` |

Secrets are `.env`-only by design (D-19) — the settings registry refuses `SecretStr`
fields at import time, so this cannot be loosened by accident.

---

## 3. When something breaks

### Diagnosing anything, in three steps

1. **Logs** page → filter level **ERROR**, set the time range.
2. Click the **correlation ID** beside the error. Every event from that one
   operation appears together — this is the difference between "generation failed
   around 3pm" and knowing which call failed and why.
3. **Export these entries (CSV)** and attach it to a bug report.

The full trail is also in `logs\app.jsonl`, which always records DEBUG regardless of
the console log level — the forensic record is no use if it was quiet when the thing
went wrong.

### AI service problems

| Symptom | Meaning | Action |
|---|---|---|
| `Test connection` fails immediately | Bad or missing key | Check `GROQ_API_KEY`; regenerate at console.groq.com |
| "The AI service is busy" | Rate limited (429) | Wait. The app honours `Retry-After`. Use fewer articles per run |
| Generation hangs then fails | Endpoint unreachable | Check `LLM_BASE_URL`; check your network |
| "couldn't summarise any of these articles" | Every stage-1 call failed | Usually rate limiting. Try one article |
| Repeated failures then instant failures | **Circuit breaker opened** | Deliberate — it stops hammering a dead service. Resets after 60s |

**Rate limiting is the most common problem.** Groq's free tier is roughly 8–12k
tokens per minute; three long articles can exceed it before composition starts. The
input budget is capped at 3000 tokens/article to compensate.

### Email problems

| Symptom | Meaning | Action |
|---|---|---|
| "A postal address is required" | `BRAND_ADDRESS` empty | Settings → Brand. Applies immediately |
| SMTP 535 | Bad credentials | Gmail needs an **App Password**, not your account password |
| Sends stop mid-campaign | Quota exhausted (402) | Campaign records what already went out; retry sends only the rest |
| Some recipients FAILED | Per-recipient rejection | Normal. History → retry failed only |
| Logo missing in received mail | `BRAND_LOGO_PATH` missing or wrong | Put a PNG in `assets\`; a missing file degrades to text, never blocks a send |

**A partial send is never lost.** An account-level failure records
`sent_before_failure`, and retry skips everyone already delivered.

### Database problems

| Symptom | Action |
|---|---|
| `no such table` | `.venv\Scripts\python.exe -m alembic upgrade head` |
| `database is locked` | Another process has it open. Close the other app instance |
| Corrupt after a hard kill | Restore from backup (§4). WAL mode makes this rare |

### The app will not start

The startup error page names the problem and the environment variable behind it.
Configuration is validated before the UI renders, and **every** problem is reported
at once so you can fix the whole file in one pass.

A saved setting that no longer validates is logged and skipped, never fatal —
refusing to boot over a value that can only be corrected in the UI you just
prevented from loading is a trap with no exit.

---

## 4. Backups

Everything that matters is three paths:

| Path | Contains | Back up |
|---|---|---|
| `data\app.db` | Campaigns, recipients, sends, users, settings, logs | **Yes — this is the product** |
| `.env` | Secrets and base configuration | Yes, **securely**. Never into git |
| `assets\` | Logo | Yes |

`logs\`, `data\outbox\` and `data\exports\` are reproducible. `.venv\` is rebuilt
from `requirements.lock.txt`.

```powershell
copy data\app.db "backups\app-%DATE:/=-%.db"
```

SQLite is a single file — copying it while the app is stopped is a complete,
consistent backup. Restore by copying it back.

---

## 5. Routine maintenance

| Task | When | How |
|---|---|---|
| Log pruning | Automatic | Rows older than 90 days are deleted at startup |
| Log file rotation | Automatic | 50 MB cap, 5 files |
| Outbox cleanup | As needed | `data\outbox\*.eml` is safe to delete |
| Dependency updates | Quarterly | `pip-audit -r requirements.lock.txt`, then update and re-run the gates |
| Suppression list | Never delete | Legal record of who unsubscribed |

### Adding a user

**Settings → Users → Add a user** (admin only), or:

```powershell
.venv\Scripts\python.exe scripts\create_user.py
```

Roles: `admin` (everything), `editor` (generate, edit, send), `viewer` (read-only).

### Resetting a forgotten password

```powershell
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from modules.repository.database import init_database; from services.auth_service import AuthService; init_database(); AuthService().set_password('USERNAME', 'a new long passphrase')"
```

Clears any lockout at the same time. Minimum 10 characters.

Five failed logins locks that username for 60 seconds. It is in-process, so a
restart also clears it.

---

## 6. Changing what the AI writes

Prompts are versioned YAML in `prompts/`. **Never edit a published version** — every
campaign records the version that produced it (D-6), and editing in place breaks
reproducibility.

To change the copy: copy `v1.1.0.yaml` to `v1.2.0.yaml`, edit, then

```powershell
.venv\Scripts\python.exe scripts\validate_prompts.py
```

`latest` resolves to the highest version automatically. Details in
[PROMPT_GUIDE.md](PROMPT_GUIDE.md).

---

## 7. Escalation

Before reporting a problem, collect:

1. The **correlation ID** from the Logs page
2. The **CSV export** of the surrounding log entries
3. What you were doing, and what you expected
4. Dashboard health at the time

`logs\app.jsonl` has the complete trail if the Logs page itself is unavailable.
