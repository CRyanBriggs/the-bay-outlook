from __future__ import annotations

from collections import Counter
from typing import Iterable

from .constants import BAY_AREA_FIPS
from .models import Observation, ValidationResult


RATE_UNITS = {"percent", "percentage points"}


def validate_observations(observations: Iterable[Observation]) -> list[ValidationResult]:
    rows = list(observations)
    results: list[ValidationResult] = []
    results.append(
        ValidationResult("nonempty", bool(rows), "error", "At least one observation is required.", 0 if rows else 1)
    )
    if not rows:
        return results

    duplicates = sum(count - 1 for count in Counter(row.natural_key for row in rows).values() if count > 1)
    results.append(
        ValidationResult(
            "unique_natural_key",
            duplicates == 0,
            "error",
            "Observation natural keys must be unique.",
            duplicates,
        )
    )

    invalid_geographies = sum(
        1 for row in rows if row.geography_type == "county" and row.geography_code not in BAY_AREA_FIPS
    )
    results.append(
        ValidationResult(
            "bay_area_geography",
            invalid_geographies == 0,
            "error",
            "County observations must use one of the nine configured Bay Area FIPS codes.",
            invalid_geographies,
        )
    )

    invalid_rates = sum(
        1
        for row in rows
        if row.value is not None and row.unit in RATE_UNITS and not (0.0 <= row.value <= 100.0)
    )
    results.append(
        ValidationResult(
            "rate_range",
            invalid_rates == 0,
            "error",
            "Percent and percentage-point observations must be between 0 and 100.",
            invalid_rates,
        )
    )

    null_without_state = sum(
        1 for row in rows if row.value is None and row.value_status not in {"suppressed", "missing", "not_computed"}
    )
    results.append(
        ValidationResult(
            "null_state",
            null_without_state == 0,
            "error",
            "Null values require an explicit suppressed, missing, or not_computed status.",
            null_without_state,
        )
    )

    missing_hashes = sum(1 for row in rows if not row.raw_sha256)
    results.append(
        ValidationResult(
            "raw_provenance",
            missing_hashes == 0,
            "error",
            "Every observation must retain the raw snapshot hash.",
            missing_hashes,
        )
    )

    fixtures = sum(1 for row in rows if row.dataset_status == "fixture")
    results.append(
        ValidationResult(
            "fixture_guard",
            True,
            "info",
            "Fixture rows are explicitly labeled and must never be published.",
            fixtures,
        )
    )
    return results


def validation_status(results: Iterable[ValidationResult]) -> str:
    return "passed" if all(result.passed or result.severity != "error" for result in results) else "failed"
