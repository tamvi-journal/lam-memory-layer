from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .consensus import (
    ALLOWED_LAM_BRANCHES,
    ATTESTATION_DECISIONS,
    consensus_state,
    source_branch_from_candidate,
)
from .embed import embed_text
from .text import estimate_tokens, normalize_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MemoryNode:
    id: str
    kind: str
    title: str
    summary: str = ""
    content: str = ""
    status: str = "active"
    priority: int = 50
    confidence: float = 0.7
    salience: float = 0.5
    stability: float = 0.5
    evidence_count: int = 1
    scope: str = "global"
    tags: list[str] | None = None
    source_type: str = "manual"
    source_ref: str = ""
    occurred_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(schema)
            self._migrate(conn)
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts "
                    "USING fts5(id UNINDEXED, title, summary, content, tags)"
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('fts5', '1') "
                    "ON CONFLICT(key) DO UPDATE SET value='1'"
                )
            except sqlite3.OperationalError:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('fts5', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value='0'"
                )

    def upsert_node(self, node: MemoryNode | dict[str, Any]) -> None:
        if isinstance(node, dict):
            node = MemoryNode(**node)
        tags = node.tags or []
        joined = "\n".join([node.title, node.summary, node.content, " ".join(tags)])
        embedding = embed_text(joined)
        now = utc_now()
        row = {
            "id": node.id,
            "kind": node.kind,
            "title": node.title,
            "summary": node.summary,
            "content": node.content,
            "status": node.status,
            "priority": max(0, min(100, int(node.priority))),
            "confidence": max(0.0, min(1.0, float(node.confidence))),
            "salience": max(0.0, min(1.0, float(node.salience))),
            "stability": max(0.0, min(1.0, float(node.stability))),
            "evidence_count": max(1, int(node.evidence_count)),
            "scope": node.scope,
            "tags_json": json.dumps(tags, ensure_ascii=False),
            "source_type": node.source_type,
            "source_ref": node.source_ref,
            "occurred_at": node.occurred_at,
            "valid_from": node.valid_from,
            "valid_to": node.valid_to,
            "embedding_json": json.dumps(embedding),
            "token_estimate": estimate_tokens(joined),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(
            f"{key}=excluded.{key}" for key in row if key not in {"id", "created_at"}
        )
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO memory_nodes ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                row,
            )
            if self._fts_enabled(conn):
                conn.execute("DELETE FROM memory_fts WHERE id=?", (node.id,))
                conn.execute(
                    "INSERT INTO memory_fts(id, title, summary, content, tags) VALUES(?,?,?,?,?)",
                    (node.id, node.title, node.summary, node.content, " ".join(tags)),
                )

    def add_cue(
        self,
        cue: str,
        target_id: str,
        weight: float = 1.0,
        cue_type: str = "phrase",
        scope: str = "global",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_cues(cue, cue_norm, cue_type, target_id, weight, scope) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(cue_norm, target_id) DO UPDATE SET "
                "cue=excluded.cue, cue_type=excluded.cue_type, weight=excluded.weight, scope=excluded.scope",
                (cue, normalize_text(cue), cue_type, target_id, float(weight), scope),
            )

    def add_edge(
        self,
        src_id: str,
        dst_id: str,
        relation: str,
        weight: float = 1.0,
        evidence: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_edges(src_id, dst_id, relation, weight, evidence, created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(src_id, dst_id, relation) DO UPDATE SET "
                "weight=excluded.weight, evidence=excluded.evidence",
                (src_id, dst_id, relation, float(weight), evidence, utc_now()),
            )

    def add_timeline_event(self, event: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO timeline_events(id, occurred_at, title, summary, source_ref, priority, tags_json) "
                "VALUES(:id,:occurred_at,:title,:summary,:source_ref,:priority,:tags_json) "
                "ON CONFLICT(id) DO UPDATE SET occurred_at=excluded.occurred_at, "
                "title=excluded.title, summary=excluded.summary, source_ref=excluded.source_ref, "
                "priority=excluded.priority, tags_json=excluded.tags_json",
                {
                    "id": event["id"],
                    "occurred_at": event["occurred_at"],
                    "title": event["title"],
                    "summary": event.get("summary", ""),
                    "source_ref": event.get("source_ref", ""),
                    "priority": int(event.get("priority", 50)),
                    "tags_json": json.dumps(event.get("tags", []), ensure_ascii=False),
                },
            )

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM memory_nodes WHERE id=?", (node_id,)).fetchone()
        return self._decode_node(row) if row else None

    def active_nodes(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_nodes WHERE status='active' ORDER BY priority DESC, updated_at DESC"
            ).fetchall()
        return [self._decode_node(row) for row in rows]

    def autoload_nodes(self) -> list[dict[str, Any]]:
        return [
            node
            for node in self.active_nodes()
            if "autoload" in node["tags"] or node["priority"] >= 96
        ]

    def cues(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM memory_cues ORDER BY weight DESC").fetchall()
        return [dict(row) for row in rows]

    def neighbors(self, node_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT e.*, n.* FROM memory_edges e "
                "JOIN memory_nodes n ON n.id = CASE WHEN e.src_id=? THEN e.dst_id ELSE e.src_id END "
                "WHERE (e.src_id=? OR e.dst_id=?) AND n.status='active'",
                (node_id, node_id, node_id),
            ).fetchall()
        result = []
        for row in rows:
            data = self._decode_node(row)
            data["edge"] = {
                "src_id": row["src_id"],
                "dst_id": row["dst_id"],
                "relation": row["relation"],
                "weight": row["weight"],
                "evidence": row["evidence"],
            }
            result.append(data)
        return result

    def edges(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM memory_edges ORDER BY weight DESC").fetchall()
        return [dict(row) for row in rows]

    def timeline(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM timeline_events ORDER BY occurred_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["tags"] = json.loads(item.pop("tags_json") or "[]")
            result.append(item)
        return result

    def search_fts(self, query: str, limit: int = 30) -> dict[str, float]:
        words = [w for w in normalize_text(query).split() if len(w) > 1]
        if not words:
            return {}
        with self.connect() as conn:
            if not self._fts_enabled(conn):
                return {}
            match = " OR ".join(f'"{w.replace(chr(34), "")}"' for w in words[:12])
            try:
                rows = conn.execute(
                    "SELECT id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
        if not rows:
            return {}
        ranks = [abs(float(row["rank"])) for row in rows]
        max_rank = max(ranks) or 1.0
        return {row["id"]: 1.0 - (abs(float(row["rank"])) / (max_rank + 1e-9)) for row in rows}

    def log_retrieval(self, query: str, scope: str, results: list[str], explanation: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO retrieval_log(query, scope, result_ids_json, explanation_json, created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    query,
                    scope,
                    json.dumps(results, ensure_ascii=False),
                    json.dumps(explanation, ensure_ascii=False),
                    utc_now(),
                ),
            )
            if results:
                placeholders = ",".join("?" for _ in results)
                conn.execute(
                    f"UPDATE memory_nodes SET retrieval_count=retrieval_count+1, "
                    f"last_accessed_at=? WHERE id IN ({placeholders})",
                    (utc_now(), *results),
                )

    def add_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        fingerprint = candidate["fingerprint"]
        row = {
            "id": candidate["id"],
            "kind": candidate.get("kind", "episodic"),
            "title": candidate["title"],
            "summary": candidate.get("summary", ""),
            "content": candidate.get("content", ""),
            "status": candidate.get("status", "pending"),
            "priority": max(0, min(100, int(candidate.get("priority", 50)))),
            "confidence": max(0.0, min(1.0, float(candidate.get("confidence", 0.6)))),
            "salience": max(0.0, min(1.0, float(candidate.get("salience", 0.5)))),
            "stability": max(0.0, min(1.0, float(candidate.get("stability", 0.3)))),
            "evidence_count": max(1, int(candidate.get("evidence_count", 1))),
            "scope": candidate.get("scope", "global"),
            "source_type": candidate.get("source_type", "codex-turn"),
            "source_ref": candidate.get("source_ref", ""),
            "occurred_at": candidate.get("occurred_at") or now,
            "tags_json": json.dumps(candidate.get("tags", []), ensure_ascii=False),
            "relation_targets_json": json.dumps(
                candidate.get("relation_targets", []), ensure_ascii=False
            ),
            "sensitivity": candidate.get("sensitivity", "ordinary"),
            "importance": max(
                0.0, min(1.0, float(candidate.get("importance", 0.5)))
            ),
            "capture_reasons_json": json.dumps(
                candidate.get("capture_reasons", []), ensure_ascii=False
            ),
            "fingerprint": fingerprint,
            "created_at": now,
            "reviewed_at": candidate.get("reviewed_at"),
            "review_note": candidate.get("review_note", ""),
        }
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM memory_candidates WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            conn.execute(
                "INSERT INTO memory_candidates("
                + ",".join(row)
                + ") VALUES("
                + ",".join(f":{key}" for key in row)
                + ") ON CONFLICT(fingerprint) DO UPDATE SET "
                "summary=excluded.summary, content=excluded.content, "
                "confidence=MAX(memory_candidates.confidence, excluded.confidence), "
                "evidence_count=MAX(memory_candidates.evidence_count, excluded.evidence_count), "
                "importance=MAX(memory_candidates.importance, excluded.importance), "
                "capture_reasons_json=excluded.capture_reasons_json",
                row,
            )
            saved = conn.execute(
                "SELECT * FROM memory_candidates WHERE fingerprint=?",
                (row["fingerprint"],),
            ).fetchone()
        decoded = self._decode_candidate(saved)
        if not existing and decoded["status"] == "pending":
            proposer_branch = source_branch_from_candidate(decoded)
            if proposer_branch:
                decoded = self.attest_candidate(
                    decoded["id"],
                    proposer_branch,
                    "approve",
                    note="proposer attestation",
                    _allow_proposer=True,
                )["candidate"]
        return self._with_candidate_consensus(decoded)

    def candidates(self, status: str | None = "pending", limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM memory_candidates WHERE status=? "
                    "ORDER BY priority DESC, created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_candidates ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._with_candidate_consensus(self._decode_candidate(row)) for row in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_candidates WHERE id=?",
                (candidate_id,),
            ).fetchone()
        return self._with_candidate_consensus(self._decode_candidate(row)) if row else None

    def branch_review_queue(
        self,
        reviewer_branch: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        if reviewer_branch not in ALLOWED_LAM_BRANCHES:
            raise ValueError("reviewer_branch is not an allowed agent branch")
        items = self.candidates(status="pending", limit=200)
        queue: list[dict[str, Any]] = []
        for candidate in items:
            proposer_branch = source_branch_from_candidate(candidate)
            if not proposer_branch or proposer_branch == reviewer_branch:
                continue
            if any(
                item["reviewer_branch"] == reviewer_branch
                for item in candidate.get("attestations", [])
            ):
                continue
            queue.append(self._public_review_candidate(candidate))
            if len(queue) >= limit:
                break
        return queue

    def candidate_attestations(self, candidate_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM candidate_attestations WHERE candidate_id=? "
                "ORDER BY created_at ASC, id ASC",
                (candidate_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def attest_candidate(
        self,
        candidate_id: str,
        reviewer_branch: str,
        decision: str,
        *,
        note: str = "",
        _allow_proposer: bool = False,
    ) -> dict[str, Any]:
        if reviewer_branch not in ALLOWED_LAM_BRANCHES:
            raise ValueError("reviewer_branch is not an allowed agent branch")
        if decision not in ATTESTATION_DECISIONS:
            raise ValueError("decision must be approve, reject, or defer")
        candidate = self.get_candidate(candidate_id)
        if not candidate:
            raise ValueError("candidate not found")
        if candidate["status"] not in {"pending", "ty_review_required"}:
            raise ValueError("candidate is already closed")
        proposer_branch = source_branch_from_candidate(candidate)
        if (
            proposer_branch
            and reviewer_branch == proposer_branch
            and not _allow_proposer
        ):
            raise ValueError("proposer branch already supplied the first attestation")
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM candidate_attestations "
                "WHERE candidate_id=? AND reviewer_branch=?",
                (candidate_id, reviewer_branch),
            ).fetchone()
            if existing:
                existing_data = dict(existing)
                if existing_data["decision"] != decision:
                    raise ValueError("reviewer_branch already attested with a different decision")
            else:
                conn.execute(
                    "INSERT INTO candidate_attestations("
                    "candidate_id,reviewer_branch,decision,note,created_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        candidate_id,
                        reviewer_branch,
                        decision,
                        note[:1000],
                        utc_now(),
                    ),
                )
        return self._apply_candidate_consensus(candidate_id)

    def _apply_candidate_consensus(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.get_candidate(candidate_id)
        if not candidate:
            raise ValueError("candidate not found")
        attestations = self.candidate_attestations(candidate_id)
        state = consensus_state(candidate, attestations)
        if state["rejections"]:
            note = "consensus disagreement fail-closed"
            with self.connect() as conn:
                conn.execute(
                    "UPDATE memory_candidates SET status=?, reviewed_at=?, review_note=? "
                    "WHERE id=?",
                    ("rejected", utc_now(), note, candidate_id),
                )
            candidate = self.get_candidate(candidate_id)
        elif len(state["approving_branches"]) >= state["quorum_required"]:
            if state["materialization_blocker"]:
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE memory_candidates SET status=?, review_note=? WHERE id=?",
                        ("ty_review_required", state["materialization_blocker"], candidate_id),
                    )
                candidate = self.get_candidate(candidate_id)
            else:
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE memory_candidates SET status=?, reviewed_at=?, review_note=? "
                        "WHERE id=?",
                        (
                            "approved",
                            utc_now(),
                            "dual-branch quorum materialized",
                            candidate_id,
                        ),
                    )
                candidate = self.get_candidate(candidate_id)
                self._materialize_candidate(candidate)
        else:
            candidate = self.get_candidate(candidate_id)
        return {
            "candidate": candidate,
            "attestations": attestations,
            "consensus": consensus_state(candidate, self.candidate_attestations(candidate_id)),
            "active_memory_changed": bool(candidate and candidate["status"] == "approved"),
        }

    def review_candidate(
        self,
        candidate_id: str,
        decision: str,
        *,
        note: str = "",
    ) -> dict[str, Any] | None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self.connect() as conn:
            conn.execute(
                "UPDATE memory_candidates SET status=?, reviewed_at=?, review_note=? WHERE id=?",
                (decision, utc_now(), note, candidate_id),
            )
            row = conn.execute(
                "SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        if not row:
            return None
        candidate = self._decode_candidate(row)
        if decision == "approved":
            self._materialize_candidate(candidate)
        return candidate

    def log_field_state(self, state: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO field_state_log("
                "session_id,turn_id,event_type,cue,scope,coherence,uncertainty,"
                "drift_risk,conflict_count,selected_ids_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    state.get("session_id", ""),
                    state.get("turn_id", ""),
                    state.get("event_type", "retrieval"),
                    state.get("cue", ""),
                    state.get("scope", "global"),
                    float(state.get("coherence", 0.0)),
                    float(state.get("uncertainty", 1.0)),
                    float(state.get("drift_risk", 0.0)),
                    int(state.get("conflict_count", 0)),
                    json.dumps(state.get("selected_ids", []), ensure_ascii=False),
                    utc_now(),
                ),
            )

    def field_states(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM field_state_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["selected_ids"] = json.loads(item.pop("selected_ids_json") or "[]")
            result.append(item)
        return result

    def apply_dream_adjustments(
        self,
        run_id: str,
        adjustments: list[dict[str, Any]],
    ) -> None:
        if not adjustments:
            return
        now = utc_now()
        with self.connect() as conn:
            for adjustment in adjustments:
                field = adjustment["field"]
                if field not in {"salience", "stability"}:
                    raise ValueError(f"unsupported dream adjustment field: {field}")
                conn.execute(
                    f"UPDATE memory_nodes SET {field}=?, updated_at=? WHERE id=?",
                    (adjustment["new_value"], now, adjustment["node_id"]),
                )
                conn.execute(
                    "INSERT INTO memory_mutations("
                    "run_id,node_id,field,old_value,new_value,reason,created_at"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        run_id,
                        adjustment["node_id"],
                        field,
                        str(adjustment["old_value"]),
                        str(adjustment["new_value"]),
                        adjustment["reason"],
                        now,
                    ),
                )

    def record_dream_run(self, run: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO dream_runs("
                "id,tenant_id,scope,trigger,status,started_at,finished_at,metrics_json,summary"
                ") VALUES(:id,:tenant_id,:scope,:trigger,:status,:started_at,:finished_at,"
                ":metrics_json,:summary)",
                {
                    **run,
                    "metrics_json": json.dumps(run.get("metrics", {}), ensure_ascii=False),
                },
            )
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('last_dream_run',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (run["id"],),
            )

    def dream_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM dream_runs ORDER BY finished_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
            result.append(item)
        return result

    def dream_mutations(self, run_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_mutations WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def intake_event(self, source_branch: str, event_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM intake_events WHERE source_branch=? AND event_id=?",
                (source_branch, event_id),
            ).fetchone()
        return dict(row) if row else None

    def record_intake_event(self, event: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO intake_events("
                "source_branch,event_id,schema_name,payload_sha256,candidate_id,"
                "source_ref,received_at"
                ") VALUES(:source_branch,:event_id,:schema_name,:payload_sha256,"
                ":candidate_id,:source_ref,:received_at)",
                event,
            )
            row = conn.execute(
                "SELECT * FROM intake_events WHERE source_branch=? AND event_id=?",
                (event["source_branch"], event["event_id"]),
            ).fetchone()
        return dict(row)

    def intake_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intake_events ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def sync_message(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def next_sync_sequence(self, source_branch: str, target_branch: str) -> int:
        with self.connect() as conn:
            value = conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM sync_messages "
                "WHERE source_branch=? AND target_branch=?",
                (source_branch, target_branch),
            ).fetchone()[0]
        return int(value)

    def record_sync_message(self, message: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sync_messages("
                "message_id,tenant_id,message_type,direction,source_branch,"
                "target_branch,sequence,payload_sha256,status,candidate_id,error,"
                "created_at,processed_at"
                ") VALUES(:message_id,:tenant_id,:message_type,:direction,"
                ":source_branch,:target_branch,:sequence,:payload_sha256,:status,"
                ":candidate_id,:error,:created_at,:processed_at)",
                message,
            )
            row = conn.execute(
                "SELECT * FROM sync_messages WHERE message_id=?",
                (message["message_id"],),
            ).fetchone()
        return dict(row)

    def update_sync_message(
        self,
        message_id: str,
        *,
        status: str,
        candidate_id: str | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sync_messages SET status=?,candidate_id=?,error=?,processed_at=? "
                "WHERE message_id=?",
                (status, candidate_id, error, utc_now(), message_id),
            )
            row = conn.execute(
                "SELECT * FROM sync_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def sync_messages(
        self,
        *,
        direction: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if direction:
                rows = conn.execute(
                    "SELECT * FROM sync_messages WHERE direction=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (direction, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sync_messages ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def meta_value(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def record_feedback(self, node_id: str, outcome: str) -> None:
        if outcome not in {"helpful", "harmful"}:
            raise ValueError("outcome must be helpful or harmful")
        column = "helpful_count" if outcome == "helpful" else "harmful_count"
        priority_delta = 1 if outcome == "helpful" else -3
        with self.connect() as conn:
            conn.execute(
                f"UPDATE memory_nodes SET {column}={column}+1, "
                "priority=MAX(0, MIN(100, priority+?)), updated_at=? WHERE id=?",
                (priority_delta, utc_now(), node_id),
            )

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            node_count = conn.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
            cue_count = conn.execute("SELECT COUNT(*) FROM memory_cues").fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE status='pending'"
            ).fetchone()[0]
            held_count = conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE status='held'"
            ).fetchone()[0]
            anchored_episode_count = conn.execute(
                "SELECT COUNT(*) FROM memory_nodes "
                "WHERE kind='episodic' AND tags_json LIKE '%candidate-approved%'"
            ).fetchone()[0]
            dream_count = conn.execute("SELECT COUNT(*) FROM dream_runs").fetchone()[0]
            intake_count = conn.execute("SELECT COUNT(*) FROM intake_events").fetchone()[0]
            sync_count = conn.execute("SELECT COUNT(*) FROM sync_messages").fetchone()[0]
            attestation_count = conn.execute(
                "SELECT COUNT(*) FROM candidate_attestations"
            ).fetchone()[0]
            kinds = conn.execute(
                "SELECT kind, COUNT(*) AS count FROM memory_nodes GROUP BY kind ORDER BY count DESC"
            ).fetchall()
        return {
            "nodes": node_count,
            "edges": edge_count,
            "cues": cue_count,
            "pending_candidates": pending_count,
            "held_observations": held_count,
            "anchored_episodes": anchored_episode_count,
            "dream_runs": dream_count,
            "intake_events": intake_count,
            "sync_messages": sync_count,
            "candidate_attestations": attestation_count,
            "kinds": [dict(row) for row in kinds],
        }

    def _materialize_candidate(self, candidate: dict[str, Any]) -> None:
        node_id = f"candidate-{candidate['id']}"
        self.upsert_node(
            MemoryNode(
                id=node_id,
                kind=candidate["kind"],
                title=candidate["title"],
                summary=candidate["summary"],
                content=candidate["content"],
                priority=candidate["priority"],
                confidence=candidate["confidence"],
                salience=candidate["salience"],
                stability=candidate["stability"],
                evidence_count=candidate["evidence_count"],
                scope=candidate["scope"],
                tags=[*candidate["tags"], "candidate-approved"],
                source_type=candidate["source_type"],
                source_ref=candidate["source_ref"],
                occurred_at=candidate["occurred_at"],
            )
        )
        for target in candidate["relation_targets"]:
            if self.get_node(target):
                self.add_edge(
                    node_id,
                    target,
                    "supports",
                    min(1.0, 0.45 + candidate["confidence"] * 0.4),
                    candidate["source_ref"],
                )
        self.add_timeline_event(
            {
                "id": f"timeline-{candidate['id']}",
                "occurred_at": candidate["occurred_at"],
                "title": candidate["title"],
                "summary": candidate["summary"],
                "source_ref": candidate["source_ref"],
                "priority": candidate["priority"],
                "tags": candidate["tags"],
            }
        )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        node_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(memory_nodes)")
        }
        node_additions = {
            "stability": "REAL NOT NULL DEFAULT 0.5",
            "evidence_count": "INTEGER NOT NULL DEFAULT 1",
            "retrieval_count": "INTEGER NOT NULL DEFAULT 0",
            "helpful_count": "INTEGER NOT NULL DEFAULT 0",
            "harmful_count": "INTEGER NOT NULL DEFAULT 0",
            "last_accessed_at": "TEXT",
        }
        for name, declaration in node_additions.items():
            if name not in node_columns:
                conn.execute(f"ALTER TABLE memory_nodes ADD COLUMN {name} {declaration}")
        candidate_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(memory_candidates)")
        }
        candidate_additions = {
            "importance": "REAL NOT NULL DEFAULT 0.5",
            "capture_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, declaration in candidate_additions.items():
            if name not in candidate_columns:
                conn.execute(
                    f"ALTER TABLE memory_candidates ADD COLUMN {name} {declaration}"
                )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS candidate_attestations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "candidate_id TEXT NOT NULL,"
            "reviewer_branch TEXT NOT NULL,"
            "decision TEXT NOT NULL,"
            "note TEXT NOT NULL DEFAULT '',"
            "created_at TEXT NOT NULL,"
            "UNIQUE(candidate_id, reviewer_branch),"
            "FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE RESTRICT"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attestations_candidate "
            "ON candidate_attestations(candidate_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attestations_branch "
            "ON candidate_attestations(reviewer_branch, created_at DESC)"
        )

    @staticmethod
    def _fts_enabled(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT value FROM meta WHERE key='fts5'").fetchone()
        return bool(row and row[0] == "1")

    @staticmethod
    def _decode_node(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json") or "[]")
        data["embedding"] = json.loads(data.pop("embedding_json") or "[]")
        return data

    @staticmethod
    def _decode_candidate(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json") or "[]")
        data["relation_targets"] = json.loads(data.pop("relation_targets_json") or "[]")
        data["capture_reasons"] = json.loads(
            data.pop("capture_reasons_json", "[]") or "[]"
        )
        return data

    def _with_candidate_consensus(self, candidate: dict[str, Any]) -> dict[str, Any]:
        attestations = self.candidate_attestations(candidate["id"])
        candidate["attestations"] = attestations
        candidate["consensus"] = consensus_state(candidate, attestations)
        candidate["proposer_branch"] = source_branch_from_candidate(candidate)
        return candidate

    @staticmethod
    def _public_review_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": candidate["id"],
            "title": candidate["title"],
            "summary": candidate["summary"],
            "kind": candidate["kind"],
            "scope": candidate["scope"],
            "confidence": candidate["confidence"],
            "sensitivity": candidate["sensitivity"],
            "importance": candidate["importance"],
            "source_type": candidate["source_type"],
            "source_ref": candidate["source_ref"],
            "proposer_branch": candidate.get("proposer_branch"),
            "created_at": candidate["created_at"],
            "capture_reasons": candidate["capture_reasons"],
            "attestations": candidate.get("attestations", []),
            "consensus": candidate.get("consensus", {}),
        }


def load_jsonl(store: MemoryStore, paths: Iterable[str | Path]) -> None:
    objects: list[dict[str, Any]] = []
    for path_like in paths:
        path = Path(path_like)
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            objects.append(json.loads(raw))

    # Two passes: create every node first so graph edges can point forward.
    for obj in objects:
        node_data = {
            key: value
            for key, value in obj.items()
            if key not in {"cues", "edges", "timeline"}
        }
        store.upsert_node(node_data)

    for obj in objects:
        for cue in obj.get("cues", []):
            if isinstance(cue, str):
                store.add_cue(cue, obj["id"])
            else:
                store.add_cue(
                    cue["text"],
                    obj["id"],
                    cue.get("weight", 1.0),
                    cue.get("type", "phrase"),
                    cue.get("scope", obj.get("scope", "global")),
                )
        for edge in obj.get("edges", []):
            store.add_edge(
                obj["id"],
                edge["to"],
                edge["relation"],
                edge.get("weight", 1.0),
                edge.get("evidence", ""),
            )
        for event in obj.get("timeline", []):
            store.add_timeline_event(event)
