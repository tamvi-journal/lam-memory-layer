from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .text import normalize_text


APPLICATION_ID = 0x414D4333  # "AMC3"
SCHEMA_VERSION = 3
EVIDENCE_IDENTITY_VERSION = "evidence-v2"


class SchemaVersionError(RuntimeError):
    """The database is not compatible with this Memory Core runtime."""


class MigrationRequiredError(SchemaVersionError):
    """The database is a recognized legacy store and needs explicit migration."""


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


def _identity_segment(value: str, fallback: str) -> str:
    normalized = normalize_text(value).strip()
    normalized = re.sub(r"[^a-z0-9._/-]+", "-", normalized).strip("-")
    return normalized or fallback


def canonical_evidence_identity(
    evidence: dict[str, Any],
    *,
    source_sha256: str | None = None,
) -> dict[str, str]:
    source_ref = str(evidence.get("source_ref", "")).strip()
    inferred_family = source_ref.split(":", 1)[0] if ":" in source_ref else source_ref
    source_family = _identity_segment(
        str(evidence.get("source_family", inferred_family)),
        "unknown-source",
    )
    independence_group = _identity_segment(
        str(evidence.get("independence_group", source_ref or source_family)),
        source_family,
    )
    if source_sha256 is None:
        source_payload = evidence.get(
            "source_payload",
            {
                "source_ref": source_ref,
                "content_summary": evidence.get("content_summary", ""),
            },
        )
        source_sha256 = hash_payload(source_payload)
    identity_version = str(
        evidence.get("identity_version", EVIDENCE_IDENTITY_VERSION)
    )
    identity = {
        "identity_version": identity_version,
        "source_family": source_family,
        "independence_group": independence_group,
        "source_sha256": source_sha256,
    }
    return {
        **identity,
        "evidence_sha256": hash_payload(identity),
    }


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _raw_connect(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        if readonly:
            uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if readonly:
                yield conn
            else:
                with conn:
                    yield conn
        finally:
            conn.close()

    @contextmanager
    def connect(
        self,
        *,
        readonly: bool = False,
        require_schema: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        if readonly and not self.db_path.exists():
            raise FileNotFoundError(self.db_path)
        with self._raw_connect(readonly=readonly) as conn:
            if require_schema:
                self._assert_schema(conn)
            yield conn

    def schema_info(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "application_id": 0,
                "user_version": 0,
                "state": "uninitialized",
            }
        with self._raw_connect(readonly=True) as conn:
            application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if application_id == APPLICATION_ID and user_version == SCHEMA_VERSION:
            state = "ready"
        elif user_version > SCHEMA_VERSION or (
            application_id not in {0, APPLICATION_ID}
        ):
            state = "incompatible"
        elif "memory_records_v2" in tables:
            state = "legacy-v2"
        else:
            state = "unknown"
        return {
            "application_id": application_id,
            "user_version": user_version,
            "state": state,
        }

    def initialize(self, *, migrate: bool = True) -> dict[str, Any]:
        """Create v3 or explicitly migrate a recognized v2 store.

        Ordinary reads never call this method. The compatibility alias `init`
        remains for existing consumers, but it is only used on write paths.
        """

        before = self.schema_info()
        if before["state"] == "ready":
            return {**before, "changed": False}
        if before["state"] == "incompatible":
            raise SchemaVersionError(
                "database application_id/user_version is newer or foreign"
            )
        if before["state"] == "legacy-v2" and not migrate:
            raise MigrationRequiredError("legacy v2 store requires migration")

        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self._raw_connect() as conn:
            application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"database user_version {user_version} exceeds {SCHEMA_VERSION}"
                )
            if application_id not in {0, APPLICATION_ID}:
                raise SchemaVersionError(
                    f"foreign application_id {application_id}; expected {APPLICATION_ID}"
                )
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            conn.executescript(schema)
            if "memory_records_v2" in tables:
                self._migrate_v2(conn)
            conn.execute(
                "INSERT INTO memory_meta_v3(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        after = self.schema_info()
        return {
            **after,
            "changed": True,
            "migrated_from": "v2" if before["state"] == "legacy-v2" else None,
        }

    def init(self) -> None:
        self.initialize()

    def migrate_to(self, target_path: str | Path) -> "MemoryStore":
        """Copy a store and migrate only the copy, leaving the source untouched."""

        target = Path(target_path)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            migrated = MemoryStore(target)
            migrated.initialize()
            return migrated
        with self._raw_connect(readonly=True) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
        migrated = MemoryStore(target)
        migrated.initialize()
        return migrated

    def _assert_schema(self, conn: sqlite3.Connection) -> None:
        application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if application_id == APPLICATION_ID and user_version == SCHEMA_VERSION:
            return
        if user_version > SCHEMA_VERSION or application_id not in {
            0,
            APPLICATION_ID,
        }:
            raise SchemaVersionError(
                "database is newer than this runtime or belongs to another application"
            )
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "memory_records_v2" in tables:
            raise MigrationRequiredError(
                "legacy v2 store must be initialized or migrated before use"
            )
        raise SchemaVersionError("memory database is not initialized")

    def current_view(self, record_id: str | None = None) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            if record_id:
                rows = conn.execute(
                    "SELECT * FROM memory_current_v3 WHERE record_id=?",
                    (record_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_current_v3 ORDER BY domain,record_id"
                ).fetchall()
        return [dict(row) for row in rows]

    def historical_view(self, record_id: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_revision_state_v3 WHERE record_id=? "
                "ORDER BY revision_number",
                (record_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def evidence_for_revision(
        self, revision_id: str
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT e.*,l.stance,l.weight,l.reason AS link_reason "
                "FROM memory_revision_evidence_v3 l "
                "JOIN memory_evidence_v3 e ON e.evidence_id=l.evidence_id "
                "WHERE l.revision_id=? "
                "ORDER BY e.captured_at,e.evidence_id,l.stance",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cue_rows(self, profile: str, scope: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_cues_v3 WHERE profile=? "
                "AND scope IN ('global',?) ORDER BY weight DESC",
                (profile, scope),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_relation_rows(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM memory_relations_v3 WHERE status='active'"
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
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            if conn.execute(
                "SELECT 1 FROM memory_records_v3 WHERE record_id=?", (record_id,)
            ).fetchone():
                raise ValueError("record already exists")
            now = utc_now()
            conn.execute(
                "INSERT INTO memory_records_v3("
                "record_id,record_class,domain,scope,created_at,created_by"
                ") VALUES(?,?,?,?,?,?)",
                (record_id, record_class, domain, scope, now, actor),
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
                "valid_from": now,
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
            self._insert_telemetry(
                conn,
                revision_id,
                salience=salience,
                stability=stability,
                accessibility=accessibility,
                now=now,
            )
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
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=revision_id,
                state="current",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:current",
                effective_at=now,
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
        semantic_fields = {
            "title",
            "summary",
            "content",
            "impact",
            "confidence",
            "authority_status",
            "valid_from",
        }
        telemetry_fields = {"salience", "stability", "accessibility"}
        unknown = set(changes) - semantic_fields - telemetry_fields
        if unknown:
            raise ValueError(
                "unsupported revision fields: " + ", ".join(sorted(unknown))
            )
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
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
                    "valid_from",
                    "authority_status",
                )
            }
            revision.update(
                {key: value for key, value in changes.items() if key in semantic_fields}
            )
            revision.update(
                {
                    "revision_id": revision_id,
                    "record_id": record_id,
                    "parent_revision_id": current["revision_id"],
                    "revision_number": next_number,
                    "created_at": now,
                    "created_by": actor,
                    "surface": surface,
                    "model_family": model_family,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                }
            )
            revision["confidence"] = clamp(revision["confidence"])
            revision["content_sha256"] = semantic_hash(revision)
            evidence_id = self._insert_evidence(conn, evidence)
            self._insert_revision(conn, revision)
            self._insert_telemetry(
                conn,
                revision_id,
                salience=changes.get("salience", current["salience"]),
                stability=changes.get("stability", current["stability"]),
                accessibility=changes.get("accessibility", current["accessibility"]),
                now=now,
            )
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
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=current["revision_id"],
                state="superseded",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:superseded",
                effective_at=now,
            )
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=revision_id,
                state="current",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:current",
                effective_at=now,
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
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, idempotency_key)
            if prior:
                return self._operation_result(conn, prior)
            current = conn.execute(
                "SELECT * FROM memory_current_v3 WHERE record_id=?", (record_id,)
            ).fetchone()
            if not current:
                raise ValueError("current record not found")
            evidence_id = self._insert_evidence(conn, evidence)
            now = utc_now()
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
            self._link_evidence(
                conn, current["revision_id"], evidence_id, "contradicts", reason
            )
            self._append_lifecycle(
                conn,
                record_id=record_id,
                revision_id=current["revision_id"],
                state="invalidated",
                actor=actor,
                surface=surface,
                reason=reason,
                operation_id=operation["operation_id"],
                idempotency_key=f"{idempotency_key}:lifecycle:invalidated",
                effective_at=now,
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
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_cues_v3("
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
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO memory_relations_v3("
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
        self.initialize()
        with self.connect() as conn:
            owner = conn.execute(
                "SELECT record_id FROM memory_revisions_v3 WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            if not owner or owner["record_id"] != record_id:
                raise ValueError("revision does not belong to record")
            now = utc_now()
            conn.execute(
                "INSERT INTO memory_access_v3("
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
                    now,
                ),
            )
            conn.execute(
                "UPDATE memory_telemetry_v3 "
                "SET accessibility=MIN(1.0,accessibility+0.01),"
                "access_count=access_count+1,last_accessed_at=?,updated_at=? "
                "WHERE revision_id=?",
                (now, now, revision_id),
            )

    def apply_maintenance(
        self,
        *,
        run_id: str,
        adjustments: list[dict[str, Any]],
        actor: str,
        reason: str,
        surface: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not run_id.strip():
            raise ValueError("maintenance run_id is required")
        if not reason.strip():
            raise ValueError("maintenance reason is required")
        allowed_fields = {"salience", "stability", "accessibility"}
        operation_key = idempotency_key or f"maintenance:{run_id}"
        self.initialize()
        with self.connect() as conn:
            prior = self._operation_by_key(conn, operation_key)
            if prior:
                return self._operation_result(conn, prior)
            applied: list[dict[str, Any]] = []
            semantic_hashes: dict[str, str] = {}
            for adjustment in adjustments:
                record_id = str(adjustment.get("record_id", "")).strip()
                field = str(adjustment.get("field", "")).strip()
                if not record_id:
                    raise ValueError("maintenance record_id is required")
                if field not in allowed_fields:
                    raise ValueError(
                        f"unsupported maintenance field: {field or '<empty>'}"
                    )
                current = conn.execute(
                    "SELECT * FROM memory_current_v3 WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                if not current:
                    raise ValueError(
                        f"maintenance current record not found: {record_id}"
                    )
                old_value = float(current[field])
                if adjustment.get("old_value") is not None:
                    expected = float(adjustment["old_value"])
                    if abs(expected - old_value) > 1e-6:
                        raise ValueError(
                            f"maintenance old_value mismatch for {record_id}.{field}"
                        )
                new_value = clamp(float(adjustment["new_value"]))
                semantic_hashes[current["revision_id"]] = current["content_sha256"]
                if abs(new_value - old_value) <= 1e-9:
                    continue
                conn.execute(
                    f"UPDATE memory_telemetry_v3 SET {field}=?,updated_at=? "
                    "WHERE revision_id=?",
                    (new_value, utc_now(), current["revision_id"]),
                )
                applied.append(
                    {
                        "record_id": record_id,
                        "revision_id": current["revision_id"],
                        "field": field,
                        "old_value": old_value,
                        "new_value": new_value,
                    }
                )
            for revision_id, expected_hash in semantic_hashes.items():
                row = conn.execute(
                    "SELECT content_sha256 FROM memory_revisions_v3 "
                    "WHERE revision_id=?",
                    (revision_id,),
                ).fetchone()
                if not row or row["content_sha256"] != expected_hash:
                    raise RuntimeError("maintenance changed semantic content hash")
            operation = self._insert_operation(
                conn,
                operation_type="maintenance",
                actor=actor,
                surface=surface,
                record_id=applied[0]["record_id"] if len(applied) == 1 else None,
                revision_id=(
                    applied[0]["revision_id"] if len(applied) == 1 else None
                ),
                evidence_ids=[],
                decision="materialized" if applied else "no_op",
                reason=reason,
                details={
                    "run_id": run_id,
                    "adjustments": applied,
                    "semantic_content_changed": False,
                },
                idempotency_key=operation_key,
            )
            return self._operation_result(conn, operation)

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
            "valid_from",
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
            "INSERT INTO memory_revisions_v3("
            + ",".join(fields)
            + ") VALUES("
            + ",".join(f":{field}" for field in fields)
            + ")",
            revision,
        )

    @staticmethod
    def _insert_telemetry(
        conn: sqlite3.Connection,
        revision_id: str,
        *,
        salience: float,
        stability: float,
        accessibility: float,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_telemetry_v3("
            "revision_id,salience,stability,accessibility,access_count,"
            "last_accessed_at,updated_at"
            ") VALUES(?,?,?,?,0,NULL,?)",
            (
                revision_id,
                clamp(salience),
                clamp(stability),
                clamp(accessibility),
                now,
            ),
        )

    @staticmethod
    def _insert_evidence(
        conn: sqlite3.Connection, evidence: dict[str, Any]
    ) -> str:
        identity = canonical_evidence_identity(evidence)
        evidence_id = (
            f"evidence:{identity['identity_version']}:"
            f"{identity['evidence_sha256'][:32]}"
        )
        conn.execute(
            "INSERT INTO memory_evidence_v3("
            "evidence_id,identity_version,evidence_type,source_ref,"
            "source_family,independence_group,source_sha256,evidence_sha256,"
            "captured_at,actor,surface,model_family,content_summary,confidence,"
            "privacy_class"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
            (
                evidence_id,
                identity["identity_version"],
                evidence.get("evidence_type", "observation"),
                evidence.get("source_ref", ""),
                identity["source_family"],
                identity["independence_group"],
                identity["source_sha256"],
                identity["evidence_sha256"],
                evidence.get("captured_at") or utc_now(),
                evidence.get("actor", ""),
                evidence.get("surface", ""),
                evidence.get("model_family", ""),
                evidence.get("content_summary", ""),
                clamp(evidence.get("confidence", 0.7)),
                evidence.get("privacy_class", "private"),
            ),
        )
        row = conn.execute(
            "SELECT evidence_id FROM memory_evidence_v3 "
            "WHERE identity_version=? AND evidence_sha256=?",
            (identity["identity_version"], identity["evidence_sha256"]),
        ).fetchone()
        if not row:
            raise RuntimeError("canonical evidence insert failed")
        return str(row["evidence_id"])

    @staticmethod
    def _link_evidence(
        conn: sqlite3.Connection,
        revision_id: str,
        evidence_id: str,
        stance: str,
        reason: str,
    ) -> None:
        conn.execute(
            "INSERT INTO memory_revision_evidence_v3("
            "revision_id,evidence_id,stance,weight,reason"
            ") VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
            (revision_id, evidence_id, stance, 1.0, reason),
        )

    @staticmethod
    def _operation_by_key(
        conn: sqlite3.Connection, idempotency_key: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM memory_operations_v3 WHERE idempotency_key=?",
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
            "INSERT INTO memory_operations_v3("
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
            "SELECT * FROM memory_operations_v3 WHERE operation_id=?",
            (operation_id,),
        ).fetchone()

    @staticmethod
    def _append_lifecycle(
        conn: sqlite3.Connection,
        *,
        record_id: str,
        revision_id: str,
        state: str,
        actor: str,
        surface: str,
        reason: str,
        operation_id: str | None,
        idempotency_key: str,
        effective_at: str,
    ) -> None:
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_number),0)+1 "
                "FROM memory_lifecycle_events_v3 WHERE record_id=?",
                (record_id,),
            ).fetchone()[0]
        )
        event_id = "lifecycle:" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        conn.execute(
            "INSERT INTO memory_lifecycle_events_v3("
            "lifecycle_event_id,record_id,revision_id,sequence_number,"
            "lifecycle_state,effective_at,actor,surface,reason,operation_id,"
            "idempotency_key,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                record_id,
                revision_id,
                sequence,
                state,
                effective_at,
                actor,
                surface,
                reason,
                operation_id,
                idempotency_key,
                utc_now(),
            ),
        )

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
                "SELECT * FROM memory_revision_state_v3 WHERE revision_id=?",
                (result["target_revision_id"],),
            ).fetchone()
            revision = dict(row) if row else None
        result["revision"] = revision
        return result

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        migrated = conn.execute(
            "SELECT value FROM memory_meta_v3 WHERE key='migrated_from_v2'"
        ).fetchone()
        if migrated:
            return

        records = conn.execute(
            "SELECT * FROM memory_records_v2 ORDER BY record_id"
        ).fetchall()
        for row in records:
            conn.execute(
                "INSERT OR IGNORE INTO memory_records_v3("
                "record_id,record_class,domain,scope,created_at,created_by"
                ") VALUES(?,?,?,?,?,?)",
                (
                    row["record_id"],
                    row["record_class"],
                    row["domain"],
                    row["scope"],
                    row["created_at"],
                    row["created_by"],
                ),
            )

        revisions = conn.execute(
            "SELECT * FROM memory_revisions_v2 "
            "ORDER BY record_id,revision_number"
        ).fetchall()
        for row in revisions:
            revision = dict(row)
            semantic = {
                key: revision[key]
                for key in (
                    "revision_id",
                    "record_id",
                    "parent_revision_id",
                    "revision_number",
                    "title",
                    "summary",
                    "content",
                    "impact",
                    "confidence",
                    "valid_from",
                    "authority_status",
                    "content_sha256",
                    "created_at",
                    "created_by",
                    "surface",
                    "model_family",
                    "reason",
                    "idempotency_key",
                )
            }
            self._insert_revision(conn, semantic)
            conn.execute(
                "INSERT INTO memory_telemetry_v3("
                "revision_id,salience,stability,accessibility,access_count,"
                "last_accessed_at,updated_at"
                ") VALUES(?,?,?,?,0,NULL,?)",
                (
                    revision["revision_id"],
                    revision["salience"],
                    revision["stability"],
                    revision["accessibility"],
                    revision["created_at"],
                ),
            )

        evidence_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT * FROM memory_evidence_v2 ORDER BY evidence_id"
        ):
            legacy = dict(row)
            identity = canonical_evidence_identity(
                {
                    **legacy,
                    "identity_version": EVIDENCE_IDENTITY_VERSION,
                    "source_family": (
                        legacy["source_ref"].split(":", 1)[0]
                        if legacy["source_ref"]
                        else "legacy"
                    ),
                    "independence_group": legacy["source_ref"] or "legacy",
                },
                source_sha256=legacy["source_sha256"],
            )
            evidence_id = (
                f"evidence:{identity['identity_version']}:"
                f"{identity['evidence_sha256'][:32]}"
            )
            conn.execute(
                "INSERT INTO memory_evidence_v3("
                "evidence_id,identity_version,evidence_type,source_ref,"
                "source_family,independence_group,source_sha256,evidence_sha256,"
                "captured_at,actor,surface,model_family,content_summary,confidence,"
                "privacy_class"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(identity_version,evidence_sha256) DO NOTHING",
                (
                    evidence_id,
                    identity["identity_version"],
                    legacy["evidence_type"],
                    legacy["source_ref"],
                    identity["source_family"],
                    identity["independence_group"],
                    identity["source_sha256"],
                    identity["evidence_sha256"],
                    legacy["captured_at"],
                    legacy["actor"],
                    legacy["surface"],
                    legacy["model_family"],
                    legacy["content_summary"],
                    legacy["confidence"],
                    legacy["privacy_class"],
                ),
            )
            canonical = conn.execute(
                "SELECT evidence_id FROM memory_evidence_v3 "
                "WHERE identity_version=? AND evidence_sha256=?",
                (identity["identity_version"], identity["evidence_sha256"]),
            ).fetchone()
            evidence_map[legacy["evidence_id"]] = str(canonical["evidence_id"])

        if self._table_exists(conn, "memory_revision_evidence_v2"):
            for row in conn.execute(
                "SELECT * FROM memory_revision_evidence_v2 "
                "ORDER BY revision_id,evidence_id,stance"
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO memory_revision_evidence_v3("
                    "revision_id,evidence_id,stance,weight,reason"
                    ") VALUES(?,?,?,?,?)",
                    (
                        row["revision_id"],
                        evidence_map[row["evidence_id"]],
                        row["stance"],
                        row["weight"],
                        row["reason"],
                    ),
                )

        for table, fields in (
            (
                "memory_relations",
                (
                    "relation_id",
                    "from_record_id",
                    "to_record_id",
                    "relation_type",
                    "weight",
                    "source_revision_id",
                    "status",
                    "created_at",
                ),
            ),
            (
                "memory_cues",
                (
                    "cue",
                    "cue_norm",
                    "cue_type",
                    "target_record_id",
                    "weight",
                    "scope",
                    "profile",
                ),
            ),
            (
                "memory_access",
                (
                    "cue_sha256",
                    "record_id",
                    "revision_id",
                    "retrieval_reason",
                    "rank",
                    "surface",
                    "created_at",
                ),
            ),
        ):
            old_name = f"{table}_v2"
            new_name = f"{table}_v3"
            if not self._table_exists(conn, old_name):
                continue
            columns = ",".join(fields)
            conn.execute(
                f"INSERT OR IGNORE INTO {new_name}({columns}) "
                f"SELECT {columns} FROM {old_name}"
            )

        if self._table_exists(conn, "memory_operations_v2"):
            for row in conn.execute(
                "SELECT * FROM memory_operations_v2 "
                "ORDER BY created_at,operation_id"
            ):
                evidence_ids = [
                    evidence_map.get(item, item)
                    for item in json.loads(row["evidence_ids_json"] or "[]")
                ]
                conn.execute(
                    "INSERT OR IGNORE INTO memory_operations_v3("
                    "operation_id,operation_type,actor,surface,target_record_id,"
                    "target_revision_id,evidence_ids_json,decision,reason,"
                    "details_json,idempotency_key,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["operation_id"],
                        row["operation_type"],
                        row["actor"],
                        row["surface"],
                        row["target_record_id"],
                        row["target_revision_id"],
                        json.dumps(evidence_ids, ensure_ascii=False),
                        row["decision"],
                        row["reason"],
                        row["details_json"],
                        row["idempotency_key"],
                        row["created_at"],
                    ),
                )

        if self._table_exists(conn, "memory_intake_v2"):
            for row in conn.execute(
                "SELECT * FROM memory_intake_v2 ORDER BY created_at,intake_id"
            ):
                evidence_ids = [
                    evidence_map.get(item, item)
                    for item in json.loads(row["evidence_ids_json"] or "[]")
                ]
                conn.execute(
                    "INSERT OR IGNORE INTO memory_intake_v3("
                    "intake_id,operation_type,target_record_id,proposal_sha256,"
                    "evidence_ids_json,status,decision_reason,operation_id,actor,"
                    "surface,idempotency_key,created_at,decided_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["intake_id"],
                        row["operation_type"],
                        row["target_record_id"],
                        row["proposal_sha256"],
                        json.dumps(evidence_ids, ensure_ascii=False),
                        row["status"],
                        row["decision_reason"],
                        row["operation_id"],
                        row["actor"],
                        row["surface"],
                        row["idempotency_key"],
                        row["created_at"],
                        row["decided_at"],
                    ),
                )

        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in revisions:
            grouped.setdefault(row["record_id"], []).append(row)
        for record_id, history in grouped.items():
            for revision in history:
                base_key = f"migration:v2:{revision['revision_id']}"
                self._append_lifecycle(
                    conn,
                    record_id=record_id,
                    revision_id=revision["revision_id"],
                    state="current",
                    actor="memory-core-migrator",
                    surface="migration",
                    reason="preserve v2 semantic chronology",
                    operation_id=None,
                    idempotency_key=f"{base_key}:current",
                    effective_at=revision["valid_from"] or revision["created_at"],
                )
                state = revision["revision_status"]
                if state != "current":
                    self._append_lifecycle(
                        conn,
                        record_id=record_id,
                        revision_id=revision["revision_id"],
                        state=state,
                        actor="memory-core-migrator",
                        surface="migration",
                        reason="preserve v2 terminal lifecycle state",
                        operation_id=None,
                        idempotency_key=f"{base_key}:{state}",
                        effective_at=revision["valid_to"] or revision["created_at"],
                    )

        conn.execute(
            "INSERT INTO memory_meta_v3(key,value) VALUES('migrated_from_v2',?)",
            (utc_now(),),
        )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
        )
