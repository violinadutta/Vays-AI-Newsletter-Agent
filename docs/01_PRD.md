# Product Requirements Document (PRD)
### AI Newsletter Generation & Distribution Platform
**Version** 1.0 · **Date** 2026-08-05  · **Status** Draft — awaiting approval

---

> **⚠ SUPERSEDED IN PART (2026-08-07, D-21).** This document was written when the LLM
> was to be self-hosted on Google Colab. **Colab has been dropped entirely** — it failed
> twice on real hardware and its 3-hour sessions, rotating tunnel URL and ToS conflict
> made it unsuitable regardless. The LLM is now **Groq** (open-weight models over an
> ordinary API). Any mention below of Colab, Cloudflare Tunnel, vLLM, or Qwen3-on-a-T4
> is historical. See `docs/04_LLM_HOSTING.md` for what is actually built.

## 1. Problem Statement

Vays Infotech's marketing team produces customer-facing newsletters from OEM partner blog
content (vendors such as Dell, HP, Cisco, Fortinet, Microsoft, etc.). Today the workflow is
entirely manual:

1. A marketing executive monitors 5–15 OEM blogs.
2. They read each new article end to end.
3. They hand-write a summary, then rewrite it in the company's voice.
4. They compose a subject line, preview text, and CTA from scratch.
5. They paste the copy into an email tool and fight with HTML formatting.
6. They build the recipient list and send.
7. Records of what was sent, to whom, and from which source live in scattered spreadsheets.

### Quantified pain

| Symptom | Current state | Consequence |
|---|---|---|
| Time cost | ~90–150 min per newsletter, per executive | Only 2–4 campaigns/month are realistic |
| Inconsistency | Tone/structure vary by author and by mood | Brand voice dilutes; quality is unpredictable |
| Scale ceiling | Adding an OEM partner adds linear human effort | Partner coverage stays shallow |
| Generic output | Under time pressure, copy becomes a rewritten press release | Low open/click rates |
| Campaign latency | 3–7 days from blog publication to send | Content is stale; first-mover advantage lost |
| No memory | No searchable archive of past campaigns | Duplicate coverage; no learning loop |

### The core insight

The work splits cleanly into **judgment** (which stories matter, is this claim accurate, is this
on-brand) and **mechanical transformation** (read → summarize → rewrite → format → send → log).
Roughly 80% of the elapsed time is mechanical. **This product automates the mechanical 80% and
keeps a human firmly in control of the judgment 20%.**

### Explicit non-goal

This is **not** an "AI sends emails by itself" product. Every campaign passes through a human
approval gate. Removing that gate is out of scope for v1 and is a deliberate design position,
not a limitation to be fixed later (see §9, A-4).

---

## 2. Business Goals

| ID | Goal | Metric | Target (90 days post-launch) |
|---|---|---|---|
| BG-1 | Cut newsletter production time | Minutes from URL paste to send-ready | ≤ 20 min (from ~120 min) — **83% reduction** |
| BG-2 | Increase campaign throughput | Campaigns sent per month | 3 → 12 |
| BG-3 | Enforce brand consistency | % of sends using an approved tone preset | ≥ 95% |
| BG-4 | Reduce content latency | Hours from OEM publication to send | ≤ 48 h (from 3–7 days) |
| BG-5 | Create an institutional archive | % of sends with a queryable record | 100% |
| BG-6 | Zero recurring AI licence cost | Monthly spend on model inference | ₹0 during development; open-weight model at all times |
| BG-7 | Deliver a handover-ready asset | Time for a new dev to run it locally | ≤ 30 min from README |

---

## 3. User Personas

### P1 — Priya, Marketing Executive *(primary user, ~80% of sessions)*
- **Role:** Executes campaigns day to day.
- **Technical level:** Comfortable with SaaS tools; not a developer. Has never seen a terminal.
- **Goals:** Ship a good newsletter today, not next week. Keep the copy on-brand. Not embarrass
  the company with an error in a customer-facing email.
- **Frustrations:** Staring at a blank page. Wrestling with HTML. Re-typing the same CTA.
- **What success looks like:** Paste 3 URLs, get a solid draft in 2 minutes, spend 10 minutes
  making it genuinely good, send with confidence.
