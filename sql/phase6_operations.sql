PRAGMA foreign_keys = ON;

CREATE TABLE operation_metadata (
    metadata_key TEXT PRIMARY KEY,
    metadata_value TEXT NOT NULL
);

CREATE TABLE orchestration_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    as_of TEXT NOT NULL,
    trigger_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    force_run INTEGER NOT NULL CHECK (force_run IN (0, 1)),
    warehouse_requested INTEGER NOT NULL CHECK (warehouse_requested IN (0, 1)),
    warehouse_refreshed INTEGER NOT NULL DEFAULT 0 CHECK (warehouse_refreshed IN (0, 1)),
    selected_sources_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    loaded_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    not_due_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT ''
);

CREATE TABLE source_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    source_id TEXT NOT NULL,
    target_release TEXT NOT NULL,
    source_vintage TEXT NOT NULL DEFAULT '',
    attempt_number INTEGER NOT NULL,
    due_reason TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    content_sha256 TEXT NOT NULL DEFAULT '',
    release_id TEXT NOT NULL DEFAULT '',
    observation_count INTEGER NOT NULL DEFAULT 0,
    transient INTEGER NOT NULL DEFAULT 0 CHECK (transient IN (0, 1)),
    next_retry_at TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE source_state (
    source_id TEXT PRIMARY KEY,
    last_checked_at TEXT NOT NULL DEFAULT '',
    last_success_at TEXT NOT NULL DEFAULT '',
    last_loaded_at TEXT NOT NULL DEFAULT '',
    last_target_release TEXT NOT NULL DEFAULT '',
    last_source_vintage TEXT NOT NULL DEFAULT '',
    last_content_sha256 TEXT NOT NULL DEFAULT '',
    last_release_id TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE release_fingerprints (
    source_id TEXT NOT NULL,
    target_release TEXT NOT NULL,
    source_vintage TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    release_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, content_sha256)
);

CREATE TABLE warehouse_refreshes (
    refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    fact_count INTEGER,
    failed_quality_check_count INTEGER,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_source_attempts_run_source
ON source_attempts(run_id, source_id, attempt_number);

CREATE INDEX idx_source_attempts_status
ON source_attempts(status, completed_at);

CREATE INDEX idx_runs_started
ON orchestration_runs(started_at DESC);

CREATE VIEW latest_source_attempts AS
SELECT * FROM (
    SELECT
        source_attempts.*,
        ROW_NUMBER() OVER (
            PARTITION BY source_id
            ORDER BY started_at DESC, attempt_id DESC
        ) AS attempt_rank
    FROM source_attempts
) ranked
WHERE attempt_rank = 1;
