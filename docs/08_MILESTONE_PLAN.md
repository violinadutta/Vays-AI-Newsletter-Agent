# Milestone Plan
### Phase 10 — Sequenced delivery plan
**Date** 2026-08-05 · **Status** Draft — awaiting approval

**Rule of engagement:** no milestone starts until the previous one is complete and demonstrated.
Within a milestone, code is delivered **module by module**, each with explanation, folder placement,
type hints, error handling, logging, docstrings and suggested unit tests — then a pause for approval.

---

> **⚠ SUPERSEDED IN PART (2026-08-07, D-21).** This document was written when the LLM
> was to be self-hosted on Google Colab. **Colab has been dropped entirely** — it failed
> twice on real hardware and its 3-hour sessions, rotating tunnel URL and ToS conflict
> made it unsuitable regardless. The LLM is now **Groq** (open-weight models over an
> ordinary API). Any mention below of Colab, Cloudflare Tunnel, vLLM, or Qwen3-on-a-T4
> is historical. See `docs/04_LLM_HOSTING.md` for what is actually built.

## Sequencing rationale

The order is driven by **risk retirement**, not by the user's workflow order.

M3 (LLM + Colab) comes early — before the UI — because it is the single highest-risk element: if
Colab cannot serve a 14B model reliably, the model choice and possibly the provider strategy change,
and everything downstream depends on that answer. Building six polished UI pages before knowing
whether the AI layer works would be building on an unvalidated assumption.

M1–M2 come first only because M3 needs config, logging and real article text to test against.

---

## M1 — Foundation
**Objective:** a running skeleton with configuration, logging, database and error handling in place,
so every later module plugs into an existing structure rather than inventing one.

| | |
|---|---|
| **Files** | `config/settings.py`, `config/constants.py`, `config/logging_config.py`, `core/models.py`, `core/enums.py`, `core/exceptions.py`, `core/validators.py`, `core/schemas.py`, `modules/repository/database.py`, `modules/repository/orm_models.py`, all repositories, `migrations/`, `app.py` (shell), `.env.example`, `requirements.txt`, `pyproject.toml` |
| **Key functions** | `get_settings()` · `configure_logging()` · `get_session()` · `init_db()` · `validate_url()` (incl. SSRF guard) · `validate_email()` · `CampaignRepository.transition_status()` |
| **Expected output** | `streamlit run app.py` shows an empty shell with sidebar nav; `alembic upgrade head` creates all tables; a startup log line appears in `logs/app.jsonl`; a bad `.env` value produces a clear startup error |
| **Testing** | Settings validation (valid + invalid) · SSRF guard rejects localhost/127.0.0.1/169.254.169.254/10.x/192.168.x and a public host resolving to a private IP · every repository CRUD · state-transition guard rejects illegal transitions · logging redacts secrets |
| **Deliverables** | Runnable skeleton · migrations · `.env.example` · ~25 unit tests |
| **Done when** | App starts, DB is created, logs are written, tests pass, and a deliberately malformed `.env` fails loudly with a useful message |

---

## M2 — Scraper & Cleaner
**Objective:** reliably turn a URL into clean, budgeted article text.

| | |
|---|---|
| **Files** | `modules/scraper/{base,fetcher,trafilatura_extractor,newspaper_extractor,fallback_extractor,extractor}.py`, `modules/cleaner/{text_cleaner,tokenizer}.py`, `services/ingestion_service.py`, `tests/fixtures/html/*.html` |
| **Key functions** | `ArticleFetcher.fetch()` · `ArticleExtractor.extract()` (3-tier cascade) · `extract_from_text()` · `TextCleaner.clean()` · `TokenBudgeter.truncate()` · `IngestionService.ingest_urls()` |
| **Expected output** | A CLI smoke script extracts 5 real OEM blog URLs and prints title, author, date, word count and which extractor tier succeeded |
| **Testing** | Cascade falls through correctly when tier 1 returns short content · malformed HTML doesn't crash · timeout handled · robots.txt respected · Unicode/whitespace normalization (property-based) · truncation keeps lead + tail · one bad URL in a batch of 3 doesn't abort the batch |
| **Deliverables** | Extraction pipeline · ≥8 saved HTML fixtures from real OEM sites · ~35 tests |
| **Done when** | ≥85% first-tier success and ≥95% overall success across 20 real OEM URLs, measured and recorded |

---

## M3 — LLM provider & Colab notebook ⚠️ highest risk
**Objective:** prove that an open-source LLM can be served from Colab and called reliably from
Windows — and settle the model choice with evidence.

