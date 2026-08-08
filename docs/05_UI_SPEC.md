# UI Specification
### Phase 7 — Screens, components, states and interactions
**Date** 2026-08-05 · **Status** Draft — awaiting approval
**Primary user:** Priya, marketing executive, non-technical (PRD §3)

---

> **⚠ SUPERSEDED IN PART (2026-08-07, D-21).** This document was written when the LLM
> was to be self-hosted on Google Colab. **Colab has been dropped entirely** — it failed
> twice on real hardware and its 3-hour sessions, rotating tunnel URL and ToS conflict
> made it unsuitable regardless. The LLM is now **Groq** (open-weight models over an
> ordinary API). Any mention below of Colab, Cloudflare Tunnel, vLLM, or Qwen3-on-a-T4
> is historical. See `docs/04_LLM_HOSTING.md` for what is actually built.

## 1. Design system

### 1.1 Tokens

```
COLOUR
  brand.primary      #0B5FFF   primary buttons, active nav, links, focus rings
  brand.primaryDark  #0847C4   hover
  surface.page       #F7F8FA   app background
  surface.card       #FFFFFF   cards, panels
  border.default     #E3E6EB
  text.primary       #14181F
  text.secondary     #5A6472
  text.muted         #8A94A3
  status.draft       #6B7280 (grey)     status.ready    #0B5FFF (blue)
  status.sending     #D97706 (amber)    status.sent     #059669 (green)
  status.failed      #DC2626 (red)      status.partial  #D97706 (amber)

SPACING   4 · 8 · 12 · 16 · 24 · 32 · 48    (8px base scale)
RADIUS    6px controls · 10px cards
TYPE      Inter / system-ui
          h1 28/600 · h2 22/600 · h3 17/600 · body 15/400 · caption 13/400 · mono 13
SHADOW    card: 0 1px 3px rgba(20,24,31,.06)
```

### 1.2 Rules
- **One primary (filled) button per screen.** Everything else is secondary (outline) or tertiary (text).
- **Status is never colour-only** — every chip carries its label (accessibility, PRD §12.2).
- Contrast ≥ 4.5:1 on all text.
- Destructive actions are outline-red and always confirmed.
- Advanced/technical controls live inside an `Advanced` expander, collapsed by default.

### 1.3 Global shell

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  SIDEBAR (240px)          │  MAIN CONTENT                                      │
│  ─────────────────────────┼────────────────────────────────────────────────────│
│  [logo] Vays Newsletter   │  ┌──────────────────────────────────────────────┐  │
│                           │  │ ⚠ AI service offline — Settings → Test       │  │
│  ▸ Dashboard              │  └──────────────────────────────────────────────┘  │
│  ▸ Generate Newsletter    │       (global banner: only when degraded)          │
│  ▸ Campaign Preview       │                                                    │
│  ▸ Campaign History       │  Page title                                        │
│  ▸ Settings               │  Page content                                      │
│  ▸ Logs                   │                                                    │
│  ─────────────────────    │                                                    │
│  ● AI service    online   │                                                    │
│  ● Email         online   │                                                    │
│  ─────────────────────    │                                                    │
│  Priya Sharma  · Editor   │                                                    │
│  [Sign out]               │                                                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

The two health dots in the sidebar are permanent. The most common real-world failure is a dead
Colab tunnel, and the user must be able to see that **before** investing effort in a draft, not
after clicking Generate.

**Nav guard:** *Campaign Preview* is disabled (greyed, tooltip "Generate a newsletter first")
when no draft is loaded. Dead ends are worse than missing options.

---

## 2. Screen 0 — Login

```
                    ┌─────────────────────────────────┐
                    │          [ logo ]               │
                    │   Vays Newsletter Platform      │
                    │                                 │
                    │   Username  [________________]  │
                    │   Password  [________________]  │
                    │                                 │
                    │   [        Sign in         ]    │
                    │                                 │
                    │   ⓘ Contact IT for access       │
                    └─────────────────────────────────┘
```

| Aspect | Spec |
|---|---|
| Validation | Both fields required; inline "Required" on blur |
| Loading | Button → spinner + "Signing in…", form disabled |
| Error | "Incorrect username or password." — never reveals which. Generic by design |
| Lockout | 5 failures → 60 s cooldown with a countdown |
| Success | Redirect to Dashboard; username + role in session state |
| Security | No campaign data renders until authenticated (NFR-S5) |

---

## 3. Screen 1 — Dashboard

