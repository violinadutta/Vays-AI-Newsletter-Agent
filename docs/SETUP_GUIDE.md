# Setup Guide

**Goal: a fresh Windows machine to a sent test newsletter, in about 20 minutes.**

Written for someone who has never seen this project. If a step assumes knowledge you
do not have, that is a bug in this document — say so.

---

## 0. What you need first

| | |
|---|---|
| **Windows 10 or 11** | The target platform. macOS and Linux work; commands differ slightly (see §9). |
| **Python 3.11, 3.12, 3.13 or 3.14** | 3.12 recommended. Check with `py --version`. |
| **A Groq API key** | Free, no card. [console.groq.com](https://console.groq.com) → API Keys. Takes two minutes. |
| **About 400 MB of disk** | Mostly the virtual environment. |

No GPU. No model downloads. No Docker. No Node.

> **Why Python 3.12?** The project is verified on 3.14.3 and pinned
> `>=3.11,<3.15`. 3.12 is recommended because every dependency ships prebuilt
> wheels for it, so installation cannot fall back to compiling from source.

---

## 1. Get the code

```powershell
git clone <repository-url> vays-newsletter-ai
cd vays-newsletter-ai
```

No repository yet? Copy the project folder — but **delete `.venv`, `data\` and
`logs\`** first. Those are machine-specific and will not work elsewhere.

---

## 2. Create the virtual environment

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

Takes 2–4 minutes. `requirements.lock.txt` pins all 132 packages to exact versions,
so you get a byte-identical install to the one that was tested.

> **Use `.venv\Scripts\python.exe` explicitly** throughout, rather than activating
> the environment. It avoids PowerShell's execution-policy prompt and removes any
> doubt about which Python is running. If you prefer to activate:
> `.venv\Scripts\Activate.ps1`, then plain `python` works.

**If installation fails**, see [KNOWN_ISSUES.md](KNOWN_ISSUES.md) § Installation.

---

## 3. Configure

```powershell
copy .env.example .env
notepad .env
```

Only one value is genuinely required:

```ini
GROQ_API_KEY=gsk_your_key_here
```

Three more you should set before sending anything real:

```ini
APP_SECRET_KEY=<see below>
BRAND_ADDRESS=Your registered postal address, City PIN, Country
UNSUBSCRIBE_BASE_URL=https://vaysinfotech.com/unsubscribe
```

Generate a secret key:

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

**`BRAND_ADDRESS` is legally required** in marketing email and the renderer refuses
to build a message without it. If you skip it now, sending fails later with a clear
message — and you can fix it in Settings → Brand without touching a file.

`.env` is git-ignored and must stay that way. It holds your API key.

### Everything else has a working default

`EMAIL_PROVIDER=console` is the shipped default: it writes `.eml` files to
`data\outbox\` and **sends nothing**. That is deliberate — a fresh clone cannot
accidentally email customers. Switch it when you are ready (§6).

---

## 4. Create the database

```powershell
.venv\Scripts\python.exe -m alembic upgrade head
```

Creates `data\app.db` with nine tables. Idempotent — safe to re-run, and `run.bat`
does it on every start, which is what makes an upgrade a pull-and-run.

---

## 5. Create your login

```powershell
.venv\Scripts\python.exe scripts\create_user.py
```

Prompts for a username and password. The password is never echoed and never stored
— only a bcrypt hash. Minimum 10 characters; a short phrase you can remember beats a
mangled word.

There is no default account. A shipped `admin/admin` is the kind of thing that
survives into production.

---

## 6. Start it

```powershell
run.bat
```

Or directly: `.venv\Scripts\python.exe -m streamlit run app.py`

Opens **http://localhost:8501**. Sign in with the account from §5.

`run.bat` checks the four things that actually go wrong — missing venv, missing
`.env`, unapplied migrations, no user accounts — and tells you what to do about each
instead of showing a traceback.

---

## 7. Prove it works

Do these in order. Each one isolates a different part of the pipeline, so a failure
tells you *where* the problem is.

### 7.1 The AI service

**Settings → AI Service → Test connection.**

Expect: `Connected — openai/gpt-oss-120b (≈500 ms)`.

Fails? Your `GROQ_API_KEY` is wrong or absent. The error names the cause.

### 7.2 Generation

**Generate** → paste one OEM blog URL, e.g.

```
https://www.dell.com/en-us/blog/
```

Pick any article link from an OEM partner blog. Click **Generate newsletter**.

Expect: extraction status chips, then a draft in Preview after ~10–20 seconds.

Some sites block scrapers. Extraction failure offers a **paste manually** box — that
is the designed fallback, not a dead end.

### 7.3 Rendering

**Preview** → check the live email render. Try **Regenerate** on the subject line.

### 7.4 An actual email

With `EMAIL_PROVIDER=console`, use **Send test** with any address. Nothing is sent —
a `.eml` file appears in `data\outbox\`. **Double-click it to open it in Outlook.**

That is a far better rendering check than the in-app preview, because it goes
through the real mail client.

### 7.5 A real send

Only when 7.1–7.4 all pass. See [RUNBOOK.md](RUNBOOK.md) § Configuring a real email
provider. Send to **yourself** first.

---

## 8. Run the test suite

Optional, but it is how you confirm the machine is sound rather than just the app:

```powershell
.venv\Scripts\python.exe -m pytest
```

Expect **835 passed** in about two minutes. The suite never touches the network or
your `.env` — it runs against a mock LLM provider and a temporary database.

---

## 9. macOS and Linux

Everything works; three differences:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
cp .env.example .env
.venv/bin/python -m alembic upgrade head
.venv/bin/python scripts/create_user.py
.venv/bin/python -m streamlit run app.py     # no run.bat
```

Paths use forward slashes and there is no `run.bat`. The application itself is
platform-agnostic — everything goes through `pathlib`.

---

## 10. When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` on start | Wrong Python | Use `.venv\Scripts\python.exe`, not bare `python` |
| `Configuration is invalid: APP_SECRET_KEY ...` | Missing or too short | §3. Must be 32+ characters |
| Login page rejects everything | No account exists | §5 |
| `no such table: campaigns` | Migrations not applied | §4 |
| Test connection fails | Bad API key | Check `GROQ_API_KEY` in `.env`; regenerate at console.groq.com |
| "A postal address is required" on send | `BRAND_ADDRESS` empty | Settings → Brand, applies immediately |
| Generation is slow / 429s | Groq free-tier token ceiling | Fewer articles per run, or upgrade. [KNOWN_ISSUES](KNOWN_ISSUES.md) § C-8 |
| Emails have no logo | `BRAND_LOGO_PATH` points at a missing file | Put a PNG in `assets\` and set the path |

Anything else: **Logs** page, filter to ERROR, click the correlation ID to see every
event from that operation. Then [RUNBOOK.md](RUNBOOK.md).

---

## What to read next

- **[RUNBOOK.md](RUNBOOK.md)** — day-to-day operation, real email providers, backups
- **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)** — limitations, before you trust it with a big list
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — before you change code
- **[SWAP_THE_LLM.md](SWAP_THE_LLM.md)** — moving to your own model
