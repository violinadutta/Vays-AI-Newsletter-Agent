"""Runtime settings: validation, persistence, live application, and the D-19 line.

The tests are named after the failures they prevent. The two that matter most
are ``test_a_rejected_value_leaves_the_old_one_in_place`` (a settings page that
half-applies a change is worse than one that refuses it) and
``test_no_secret_is_runtime_editable`` (the mechanical form of D-19).
"""

from __future__ import annotations

import logging

import pytest

from config import get_settings
from config.settings import reset_settings_cache
from core.exceptions import ValidationError
from modules.repository.database import unit_of_work
from modules.repository.settings_repo import SettingsRepository
from services import settings_service
from services.settings_service import EDITABLE, SettingsService, bounds

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    """A fresh settings object and baseline per test."""
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)
    reset_settings_cache()
    settings_service.reset_baseline()
    yield
    reset_settings_cache()
    settings_service.reset_baseline()


@pytest.fixture
def service() -> SettingsService:
    return SettingsService()


# ─────────────────────────────────────────────────────────────────────────────
#  The registry itself
# ─────────────────────────────────────────────────────────────────────────────
class TestRegistry:
    def test_no_secret_is_runtime_editable(self) -> None:
        """D-19, mechanically. A secret in this registry would be rendered in the
        UI and written to SQLite — where it would then sit in every backup."""
        from pydantic import SecretStr

        from config.settings import SECTIONS

        sections = dict(SECTIONS)
        for item in EDITABLE:
            annotation = sections[item.section].model_fields[item.field].annotation
            assert annotation is not SecretStr, f"{item.key} exposes a secret"

    def test_every_registered_field_exists(self) -> None:
        """A renamed settings field must not leave a dead row that silently stops
        applying — the import-time check is what catches it."""
        settings_service._validate_registry()  # noqa: SLF001 - that is the unit here

    def test_a_secret_in_the_registry_is_refused_at_import(self) -> None:
        from services.settings_service import EditableSetting

        original = settings_service.EDITABLE
        settings_service.EDITABLE = (
            *original,
            EditableSetting("llm.api_key", "llm", "api_key", "Key", "", "text", "AI"),
        )
        try:
            with pytest.raises(RuntimeError, match="SecretStr"):
                settings_service._validate_registry()  # noqa: SLF001
        finally:
            settings_service.EDITABLE = original

    def test_keys_are_unique(self) -> None:
        keys = [item.key for item in EDITABLE]
        assert len(keys) == len(set(keys))

    def test_env_var_names_are_resolvable(self) -> None:
        """The UI names the .env variable to edit; a wrong name sends the user
        looking for a line that isn't there."""
        assert {i.key: i.env_var for i in EDITABLE}["llm.base_url"] == "LLM_BASE_URL"
        assert {i.key: i.env_var for i in EDITABLE}["email.smtp_host"] == "SMTP_HOST"

    def test_numeric_bounds_come_from_the_field(self) -> None:
        spec = next(i for i in EDITABLE if i.key == "llm.temperature")

        assert bounds(spec) == (0.0, 2.0)


# ─────────────────────────────────────────────────────────────────────────────
#  Reading
# ─────────────────────────────────────────────────────────────────────────────
class TestEffective:
    def test_untouched_settings_report_env_as_their_source(self, service) -> None:  # noqa: ANN001
        views = {v.spec.key: v for v in service.effective()}

        assert views["llm.model"].source == ".env"
        assert not views["llm.model"].is_overridden

    def test_choices_are_offered_for_literal_fields(self, service) -> None:  # noqa: ANN001
        views = {v.spec.key: v for v in service.effective()}

        assert views["llm.provider"].choices == ("groq", "hosted", "mock")
        assert views["llm.model"].choices is None

    def test_an_unknown_key_is_refused_with_a_usable_message(self, service) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError) as exc:
            service.get("llm.api_key")

        assert "Edit .env" in exc.value.user_message