- **Design implication:** The UI must never expose a stack trace, a token count, or the word
  "temperature". Errors must be phrased as actions ("The AI service is offline — check Settings").

### P2 — Rahul, Marketing Manager *(reviewer / approver, ~15% of sessions)*
- **Role:** Owns brand voice and campaign strategy; approves before send.
- **Technical level:** Non-technical.
- **Goals:** See what went out, to whom, and how it performed. Catch off-brand copy before it ships.
- **Frustrations:** No visibility until after the fact. No audit trail.
- **Design implication:** Campaign History and a readable preview are first-class features, not
  afterthoughts. Needs side-by-side "AI original vs human-edited" visibility.

### P3 — Sidhant, Developer / Maintainer *(~5% of sessions, 100% of the pain)*
- **Role:** Builds and operates the system; hands it over at internship end.
- **Technical level:** Python developer.
- **Goals:** Debug a failure in minutes. Swap the LLM host without touching business logic.
  Hand over cleanly.
- **Frustrations:** Colab sessions dying mid-demo. Silent failures. Undocumented magic.
- **Design implication:** Structured logs, a Logs page, a health check, a provider abstraction,
  and a README that assumes zero context.

### P4 — Neha, Recipient *(never opens the app, judges the output)*
- **Role:** Customer/prospect receiving the newsletter.
- **Design implication:** Output must render correctly in Outlook, Gmail, and on mobile; must have
  a working unsubscribe; must not read like machine output.

---

## 4. Functional Requirements

Priority: **M** = Must (v1), **S** = Should (v1 if time), **C** = Could (v2).

### 4.1 Input & Ingestion

| ID | Priority | Requirement |
|---|---|---|
| FR-1.1 | M | Accept 1–10 blog URLs in a single input (newline or comma separated) |
| FR-1.2 | M | Validate each URL syntactically before processing; reject malformed input with a per-URL message |
| FR-1.3 | M | De-duplicate identical URLs within a submission |
| FR-1.4 | M | Fetch each URL with a timeout, a descriptive User-Agent, and bounded retries |
| FR-1.5 | M | Extract main article content, title, author, and publication date, discarding nav/ads/footers/comments |
| FR-1.6 | M | Fall back to a secondary extractor if the primary returns too little text (< 200 words) |
| FR-1.7 | M | Offer a **manual paste** mode when all extractors fail or the site blocks access |
| FR-1.8 | M | Process URLs independently — one failure must not abort the batch |
| FR-1.9 | S | Respect `robots.txt` and rate-limit requests per domain |
| FR-1.10 | C | Ingest an RSS/Atom feed and list recent articles for selection |

### 4.2 Cleaning & Preparation

| ID | Priority | Requirement |
|---|---|---|
| FR-2.1 | M | Normalize whitespace, Unicode (NFKC), smart quotes, and HTML entities |
| FR-2.2 | M | Strip boilerplate (share prompts, "read more", cookie notices, author bios) |
| FR-2.3 | M | Detect language; warn if not English |
| FR-2.4 | M | Enforce a token budget by intelligent truncation (keep lead + headings + tail), never a blind cut |
| FR-2.5 | M | Report per-article word count and estimated token count to the user |
| FR-2.6 | S | Flag articles that appear to be pure marketing/press release with little substance |

### 4.3 AI Generation

| ID | Priority | Requirement |
|---|---|---|
| FR-3.1 | M | Generate, per submission: `title`, `summary`, `newsletter`, `subject`, `preview_text`, `cta`, `keywords[]`, `category`, `tone` |
| FR-3.2 | M | Return output as JSON validated against a fixed schema; reject and retry on violation |
| FR-3.3 | M | Support user-selectable **tone presets** (Professional, Friendly, Technical, Executive, Enthusiastic) |
| FR-3.4 | M | Support user-selectable **length presets** (Short ≈150w, Medium ≈300w, Long ≈500w) |
| FR-3.5 | M | Support user-selectable **audience** (Enterprise IT, SMB, Channel Partner, C-Suite) |
| FR-3.6 | M | Handle multiple articles: summarize each, then compose one cohesive multi-story newsletter |
| FR-3.7 | M | Stream or show meaningful progress; generation must never look frozen |
| FR-3.8 | M | Allow regeneration of a **single field** without regenerating everything |
| FR-3.9 | M | Record the model name, prompt version, and generation parameters with every result |
| FR-3.10 | S | Generate 2–3 subject line variants for the user to pick from |
| FR-3.11 | S | Attach each newsletter section to its source URL for provenance |
| FR-3.12 | C | A/B subject line testing with split sends |

