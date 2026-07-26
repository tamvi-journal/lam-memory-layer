from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .consolidator import consolidate_candidates
from .store import MemoryStore

DEFAULT_TENANT_ID = "lam-ty-primary"
PROTECTED_KINDS = {"identity", "relationship", "axis", "boundary"}
HALF_LIFE_DAYS = {
    "episodic": 45.0,
    "project": 120.0,
    "procedural": 240.0,
    "semantic": 365.0,
}
SALIENCE_FLOORS = {
    "episodic": 0.08,
    "project": 0.18,
    "procedural": 0.28,
    "semantic": 0.32,
}


@dataclass(frozen=True)
class DreamResult:
    run_id: str
    tenant_id: str
    scope: str
    trigger: str
    dry_run: bool
    metrics: dict[str, Any]
    summary: str
    adjustments: list[dict[str, Any]]
    proposals: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "scope": self.scope,
            "trigger": self.trigger,
            "dry_run": self.dry_run,
            "metrics": self.metrics,
            "summary": self.summary,
            "adjustments": self.adjustments,
            "proposals": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "status": item["status"],
                    "evidence_count": item["evidence_count"],
                }
                for item in self.proposals
            ],
        }


def run_dream_cycle(
    store: MemoryStore,
    *,
    scope: str = "global",
    tenant_id: str = DEFAULT_TENANT_ID,
    trigger: str = "manual",
    now: datetime | None = None,
    dry_run: bool = False,
    summary_out: str | Path | None = None,
    min_evidence: int = 3,
) -> DreamResult:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    started_at = current.isoformat(timespec="microseconds")
    run_id = _run_id(tenant_id, scope, trigger, started_at)
    previous_runs = store.dream_runs(limit=1)
    previous_finished_at = (
        _parse_datetime(previous_runs[0]["finished_at"]) if previous_runs else None
    )

    nodes = [
        node
        for node in store.active_nodes()
        if scope == "global" or node["scope"] in {"global", scope}
    ]
    adjustments = _plan_adjustments(nodes, current, previous_finished_at)
    proposals = [] if dry_run else consolidate_candidates(store, min_evidence=min_evidence)
    field_states = store.field_states(limit=100)
    relevant_states = [
        state
        for state in field_states
        if scope == "global" or state["scope"] in {"global", scope}
    ]
    conflicts = _active_conflicts(store, {node["id"] for node in nodes})
    pending = store.candidates(status="pending", limit=100)
    pending_relevant = [
        item
        for item in pending
        if scope == "global" or item["scope"] in {"global", scope}
    ]
    held = store.candidates(status="held", limit=500)
    held_relevant = [
        item
        for item in held
        if scope == "global" or item["scope"] in {"global", scope}
    ]
    metrics = {
        "active_nodes": len(nodes),
        "protected_nodes": sum(1 for node in nodes if _is_protected(node)),
        "adjusted_nodes": len({item["node_id"] for item in adjustments}),
        "adjustment_count": len(adjustments),
        "semantic_proposals": len(proposals),
        "pending_candidates": len(pending_relevant),
        "held_observations": len(held_relevant),
        "anchored_episodes": sum(
            1
            for node in nodes
            if node["kind"] == "episodic"
            and "candidate-approved" in node["tags"]
        ),
        "conflict_count": len(conflicts),
        "mean_recent_coherence": _mean_metric(relevant_states, "coherence"),
        "mean_recent_uncertainty": _mean_metric(relevant_states, "uncertainty"),
        "mean_recent_drift_risk": _mean_metric(relevant_states, "drift_risk"),
    }
    summary = _build_summary(
        tenant_id=tenant_id,
        scope=scope,
        generated_at=started_at,
        nodes=nodes,
        pending=pending_relevant,
        held=held_relevant,
        conflicts=conflicts,
        metrics=metrics,
        dry_run=dry_run,
    )

    if not dry_run:
        store.record_dream_run(
            {
                "id": run_id,
                "tenant_id": tenant_id,
                "scope": scope,
                "trigger": trigger,
                "status": "completed",
                "started_at": started_at,
                "finished_at": current.isoformat(timespec="seconds"),
                "metrics": metrics,
                "summary": summary,
            }
        )
        store.apply_dream_adjustments(run_id, adjustments)
        store.set_meta("tenant_id", tenant_id)
        if summary_out:
            _atomic_write(Path(summary_out), summary)

    return DreamResult(
        run_id=run_id,
        tenant_id=tenant_id,
        scope=scope,
        trigger=trigger,
        dry_run=dry_run,
        metrics=metrics,
        summary=summary,
        adjustments=adjustments,
        proposals=proposals,
    )


