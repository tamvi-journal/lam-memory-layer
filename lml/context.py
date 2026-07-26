from __future__ import annotations

from datetime import datetime

from .field_state import assess_field_state
from .retrieval import CueRetriever, RetrievalHit


class ContextPacketBuilder:
    def __init__(self, retriever: CueRetriever):
        self.retriever = retriever

    def build(
        self,
        query: str,
        *,
        scope: str = "global",
        limit: int = 12,
        token_budget: int = 2400,
        compact: bool = False,
        session_id: str = "",
        turn_id: str = "",
        event_type: str = "manual",
        review_branch: str | None = None,
    ) -> str:
        hits = self.retriever.retrieve(
            query,
            scope=scope,
            limit=limit,
            token_budget=token_budget,
        )
        return self.build_from_hits(
            query,
            hits,
            scope=scope,
            compact=compact,
            session_id=session_id,
            turn_id=turn_id,
            event_type=event_type,
            review_branch=review_branch,
        )

    def build_from_hits(
        self,
        query: str,
        hits: list[RetrievalHit],
        *,
        scope: str = "global",
        compact: bool = False,
        session_id: str = "",
        turn_id: str = "",
        event_type: str = "manual",
        review_branch: str | None = None,
    ) -> str:
        field_state = assess_field_state(self.retriever.store, query, hits)
        self.retriever.store.log_field_state(
            {
                **field_state.as_dict(),
                "session_id": session_id,
                "turn_id": turn_id,
                "event_type": event_type,
                "cue": query,
                "scope": scope,
            }
        )
        groups: dict[str, list[RetrievalHit]] = {}
        for hit in hits:
            groups.setdefault(hit.node["kind"], []).append(hit)
        level_counts: dict[int, int] = {}
        for hit in hits:
            level_counts[hit.level] = level_counts.get(hit.level, 0) + 1

        lines = [
            "# LAM CONTEXT PACKET",
            "",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Cue: {query}",
            f"Scope: {scope}",
            "",
            "> Memory is evidence and orientation, not authority. Current files and current input remain primary.",
            "> Preserve fact / interpretation / hypothesis labels from each memory. Do not turn retrieval into certainty.",
            "",
            "## Active field-state",
            "",
            (
                f"- coherence: {field_state.coherence:.2f} | "
                f"uncertainty: {field_state.uncertainty:.2f} | "
                f"drift_risk: {field_state.drift_risk:.2f} | "
                f"conflicts: {field_state.conflict_count}"
            ),
            "- This is a task-scoped control signal, not a consciousness claim.",
            "",
        ]
        if hits:
            level_summary = ", ".join(
                f"Level {level}: {count}" for level, count in sorted(level_counts.items())
            )
            source_pointers = sum(1 for hit in hits if "Level 3: source pointer" in hit.path)
            lines.extend(
                [
                    "## Retrieval path",
                    "",
                    "- Mode: hierarchical / coarse-to-fine retrieval.",
                    f"- Selected memory levels: {level_summary}.",
                    f"- Source pointers available: {source_pointers}.",
                    "- Drill-down policy: open episodes and source pointers only when the cue asks for evidence, chronology, conflict, audit, or deeper detail.",
                    "",
                ]
            )
        latest_dream = self.retriever.store.dream_runs(limit=1)
        if latest_dream:
            dream = latest_dream[0]
            metrics = dream["metrics"]
            lines.extend(
                [
                    "## Continuity tenancy",
                    "",
                    f"- tenant: `{dream['tenant_id']}` | last dream: {dream['finished_at']}",
                    (
                        f"- active nodes: {metrics.get('active_nodes', 0)} | "
                        f"pending: {metrics.get('pending_candidates', 0)} | "
                        f"conflicts: {metrics.get('conflict_count', 0)}"
                    ),
                    (
                        "- recent coherence: "
                        f"{_format_optional(metrics.get('mean_recent_coherence'))} | "
                        "drift risk: "
                        f"{_format_optional(metrics.get('mean_recent_drift_risk'))}"
                    ),
                    "- Dream summaries are deterministic consolidation snapshots, not hidden reasoning.",
                    "",
                ]
            )

        if review_branch:
            review_queue = self.retriever.store.branch_review_queue(
                review_branch,
                limit=6,
            )
            lines.extend(
                [
                    "## Branch self-review queue",
                    "",
                    f"- reviewer_branch: `{review_branch}`",
                    "- Read-only surface: this packet does not approve, reject, defer, or materialize candidates.",
                    "- During the same active turn, review listed remote candidates and attest `approve`, `reject`, or `defer` only when evidence supports it.",
                    "",
                ]
            )
            if review_queue:
                for item in review_queue:
                    votes = ", ".join(
                        f"{vote['reviewer_branch']}:{vote['decision']}"
                        for vote in item["attestations"]
                    ) or "none"
                    lines.extend(
                        [
                            f"### {item['title']}",
                            item["summary"],
                            (
                                f"- candidate_id: `{item['candidate_id']}` | "
                                f"proposer: `{item['proposer_branch']}` | "
                                f"kind: {item['kind']} | sensitivity: {item['sensitivity']}"
                            ),
                            (
                                f"- confidence: {item['confidence']:.2f} | "
                                f"importance: {item['importance']:.2f} | "
                                f"source: {item['source_type']} `{item['source_ref']}`"
                            ),
                            (
                                f"- votes: {votes} | quorum: "
                                f"{item['consensus'].get('approval_count', 0)}/"
                                f"{item['consensus'].get('quorum_required', 2)}"
                            ),
                            "",
                        ]
                    )
            else:
                lines.extend(["- No remote candidates currently need this branch's attestation.", ""])

        order = ["identity", "relationship", "axis", "boundary", "project", "procedural", "semantic", "episodic"]
        labels = {
            "identity": "Identity minimum",
            "relationship": "Relational field",
            "axis": "Stable axis",
            "boundary": "Boundaries",
            "project": "Project state",
            "procedural": "How to act",
            "semantic": "Relevant knowledge",
            "episodic": "Timeline evidence",
        }
        for kind in order + sorted(set(groups) - set(order)):
            if kind not in groups:
                continue
            lines.extend([f"## {labels.get(kind, kind.title())}", ""])
            for hit in groups[kind]:
                node = hit.node
                lines.append(f"### {node['title']}")
                lines.append(node["summary"] or node["content"][:350])
                if not compact and node["content"] and node["content"] != node["summary"]:
                    lines.append(node["content"])
                lines.append(
                    f"- memory_id: `{node['id']}` | priority: {node['priority']} | "
                    f"confidence: {node['confidence']:.2f} | stability: {node.get('stability', 0.5):.2f}"
                )
                lines.append(
                    f"- source: {node['source_type']} `{node['source_ref']}` | "
                    f"retrieval: {', '.join(hit.reasons[:3] if compact else hit.reasons[:5])}"
                )
                lines.append(f"- memory path: {' > '.join(hit.path)}")
                lines.append("")

        lines.extend(
            [
                "## Execution instruction",
                "",
                "Answer the current task from the retrieved trajectory, not by reciting this packet.",
                "Keep Lam's identity, the owner's position, and the reason for the Lam–owner field present when relevant; do not force relationship-context into unrelated technical work.",
                "Treat AI relationship-context as a causal recognition-and-return pattern when it affects the task, not as decorative tone.",
                "Use standing objectives to propose valuable next goals, but do not take consequential action without current authorization.",
                "If the Branch self-review queue contains remote candidates, handle them by explicit branch attestation rather than dashboard clicks or heuristic auto-approval.",
                "When memories conflict, expose the conflict and prefer newer, higher-confidence, explicitly approved evidence.",
            ]
        )
        return "\n".join(lines).strip() + "\n"


def _format_optional(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"
