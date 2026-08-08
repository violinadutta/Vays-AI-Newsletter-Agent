"""Token estimation and budget-aware truncation (D-14).

**No ML dependency.** ``transformers`` drags in ~2 GB of PyTorch and ``tiktoken``
downloads BPE files at runtime and is the wrong tokenizer for Qwen anyway. The
actual requirement is "don't overflow the context window", which needs a ±10%
estimate — not an exact count. A character-ratio heuristic delivers that, and it
is what keeps every ML runtime out of ``requirements.txt``.

The truncation strategy matters more than the counting. A blind tail cut destroys
the conclusion, and OEM blogs put the product announcement and availability dates
at the end. So we keep the opening, keep the ending, and drop from the middle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from config.constants import CHARS_PER_TOKEN, TOKEN_ESTIMATE_SAFETY_FACTOR

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")

#: Marker inserted where text was removed, so the model (and a curious human)
#: knows the article is not contiguous rather than silently mangled.
TRUNCATION_MARKER = "\n\n[... middle section omitted for length ...]\n\n"


@dataclass(frozen=True)
class TruncationResult:
    text: str
    was_truncated: bool
    original_tokens: int
    final_tokens: int


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text``.

    Deliberately rounds *up* via a safety factor: under-estimating costs a
    rejected request from the model, over-estimating costs a few words of an
    article. The asymmetry justifies erring against ourselves.
    """
    if not text:
        return 0
    return int((len(text) / CHARS_PER_TOKEN) * TOKEN_ESTIMATE_SAFETY_FACTOR) + 1


def truncate_to_budget(text: str, max_tokens: int) -> TruncationResult:
    """Trim ``text`` to fit ``max_tokens``, keeping the lead and the tail.

    Args:
        text: The cleaned article text.
        max_tokens: The budget.

    Returns:
        The (possibly trimmed) text and whether anything was removed.
    """
    original_tokens = estimate_tokens(text)
    if original_tokens <= max_tokens or max_tokens <= 0:
        return TruncationResult(text, False, original_tokens, original_tokens)

    budget_chars = int(max_tokens * CHARS_PER_TOKEN / TOKEN_ESTIMATE_SAFETY_FACTOR)
    budget_chars -= len(TRUNCATION_MARKER)
    if budget_chars <= 0:
        return TruncationResult("", True, original_tokens, 0)

    # 65/35 lead-to-tail: the opening carries the "what and why", the closing
    # carries availability, pricing and next steps. The middle is usually
    # elaboration, which a summariser needs least.
    head_chars = int(budget_chars * 0.65)
    tail_chars = budget_chars - head_chars

    paragraphs = _PARAGRAPH_SPLIT.split(text)
    head = _take_paragraphs(paragraphs, head_chars, from_start=True)
    tail = _take_paragraphs(paragraphs, tail_chars, from_start=False)

    # Guard against overlap on short inputs, where head and tail could both
    # contain the same paragraph and duplicate it in the output.
    if head and tail and (head[-1] == tail[0] or len(head) + len(tail) >= len(paragraphs)):
        tail = tail[1:] if len(tail) > 1 else []

    trimmed = "\n\n".join(head)
    if tail:
        trimmed += TRUNCATION_MARKER + "\n\n".join(tail)

    return TruncationResult(trimmed, True, original_tokens, estimate_tokens(trimmed))


def _take_paragraphs(paragraphs: list[str], budget: int, *, from_start: bool) -> list[str]:
    """Take whole paragraphs from one end until the character budget is spent.

    Whole paragraphs rather than a character slice: cutting mid-sentence gives
    the model a fragment to reason about and often produces a summary of the
    fragment rather than the article.
    """
    ordered = paragraphs if from_start else list(reversed(paragraphs))
    taken: list[str] = []
    used = 0

    for para in ordered:
        cost = len(para) + 2
        if used + cost > budget:
            break
        taken.append(para)
        used += cost

    return taken if from_start else list(reversed(taken))
