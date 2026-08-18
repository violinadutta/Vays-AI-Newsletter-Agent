# Making approval links work outside this PC

The approval email contains a **Review & approve** button. It points at whatever
`AGENT_APP_BASE_URL` says. By default that is `http://localhost:8501`, which on
anyone else's device means *their* computer — so the link does nothing.

This is only about the **link**. Approving always works from the dashboard's
Approvals page, on this machine, with no tunnel at all.

---

## Which option do you need?

| Who approves | Set `AGENT_APP_BASE_URL` to | Tunnel needed |
|---|---|---|
| You, on this PC | `http://localhost:8501` (default) | No |
| Someone on the same office network | `http://<this-pc-ip>:8501` | No |
| Someone anywhere — phone, home, another office | ngrok (below) | **Yes** |

Find this PC's address with `ipconfig` — the IPv4 line, e.g. `192.168.1.40`.

---

## ngrok

### 1. Install

1. Download from **https://ngrok.com/download**
2. Unzip `ngrok.exe` somewhere on your PATH, or into this project folder
3. Create a free account, copy your authtoken from
   **https://dashboard.ngrok.com/get-started/your-authtoken**
4. Register it once:

```powershell
ngrok config add-authtoken YOUR_TOKEN_HERE
```

### 2. Reserve a domain — do not skip this

The free tier gives you **one reserved domain**. Claim it at
**https://dashboard.ngrok.com/domains**. You get something like
`vays-newsletter.ngrok-free.app`.

**Why it matters.** Without a reserved domain, ngrok issues a *new random
hostname every time it restarts*. Approval emails already sitting in someone's
inbox then point at a tunnel that no longer exists — the mail arrives, the button
does nothing, and there is no error anywhere to tell you.

This project has been bitten by exactly that before: a rotating tunnel URL was
one of the reasons Colab was abandoned ([ADR 0001](ADR/0001-groq-replaces-colab.md)).

Once you have it:

```powershell
setx NGROK_DOMAIN vays-newsletter.ngrok-free.app
```

`run_tunnel.bat` picks that up automatically. Then set the address once and leave
it alone:

```ini
AGENT_APP_BASE_URL=https://vays-newsletter.ngrok-free.app
```

### 3. Without a reserved domain

If you have not claimed one, set:

```ini
AGENT_APP_BASE_URL=auto
```

The app then asks the running ngrok agent for its **current** address each time
it composes an approval email, so newly sent links are always right. Links sent
*before* the last restart still break — which is why the reserved domain is
better.

---

## Running it

Three windows, all left open:

```
run.bat          the dashboard        (localhost:8501)
run_tunnel.bat   the public address   (ngrok -> 8501)
run_agent.bat    the automation
```

Start the tunnel **before** the agent, so the first approval email already has a
working address.

Check it: ngrok prints a `Forwarding` line. Open that URL in a browser — you
should see the login screen.

---

## ⚠ Security — read before you leave this running

A tunnel puts your dashboard **on the public internet**. Anyone with the URL
reaches the login page. That changes the weight of a few things:

| | |
|---|---|
| **Passwords** | `admin123` is fine on a laptop and **not fine** facing the internet — it is in every credential-stuffing list. Change it in Settings → Account before opening a tunnel. Minimum 10 characters |
| **Who is behind the door** | Login, bcrypt hashing and a 5-attempt lockout. That is the whole defence |
| **TLS** | ngrok terminates HTTPS, so traffic is encrypted in transit. The app itself still speaks plain HTTP locally |
| **What is exposed** | Campaign content, recipient email addresses, logs, and settings. Treat the URL as sensitive |
| **Not implemented** | No CSRF protection, no IP allow-list, no rate limiting beyond the login lockout |

**Close the tunnel when you are not using it.** `Ctrl+C` in the tunnel window.
The dashboard and agent keep working; only the public address goes away.

For a permanent deployment, the right answer is not a tunnel — it is hosting the
app behind a real reverse proxy with TLS on a Vays-controlled domain, which also
fixes the URL question for good.

---

## When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `ngrok is not installed` | Not on PATH | Put `ngrok.exe` in this folder or on PATH |
| `ERR_NGROK_4018` | No authtoken | `ngrok config add-authtoken ...` |
| Link still says `localhost` | `AGENT_APP_BASE_URL` not updated | Set it, or use `auto` |
| `auto` still gives localhost | Tunnel not running when the email was composed | Start `run_tunnel.bat` first; the log line is `tunnel.not_found` |
| Tunnel opens, page will not load | Dashboard not running | Start `run.bat` — ngrok forwards to 8501, it does not serve anything itself |
| Old emails' links are dead | Ephemeral address rotated | Reserve a domain (§2) |