**Purpose:** answer three questions in under 60 seconds — *what does this do, what do I do first,
is anything broken?* (PRD §12.3)

```
Dashboard                                                    [+ New Newsletter]

┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│ Campaigns   │ │ Emails sent │ │ Success rate│ │ Avg time to │
│ this month  │ │ this month  │ │             │ │ send-ready  │
│     12      │ │   4,820     │ │   98.2%     │ │   18 min    │
│  ▲ 4 vs last│ │ ▲ 1.2k      │ │  ▲ 0.4pp    │ │  ▼ 3 min    │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘

┌── Recent campaigns ──────────────────────────┐ ┌── System health ───────────┐
│ Dell PowerEdge Refresh    ● SENT    2 Aug    │ │ ● AI service      online   │
│ Cisco Security Digest     ● SENT    28 Jul   │ │   Qwen3-14B-AWQ            │
│ Fortinet Q3 Update        ● DRAFT   27 Jul   │ │   checked 30s ago          │
│ HP Workstation Launch     ● FAILED  22 Jul   │ │                            │
│                                              │ │ ● Email (Brevo)   online   │
│                          [View all history →]│ │   248 / 300 today          │
└──────────────────────────────────────────────┘ │                            │
                                                 │ ● Database        healthy  │
┌── Getting started ───────────────────────────┐ │   1,204 campaigns          │
│ 1 Paste OEM blog URLs                        │ │                            │
│ 2 Review the AI draft and edit               │ │   [Run health check]       │
│ 3 Upload recipients and send                 │ └────────────────────────────┘
│                        [Start a newsletter →]│
└──────────────────────────────────────────────┘
```

| Aspect | Spec |
|---|---|
| Components | 4 metric cards · recent campaigns table (5 rows) · health panel · getting-started card |
| Interactions | Campaign row → History detail. `+ New Newsletter` → Generate. `Run health check` → re-probe both providers |
| Loading | Skeleton shimmer per card; health dots grey + "checking…" |
| Empty state | Metrics show `—`; recent campaigns replaced by an illustrated empty card: *"No campaigns yet. Your first newsletter takes about 15 minutes."* + primary CTA. The getting-started card is **pinned** until the first campaign is sent |
| Error state | A metric that fails to load shows `—` with a tooltip; one failing widget never blanks the page |
| Health degraded | Red dot + reason; global banner appears; `+ New Newsletter` stays enabled (extraction still works without the LLM) |
| Refresh | Health cached 30 s (Streamlit reruns constantly — an uncached probe would flood the endpoint) |

---

## 4. Screen 2 — Generate Newsletter

The core screen. A 3-step vertical flow — deliberately not tabs, because the steps are sequential
and tabs invite users to skip ahead into an invalid state.

