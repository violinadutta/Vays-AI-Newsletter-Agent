"""Runtime-adjustable settings: read, validate, persist, and apply without a restart.

This is what turns the app from "operable by the developer" into "operable by
the marketing team" (M8). Until now, changing the LLM endpoint meant editing
``.env`` and restarting — fine for me, useless for the person who actually runs
campaigns, and a support call for whoever inherits this.

Three properties hold, and each is enforced by a mechanism rather than a
convention:

**Secrets are unreachable from here (D-19).** The editable registry is checked at
import time against the Pydantic field annotations: if any registered field is a
``SecretStr``, the module refuses to import. A future edit that adds
``llm.api_key`` to the registry fails the build, not review. API keys and
passwords stay in ``.env``, where they are not in every database backup.

**A saved value is a validated value.** Writes go through attribute assignment on
the live settings object, which — because ``validate_assignment=True`` — runs the
same validators as startup. A rejected value leaves the old one in place, so the
application is never left holding a configuration that would not have booted.
The value *persisted* is read back off the model afterwards, so normalisation
(``https://host/v1`` → ``https://host``) is stored, not the raw input. Otherwise
the database and the running process would disagree about what is configured,
which is precisely the class of bug this page exists to diagnose.

**Changes take effect immediately.** The settings object is mutated in place, not
rebuilt, so every existing holder of it sees the new value. This works because
providers are constructed per operation (``create_provider()`` reads
``get_settings()`` on each call) rather than held as singletons — if that ever
changes, this guarantee changes with it.

``.env`` remains the source of truth for anything not overridden here, and
"Reset to .env" restores that. The two-layer arrangement is deliberate: a
handover install is configured by a file the next developer can read in Git, and
day-to-day adjustments do not require one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings

from config import get_logger, get_settings, set_log_level
from config.settings import SECTIONS, Settings, env_var_name
from core.exceptions import NewsletterAppError, ValidationError
from modules.repository.database import unit_of_work
from modules.repository.settings_repo import SettingsRepository

log = get_logger(__name__)

Kind = Literal["text", "choice", "int", "float", "bool"]


# ─────────────────────────────────────────────────────────────────────────────
#  What may be edited
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EditableSetting:
    """One setting exposed to the Settings page.

    ``key`` doubles as the database key, so it must never be renamed without a
    migration — an orphaned row would silently stop applying.
    """

    key: str
    section: str
    field: str
    label: str
    help: str
    kind: Kind = "text"
    group: str = "General"

    @property
    def env_var(self) -> str:
        """The ``.env`` variable behind this field, for the UI to name."""
        return env_var_name(_SECTION_TYPES[self.section], self.field)


def _s(section: str, field: str, label: str, help_: str, kind: Kind, group: str) -> EditableSetting:
    return EditableSetting(
        key=f"{section}.{field}",
        section=section,
        field=field,
        label=label,
        help=help_,
        kind=kind,
        group=group,
    )


#: The whitelist. Anything absent is not editable at runtime — either because it
#: is a secret (D-19), or because changing it mid-process would leave the
#: application in a state it could not have booted into. ``database.url`` is the
#: clearest example of the latter: the engine is bound at startup, so a new URL
#: would be accepted and then ignored, which is worse than refusing it.
EDITABLE: tuple[EditableSetting, ...] = (
    # ── AI ───────────────────────────────────────────────────────────────────
    _s("llm", "provider", "Provider", "Which LLM backend to call.", "choice", "AI"),
    _s("llm", "model", "Model", "Model identifier as the provider names it.", "text", "AI"),
    _s(
        "llm",
        "base_url",
        "Endpoint URL",
        "The OpenAI-compatible base URL. A trailing /v1 is stripped automatically.",
        "text",
        "AI",
    ),
    _s(
        "llm",
        "temperature",
        "Temperature",
        "Higher is more varied. 0.7 is a good default.",
        "float",
        "AI",
    ),  # noqa: E501
    _s(
        "llm",
        "max_tokens",
        "Max output tokens",
        "Must be at least 2048: gpt-oss spends invisible reasoning tokens from this "
        "same budget, and a truncated response is reported as a schema failure.",
        "int",
        "AI",
    ),
    _s("llm", "timeout_s", "Timeout (seconds)", "Per-request timeout.", "int", "AI"),
    _s("llm", "max_retries", "Retries", "Attempts after a retryable failure.", "int", "AI"),
    # ── Email ────────────────────────────────────────────────────────────────
    _s(
        "email",
        "provider",
        "Provider",
        "'console' writes .eml files to data/outbox and sends nothing.",
        "choice",
        "Email",
    ),
    _s("email", "sender_name", "Sender name", "Shown as the From name.", "text", "Email"),
    _s(
        "email",
        "sender_address",
        "Sender address",
        "Must be on a domain with SPF and DKIM configured, or mail lands in spam.",
        "text",
        "Email",
    ),
    _s("email", "reply_to", "Reply-to", "Optional. Where replies go.", "text", "Email"),
    _s("email", "batch_size", "Batch size", "Recipients per batch.", "int", "Email"),
    _s("email", "batch_delay_s", "Batch delay (s)", "Pause between batches.", "float", "Email"),
    _s(
        "email", "smtp_host", "SMTP host", "Only used when the provider is 'smtp'.", "text", "Email"
    ),  # noqa: E501
    _s(
        "email", "smtp_port", "SMTP port", "587 for STARTTLS, 465 for implicit TLS.", "int", "Email"
    ),  # noqa: E501
    _s("email", "smtp_username", "SMTP username", "The password stays in .env.", "text", "Email"),
    _s(
        "email",
        "smtp_use_tls",
        "Use TLS",
        "Leave on unless the server cannot do TLS.",
        "bool",
        "Email",
    ),  # noqa: E501
    # ── Brand ────────────────────────────────────────────────────────────────
    _s("brand", "name", "Brand name", "Appears in the email header and footer.", "text", "Brand"),
    _s("brand", "primary_color", "Primary colour", "Hex, e.g. #0B5FFF.", "text", "Brand"),
    _s("brand", "website", "Website", "Linked from the logo.", "text", "Brand"),
    _s(
        "brand",
        "address",
        "Postal address",
        "Legally required in marketing email. Sending is blocked while this is empty.",
        "text",
        "Brand",
    ),
    _s(
        "brand",
        "unsubscribe_base_url",
        "Unsubscribe URL",
        "Every marketing email must carry one.",
        "text",
        "Brand",
    ),
    # ── Content ──────────────────────────────────────────────────────────────
    _s(
        "scraper",
        "max_input_tokens",
        "Input budget per article",
        "Kept low for Groq's free tier, which is tight on tokens per minute. "
        "Raise it on a paid tier.",
        "int",
        "Content",
    ),
    _s(
        "scraper",
        "min_word_count",
        "Minimum article words",
        "Shorter extractions are refused.",
        "int",
        "Content",
    ),  # noqa: E501
    _s("scraper", "timeout_s", "Fetch timeout (s)", "Per-URL timeout.", "int", "Content"),
    _s(
        "scraper",
        "respect_robots",
        "Respect robots.txt",
        "Turning this off is your legal call.",
        "bool",
        "Content",
    ),  # noqa: E501
    _s(
        "scraper",
        "max_concurrent",
        "Concurrent fetches",
        "Higher is faster and ruder.",
        "int",
        "Content",
    ),  # noqa: E501
    # ── Operations ───────────────────────────────────────────────────────────
    _s(
        "logging",
        "log_level",
        "Log level",
        "DEBUG is verbose; INFO is the norm.",
        "choice",
        "Operations",
    ),  # noqa: E501
)

_SECTION_TYPES: dict[str, type[BaseSettings]] = dict(SECTIONS)
_BY_KEY: dict[str, EditableSetting] = {item.key: item for item in EDITABLE}


def _choices(item: EditableSetting) -> tuple[str, ...] | None:
    """Allowed values for a ``Literal``-typed field, for rendering a dropdown."""
    annotation = _SECTION_TYPES[item.section].model_fields[item.field].annotation
    if get_origin(annotation) is Literal:
        return tuple(str(value) for value in get_args(annotation))
    return None


def bounds(item: EditableSetting) -> tuple[float | None, float | None]:
    """The ``ge``/``le`` constraints on a numeric field, if it has any.

    Read off the Pydantic field rather than duplicated in the page, so a
    constraint tightened in ``config/settings.py`` narrows the input widget
    automatically. Preventing an out-of-range entry is better than validating it
    and reporting an error the user could not have anticipated.
    """
    low: float | None = None
    high: float | None = None
    for constraint in _SECTION_TYPES[item.section].model_fields[item.field].metadata:
        if (value := getattr(constraint, "ge", None)) is not None:
            low = float(value)
        if (value := getattr(constraint, "le", None)) is not None:
            high = float(value)
    return low, high


def _validate_registry() -> None:
    """Refuse to import if the registry names a secret or a field that moved.

    D-19 says secrets never leave ``.env``. That is a property of this registry,
    so it is checked here rather than trusted — a typo that exposed
    ``brevo_api_key`` in the UI would otherwise be discovered by a screenshot.
    """
    for item in EDITABLE:
        section = _SECTION_TYPES.get(item.section)
        if section is None:
            msg = f"{item.key}: no settings section named {item.section!r}"
            raise RuntimeError(msg)
        field = section.model_fields.get(item.field)
        if field is None:
            msg = f"{item.key}: {item.section} has no field {item.field!r}"
            raise RuntimeError(msg)
        if field.annotation is SecretStr:
            msg = (
                f"{item.key} is a SecretStr and must not be runtime-editable (D-19). "
                "Secrets belong in .env only."
            )
            raise RuntimeError(msg)


_validate_registry()


# ─────────────────────────────────────────────────────────────────────────────
#  Views returned to the UI
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SettingView:
    """One setting, with enough context for the page to explain itself."""

    spec: EditableSetting
    value: Any
    env_value: Any
    choices: tuple[str, ...] | None

    @property
    def is_overridden(self) -> bool:
        """Whether a saved value is masking the ``.env`` value."""
        return bool(self.value != self.env_value)

    @property
    def source(self) -> str:
        return "Saved in app" if self.is_overridden else ".env"


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of a Test Connection click."""

    healthy: bool
    detail: str
    latency_ms: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