| | |
|---|---|
| **Files** | `modules/ai/{base,colab_provider,hosted_provider,mock_provider,factory,circuit_breaker}.py`, `notebooks/colab_llm_server.ipynb`, `services/health_service.py`, `scripts/check_no_local_inference.py`, `tests/fixtures/llm_responses/*.json` |
| **Key functions** | `LLMProvider.generate()` / `.health_check()` · `CircuitBreaker.call()` · `provider_factory()` · notebook cells 1–9 (Colab doc §3) |
| **Expected output** | Notebook runs top-to-bottom on a fresh runtime and prints copy-pasteable `LLM_BASE_URL` / `LLM_API_KEY`; a Python script on Windows sends a guided-JSON request through the tunnel and receives schema-valid JSON |
| **Testing** | `MockLLMProvider` returns fixtures with no network · shared `LLMProvider` contract suite passes for every implementation · retry on 429/500/timeout, no retry on 401/404 · circuit opens after 3 failures and half-opens after 60 s · `respx`-simulated failures |
| **Deliverables** | Provider layer · Colab notebook · circuit breaker · health service · ADR recording the **Qwen3-14B-AWQ vs Qwen3-8B-AWQ comparison on 3 real OEM articles** |
| **Done when** | All 9 acceptance criteria in Colab doc §10 pass — including killing the runtime mid-request and confirming clean recovery, and running the full path with `LLM_PROVIDER=mock` offline |