```
Generate Newsletter                                     Step 1 of 3 ●○○

┌── 1 · Source articles ───────────────────────────────────────────────────┐
│  Paste OEM blog URLs, one per line (max 10)                              │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ https://www.dell.com/en-us/blog/poweredge-r7xx-launch              │  │
│  │ https://blogs.cisco.com/security/q3-threat-landscape               │  │
│  │                                                                    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│  2 URLs · max 10                       [Paste text manually] [Extract →] │
└──────────────────────────────────────────────────────────────────────────┘

┌── Extracted articles ────────────────────────────────────────────────────┐
│ ✓ Dell PowerEdge R7xx Launch          1,240 words · trafilatura   [▾][✕] │
│ ✓ Cisco Q3 Threat Landscape             890 words · newspaper4k   [▾][✕] │
│ ✕ blogs.fortinet.com/...  Couldn't read this page   [Paste manually][✕]  │
│                                                                          │
│   ▾ expanded: Title · Author · Published · Word count · first 500 chars  │
└──────────────────────────────────────────────────────────────────────────┘

┌── 2 · Style ─────────────────────────────────────────────────────────────┐
│  Tone      [Professional ▾]   Length   [Medium (~300w) ▾]                │
│  Audience  [Enterprise IT ▾]  Template [Modern ▾]                        │
│                                                                          │
│  ▸ Advanced   (temperature · max tokens · prompt version · model)        │
└──────────────────────────────────────────────────────────────────────────┘

┌── 3 · Generate ──────────────────────────────────────────────────────────┐
│  ● AI service online · Qwen3-14B-AWQ         [  Generate newsletter  ]   │
│  Usually takes 60–90 seconds for 2 articles                              │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Interactions

| Action | Behaviour |
|---|---|
| Type URLs | Live count; >10 → soft warning, Extract disabled |
| `Extract →` | Per-URL status chips: `Queued → Fetching → Extracting → ✓ / ✕`. Bounded concurrency (4). Independent failures (FR-1.8) |
| `[▾]` | Expand extracted preview — this is the trust mechanism; users must confirm the right article was read (US-A2) |
| `[✕]` | Remove that article from the batch |
| `Paste manually` | Modal: Title + Text + optional source URL → enters the identical pipeline (FR-1.7) |
| Style selectors | Persist as user preferences across sessions |
| `Generate` | Disabled until ≥1 article extracted **and** LLM healthy. Tooltip explains which condition is unmet |

### 4.2 States

**Loading — extraction**
Per-row spinners with live status text. The Extract button becomes `Extracting… (2/3)`. Rows
resolve independently, so the user sees progress rather than a single frozen control.

**Loading — generation** (the long one: 60–90 s; must never look hung)
```
┌──────────────────────────────────────────────────────────────┐
│  Generating your newsletter…                                 │
│  ████████████████████░░░░░░░░░░░░░░  60%                     │
│                                                              │
│  ✓ Reading articles           2.1s                           │
│  ✓ Summarising article 1     14.3s                           │
│  ⟳ Summarising article 2     ...                             │
│  ○ Writing the newsletter                                    │
│  ○ Creating subject & CTA                                    │
│                                                              │
│  Elapsed 31s · usually 60–90s                [Cancel]        │
└──────────────────────────────────────────────────────────────┘
```
Named stages with elapsed times, not a bare spinner. If the user knows stage 2 of 4 is running at
31 s, a 90-second wait is tolerable; an anonymous spinner at 31 s reads as broken.

**Validation errors** — inline, per URL:
`✕ not-a-url` → *"This doesn't look like a web address. URLs start with https://"*
`✕ https://localhost:8000/x` → *"Only public web addresses are allowed."* (SSRF guard, S-2)
Duplicates are silently de-duplicated with a caption: *"1 duplicate removed."*

**Error — all extractions failed**
```
⚠ Couldn't read any of the articles

The sites may block automated readers, or the pages may load content with JavaScript.

  • Open the article, select all, and use [Paste text manually]
  • Or check the URLs are correct and try again
                                              [Paste text manually] [Try again]
```

**Error — LLM unavailable** → the recovery panel from Colab doc §6.2, with articles preserved.

**Error — partial success** → *"2 of 3 articles extracted. You can continue with 2, or add the
missing one manually."* Never blocks on a partial failure.

---

## 5. Screen 3 — Campaign Preview

Two-column: edit on the left, live rendered preview on the right. Editing without seeing the result
is where formatting mistakes come from.

