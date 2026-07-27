from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .text import normalize_text


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_payload(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def semantic_hash(value: dict[str, Any]) -> str:
    return hash_payload(
        {
            "title": value.get("title", ""),
            "summary": value.get("summary", ""),
            "content": value.get("content", ""),
            "impact": value.get("impact", ""),
            "confidence": float(value.get("confidence", 0.7)),
            "authority_status": value.get(
                "authority_status", "canonical_reference"
            ),
        }
    )


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


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
            conn.execute("DROP VIEW IF EXISTS memory_current_v2")
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO memory_meta_v2(key,value) VALUES('schema_version','2') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    def current_view(self, record_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
            if record_id:
                rows = conn.execute(
                    "SELECT * FROM memory_current_v2 WHERE record_id=?",
                    (record_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_current_v2 ORDER BY domain,record_id"
                ).fetchall()
        return [dict(row) for row in rows]

    def historical_view(self, record_id: str) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.record_class,r.domain,r.scope,r.record_status,v.* "
                "FROM memory_records_v2 r JOIN memory_revisions_v2 v "
                "ON v.record_id=r.record_id WHERE r.record_id=? "
                "ORDER BY v.revision_number",
                (record_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_current(
        self,
        *,
        record_id: str,
        record_class: str,
        domain: str,
        title: str,
        summary: str = "",
        content: str = "",
        impact: str = "",
        confidence: float = 0.7,
        salience: float = 0.5,
        stability: float = 0.5,
        accessibility: float = 0.5,
        authority_status: str = "canonical_reference",
        scope: str = "global",
        actor: str,
        surface: str = "",
        model_family: str = "",
        reason: str,
        evidence: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.init()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            if conn.execute(
                "SELECT 1 FROM memory_records_v2 WHERE record_id=?", (record_id,)
            ).fetchone():
                raise ValueError("record already exists")
            now = utc_now()
            conn.execute(
                "INSERT INTO memory_records_v2("
                "record_id,record_class,domain,scope,record_status,created_at,created_by"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    record_id,
                    record_class,
                    domain,
                    scope,
                    "active",
                    now,
                    actor,
                ),
            )
            evidence_id = self._insert_evidence(conn, evidence)
            revision_id = self._revision_id(record_id, 1, idempotency_key)
            revision = {
                "revision_id": revision_id,
                "record_id": record_id,
                "parent_revision_id": None,
                "revision_number": 1,
                "title": title,
                "summary": summary,
                "content": content,
                "impact": impact,
                "confidence": clamp(confidence),
                "salience": clamp(salience),
                "stability": clamp(stability),
                "accessibility": clamp(accessibility),
                "valid_from": now,
                "valid_to": None,
                "revision_status": "current",
                "authority_status": authority_status,
                "created_at": now,
                "created_by": actor,
                "surface": surface,
                "model_family": model_family,
                "reason": reason,
                "idempotency_key": idempotency_key,
            }
            revision["content_sha256"] = semantic_hash(revision)
            self._insert_revision(conn, revision)
            self._link_evidence(conn, revision_id, evidence_id, "supports", reason)
            operation = self._insert_operation(
                conn,
                operation_type="create",
                actor=actor,
                surface=surface,
                record_id=record_id,
                revision_id=revision_id,
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={"record_class": record_class, "domain": domain},
                idempotency_key=idempotency_key,
            )
            return self._operation_result(conn, operation)

    def revise(
        self,
        record_id: str,
        *,
        operation_type: str,
        actor: str,
        reason: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        changes: dict[str, Any],
        surface: str = "",
        model_family: str = "",
    ) -> dict[str, Any]:
        if operation_type not in {"correct", "refine", "supersede"}:
            raise ValueError("unsupported semantic operation")
        allowed = {
            "title",
            "summary",
            "content",
            "impact",
            "confidence",
            "salience",
            "stability",
            "accessibility",
            "authority_status",
            "valid_from",
            "valid_to",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported revision fields: " + ", ".join(sorted(unknown)))
        self.init()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            current = conn.execute(
                "SELECT * FROM memory_current_v2 WHERE record_id=?", (record_id,)
            ).fetchone()
            if not current:
                raise ValueError("current record not found")
            now = utc_now()
            next_number = int(current["revision_number"]) + 1
            revision_id = self._revision_id(
                record_id, next_number, idempotency_key
            )
            revision = {
                key: current[key]
                for key in (
                    "title",
                    "summary",
                    "content",
                    "impact",
                    "confidence",
                    "salience",
                    "stability",
                    "accessibility",
                    "valid_from",
                    "valid_to",
                    "authority_status",
                )
            }
            revision.update(changes)
            revision.update(
                {
                    "revision_id": revision_id,
                    "record_id": record_id,
                    "parent_revision_id": current["revision_id"],
                    "revision_number": next_number,
                    "revision_status": "current",
                    "created_at": now,
                    "created_by": actor,
                    "surface": surface,
                    "model_family": model_family,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                }
            )
            for field in ("confidence", "salience", "stability", "accessibility"):
                revision[field] = clamp(revision[field])
            revision["content_sha256"] = semantic_hash(revision)
            evidence_id = self._insert_evidence(conn, evidence)
            conn.execute(
                "UPDATE memory_revisions_v2 SET revision_status='superseded',"
                "valid_to=? WHERE revision_id=?",
                (now, current["revision_id"]),
            )
            self._insert_revision(conn, revision)
            self._link_evidence(conn, revision_id, evidence_id, "supports", reason)
            operation = self._insert_operation(
                conn,
                operation_type=operation_type,
                actor=actor,
                surface=surface,
                record_id=record_id,
                revision_id=revision_id,
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={"parent_revision_id": current["revision_id"]},
                idempotency_key=idempotency_key,
            )
            return self._operation_result(conn, operation)

    def invalidate(
        self,
        record_id: str,
        *,
        actor: str,
        reason: str,
        evidence: dict[str, Any],
        idempotency_key: str,
        surface: str = "",
    ) -> dict[str, Any]:
        self.init()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            current = conn.execute(
                "SELECT * FROM memory_current_v2 WHERE record_id=?", (record_id,)
            ).fetchone()
            if not current:
                raise ValueError("current record not found")
            evidence_id = self._insert_evidence(conn, evidence)
            now = utc_now()
            conn.execute(
                "UPDATE memory_revisions_v2 SET revision_status='invalidated',"
                "valid_to=? WHERE revision_id=?",
                (now, current["revision_id"]),
            )
            conn.execute(
                "UPDATE memory_records_v2 SET record_status='invalidated' "
                "WHERE record_id=?",
                (record_id,),
            )
            operation = self._insert_operation(
                conn,
                operation_type="invalidate",
                actor=actor,
                surface=surface,
                record_id=record_id,
                revision_id=current["revision_id"],
                evidence_ids=[evidence_id],
                decision="materialized",
                reason=reason,
                details={},
                idempotency_key=idempotency_key,
            )
            return self._operation_result(conn, operation)

    def add_cue(
        self,
        *,
        profile: str,
        cue: str,
        target_record_id: str,
        weight: float = 1.0,
        cue_type: str = "phrase",
        scope: str = "global",
    ) -> None:
        self.init()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_cues_v2("
                "cue,cue_norm,cue_type,target_record_id,weight,scope,profile"
                ") VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(profile,cue_norm,target_record_id) DO UPDATE SET "
                "cue=excluded.cue,cue_type=excluded.cue_type,"
                "weight=excluded.weight,scope=excluded.scope",
                (
                    cue,
                    normalize_text(cue),
                    cue_type,
                    target_record_id,
                    float(weight),
                    scope,
                    profile,
                ),
            )

    def add_relation(
        self,
        *,
        relation_id: str,
        from_record_id: str,
        to_record_id: str,
        relation_type: str,
        weight: float = 1.0,
        source_revision_id: str | None = None,
    ) -> None:
        self.init()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_relations_v2("
                "relation_id,from_record_id,to_record_id,relation_type,"
                "weight,source_revision_id,status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(from_record_id,to_record_id,relation_type) "
                "DO UPDATE SET weight=excluded.weight,"
                "source_revision_id=excluded.source_revision_id,status='active'",
                (
                    relation_id,
                    from_record_id,
                    to_record_id,
                    relation_type,
                    float(weight),
                    source_revision_id,
                    "active",
                    utc_now(),
                ),
            )

    def record_access(
        self,
        *,
        cue: str,
        record_id: str,
        revision_id: str,
        retrieval_reason: str,
        rank: int,
        surface: str,
    ) -> None:
        with self.connect() as conn:
            owner = conn.execute(
                "SELECT record_id FROM memory_revisions_v2 WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if not owner or owner["record_id"] != record_id:
                raise ValueError("revision does not belong to record")
            conn.execute(
                "INSERT INTO memory_access_v2("
                "cue_sha256,record_id,revision_id,retrieval_reason,rank,"
                "surface,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(cue.encode("utf-8")).hexdigest(),
                    record_id,
                    revision_id,
                    retrieval_reason,
                    int(rank),
                    surface,
                    utc_now(),
                ),
            )
            conn.execute(
                "UPDATE memory_revisions_v2 "
                "SET accessibility=MIN(1.0,accessibility+0.01) "
                "WHERE revision_id=?",
                (revision_id,),
            )

    @staticmethod
    def _revision_id(
        record_id: str, revision_number: int, idempotency_key: str
    ) -> str:
        suffix = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]
        return f"{record_id}@r{revision_number}-{suffix}"

    @staticmethod
    def _insert_revision(
        conn: sqlite3.Connection, revision: dict[str, Any]
    ) -> None:
        fields = (
            "revision_id",
            "record_id",
            "parent_revision_id",
            "revision_number",
            "title",
            "summary",
            "content",
            "impact",
            "confidence",
            "salience",
            "stability",
            "accessibility",
            "valid_from",
            "valid_to",
            "revision_status",
            "authority_status",
            "content_sha256",
            "created_at",
            "created_by",
            "surface",
            "model_family",
            "reason",
            "idempotency_key",
        )
        conn.execute(
            "INSERT INTO memory_revisions_v2("
            + ",".join(fields)
            + ") VALUES("
            + ",".join(f":{field}" for field in fields)
            + ")",
            revision,
        )

    @staticmethod
    def _insert_evidence(
        conn: sqlite3.Connection, evidence: dict[str, Any]
    ) -> str:
        source_payload = evidence.get(
            "source_payload",
            {
                "source_ref": evidence.get("source_ref", ""),
                "content_summary": evidence.get("content_summary", ""),
            },
        )
        source_sha256 = hash_payload(source_payload)
        evidence_id = evidence.get("evidence_id") or (
            "evidence:" + source_sha256[:32]
        )
        conn.execute(
            "INSERT INTO memory_evidence_v2("
            "evidence_id,evidence_type,source_ref,source_sha256,captured_at,"
            "actor,surface,model_family,content_summary,confidence,privacy_class"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(evidence_id) DO NOTHING",
            (
                evidence_id,
                evidence.get("evidence_type", "observation"),
                evidence.get("source_ref", ""),
                source_sha256,
                evidence.get("captured_at") or utc_now(),
                evidence.get("actor", ""),
                evidence.get("surface", ""),
                evidence.get("model_family", ""),
                evidence.get("content_summary", ""),
                clamp(evidence.get("confidence", 0.7)),
                evidence.get("privacy_class", "private"),
            ),
        )
        return evidence_id

    @staticmethod
    def _link_evidence(
        conn: sqlite3.Connection,
        revision_id: str,
        evidence_id: str,
        stance: str,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_revision_evidence_v2("
            "revision_id,evidence_id,stance,weight,reason"
            ") VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
            (revision_id, evidence_id, stance, 1.0, reason),
        )

    @staticmethod
    def _operation_by_key(
        conn: sqlite3.Connection, idempotency_key: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM memory_operations_v2 WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()

    @staticmethod
    def _insert_operation(
        conn: sqlite3.Connection,
        *,
        operation_type: str,
        actor: str,
        surface: str,
        record_id: str | None,
        revision_id: str | None,
        evidence_ids: list[str],
        decision: str,
        reason: str,
        details: dict[str, Any],
        idempotency_key: str,
    ) -> sqlite3.Row:
        operation_id = "operation:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            "INSERT INTO memory_operations_v2("
            "operation_id,operation_type,actor,surface,target_record_id,"
            "target_revision_id,evidence_ids_json,decision,reason,"
            "details_json,idempotency_key,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                operation_type,
                actor,
                surface,
                record_id,
                revision_id,
                json.dumps(evidence_ids, ensure_ascii=False),
                decision,
                reason,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                idempotency_key,
                utc_now(),
            ),
        )
        return conn.execute(
            "SELECT * FROM memory_operations_v2 WHERE operation_id=?",
            (operation_id,),
        ).fetchone()

    @staticmethod
    def _operation_result(
        conn: sqlite3.Connection, operation: sqlite3.Row
    ) -> dict[str, Any]:
        result = dict(operation)
        result["evidence_ids"] = json.loads(
            result.pop("evidence_ids_json") or "[]"
        )
        result["details"] = json.loads(result.pop("details_json") or "{}")
        revision = None
        if result["target_revision_id"]:
            row = conn.execute(
                "SELECT * FROM memory_revisions_v2 WHERE revision_id=?",
                (result["target_revision_id"],),
            ).fetchone()
            revision = dict(row) if row else None
        result["revision"] = revision
        return result
