from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "6.0.0"

OPERATIONS_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operation_metadata (
    metadata_key TEXT PRIMARY KEY,
    metadata_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_runs (
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

CREATE TABLE IF NOT EXISTS source_attempts (
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

CREATE TABLE IF NOT EXISTS source_state (
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

CREATE TABLE IF NOT EXISTS release_fingerprints (
    source_id TEXT NOT NULL,
    target_release TEXT NOT NULL,
    source_vintage TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    release_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_id, content_sha256)
);

CREATE TABLE IF NOT EXISTS warehouse_refreshes (
    refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES orchestration_runs(run_id),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    fact_count INTEGER,
    failed_quality_check_count INTEGER,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_source_attempts_run_source
ON source_attempts(run_id, source_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_source_attempts_status
ON source_attempts(status, completed_at);

CREATE INDEX IF NOT EXISTS idx_runs_started
ON orchestration_runs(started_at DESC);

CREATE VIEW IF NOT EXISTS latest_source_attempts AS
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
"""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    # The run lock already serializes writers. DELETE mode keeps the portable
    # checkpoint self-contained instead of requiring SQLite WAL sidecars.
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.executescript(OPERATIONS_SCHEMA)
    connection.execute(
        "INSERT OR REPLACE INTO operation_metadata VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    return connection


def read_source_state(path: Path, source_id: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        return None


class OperationsStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        with _connect(self.path):
            pass

    def start_run(self, values: dict[str, Any]) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO orchestration_runs (
                    run_id, started_at, as_of, trigger_name, status, force_run,
                    warehouse_requested, selected_sources_json, plan_json
                ) VALUES (
                    :run_id, :started_at, :as_of, :trigger_name, 'running', :force_run,
                    :warehouse_requested, :selected_sources_json, :plan_json
                )
                """,
                values,
            )

    def finish_run(self, run_id: str, values: dict[str, Any]) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                """
                UPDATE orchestration_runs SET
                    completed_at = :completed_at,
                    status = :status,
                    warehouse_refreshed = :warehouse_refreshed,
                    loaded_count = :loaded_count,
                    duplicate_count = :duplicate_count,
                    blocked_count = :blocked_count,
                    failure_count = :failure_count,
                    not_due_count = :not_due_count,
                    error_summary = :error_summary
                WHERE run_id = :run_id
                """,
                {"run_id": run_id, **values},
            )

    def start_attempt(self, values: dict[str, Any]) -> int:
        with _connect(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO source_attempts (
                    run_id, source_id, target_release, attempt_number,
                    due_reason, started_at, status
                ) VALUES (
                    :run_id, :source_id, :target_release, :attempt_number,
                    :due_reason, :started_at, 'running'
                )
                """,
                values,
            )
            return int(cursor.lastrowid)

    def finish_attempt(self, attempt_id: int, values: dict[str, Any]) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                """
                UPDATE source_attempts SET
                    completed_at = :completed_at,
                    status = :status,
                    source_vintage = :source_vintage,
                    content_sha256 = :content_sha256,
                    release_id = :release_id,
                    observation_count = :observation_count,
                    transient = :transient,
                    next_retry_at = :next_retry_at,
                    error_class = :error_class,
                    error_message = :error_message
                WHERE attempt_id = :attempt_id
                """,
                {"attempt_id": attempt_id, **values},
            )

    def update_source_state(
        self,
        *,
        source_id: str,
        checked_at: str,
        status: str,
        target_release: str,
        source_vintage: str = "",
        content_sha256: str = "",
        release_id: str = "",
        next_retry_at: str = "",
        error_message: str = "",
        successful: bool = False,
        loaded: bool = False,
    ) -> None:
        with _connect(self.path) as connection:
            current = connection.execute(
                "SELECT * FROM source_state WHERE source_id = ?", (source_id,)
            ).fetchone()
            previous = dict(current) if current else {}
            failures = 0 if successful else int(previous.get("consecutive_failures", 0)) + 1
            values = {
                "source_id": source_id,
                "last_checked_at": checked_at,
                "last_success_at": checked_at if successful else previous.get("last_success_at", ""),
                "last_loaded_at": checked_at if loaded else previous.get("last_loaded_at", ""),
                "last_target_release": target_release or previous.get("last_target_release", ""),
                "last_source_vintage": source_vintage or previous.get("last_source_vintage", ""),
                "last_content_sha256": content_sha256 or previous.get("last_content_sha256", ""),
                "last_release_id": release_id or previous.get("last_release_id", ""),
                "last_status": status,
                "consecutive_failures": failures,
                "next_retry_at": next_retry_at,
                "last_error": error_message,
            }
            connection.execute(
                """
                INSERT INTO source_state VALUES (
                    :source_id, :last_checked_at, :last_success_at, :last_loaded_at,
                    :last_target_release, :last_source_vintage, :last_content_sha256,
                    :last_release_id, :last_status, :consecutive_failures,
                    :next_retry_at, :last_error
                )
                ON CONFLICT(source_id) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = excluded.last_success_at,
                    last_loaded_at = excluded.last_loaded_at,
                    last_target_release = excluded.last_target_release,
                    last_source_vintage = excluded.last_source_vintage,
                    last_content_sha256 = excluded.last_content_sha256,
                    last_release_id = excluded.last_release_id,
                    last_status = excluded.last_status,
                    consecutive_failures = excluded.consecutive_failures,
                    next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error
                """,
                values,
            )

    def record_fingerprint(
        self,
        *,
        source_id: str,
        target_release: str,
        source_vintage: str,
        content_sha256: str,
        release_id: str,
        first_seen_at: str,
    ) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO release_fingerprints VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_id, target_release, source_vintage, content_sha256, release_id, first_seen_at),
            )

    def start_warehouse_refresh(self, run_id: str, started_at: str) -> int:
        with _connect(self.path) as connection:
            cursor = connection.execute(
                "INSERT INTO warehouse_refreshes (run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, started_at),
            )
            return int(cursor.lastrowid)

    def finish_warehouse_refresh(
        self,
        refresh_id: int,
        *,
        completed_at: str,
        status: str,
        fact_count: int | None = None,
        failed_quality_check_count: int | None = None,
        error_message: str = "",
    ) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                """
                UPDATE warehouse_refreshes SET
                    completed_at = ?, status = ?, fact_count = ?,
                    failed_quality_check_count = ?, error_message = ?
                WHERE refresh_id = ?
                """,
                (
                    completed_at,
                    status,
                    fact_count,
                    failed_quality_check_count,
                    error_message,
                    refresh_id,
                ),
            )


def operations_summary(path: Path) -> dict[str, Any]:
    empty = {
        "database": str(path),
        "schema_version": SCHEMA_VERSION,
        "initialized": False,
        "run_count": 0,
        "attempt_count": 0,
        "fingerprint_count": 0,
        "warehouse_refresh_count": 0,
        "source_state": [],
        "recent_runs": [],
    }
    if not path.exists():
        return empty
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            counts = {
                "run_count": connection.execute("SELECT COUNT(*) FROM orchestration_runs").fetchone()[0],
                "attempt_count": connection.execute("SELECT COUNT(*) FROM source_attempts").fetchone()[0],
                "fingerprint_count": connection.execute("SELECT COUNT(*) FROM release_fingerprints").fetchone()[0],
                "warehouse_refresh_count": connection.execute("SELECT COUNT(*) FROM warehouse_refreshes").fetchone()[0],
            }
            states = [dict(row) for row in connection.execute("SELECT * FROM source_state ORDER BY source_id")]
            runs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, started_at, completed_at, as_of, trigger_name, status,
                           loaded_count, duplicate_count, blocked_count, failure_count,
                           not_due_count, warehouse_refreshed
                    FROM orchestration_runs ORDER BY started_at DESC, run_id DESC LIMIT 10
                    """
                )
            ]
        return {**empty, "initialized": True, **counts, "source_state": states, "recent_runs": runs}
    except sqlite3.Error as error:
        return {**empty, "error": str(error)}
