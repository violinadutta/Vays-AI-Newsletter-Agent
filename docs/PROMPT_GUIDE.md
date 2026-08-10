# Prompt Guide

Changing what the AI writes, without breaking reproducibility.

---

## Where prompts live

```
prompts/
├── article_summary/      v1.0.0.yaml          stage 1 — extractive
├── newsletter_compose/   v1.0.0, v1.1.0.yaml  stage 2 — generative
├── field_regenerate/     v1.0.0, v1.1.0.yaml  one field at a time
├── subject_variants/     v1.0.0.yaml          alternative subject lines
└── _shared/
    ├── system_persona.md      who the model is
    ├── human_voice.md         how not to sound generated
    ├── length_rules.md        word counts per preset
    ├── tone/*.md              5 tones
    └── audience/*.md          4 audiences
```

Plain YAML with Jinja2 includes. Git-native, reviewable in a pull request, no infra.

---

## The one rule

**Never edit a published version.**

Every campaign records the prompt version that produced it (D-6). Editing `v1.1.0`
in place means a campaign claiming to be reproducible no longer is — you would get
different copy from the same recorded inputs, with nothing to explain why.

To change a prompt:

```powershell
copy prompts\newsletter_compose\v1.1.0.yaml prompts\newsletter_compose\v1.2.0.yaml
notepad prompts\newsletter_compose\v1.2.0.yaml   # bump `version:` inside the file
.venv\Scripts\python.exe scripts\validate_prompts.py
```

`latest` resolves to the highest semantic version automatically. Old versions keep
resolving for campaigns that recorded them.

---

## Anatomy

```yaml
name: newsletter_compose
version: "1.1.0"
description: >
  What this does, and what changed from the previous version — including why.
created: 2026-08-08
model_tested: openai/gpt-oss-120b

defaults:
  temperature: 0.85
  top_p: 0.95
  max_tokens: 4096        # must be >= 2048; see below

required_context:         # validated before rendering; a missing key fails loudly
  - summaries
  - tone
  - audience_label
  - length_words
  - brand_name

output_schema: newsletter # the Pydantic model the response is validated against

system: |
  {% include "_shared/system_persona.md" %}
  {% include "_shared/human_voice.md" %}
  {% include "_shared/tone/" + tone + ".md" %}

user: |
  Write a customer newsletter for {{ brand_name }} ...
```

`max_tokens` must be **≥ 2048**. gpt-oss models emit internal reasoning tokens that
count against the budget but never appear in the response; a budget sized to the
visible output gets cut off mid-JSON, and the API reports that as
`json_validate_failed` rather than a length finish. The validator enforces the floor.

---

## What was learned the hard way

These are measured findings, not style preferences.

### Hard limits work; "approximately" does not

Without explicit limits, `subject`, `preview_text` and `cta` overshot **every time**.
With:

```
MAXIMUM 60 characters (this is a hard limit, not a target)
```

they landed in range on the first attempt. Note that `strict` mode does **not**
enforce string lengths — the API rejects `minLength`/`maxLength`. Length is carried
entirely by prompt wording, checked by Pydantic, repaired on retry.

### Asking for formatting the field cannot carry produces literal markup

`v1.0.0` said *"Give each story a short bold heading."* The field is plain text, so
the model reached for `**Heading**` — and those asterisks reached the customer.

**Measured, same input:** v1.0.0 produced **10** asterisks; v1.1.0 produced **0**.

If you want emphasis, say what the field is and let the renderer handle it. The
renderer converts a whitelist of markdown to real HTML as a safety net, but a prompt
that does not ask for markdown is the actual fix.

### "Be natural" does nothing; a list of tells works

`human_voice.md` enumerates the specific signals — uniform sentence length,
signposting, the rule of three, decorative hedging, "delve/leverage/seamless",
em-dash overuse — rather than asking for a quality.

Temperature was raised 0.7 → 0.85 at the same time. The banned-construction list is
what stops the extra variance wandering; do not raise temperature without it.

### Synthetic fixtures do not exercise token budgets

`article_summary` at 1024 tokens passed on a synthetic article and failed on a real
1,100-word one. **Test prompt changes against real articles.**

---

## Editing safely

### Tone and audience fragments

`_shared/tone/*.md` and `_shared/audience/*.md` are shared by every prompt version.
**Changing one changes the behaviour of already-published versions**, which is the
one place the versioning guarantee is thinner than it looks.

For a small wording fix, that is usually acceptable. For a change in register, add a
new fragment and reference it from a new prompt version instead.

### Validate before running

```powershell
.venv\Scripts\python.exe scripts\validate_prompts.py
```

Checks that every prompt renders with representative context, that
`required_context` is complete, that the schema exists, and that `max_tokens` clears
the floor. Runs in CI.

### Test against a real article

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

Generate from a genuine OEM blog URL and read the output. Compare against the
previous version — an A/B on the same input is the only honest measure.

---

## Adding a prompt

1. `prompts/<name>/v1.0.0.yaml`
2. Add the response model to `core/models.py` and register it in `core/schemas.py`
3. Call it through `AIEngine`, which handles rendering, schema enforcement,
   validation and the repair path

The registry discovers prompts from the directory — no registration list to update.

---

## Grounding and hallucination

The controls, in the order they apply:

1. **Two stages** (D-4) — stage 1 extracts facts; stage 2 composes from those facts
   rather than from raw article text
2. **`technical_facts`** — forcing verifiable details to be enumerated separately
   makes a fabrication conspicuous to the reviewer instead of buried in prose
3. **`Use ONLY facts from the stories above`**, stated in the prompt
4. **A banned-exaggeration list** in the persona
5. **Human review** — the actual control

If you weaken any of the first four, the fifth carries more load. Do not remove it.