### 4.4 Review & Editing

| ID | Priority | Requirement |
|---|---|---|
| FR-4.1 | M | Every AI-generated field must be editable in the UI |
| FR-4.2 | M | Persist edits across page navigation within a session |
| FR-4.3 | M | Show live character counts with limits for `subject` (≤ 60) and `preview_text` (≤ 100) |
| FR-4.4 | M | Provide "revert to AI original" per field |
| FR-4.5 | M | Save a draft and resume it later |
| FR-4.6 | S | Show a diff between the AI original and the human-edited version |

### 4.5 Rendering

| ID | Priority | Requirement |
|---|---|---|
| FR-5.1 | M | Render the approved content into a responsive HTML email |
| FR-5.2 | M | Offer at least 2 selectable templates |
| FR-5.3 | M | Support logo, brand colour, footer, and company address configuration |
| FR-5.4 | M | Auto-generate a plain-text alternative part |
| FR-5.5 | M | Preview in-app at desktop and mobile widths |
| FR-5.6 | M | Include a functioning unsubscribe link and physical address (CAN-SPAM/GDPR hygiene) |
| FR-5.7 | M | Download the rendered `.html` file |
| FR-5.8 | S | Send a test email to a single address before the real campaign |

### 4.6 Distribution

| ID | Priority | Requirement |
|---|---|---|
| FR-6.1 | M | Upload a recipient list via CSV (`email`, optional `name`, `company`) |
| FR-6.2 | M | Validate email syntax; report and skip invalid rows without aborting |
| FR-6.3 | M | De-duplicate recipients and honour a suppression/unsubscribe list |
| FR-6.4 | M | Send in batches with configurable size and inter-batch delay |
| FR-6.5 | M | Show live send progress (sent / failed / remaining) |
| FR-6.6 | M | Retry transient failures with exponential backoff; record permanent failures |
| FR-6.7 | M | Require an explicit confirmation step showing recipient count before sending |
| FR-6.8 | M | Support basic personalization tokens (`{{name}}`, `{{company}}`) |
| FR-6.9 | S | Schedule a send for a future date/time |
| FR-6.10 | C | Bounce and open/click tracking via provider webhooks |

### 4.7 Campaign Management & History

| ID | Priority | Requirement |
|---|---|---|
| FR-7.1 | M | Persist every campaign: source URLs, generated content, final content, template, recipients, timestamps, status |
| FR-7.2 | M | List campaigns with filter by status/date and search by title |
| FR-7.3 | M | View a past campaign's full detail including the rendered HTML |
| FR-7.4 | M | Duplicate a past campaign as a new draft |
| FR-7.5 | M | Show per-campaign delivery stats (attempted / sent / failed) |
| FR-7.6 | S | Export campaign history to CSV |

### 4.8 Configuration, Auth & Observability

| ID | Priority | Requirement |
|---|---|---|
| FR-8.1 | M | Login screen; unauthenticated users see no campaign data |
| FR-8.2 | M | Settings page for LLM endpoint URL, email provider credentials, brand assets, defaults |
| FR-8.3 | M | "Test connection" buttons for both the LLM endpoint and the email provider |
| FR-8.4 | M | Never display secrets in plaintext after saving; never write them to logs |
| FR-8.5 | M | Logs page with level filter, search, and time range |
| FR-8.6 | M | Dashboard with counts (campaigns, sends, success rate) and system health indicators |
| FR-8.7 | S | Role separation: Editor (create/edit) vs Approver (send) |

---

## 5. Non-Functional Requirements

### 5.1 Performance
| ID | Requirement |
|---|---|
| NFR-P1 | Single-URL extraction + cleaning completes in ≤ 15 s (p95) |
| NFR-P2 | AI generation for 3 articles completes in ≤ 90 s (p95) on the reference Colab T4 setup |
| NFR-P3 | Any UI interaction that exceeds 1 s shows a spinner with descriptive text |
| NFR-P4 | Campaign History loads in ≤ 2 s with 1,000 campaigns stored |
| NFR-P5 | Email send throughput ≥ 100 recipients/min within provider rate limits |

