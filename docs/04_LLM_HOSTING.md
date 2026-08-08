# LLM Hosting
### Groq — how the model is served, and how to replace it
**Date** 2026-08-07 · **Status** LOCKED (D-21) · *Supersedes `04_COLAB_LLM_ARCHITECTURE.md`, deleted*

---

## 1. Why Groq, and why not Colab

The original design served Qwen3 from a Colab notebook over a Cloudflare Tunnel.
**That approach is abandoned.** It was tried twice on real hardware and failed both
times before a single token was generated:

| Attempt | Failure |
|---|---|
| 1 | `pip install vllm` fetched the CUDA 13 wheel; Colab runs CUDA 12.x → `ImportError: libcudart.so.13` |
| 2 | Install-then-restart loop; the kernel restarted three times and never produced a working vLLM |

Those were fixable. What was not fixable is the shape of the thing: a ~3 hour
session life, a tunnel URL that rotates on every restart, no guaranteed GPU, a
5–10 minute cold start, a CUDA/torch matrix that shifts without notice, and a
standing conflict with Colab's terms of service. That is a lot of ongoing tax on
a project whose actual value is the pipeline, not the hosting.

**Groq removes all of it:**

| | Colab + vLLM | Groq |
|---|---|---|
| Setup | notebook, tunnel, 5–10 min per session | one API key |
| Session life | ~3 h, then re-paste a new URL | none — it is an API |
| GPU availability | not guaranteed | not our problem |
| Terms of service | grey area (§0.1 of the old doc) | ordinary API use |
| Open-source model | yes | **yes — Groq serves only open-source models** |
| Cost | ₹0 | ₹0 on the free tier, no card |
| Schema enforcement | vLLM + XGrammar | `strict: true` on supported models |

The brief's hard constraint — *the LLM must be open source* — is satisfied
**more** cleanly than before. There are no proprietary models on Groq's platform
at all, so the question cannot even arise (D-2: the constraint is on the model's
licence, not on who runs the GPU).

### What this cost to change

One small class (`modules/ai/groq_provider.py`) and a config default. Nothing in
the prompts, schemas, services, repositories or UI moved. That is the return on
building the `LLMProvider` seam up front, and it is the same seam Vays will use
to swap in their own model.

---

## 2. Architecture

```
┌──────────── WINDOWS LAPTOP · 8 GB RAM · NO GPU ────────────┐
│  Streamlit → GenerationService                             │
│        ▼                                                   │
│  LLMProvider  (abstract port)                              │
│    ├── health_check()                                      │
│    └── generate(messages, schema, params)                  │
│        selected by LLM_PROVIDER                            │
│     ┌──────────┬──────────────┬──────────┐                 │
│     ▼          ▼              ▼                            │
│   Groq      Hosted          Mock                           │
│     │          │              └─ fixtures; no network      │
│     │          └─ any OpenAI-compatible endpoint (handover)│
│     │                                                      │
│  Circuit breaker → tenacity (+ Retry-After) → httpx        │
│                                                            │
│  ✗ No provider ever loads model weights. By construction.  │
└─────┼──────────────────────────────────────────────────────┘
      │ HTTPS · Authorization: Bearer · /v1/chat/completions
      ▼
   api.groq.com  —  open-weight models on LPU hardware
```

---

## 3. Setup (about two minutes)