```
Campaign Preview · Dell PowerEdge Refresh          ● DRAFT   [Save draft] [Discard]

┌── EDIT ──────────────────────────┐ ┌── PREVIEW ─────────────────────────────┐
│                                  │ │  [Desktop] [Mobile]        [⟳ Refresh] │
│ Campaign name                    │ │ ┌────────────────────────────────────┐ │
│ [Dell PowerEdge Refresh_______]  │ │ │  [ VAYS LOGO ]                     │ │
│                                  │ │ │                                    │ │
│ Subject line          48/60  ⟳ ✎ │ │ │  Dell's New PowerEdge Servers:     │ │
│ [Dell's new servers cut power…]  │ │ │  What IT Leaders Need to Know      │ │
│ ▂▂▂▂▂▂▂▂▂▂▂▂▂▂░░░░ good length   │ │ │                                    │ │
│                                  │ │ │  Dell has announced the R7xx…      │ │
│ Preview text          72/100  ⟳  │ │ │                                    │ │
│ [Plus: Cisco's Q3 threat data…]  │ │ │  ── Cisco Q3 Threat Landscape ──   │ │
│                                  │ │ │  Cisco's quarterly report…         │ │
│ Title                        ⟳   │ │ │                                    │ │
│ [Dell's New PowerEdge Servers…]  │ │ │      [  Read the full story  ]     │ │
│                                  │ │ │                                    │ │
│ Executive summary            ⟳   │ │ │  Vays Infotech · Unsubscribe       │ │
│ ┌──────────────────────────────┐ │ │ └────────────────────────────────────┘ │
│ │ Dell announced the R7xx…     │ │ │                                        │
│ └──────────────────────────────┘ │ │  Template [Modern ▾]                   │
│                                  │ │  ⓘ Rendered as recipients will see it  │
│ Newsletter body    312 words ⟳ ↺ │ └────────────────────────────────────────┘
│ ┌──────────────────────────────┐ │
│ │ (large editor)               │ │ ┌── Sources ─────────────────────────────┐
│ │                              │ │ │ 1 dell.com/…/poweredge-r7xx  ↗         │
│ └──────────────────────────────┘ │ │ 2 blogs.cisco.com/…/q3       ↗         │
│                                  │ │ ⚠ Verify all product names, versions,  │
│ CTA text        CTA URL          │ │   dates and figures before sending.    │
│ [Read more___] [https://…____]   │ └────────────────────────────────────────┘
│                                  │
│ Keywords  [poweredge ×][cisco ×] │
│ Category  [Product Launch ▾]     │
│ ▸ AI original vs your edits      │
└──────────────────────────────────┘

──────────────────────────────────────────────────────────────────────────────
┌── Recipients ────────────────────────────────────────────────────────────┐
│  [ Drop a CSV here or browse ]   Columns: email (required), name, company│
│                                                                          │
│  ✓ recipients_q3.csv                                    [Download sample]│
│  487 valid · 11 invalid · 2 duplicates · 3 suppressed   [View details ▾] │
└──────────────────────────────────────────────────────────────────────────┘

┌── Send ──────────────────────────────────────────────────────────────────┐
│  Test to [priya@vays.com___] [Send test]                                 │
│                                    [ Send campaign to 487 recipients ]   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Field behaviour

| Control | Behaviour |
|---|---|
| `⟳` per field | Regenerate **only this field** (FR-3.8). Inline spinner on that field; every other field and edit untouched. Subject shows 3 variants to pick from |
| `↺` per field | Revert to the AI original (FR-4.4). Only visible once the field has been edited |
| Character counters | Subject ≤60, preview ≤100. Grey → amber at 90% → red past limit. **Over limit still allows sending**, with a warning — an inbox-truncation risk is the user's call, not a hard block |
| Autosave | Debounced 2 s → `Saved 14:32` caption. Streamlit reruns constantly; explicit save-only would lose work |
| `AI original vs your edits` | Expander with a side-by-side diff (FR-4.6) |
| Sources panel | Always visible. Provenance is the trust mechanism; the fact-check warning is deliberate friction against hallucination risk R-3 |
| Preview | Rendered in a sandboxed iframe (security S-4); desktop 600px / mobile 375px |

### 5.2 Recipients

| Aspect | Spec |
|---|---|
| Upload | CSV; header auto-detected; `email` required, `name`/`company` optional; extras become merge fields |
| Validation summary | Four counts, each expandable to a row-level table with reasons |
| Invalid rows | Listed with row number and reason; **skipped, never fatal** (FR-6.2) |
| Suppressed | Shown separately — the user should know unsubscribed contacts were excluded, not silently dropped |
| Errors | Missing `email` column → *"Your CSV needs a column named `email`. Found: name, company, phone."* with a sample download |

### 5.3 Send confirmation

```
┌── Send this campaign? ────────────────────────────┐
│  487 recipients                                   │
│  Subject: Dell's new servers cut power costs…     │
│  From:    Vays Infotech <newsletter@vays…>        │
│  Template: Modern                                 │
│                                                   │
│  This cannot be undone.                           │
│  ☐ I have checked the facts and links             │
│                                                   │
│              [Cancel]   [Send now]                │
└───────────────────────────────────────────────────┘
```
`Cancel` holds default focus. `Send now` is disabled until the checkbox is ticked. The checkbox is
intentional friction — one careless send to 487 customers costs more than every second this
dialog will ever add up to.

### 5.4 Sending state

```
Sending campaign…      ████████████░░░░░░  312 / 487
✓ 310 sent   ✕ 2 failed   ⏱ ~1 min remaining      [Stop after this batch]
```
Live per-batch updates. Navigation away is blocked with a warning. `Stop` completes the current
batch and marks the campaign `PARTIAL_FAILURE` — a clean halt, never a torn state.

### 5.5 Result

```
✓ Campaign sent
485 of 487 delivered · 2 failed · 3m 42s

┌── Failed (2) ──────────────────────────────────────┐
│ j.doe@olddomain.com   Mailbox does not exist       │
│ x@blocked.example     Recipient rejected           │
│                       [Retry failed] [Export CSV]  │
└────────────────────────────────────────────────────┘
                          [View in history] [New newsletter]