### 5.2 Reliability
| ID | Requirement |
|---|---|
| NFR-R1 | An LLM outage must not lose user input — drafts survive and are resumable |
| NFR-R2 | Failed sends are recorded per recipient and are individually retryable |
| NFR-R3 | The application starts successfully even when the LLM endpoint is unreachable (degraded mode with a clear banner) |
| NFR-R4 | No data loss on process crash — every state transition is committed to the DB |

### 5.3 Usability
| ID | Requirement |
|---|---|
| NFR-U1 | A non-technical user completes their first campaign without training in ≤ 20 min |
| NFR-U2 | Error messages state what happened, why, and what to do next — never a raw exception |
| NFR-U3 | No destructive action (send, delete) occurs without explicit confirmation |
| NFR-U4 | Consistent visual language: same colours, spacing, and button hierarchy across pages |

### 5.4 Security
| ID | Requirement |
|---|---|
| NFR-S1 | Credentials live in environment variables / `.env`, never in source or the DB in plaintext |
| NFR-S2 | Passwords stored as bcrypt hashes |
| NFR-S3 | All external URLs validated; SSRF protection blocks private/loopback/link-local ranges |
| NFR-S4 | User-supplied content is escaped before HTML rendering to prevent template injection |
| NFR-S5 | Logs are scrubbed of API keys, passwords, and recipient email addresses at INFO level and above |
| NFR-S6 | The LLM tunnel endpoint requires a bearer token; it is never publicly open |

### 5.5 Maintainability
| ID | Requirement |
|---|---|
| NFR-M1 | No module exceeds ~400 lines; no function exceeds ~50 lines without justification |
| NFR-M2 | Public functions carry type hints and docstrings |
| NFR-M3 | Core business logic (scraper, cleaner, ai, template, email) has ≥ 70% test coverage |
| NFR-M4 | Swapping the LLM host requires changing configuration only, not code |
| NFR-M5 | Swapping the email provider requires implementing one interface, no call-site changes |

### 5.6 Portability
| ID | Requirement |
|---|---|
| NFR-PO1 | Runs on Windows 10/11 with Python 3.11+ and ≤ 2 GB RAM for the app process |
| NFR-PO2 | No GPU required on the client machine |
| NFR-PO3 | All paths handled via `pathlib`; no hard-coded drive letters or POSIX separators |
| NFR-PO4 | Fresh-machine setup is ≤ 5 commands |

### 5.7 Compliance
| ID | Requirement |
|---|---|
| NFR-C1 | Every email carries an unsubscribe link, `List-Unsubscribe` header, and a physical address |
| NFR-C2 | Source articles are summarized and attributed with a link, never republished verbatim |
| NFR-C3 | Recipient data is stored locally only and is exportable/deletable on request |

---

## 6. User Stories

### Epic A — Content Ingestion
- **US-A1** As Priya, I want to paste several OEM blog URLs at once so I can build a multi-story newsletter in one pass.
  *AC:* 1–10 URLs accepted; each shows an independent status chip (Queued → Fetching → Extracted → Failed); a failure on one does not block the others.
- **US-A2** As Priya, I want to see the extracted text before generation so I can confirm the system read the right article.
  *AC:* Expandable preview per article showing title, author, date, word count, and the first 500 characters.
- **US-A3** As Priya, when a site blocks the scraper, I want to paste the article text manually so I'm never fully blocked.
  *AC:* "Paste manually" control appears on extraction failure; pasted text enters the identical downstream pipeline.

### Epic B — AI Generation
- **US-B1** As Priya, I want to choose tone, length, and audience before generating so the draft matches the campaign.
  *AC:* Three selectors with sensible defaults, persisted as user preferences; the choice is recorded on the campaign.
- **US-B2** As Priya, I want to see progress during generation so I know it hasn't hung.
  *AC:* Stage-by-stage indicator (Extracting → Summarizing → Composing → Formatting) with elapsed time.
