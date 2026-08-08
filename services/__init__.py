"""Service layer — the use cases, the verbs of the product.

Each service maps to something a user would recognise doing: ingest articles,
generate a newsletter, send a campaign. Services own transaction boundaries and
orchestration.

**Nothing in this package may import Streamlit.** That rule is enforced by an
``import-linter`` contract, and it is what allows the same code to run later
behind FastAPI or inside a background worker with no changes.

Populated across M2–M6.
"""
