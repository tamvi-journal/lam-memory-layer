from __future__ import annotations

import re
import unicodedata

_WORD_RE = re.compile(r"[\wÀ-ỹ]+", re.UNICODE)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def tokens(text: str) -> list[str]:
    return [t for t in _WORD_RE.findall(normalize_text(text)) if len(t) > 1]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "memory"


def estimate_tokens(text: str) -> int:
    # Enough for budgeting without a tokenizer dependency.
    return max(1, int(len(text) / 3.6))
