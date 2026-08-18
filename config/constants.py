"""Project-wide constants and filesystem paths.

Everything here is a compile-time constant: values that are the same on every
machine and are not user-configurable. Anything an operator might reasonably
want to change belongs in :mod:`config.settings` (``.env``) or in the ``settings``
database table instead.

All paths are derived from :data:`PROJECT_ROOT` via :mod:`pathlib`, so the app
behaves identically regardless of the working directory Streamlit is launched
from — which on Windows is frequently not the project root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# ─────────────────────────────────────────────────────────────────────────────
#  Paths
# ─────────────────────────────────────────────────────────────────────────────
# config/constants.py -> config/ -> project root
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

ENV_FILE: Final[Path] = PROJECT_ROOT / ".env"
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"
LOGS_DIR: Final[Path] = PROJECT_ROOT / "logs"
UPLOADS_DIR: Final[Path] = DATA_DIR / "uploads"
EXPORTS_DIR: Final[Path] = DATA_DIR / "exports"
OUTBOX_DIR: Final[Path] = DATA_DIR / "outbox"  # console email provider writes .eml here
PROMPTS_DIR: Final[Path] = PROJECT_ROOT / "prompts"
TEMPLATES_DIR: Final[Path] = PROJECT_ROOT / "templates"
EMAIL_TEMPLATES_DIR: Final[Path] = TEMPLATES_DIR / "email"
#: Operational mail (approval requests). Deliberately NOT under email/, which is
#: scanned by `TemplateRenderer.list_templates()` to build the newsletter layout
#: picker — an internal template offered there would be selectable as a campaign
#: layout and would fail to render, because it needs entirely different context.
INTERNAL_TEMPLATES_DIR: Final[Path] = TEMPLATES_DIR / "internal"
ASSETS_DIR: Final[Path] = PROJECT_ROOT / "assets"

LOG_FILE: Final[Path] = LOGS_DIR / "app.jsonl"

#: Directories created on startup if absent. Deliberately excludes PROMPTS_DIR
#: and TEMPLATES_DIR — those ship with the repository, and silently creating an
#: empty one would turn "the deployment is broken" into a confusing runtime error.
RUNTIME_DIRS: Final[tuple[Path, ...]] = (
    DATA_DIR,
    LOGS_DIR,
    UPLOADS_DIR,
    EXPORTS_DIR,
    OUTBOX_DIR,
)


def ensure_runtime_dirs() -> None:
    """Create the runtime directories if they do not exist.

    Called once at application startup. Safe to call repeatedly.
    """
    for directory in RUNTIME_DIRS:
        directory.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Content limits
# ─────────────────────────────────────────────────────────────────────────────
MAX_URLS_PER_BATCH: Final[int] = 10

#: Enforced by the JSON schema at generation time (guided decoding), so the model
#: physically cannot emit a longer value — these are not post-hoc checks.
SUBJECT_MAX_LENGTH: Final[int] = 60
PREVIEW_TEXT_MAX_LENGTH: Final[int] = 100
TITLE_MAX_LENGTH: Final[int] = 120
CTA_MAX_LENGTH: Final[int] = 40
KEYWORDS_MIN: Final[int] = 3
KEYWORDS_MAX: Final[int] = 8

#: Length presets offered in the UI, in approximate words.
LENGTH_PRESET_WORDS: Final[dict[str, int]] = {"short": 150, "medium": 300, "long": 500}

# ─────────────────────────────────────────────────────────────────────────────
#  Token estimation (D-14)
# ─────────────────────────────────────────────────────────────────────────────
#: Average characters per token for English prose under Qwen3's tokenizer.
#:
#: This is a heuristic, and deliberately so. We rejected `transformers`
#: (~2 GB of torch) and `tiktoken` (downloads BPE files at runtime, and is the
#: wrong tokenizer for Qwen anyway) because the actual requirement is "don't
#: overflow the context window", which needs a ±10% estimate — not an exact
#: count. This constant is calibrated once in M2 against real article text and
#: is what keeps every ML runtime out of requirements.txt.
CHARS_PER_TOKEN: Final[float] = 3.7

#: Safety margin applied to the estimate before comparing against the budget.
#: Under-estimating costs a rejected request; over-estimating costs a few words
#: of an article. The asymmetry justifies rounding against ourselves.
TOKEN_ESTIMATE_SAFETY_FACTOR: Final[float] = 1.15

# ─────────────────────────────────────────────────────────────────────────────
#  Networking
# ─────────────────────────────────────────────────────────────────────────────
HTTP_MAX_REDIRECTS: Final[int] = 3
HTTP_MAX_RESPONSE_BYTES: Final[int] = 10 * 1024 * 1024  # 10 MB — a blog post is never this big

#: Health-check results are cached for this long. Streamlit re-executes the whole
#: script on every interaction, so an uncached probe would flood the endpoint with
#: requests from a single user clicking around.
HEALTH_CHECK_CACHE_S: Final[int] = 30

# ─────────────────────────────────────────────────────────────────────────────
#  Retention
# ─────────────────────────────────────────────────────────────────────────────
LOG_RETENTION_DAYS: Final[int] = 90
LOG_FILE_MAX_BYTES: Final[int] = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT: Final[int] = 5
