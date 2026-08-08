from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Observation, SourceRelease, ValidationResult


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_releases (
    release_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    source_release_date TEXT,
    retrieved_at TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    revision_notes TEXT,
    next_expected_release TEXT,
    dataset_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    observation_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL REFERENCES source_releases(release_id),
    indicator_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    geography_type TEXT NOT NULL,
    geography_code TEXT NOT NULL,
    geography_name TEXT NOT NULL,
    period TEXT NOT NULL,
    frequency TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL,
    margin_of_error REAL,
    subgroup TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    value_status TEXT NOT NULL,
    dataset_status TEXT NOT NULL,
    source_release TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    notes TEXT,
    UNIQUE (
        release_id, indicator_id, metric_id, source_id, geography_type,
        geography_code, period, subgroup, industry_code, source_release
    )
);

CREATE TABLE IF NOT EXISTS validation_results (
    release_id TEXT NOT NULL REFERENCES source_releases(release_id),
    check_name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    affected_count INTEGER NOT NULL,
    PRIMARY KEY (release_id, check_name)
);

CREATE INDEX IF NOT EXISTS idx_observations_indicator_period
ON observations(indicator_id, metric_id, period);

CREATE INDEX IF NOT EXISTS idx_observations_geography
ON observations(geography_type, geography_code);

CREATE VIEW IF NOT EXISTS latest_observations AS
SELECT * FROM (
    SELECT
        observations.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                indicator_id, metric_id, source_id, geography_type,
                geography_code, period, subgroup, industry_code, source_release
            ORDER BY retrieved_at DESC, observation_pk DESC
        ) AS retrieval_rank
    FROM observations
) ranked
WHERE retrieval_rank = 1;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    return connection


def load_release(
    path: Path,
    release: SourceRelease,
    observations: Iterable[Observation],
    validations: Iterable[ValidationResult],
) -> None:
    rows = list(observations)
    checks = list(validations)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO source_releases VALUES
            (:release_id, :source_id, :source_release_date, :retrieved_at,
             :raw_sha256, :raw_path, :processing_status, :validation_status,
             :observation_count, :revision_notes, :next_expected_release,
             :dataset_status)
            """,
            release.as_dict(),
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO observations (
                release_id, indicator_id, metric_id, source_id, geography_type,
                geography_code, geography_name, period, frequency, value, unit,
                margin_of_error, subgroup, industry_code, adjustment, value_status,
                dataset_status, source_release, retrieved_at, raw_sha256, notes
            ) VALUES (
                :release_id, :indicator_id, :metric_id, :source_id, :geography_type,
                :geography_code, :geography_name, :period, :frequency, :value, :unit,
                :margin_of_error, :subgroup, :industry_code, :adjustment, :value_status,
                :dataset_status, :source_release, :retrieved_at, :raw_sha256, :notes
            )
            """,
            [{"release_id": release.release_id, **row.as_dict()} for row in rows],
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO validation_results
            (release_id, check_name, passed, severity, message, affected_count)
            VALUES (:release_id, :check_name, :passed, :severity, :message, :affected_count)
            """,
            [
                {
                    "release_id": release.release_id,
                    **check.as_dict(),
                    "passed": int(check.passed),
                }
                for check in checks
            ],
        )


def summary(path: Path) -> dict[str, object]:
    with connect(path) as connection:
        releases = connection.execute("SELECT COUNT(*) FROM source_releases").fetchone()[0]
        observations = connection.execute("SELECT COUNT(*) FROM latest_observations").fetchone()[0]
        indicators = connection.execute("SELECT COUNT(DISTINCT indicator_id) FROM latest_observations").fetchone()[0]
        geographies = connection.execute("SELECT COUNT(DISTINCT geography_code) FROM latest_observations").fetchone()[0]
        sources = connection.execute(
            """
            SELECT source_id, COUNT(*) AS observations
            FROM latest_observations GROUP BY source_id ORDER BY source_id
            """
        ).fetchall()
        failed = connection.execute(
            "SELECT COUNT(*) FROM validation_results WHERE passed = 0 AND severity = 'error'"
        ).fetchone()[0]
    return {
        "database": str(path),
        "release_count": releases,
        "observation_count": observations,
        "indicator_count": indicators,
        "geography_count": geographies,
        "failed_validation_count": failed,
        "sources": [{"source_id": source_id, "observations": count} for source_id, count in sources],
    }


def print_summary(path: Path) -> None:
    print(json.dumps(summary(path), indent=2, sort_keys=True))
