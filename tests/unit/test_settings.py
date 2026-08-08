"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import build_settings, get_settings, reset_settings_cache
from core.exceptions import ConfigurationError
from tests.conftest import MINIMAL_ENV


class TestFailFast:
    def test_missing_required_variable_names_the_env_var(self) -> None:
        """The message must name ``APP_SECRET_KEY`` — the thing the user edits —
        not ``secret_key``, which appears nowhere in their .env file."""
        with pytest.raises(ConfigurationError) as exc_info:
            build_settings(env_file=None)

        assert "APP_SECRET_KEY" in exc_info.value.message
        assert "required" in exc_info.value.message

    def test_all_problems_are_reported_at_once(self, set_env) -> None:  # noqa: ANN001
        """Otherwise setup is 'fix one, restart, discover the next' — the single
        most demoralising part of standing a project up on a new machine."""
        set_env(
            APP_SECRET_KEY="too-short",
            LLM_TEMPERATURE="9.5",
            LLM_MAX_TOKENS="50",
            SCRAPER_TIMEOUT_S="9999",
        )
        with pytest.raises(ConfigurationError) as exc_info:
            build_settings(env_file=None)

        message = exc_info.value.message
        assert exc_info.value.context["problem_count"] == 4
        for var in ("APP_SECRET_KEY", "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "SCRAPER_TIMEOUT_S"):
            assert var in message

    def test_secret_key_error_includes_the_generator_command(self, set_env) -> None:  # noqa: ANN001
        """An error that says what is wrong but not how to fix it is half an error."""
        set_env(APP_SECRET_KEY="short")
        with pytest.raises(ConfigurationError) as exc_info:
            build_settings(env_file=None)

        assert "secrets.token_urlsafe" in exc_info.value.message

    def test_user_message_is_distinct_from_the_technical_message(self, set_env) -> None:  # noqa: ANN001
        set_env(APP_SECRET_KEY="short")
        with pytest.raises(ConfigurationError) as exc_info:
            build_settings(env_file=None)

        assert exc_info.value.user_message != exc_info.value.message
        assert ".env" in exc_info.value.user_message


class TestLLMBaseUrlNormalisation:
    """Every provider's docs quote a base URL ending in ``/v1`` — Groq's included
    — while the client appends ``/v1`` itself. Pasting the documented value
    verbatim would yield ``/v1/v1/chat/completions`` and a 404 that looks like a
    dead endpoint: an evening lost to a problem one ``rstrip`` prevents."""

    @pytest.mark.parametrize(
        "raw",
        [
            "https://api.groq.com/openai",
            "https://api.groq.com/openai/",
            "https://api.groq.com/openai/v1",
            "https://api.groq.com/openai/v1/",
            "  https://api.groq.com/openai/v1  ",
        ],
    )
    def test_all_paste_variants_normalise_identically(self, set_env, raw: str) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV, LLM_BASE_URL=raw)
        settings = build_settings(env_file=None)

        assert settings.llm.base_url == "https://api.groq.com/openai"

    @pytest.mark.parametrize("raw", ["ftp://host/v1", "api.groq.com", "  "])
    def test_non_http_scheme_is_rejected(self, set_env, raw: str) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV, LLM_BASE_URL=raw)
        with pytest.raises(ConfigurationError) as exc_info:
            build_settings(env_file=None)

        assert "LLM_BASE_URL" in exc_info.value.message


