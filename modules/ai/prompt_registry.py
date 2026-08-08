"""Versioned prompt loading and rendering (D-6).

Prompts live in ``prompts/<name>/v<major>.<minor>.<patch>.yaml`` and are rendered
with Jinja2. Git-native by design: a prompt change shows up as a reviewable diff,
`git blame` says who changed the wording, and a bad edit is one revert away.

**A released version is never edited in place.** Every campaign records the prompt
version that produced it, so editing history would silently invalidate the audit
trail. Change means a new file.

Two failure modes are made loud rather than silent:

* A missing required variable raises :class:`PromptContextError` instead of
  rendering ``"Write a newsletter about "`` — the kind of defect that produces
  plausible-looking garbage and is very hard to spot in output.
* An unknown prompt or version raises :class:`PromptNotFoundError` at render
  time, not a ``KeyError`` three frames deeper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError
from jinja2 import UndefinedError as JinjaUndefinedError

from config import get_logger
from config.constants import PROMPTS_DIR
from core.exceptions import AIError, PromptNotFoundError
from core.models import GenerationParams, Message

log = get_logger(__name__)

_VERSION_FILE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)\.ya?ml$")

LATEST = "latest"


class PromptContextError(AIError):
    """A prompt was rendered without a variable it declares as required."""

    default_user_message = (
        "An internal prompt is missing information it needs. This is a bug — "
        "please report it with the reference code shown on the Logs page."
    )


@dataclass(frozen=True)
class PromptTemplate:
    """One versioned prompt, as loaded from disk."""

    name: str
    version: str
    description: str
    system: str
    user: str
    output_schema: str
    required_context: tuple[str, ...] = ()
    defaults: GenerationParams = field(default_factory=GenerationParams)
    examples: tuple[dict[str, Any], ...] = ()
    model_tested: str = ""


@dataclass(frozen=True)
class RenderedPrompt:
    """A prompt with its context filled in, ready to send."""

    name: str
    version: str
    messages: list[Message]
    params: GenerationParams
    output_schema: str

    @property
    def approx_input_chars(self) -> int:
        return sum(len(m.content) for m in self.messages)


def _parse_version(filename: str) -> tuple[int, int, int] | None:
    match = _VERSION_FILE.match(filename)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


class PromptRegistry:
    """Loads, versions and renders the prompt library."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self.root = prompts_dir or PROMPTS_DIR
        self._env = Environment(
            loader=FileSystemLoader(self.root),
            undefined=StrictUndefined,  # a missing variable is an error, not ""
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
            # S701 flags autoescape=False as an XSS risk, which is correct for
            # HTML and wrong here. These templates render *prompts*, not markup:
            # escaping would turn `&` into `&amp;` and `<` into `&lt;` inside the
            # article text we send the model, corrupting the input it reasons
            # about. There is no injection path either — article content is passed
            # as a template *variable*, never concatenated into template source,
            # so Jinja inserts it without evaluating it.
            #
            # The email renderer (M5) is the opposite case and uses a
            # SandboxedEnvironment with autoescape=True.
            autoescape=False,  # noqa: S701
        )
        self._cache: dict[tuple[str, str], PromptTemplate] = {}

    # ── discovery ────────────────────────────────────────────────────────────
    def list_prompts(self) -> list[str]:
        """Every prompt name in the library."""
        if not self.root.exists():
            return []
        return sorted(
            d.name for d in self.root.iterdir() if d.is_dir() and not d.name.startswith("_")
        )

    def list_versions(self, name: str) -> list[str]:
        """Available versions of ``name``, oldest first."""
        directory = self.root / name
        if not directory.is_dir():
            return []
        versions = [
            (parsed, path.name)
            for path in directory.iterdir()
            if (parsed := _parse_version(path.name)) is not None
        ]
        return [f"{a}.{b}.{c}" for (a, b, c), _ in sorted(versions)]

    def resolve_version(self, name: str, version: str = LATEST) -> str:
        """Turn ``latest`` into a concrete version.

        Raises:
            PromptNotFoundError: If the prompt or version does not exist.
        """
        available = self.list_versions(name)
        if not available:
            raise PromptNotFoundError(
                f"no prompt named {name!r} in {self.root}",
                context={"prompt": name, "available": self.list_prompts()},
            )
        if version == LATEST:
            return available[-1]
        if version not in available:
            raise PromptNotFoundError(
                f"prompt {name!r} has no version {version!r}",
                context={"prompt": name, "requested": version, "available": available},
            )
        return version

    # ── loading ──────────────────────────────────────────────────────────────
    def get(self, name: str, version: str = LATEST) -> PromptTemplate:
        """Load a prompt definition. Cached per (name, version)."""
        resolved = self.resolve_version(name, version)
        key = (name, resolved)
        if key in self._cache:
            return self._cache[key]

        path = self.root / name / f"v{resolved}.yaml"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PromptNotFoundError(
                f"could not read {path}: {exc}", context={"prompt": name}
            ) from exc
        except yaml.YAMLError as exc:
            raise AIError(
                f"prompt {name} v{resolved} is not valid YAML: {exc}",
                context={"prompt": name, "version": resolved},
            ) from exc

        template = self._build(name, resolved, raw or {})
        self._cache[key] = template
        return template

    @staticmethod
    def _build(name: str, version: str, raw: dict[str, Any]) -> PromptTemplate:
        missing = [k for k in ("system", "user", "output_schema") if k not in raw]
        if missing:
            raise AIError(
                f"prompt {name} v{version} is missing required key(s): {missing}",
                context={"prompt": name, "version": version, "missing": missing},
            )
        if raw.get("version") and str(raw["version"]) != version:
            # A mismatch means someone copied a file and forgot to update it, and
            # the campaign audit trail would then record the wrong version.
            raise AIError(
                f"prompt {name}: file says v{version} but `version:` says {raw['version']}",
                context={
                    "prompt": name,
                    "filename_version": version,
                    "body_version": raw["version"],
                },
            )

        defaults = raw.get("defaults") or {}
        return PromptTemplate(
            name=name,
            version=version,
            description=str(raw.get("description", "")),
            system=str(raw["system"]),
            user=str(raw["user"]),
            output_schema=str(raw["output_schema"]),
            required_context=tuple(raw.get("required_context") or ()),
            defaults=GenerationParams(
                temperature=float(defaults.get("temperature", 0.7)),
                top_p=float(defaults.get("top_p", 0.9)),
                max_tokens=int(defaults.get("max_tokens", 2048)),
            ),
            examples=tuple(raw.get("examples") or ()),
            model_tested=str(raw.get("model_tested", "")),
        )

    # ── rendering ────────────────────────────────────────────────────────────
    def render(self, name: str, version: str = LATEST, /, **context: Any) -> RenderedPrompt:
        """Render a prompt with ``context``.

        ``name`` and ``version`` are **positional-only** (note the ``/``). Without
        it, a prompt whose context includes a variable called ``name`` — a brand
        name, a field name, a user's name — collides with the parameter and fails
        with a baffling *"got multiple values for argument 'name'"*.

        Raises:
            PromptContextError: A declared required variable is missing, or the
                template referenced an undefined name.
            PromptNotFoundError: Unknown prompt or version.
        """
        template = self.get(name, version)

        absent = [key for key in template.required_context if key not in context]
        if absent:
            raise PromptContextError(
                f"prompt {name} v{template.version} requires {absent}, which were not supplied",
                context={"prompt": name, "version": template.version, "missing": absent},
            )

        system = self._render_string(template.system, context, template)
        user = self._render_string(template.user, context, template)

        messages = [Message(role="system", content=system), Message(role="user", content=user)]
        log.debug(
            "prompt.rendered",
            prompt=name,
            version=template.version,
            chars=len(system) + len(user),
        )
        return RenderedPrompt(
            name=name,
            version=template.version,
            messages=messages,
            params=template.defaults,
            output_schema=template.output_schema,
        )

    def _render_string(self, source: str, context: dict[str, Any], template: PromptTemplate) -> str:
        try:
            return self._env.from_string(source).render(**context).strip()
        except JinjaUndefinedError as exc:
            raise PromptContextError(
                f"prompt {template.name} v{template.version} referenced an undefined "
                f"variable: {exc.message}",
                context={"prompt": template.name, "version": template.version},
            ) from exc
        except TemplateError as exc:
            raise AIError(
                f"prompt {template.name} v{template.version} failed to render: {exc}",
                context={"prompt": template.name, "version": template.version},
            ) from exc


@lru_cache(maxsize=1)
def get_registry() -> PromptRegistry:
    """The process-wide registry. Prompt files are read once and cached."""
    return PromptRegistry()
