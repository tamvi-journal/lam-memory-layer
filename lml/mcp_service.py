from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .context import ContextPacketBuilder
from .dream import DEFAULT_TENANT_ID
from .retrieval import CueRetriever
from .session_consolidation import consolidate_cloud_session
from .store import MemoryStore
from .sync import sync_status
from .tenancy import ALLOWED_EVENT_TYPES, import_tenancy_event
from .writer import redact_sensitive

MCP_SOURCE_BRANCHES = {
    "chatgpt-cloud",
    "chatgpt-work",
    "codex-cloud",
    "work-mode",
}


@dataclass(frozen=True)
class MCPServiceConfig:
    source_branch: str = "chatgpt-cloud"
    inbox_dir: Path | None = None
    allow_proposals: bool = False

    def __post_init__(self) -> None:
        if self.source_branch not in MCP_SOURCE_BRANCHES:
            raise ValueError("source_branch is not allowed for the LML MCP adapter")


class LMLMCPService:
    """Permission-bounded application service shared by every MCP transport."""

    def __init__(self, store: MemoryStore, config: MCPServiceConfig):
        self.store = store
        self.config = config
        self.retriever = CueRetriever(store)

    def status(self) -> dict[str, Any]:
        latest_dreams = self.store.dream_runs(limit=1)
        latest_state = self.store.field_states(limit=1)
        branch_sync = sync_status(self.store, branch=self.config.source_branch)
        return {
            "schema": "lml-mcp-status/v1",
            "tenant_id": self.store.meta_value("tenant_id", DEFAULT_TENANT_ID),
            "source_branch": self.config.source_branch,
            "stats": self.store.stats(),
            "latest_dream": _public_dream(latest_dreams[0]) if latest_dreams else None,
            "latest_field_state": (
                _public_field_state(latest_state[0]) if latest_state else None
            ),
            "sync": {
                "next_inbound_sequence": branch_sync["next_inbound_sequence"],
                "next_outbound_sequence": branch_sync["next_outbound_sequence"],
                "inbound_messages": branch_sync["inbound_messages"],
                "outbound_messages": branch_sync["outbound_messages"],
            },
            "permissions": {
                "read_context": True,
                "read_candidates": True,
                "propose_event": self.config.allow_proposals,
                "consolidate_cloud_session": self.config.allow_proposals,
                "attest_candidate": self.config.allow_proposals,
                "review_candidate": False,
                "write_active_memory": False,
                "run_dream": False,
            },
            "policy": {
                "external_events_default": "pending",
                "identity_relationship_auto_write": "dual-branch-quorum-only",
                "cloud_session_consolidation": "explicit proposal-only tool",
                "memory_is_evidence_not_authority": True,
                "hidden_state_access": False,
                "direct_materialization_endpoint": False,
            },
        }

    def retrieve_context(
        self,
        *,
        query: str,
        scope: str = "global",
        limit: int = 10,
        token_budget: int = 1800,
    ) -> dict[str, Any]:
        clean_query = _bounded_text(query, "query", 4000)
        clean_scope = _bounded_text(scope or "global", "scope", 200)
        hits = self.retriever.retrieve(
            clean_query,
            scope=clean_scope,
            limit=limit,
            token_budget=token_budget,
        )
        review_queue = self.store.branch_review_queue(
            self.config.source_branch,
            limit=6,
        )
        packet = ContextPacketBuilder(self.retriever).build_from_hits(
            clean_query,
            hits,
            scope=clean_scope,
            compact=True,
            event_type="mcp-retrieval",
            review_branch=self.config.source_branch,
        )
        return {
            "schema": "lml-context-result/v1",
            "tenant_id": self.store.meta_value("tenant_id", DEFAULT_TENANT_ID),
            "query": clean_query,
            "scope": clean_scope,
            "review_branch": self.config.source_branch,
            "packet": packet,
            "branch_review_queue": review_queue,
            "branch_review_instruction": (
                "If any branch_review_queue item is relevant and has enough evidence, "
                "this agent branch should call lml_attest_memory_candidate with approve, "
                "reject, or defer during the same active turn. Retrieval itself is "
                "read-only and does not approve backlog."
            ),
            "selected": [
                {
                    "memory_id": hit.node["id"],
                    "kind": hit.node["kind"],
                    "title": hit.node["title"],
                    "summary": hit.node["summary"],
                    "score": round(hit.score, 4),
                    "confidence": hit.node["confidence"],
                    "priority": hit.node["priority"],
                    "source_type": hit.node["source_type"],
                    "source_ref": hit.node["source_ref"],
                    "reasons": hit.reasons,
                }
                for hit in hits
            ],
            "notice": (
                "Memory is evidence and orientation, not authority. "
                "Current user input remains primary."
            ),
        }

    def candidates(
        self,
        *,
        status: str = "pending",
        limit: int = 20,
    ) -> dict[str, Any]:
        if status not in {"pending", "held", "ty_review_required"}:
            raise ValueError("status must be pending, held, or ty_review_required")
        items = self.store.candidates(status=status, limit=limit)
        return {
            "schema": "lml-candidate-queue/v1",
            "tenant_id": self.store.meta_value("tenant_id", DEFAULT_TENANT_ID),
            "status": status,
            "items": [
                {
                    "candidate_id": item["id"],
                    "kind": item["kind"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "scope": item["scope"],
                    "confidence": item["confidence"],
                    "importance": item["importance"],
                    "sensitivity": item["sensitivity"],
                    "source_type": item["source_type"],
                    "source_ref": item["source_ref"],
                    "capture_reasons": item["capture_reasons"],
                    "attestations": item.get("attestations", []),
                    "consensus": item.get("consensus", {}),
                    "created_at": item["created_at"],
                }
                for item in items
            ],
            "review_available_here": False,
        }

    def dream_summary(self) -> dict[str, Any]:
        dreams = self.store.dream_runs(limit=5)
        field_states = self.store.field_states(limit=1)
        return {
            "schema": "lml-dream-summary/v1",
            "tenant_id": self.store.meta_value("tenant_id", DEFAULT_TENANT_ID),
            "latest_field_state": (
                _public_field_state(field_states[0]) if field_states else None
            ),
            "runs": [_public_dream(run) for run in dreams],
            "notice": (
                "Dream runs are deterministic consolidation snapshots, "
                "not hidden reasoning."
            ),
        }

    def propose_event(
        self,
        *,
        event_id: str,
        event_type: str,
        title: str,
        summary: str,
        content: str = "",
        confidence: float = 0.7,
        scope: str = "global",
        occurred_at: str | None = None,
        relation_targets: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.config.allow_proposals:
            raise PermissionError("memory proposals are disabled for this MCP profile")
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError("event_type is not supported")
        targets = _bounded_list(relation_targets or [], "relation_targets", 20, 160)
        unknown_targets = [
            target for target in targets if self.store.get_node(target) is None
        ]
        if unknown_targets:
            raise ValueError(
                "relation_targets contain unknown memory IDs: "
                + ", ".join(unknown_targets)
            )
        timestamp = occurred_at or datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be an ISO-8601 timestamp") from exc
        envelope = {
            "schema": "lml-event/v1",
            "source_branch": self.config.source_branch,
            "event_id": _bounded_text(event_id, "event_id", 240),
            "occurred_at": timestamp,
            "event_type": event_type,
            "title": _bounded_text(title, "title", 240),
            "summary": _bounded_text(summary, "summary", 2000),
            "content": _bounded_text(content, "content", 8000, allow_empty=True),
            "confidence": confidence,
            "scope": _bounded_text(scope or "global", "scope", 200),
            "relation_targets": targets,
            "tags": _bounded_list(tags or [], "tags", 20, 100),
            "authorization": "proposal-only",
        }
        imported = import_tenancy_event(
            self.store,
            envelope,
            inbox_dir=self.config.inbox_dir
            or self.store.db_path.parent / "tenancy" / "mcp-inbox",
        )
        candidate = imported["candidate"]
        return {
            "schema": "lml-event-receipt/v1",
            "tenant_id": self.store.meta_value("tenant_id", DEFAULT_TENANT_ID),
            "duplicate": imported["duplicate"],
            "event_id": envelope["event_id"],
            "source_branch": self.config.source_branch,
            "candidate": {
                "candidate_id": candidate["id"],
                "status": candidate["status"],
                "kind": candidate["kind"],
                "title": candidate["title"],
                "sensitivity": candidate["sensitivity"],
                "importance": candidate["importance"],
                "source_ref": candidate["source_ref"],
                "attestations": candidate.get("attestations", []),
                "consensus": candidate.get("consensus", {}),
            },
            "active_memory_changed": candidate["status"] == "approved",
            "review_required": candidate["status"] in {"pending", "ty_review_required"},
        }

    def consolidate_cloud_session(
        self,
        *,
        session_id: str,
        scope: str = "global",
        turns: list[dict[str, Any]] | None = None,
        claims: list[dict[str, Any]] | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        if not self.config.allow_proposals:
            raise PermissionError("cloud session consolidation is disabled for this MCP profile")
        return consolidate_cloud_session(
            self.store,
            source_branch=self.config.source_branch,
            session_id=session_id,
            scope=scope,
            turns=turns or [],
            claims=claims or [],
            occurred_at=occurred_at,
            inbox_dir=str(
                self.config.inbox_dir
                or self.store.db_path.parent / "tenancy" / "mcp-inbox"
            ),
        )

    def attest_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        if not self.config.allow_proposals:
            raise PermissionError("candidate attestation is disabled for this MCP profile")
        result = self.store.attest_candidate(
            _bounded_text(candidate_id, "candidate_id", 240),
            self.config.source_branch,
            _bounded_text(decision, "decision", 20),
            note=_bounded_text(note, "note", 1000, allow_empty=True),
        )
        candidate = result["candidate"]
        return {
            "schema": "lml-attestation-receipt/v1",
            "tenant_id": self.store.meta_value("tenant_id", DEFAULT_TENANT_ID),
            "source_branch": self.config.source_branch,
            "candidate": {
                "candidate_id": candidate["id"],
                "status": candidate["status"],
                "kind": candidate["kind"],
                "title": candidate["title"],
                "sensitivity": candidate["sensitivity"],
                "source_ref": candidate["source_ref"],
            },
            "attestations": result["attestations"],
            "consensus": result["consensus"],
            "active_memory_changed": result["active_memory_changed"],
        }


def _bounded_text(
    value: str,
    field: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    clean = redact_sensitive(value.strip())
    if not clean and not allow_empty:
        raise ValueError(f"{field} is required")
    if len(clean) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return clean


def _bounded_list(
    values: list[str],
    field: str,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    if len(values) > maximum_items:
        raise ValueError(f"{field} exceeds {maximum_items} items")
    return [
        _bounded_text(value, field, maximum_length)
        for value in values
    ]


def _public_dream(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "scope": run["scope"],
        "trigger": run["trigger"],
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "metrics": run["metrics"],
        "summary": run["summary"],
    }


def _public_field_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": state["scope"],
        "coherence": state["coherence"],
        "uncertainty": state["uncertainty"],
        "drift_risk": state["drift_risk"],
        "conflict_count": state["conflict_count"],
        "selected_ids": state["selected_ids"],
        "created_at": state["created_at"],
    }