#  Service
# ─────────────────────────────────────────────────────────────────────────────
class SettingsService:
    """Read, validate, persist and apply runtime settings.

    Stateless apart from the ``.env`` baseline, which is module-level because it
    describes the process, not an instance.
    """

    # ── reading ──────────────────────────────────────────────────────────────
    def effective(self) -> list[SettingView]:
        """Every editable setting, with its live value and its ``.env`` value."""
        settings = get_settings()
        baseline = _baseline(settings)
        return [
            SettingView(
                spec=item,
                value=getattr(getattr(settings, item.section), item.field),
                env_value=baseline[item.key],
                choices=_choices(item),
            )
            for item in EDITABLE
        ]

    def get(self, key: str) -> SettingView:
        """One setting by key.

        Raises:
            ValidationError: If the key is not editable.
        """
        spec = self._spec(key)
        settings = get_settings()
        return SettingView(
            spec=spec,
            value=getattr(getattr(settings, spec.section), spec.field),
            env_value=_baseline(settings)[key],
            choices=_choices(spec),
        )

    # ── writing ──────────────────────────────────────────────────────────────
    def set(self, key: str, value: Any, *, updated_by: str | None = None) -> Any:
        """Validate, apply live, and persist one setting. Returns the stored value.

        The order matters. Applying first means an invalid value is rejected
        before anything is written, so the database can never hold a
        configuration the application would refuse to start with.

        Raises:
            ValidationError: If the key is not editable, or the value fails the
                same validation the field would apply at startup.
        """
        spec = self._spec(key)
        settings = get_settings()
        _baseline(settings)  # snapshot .env before the first mutation

        section = getattr(settings, spec.section)
        previous = getattr(section, spec.field)
        try:
            setattr(section, spec.field, value)
        except PydanticValidationError as exc:
            raise ValidationError(
                f"{key}={value!r} rejected: {exc.errors()[0]['msg']}",
                user_message=f"{spec.label}: {exc.errors()[0]['msg']}.",
                context={"key": key},
            ) from exc

        # Read back rather than storing the input: field validators normalise
        # (a trailing /v1 is stripped, numbers are coerced), and the database
        # must agree with the running process about what is configured.
        stored = getattr(section, spec.field)

        try:
            with unit_of_work() as db:
                SettingsRepository(db).set(key, stored, updated_by=updated_by)
        except Exception:
            setattr(section, spec.field, previous)  # keep the two layers in step
            log.exception("settings.persist_failed", key=key)
            raise

        _side_effect(key, stored)
        log.info("settings.changed", key=key, value=_loggable(stored), by=updated_by)
        return stored

    def reset(self, key: str, *, updated_by: str | None = None) -> Any:
        """Drop the saved override and restore the ``.env`` value. Returns it."""
        spec = self._spec(key)
        settings = get_settings()
        env_value = _baseline(settings)[key]

        with unit_of_work() as db:
            SettingsRepository(db).delete(key)

        setattr(getattr(settings, spec.section), spec.field, env_value)
        _side_effect(key, env_value)
        log.info("settings.reset", key=key, by=updated_by)
        return env_value

    def reset_all(self, *, updated_by: str | None = None) -> int:
        """Drop every override. Returns how many were removed."""
        settings = get_settings()
        baseline = _baseline(settings)

        with unit_of_work() as db:
            repo = SettingsRepository(db)
            saved = [key for key in repo.all() if key in _BY_KEY]
            for key in saved:
                repo.delete(key)

        for key in saved:
            spec = _BY_KEY[key]
            setattr(getattr(settings, spec.section), spec.field, baseline[key])
            _side_effect(key, baseline[key])

        log.info("settings.reset_all", count=len(saved), by=updated_by)
        return len(saved)

    # ── startup ──────────────────────────────────────────────────────────────
    def apply_saved(self) -> int:
        """Apply saved overrides to the live settings. Returns how many applied.

        Called once at startup, after the database is initialised. A stored value
        that no longer validates — a model name the provider dropped, a field
        whose constraints tightened in a later version — is **logged and
        skipped**, not raised: refusing to start because of a value that can only
        be corrected through the UI you just prevented from loading is a trap
        with no exit.
        """
        settings = get_settings()
        _baseline(settings)

        try:
            with unit_of_work() as db:
                saved = SettingsRepository(db).all()
        except Exception:
            log.exception("settings.load_failed")
            return 0

        applied = 0
        for key, value in saved.items():
            spec = _BY_KEY.get(key)
            if spec is None:
                log.warning("settings.unknown_key_ignored", key=key)
                continue
            try:
                setattr(getattr(settings, spec.section), spec.field, value)
            except PydanticValidationError as exc:
                log.warning(
                    "settings.saved_value_rejected",
                    key=key,
                    value=_loggable(value),
                    reason=exc.errors()[0]["msg"],
                )
                continue
            _side_effect(key, value)
            applied += 1

        if applied:
            log.info("settings.applied", count=applied)
        return applied

    # ── connection tests ─────────────────────────────────────────────────────
    def test_llm(self) -> ConnectionResult:
        """Call the configured LLM endpoint. Never raises."""
        from services.health_service import HealthService

        started = time.perf_counter()
        try:
            status = HealthService().check(force=True).llm
        except NewsletterAppError as exc:
            return ConnectionResult(False, exc.user_message)
        except Exception as exc:  # noqa: BLE001 - a test button must not crash the page
            log.exception("settings.llm_test_failed")
            return ConnectionResult(False, f"Unexpected error: {exc}")

        elapsed = int((time.perf_counter() - started) * 1000)
        return ConnectionResult(status.healthy, status.detail, status.latency_ms or elapsed)

    def test_email(self) -> ConnectionResult:
        """Verify email credentials without sending anything. Never raises."""
        from modules.email.factory import create_email_provider

        started = time.perf_counter()
        provider = None
        try:
            provider = create_email_provider()
            status = provider.verify_credentials()
        except NewsletterAppError as exc:
            return ConnectionResult(False, exc.user_message)
        except Exception as exc:  # noqa: BLE001 - same reason as above
            log.exception("settings.email_test_failed")
            return ConnectionResult(False, f"Unexpected error: {exc}")
        finally:
            if provider is not None:
                provider.close()

        return ConnectionResult(
            status.healthy, status.detail, int((time.perf_counter() - started) * 1000)
        )

    # ── internals ────────────────────────────────────────────────────────────
    @staticmethod
    def _spec(key: str) -> EditableSetting:
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValidationError(
                f"{key!r} is not a runtime-editable setting",
                user_message="That setting can't be changed here. Edit .env and restart.",
                context={"key": key},
            )
        return spec