- **US-B3** As Priya, I want to regenerate just the subject line so I don't lose newsletter body edits I already made.
  *AC:* Per-field regenerate control; only that field changes; other fields and edits remain intact.
- **US-B4** As Rahul, I want to know which model and prompt version produced a campaign so results are reproducible and auditable.
  *AC:* Campaign detail shows model name, prompt version, tone/length/audience, and generation timestamp.

### Epic C — Review & Edit
- **US-C1** As Priya, I want to edit every generated field so the final copy is mine, not the machine's.
  *AC:* All nine fields editable; edits persist across navigation; "revert to AI original" available per field.
- **US-C2** As Priya, I want subject and preview text to warn me when they're too long so they don't truncate in inboxes.
  *AC:* Live counter turns amber at 90% and red past the limit; send is still permitted with an explicit warning.
- **US-C3** As Priya, I want to save a draft and finish tomorrow so I'm not forced to complete in one sitting.
  *AC:* Draft persists with all edits; appears in History as `DRAFT`; reopens in the exact prior state.

### Epic D — Rendering
- **US-D1** As Priya, I want to preview the newsletter as it will appear in an inbox so I catch layout problems before sending.
  *AC:* Rendered HTML preview with a desktop/mobile width toggle.
- **US-D2** As Priya, I want to send myself a test email so I can check it in my real mail client.
  *AC:* Test-send field accepts one address; sends immediately; result reported inline; does not create a campaign record.
- **US-D3** As Rahul, I want the brand logo and colours applied automatically so every send looks like us.
  *AC:* Settings-configured logo, primary colour, and footer are applied to every template.

### Epic E — Distribution
- **US-E1** As Priya, I want to upload a CSV of recipients so I don't retype addresses.
  *AC:* CSV parsed with header detection; a validation summary shows valid / invalid / duplicate counts before send is enabled.
- **US-E2** As Priya, I want a confirmation step showing exactly how many people will receive this so I never send by accident.
  *AC:* Modal states the recipient count, subject line, and sender; requires an explicit click; is not the default focus.
- **US-E3** As Priya, I want to watch send progress so I know it's working on a large list.
  *AC:* Live progress bar with sent/failed/remaining counts, updated per batch.
- **US-E4** As Rahul, I want failed sends listed with reasons so we can fix and retry.
  *AC:* Failure table with email, error reason, timestamp; a "retry failed only" action.

### Epic F — History & Operations
- **US-F1** As Rahul, I want to browse all past campaigns so I can see what we've published.
  *AC:* Paginated list with title, date, status, recipient count, success rate; filter and search.
- **US-F2** As Priya, I want to duplicate a past campaign so recurring formats are one click.
  *AC:* Creates a new `DRAFT` with copied content and settings; the original is untouched.
- **US-F3** As Sidhant, I want to see system health at a glance so I can fix the LLM tunnel before a demo.
  *AC:* Dashboard shows LLM endpoint, email provider, and database status with last-checked time.
- **US-F4** As Sidhant, I want searchable structured logs so I can diagnose a failure without a terminal.
  *AC:* Logs page with level filter, free-text search, time range, and CSV export.

---

## 7. Success Metrics

### 7.1 Product
| Metric | Baseline | Target | How measured |
|---|---|---|---|
| Time to send-ready draft | ~120 min | ≤ 20 min | In-app timer from first URL to `READY` |
| Campaigns per month | 3 | 12 | Campaign table count |
| Edit ratio (chars changed / chars generated) | n/a | ≤ 30% | Diff between AI original and final |
| Regeneration rate | n/a | ≤ 1.5 per campaign | Counter on the campaign row |
| Draft abandonment | n/a | ≤ 15% | Drafts never reaching `SENT` |

*Edit ratio is the honest quality signal: if users rewrite more than a third of the output, the prompt or the model is underperforming, regardless of how good the demo looks.*

### 7.2 Technical
| Metric | Target |
|---|---|
| Extraction success rate (first attempt, primary extractor) | ≥ 85% |
| Extraction success rate (any tier of the cascade) | ≥ 95% |
| JSON schema validity on first LLM attempt | ≥ 98% (guided decoding) |
| Email delivery success rate | ≥ 97% of valid addresses |
| Unhandled exceptions surfaced to the user | 0 |
| Test coverage on core modules | ≥ 70% |

