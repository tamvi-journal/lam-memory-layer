from __future__ import annotations

import re
from pathlib import Path

from .context import ContextPacketBuilder
from .retrieval import CueRetriever


def infer_scope(cwd: str | Path) -> str:
    path = Path(cwd).resolve()
    for part in reversed(path.parts):
        if part and part not in {"/", "Users", "<user>", "Projects", "Continuity-Projects"}:
            return re.sub(r"[^a-z0-9]+", "-", part.lower()).strip("-") or "global"
    return "global"


def build_cold_start_cue(task: str, *, cwd: str | Path, project: str | None = None) -> str:
    resolved = Path(cwd).resolve()
    scope = project or infer_scope(resolved)
    parts = [
        task.strip(),
        f"cwd: {resolved}",
        f"active project: {scope}",
    ]
    if "Continuity-Projects" in resolved.parts:
        parts.append("workspace family: Continuity-Projects")
    if "continuity-memory-layer" in resolved.parts:
        parts.append("continuity-local-objective memory-cue-principle Lam Memory Layer")
    return "; ".join(part for part in parts if part)


def write_preflight_packet(
    retriever: CueRetriever,
    task: str,
    *,
    cwd: str | Path,
    scope: str | None = None,
    out: str | Path,
    limit: int = 12,
    token_budget: int = 2400,
) -> dict[str, str]:
    detected_scope = scope or infer_scope(cwd)
    cue = build_cold_start_cue(task, cwd=cwd, project=detected_scope)
    packet = ContextPacketBuilder(retriever).build(
        cue,
        scope=detected_scope,
        limit=limit,
        token_budget=token_budget,
        review_branch="work-mode",
    )
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(packet, encoding="utf-8")
    return {
        "cue": cue,
        "scope": detected_scope,
        "out": str(out_path),
    }
