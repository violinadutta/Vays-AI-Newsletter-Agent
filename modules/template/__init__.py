"""Email rendering — newsletter content into HTML a mail client will display.

Hand-authored table layouts (D-23), not MJML. See ``docs/09_FINAL_DECISIONS.md``.
"""

from modules.template.brand import ResolvedBrand, resolve_brand
from modules.template.renderer import (
    MERGE_TOKENS,
    TemplateRenderer,
    apply_merge_tokens,
    build_plain_text,
)

__all__ = [
    "MERGE_TOKENS",
    "ResolvedBrand",
    "TemplateRenderer",
    "apply_merge_tokens",
    "build_plain_text",
    "resolve_brand",
]