### 7.3 Business
| Metric | Target |
|---|---|
| Recurring inference cost | ₹0 during development |
| New-developer time to first successful local run | ≤ 30 min |
| Open rate of generated newsletters | ≥ parity with manual (measured post-launch) |

---

## 8. Risks

| ID | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-1 | **Colab ToS**: hosting a served API over a tunnel is outside Colab's stated purpose ("no web service offerings not related to interactive compute"; free tiers disallow remote-control tunnels). Account could be throttled or restricted. | Medium | High | Provider abstraction with a hosted open-weight fallback; use a dedicated non-primary Google account; treat Colab as development-only and document the production swap | Sidhant |
| R-2 | Colab session terminates mid-campaign (~3 h typical, 12 h max) and the tunnel URL rotates | High | Medium | Health check before generation; drafts persisted before any LLM call; settings-page endpoint update; named tunnel for a stable hostname; circuit breaker + clear degraded-mode banner | Sidhant |
| R-3 | LLM hallucinates product facts, versions, or pricing in customer-facing copy | Medium | **Critical** | Mandatory human approval gate; extractive summary stage before generative rewrite; grounding constraints in the prompt; source link shown beside each section; explicit "verify facts" checklist before send | Priya/Rahul |
| R-4 | OEM sites block scraping or render content client-side | Medium | Medium | Three-tier extractor cascade; polite UA and rate limits; manual paste fallback; per-domain notes in the runbook |Sidhant |
| R-5 | Emails classified as spam | Medium | High | Verified sender domain with SPF/DKIM/DMARC; plain-text alternative; `List-Unsubscribe`; suppression list; warm-up guidance; no spam-trigger phrasing in generated subjects | Sidhant |
| R-6 | Copyright/IP concerns over OEM content | Low | High | Original summaries only, never verbatim republication; mandatory attribution and source link; documented editorial policy | Rahul |
| R-7 | Mid-size open model produces bland or off-brand marketing copy | Medium | Medium | Few-shot exemplars in prompts; tone presets tuned against real past newsletters; model swap is a config change; edit-ratio metric detects the problem early | Sidhant |
| R-8 | Recipient PII stored insecurely | Low | High | Local SQLite only; no third-party sync; scrubbed logs; documented deletion procedure | Sidhant |
| R-9 | Streamlit's rerun model causes lost edits or double-sends | Medium | High | All mutable state in `st.session_state` with explicit keys; idempotency key on send; send button disabled and guarded by a DB status transition | Sidhant |
| R-10 | Internship ends with the project half-documented | Medium | High | Documentation is milestone M0, not M9; README and handover pack are acceptance criteria, not nice-to-haves | Sidhant |

---

## 9. Assumptions

| ID | Assumption | If wrong |
|---|---|---|
| A-1 | Users have reliable internet; the app is used on a LAN/desktop, not offline | Offline mode would be a significant redesign |
| A-2 | Recipient lists are ≤ 10,000 addresses per campaign | Above that, a queue/worker (Celery + Redis) is required |
| A-3 | Concurrent users ≤ 5 | Above that, SQLite must become Postgres and Streamlit needs sticky sessions |
| A-4 | Every campaign has a human approver; no fully autonomous sending | Autonomous sending would require a much stronger fact-checking layer |
| A-5 | OEM blogs are primarily English, server-rendered HTML | JS-heavy sites need Playwright; other languages need a multilingual prompt set |
| A-6 | A free-tier email provider (300/day) is sufficient during development | Production volume requires a paid plan — budget item, not a design change |
| A-7 | A Google account with Colab access is available and GPU allocation is usually granted | Fallback to hosted open-weight inference (already designed) |
| A-8 | Vays Infotech will supply brand assets (logo, colours, footer, sender domain) | Placeholders used until supplied |
| A-9 | "Open source LLM" constrains the *model licence*, not the *hosting method* | If self-hosting is also mandated, the hosted fallback is unavailable and Colab/company server becomes the only path |

---

## 10. Future Scope (v2+)

**Near term (v1.1)**
- RSS/Atom auto-monitoring of OEM blogs with a "new articles" digest
- Scheduled sends
- Subject line A/B testing
- Multiple brand profiles for different OEM co-marketing campaigns

