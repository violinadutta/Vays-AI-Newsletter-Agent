# Architecture Decision Records

Decisions that **changed** during the project, and why. The original locked register
is [09_FINAL_DECISIONS.md](../09_FINAL_DECISIONS.md) (D-1 … D-24) — these ADRs cover
the ones that were reversed or added under pressure, because those are the ones a
future developer will otherwise re-litigate.

Each records what was believed, what actually happened, and what it cost.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-groq-replaces-colab.md) | Groq replaces Google Colab as the LLM host | Accepted |
| [0002](0002-drop-mjml.md) | Hand-authored table HTML instead of MJML | Accepted |
| [0003](0003-two-layer-configuration.md) | Runtime settings override `.env` | Accepted |
| [0004](0004-cid-embedded-logo.md) | Embed the logo as a CID attachment | Accepted |

## Format

Short. Context (what forced the decision) → Decision → Consequences (including the
bad ones). If an ADR does not say what it cost, it is marketing.
