from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Observation:
    indicator_id: str
    metric_id: str
    source_id: str
    geography_type: str
    geography_code: str
    geography_name: str
    period: str
    frequency: str
    value: float | None
    unit: str
    margin_of_error: float | None = None
    subgroup: str = "all"
    industry_code: str = "all"
    adjustment: str = "none"
    value_status: str = "final"
    dataset_status: str = "official"
    source_release: str = ""
    retrieved_at: str = ""
    raw_sha256: str = ""
    notes: str = ""

    @property
    def natural_key(self) -> tuple[str, ...]:
        return (
            self.indicator_id,
            self.metric_id,
            self.source_id,
            self.geography_type,
            self.geography_code,
            self.period,
            self.subgroup,
            self.industry_code,
            self.source_release,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Snapshot:
    source_id: str
    source_url: str
    source_release: str
    retrieved_at: str
    sha256: str
    path: Path
    metadata_path: Path
    dataset_status: str = "official"


@dataclass(frozen=True, slots=True)
class SourceRelease:
    release_id: str
    source_id: str
    source_release_date: str
    retrieved_at: str
    raw_sha256: str
    raw_path: str
    processing_status: str
    validation_status: str
    observation_count: int
    revision_notes: str = ""
    next_expected_release: str = ""
    dataset_status: str = "official"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    check_name: str
    passed: bool
    severity: str
    message: str
    affected_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