**Medium term (v2)**
- Open/click tracking via provider webhooks and an analytics dashboard
- Segment-based personalization (industry, company size, past engagement)
- Image handling: pull the article hero image and place it in the template
- Multi-language generation
- Approval workflow with a formal reviewer sign-off step
- Migration to Postgres + FastAPI + a background worker

**Long term (v3)**
- CRM/marketing-automation integration (HubSpot, Salesforce, Zoho)
- Fine-tuned or LoRA-adapted model trained on Vays' own approved newsletters
- Engagement-driven content recommendation ("this OEM topic performs best with SMB")
- Landing-page generation to match each newsletter

---

## 11. Acceptance Criteria (v1 release gate)

The v1 is accepted only when **all** of the following are demonstrably true:

**Functional**
1. Pasting 3 valid OEM blog URLs produces a complete, schema-valid newsletter draft with all nine fields populated.
2. Every field is editable, edits persist across page navigation, and each field can be reverted to its AI original.
3. The rendered HTML displays correctly in Gmail (web + mobile) and Outlook desktop.
4. A CSV of ≥ 100 recipients uploads, validates, and sends with per-recipient success/failure recorded.
5. A campaign appears in History immediately after sending with correct stats and is reopenable in full detail.
6. The unsubscribe link is present and functional in every delivered email.
7. An invalid URL, a blocked site, an LLM outage, and an email-provider failure each produce a clear, actionable message — never a stack trace.
8. Login is required; an unauthenticated visitor sees no campaign data.

**Technical**
9. `pytest` passes with ≥ 70% coverage on the core modules.
10. The app starts and remains usable when the LLM endpoint is down, showing a degraded-mode banner.
11. Changing `LLM_PROVIDER` in `.env` switches inference backend with no code change.
12. No secret appears in any log file, in the UI, or in the repository.
13. A cold start on a clean Windows machine, following only the README, reaches a running app in ≤ 30 min.

**Documentation**
14. README covers installation, configuration, running, troubleshooting, and handover.
15. The Colab notebook runs top to bottom and yields a working endpoint.
16. Architecture docs (PRD, TRD, UI spec, prompt library, runbook) are complete and current.
17. `.env.example` documents every variable with a description and a safe default.

---

## 12. UX Goals

### 12.1 Design principles
1. **The human is the author; the AI is the intern.** The UI must never imply the output is
   finished. Language is "Draft generated", never "Newsletter ready".
2. **Nothing irreversible happens quietly.** Sends and deletes require deliberate confirmation
   that states the blast radius.
3. **Show the work.** Users see extracted text, provenance links, and which model/prompt produced
   what. Opacity destroys trust in AI tooling faster than any bug.
4. **Progress over spinners.** Long operations report their current stage, not an anonymous
   rotating icon.
5. **One primary action per screen.** Each page has exactly one obvious next step.
6. **Errors are instructions.** "Couldn't reach the AI service. Open Settings → Test Connection,
   or paste the new Colab URL." — not `ConnectionError: HTTPSConnectionPool(...)`.
7. **Non-technical by default.** Model parameters, token counts, and provider internals live
   behind an "Advanced" expander.

### 12.2 Visual and interaction targets
| Goal | Concretely |
|---|---|
| Professional, not toy-like | Restrained palette (one brand accent + neutral greys), generous whitespace, consistent 8px spacing scale, no emoji-driven layout |
| Fast perceived performance | Skeletons and staged progress, optimistic UI where safe, cached extraction results |
| Legible information density | Card layout on Dashboard; tables with fixed column widths in History; no horizontal scrolling |
| Obvious state | Every campaign shows a coloured status chip: `DRAFT` (grey), `READY` (blue), `SENDING` (amber, animated), `SENT` (green), `FAILED` (red) |
| Accessible | ≥ 4.5:1 contrast on text, keyboard-navigable forms, status never conveyed by colour alone (chip carries a label) |
| Forgiving | Autosave drafts; confirmations on destructive actions; per-field revert |

### 12.3 The one-minute test
A new marketing executive, given only the app and no training, should be able to answer within
60 seconds: *What does this do? What do I do first? Is anything broken right now?*
The Dashboard exists primarily to answer those three questions.
