PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS memory_records_v2 (
    record_id TEXT PRIMARY KEY,
    record_class TEXT NOT NULL
        CHECK(record_class IN ('event', 'belief', 'axis')),
    domain TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    record_status TEXT NOT NULL DEFAULT 'active'
        CHECK(record_status IN ('active', 'archived', 'invalidated')),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_revisions_v2 (
    revision_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    parent_revision_id TEXT,
    revision_number INTEGER NOT NULL CHECK(revision_number > 0),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.7 CHECK(confidence BETWEEN 0.0 AND 1.0),
    salience REAL NOT NULL DEFAULT 0.5 CHECK(salience BETWEEN 0.0 AND 1.0),
    stability REAL NOT NULL DEFAULT 0.5 CHECK(stability BETWEEN 0.0 AND 1.0),
    accessibility REAL NOT NULL DEFAULT 0.5 CHECK(accessibility BETWEEN 0.0 AND 1.0),
    valid_from TEXT,
    valid_to TEXT,
    revision_status TEXT NOT NULL
        CHECK(revision_status IN (
            'current', 'superseded', 'deprecated', 'invalidated'
        )),
    authority_status TEXT NOT NULL
        CHECK(authority_status IN (
            'non_authoritative', 'canonical_reference', 'protected'
        )),
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    model_family TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT UNIQUE,
    FOREIGN KEY(record_id) REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(parent_revision_id)
        REFERENCES memory_revisions_v2(revision_id) ON DELETE RESTRICT,
    UNIQUE(record_id, revision_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_core_one_current
ON memory_revisions_v2(record_id)
WHERE revision_status='current';

CREATE INDEX IF NOT EXISTS idx_memory_core_history
ON memory_revisions_v2(record_id, revision_number DESC);

CREATE TABLE IF NOT EXISTS memory_evidence_v2 (
    evidence_id TEXT PRIMARY KEY,
    evidence_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    surface TEXT NOT NULL DEFAULT '',
    model_family TEXT NOT NULL DEFAULT '',
    content_summary TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0.0 AND 1.0),
    privacy_class TEXT NOT NULL DEFAULT 'private'
);

CREATE TABLE IF NOT EXISTS memory_revision_evidence_v2 (
    revision_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    stance TEXT NOT NULL
        CHECK(stance IN ('supports', 'contradicts', 'qualifies', 'contextualizes')),
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight BETWEEN 0.0 AND 1.0),
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(revision_id, evidence_id, stance),
    FOREIGN KEY(revision_id)
        REFERENCES memory_revisions_v2(revision_id) ON DELETE RESTRICT,
    FOREIGN KEY(evidence_id)
        REFERENCES memory_evidence_v2(evidence_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_relations_v2 (
    relation_id TEXT PRIMARY KEY,
    from_record_id TEXT NOT NULL,
    to_record_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    source_revision_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    FOREIGN KEY(from_record_id)
        REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(to_record_id)
        REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(source_revision_id)
        REFERENCES memory_revisions_v2(revision_id) ON DELETE RESTRICT,
    UNIQUE(from_record_id, to_record_id, relation_type)
);

CREATE TABLE IF NOT EXISTS memory_cues_v2 (
    cue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cue TEXT NOT NULL,
    cue_norm TEXT NOT NULL,
    cue_type TEXT NOT NULL DEFAULT 'phrase',
    target_record_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    scope TEXT NOT NULL DEFAULT 'global',
    profile TEXT NOT NULL,
    FOREIGN KEY(target_record_id)
        REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    UNIQUE(profile, cue_norm, target_record_id)
);

CREATE TABLE IF NOT EXISTS memory_operations_v2 (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    target_record_id TEXT,
    target_revision_id TEXT,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(target_record_id)
        REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(target_revision_id)
        REFERENCES memory_revisions_v2(revision_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_access_v2 (
    access_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cue_sha256 TEXT NOT NULL,
    record_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    retrieval_reason TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL DEFAULT 0,
    surface TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(record_id)
        REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(revision_id)
        REFERENCES memory_revisions_v2(revision_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_intake_v2 (
    intake_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL,
    target_record_id TEXT,
    proposal_sha256 TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL
        CHECK(status IN ('received', 'held', 'materialized', 'rejected', 'no_op')),
    decision_reason TEXT NOT NULL DEFAULT '',
    operation_id TEXT,
    actor TEXT NOT NULL,
    surface TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY(target_record_id)
        REFERENCES memory_records_v2(record_id) ON DELETE RESTRICT,
    FOREIGN KEY(operation_id)
        REFERENCES memory_operations_v2(operation_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_meta_v2 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS memory_current_v2 AS
SELECT
    r.record_id,
    r.record_class,
    r.domain,
    r.scope,
    r.record_status,
    v.*
FROM memory_records_v2 r
JOIN memory_revisions_v2 v ON v.record_id=r.record_id
WHERE r.record_status='active' AND v.revision_status='current';