class TestLLMSettings:
    def test_provider_defaults_to_groq(self, set_env) -> None:  # noqa: ANN001
        set_env(APP_SECRET_KEY="k" * 40)
        assert build_settings(env_file=None).llm.provider == "groq"

    def test_the_default_model_supports_strict_schema_enforcement(self, set_env) -> None:  # noqa: ANN001
        """Shipping a default that cannot enforce a schema would quietly demote
        D-3 from a guarantee to a hope."""
        from modules.ai.groq_provider import supports_strict_schema

        set_env(APP_SECRET_KEY="k" * 40)

        assert supports_strict_schema(build_settings(env_file=None).llm.model)

    def test_groq_api_key_is_accepted_under_its_vendor_name(self, set_env) -> None:  # noqa: ANN001
        """Groq's console hands you a value labelled GROQ_API_KEY. Ignoring that
        name would be a needless first-run failure."""
        set_env(**MINIMAL_ENV, GROQ_API_KEY="gsk-from-groq-console")

        settings = build_settings(env_file=None)

        assert settings.llm.api_key.get_secret_value() == "gsk-from-groq-console"

    def test_the_generic_key_name_still_wins(self, set_env) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV, LLM_API_KEY="explicit", GROQ_API_KEY="vendor")

        assert build_settings(env_file=None).llm.api_key.get_secret_value() == "explicit"

    def test_local_provider_is_not_a_valid_choice(self, set_env) -> None:  # noqa: ANN001
        """D-12: no adapter may run a model on this machine. Rejecting the value
        at config level means the constraint cannot be re-introduced by a typo."""
        set_env(**{**MINIMAL_ENV, "LLM_PROVIDER": "local"})
        with pytest.raises(ConfigurationError) as exc_info:
            build_settings(env_file=None)

        assert "LLM_PROVIDER" in exc_info.value.message

    def test_mock_provider_needs_no_endpoint(self, set_env) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV)
        assert build_settings(env_file=None).llm.requires_endpoint is False

    def test_groq_provider_needs_an_endpoint(self, set_env) -> None:  # noqa: ANN001
        set_env(**{**MINIMAL_ENV, "LLM_PROVIDER": "groq"})
        assert build_settings(env_file=None).llm.requires_endpoint is True

    @pytest.mark.parametrize("removed", ["colab", "ollama", "local"])
    def test_retired_providers_are_rejected(self, set_env, removed: str) -> None:  # noqa: ANN001
        """Colab is gone (D-21). An old .env carrying `LLM_PROVIDER=colab` must
        fail loudly at startup rather than fall back to something unexpected."""
        set_env(**{**MINIMAL_ENV, "LLM_PROVIDER": removed})

        with pytest.raises(ConfigurationError):
            build_settings(env_file=None)

    @pytest.mark.parametrize(
        ("var", "value"),
        [
            ("LLM_TEMPERATURE", "-0.1"),
            ("LLM_TEMPERATURE", "2.1"),
            ("LLM_MAX_TOKENS", "255"),
            ("LLM_MAX_TOKENS", "8193"),
            ("LLM_TIMEOUT_S", "0"),
            ("LLM_CIRCUIT_FAILURE_THRESHOLD", "0"),
        ],
    )
    def test_out_of_range_values_are_rejected(self, set_env, var: str, value: str) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV, **{var: value})
        with pytest.raises(ConfigurationError):
            build_settings(env_file=None)


class TestSecretHandling:
    def test_secrets_are_not_exposed_by_repr(self, minimal_settings) -> None:  # noqa: ANN001
        """`repr()` of a settings object ends up in tracebacks and debug output."""
        blob = repr(minimal_settings)
        assert MINIMAL_ENV["APP_SECRET_KEY"] not in blob
        assert "**" in repr(minimal_settings.app.secret_key)

    def test_secret_is_still_retrievable_when_explicitly_asked(self, minimal_settings) -> None:  # noqa: ANN001
        assert minimal_settings.app.secret_key.get_secret_value() == MINIMAL_ENV["APP_SECRET_KEY"]


class TestEnvFileIsolation:
    """A developer's local ``.env`` must never influence a test result."""

    def test_env_file_none_ignores_dotenv(self, tmp_path: Path, set_env) -> None:  # noqa: ANN001
        dotenv = tmp_path / ".env"
        dotenv.write_text("APP_SECRET_KEY=" + "z" * 40 + "\nLLM_MODEL=from-dotenv\n")
        set_env(**MINIMAL_ENV)

        assert build_settings(env_file=None).llm.model != "from-dotenv"

    def test_explicit_env_file_is_read(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text("APP_SECRET_KEY=" + "z" * 40 + "\nLLM_MODEL=from-dotenv\n")

        assert build_settings(env_file=dotenv).llm.model == "from-dotenv"

    def test_real_env_vars_win_over_dotenv(self, tmp_path: Path, set_env) -> None:  # noqa: ANN001
        dotenv = tmp_path / ".env"
        dotenv.write_text("APP_SECRET_KEY=" + "z" * 40 + "\nLLM_MODEL=from-dotenv\n")
        set_env(LLM_MODEL="from-environment")

        assert build_settings(env_file=dotenv).llm.model == "from-environment"


class TestCaching:
    def test_get_settings_returns_the_same_instance(self, set_env) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV)
        reset_settings_cache()

        assert get_settings() is get_settings()

    def test_reset_cache_rebuilds(self, set_env) -> None:  # noqa: ANN001
        set_env(**MINIMAL_ENV)
        reset_settings_cache()
        first = get_settings()
        reset_settings_cache()

        assert get_settings() is not first


class TestDefaults:
    def test_email_defaults_to_console_so_nothing_can_be_sent_by_accident(self, set_env) -> None:  # noqa: ANN001
        """A fresh checkout must not be able to email a real customer."""
        set_env(APP_SECRET_KEY="k" * 40)
        assert build_settings(env_file=None).email.provider == "console"

    def test_local_profile_flag(self, minimal_settings) -> None:  # noqa: ANN001
        assert minimal_settings.is_local is True
        assert minimal_settings.is_production is False
