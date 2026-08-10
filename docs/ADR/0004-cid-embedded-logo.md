# ADR 0004 — Embed the logo as a CID attachment

**Status:** Accepted · **Date:** 2026-08-08

## Context

The first real Gmail send arrived with **no logo**, and inspection showed it could
never have worked. `resolve_brand()` put `logo_path` — `assets/logo.png`, a
*filesystem* path — straight into `<img src>`. No mail client can resolve a local
path. The `assets/` directory did not exist either, and `minimal.html` had no logo
block at all.

None of the 24 template tests caught it, because every one asserted on the HTML
string. `<img src="assets/logo.png">` is perfectly valid HTML. It is only wrong once
it is somewhere else.

Three options:

| | |
|---|---|
| **Hosted URL** | Best deliverability, but needs a public web server. Vays has one; the developer machine does not, and the handover cannot depend on infrastructure that may not exist |
| **`data:` URI** | Self-contained — and **stripped by both Gmail and Outlook**. Non-starter |
| **CID attachment** | The image travels inside the message as a `multipart/related` part. Renders in every major client |

## Decision

**CID embedding, with the hosted path left open.**

`resolve_logo_url()` returns `cid:vays-logo` for a local file and passes an
`http(s)` URL through unchanged — so Vays can move to hosting later by changing one
setting, with no code change.

`build_mime_body()` in `modules/email/base.py` attaches the bytes as
`multipart/related` **under the HTML alternative**. Attaching at the top level
instead makes it an ordinary attachment and the `cid:` resolves to nothing — the
subtlety that makes this worth an ADR.

Shared by the console and SMTP providers, so the `.eml` preview and the real send
cannot drift.

A missing logo file degrades to the brand name as text. **A cosmetic problem must
never block a send.**

## Consequences

Good:
- The logo renders with no hosting, no CDN, no external request
- Verified byte-identical: sha256 of the source PNG matches the embedded payload
- Recipients with remote images disabled still see it — CID parts are not remote

Bad:
- **Every message carries the image.** A 21 KB logo across 500 recipients is ~10 MB
  of extra traffic. Hosting is better at scale, which is why that path stays open
- Message size roughly triples (13 KB → 43 KB), which marginally affects some spam
  heuristics
- Dark mode is still unsolved: a transparent-background logo can look wrong where a
  client force-inverts. Mitigated with an explicit white plate; not eliminated

Also found and fixed here: `modern.html` was placing the logo on the `#0B5FFF` brand
band, which would have made a dark logo unreadable. Logos now get a white plate with
the brand colour as a rule beneath.