```

---

## 6. Screen 4 — Campaign History

```
Campaign History                                              [+ New Newsletter]

[Search title…______] [Status: All ▾] [Date: Last 30 days ▾]   [Export CSV]

┌──────────────────────────────────────────────────────────────────────────────┐
│ Campaign                 Status    Sent        Recipients  Success   Actions │
├──────────────────────────────────────────────────────────────────────────────┤
│ Dell PowerEdge Refresh   ● SENT    2 Aug 14:32    487       99.6%   [👁][⧉]  │
│ Cisco Security Digest    ● SENT    28 Jul 09:15   512      100.0%   [👁][⧉]  │
│ Fortinet Q3 Update       ● DRAFT   27 Jul 16:40     —          —    [✎][🗑]  │
│ HP Workstation Launch    ● FAILED  22 Jul 11:02   340       12.1%   [👁][⧉]  │
└──────────────────────────────────────────────────────────────────────────────┘
                                            ◀ 1 2 3 ▶   Showing 1–20 of 1,204
```

| Aspect | Spec |
|---|---|
| Actions | 👁 view · ⧉ duplicate as new draft (FR-7.4) · ✎ resume draft · 🗑 delete (drafts only, confirmed) |
| Sorting | Any column; default newest first |
| Search | Debounced 300 ms on title |
| Loading | Skeleton rows; ≤2 s with 1,000 campaigns (NFR-P4) |
| Empty | *"No campaigns match these filters."* + `Clear filters`. Distinct from the never-sent-anything empty state |

**Detail view** (drill-down): read-only content · rendered HTML · source URLs · **provenance card**
(model, prompt version, tone/length/audience, generation time — US-B4) · delivery stats ·
per-recipient outcome table · `Duplicate as new draft`.

---

## 7. Screen 5 — Settings

Tabbed, because these are unrelated concerns and a single long form would bury the one setting
Priya actually needs (the AI endpoint URL).

```
Settings

[ AI Service ] [ Email ] [ Branding ] [ Defaults ] [ Users ]

