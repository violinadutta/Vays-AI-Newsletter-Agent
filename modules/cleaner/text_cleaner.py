"""Deterministic text normalisation.

Pure functions: no network, no database, no clock. That is what makes this the
cheapest module in the project to test exhaustively — and it needs to be, because
text normalisation is exactly the kind of code that ships a Unicode edge case to
production unnoticed.
"""

from __future__ import annotations

import re
import unicodedata

from config import get_logger
from config.constants import CHARS_PER_TOKEN  # noqa: F401 - documents the tokeniser link
from core.enums import ExtractorTier
from core.models import CleanedArticle, ExtractedArticle
from modules.cleaner.tokenizer import estimate_tokens, truncate_to_budget

log = get_logger(__name__)

#: Lines that are navigation, sharing widgets or legal furniture rather than
#: article content. Matched case-insensitively against a whole stripped line, so
#: a sentence merely *containing* "share this" survives.
_BOILERPLATE_LINES = re.compile(
    r"^(share (this|on)\b.*|read more\b.*|related (posts?|articles?|reading)\b.*"
    r"|subscribe( to| now)?\b.*|sign up\b.*|follow us\b.*|advertisement"
    r"|cookie(s)? (policy|settings|notice)\b.*|accept( all)? cookies\b.*"
    r"|privacy policy|terms of (use|service)|all rights reserved.*"
    r"|posted (in|on)\b.*|tags?:.*|categor(y|ies):.*"
    r"|\d+ min(ute)? read|by [A-Z][a-z]+ [A-Z][a-z]+)$",
    re.IGNORECASE,
)

_MULTI_NEWLINE = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t ]{2,}")
_ZERO_WIDTH = re.compile(r"[​-‏  ﻿]")


class TextCleaner:
    """Normalises extracted article text."""

    def clean_text(self, raw: str) -> str:
        """Normalise, de-boilerplate and de-duplicate.

        Order matters: normalise Unicode first so the boilerplate patterns see
        ordinary characters rather than curly quotes and non-breaking spaces.
        """
        if not raw:
            return ""

        # NFKC folds compatibility forms — ligatures, full-width Latin, and the
        # non-breaking spaces that OEM CMS platforms scatter through copy.
        text = unicodedata.normalize("NFKC", raw)
        text = _ZERO_WIDTH.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        lines = [line.strip() for line in text.split("\n")]
        kept = [line for line in lines if not _BOILERPLATE_LINES.match(line)]

        text = "\n".join(kept)
        text = _MULTI_SPACE.sub(" ", text)
        text = _MULTI_NEWLINE.sub("\n\n", text)

        return self._dedupe_paragraphs(text).strip()

    @staticmethod
    def _dedupe_paragraphs(text: str) -> str:
        """Drop repeated paragraphs, preserving order.

        Scraped pages routinely repeat a pull-quote or a summary block that also
        appears in the body. Left in, the model treats the repetition as emphasis
        and over-weights that point in the summary.
        """
        seen: set[str] = set()
        kept: list[str] = []
        for para in text.split("\n\n"):
            fingerprint = " ".join(para.split()).lower()
            if len(fingerprint) < 40:  # short lines repeat legitimately (headings)
                kept.append(para)
                continue
            if fingerprint not in seen:
                seen.add(fingerprint)
                kept.append(para)
        return "\n\n".join(kept)

    def detect_language(self, text: str) -> str | None:
        """Best-effort language code, or ``None`` if undetectable.

        Uses ``py3langid`` — pure Python, no model download, no network.
        """
        sample = text[:2000].strip()
        if len(sample) < 40:
            return None
        try:
            import py3langid

            code, _score = py3langid.classify(sample)
        except Exception:  # noqa: BLE001 - language is advisory, never fatal
            return None
        return str(code)

    def clean(self, article: ExtractedArticle, max_tokens: int) -> CleanedArticle:
        """Clean an extracted article and fit it to the token budget.

        Args:
            article: Raw extractor output.
            max_tokens: Input budget for the LLM stage.
        """
        cleaned = self.clean_text(article.text)
        truncation = truncate_to_budget(cleaned, max_tokens)
        language = self.detect_language(truncation.text)

        if truncation.was_truncated:
            log.info(
                "article.truncated",
                url=article.url,
                from_tokens=truncation.original_tokens,
                to_tokens=truncation.final_tokens,
                budget=max_tokens,
            )
        if language and language != "en":
            log.warning("article.non_english", url=article.url, language=language)

        return CleanedArticle(
            url=article.url,
            title=self.clean_title(article.title),
            cleaned_text=truncation.text,
            author=article.author,
            published_at=article.published_at,
            extractor=article.extractor or ExtractorTier.FALLBACK,
            word_count=len(truncation.text.split()),
            token_estimate=estimate_tokens(truncation.text),
            language=language,
            was_truncated=truncation.was_truncated,
        )

    @staticmethod
    def clean_title(title: str) -> str:
        """Normalise a title and strip trailing site branding.

        ``"Dell's New Servers | Dell Technologies"`` becomes
        ``"Dell's New Servers"``. Left in, the branding leaks into the generated
        newsletter headline and reads like a scraped page.
        """
        cleaned = unicodedata.normalize("NFKC", title or "").strip()
        cleaned = _MULTI_SPACE.sub(" ", cleaned)

        for separator in (" | ", " – ", " — ", " - ", " :: "):
            if separator in cleaned:
                head, _, tail = cleaned.rpartition(separator)
                # Only strip when the tail looks like a site name rather than
                # part of the headline: short, and no sentence punctuation.
                if head and len(tail) <= 40 and not tail.endswith((".", "?", "!")):
                    cleaned = head.strip()
                    break

        return cleaned or "(untitled)"