# ─────────────────────────────────────────────────────────────────────────────
#  The .env baseline
# ─────────────────────────────────────────────────────────────────────────────
#: Snapshot of the settings as ``.env`` produced them, taken before the first
#: override is applied — this is what "Reset to .env" restores and what the page
#: compares against to decide whether a value is overridden.
#:
#: Tied to the settings *object* it was taken from, so clearing the settings
#: cache (which the test suite does between tests) yields a fresh baseline rather
#: than silently comparing against the previous environment. The reference is
#: held rather than an ``id()``, because CPython reuses addresses after garbage
#: collection: a rebuilt Settings landing at a freed one's address would compare
#: equal and keep a stale baseline. Holding it costs one object that
#: ``get_settings``'s own cache is holding anyway.
_BASELINE: dict[str, Any] = {}
_BASELINE_OF: Settings | None = None


def _baseline(settings: Settings) -> dict[str, Any]:
    """Return — capturing on first use — the pre-override value of every setting."""
    global _BASELINE_OF, _BASELINE  # noqa: PLW0603 - process-level snapshot
    if _BASELINE_OF is not settings:
        _BASELINE = {
            item.key: getattr(getattr(settings, item.section), item.field) for item in EDITABLE
        }
        _BASELINE_OF = settings
    return _BASELINE


def reset_baseline() -> None:
    """Forget the captured baseline. For tests, alongside ``reset_settings_cache``."""
    global _BASELINE_OF  # noqa: PLW0603
    _BASELINE_OF = None


#: Settings whose new value has to be pushed somewhere beyond the settings
#: object to actually take effect. Most do not — providers read `get_settings()`
#: on every call, so mutating the object is enough. The log level is the
#: exception: the console handler holds its own threshold, set once at startup.
#: Without this, the UI would confirm the change and nothing would happen.
def _side_effect(key: str, value: Any) -> None:
    """Push a changed setting to whatever else is holding a copy of it."""
    if key == "logging.log_level":
        set_log_level(str(value))


def _loggable(value: Any) -> Any:
    """Truncate long values so a log line stays readable."""
    text = str(value)
    return value if len(text) <= 120 else text[:117] + "…"
