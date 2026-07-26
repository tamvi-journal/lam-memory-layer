from __future__ import annotations

import hashlib
import math
from collections import Counter

from .text import normalize_text, tokens

DIM = 256


def _bucket(feature: str) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    raw = int.from_bytes(digest, "big")
    return raw % DIM, 1.0 if (raw >> 8) & 1 else -1.0


def embed_text(text: str) -> list[float]:
    """Dependency-free semantic-ish embedding using signed feature hashing.

    It mixes word features and character trigrams. It is intentionally a
    fallback: deterministic, private, and good enough to provide fuzzy recall
    before a local sentence-transformer is installed.
    """
    norm = normalize_text(text)
    feats: Counter[str] = Counter(tokens(norm))
    compact = norm.replace(" ", "_")
    for i in range(max(0, len(compact) - 2)):
        feats[f"#3:{compact[i:i+3]}"] += 0.25

    vec = [0.0] * DIM
    for feature, count in feats.items():
        idx, sign = _bucket(feature)
        vec[idx] += sign * (1.0 + math.log1p(float(count)))

    norm_value = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm_value for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))