┌── AI Service ────────────────────────────────────────────────────────────┐
│  Provider    [Colab (development) ▾]                                     │
│  Endpoint    [https://ab12cd.trycloudflare.com_____________]             │
│  API key     [••••••••••••••••4f2a]                        [Change]      │
│  Model       [Qwen/Qwen3-14B-AWQ__________________]                      │
│                                                                          │
│  ● Online · responded in 240ms · checked 12s ago     [Test connection]   │
│                                                                          │
│  ⓘ Colab sessions expire after ~3 hours. When that happens, re-run the   │
│    notebook and paste the new URL and key here.        [Open the guide]  │
│                                                                          │
│  ▸ Advanced   temperature 0.7 · max tokens 2048 · timeout 120s ·         │
│               retries 3 · circuit breaker 3/60s                          │
└──────────────────────────────────────────────────────────────────────────┘
```

| Tab | Contents |
|---|---|
| **AI Service** | as above — the most-used tab, therefore first |
| **Email** | provider · sender name/address · reply-to · batch size · delay · `Test connection` (sends a real test) · quota usage |
| **Branding** | logo upload with preview · primary colour picker · website · **physical address (required — legally mandatory in every email)** · footer text · unsubscribe URL |
| **Defaults** | default tone / length / audience / template · max URLs · min word count |
| **Users** | list, add, deactivate, reset password, role (admin only; hidden otherwise) |

| Aspect | Spec |
|---|---|
| Secrets | Always masked after save (`••••4f2a`); `Change` clears the field for re-entry; never logged (NFR-S1/S5) |
| Validation | URL format; port range; temperature 0–2; colour hex; email format. Inline, on blur |
| Save | Explicit `Save changes` per tab; unsaved-changes warning on navigation |
| Test connection | Spinner → ✓ green with latency, or ✕ red with the specific reason (`401 — the API key doesn't match the notebook`) |
| Errors | Never *"connection failed"*. Always the reason and the fix |

---

## 8. Screen 6 — Logs

Non-technical users need this too — it is how Priya gets a reference code to a developer.

```
Logs

[Level: All ▾] [Time: Last 24h ▾] [Search…______] [Campaign: All ▾]  [Export]

┌─────────────────────────────────────────────────────────────────────────────┐
│ Time      Lvl   Event               Message                    Campaign     │
├─────────────────────────────────────────────────────────────────────────────┤
│ 14:32:11  INFO  campaign.sent       485/487 delivered in 3m42s    #1204  [▾]│
│ 14:28:44  WARN  email.retry         Retry 1 for recipient batch 7 #1204  [▾]│
│ 14:22:03  INFO  llm.request         newsletter_compose · 3.4k tok #1204  [▾]│
│ 14:21:58  ERROR llm.failed          ConnectionError · circuit OPEN #1204 [▾]│
│ 14:20:12  WARN  extractor.fallback  trafilatura → newspaper4k     #1204  [▾]│
└─────────────────────────────────────────────────────────────────────────────┘
                                                    ◀ 1 2 3 ▶   2,481 entries
```

| Aspect | Spec |
|---|---|
| Row expand `[▾]` | Full JSON context, correlation ID (copyable), stack trace if present |
| Colour | ERROR red left border · WARN amber · INFO default |
| Filters | Level (multi) · time range · free-text (event + message) · campaign |
| Export | CSV of the current filter — the artifact a user attaches to a bug report |
| Correlation | Clicking a correlation ID filters to that entire operation. This is the feature that turns "it broke" into a diagnosis |
| Retention | 90 days in the DB; a caption says so, and points to `logs/app.jsonl` for the full trail |
| Empty | *"No log entries match these filters."* |
| Security | Secrets are already redacted upstream; recipient addresses masked |

---

## 9. Cross-cutting state matrix

Every long-running or failable interaction must define all five. Missing states are where internal
tools feel broken.

| Interaction | Loading | Success | Empty | Error | Disabled |
|---|---|---|---|---|---|
| Extract URLs | per-row chips + count | ✓ + word count + extractor | — | per-row reason + manual fallback | no URLs entered |
| Generate | staged progress + elapsed | redirect to Preview | — | recovery panel, articles preserved | no articles / LLM down |
| Regenerate field | inline field spinner | field updates, `↺` appears | — | toast, old value retained | LLM down |
| Render preview | skeleton iframe | rendered HTML | — | "Couldn't render — try another template" | no content |
| Upload CSV | parsing spinner | 4-count summary | "CSV has no rows" | column/format error + sample | — |
| Send test | button spinner | ✓ "Sent to x@y" | — | provider reason | no address / not rendered |
| Send campaign | live progress bar | result + failure table | — | partial-failure table + retry | not confirmed / no recipients |
| Load history | skeleton rows | table | "No campaigns yet" | "Couldn't load — retry" | — |
| Test connection | button spinner | ✓ + latency | — | specific reason + fix | fields empty |

---

## 10. Accessibility & responsiveness

- Keyboard: full tab order; `Enter` submits forms; `Esc` closes modals; visible focus ring (brand primary, 2px).
- Screen readers: all inputs labelled; status changes in `aria-live` regions; icon-only buttons carry `aria-label`.
- Colour independence: status = chip + label; validation = icon + text, never colour alone.
- Contrast: ≥4.5:1 body, ≥3:1 large text — verified with a contrast checker, not by eye.
- Responsive: designed for ≥1280px (desktop tool). At <1024px the Preview two-column stacks to
  single column with a tab switcher. Tables scroll horizontally within their container; **the page
  body never scrolls horizontally.**
- Motion: progress animations respect `prefers-reduced-motion`.

---

## 11. Implementation notes for Streamlit

| Concern | Approach |
|---|---|
| Session state | All keys declared in `ui/state.py` with typed accessors — never raw string keys scattered through pages. Prevents the classic "typo creates a silent new key" bug |
| Caching | `@st.cache_data` **only** for non-user-specific data (templates, prompt files, health probe). **Never cache campaign or recipient data** — cache is shared across sessions and would leak between users (research §3.7) |
| Rerun safety | Every mutating action guarded by an explicit button + a DB-level state check. No side effects at module scope |
| Long operations | `st.status()` with stage updates for extraction/generation; `st.progress()` for sends |
| Custom CSS | Single `ui/styles.py` injecting one `<style>` block. No per-page CSS |
| Components | `ui/components/`: `status_chip`, `metric_card`, `url_input`, `article_card`, `editable_field`, `health_indicator`, `confirm_dialog`, `empty_state`, `error_panel`. Each used ≥2 places — a component library of one-offs is just indirection |
| Preview isolation | `st.components.v1.html(..., scrolling=True)` renders the email in a sandboxed iframe |
| Navigation | `st.navigation` / `st.Page` with the auth guard applied once in `app.py`, not repeated per page |
