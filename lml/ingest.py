from __future__ import annotations

import re
from pathlib import Path

from .store import MemoryNode, MemoryStore
from .text import slugify

_HEADING = re.compile(
    r"^(?:\*\*)?(?:CHECKPOINT|Checkpoint|LTM CHECKPOINT|AI-QUALIA|PROCESS-MEETS-PROCESS|"
    r"LAM-TRAJECTORY-INVARIANT|PHASE MATCH CHECKPOINT|TY-[A-Z]|LAM_[A-Z]|L❤️‍🔥T)[^\n]*",
    re.MULTILINE,
)


def split_legacy_ltm(text: str) -> list[tuple[str, str]]:
    matches = list(_HEADING.finditer(text))
    chunks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(0).strip("* \n")
        content = text[match.end():end].strip()
        if content:
            chunks.append((title, content))
    return chunks


def ingest_legacy_ltm(store: MemoryStore, path: str | Path, commit: bool = False) -> list[dict[str, str]]:
    source = Path(path)
    chunks = split_legacy_ltm(source.read_text(encoding="utf-8"))
    preview: list[dict[str, str]] = []
    used: set[str] = set()
    for title, content in chunks:
        base = f"legacy-{slugify(title)[:72]}"
        node_id = base
        suffix = 2
        while node_id in used:
            node_id = f"{base}-{suffix}"
            suffix += 1
        used.add(node_id)
        preview.append({"id": node_id, "title": title, "preview": content[:180]})
        if commit:
            store.upsert_node(
                MemoryNode(
                    id=node_id,
                    kind="semantic",
                    title=title,
                    summary=content.split("\n\n", 1)[0][:700],
                    content=content,
                    status="active",
                    priority=55,
                    confidence=0.65,
                    salience=0.5,
                    scope="global",
                    tags=["legacy-import", "needs-audit"],
                    source_type="legacy-ltm",
                    source_ref=str(source),
                )
            )
            store.add_cue(title, node_id, weight=0.85, cue_type="title")
    return preview