1. Sign up at **[console.groq.com](https://console.groq.com)** — no credit card.
2. Create an API key at **console.groq.com/keys**.
3. In `.env`:

```ini
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_key_here
LLM_MODEL=openai/gpt-oss-120b
```

4. `streamlit run app.py`

That is the whole procedure. There is no notebook, no tunnel, and nothing to
restart every three hours.

---

## 4. Model choice

Groq serves **only** open-source models, so every option here satisfies the brief.
The distinction that matters is **strict schema enforcement**.

| Model | Licence | `strict: true`? | Notes |
|---|---|---|---|
| **`openai/gpt-oss-120b`** | Apache 2.0 | ✅ | **Default.** Best quality with hard schema enforcement |
| `openai/gpt-oss-20b` | Apache 2.0 | ✅ | Faster, lighter on the token quota |
| `moonshotai/kimi-k2-instruct-0905` | Modified MIT | ✅ | Very large; slower on the free tier |
| `llama-3.3-70b-versatile` | Llama Community | ❌ | Schema is a suggestion; repair path engages |
| `qwen-*`, `gemma2-*` | Apache 2.0 / Gemma | ❌ | Same |

### The naming question, answered before it is asked

**`openai/gpt-oss-120b` is not a proprietary OpenAI model.** It is OpenAI's
open-weight release under **Apache 2.0** — downloadable, self-hostable, no usage
restrictions. The vendor prefix in the name is the only thing about it that reads
proprietary.

This is worth stating plainly because someone at Vays will see "openai/" in a
config file and object on sight. If that argument is not worth having, switching
to `llama-3.3-70b-versatile` or a Qwen model is one `.env` line — at the cost of
dropping from *guaranteed* schema-valid JSON to *usually* schema-valid, with the
engine's repair-retry covering the difference.

`supports_guided_json` is a provider property, and the application already
branches on it. Nothing breaks either way.

---

## 4a. What `strict: true` actually enforces — measured, not assumed

Verified against the live API on 2026-08-07. This differs from what the planning
docs assumed, and the difference matters:

| Schema keyword | Enforced by the decoder? |
|---|---|
| exact key set (`required` + `additionalProperties: false`) | ✅ |
| types | ✅ |
| `enum` membership | ✅ |
| `minItems` / `maxItems` on arrays | ✅ |
| **`minLength` / `maxLength` on strings** | ❌ **request is rejected outright** |

Two consequences, both handled:

1. **`required` must list every property.** Pydantic omits fields that carry a
   default, which failed the very first request:
   *"`required` … must include every key in properties: technical_facts"*.
   `core.schemas._strictify` now marks every property required.
2. **String lengths are stripped and moved to the prompt.** Structure is
   guaranteed; length is not. The prompt states each limit explicitly, the
   Pydantic model enforces it, and the engine's repair-retry covers an overshoot.

Measured with a real article: limits *omitted* from the prompt → `subject`,
`preview_text` and `cta` all overshot. Limits *stated* as
`MAXIMUM 60 characters (this is a hard limit, not a target)` → 56/60, 95/100,
21/40, valid on the first attempt.

### Live baseline (1 article, `openai/gpt-oss-120b`)

| | |
|---|---|
| Health check | ~320 ms |
| Stage 1 (summary) | ~1.7 s · 599 in / 603 out |
| Stage 2 (compose) | ~3.6 s · 737 in / 1511 out |
| **Total** | **~3,450 tokens, 5.3 s** |

For comparison, the Colab design budgeted 60–90 s for the same work.

---

## 5. Rate limits — the one real constraint

Groq's free tier is **generous on requests and tight on tokens**:

| | Free tier (approx.) |
|---|---|
| Requests/minute | ~30 |
| Requests/day | ~1,000 |
| **Tokens/minute** | **~8,000–12,000** ← the binding limit |
| Cost | ₹0, no card |

**Why that matters here.** The original input budget was 6,000 tokens per
article. Three articles plus the composition call would exceed the per-minute
ceiling before the newsletter was written, and every call would 429.

**What was changed:**

1. `SCRAPER_MAX_INPUT_TOKENS` lowered **6000 → 3000**. Articles are truncated
   lead-and-tail, which the cleaner already did well.
2. The client **honours `Retry-After`** rather than guessing at a backoff. Groq
   states how long to wait; exponential backoff either sleeps too long or retries
   too early and burns another request against the same quota.
3. A rate limit raises `LLMRateLimitedError`, **not** `LLMUnavailableError` —
   nothing is broken, and telling the user to check their connection would send
   them hunting for a fault that does not exist.

If limits become a nuisance, adding a card unlocks roughly 10× with no minimum
spend. That is a budget decision, not a code change.

---

## 6. Failure handling

| Failure | Detection | Response |
|---|---|---|
| No API key | startup | Config error naming `GROQ_API_KEY` and console.groq.com |
| Bad API key | `401` | **No retry** — retrying a rejected key wastes three timeouts and hides the cause |
| Unknown model | `404` | Message names the model *and* the endpoint; a 404 here is usually a typo'd model |
| Rate limited | `429` | Sleep per `Retry-After` (capped at 30 s), retry, then `LLMRateLimitedError` |
| Request too large | `413` | "Use fewer articles, or a shorter length" |
| Server error | `5xx` | Exponential backoff, then the circuit breaker |
| Repeated failure | 3 consecutive | Circuit opens for 60 s — fails fast instead of one timeout per article |
| Truncated output | `finish_reason=length` | Reports the token budget, not a parse error |

Drafts and extracted articles are persisted **before** any LLM call, so any of
these costs the regeneration and never the work (NFR-R1).

---

## 7. Handover — swapping the LLM

Vays replaces Groq by editing three lines:

```ini
LLM_PROVIDER=hosted
LLM_BASE_URL=https://their-llm-server
LLM_API_KEY=their-key
```

No code changes. Any server speaking the OpenAI `/v1/chat/completions` protocol
works — vLLM, TGI, Ollama, LM Studio, or another commercial endpoint.

`HostedProvider` reports `supports_guided_json = False` deliberately: an unknown
endpoint's capabilities are unknown, so the repair path stays armed. Failing safe
beats failing optimistically. If their endpoint does enforce schemas, flipping
that flag is a one-line change with a measurable payoff.

The full chapter, with verification steps and a rollback, lands in M9 as
`docs/SWAP_THE_LLM.md`.

---

## 8. What was deleted

`notebooks/colab_llm_server.ipynb` · `scripts/build_colab_notebook.py` ·
`docs/COLAB_SETUP_GUIDE.md` · `docs/04_COLAB_LLM_ARCHITECTURE.md` ·
`modules/ai/colab_provider.py` · `modules/ai/ollama_provider.py`

`LLM_PROVIDER=colab` and `=ollama` are now **rejected at startup** rather than
silently ignored, so an old `.env` fails loudly instead of behaving oddly.
