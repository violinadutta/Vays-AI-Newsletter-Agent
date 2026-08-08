"""JSON Schemas handed to the LLM for guided decoding.

**These are generated from the Pydantic models, not hand-written.** That is the
whole point: `core.models.NewsletterContent` is the single definition of what a
newsletter is, so the constraint the model generates under and the validation the
response is checked against cannot drift apart. A hand-maintained second copy
would drift the first time someone adjusts a length limit.

Two transformations are applied to Pydantic's output:

1. **``$ref``/``$defs`` are inlined.** Grammar backends vary in how well they
   handle references; a fully inlined schema works everywhere.
2. **``additionalProperties: false`` is set on every object.** This is what makes
   the schema *strict* — without it, a model may emit extra keys, and
   ``extra="forbid"`` on the Pydantic side would then reject a response the
   decoder considered valid. The two must agree.

With these in place, invalid JSON is not merely unlikely — the tokens that would
produce it are masked out during generation, so ``json.loads`` cannot fail.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from core.models import ArticleSummary, NewsletterContent

__all__ = [
    "ARTICLE_SUMMARY_SCHEMA",
    "NEWSLETTER_SCHEMA",
    "SCHEMA_REGISTRY",
    "SUBJECT_VARIANTS_SCHEMA",
    "UNSUPPORTED_BY_STRICT_MODE",
    "get_schema",
    "single_field_schema",
    "to_strict_schema",
]


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Recursively replace ``$ref`` pointers with the definitions they name."""
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    # Pydantic wraps a referenced type in a single-element allOf when the field
    # also carries metadata (a default, a description). Flatten that.
    if "allOf" in node and len(node["allOf"]) == 1:
        merged = {k: v for k, v in node.items() if k != "allOf"}
        merged.update(_inline_refs(node["allOf"][0], defs))
        return _inline_refs(merged, defs) if "$ref" in merged else merged

    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        resolved = dict(_inline_refs(defs[name], defs))
        # Keep any sibling metadata (description, default) alongside the target.
        resolved.update({k: v for k, v in node.items() if k != "$ref"})
        return resolved

    return {key: _inline_refs(value, defs) for key, value in node.items()}


#: Keywords a strict structured-output backend rejects or cannot enforce.
#:
#: Determined empirically against Groq on 2026-08-07, not from documentation:
#: a schema carrying ``minLength``/``maxLength`` is refused outright with
#: ``json_validate_failed``. ``minItems``/``maxItems`` **are** supported and are
#: deliberately kept.
UNSUPPORTED_BY_STRICT_MODE: frozenset[str] = frozenset({"minLength", "maxLength"})


def _strictify(node: Any) -> Any:
    """Make every object in the schema strict, and drop what the backend rejects.

    Three transformations, all learned from a real request rather than a spec:

    1. ``additionalProperties: false`` — the model may not invent keys.
    2. **``required`` must list every property.** Pydantic omits fields that have
       defaults, so ``technical_facts: list[str] = []`` was generated as optional
       and the API refused the request outright. Making everything required is
       the correct reading anyway: under constrained decoding the model emits the
       whole object in one pass, and a field that can be empty says so with
       ``[]``, which is more useful to a caller than an absent key.
    3. **``minLength``/``maxLength`` are stripped.** They are not enforceable
       here, and their presence fails the request.

    Point 3 costs a guarantee, so it is worth being precise about what survives.
    The decoder still guarantees **structure**: the exact key set, types, enum
    membership, and array bounds. It does *not* guarantee **string lengths** — so
    a 70-character subject line is now possible. That is caught one layer up:
    the Pydantic model keeps its ``max_length``, rejects the response, and the
    engine's repair-retry asks again. Length is enforced, just not for free.
    """
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {
        key: _strictify(value)
        for key, value in node.items()
        if key not in UNSUPPORTED_BY_STRICT_MODE
    }
    if out.get("type") == "object" or "properties" in out:
        out.setdefault("additionalProperties", False)
        if "properties" in out:
            out["required"] = list(out["properties"].keys())
    return out


def to_strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Build a strict, fully-inlined JSON Schema from a Pydantic model.

    Args:
        model: The model defining the expected response shape.

    Returns:
        A schema suitable for ``response_format={"type": "json_schema", ...}``.
    """
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    resolved: dict[str, Any] = _strictify(_inline_refs(schema, defs))
    return resolved


def single_field_schema(
    name: str, *, description: str, max_length: int | None = None
) -> dict[str, Any]:
    """Schema for regenerating one field (FR-3.8).

    A narrow schema for a narrow task: constraining generation to exactly one
    property stops the model from helpfully rewriting the rest of the newsletter
    and discarding the user's edits.
    """
    field: dict[str, Any] = {"type": "string", "description": description}
    if max_length is not None:
        field["maxLength"] = max_length
    return {
        "type": "object",
        "properties": {name: field},
        "required": [name],
        "additionalProperties": False,
    }


#: Stage 1 — per-article extraction.
ARTICLE_SUMMARY_SCHEMA: dict[str, Any] = to_strict_schema(ArticleSummary)

#: Stage 2 — the newsletter itself. The contract from the brief, with constraints.
NEWSLETTER_SCHEMA: dict[str, Any] = to_strict_schema(NewsletterContent)

#: Subject-line alternatives (FR-3.10). ``minItems``/``maxItems`` **are** enforced
#: by the decoder, so "give me three" reliably yields exactly three — unlike the
#: per-string length, which the prompt has to carry.
SUBJECT_VARIANTS_SCHEMA: dict[str, Any] = _strictify(
    {
        "type": "object",
        "properties": {
            "variants": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 3,
                "description": (
                    "Three distinct angles: benefit-led, curiosity-led, specific/factual"
                ),
            }
        },
        "required": ["variants"],
    }
)

#: Maps a prompt's ``output_schema:`` name to the schema itself.
#:
#: ``single_field`` is absent on purpose: it is built per call from the field name
#: and its constraints, so it cannot be a constant.
SCHEMA_REGISTRY: dict[str, dict[str, Any]] = {
    "article_summary": ARTICLE_SUMMARY_SCHEMA,
    "newsletter": NEWSLETTER_SCHEMA,
    "subject_variants": SUBJECT_VARIANTS_SCHEMA,
}


def get_schema(name: str) -> dict[str, Any]:
    """Look up a schema by the name a prompt declares.

    Raises:
        KeyError: With the valid names listed, because the alternative is a
            confusing ``None`` reaching the provider.
    """
    try:
        return SCHEMA_REGISTRY[name]
    except KeyError:
        msg = f"unknown output_schema {name!r}; known: {sorted(SCHEMA_REGISTRY)} (+ 'single_field')"
        raise KeyError(msg) from None
