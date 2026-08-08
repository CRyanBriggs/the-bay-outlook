from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

from .database import load_release
from .models import Observation, SourceRelease
from .storage import append_release_log, write_observations_csv
from .validation import validate_observations, validation_status


def combined_hash(hashes: Iterable[str]) -> str:
    values = sorted(set(hashes))
    if len(values) == 1:
        return values[0]
    return "multiple:" + hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def persist(
    *,
    observations: Iterable[Observation],
    release_id: str,
    source_id: str,
    source_release_date: str,
    retrieved_at: str,
    raw_path: str,
    processed_path: Path,
    database_path: Path,
    release_log_path: Path,
    dataset_status: str = "official",
    revision_notes: str = "",
    next_expected_release: str = "",
) -> SourceRelease:
    rows = list(observations)
    checks = validate_observations(rows)
    status = validation_status(checks)
    release = SourceRelease(
        release_id=release_id,
        source_id=source_id,
        source_release_date=source_release_date,
        retrieved_at=retrieved_at,
        raw_sha256=combined_hash(row.raw_sha256 for row in rows),
        raw_path=raw_path,
        processing_status="processed" if rows else "blocked",
        validation_status=status,
        observation_count=len(rows),
        revision_notes=revision_notes,
        next_expected_release=next_expected_release,
        dataset_status=dataset_status,
    )
    if status != "passed":
        failures = "; ".join(check.message for check in checks if not check.passed and check.severity == "error")
        raise ValueError(f"Validation failed for {release_id}: {failures}")
    write_observations_csv(processed_path, rows)
    load_release(database_path, release, rows, checks)
    append_release_log(release_log_path, release)
    return release
