# Backend Architecture
### Phase 8 — Module contracts, interfaces and design principles
**Date** 2026-08-05 · **Status** Draft — awaiting approval

---

## 1. Principles applied (and where they are deliberately not)

SOLID is a means, not a scorecard. Applied where it buys something:

| Principle | Applied | Concretely |
|---|---|---|
| **S**ingle responsibility | Strongly | Scraper fetches. Cleaner cleans. Engine orchestrates the LLM. Renderer renders. No module does two of these. The test: can you name what a module does without using "and"? |
| **O**pen/closed | At integration points only | Adding a 4th LLM provider or a 3rd email provider means adding a class, not editing existing code. Not applied to core domain logic, where it would be speculative |
| **L**iskov | Enforced by tests | Every `LLMProvider` implementation passes the same shared contract test suite. A provider that violates the contract fails CI |
| **I**nterface segregation | Yes | `LLMProvider` has 3 members, not 12. `EmailProvider` has 2. Fat interfaces force implementations to stub methods they don't need |
| **D**ependency inversion | Yes | Services depend on ABCs; concrete classes are injected by factories reading config. This is what makes the mock provider possible, which is what makes the project testable without a GPU |

**Deliberately not done:** a repository interface per entity with multiple implementations (there
is one database), a generic event bus, a plugin system, or dependency-injection framework. Each
would add indirection for a flexibility no requirement asks for. Over-abstraction is as damaging
to handover as no abstraction.

---

## 2. Module contracts

### 2.1 `modules/scraper` — get article text from a URL

```python
class ExtractorStrategy(Protocol):
    name: str

    def extract(self, html: str, url: str) -> ExtractedArticle | None: ...

    # returns None (not an exception) when this strategy can't handle the page —
    # "can't extract" is an expected outcome in a cascade, not an error


class ArticleFetcher:
    def fetch(self, url: str) -> FetchResult:
        """HTTP GET with timeout, retry, UA, robots.txt check, redirect re-validation.
        Raises: FetchError, InvalidURLError (incl. SSRF rejection)."""


class ArticleExtractor:
    """Cascade orchestrator — the only class services talk to."""

    def extract(self, url: str) -> ExtractedArticle:
        """trafilatura → newspaper4k → bs4 fallback.
        Accepts the first result with >= SCRAPER_MIN_WORD_COUNT words.
        Records which tier succeeded (goes into articles.extractor_used).
        Raises: AllExtractorsFailed."""

    def extract_from_text(self, title: str, text: str, url: str | None) -> ExtractedArticle:
        """Manual-paste path — same output type, so downstream code is identical."""
```

**Design note:** strategies return `None` rather than raising, because in a cascade a failed tier
is normal control flow. Reserving exceptions for genuine errors keeps the orchestrator readable
and keeps the logs honest — a `trafilatura` miss is a `WARNING`, not an `ERROR`.

**SSRF guard lives in the fetcher, not the service.** Validation that protects the process must sit
at the boundary that performs the dangerous operation, so no future caller can bypass it.

### 2.2 `modules/cleaner` — pure text transformation

```python
class TextCleaner:
    def clean(self, raw: str) -> CleanedText:
        """NFKC normalize · collapse whitespace · strip boilerplate patterns ·
        de-duplicate repeated paragraphs · detect language.
        Pure function. No I/O. Deterministic."""


class TokenBudgeter:
    def estimate(self, text: str) -> int: ...
    def truncate(self, text: str, max_tokens: int) -> TruncationResult:
        """Keeps the lead paragraphs, section headings, and the closing paragraph;
        drops from the middle. Blind tail-truncation destroys conclusions, which is
        exactly where OEM blogs put the product announcement."""
```

Entirely pure — which is why it gets 85% coverage and property-based tests cheaply. Text
normalization is the classic place where a Unicode edge case ships to production unnoticed.

### 2.3 `modules/ai` — the LLM boundary

```python
class LLMProvider(ABC):
    def health_check(self) -> HealthStatus: ...
    def generate(
        self, messages: list[Message], *, json_schema: dict | None, params: GenerationParams
    ) -> LLMResponse: ...
    @property
    def supports_guided_json(self) -> bool: ...


class PromptRegistry:
    def get(self, name: str, version: str = "latest") -> PromptTemplate: ...
    def render(self, name: str, version: str, **ctx) -> RenderedPrompt:
        """Jinja2 render of the YAML prompt. Raises PromptNotFoundError,
        or PromptContextError if a declared required variable is missing —
        a missing variable must fail loudly, not silently render an empty string."""

    def list_versions(self, name: str) -> list[str]: ...


class AIEngine:
    """The two-stage pipeline. The only AI entry point services use."""

    def summarize_article(
        self, article: CleanedArticle, opts: GenerationOptions
    ) -> ArticleSummary: ...
    def compose_newsletter(
        self, summaries: list[ArticleSummary], opts: GenerationOptions
    ) -> NewsletterContent: ...
    def regenerate_field(
        self, draft: NewsletterContent, field: EditableField, instruction: str | None
    ) -> str: ...
    def generate_subject_variants(self, draft: NewsletterContent, n: int) -> list[str]: ...
```

