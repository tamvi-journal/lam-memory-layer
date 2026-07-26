PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS memory_nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 50,
    confidence REAL NOT NULL DEFAULT 0.7,
    salience REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.5,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    retrieval_count INTEGER NOT NULL DEFAULT 0,
    helpful_count INTEGER NOT NULL DEFAULT 0,
    harmful_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TEXT,
    scope TEXT NOT NULL DEFAULT 'global',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    occurred_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    embedding_json TEXT NOT NULL DEFAULT '[]',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_nodes(status);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_nodes(scope);
CREATE INDEX IF NOT EXISTS idx_memory_priority ON memory_nodes(priority DESC);
CREATE INDEX IF NOT EXISTS idx_memory_occurred ON memory_nodes(occurred_at DESC);

CREATE TABLE IF NOT EXISTS memory_edges (
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (src_id, dst_id, relation),
    FOREIGN KEY (src_id) REFERENCES memory_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (dst_id) REFERENCES memory_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON memory_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON memory_edges(dst_id);

CREATE TABLE IF NOT EXISTS memory_cues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cue TEXT NOT NULL,
    cue_norm TEXT NOT NULL,
    cue_type TEXT NOT NULL DEFAULT 'phrase',
    target_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    scope TEXT NOT NULL DEFAULT 'global',
    UNIQUE(cue_norm, target_id),
    FOREIGN KEY (target_id) REFERENCES memory_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cues_norm ON memory_cues(cue_norm);
CREATE INDEX IF NOT EXISTS idx_cues_target ON memory_cues(target_id);

CREATE TABLE IF NOT EXISTS timeline_events (
    id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 50,
    tags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_events(occurred_at DESC);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    result_ids_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 50,
    confidence REAL NOT NULL DEFAULT 0.6,
    salience REAL NOT NULL DEFAULT 0.5,
    stability REAL NOT NULL DEFAULT 0.3,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL DEFAULT 'global',
    source_type TEXT NOT NULL DEFAULT 'codex-turn',
    source_ref TEXT NOT NULL DEFAULT '',
    occurred_at TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    relation_targets_json TEXT NOT NULL DEFAULT '[]',
    sensitivity TEXT NOT NULL DEFAULT 'ordinary',
    importance REAL NOT NULL DEFAULT 0.5,
    capture_reasons_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_candidates_status ON memory_candidates(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_scope ON memory_candidates(scope, created_at DESC);

CREATE TABLE IF NOT EXISTS candidate_attestations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    reviewer_branch TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, reviewer_branch),
    FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_attestations_candidate
ON candidate_attestations(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attestations_branch
ON candidate_attestations(reviewer_branch, created_at DESC);

CREATE TABLE IF NOT EXISTS field_state_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'retrieval',
    cue TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'global',
    coherence REAL NOT NULL DEFAULT 0.0,
    uncertainty REAL NOT NULL DEFAULT 1.0,
    drift_risk REAL NOT NULL DEFAULT 0.0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    selected_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_field_state_created ON field_state_log(created_at DESC);

CREATE TABLE IF NOT EXISTS dream_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    trigger TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_dream_runs_finished ON dream_runs(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_dream_runs_tenant ON dream_runs(tenant_id, finished_at DESC);

CREATE TABLE IF NOT EXISTS memory_mutations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES dream_runs(id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES memory_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mutations_run ON memory_mutations(run_id);
CREATE INDEX IF NOT EXISTS idx_mutations_node ON memory_mutations(node_id, created_at DESC);

CREATE TABLE IF NOT EXISTS intake_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_branch TEXT NOT NULL,
    event_id TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(source_branch, event_id),
    FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_intake_received ON intake_events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_candidate ON intake_events(candidate_id);

CREATE TABLE IF NOT EXISTS sync_messages (
    message_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    candidate_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(source_branch, target_branch, sequence)
);

CREATE INDEX IF NOT EXISTS idx_sync_direction_status
ON sync_messages(direction, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sync_source_sequence
ON sync_messages(source_branch, target_branch, sequence DESC);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
