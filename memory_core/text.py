from __future__ import annotations

import re
import unicodedata


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return " ".join(
        re.findall(
            r"[a-z0-9_]+",
            "".join(char for char in normalized if not unicodedata.combining(char)),
        )
    )


def tokens(value: str) -> list[str]:
    return [part for part in normalize_text(value).split() if len(part) > 1]