def dream_due(
    store: MemoryStore,
    *,
    now: datetime | None = None,
    interval_hours: float = 12.0,
) -> bool:
    runs = store.dream_runs(limit=1)
    if not runs:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        finished = datetime.fromisoformat(runs[0]["finished_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return (current - finished).total_seconds() >= interval_hours * 3600


def _plan_adjustments(
    nodes: list[dict[str, Any]],
    now: datetime,
    previous_finished_at: datetime | None,
) -> list[dict[str, Any]]:
    adjustments: list[dict[str, Any]] = []
    for node in nodes:
        if _is_protected(node):
            continue
        elapsed_days = _elapsed_days(node, now, previous_finished_at)
        if elapsed_days is None:
            continue
        kind = node["kind"]
        half_life = HALF_LIFE_DAYS.get(kind, 180.0)
        evidence_factor = min(
            2.0,
            math.log1p(max(1, node["evidence_count"])) * 0.3
            + int(node["helpful_count"]) * 0.1,
        )
        effective_half_life = half_life * (1.0 + evidence_factor)
        floor = SALIENCE_FLOORS.get(kind, 0.18)
        old_salience = float(node["salience"])
        decayed = floor + (old_salience - floor) * math.exp(
            -math.log(2.0) * elapsed_days / effective_half_life
        )
        new_salience = _clamp(decayed)
        if abs(new_salience - old_salience) >= 0.01:
            adjustments.append(
                {
                    "node_id": node["id"],
                    "field": "salience",
                    "old_value": round(old_salience, 4),
                    "new_value": round(new_salience, 4),
                    "reason": (
                        f"{kind} temporal regulation; elapsed={elapsed_days:.1f}d; "
                        f"effective_half_life={effective_half_life:.0f}d; "
                        f"evidence={node['evidence_count']}; "
                        f"feedback=+{node['helpful_count']}/-{node['harmful_count']}"
                    ),
                }
            )

        if kind == "semantic":
            old_stability = float(node["stability"])
            target = min(0.94, 0.45 + math.log1p(max(1, node["evidence_count"])) * 0.13)
            new_stability = _clamp(max(old_stability, target))
            if new_stability - old_stability >= 0.01:
                adjustments.append(
                    {
                        "node_id": node["id"],
                        "field": "stability",
                        "old_value": round(old_stability, 4),
                        "new_value": round(new_stability, 4),
                        "reason": f"semantic evidence reinforcement; evidence={node['evidence_count']}",
                    }
                )
    return adjustments


def _is_protected(node: dict[str, Any]) -> bool:
    return (
        node["kind"] in PROTECTED_KINDS
        or "autoload" in node["tags"]
        or "no-decay" in node["tags"]
    )


def _elapsed_days(
    node: dict[str, Any],
    now: datetime,
    previous_finished_at: datetime | None,
) -> float | None:
    stamp = node.get("last_accessed_at") or node.get("occurred_at")
    if not stamp:
        return None
    value = _parse_datetime(stamp)
    if value is None:
        return None
    baseline = max(value, previous_finished_at) if previous_finished_at else value
    return max(0.0, (now - baseline).total_seconds() / 86400.0)


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _active_conflicts(
    store: MemoryStore,
    active_ids: set[str],
) -> list[dict[str, Any]]:
    return [
        edge
        for edge in store.edges()
        if edge["src_id"] in active_ids
        and edge["dst_id"] in active_ids
        and edge["relation"] in {"contradicts", "supersedes"}
    ]


def _mean_metric(states: list[dict[str, Any]], key: str) -> float | None:
    values = [float(state[key]) for state in states[:25]]
    return round(mean(values), 4) if values else None


def _build_summary(
    *,
    tenant_id: str,
    scope: str,
    generated_at: str,
    nodes: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    held: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    metrics: dict[str, Any],
    dry_run: bool,
) -> str:
    anchors = sorted(
        nodes,
        key=lambda node: (
            "autoload" not in node["tags"],
            -int(node["priority"]),
            -float(node["salience"]),
        ),
    )[:10]
    recent = sorted(
        [node for node in nodes if node.get("occurred_at")],
        key=lambda node: node["occurred_at"],
        reverse=True,
    )[:8]
    lines = [
        "# LML DREAM SUMMARY",
        "",
        f"Generated: {generated_at}",
        f"Tenant: {tenant_id}",
        f"Scope: {scope}",
        f"Mode: {'dry-run' if dry_run else 'committed'}",
        "",
        "> A deterministic consolidation snapshot, not hidden reasoning or a consciousness claim.",
        "> Current input and source files remain primary. Pending items are not accepted memory.",
        "",
        "## Field regulation",
        "",
        f"- active nodes: {metrics['active_nodes']}",
        f"- protected anchors: {metrics['protected_nodes']}",
        f"- adjusted nodes: {metrics['adjusted_nodes']}",
        f"- semantic proposals: {metrics['semantic_proposals']}",
        f"- pending candidates: {metrics['pending_candidates']}",
        f"- held observations: {metrics['held_observations']}",
        f"- anchored episodes: {metrics['anchored_episodes']}",
        f"- active conflicts: {metrics['conflict_count']}",
        f"- recent coherence: {_format_metric(metrics['mean_recent_coherence'])}",
        f"- recent uncertainty: {_format_metric(metrics['mean_recent_uncertainty'])}",
        f"- recent drift risk: {_format_metric(metrics['mean_recent_drift_risk'])}",
        "",
        "## Stable anchors",
        "",
    ]
    lines.extend(
        f"- `{node['id']}` [{node['kind']}] {node['title']}"
        for node in anchors
    )
    lines.extend(["", "## Recent evidence", ""])
    if recent:
        lines.extend(
            f"- {node['occurred_at']} — `{node['id']}`: {node['summary']}"
            for node in recent
        )
    else:
        lines.append("- None.")
    lines.extend(["", "## Held observations", ""])
    if held:
        held_ranked = sorted(
            held,
            key=lambda item: (
                -float(item.get("importance", 0.0)),
                item.get("occurred_at") or "",
            ),
        )
        lines.extend(
            f"- `{item['id']}` importance={float(item.get('importance', 0.0)):.2f}: "
            f"{item['title']}"
            for item in held_ranked[:8]
        )
    else:
        lines.append("- None.")
    lines.extend(["", "## Pending review", ""])
    if pending:
        lines.extend(
            f"- `{item['id']}` [{item['sensitivity']}] {item['title']}"
            for item in pending[:12]
        )
    else:
        lines.append("- None.")
    lines.extend(["", "## Conflicts", ""])
    if conflicts:
        lines.extend(
            f"- `{edge['src_id']}` {edge['relation']} `{edge['dst_id']}`"
            for edge in conflicts
        )
    else:
        lines.append("- None detected among active nodes.")
    return "\n".join(lines).strip() + "\n"


def _run_id(tenant_id: str, scope: str, trigger: str, started_at: str) -> str:
    raw = json.dumps(
        [tenant_id, scope, trigger, started_at],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "dream-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def _format_metric(value: float | None) -> str:
    return "not enough observations" if value is None else f"{value:.2f}"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