**If this milestone fails** (Colab won't serve 14B reliably): fall back to Qwen3-8B, or to
`LLM_PROVIDER=hosted`. Both are one-line config changes — which is precisely why this milestone is
scheduled third rather than last.

---

## M4 — Prompt registry & AI engine
**Objective:** the two-stage generation pipeline producing validated newsletter JSON.

| | |
|---|---|
| **Files** | `modules/ai/{prompt_registry,engine}.py`, `prompts/**/*.yaml`, `prompts/_shared/**`, `services/generation_service.py`, `scripts/validate_prompts.py`, `scripts/eval_prompts.py` |
| **Key functions** | `PromptRegistry.render()` · `AIEngine.summarize_article()` · `.compose_newsletter()` · `.regenerate_field()` · `.generate_subject_variants()` · `GenerationService.generate()` |
| **Expected output** | A CLI script takes 3 URLs and prints a complete, validated newsletter JSON with all nine fields |
| **Testing** | Prompt rendering with all context · `PromptContextError` on a missing required variable · schema validation accepts valid / rejects invalid · repair-retry path · every example output in every prompt file validates against its declared schema · single-field regeneration leaves other fields untouched |
| **Deliverables** | Prompt library v1.0.0 · AI engine · generation service · prompt validator in CI · golden-set evaluation results |
| **Done when** | 20 consecutive generations produce schema-valid JSON; the golden-set evaluation (Prompt doc §6) passes every gate, **including zero fabricated facts** |

---

## M5 — Template engine
**Objective:** newsletter content → responsive HTML that survives Outlook.

| | |
|---|---|
| **Files** | `templates/email/src/{modern,classic,minimal}.mjml`, compiled `*.html`, `modules/template/{renderer,brand}.py` |
| **Key functions** | `TemplateRenderer.render()` · `list_templates()` · `BrandConfig.load()` · plain-text generation |
| **Expected output** | Three rendered `.html` files verified in Gmail (web + mobile), Outlook desktop, and Apple Mail |
| **Testing** | Merge fields substituted · **unsubscribe link present in every template** · **plain-text part non-empty** · brand colour/logo applied · `{{ }}` in article content is escaped, not executed · golden-file comparison |
| **Deliverables** | 3 MJML sources + compiled HTML · renderer · brand config · client-compatibility screenshots |
| **Done when** | All 3 templates render correctly in the 4 target clients, and the compatibility evidence is committed |

---

## M6 — Email engine & campaign manager
**Objective:** send a real campaign to a real list, with per-recipient outcomes.

| | |
|---|---|
| **Files** | `modules/email/{base,brevo_provider,smtp_provider,console_provider,factory,batcher}.py`, `services/{campaign_service,delivery_service}.py` |
| **Key functions** | `EmailProvider.send()` / `.verify_credentials()` · `BatchSender.send_many()` · `DeliveryService.validate_recipients()` / `.send_test()` / `.send_campaign()` / `.retry_failed()` · `CampaignService.duplicate()` |
| **Expected output** | A CLI script sends a rendered newsletter to a 10-address test list and reports per-recipient results |
| **Testing** | CSV parsing incl. malformed rows · dedupe · **suppression list honoured even when the address is in the CSV** · batching and pacing · retry on 429/5xx · partial failure returns results without raising · **double-send guard: invoking send twice sends once** · `console` provider writes `.eml` files |
| **Deliverables** | Email layer · campaign lifecycle · delivery service · ~40 tests |
| **Done when** | A 50-recipient test campaign sends with correct per-recipient records, and the double-send test passes |

---

## M7 — Streamlit UI
**Objective:** the six pages, built against services that already work.

Delivered page by page, in this order (each is a separate approval point):
`Generate → Preview → Dashboard → History → Settings → Logs`

| | |
|---|---|
| **Files** | `ui/pages/1..6`, `ui/components/*.py`, `ui/state.py`, `ui/styles.py`, `app.py` (nav + guard) |
| **Key functions** | Per-page `render()` · component functions (`status_chip`, `metric_card`, `editable_field`, `article_card`, `health_indicator`, `confirm_dialog`, `empty_state`, `error_panel`) · typed session-state accessors |
| **Expected output** | Full workflow usable end to end in the browser |
| **Testing** | Streamlit `AppTest` for the happy path · state persists across page navigation · **send button disabled without confirmation** · error states render for each failure class · no user-specific data passed to `@st.cache_data` |
| **Deliverables** | 6 pages · component library · state module · styling |
| **Done when** | Every state in the UI-spec §9 matrix is implemented and demonstrable — including all five states for each long-running interaction |

---

## M8 — Auth, settings & observability
**Objective:** make it operable by someone who isn't the developer.

| | |
|---|---|
| **Files** | `services/settings_service.py`, auth module, `scripts/create_user.py`, Settings and Logs pages completed |
| **Key functions** | `authenticate()` · `require_auth()` guard · `SettingsService.get/set/test_connection()` · secret masking · log querying |
| **Expected output** | Login required; settings editable in-app; Test Connection works for both providers; Logs page filters and exports |
| **Testing** | Auth rejects bad credentials with a generic message · lockout after 5 attempts · unauthenticated access to a page redirects · **secrets never appear in any log record or in the UI after save** · settings persist and take effect without restart |
| **Deliverables** | Auth · settings management · logs page · admin script |
| **Done when** | A second user account can log in, change the LLM endpoint URL, test it, and run a campaign — without touching a file or a terminal |

---

## M9 — Testing, documentation & handover
**Objective:** make it survivable without me.

| | |
|---|---|
| **Files** | `README.md`, `docs/{SETUP_GUIDE,ARCHITECTURE,RUNBOOK,API_REFERENCE,PROMPT_GUIDE,KNOWN_ISSUES}.md`, `docs/ADR/*.md`, `.github/workflows/ci.yml`, `run.bat` |
| **Key work** | Raise coverage to ≥70% · end-to-end tests · CI pipeline (lint, types, import-linter, tests on Windows + Linux, pip-audit, prompt validation) · full documentation set · handover recording |
| **Expected output** | Green CI; a documentation set that answers a new developer's questions without asking me |
| **Testing** | **The handover test: a developer with no context, given only the repo, reaches a running app and a sent test newsletter in ≤30 minutes.** Run this with an actual person, not hypothetically |
| **Deliverables** | Full docs · CI · `run.bat` · handover session + recording · `KNOWN_ISSUES.md` |
| **Done when** | Every acceptance criterion in PRD §11 is demonstrably met |

---

## Dependency graph

```
M1 Foundation
 ├──▶ M2 Scraper/Cleaner ──┐
 └──▶ M3 LLM + Colab ──────┴──▶ M4 AI Engine ──┐
                                                ├──▶ M6 Email + Campaign ──▶ M7 UI ──▶ M8 Auth/Ops ──▶ M9 Handover
                             M5 Templates ──────┘
```
M5 has no dependency on M3/M4 — it can be built in parallel or used as productive work while
waiting on a Colab GPU allocation.

---

## Cross-cutting definition of done

Every milestone, without exception:

- [ ] Type hints on all public functions; `mypy` clean
- [ ] Docstrings (purpose, args, returns, raises) on all public functions
- [ ] Errors raise domain exceptions from `core/exceptions.py`, never bare `Exception`
- [ ] Structured logging at meaningful points, with correlation IDs
- [ ] No secrets in code, logs, or the UI
- [ ] Tests written **with** the code, not after
- [ ] `ruff` and `import-linter` clean
- [ ] `CLAUDE.md` milestone table updated
- [ ] Demonstrated working before moving on

---

## Risks to the plan itself

| Risk | Mitigation |
|---|---|
| M3 slips because Colab won't cooperate | M5 (templates) is independent — switch to it while waiting. Fallback providers are already designed |
| Scope creep from "just one more feature" | The PRD priority column (M/S/C) is the arbiter. `C` items go to Future Scope, not into v1 |
| Documentation deferred to the end | Docs are M0 (already done) and M9, and are acceptance criteria — not optional |
| Internship ends mid-milestone | Milestones are independently demonstrable; each ends at a working state, never a half-integrated one |
| Prompt quality disappoints late | M4's golden-set evaluation surfaces this early, not after the UI is built on top of it |
