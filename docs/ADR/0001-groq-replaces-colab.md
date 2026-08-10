# ADR 0001 — Groq replaces Google Colab as the LLM host

**Status:** Accepted · **Date:** 2026-08-07 · **Supersedes:** D-17

## Context

The brief required an **open-source model** and the development machine (Windows 11,
8 GB RAM, no GPU) could not run inference. Vays provided no hosted LLM and no cloud
budget. Colab's free GPU tier appeared to be the only option, and D-17 shipped the
project defaulting to `LLM_PROVIDER=colab` with a tunnelled endpoint.

**It was attempted twice on real hardware and never generated a single token.**

1. `pip` resolved a CUDA-13 build of PyTorch against Colab's CUDA-12 runtime →
   `ImportError: libcudart.so.13`. Fixed by switching to `uv --torch-backend=auto`.
2. The next attempt entered an install/restart loop — the kernel restarted three
   times and never reached a serving state. My own `os.kill(os.getpid(), 9)` in the
   setup notebook was part of the cause.

Both were fixable. The **shape** was not:

- ~3-hour session limit, after which everything stops mid-campaign
- A tunnel URL that rotates on every restart, so the config is never stable
- No guaranteed GPU allocation
- A shifting CUDA/torch compatibility matrix outside our control
- A standing tension with Colab's terms, which do not contemplate serving an API

A handover deliverable whose first instruction is "open a notebook and wait for a
GPU" is not a deliverable.

## Decision

**Use Groq.** Open-weight models over an ordinary HTTPS API.

Default `openai/gpt-oss-120b` — Apache 2.0 open-weight despite the vendor prefix.
Delete all Colab code, notebooks and documentation; make `LLM_PROVIDER=colab` fail at
startup rather than linger as a broken option.

This satisfies the constraint **more** cleanly, not less: Groq serves *only*
open-source models, so "the model is open source" is guaranteed by the host's
catalogue rather than by our discipline. D-2 already established that the constraint
binds the model, not the hosting.

## Consequences

**Cost of the switch: one small class and a config default.** The strongest evidence
the `LLMProvider` seam was worth building.

Good:
- No sessions, no tunnel, no GPU lottery. Measured **3,450 tokens / 5.3 s** for a
  full two-stage run against a Colab design budget of 60–90 s
- `strict: true` structured outputs make malformed JSON impossible (D-3 becomes a
  guarantee rather than a hope)
- The handover story improves: a free API key replaces a notebook

Bad:
- **A new binding constraint: ~8–12k tokens/minute on the free tier.** Three long
  articles can exceed it. Mitigated by a 3000-token input budget per article,
  `Retry-After` handling, and capped stage-1 concurrency — but it is now the first
  thing users hit (see KNOWN_ISSUES § 1.1)
- Dependence on a third-party API where there was previously none
- `openai/gpt-oss-120b` **looks** proprietary and is not; this needs explaining to
  anyone reviewing the open-source constraint

Two schema incompatibilities surfaced only on the first real call, invisible to the
mock provider: `required` must list every property, and `minLength`/`maxLength` are
rejected by strict mode. Both fixed. **The lesson recorded here is that a fixture
provider cannot validate a wire protocol.**
