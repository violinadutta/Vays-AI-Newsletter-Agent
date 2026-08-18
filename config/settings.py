"""Typed, validated application configuration.

Loaded from environment variables and ``.env`` (see ``.env.example``), validated
by Pydantic at startup.

Two design decisions worth knowing about:

**Fail fast, and fail completely.** A typo in ``.env`` produces
``LLM_TEMPERATURE: input should be less than or equal to 2`` before the UI renders,
rather than a confusing model failure an hour into a campaign. And because each
section is built independently, *every* problem is reported at once — you fix the
whole file in one pass instead of discovering the next missing variable after each
restart. On a project whose most common failure mode is a stale endpoint URL, that
matters more than it looks.

**Sections are separate ``BaseSettings`` classes** with an ``env_prefix`` each.
This keeps environment variable names flat and readable (``LLM_BASE_URL``, not
``LLM__BASE_URL``) while the Python API stays grouped (``settings.llm.base_url``).
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.constants import ENV_FILE
from core.exceptions import ConfigurationError

# Shared config for every section. `extra="ignore"` is essential: each section
# sees the whole .env file and must tolerate variables belonging to other sections.
#
# `validate_assignment=True` matters because of M8: `SettingsService` edits this
# object in place so a settings change takes effect without a restart. Without
# it, Pydantic validates on construction only, and `settings.llm.temperature = 9`
# would be accepted silently — the UI would report success and the next
# generation call would fail with a 400 from the API. With it, assignment runs
# the same validators and field constraints as startup, and a rejected value
# leaves the previous one in place.
#: ``H:MM`` or ``HH:MM``, 00:00-23:59. Anchored via fullmatch at the call site.
_CLOCK_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")

#: Monday=0 … Sunday=6, matching ``date.weekday()``.
_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    """Advance a year/month pair, rolling the year over correctly."""
    index = (year * 12 + (month - 1)) + offset
    return index // 12, index % 12 + 1


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> int:
    """Day-of-month of the ``nth`` ``weekday``, clamped to the last occurrence.

    Most months have only four of any given weekday. Asking for the 5th and
    getting nothing would silently skip those months — a newsletter that fails
    to arrive with no error anywhere — so ``5`` is read as "last", which is what
    people mean by it anyway.
    """
    first_weekday = date(year, month, 1).weekday()
    first_occurrence = 1 + (weekday - first_weekday) % 7
    day = first_occurrence + (nth - 1) * 7

    days_in_month = calendar.monthrange(year, month)[1]
    while day > days_in_month:
        day -= 7
    return day


_BASE = SettingsConfigDict(
    env_file=ENV_FILE,
    env_file_encoding="utf-8",
    extra="ignore",
    protected_namespaces=(),  # allows a field literally named `model`
    validate_assignment=True,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Sections
# ─────────────────────────────────────────────────────────────────────────────
class AppSettings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(**_BASE, env_prefix="APP_")

    env: Literal["local", "dev", "staging", "prod"] = "dev"

    #: The port the dashboard serves on. Read by ``run.bat``, by the tunnel
    #: script, and by the localhost fallback for approval links — a number
    #: repeated in several places is one somebody changes in three of them.
    port: int = Field(8501, ge=1, le=65535)

    secret_key: SecretStr = Field(
        ...,
        description=(
            "Signs session tokens. Generate with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        ),
    )

    @field_validator("secret_key")
    @classmethod
    def _secret_key_is_strong(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 32:
            msg = (
                "must be at least 32 characters. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
            raise ValueError(msg)
        return value


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(**_BASE)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class LLMSettings(BaseSettings):
    """LLM provider configuration.

    **This is the seam Vays uses to swap in their own model (D-17).** Every
    provider speaks the OpenAI ``/v1/chat/completions`` protocol, so repointing
    the application is ``LLM_BASE_URL`` + ``LLM_API_KEY`` and nothing else.
    No code changes. See ``docs/SWAP_THE_LLM.md``.

    There is deliberately no ``local`` option: no model ever runs on this machine
    (D-12). ``mock`` serves offline development from JSON fixtures.
    """

    model_config = SettingsConfigDict(**_BASE, env_prefix="LLM_")

    #: ``groq``   = open-weight models on Groq's API (default, D-21)
    #: ``hosted`` = any other OpenAI-compatible endpoint — the handover path
    #: ``mock``   = fixtures; no network, no GPU
    #: No ``local``: nothing ever runs a model on this machine (D-12).
    provider: Literal["groq", "hosted", "mock"] = "groq"

    base_url: str = "https://api.groq.com/openai"

    #: Accepts ``GROQ_API_KEY`` as well as ``LLM_API_KEY`` — the former is what
    #: Groq's console tells you to call it, and silently ignoring the name a user
    #: was just handed is a needless first-run failure.
    api_key: SecretStr = Field(
        SecretStr(""), validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY")
    )

    #: Default is the largest Groq model supporting ``strict`` schema enforcement,
    #: which is what makes malformed JSON impossible rather than unlikely (D-3).
    #: ``openai/gpt-oss-120b`` is **Apache 2.0 open-weight** despite the vendor
    #: prefix in its name — it satisfies the open-source-model constraint.
    model: str = "openai/gpt-oss-120b"

    timeout_s: int = Field(120, ge=5, le=600)
    max_retries: int = Field(3, ge=0, le=10)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(2048, ge=256, le=8192)

    circuit_failure_threshold: int = Field(3, ge=1, le=20)
    circuit_reset_s: int = Field(60, ge=5, le=3600)

    @field_validator("base_url")
    @classmethod
    def _normalise_base_url(cls, value: str) -> str:
        """Strip a trailing slash and a trailing ``/v1``.

        The OpenAI client appends ``/v1`` itself. Pasting a URL that already ends
        in ``/v1`` — which is exactly what people copy from vLLM's own startup
        output — otherwise produces ``/v1/v1/chat/completions`` and a 404 that
        looks like the tunnel is broken. Cheap to normalise, expensive to debug.
        """
        cleaned = value.strip().rstrip("/")
        if cleaned.endswith("/v1"):
            cleaned = cleaned[: -len("/v1")]
        if not cleaned.startswith(("http://", "https://")):
            msg = f"must start with http:// or https:// (got {value!r})"
            raise ValueError(msg)
        return cleaned

    @property
    def requires_endpoint(self) -> bool:
        """True when this provider actually needs a reachable URL and key."""
        return self.provider != "mock"


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(**_BASE, env_prefix="EMAIL_")

    #: ``console`` writes .eml files to data/outbox and sends nothing. It is the
    #: default so that a fresh checkout cannot accidentally email real customers.
    provider: Literal["brevo", "smtp", "console"] = "console"

    sender_name: str = "Vays Infotech"
    sender_address: str = "newsletter@example.com"
    reply_to: str = ""

    batch_size: int = Field(50, ge=1, le=500)
    batch_delay_s: float = Field(2.0, ge=0.0, le=60.0)
    max_retries: int = Field(3, ge=0, le=10)

    # These live outside the EMAIL_ prefix in .env because they name a vendor.
    brevo_api_key: SecretStr = Field(SecretStr(""), validation_alias="BREVO_API_KEY")
    smtp_host: str = Field("", validation_alias="SMTP_HOST")
    smtp_port: int = Field(587, ge=1, le=65535, validation_alias="SMTP_PORT")
    smtp_username: str = Field("", validation_alias="SMTP_USERNAME")
    smtp_password: SecretStr = Field(SecretStr(""), validation_alias="SMTP_PASSWORD")
    smtp_use_tls: bool = Field(True, validation_alias="SMTP_USE_TLS")


class ScraperSettings(BaseSettings):
    model_config = SettingsConfigDict(**_BASE, env_prefix="SCRAPER_")

    timeout_s: int = Field(20, ge=1, le=120)
    max_retries: int = Field(2, ge=0, le=5)
    user_agent: str = "VaysNewsletterBot/1.0 (+https://vaysinfotech.com/bot)"
    respect_robots: bool = True
    max_concurrent: int = Field(4, ge=1, le=16)
    min_word_count: int = Field(200, ge=0, le=5000)

    #: Input budget per article. Lowered from 6000 for Groq's free tier, which is
    #: generous on requests (~30/min) and tight on **tokens** (~8–12k/min): three
    #: articles at 6k each would exceed the per-minute ceiling before the
    #: composition call even started, and every one of them would 429.
    max_input_tokens: int = Field(3000, ge=500, le=100_000)


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(**_BASE, env_prefix="DATABASE_")

    url: str = "sqlite:///./data/app.db"


class BrandSettings(BaseSettings):
    model_config = SettingsConfigDict(**_BASE, env_prefix="BRAND_")

    name: str = "Vays Infotech"
    primary_color: str = "#0B5FFF"
    logo_path: str = "assets/logo.png"
    website: str = "https://vaysinfotech.com"

    #: Legally required in marketing email in most jurisdictions (CAN-SPAM,
    #: GDPR). Empty is allowed here so the app can start and be configured
    #: through the UI; the *renderer* refuses to build an email without it.
    #: Blocking startup on a branding value would be the wrong trade — blocking
    #: the send is the right one.
    address: str = ""

    unsubscribe_base_url: str = Field(
        "https://vaysinfotech.com/unsubscribe",
        validation_alias="UNSUBSCRIBE_BASE_URL",
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Composed settings object
# ─────────────────────────────────────────────────────────────────────────────
class Settings(BaseModel):
    """The single, validated configuration object for the whole application.

    Obtain it with :func:`get_settings` — never construct it directly, or the
    aggregated error reporting is bypassed.
    """

    app: AppSettings
    agent: AgentSettings
    logging: LoggingSettings
    llm: LLMSettings
    email: EmailSettings
    scraper: ScraperSettings
    database: DatabaseSettings
    brand: BrandSettings

    @property
    def is_local(self) -> bool:
        """True in the fully offline profile: mock LLM, console email, no network."""
        return self.app.env == "local"

    @property
    def is_production(self) -> bool:
        return self.app.env == "prod"


class AgentSettings(BaseSettings):
    """The automation agent: discovery, approval, and when campaigns go out.

    **Ships disabled.** ``enabled=False`` means a fresh clone runs exactly as it
    did before — the agent is opt-in, and nothing sends autonomously until
    someone deliberately turns it on and supplies an approval address.
    """

    model_config = SettingsConfigDict(**_BASE, env_prefix="AGENT_")

    enabled: bool = False

    #: The site to watch. WordPress REST API first, RSS feed as fallback.
    blog_url: str = "https://vaysinfotech.com"
    discovery_interval_hours: int = Field(6, ge=1, le=168)

    #: Posts pulled into the pipeline per run. Deliberately small: the binding
    #: constraint is Groq's free-tier token ceiling (~8-12k/minute), and one
    #: article costs ~3,450 tokens end to end.
    max_posts_per_run: int = Field(1, ge=1, le=20)

    #: **How many newsletters may be waiting at once** — the setting that makes
    #: "one per month" actually mean one.
    #:
    #: A per-run cap alone does not: discovery runs every few hours, so a cap of
    #: one would still draft one *per run* and queue thirty in a month, all of
    #: which then send together on the same day. That is precisely what happened
    #: on the first live run.
    #:
    #: So drafting is gated on how many campaigns are already awaiting approval
    #: or approved-and-unsent. At 1, the agent writes the next newsletter only
    #: once the previous one has gone out. Discovery keeps running regardless —
    #: newly published posts are still recorded, they just wait their turn.
    max_in_flight: int = Field(1, ge=1, le=50)

    #: Attempts before a post is abandoned. A site that permanently blocks
    #: extraction must stop consuming a slot on every run forever.
    max_attempts: int = Field(3, ge=1, le=10)

    #: Daily clock time for approved campaigns, ``HH:MM`` in ``timezone``.
    send_time: str = "11:00"

    #: ``monthly`` sends on the Nth given weekday of each month — the newsletter
    #: cadence Vays actually runs. ``daily`` sends at ``send_time`` every day,
    #: kept because it is the obvious thing to want during testing and costs one
    #: branch to support.
    send_schedule: Literal["monthly", "daily"] = "monthly"

    #: Which weekday, and which occurrence of it. Together with ``send_time``
    #: these read as "the 3rd Wednesday of the month at 11:00".
    send_weekday: Literal[
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ] = "wednesday"
    #: 1st through 4th, or 5th meaning **last** — most months have no 5th
    #: Wednesday, and silently skipping those months would be a scheduling bug
    #: nobody notices until a newsletter does not arrive.
    send_week_of_month: int = Field(3, ge=1, le=5)

    #: IANA name. Windows ships no system timezone database, which is why
    #: ``tzdata`` is a declared dependency.
    timezone: str = "Asia/Kolkata"

    #: Where the approval request goes. The agent refuses to run without it —
    #: generating drafts nobody is asked to approve would silently pile up work.
    approval_email: str = ""
    approval_token_ttl_hours: int = Field(72, ge=1, le=720)

    #: Where this application is reachable *from the approver's machine*.
    #:
    #: ``auto`` asks the locally running ngrok agent for its current public URL
    #: each time an approval email is composed — which is what makes a rotating
    #: free-tier tunnel survivable.
    #:
    #: **This is the production switch.** Point it at the real hostname once the
    #: app is hosted and nothing else changes: no code, no redeploy, and links
    #: already in flight keep resolving because the value is read per email.
    #:
    #: Empty means "use localhost on the configured port" — correct on this
    #: machine, unusable from anywhere else, which is why it is not the thing to
    #: leave set once anyone else has to approve.
    app_base_url: str = "auto"

    #: Directory scanned for the recipient CSV; the newest file wins.
    recipients_dir: str = "data/recipients"

    @field_validator("send_time")
    @classmethod
    def _valid_clock_time(cls, value: str) -> str:
        """Accept ``H:MM`` or ``HH:MM``, and store the zero-padded form.

        A regex rather than ``time.fromisoformat``, which is wrong in both
        directions for a settings box someone types into: it rejects ``9:00``,
        which is a perfectly reasonable thing to write, and silently accepts
        ``24:00`` as midnight — turning "send at the end of the day" into "send
        at the start of it".

        Rejected at configuration time rather than at 09:00 on the morning a
        campaign was due. A malformed send time discovered by the scheduler is
        discovered by nobody, because nobody is watching when it fires.
        """
        cleaned = value.strip()
        match = _CLOCK_TIME.fullmatch(cleaned)
        if match is None:
            msg = f"must be a 24-hour clock time between 00:00 and 23:59 (got {value!r})"
            raise ValueError(msg)
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        cleaned = value.strip()
        try:
            ZoneInfo(cleaned)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = (
                f"{value!r} is not a known IANA timezone (e.g. Asia/Kolkata, UTC). "
                "On Windows this also requires the tzdata package."
            )
            raise ValueError(msg) from exc
        return cleaned

    @field_validator("blog_url")
    @classmethod
    def _normalise_blog_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            msg = f"must start with http:// or https:// (got {value!r})"
            raise ValueError(msg)
        return cleaned

    @field_validator("app_base_url")
    @classmethod
    def _normalise_app_url(cls, value: str) -> str:
        """As above, but ``auto`` is also allowed — it is resolved at send time."""
        cleaned = value.strip().rstrip("/")
        if cleaned.lower() == "auto":
            return "auto"
        if not cleaned.startswith(("http://", "https://")):
            msg = f"must be a URL starting with http:// or https://, or 'auto' (got {value!r})"
            raise ValueError(msg)
        return cleaned

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def clock(self) -> tuple[int, int]:
        """``send_time`` as (hour, minute)."""
        hour, minute = self.send_time.split(":")
        return int(hour), int(minute)

    def next_send_after(self, now: datetime) -> datetime:
        """The next moment a campaign approved at ``now`` may be sent.

        The **next occurrence strictly after** ``now`` — never the same instant.
        On the monthly schedule that means approving at 11:05 on the 3rd
        Wednesday waits for next month, which is the conservative reading and
        the one that cannot surprise a customer. In practice approval happens
        days before the send date, so the case is rare.

        Args:
            now: Timezone-aware. A naive value is rejected rather than assumed
                to be local: guessing produces an error that surfaces only as
                mail sent at the wrong hour.
        """
        if now.tzinfo is None:
            msg = "next_send_after requires a timezone-aware datetime"
            raise ValueError(msg)

        local = now.astimezone(self.zone)
        hour, minute = self.clock

        if self.send_schedule == "daily":
            slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return slot if slot > local else slot + timedelta(days=1)

        # Monthly: this month's occurrence if it is still ahead, otherwise next
        # month's. Two candidates are enough — an occurrence always exists in
        # every month, because the 5th is clamped to the last one.
        for offset in (0, 1):
            year, month = _add_months(local.year, local.month, offset)
            day = _nth_weekday_of_month(
                year, month, _WEEKDAYS[self.send_weekday], self.send_week_of_month
            )
            slot = datetime(year, month, day, hour, minute, tzinfo=self.zone)
            if slot > local:
                return slot

        # Unreachable: next month's occurrence is always after any instant in
        # this one. Kept as a guard rather than an assertion so a future edit to
        # the loop above cannot silently return None.
        msg = "could not compute the next send window"
        raise ValueError(msg)

    def describe_schedule(self) -> str:
        """The schedule in words, for the dashboard and the approval email."""
        if self.send_schedule == "daily":
            return f"every day at {self.send_time} ({self.timezone})"
        ordinal = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "last"}[self.send_week_of_month]
        return (
            f"the {ordinal} {self.send_weekday.capitalize()} of each month "
            f"at {self.send_time} ({self.timezone})"
        )

    def is_send_window_open(self, now: datetime, approved_at: datetime) -> bool:
        """Whether a campaign approved at ``approved_at`` may go out at ``now``."""
        return now.astimezone(self.zone) >= self.next_send_after(approved_at)


SECTIONS: tuple[tuple[str, type[BaseSettings]], ...] = (
    ("app", AppSettings),
    ("agent", AgentSettings),
    ("logging", LoggingSettings),
    ("llm", LLMSettings),
    ("email", EmailSettings),
    ("scraper", ScraperSettings),
    ("database", DatabaseSettings),
    ("brand", BrandSettings),
)


def env_var_name(section: type[BaseSettings], field_name: str) -> str:
    """Best-effort reconstruction of the environment variable behind a field.

    Error messages must name the variable the user has to edit — ``base_url``
    is not something they can find in ``.env``, but ``LLM_BASE_URL`` is. The
    Settings page uses it for the same reason.
    """
    field = section.model_fields.get(field_name)
    alias = getattr(field, "validation_alias", None) if field else None
    if isinstance(alias, str):
        return alias
    prefix = section.model_config.get("env_prefix", "")
    return f"{prefix}{field_name}".upper()


def _describe_errors(section: type[BaseSettings], exc: ValidationError) -> list[str]:
    """Render Pydantic validation errors as actionable, env-var-named lines."""
    lines: list[str] = []
    for error in exc.errors():
        field_name = str(error["loc"][0]) if error["loc"] else "<unknown>"
        var = env_var_name(section, field_name)
        if error["type"] == "missing":
            lines.append(f"  {var} is required but not set")
        else:
            lines.append(f"  {var}: {error['msg']}")
    return lines


class _Unset:
    """Sentinel: 'use the default .env file'. Distinct from ``None``, which means
    'read no dotenv file at all'."""


_UNSET = _Unset()


def build_settings(env_file: Path | None | _Unset = _UNSET) -> Settings:
    """Build and validate settings, uncached.

    Args:
        env_file: Path to a dotenv file. Defaults to the project's ``.env``.
            Pass ``None`` to read **only** real environment variables — which is
            what the test suite does, so a developer's local ``.env`` can never
            change a test result. Without this, tests pass on the machine that
            has a ``.env`` and fail in CI, which is the worst kind of flake.

    Returns:
        The validated :class:`Settings`.

    Raises:
        ConfigurationError: If any section fails validation. The message lists
            **every** problem found across **all** sections, each named by its
            environment variable, so the whole file can be fixed in one pass.
    """
    kwargs: dict[str, object] = {} if isinstance(env_file, _Unset) else {"_env_file": env_file}

    parts: dict[str, BaseSettings] = {}
    problems: list[str] = []

    for name, section_cls in SECTIONS:
        try:
            parts[name] = section_cls(**kwargs)  # type: ignore[arg-type]
        except ValidationError as exc:
            problems.extend(_describe_errors(section_cls, exc))

    if problems:
        detail = "\n".join(problems)
        env_hint = (
            f"Checked: {ENV_FILE}"
            if ENV_FILE.exists()
            else f"No .env file found at {ENV_FILE}. Copy .env.example to .env first."
        )
        raise ConfigurationError(
            f"Configuration is invalid:\n{detail}\n\n{env_hint}",
            user_message=(
                "The application isn't configured correctly:\n\n"
                f"{detail}\n\nFix these in your .env file and restart."
            ),
            context={"problem_count": len(problems)},
        )

    return Settings(**parts)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application settings, cached for the process lifetime.

    This is the accessor every module should use: the ``.env`` file is read once
    and all callers share one validated instance.
    """
    return build_settings()


def reset_settings_cache() -> None:
    """Clear the cached settings.

    For tests only — production code must not reload configuration at runtime,
    because half the application would still be holding the old values.
    """
    get_settings.cache_clear()
