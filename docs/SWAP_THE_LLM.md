# Swapping the LLM

**This is the handover path.** The project runs on Groq because that was the only
way to satisfy "open-source model, zero local inference, no budget" during
development. Vays will want their own model. This is how.

**It is a configuration change. There is no code to write.**

---

## The short version

```ini
LLM_PROVIDER=hosted
LLM_BASE_URL=https://your-endpoint.vaysinfotech.com
LLM_API_KEY=your-key
LLM_MODEL=your-model-name
```

Restart. Done.

Or without touching a file: **Settings → AI Service**, change provider, endpoint and
model, click **Test connection**. Applies immediately, no restart. Only the API key
needs `.env`, because secrets never enter the database (D-19).

---

## Why it is this easy

Every provider speaks the **OpenAI `/v1/chat/completions` protocol**. It is the de
facto standard — vLLM, Ollama, TGI, Together, Fireworks, Anyscale, LM Studio, Azure
OpenAI and Groq all implement it.

The application depends on the `LLMProvider` interface, never on a concrete class.
Selection happens in exactly one place, `modules/ai/factory.py`.

**This was proven once, under pressure.** Migrating from Colab to Groq — a different
host, different auth, different failure modes — cost **one small class and a config
default**. That is the strongest evidence the seam is real.

---

## What to check before you switch

### 1. Does the model support strict structured output?

This matters more than anything else on the page.

**With** `strict: true`, generation is constrained to the JSON schema at the decoding
layer — malformed JSON is impossible.

**Without** it, the app falls back to a repair-retry: it asks the model to fix its
own output. Slower, and occasionally it fails.

| | |
|---|---|
| Supported today | `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `moonshotai/kimi-k2-instruct-0905` |
| vLLM | Supports guided decoding — check your version's flag |
| Most self-hosted | Usually not; repair-retry applies |

Settings → AI Service warns when the configured model lacks it. To declare support
for a new one, add it to `STRICT_SCHEMA_MODELS` in `modules/ai/groq_provider.py`.

> **What `strict` actually enforces:** structure — keys, types, enums, array bounds.
> **Not** string lengths. `minLength`/`maxLength` are rejected by the API and stripped
> from the wire schema. Length limits are carried by prompt wording, validated by
> Pydantic, and repaired on retry. This was measured, not assumed.

### 2. Is it an open-weight model?

The founding constraint: **the model must be open source.** `openai/gpt-oss-120b` is
Apache 2.0 open-weight despite the vendor prefix in its name.

Switching to a proprietary model would satisfy the code and break the brief. Qwen,
Llama, Mistral and DeepSeek families all qualify.

### 3. Token budget

`max_tokens` must be **at least 2048**. gpt-oss models emit internal reasoning tokens
that count against the budget but never appear in the response — a budget sized to
the visible output gets cut off mid-JSON, and the API reports that as a schema
failure rather than a length finish. `scripts/validate_prompts.py` enforces the floor.

---

## Common targets

### Self-hosted vLLM

```bash
vllm serve Qwen/Qwen2.5-72B-Instruct --api-key your-key --port 8000
```

```ini
LLM_PROVIDER=hosted
LLM_BASE_URL=http://your-server:8000
LLM_API_KEY=your-key
LLM_MODEL=Qwen/Qwen2.5-72B-Instruct
```

> Do **not** include `/v1` in `LLM_BASE_URL` — the client appends it. Pasting the URL
> vLLM prints at startup produces `/v1/v1/chat/completions` and a 404 that looks like
> the server is down. The setting strips a trailing `/v1` automatically, but knowing
> why saves an hour.

### Azure OpenAI

Deployment name goes in `LLM_MODEL`; the endpoint is your resource URL. Note that
Azure serves proprietary models — allowed by the code, contrary to the brief.

### Another OpenAI-compatible host

Together, Fireworks, Anyscale, OpenRouter: `LLM_PROVIDER=hosted`, their base URL and
key. All tested protocols, none tested by this project.

---

## Verifying the switch

In order. Each step isolates a different failure.

1. **Settings → AI Service → Test connection.** Confirms reachability and auth.
   Reports latency and the model name.
2. **Generate a newsletter from one real article.** Confirms both prompt stages and
   schema compliance.
3. **Check `strict` support** — the warning banner tells you. If it is absent, watch
   the logs for `llm.repair_attempt`.
4. **Run the suite:** `pytest -m "not network"`. Should stay green; it uses the mock
   provider and is unaffected by the endpoint.
5. **Watch the first few generations** in Logs, filtered to `llm.*`.

---

## If it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Connection refused | Endpoint unreachable | Check URL, port, firewall |
| 401 / 403 | Bad key | Check `LLM_API_KEY`; some hosts need a `Bearer` prefix the client adds itself |
| 404 on `/chat/completions` | `/v1` doubled | Remove `/v1` from `LLM_BASE_URL` |
| 400 `json_validate_failed` | Response truncated mid-JSON | Raise `max_tokens` to 4096 |
| "model not found" | Wrong identifier | Use the exact string the host expects — usually `org/model` |
| Repair retries in the logs | No strict support | Expected. Add to `STRICT_SCHEMA_MODELS` if the model does support it |
| 429s | Rate limited | The app honours `Retry-After`. Reduce articles per run |

---

## Reverting

Set `LLM_PROVIDER=groq` and restore the key. Or in Settings → AI Service, **Revert**
next to each changed field — that restores the `.env` value.

Nothing else changed, so nothing else needs undoing.

---

## Working offline

```ini
LLM_PROVIDER=mock
```

Deterministic JSON fixtures from `modules/ai/fixtures/`. The entire pipeline runs
with no network, no key and no cost — useful for UI work, demos and CI. There is
deliberately **no local-inference adapter** (D-12): nothing ever loads a model on the
machine running this app, and CI fails the build if a dependency tries.