# ─────────────────────────────────────────────────────────────────────────────
#  Writing
# ─────────────────────────────────────────────────────────────────────────────
class TestSet:
    def test_a_change_takes_effect_immediately(self, service) -> None:  # noqa: ANN001
        """The whole point of M8: no restart. Anything reading get_settings()
        after this call sees the new value."""
        service.set("llm.model", "openai/gpt-oss-20b")

        assert get_settings().llm.model == "openai/gpt-oss-20b"

    def test_a_change_is_persisted(self, service) -> None:  # noqa: ANN001
        service.set("llm.model", "openai/gpt-oss-20b", updated_by="priya")

        with unit_of_work() as db:
            assert SettingsRepository(db).get("llm.model") == "openai/gpt-oss-20b"

    def test_a_rejected_value_leaves_the_old_one_in_place(self, service) -> None:  # noqa: ANN001
        """Half-applying a change is worse than refusing it: the app would be
        running a configuration it could not have booted with."""
        before = get_settings().llm.temperature

        with pytest.raises(ValidationError):
            service.set("llm.temperature", 99.0)

        assert get_settings().llm.temperature == before
        with unit_of_work() as db:
            assert SettingsRepository(db).get("llm.temperature") is None

    def test_a_rejected_value_names_the_field_a_human_recognises(self, service) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError) as exc:
            service.set("llm.temperature", 99.0)

        assert exc.value.user_message.startswith("Temperature:")

    def test_the_stored_value_is_the_normalised_one(self, service) -> None:  # noqa: ANN001
        """base_url strips a trailing /v1. If the raw input were stored, the
        database and the running process would disagree about the endpoint —
        and this page is where you would go to work out why."""
        stored = service.set("llm.base_url", "https://llm.vays.internal/v1/")

        assert stored == "https://llm.vays.internal"
        with unit_of_work() as db:
            assert SettingsRepository(db).get("llm.base_url") == "https://llm.vays.internal"

    def test_an_unknown_key_cannot_be_written(self, service) -> None:  # noqa: ANN001
        with pytest.raises(ValidationError):
            service.set("app.secret_key", "hunter2")

        with unit_of_work() as db:
            assert SettingsRepository(db).get("app.secret_key") is None

    def test_the_postal_address_can_be_set_from_the_ui(self, service) -> None:  # noqa: ANN001
        """The one setting that blocks sending outright, and the most likely
        reason a first-time user is stuck."""
        service.set("brand.address", "Vays Infotech, Pune 411045, India")

        assert get_settings().brand.address.endswith("India")

    def test_changing_the_log_level_moves_the_console_handler(self, service) -> None:  # noqa: ANN001
        """Without the side-effect hook the save would succeed and change
        nothing — the handler holds its own threshold."""
        from config import configure_logging

        configure_logging("INFO")

        service.set("logging.log_level", "WARNING")

        console = [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert console, "expected a console handler"
        assert all(h.level == logging.WARNING for h in console)

    def test_the_file_sink_stays_at_debug(self, service) -> None:  # noqa: ANN001
        """The forensic trail is worth more than the disk space, and it is no use
        if it was quiet when the thing went wrong."""
        from config import configure_logging

        configure_logging("INFO")
        service.set("logging.log_level", "CRITICAL")

        files = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        assert all(h.level == logging.DEBUG for h in files)


# ─────────────────────────────────────────────────────────────────────────────
#  Reverting
# ─────────────────────────────────────────────────────────────────────────────
class TestReset:
    def test_reset_restores_the_env_value(self, service) -> None:  # noqa: ANN001
        original = get_settings().llm.model
        service.set("llm.model", "openai/gpt-oss-20b")

        service.reset("llm.model")

        assert get_settings().llm.model == original
        with unit_of_work() as db:
            assert SettingsRepository(db).get("llm.model") is None

    def test_reset_all_clears_every_override(self, service) -> None:  # noqa: ANN001
        service.set("llm.model", "openai/gpt-oss-20b")
        service.set("brand.name", "Someone Else Ltd")

        removed = service.reset_all()

        assert removed == 2
        assert not [v for v in service.effective() if v.is_overridden]

    def test_reset_all_ignores_foreign_rows(self, service) -> None:  # noqa: ANN001
        """The settings table is shared; a row this service does not own must be
        left alone rather than deleted as collateral."""
        with unit_of_work() as db:
            SettingsRepository(db).set("ui.last_template", "modern")
        service.set("llm.model", "openai/gpt-oss-20b")

        assert service.reset_all() == 1
        with unit_of_work() as db:
            assert SettingsRepository(db).get("ui.last_template") == "modern"


# ─────────────────────────────────────────────────────────────────────────────
#  Startup
# ─────────────────────────────────────────────────────────────────────────────
class TestApplySaved:
    def test_saved_values_survive_a_restart(self, service) -> None:  # noqa: ANN001
        service.set("llm.model", "openai/gpt-oss-20b")

        reset_settings_cache()  # a new process
        settings_service.reset_baseline()
        assert get_settings().llm.model != "openai/gpt-oss-20b"  # .env value first

        applied = SettingsService().apply_saved()

        assert applied == 1
        assert get_settings().llm.model == "openai/gpt-oss-20b"

    def test_a_value_that_no_longer_validates_is_skipped_not_fatal(self, service) -> None:  # noqa: ANN001
        """A model name the provider dropped, or a constraint tightened in a
        later version, must not make the app unbootable — the only way to fix it
        is the UI that would have failed to load."""
        with unit_of_work() as db:
            SettingsRepository(db).set("llm.temperature", 99.0)

        reset_settings_cache()
        settings_service.reset_baseline()

        assert SettingsService().apply_saved() == 0
        assert get_settings().llm.temperature == 0.7

    def test_an_unrecognised_key_is_ignored(self) -> None:
        with unit_of_work() as db:
            SettingsRepository(db).set("llm.removed_field", "x")

        assert SettingsService().apply_saved() == 0

    def test_it_is_a_no_op_with_nothing_saved(self) -> None:
        assert SettingsService().apply_saved() == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Connection tests
# ─────────────────────────────────────────────────────────────────────────────
class TestConnectionTests:
    def test_the_mock_provider_reports_healthy(self, service, set_env) -> None:  # noqa: ANN001
        set_env(LLM_PROVIDER="mock")
        reset_settings_cache()
        settings_service.reset_baseline()

        assert service.test_llm().healthy

    def test_a_broken_provider_returns_a_result_rather_than_raising(
        self, service, monkeypatch
    ) -> None:  # noqa: ANN001, E501
        """A Test Connection button that throws takes the page down with it —
        and the page is where you go when things are already broken."""
        from services import health_service

        def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            msg = "connection refused"
            raise OSError(msg)

        monkeypatch.setattr(health_service.HealthService, "check", boom)

        result = service.test_llm()

        assert not result.healthy
        assert "connection refused" in result.detail

    def test_the_console_email_provider_reports_healthy(self, service) -> None:  # noqa: ANN001
        assert service.test_email().healthy


# ─────────────────────────────────────────────────────────────────────────────
#  The claim that makes the rest worth anything
# ─────────────────────────────────────────────────────────────────────────────
class TestChangesReachTheRestOfTheApp:
    """Mutating the settings object is only useful if the components that read it
    are built *after* the change. They are, because the factories call
    ``get_settings()`` per operation rather than holding a captured copy — but
    that is an invariant of other modules, so it is asserted here rather than
    assumed. If someone later caches a provider, these fail."""

    def test_switching_the_llm_provider_changes_what_the_factory_builds(self, service) -> None:  # noqa: ANN001
        from modules.ai.factory import create_provider
        from modules.ai.mock_provider import MockProvider

        service.set("llm.provider", "mock")

        assert isinstance(create_provider(), MockProvider)

    def test_switching_the_email_provider_changes_what_the_factory_builds(self, service) -> None:  # noqa: ANN001
        from modules.email.console_provider import ConsoleEmailProvider
        from modules.email.factory import create_email_provider

        service.set("email.provider", "console")

        provider = create_email_provider()
        try:
            assert isinstance(provider, ConsoleEmailProvider)
        finally:
            provider.close()

    def test_a_new_endpoint_is_what_the_provider_is_built_with(self, service) -> None:  # noqa: ANN001
        """The literal M8 done-criterion: change the LLM endpoint URL from the
        UI and have the next call go somewhere else."""
        from modules.ai.factory import create_provider

        service.set("llm.provider", "hosted")
        service.set("llm.base_url", "https://llm.vays.internal/v1")

        provider = create_provider()
        try:
            assert provider.base_url == "https://llm.vays.internal"
        finally:
            provider.close()
