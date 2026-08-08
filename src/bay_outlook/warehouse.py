from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import PROJECT_ROOT


SCHEMA_VERSION = "5.0.0"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "phase5_warehouse.sql"
REQUIRED_STAGING_OBJECTS = {"source_releases", "observations", "validation_results", "latest_observations"}
MISSING_VALUE_STATES = {"suppressed", "missing", "not_computed"}
STANDARD_VALUE_STATES = ("final", "preliminary", "suppressed", "missing", "not_computed")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_project_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _score_class(total: int) -> str:
    if total >= 16:
        return "Core"
    if total >= 12:
        return "Caveat"
    if total >= 8:
        return "Experimental"
    return "Exclude"


def _period_attributes(label: str, frequency: str) -> dict[str, int | str | None]:
    annual = re.fullmatch(r"(\d{4})", label)
    quarter = re.fullmatch(r"(\d{4})-Q([1-4])", label, flags=re.IGNORECASE)
    month = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", label)
    academic = re.fullmatch(r"(\d{4})-(\d{2}|\d{4})", label)
    if annual:
        year = int(annual.group(1))
        return {
            "period_type": "calendar_year",
            "calendar_year": year,
            "quarter": None,
            "month": None,
            "academic_start_year": None,
            "academic_end_year": None,
            "sort_key": year * 100,
        }
    if quarter:
        year, quarter_number = int(quarter.group(1)), int(quarter.group(2))
        return {
            "period_type": "quarter",
            "calendar_year": year,
            "quarter": quarter_number,
            "month": None,
            "academic_start_year": None,
            "academic_end_year": None,
            "sort_key": year * 100 + quarter_number * 3,
        }
    if month:
        year, month_number = int(month.group(1)), int(month.group(2))
        return {
            "period_type": "month",
            "calendar_year": year,
            "quarter": (month_number - 1) // 3 + 1,
            "month": month_number,
            "academic_start_year": None,
            "academic_end_year": None,
            "sort_key": year * 100 + month_number,
        }
    if academic:
        start = int(academic.group(1))
        suffix = academic.group(2)
        end = int(suffix) if len(suffix) == 4 else (start // 100) * 100 + int(suffix)
        if end < start:
            end += 100
        return {
            "period_type": "academic_year",
            "calendar_year": None,
            "quarter": None,
            "month": None,
            "academic_start_year": start,
            "academic_end_year": end,
            "sort_key": end * 100 + 99,
        }
    digits = re.search(r"\d{4}", label)
    approximate_year = int(digits.group()) if digits else 0
    return {
        "period_type": "other",
        "calendar_year": None,
        "quarter": None,
        "month": None,
        "academic_start_year": None,
        "academic_end_year": None,
        "sort_key": approximate_year * 100,
    }


def _metric_label(metric_id: str) -> str:
    return metric_id.replace("_", " ").strip().title()


def _industry_attributes(code: str) -> tuple[str, str, int]:
    if code == "all":
        return "Not applicable / all", "none", 1
    if code == "10":
        return "Total, all industries", "QCEW", 1
    return code, "source-defined", 0


def _adjustment_attributes(code: str) -> tuple[str, int, int]:
    normalized = code.casefold()
    return (
        code.replace("_", " ").title(),
        int("seasonally adjusted" in normalized and "not seasonally" not in normalized),
        int("inflation" in normalized or "real" in normalized),
    )


def _required_staging_objects(connection: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = REQUIRED_STAGING_OBJECTS - names
    if missing:
        raise ValueError(f"Staging database is missing required objects: {sorted(missing)}")


def _rows(connection: sqlite3.Connection, query: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cursor = connection.execute(query, tuple(parameters))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _load_dimensions(
    connection: sqlite3.Connection,
    observations: list[dict[str, Any]],
    releases: list[dict[str, Any]],
    validations: list[dict[str, Any]],
    catalog: list[dict[str, str]],
    sources: list[dict[str, str]],
    counties: list[dict[str, str]],
) -> None:
    for key, row in enumerate(sorted(catalog, key=lambda item: item["indicator_id"]), start=1):
        connection.execute(
            "INSERT INTO dim_indicator VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                row["indicator_id"],
                row["pillar"],
                row["indicator_name"],
                row["primary_geography"],
                row["frequency"],
                row["priority"],
                int(row["phase4_pilot"] == "Yes"),
                row["headline_measure"],
            ),
        )

    score_columns = (
        "authority_score",
        "geographic_precision_score",
        "update_frequency_score",
        "historical_depth_score",
        "transparency_score",
        "reproducibility_score",
    )
    for key, row in enumerate(sorted(sources, key=lambda item: item["source_id"]), start=1):
        scores = [int(row[column]) for column in score_columns]
        quality_total = sum(scores)
        if row.get("quality_total") and int(row["quality_total"]) != quality_total:
            raise ValueError(f"Source quality score mismatch for {row['source_id']}")
        connection.execute(
            """
            INSERT INTO dim_source VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                key,
                row["source_id"],
                row["dataset_name"],
                row["agency"],
                int(row["source_tier"]),
                row["landing_url"],
                row["access_method"],
                int(row["credential_required"] == "Yes"),
                row["geographic_level"],
                row["geography_basis"],
                row["frequency"],
                row["historical_coverage"],
                row["typical_lag"],
                row["license_or_terms"],
                *scores,
                quality_total,
                _score_class(quality_total),
                row["phase4_status"],
                row["notes"],
            ),
        )

    indicator_keys = dict(connection.execute("SELECT indicator_id, indicator_key FROM dim_indicator"))
    metric_profiles: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in observations:
        metric_profiles[(row["indicator_id"], row["metric_id"])].add((row["unit"], row["frequency"]))
    for natural_key, profiles in metric_profiles.items():
        if len(profiles) != 1:
            raise ValueError(f"Metric has inconsistent unit/frequency: {natural_key} -> {sorted(profiles)}")
    for key, ((indicator_id, metric_id), profiles) in enumerate(sorted(metric_profiles.items()), start=1):
        if indicator_id not in indicator_keys:
            raise ValueError(f"Observation references unknown indicator: {indicator_id}")
        unit, frequency = next(iter(profiles))
        connection.execute(
            "INSERT INTO dim_metric VALUES (?, ?, ?, ?, ?, ?)",
            (key, indicator_keys[indicator_id], metric_id, _metric_label(metric_id), unit, frequency),
        )

    geography_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for county in counties:
        geography_rows[("county", county["county_fips"])] = {
            "geography_type": "county",
            "geography_code": county["county_fips"],
            "geography_name": county["county_name"],
            "state_fips": county["county_fips"][:2],
            "county_fips": county["county_fips"],
            "cde_county_code": county["county_code_cde"],
            "is_bay_area": int(county["region_member"] == "1"),
            "region_name": "San Francisco Bay Area" if county["region_member"] == "1" else "",
        }
    for row in observations:
        natural_key = (row["geography_type"], row["geography_code"])
        existing = geography_rows.get(natural_key)
        if existing and existing["geography_name"] != row["geography_name"]:
            raise ValueError(f"Conflicting geography names for {natural_key}")
        if not existing:
            geography_rows[natural_key] = {
                "geography_type": row["geography_type"],
                "geography_code": row["geography_code"],
                "geography_name": row["geography_name"],
                "state_fips": None,
                "county_fips": None,
                "cde_county_code": None,
                "is_bay_area": 0,
                "region_name": "",
            }
    for key, (_, row) in enumerate(sorted(geography_rows.items()), start=1):
        connection.execute(
            "INSERT INTO dim_geography VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                row["geography_type"],
                row["geography_code"],
                row["geography_name"],
                row["state_fips"],
                row["county_fips"],
                row["cde_county_code"],
                row["is_bay_area"],
                row["region_name"],
            ),
        )

    periods = sorted({(row["period"], row["frequency"]) for row in observations})
    for key, (label, frequency) in enumerate(periods, start=1):
        attributes = _period_attributes(label, frequency)
        connection.execute(
            "INSERT INTO dim_period VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                label,
                frequency,
                attributes["period_type"],
                attributes["calendar_year"],
                attributes["quarter"],
                attributes["month"],
                attributes["academic_start_year"],
                attributes["academic_end_year"],
                attributes["sort_key"],
            ),
        )

    subgroup_codes = sorted({row["subgroup"] for row in observations} or {"all"})
    for key, code in enumerate(subgroup_codes, start=1):
        connection.execute(
            "INSERT INTO dim_subgroup VALUES (?, ?, ?, ?)",
            (key, code, "All people / total" if code == "all" else _metric_label(code), int(code == "all")),
        )

    industry_codes = sorted({row["industry_code"] for row in observations} or {"all"})
    for key, code in enumerate(industry_codes, start=1):
        label, system, is_total = _industry_attributes(code)
        connection.execute(
            "INSERT INTO dim_industry VALUES (?, ?, ?, ?, ?)",
            (key, code, label, system, is_total),
        )

    adjustment_codes = sorted({row["adjustment"] for row in observations} or {"none"})
    for key, code in enumerate(adjustment_codes, start=1):
        label, seasonally_adjusted, inflation_adjusted = _adjustment_attributes(code)
        connection.execute(
            "INSERT INTO dim_adjustment VALUES (?, ?, ?, ?, ?)",
            (key, code, label, seasonally_adjusted, inflation_adjusted),
        )

    observed_statuses = {(row["value_status"], row["dataset_status"]) for row in observations}
    status_rows = observed_statuses | {
        (value_status, dataset_status)
        for dataset_status in ("official", "fixture")
        for value_status in STANDARD_VALUE_STATES
    }
    for key, (value_status, dataset_status) in enumerate(sorted(status_rows), start=1):
        connection.execute(
            "INSERT INTO dim_value_status VALUES (?, ?, ?, ?, ?, ?)",
            (
                key,
                value_status,
                dataset_status,
                int(dataset_status == "official"),
                int(value_status == "suppressed"),
                int(value_status in MISSING_VALUE_STATES),
            ),
        )

    source_keys = dict(connection.execute("SELECT source_id, source_key FROM dim_source"))
    vintages: dict[str, set[str]] = defaultdict(set)
    for row in observations:
        vintages[row["release_id"]].add(row["source_release"])
    for key, row in enumerate(sorted(releases, key=lambda item: item["release_id"]), start=1):
        if row["source_id"] not in source_keys:
            raise ValueError(f"Release references unknown source: {row['source_id']}")
        release_vintages = vintages.get(row["release_id"], set())
        if len(release_vintages) > 1:
            raise ValueError(f"Release has multiple source vintages: {row['release_id']}")
        source_vintage = next(iter(release_vintages), row["source_release_date"] or "")
        connection.execute(
            "INSERT INTO dim_source_release VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                row["release_id"],
                source_keys[row["source_id"]],
                source_vintage,
                row["source_release_date"],
                row["retrieved_at"],
                row["raw_sha256"],
                row["raw_path"],
                row["processing_status"],
                row["validation_status"],
                row["observation_count"],
                row["revision_notes"] or "",
                row["next_expected_release"] or "",
                row["dataset_status"],
            ),
        )

    release_keys = dict(connection.execute("SELECT release_id, release_key FROM dim_source_release"))
    metric_keys = {
        (indicator_id, metric_id): key
        for indicator_id, metric_id, key in connection.execute(
            """
            SELECT i.indicator_id, m.metric_id, m.metric_key
            FROM dim_metric AS m JOIN dim_indicator AS i USING (indicator_key)
            """
        )
    }
    geography_keys = {
        (kind, code): key
        for kind, code, key in connection.execute(
            "SELECT geography_type, geography_code, geography_key FROM dim_geography"
        )
    }
    period_keys = {
        (label, frequency): key
        for label, frequency, key in connection.execute(
            "SELECT period_label, frequency, period_key FROM dim_period"
        )
    }
    subgroup_keys = dict(connection.execute("SELECT subgroup_code, subgroup_key FROM dim_subgroup"))
    industry_keys = dict(connection.execute("SELECT industry_code, industry_key FROM dim_industry"))
    adjustment_keys = dict(connection.execute("SELECT adjustment_code, adjustment_key FROM dim_adjustment"))
    value_status_keys = {
        (value_status, dataset_status): key
        for value_status, dataset_status, key in connection.execute(
            "SELECT value_status, dataset_status, value_status_key FROM dim_value_status"
        )
    }

    releases_by_id = {row["release_id"]: row for row in releases}
    fact_rows = []
    for observation_key, row in enumerate(observations, start=1):
        release = releases_by_id[row["release_id"]]
        if release["source_id"] != row["source_id"]:
            raise ValueError(f"Observation/release source mismatch for {row['release_id']}")
        if release["dataset_status"] != row["dataset_status"]:
            raise ValueError(f"Observation/release dataset-status mismatch for {row['release_id']}")
        fact_rows.append(
            (
                observation_key,
                release_keys[row["release_id"]],
                metric_keys[(row["indicator_id"], row["metric_id"])],
                geography_keys[(row["geography_type"], row["geography_code"])],
                period_keys[(row["period"], row["frequency"])],
                subgroup_keys[row["subgroup"]],
                industry_keys[row["industry_code"]],
                adjustment_keys[row["adjustment"]],
                value_status_keys[(row["value_status"], row["dataset_status"])],
                row["value"],
                row["margin_of_error"],
                row["notes"] or "",
            )
        )
    connection.executemany(
        "INSERT INTO fact_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        fact_rows,
    )

    for row in validations:
        connection.execute(
            "INSERT INTO fact_validation_result VALUES (?, ?, ?, ?, ?, ?)",
            (
                release_keys[row["release_id"]],
                row["check_name"],
                row["passed"],
                row["severity"],
                row["message"],
                row["affected_count"],
            ),
        )


def _quality_checks(
    connection: sqlite3.Connection,
    expected_fact_count: int,
    include_fixtures: bool,
) -> list[dict[str, Any]]:
    foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    fact_count = connection.execute("SELECT COUNT(*) FROM fact_observation").fetchone()[0]
    duplicate_grain = connection.execute(
        """
        SELECT COALESCE(SUM(row_count - 1), 0)
        FROM (
            SELECT COUNT(*) AS row_count
            FROM fact_observation
            GROUP BY release_key, metric_key, geography_key, period_key,
                     subgroup_key, industry_key, adjustment_key
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    null_state_errors = connection.execute(
        """
        SELECT COUNT(*)
        FROM fact_observation AS f
        JOIN dim_value_status AS s USING (value_status_key)
        WHERE f.value IS NULL AND s.value_status NOT IN ('suppressed', 'missing', 'not_computed')
        """
    ).fetchone()[0]
    fixture_facts = connection.execute(
        """
        SELECT COUNT(*)
        FROM fact_observation AS f
        JOIN dim_value_status AS s USING (value_status_key)
        WHERE s.dataset_status = 'fixture'
        """
    ).fetchone()[0]
    bay_area_counties = connection.execute(
        "SELECT COUNT(*) FROM dim_geography WHERE geography_type = 'county' AND is_bay_area = 1"
    ).fetchone()[0]
    failed_source_validations = connection.execute(
        """
        SELECT COUNT(*) FROM fact_validation_result
        WHERE passed = 0 AND severity = 'error'
        """
    ).fetchone()[0]
    release_count = connection.execute("SELECT COUNT(*) FROM dim_source_release").fetchone()[0]
    validated_release_count = connection.execute(
        "SELECT COUNT(DISTINCT release_key) FROM fact_validation_result"
    ).fetchone()[0]
    blocked_release_count = connection.execute(
        """
        SELECT COUNT(*) FROM dim_source_release
        WHERE processing_status != 'processed' OR validation_status != 'passed'
        """
    ).fetchone()[0]
    release_provenance_errors = connection.execute(
        """
        SELECT COUNT(*) FROM dim_source_release
        WHERE raw_sha256 = '' OR raw_path = '' OR retrieved_at = ''
        """
    ).fetchone()[0]
    checks = [
        {
            "check_name": "source_fact_count",
            "passed": int(fact_count == expected_fact_count),
            "affected_count": abs(fact_count - expected_fact_count),
            "message": f"Expected and loaded fact counts must match ({expected_fact_count}).",
        },
        {
            "check_name": "foreign_key_integrity",
            "passed": int(foreign_key_violations == 0),
            "affected_count": foreign_key_violations,
            "message": "All fact and audit foreign keys must resolve.",
        },
        {
            "check_name": "unique_fact_grain",
            "passed": int(duplicate_grain == 0),
            "affected_count": duplicate_grain,
            "message": "The declared Phase 5 fact grain must be unique.",
        },
        {
            "check_name": "null_value_state",
            "passed": int(null_state_errors == 0),
            "affected_count": null_state_errors,
            "message": "Null facts require an explicit suppressed, missing, or not_computed state.",
        },
        {
            "check_name": "fixture_exclusion",
            "passed": int(include_fixtures or fixture_facts == 0),
            "affected_count": 0 if include_fixtures else fixture_facts,
            "message": "Fixture facts are excluded unless include_fixtures is explicitly enabled.",
        },
        {
            "check_name": "nine_county_scope",
            "passed": int(bay_area_counties == 9),
            "affected_count": abs(bay_area_counties - 9),
            "message": "The geography dimension must contain all nine Bay Area counties.",
        },
        {
            "check_name": "source_validation_gate",
            "passed": int(failed_source_validations == 0),
            "affected_count": failed_source_validations,
            "message": "No loaded release may have a failed error-severity staging check.",
        },
        {
            "check_name": "release_validation_completeness",
            "passed": int(validated_release_count == release_count),
            "affected_count": abs(validated_release_count - release_count),
            "message": "Every loaded release must retain its staging validation results.",
        },
        {
            "check_name": "release_status_gate",
            "passed": int(blocked_release_count == 0),
            "affected_count": blocked_release_count,
            "message": "Loaded releases must be processed and validation-passed.",
        },
        {
            "check_name": "release_provenance",
            "passed": int(release_provenance_errors == 0),
            "affected_count": release_provenance_errors,
            "message": "Every loaded release must retain retrieval time, raw path, and raw SHA-256.",
        },
    ]
    connection.executemany(
        "INSERT INTO warehouse_quality_checks VALUES (:check_name, :passed, :affected_count, :message)",
        checks,
    )
    return checks


def build_warehouse(
    staging_path: Path,
    warehouse_path: Path,
    *,
    include_fixtures: bool = False,
) -> dict[str, Any]:
    staging_path = staging_path.resolve()
    warehouse_path = warehouse_path.resolve()
    if not staging_path.is_file():
        raise FileNotFoundError(f"Staging database not found: {staging_path}")
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"Warehouse schema not found: {SCHEMA_PATH}")

    catalog = _read_csv(PROJECT_ROOT / "metadata" / "indicator_catalog.csv")
    sources = _read_csv(PROJECT_ROOT / "metadata" / "source_registry.csv")
    counties = _read_csv(PROJECT_ROOT / "config" / "counties.csv")
    if len(catalog) != 25:
        raise ValueError(f"Expected 25 catalog indicators; found {len(catalog)}")
    if len(counties) != 9:
        raise ValueError(f"Expected nine Bay Area counties; found {len(counties)}")

    with sqlite3.connect(staging_path) as staging:
        _required_staging_objects(staging)
        where = "" if include_fixtures else "WHERE dataset_status != 'fixture'"
        observations = _rows(staging, f"SELECT * FROM latest_observations {where} ORDER BY observation_pk")
        release_ids = sorted({row["release_id"] for row in observations})
        if release_ids:
            placeholders = ",".join("?" for _ in release_ids)
            releases = _rows(
                staging,
                f"SELECT * FROM source_releases WHERE release_id IN ({placeholders}) ORDER BY release_id",
                release_ids,
            )
            validations = _rows(
                staging,
                f"SELECT * FROM validation_results WHERE release_id IN ({placeholders}) ORDER BY release_id, check_name",
                release_ids,
            )
        else:
            releases = []
            validations = []

    warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix=f"{warehouse_path.name}.",
        suffix=".tmp",
        dir=warehouse_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    started_at = _utc_now()
    source_hash = _sha256_file(staging_path)
    try:
        with sqlite3.connect(temporary_path) as warehouse:
            warehouse.execute("PRAGMA foreign_keys = ON")
            warehouse.execute("PRAGMA journal_mode = DELETE")
            warehouse.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            metadata = {
                "project": "The Bay Outlook",
                "phase": "5",
                "schema_version": SCHEMA_VERSION,
                "fact_grain": (
                    "one row per source retrieval, metric, geography, period, subgroup, "
                    "industry, and adjustment state"
                ),
                "built_from_sha256": source_hash,
                "built_at": started_at,
            }
            warehouse.executemany(
                "INSERT INTO warehouse_metadata VALUES (?, ?)",
                sorted(metadata.items()),
            )
            load_key = warehouse.execute(
                """
                INSERT INTO warehouse_loads (
                    started_at, source_database, source_database_sha256,
                    include_fixtures, source_observation_count, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (
                    started_at,
                    _portable_project_path(staging_path),
                    source_hash,
                    int(include_fixtures),
                    len(observations),
                ),
            ).lastrowid
            _load_dimensions(warehouse, observations, releases, validations, catalog, sources, counties)
            checks = _quality_checks(warehouse, len(observations), include_fixtures)
            failed = [check for check in checks if not check["passed"]]
            if failed:
                raise ValueError(f"Warehouse quality checks failed: {failed}")
            completed_at = _utc_now()
            warehouse.execute(
                """
                UPDATE warehouse_loads
                SET completed_at = ?, loaded_fact_count = ?, status = 'passed'
                WHERE load_key = ?
                """,
                (completed_at, len(observations), load_key),
            )
            warehouse.commit()
        os.replace(temporary_path, warehouse_path)
        os.chmod(warehouse_path, 0o644)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return warehouse_summary(warehouse_path)


def warehouse_summary(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        metadata = dict(connection.execute("SELECT metadata_key, metadata_value FROM warehouse_metadata"))
        table_counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "dim_indicator",
                "dim_metric",
                "dim_source",
                "dim_geography",
                "dim_period",
                "dim_subgroup",
                "dim_industry",
                "dim_adjustment",
                "dim_value_status",
                "dim_source_release",
                "fact_observation",
                "fact_validation_result",
            )
        }
        publishable = connection.execute("SELECT COUNT(*) FROM vw_publishable_observations").fetchone()[0]
        education_facts = connection.execute(
            """
            SELECT COUNT(*)
            FROM vw_observation_enriched
            WHERE indicator_id GLOB 'E[0-9]*'
            """
        ).fetchone()[0]
        publishable_education = connection.execute(
            "SELECT COUNT(*) FROM vw_education_observations"
        ).fetchone()[0]
        failed_checks = connection.execute(
            "SELECT COUNT(*) FROM warehouse_quality_checks WHERE passed = 0"
        ).fetchone()[0]
        coverage = [
            {
                "indicator_id": row[0],
                "observation_count": row[1],
                "geography_count": row[2],
                "source_count": row[3],
            }
            for row in connection.execute(
                """
                SELECT indicator_id, observation_count, geography_count, source_count
                FROM vw_indicator_coverage
                WHERE observation_count > 0
                ORDER BY indicator_id
                """
            )
        ]
    return {
        "database": str(path),
        "schema_version": metadata.get("schema_version"),
        "fact_grain": metadata.get("fact_grain"),
        "table_counts": table_counts,
        "publishable_observation_count": publishable,
        "education_fact_count": education_facts,
        "publishable_education_observation_count": publishable_education,
        "failed_quality_check_count": failed_checks,
        "indicator_coverage": coverage,
    }


def print_warehouse_summary(path: Path) -> None:
    print(json.dumps(warehouse_summary(path), indent=2, sort_keys=True))