**JSON validation ladder** (defence in depth, cheapest check first):
1. Guided decoding constrains generation — invalid tokens are unselectable.
2. `json.loads` — should never fail with (1) in place; if it does, that's a server bug worth logging loudly.
3. Pydantic model validation — catches semantically empty output that is still schema-valid
   (e.g. `summary: ""`).
4. Business rules — subject length, required non-empty fields, keyword count.
5. **Repair retry** — for non-guided providers only: re-prompt with the malformed output and the
   specific validation error. One attempt, then fail.

**Circuit breaker wraps the provider, not the engine.** The engine should be able to assume that if
`generate()` returns, it returned something usable.

### 2.4 `modules/template` — content → email HTML

```python
class TemplateRenderer:
    def render(
        self,
        content: NewsletterContent,
        template_id: str,
        brand: BrandConfig,
        recipient: Recipient | None = None,
    ) -> RenderedEmail:
        """Jinja2 (SandboxedEnvironment, autoescape=True) over MJML-compiled HTML,
        then premailer CSS inlining, then plain-text generation.
        `recipient` supplies merge fields; None renders with placeholder values for preview.
        Raises: TemplateError."""

    def list_templates(self) -> list[TemplateInfo]: ...
```

The sandboxed environment matters: article content is untrusted input, and a `{{ }}` sequence
surviving extraction into a template string is a real code-execution path (S-3).

Every rendered email is asserted — in tests, not by convention — to contain an unsubscribe link,
a physical address, and a non-empty plain-text part.

### 2.5 `modules/email` — delivery

```python
class EmailProvider(ABC):
    def send(self, message: EmailMessage) -> SendResult: ...
    def verify_credentials(self) -> HealthStatus: ...


class BatchSender:
    """Owns batching, pacing and retry — deliberately NOT the provider's job,
    so retry policy is identical across Brevo, SMTP and console."""

    def send_many(
        self, messages: list[EmailMessage], *, on_progress: ProgressCB | None
    ) -> list[SendResult]:
        """Batches of EMAIL_BATCH_SIZE with EMAIL_BATCH_DELAY_S between them.
        Retries transient failures (429/5xx/timeout) with exponential backoff.
        Never raises on partial failure — returns per-recipient results."""
```

Separating retry from the provider is the decision that keeps a second provider cheap: a new
adapter implements two methods and inherits all the delivery robustness.

### 2.6 `modules/repository` — persistence

One repository per aggregate; each exposes intention-revealing methods (`get_recent`,
`list_by_status`, `mark_sent`), never a generic `query()` that leaks SQLAlchemy into services.

```python
class CampaignRepository:
    def create(self, campaign: CampaignCreate) -> Campaign: ...
    def get(self, id: int) -> Campaign | None: ...
    def list(self, f: CampaignFilter) -> Page[CampaignSummary]: ...
    def update_content(self, id: int, patch: ContentPatch) -> Campaign: ...
    def transition_status(self, id: int, from_: set[CampaignStatus], to: CampaignStatus) -> bool:
        """Conditional UPDATE ... WHERE status IN (...). Returns False if no row matched.
        This is the double-send guard (TRD §3.2) — it must be a single atomic
        statement, not read-then-write."""
```

**Transactions are owned by services, not repositories.** A repository that commits on its own
makes multi-step use cases impossible to make atomic.

---

## 3. Service layer

Services are the **verbs of the product**. Each one maps to a use case a user would recognise.

```python
class GenerationService:
    def __init__(self, engine: AIEngine, articles: ArticleRepository,
                 campaigns: CampaignRepository, uow: UnitOfWork): ...

    def generate(self, req: GenerationRequest, *, on_progress=None) -> NewsletterDraft:
        # 1. load articles (already persisted by IngestionService)
        # 2. health check once — fail fast and legibly
        # 3. STAGE 1: summarize each article (bounded parallelism, max 3)
        # 4. STAGE 2: compose the newsletter from the summaries
        # 5. persist draft + provenance (model, prompt version, params, timings)
        # 6. return; the UI redirects to Preview
```

**Rules**
- Zero `import streamlit`. Enforced by `import-linter` in CI, not by discipline.
- Every public method takes and returns Pydantic models.
- Every method accepts an optional `on_progress` callback — this is what lets a long operation
  report stages to the UI today and run in a worker tomorrow **without a signature change**.
- Services own transaction boundaries via an explicit `UnitOfWork` context manager.
- Services translate low-level exceptions into domain exceptions; the UI only ever sees
  `NewsletterAppError` subclasses.

---

## 4. Data flow — one request, end to end

