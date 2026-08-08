"""Prompt registry tests.

The registry's job is to make two failure modes loud: a missing context variable
(which otherwise renders `"Write a newsletter about "` and produces confident
garbage) and a wrong version (which otherwise corrupts the campaign audit trail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.exceptions import AIError, PromptNotFoundError
from core.models import GenerationParams
from modules.ai.prompt_registry import (
    LATEST,
    PromptContextError,
    PromptRegistry,
    get_registry,
)


@pytest.fixture
def temp_registry(tmp_path: Path) -> PromptRegistry:
    """A registry over a throwaway prompt library."""
    (tmp_path / "_shared").mkdir()
    (tmp_path / "_shared" / "persona.md").write_text("You are a copywriter.", encoding="utf-8")

    directory = tmp_path / "greet"
    directory.mkdir()
    (directory / "v1.0.0.yaml").write_text(
        'version: "1.0.0"\n'
        "output_schema: newsletter\n"
        "required_context: [name]\n"
        "defaults: {temperature: 0.4, max_tokens: 1500}\n"
        'system: |\n  {% include "_shared/persona.md" %}\n'
        "user: |\n  Hello {{ name }}.\n",
        encoding="utf-8",
    )
    (directory / "v1.1.0.yaml").write_text(
        'version: "1.1.0"\n'
        "output_schema: newsletter\n"
        "required_context: [name]\n"
        "system: |\n  system v1.1\n"
        "user: |\n  Hi {{ name }}, this is version two.\n",
        encoding="utf-8",
    )
    return PromptRegistry(tmp_path)


class TestDiscovery:
    def test_lists_prompts_excluding_shared_fragments(self, temp_registry: PromptRegistry) -> None:
        """`_shared` holds includes, not prompts."""
        assert temp_registry.list_prompts() == ["greet"]

    def test_lists_versions_oldest_first(self, temp_registry: PromptRegistry) -> None:
        assert temp_registry.list_versions("greet") == ["1.0.0", "1.1.0"]

    def test_versions_sort_numerically_not_lexically(self, tmp_path: Path) -> None:
        """v1.10.0 is newer than v1.9.0; string sorting says otherwise and would
        silently pin the library to an old version."""
        directory = tmp_path / "p"
        directory.mkdir()
        for version in ("1.9.0", "1.10.0", "1.2.0"):
            (directory / f"v{version}.yaml").write_text(
                f'version: "{version}"\noutput_schema: newsletter\nsystem: s\nuser: u\n',
                encoding="utf-8",
            )

        assert PromptRegistry(tmp_path).resolve_version("p", LATEST) == "1.10.0"

    def test_latest_resolves_to_the_newest(self, temp_registry: PromptRegistry) -> None:
        assert temp_registry.resolve_version("greet", LATEST) == "1.1.0"

    def test_an_unknown_prompt_lists_what_exists(self, temp_registry: PromptRegistry) -> None:
        with pytest.raises(PromptNotFoundError) as exc_info:
            temp_registry.get("nope")

        assert exc_info.value.context["available"] == ["greet"]

    def test_an_unknown_version_lists_what_exists(self, temp_registry: PromptRegistry) -> None:
        with pytest.raises(PromptNotFoundError) as exc_info:
            temp_registry.get("greet", "9.9.9")

        assert "1.1.0" in exc_info.value.context["available"]


class TestLoading:
    def test_loads_defaults(self, temp_registry: PromptRegistry) -> None:
        template = temp_registry.get("greet", "1.0.0")

        assert template.defaults == GenerationParams(temperature=0.4, max_tokens=1500)

    def test_a_version_mismatch_is_rejected(self, tmp_path: Path) -> None:
        """Someone copied v1.0.0.yaml to v1.1.0.yaml and forgot the body. Loading
        it anyway would record the wrong version on every campaign it produced."""
        directory = tmp_path / "p"
        directory.mkdir()
        (directory / "v2.0.0.yaml").write_text(
            'version: "1.0.0"\noutput_schema: newsletter\nsystem: s\nuser: u\n', encoding="utf-8"
        )

        with pytest.raises(AIError, match="version"):
            PromptRegistry(tmp_path).get("p", "2.0.0")

    def test_a_missing_required_key_is_rejected(self, tmp_path: Path) -> None:
        directory = tmp_path / "p"
        directory.mkdir()
        (directory / "v1.0.0.yaml").write_text('version: "1.0.0"\nsystem: s\n', encoding="utf-8")

        with pytest.raises(AIError, match="missing required key"):
            PromptRegistry(tmp_path).get("p")

    def test_malformed_yaml_is_reported_clearly(self, tmp_path: Path) -> None:
        directory = tmp_path / "p"
        directory.mkdir()
        (directory / "v1.0.0.yaml").write_text("system: [unclosed\n", encoding="utf-8")

        with pytest.raises(AIError, match="not valid YAML"):
            PromptRegistry(tmp_path).get("p")


class TestRendering:
    def test_renders_context_into_both_messages(self, temp_registry: PromptRegistry) -> None:
        rendered = temp_registry.render("greet", "1.0.0", name="Priya")

        assert rendered.messages[0].role == "system"
        assert "copywriter" in rendered.messages[0].content  # the include resolved
        assert rendered.messages[1].content == "Hello Priya."

    def test_a_missing_declared_variable_raises(self, temp_registry: PromptRegistry) -> None:
        """The whole point. Rendering "Hello ." would produce confident garbage
        that is very hard to spot in output."""
        with pytest.raises(PromptContextError) as exc_info:
            temp_registry.render("greet", "1.0.0")

        assert exc_info.value.context["missing"] == ["name"]

    def test_an_undeclared_but_used_variable_still_raises(self, tmp_path: Path) -> None:
        """StrictUndefined is the backstop for a variable someone forgot to add to
        required_context."""
        directory = tmp_path / "p"
        directory.mkdir()
        (directory / "v1.0.0.yaml").write_text(
            'version: "1.0.0"\noutput_schema: newsletter\nsystem: s\nuser: "{{ forgotten }}"\n',
            encoding="utf-8",
        )

        with pytest.raises(PromptContextError):
            PromptRegistry(tmp_path).render("p")

    def test_the_resolved_version_is_returned(self, temp_registry: PromptRegistry) -> None:
        """Recorded on the campaign, so it must be concrete rather than 'latest'."""
        assert temp_registry.render("greet", LATEST, name="x").version == "1.1.0"

    def test_defaults_travel_with_the_rendered_prompt(self, temp_registry: PromptRegistry) -> None:
        assert temp_registry.render("greet", "1.0.0", name="x").params.temperature == 0.4


class TestRealLibrary:
    """The shipped prompts, not fixtures."""

    def test_all_four_prompts_exist(self) -> None:
        assert set(get_registry().list_prompts()) == {
            "article_summary",
            "newsletter_compose",
            "field_regenerate",
            "subject_variants",
        }

    @pytest.mark.parametrize(
        "name", ["article_summary", "newsletter_compose", "field_regenerate", "subject_variants"]
    )
    def test_every_prompt_loads(self, name: str) -> None:
        template = get_registry().get(name)

        assert template.system and template.user
        assert template.output_schema

    @pytest.mark.parametrize(
        "name", ["article_summary", "newsletter_compose", "field_regenerate", "subject_variants"]
    )
    def test_token_budgets_clear_the_floor(self, name: str) -> None:
        """Measured: gpt-oss emits reasoning tokens that count toward max_tokens
        but never appear in the output. A budget sized to the visible response
        gets cut off mid-JSON and fails as `json_validate_failed`."""
        assert get_registry().get(name).defaults.max_tokens >= 2048

    def test_the_summary_prompt_runs_cold(self) -> None:
        """Stage 1 is about faithfulness; a high temperature invents details that
        stage 2 then amplifies."""
        assert get_registry().get("article_summary").defaults.temperature <= 0.4

    def test_the_variants_prompt_runs_hot(self) -> None:
        """Diversity is the entire point — three paraphrases would be a bug."""
        assert get_registry().get("subject_variants").defaults.temperature >= 0.85