```
UI: [Generate] clicked
 └─ GenerationService.generate(req, on_progress=ui_callback)
     ├─ correlation_id = new_id(); structlog.bind(correlation_id=cid)
     ├─ articles = ArticleRepository.get_many(req.article_ids)
     ├─ health = engine.provider.health_check()        → raise LLMUnavailableError if down
     │
     ├─ STAGE 1  (ThreadPoolExecutor, max_workers=3)
     │   for each article:
     │     ├─ prompt = PromptRegistry.render("article_summary", "1.0.0", article=…, opts=…)
     │     ├─ resp   = provider.generate(prompt, schema=ARTICLE_SUMMARY_SCHEMA, params=…)
     │     ├─ summary= ArticleSummary.model_validate(resp.payload)
     │     └─ on_progress(f"Summarised {i}/{n}")
     │
     ├─ STAGE 2
     │   ├─ prompt = PromptRegistry.render("newsletter_compose", "1.0.0", summaries=…, opts=…)
     │   ├─ resp   = provider.generate(prompt, schema=NEWSLETTER_SCHEMA, params=…)
     │   └─ content= NewsletterContent.model_validate(resp.payload)
     │
     ├─ with uow:                                        ← single transaction
     │     campaign = CampaignRepository.create(... ai_* = content, final = copy(content),
     │                                          model, prompt_version, params, timings)
     │     link campaign ↔ articles with per-article section summaries
     │
     └─ return NewsletterDraft(campaign_id=…, content=content)
```

Every step logs with the same `correlation_id`, so the Logs page can reconstruct the whole
operation from one click (TRD §8.2).

---

## 5. Concurrency

| Operation | Approach | Why |
|---|---|---|
| URL extraction | `ThreadPoolExecutor(max_workers=4)` | I/O-bound; GIL is released during network waits |
| Stage-1 summarization | `ThreadPoolExecutor(max_workers=3)` | Bounded — a T4 serving one model does not benefit from 10 concurrent requests, and unbounded fan-out just causes timeouts |
| Email batches | Sequential batches, parallel within a batch (max 10) | Respects provider rate limits; parallelism here risks 429s for little gain |
| Everything else | Synchronous | Streamlit's model is synchronous; async would add complexity with no user-visible benefit |

**Not used:** `asyncio`. Streamlit's execution model is synchronous, the libraries in play have
solid sync APIs, and threads are sufficient for this level of I/O concurrency. Introducing an event
loop would complicate the handover for a performance gain nobody would notice.

---

## 6. How to extend it — the questions a successor will actually ask

| "How do I…" | Answer |
|---|---|
| add an LLM provider | New class in `modules/ai/` implementing `LLMProvider`; register in `factory.py`; add the enum value to config. **No other file changes.** |
| add an email provider | New class implementing `EmailProvider` (2 methods); register in `factory.py`. Batching and retry come free |
| add an email template | Author `templates/email/src/x.mjml`, compile, commit the HTML, add to the template list |
| change a prompt | Copy `prompts/<name>/v1.0.0.yaml` → `v1.1.0.yaml`, edit, bump the default version in config. **Old campaigns remain reproducible** because their version is recorded |
| add a field to the newsletter | Add to `core/schemas.py` + the Pydantic model + the prompt YAML + a DB migration + one editor widget. Five deliberate steps — that's the schema doing its job |
| move to Postgres | Change `DATABASE_URL`; run `alembic upgrade head`. No code changes (no raw SQL anywhere) |
| add a background worker | Services already accept `on_progress` and never touch Streamlit — wrap the call in Celery/RQ |
| add an HTTP API | Wrap `services/` in FastAPI routers; the signatures already take and return Pydantic models |

---

## 7. Anti-patterns this design forbids

| Forbidden | Why | Enforcement |
|---|---|---|
| `import streamlit` in `services/` or `modules/` | Makes the logic untestable and unmovable | `import-linter` in CI |
| Raw dicts across module boundaries | Silent typos, no validation, unknowable shape | Type hints + `mypy` |
| Business logic in `ui/pages/` | Cannot be tested, cannot be reused, will be duplicated | Code review; page files should be short |
| `except Exception: pass` | Hides the bug you'll spend a day finding later | `ruff` rule |
| Instantiating a provider inside a service | Breaks testability; hardcodes the choice | Constructor injection only |
| Caching user-specific data with `@st.cache_data` | Cache is shared across sessions — leaks data between users | Code review; documented in UI spec §11 |
| Committing secrets | — | `.gitignore` + `detect-secrets` pre-commit hook |
| Raw SQL | Breaks the Postgres migration path | Code review |

---

## 8. Module-level test strategy

| Module | Focus | Notable technique |
|---|---|---|
| `scraper` | Cascade order; SSRF rejection; malformed HTML | Saved HTML fixtures of real OEM pages |
| `cleaner` | Unicode, whitespace, truncation boundaries | `hypothesis` property tests |
| `ai` | Schema validation, repair path, circuit breaker, prompt rendering | `MockLLMProvider` + shared `LLMProvider` contract suite run against every implementation |
| `template` | Unsubscribe present, text part present, merge fields | Golden-file comparison |
| `email` | Batching, backoff, partial failure, suppression | `respx` mocking 429/500/timeout |
| `repository` | State-transition guard, cascade deletes, dedupe constraint | Temp SQLite per test |
| `services` | Orchestration, transaction rollback, progress callbacks | All adapters mocked |
