from __future__ import annotations

import calendar
import csv
import hashlib
import json
import os
import re
import sqlite3
import statistics
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import PROJECT_ROOT


SCHEMA_VERSION = "7.0.0"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "phase7_analysis.sql"
POLICY_PATH = PROJECT_ROOT / "metadata" / "analysis_metric_policy.csv"
REQUIRED_WAREHOUSE_OBJECTS = {
    "warehouse_metadata",
    "dim_indicator",
    "vw_observation_enriched",
}
EXPORT_VIEWS = {
    "latest_county_snapshot.csv": (
        "vw_latest_county_snapshot",
        "indicator_id, metric_id, geography_code, period_sort_key",
    ),
    "headline_snapshot.csv": (
        "vw_headline_snapshot",
        "indicator_id, metric_id, geography_code, period_sort_key",
    ),
    "education_snapshot.csv": (
        "vw_education_snapshot",
        "indicator_id, metric_id, geography_code, period_sort_key",
    ),
    "county_time_series.csv": (
        "vw_county_time_series",
        "indicator_id, metric_id, geography_code, period_sort_key",
    ),
    "indicator_readiness.csv": ("indicator_readiness", "indicator_id"),
}


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


def _rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, tuple(parameters))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _required_warehouse_objects(connection: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = REQUIRED_WAREHOUSE_OBJECTS - names
    if missing:
        raise ValueError(f"Warehouse is missing required objects: {sorted(missing)}")


def _parse_as_of(value: str | date | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _metric_label(metric_id: str) -> str:
    return metric_id.replace("_", " ").strip().title()


def _normalize_policy(row: dict[str, str], *, policy_status: str = "configured") -> dict[str, Any]:
    normalized = {
        "indicator_id": row["indicator_id"].strip(),
        "metric_id": row["metric_id"].strip(),
        "analysis_label": row["analysis_label"].strip(),
        "analysis_class": row["analysis_class"].strip(),
        "primary_metric": int(row["primary_metric"].strip().casefold() in {"yes", "true", "1"}),
        "comparison_basis": row["comparison_basis"].strip(),
        "rank_direction": row["rank_direction"].strip(),
        "change_method": row["change_method"].strip(),
        "stable_threshold": float(row["stable_threshold"]),
        "benchmark_method": row["benchmark_method"].strip(),
        "display_precision": int(row["display_precision"]),
        "interpretation_note": row["interpretation_note"].strip(),
        "policy_status": policy_status,
    }
    allowed = {
        "analysis_class": {"rate", "level", "currency", "index"},
        "comparison_basis": {"none", "level", "year_change"},
        "rank_direction": {"higher_is_better", "lower_is_better", "neutral"},
        "change_method": {"percent", "percentage_point"},
    }
    for field, values in allowed.items():
        if normalized[field] not in values:
            raise ValueError(f"Invalid {field} for {normalized['indicator_id']}/{normalized['metric_id']}")
    if normalized["comparison_basis"] != "none" and not normalized["benchmark_method"]:
        raise ValueError(
            f"A comparison policy requires a benchmark method: "
            f"{normalized['indicator_id']}/{normalized['metric_id']}"
        )
    if normalized["comparison_basis"] == "none" and normalized["rank_direction"] != "neutral":
        raise ValueError(
            f"A non-comparable metric must use neutral rank direction: "
            f"{normalized['indicator_id']}/{normalized['metric_id']}"
        )
    return normalized


def _inferred_policy(observation: dict[str, Any]) -> dict[str, Any]:
    is_rate = str(observation["unit"]).casefold() == "percent"
    return {
        "indicator_id": observation["indicator_id"],
        "metric_id": observation["metric_id"],
        "analysis_label": _metric_label(observation["metric_id"]),
        "analysis_class": "rate" if is_rate else "level",
        "primary_metric": 0,
        "comparison_basis": "none",
        "rank_direction": "neutral",
        "change_method": "percentage_point" if is_rate else "percent",
        "stable_threshold": 0.1 if is_rate else 0.5,
        "benchmark_method": "",
        "display_precision": 1 if is_rate else 2,
        "interpretation_note": (
            "Safe inferred policy: trends are calculated, but county comparison and normative ranking "
            "remain disabled until an explicit metric policy is reviewed."
        ),
        "policy_status": "inferred",
    }


def _period_attributes(row: dict[str, Any]) -> tuple[int, str, int]:
    frequency = str(row["frequency"])
    period_type = str(row["period_type"])
    period = str(row["period"])
    month_match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", period)
    quarter_match = re.fullmatch(r"(\d{4})-Q([1-4])", period, flags=re.IGNORECASE)
    academic_match = re.fullmatch(r"(\d{4})-(\d{2}|\d{4})", period)
    annual_match = re.fullmatch(r"(\d{4})", period)
    calendar_year = row.get("calendar_year")
    month_number = row.get("month")
    quarter_number = row.get("quarter")
    if frequency == "monthly" and (calendar_year or month_match):
        year = int(calendar_year or month_match.group(1))
        month = int(month_number or month_match.group(2))
        end = date(year, month, calendar.monthrange(year, month)[1])
        return year * 12 + month - 1, end.isoformat(), 12
    if frequency == "quarterly" and (calendar_year or quarter_match):
        year = int(calendar_year or quarter_match.group(1))
        quarter = int(quarter_number or quarter_match.group(2))
        month = quarter * 3
        end = date(year, month, calendar.monthrange(year, month)[1])
        return year * 4 + quarter - 1, end.isoformat(), 4
    if period_type == "academic_year" and (row.get("academic_end_year") or academic_match):
        if row.get("academic_end_year"):
            end_year = int(row["academic_end_year"])
        else:
            start_year = int(academic_match.group(1))
            suffix = academic_match.group(2)
            end_year = int(suffix) if len(suffix) == 4 else (start_year // 100) * 100 + int(suffix)
            if end_year < start_year:
                end_year += 100
        return end_year, date(end_year, 6, 30).isoformat(), 1
    if frequency == "annual" and (calendar_year or annual_match):
        year = int(calendar_year or annual_match.group(1))
        return year, date(year, 12, 31).isoformat(), 1
    return int(row["period_sort_key"]), "", 1


def _series_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["analysis_metric_key"],
        row["source_id"],
        row["geography_type"],
        row["geography_code"],
        row["frequency"],
        row["subgroup"],
        row["industry_code"],
        row["adjustment"],
    )


def _source_observations(
    connection: sqlite3.Connection,
    *,
    include_fixtures: bool,
) -> tuple[int, list[dict[str, Any]]]:
    fixture_clause = "" if include_fixtures else "AND dataset_status = 'official' AND is_publishable = 1"
    source_count = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM vw_observation_enriched
        WHERE processing_status = 'processed'
          AND validation_status = 'passed'
          {fixture_clause}
        """
    ).fetchone()[0]
    observations = _rows(
        connection,
        f"""
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        indicator_id, metric_id, source_id, geography_type,
                        geography_code, period, frequency, subgroup,
                        industry_code, adjustment
                    ORDER BY
                        CASE WHEN dataset_status = 'official' THEN 0 ELSE 1 END,
                        retrieved_at DESC,
                        observation_key DESC
                ) AS current_rank
            FROM vw_observation_enriched
            WHERE processing_status = 'processed'
              AND validation_status = 'passed'
              {fixture_clause}
        )
        SELECT * FROM ranked
        WHERE current_rank = 1
        ORDER BY
            indicator_id, metric_id, source_id, geography_type,
            geography_code, frequency, period_sort_key, subgroup,
            industry_code, adjustment
        """,
    )
    return int(source_count), observations


def _load_metric_policies(
    connection: sqlite3.Connection,
    observations: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    policy_path: Path,
) -> tuple[dict[tuple[str, str], dict[str, Any]], int]:
    indicator_ids = {row["indicator_id"] for row in catalog}
    policies: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in _read_csv(policy_path):
        policy = _normalize_policy(raw)
        key = (policy["indicator_id"], policy["metric_id"])
        if key in policies:
            raise ValueError(f"Duplicate analytical metric policy: {key}")
        if policy["indicator_id"] not in indicator_ids:
            raise ValueError(f"Policy references an unknown indicator: {key}")
        policies[key] = policy

    inferred_count = 0
    for observation in observations:
        key = (observation["indicator_id"], observation["metric_id"])
        if key not in policies:
            policies[key] = _inferred_policy(observation)
            inferred_count += 1

    for analysis_metric_key, key in enumerate(sorted(policies), start=1):
        policy = policies[key]
        policy["analysis_metric_key"] = analysis_metric_key
        connection.execute(
            """
            INSERT INTO dim_analysis_metric VALUES (
                :analysis_metric_key, :indicator_id, :metric_id, :analysis_label,
                :analysis_class, :primary_metric, :comparison_basis,
                :rank_direction, :change_method, :stable_threshold,
                :benchmark_method, :display_precision, :interpretation_note,
                :policy_status
            )
            """,
            policy,
        )
    return policies, inferred_count


def _load_current_observations(
    connection: sqlite3.Connection,
    observations: list[dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    current_rows: list[dict[str, Any]] = []
    for key, observation in enumerate(observations, start=1):
        policy = policies[(observation["indicator_id"], observation["metric_id"])]
        time_index, period_end_date, _ = _period_attributes(observation)
        row = {
            "analysis_observation_key": key,
            "warehouse_observation_key": observation["observation_key"],
            "analysis_metric_key": policy["analysis_metric_key"],
            "indicator_id": observation["indicator_id"],
            "metric_id": observation["metric_id"],
            "source_id": observation["source_id"],
            "release_id": observation["release_id"],
            "source_vintage": observation["source_vintage"],
            "retrieved_at": observation["retrieved_at"],
            "raw_sha256": observation["raw_sha256"],
            "geography_basis": observation["geography_basis"],
            "geography_type": observation["geography_type"],
            "geography_code": observation["geography_code"],
            "geography_name": observation["geography_name"],
            "is_bay_area": observation["is_bay_area"],
            "period": observation["period"],
            "frequency": observation["frequency"],
            "period_type": observation["period_type"],
            "period_sort_key": observation["period_sort_key"],
            "time_index": time_index,
            "period_end_date": period_end_date,
            "subgroup": observation["subgroup"],
            "industry_code": observation["industry_code"],
            "adjustment": observation["adjustment"],
            "value": observation["value"],
            "unit": observation["unit"],
            "margin_of_error": observation["margin_of_error"],
            "value_status": observation["value_status"],
            "dataset_status": observation["dataset_status"],
            "notes": observation["notes"] or "",
        }
        connection.execute(
            """
            INSERT INTO current_observation (
                analysis_observation_key, warehouse_observation_key,
                analysis_metric_key, indicator_id, metric_id, source_id,
                release_id, source_vintage, retrieved_at, raw_sha256,
                geography_basis, geography_type, geography_code, geography_name,
                is_bay_area, period, frequency, period_type, period_sort_key, time_index,
                period_end_date, subgroup, industry_code, adjustment, value,
                unit, margin_of_error, value_status, dataset_status, notes
            ) VALUES (
                :analysis_observation_key, :warehouse_observation_key,
                :analysis_metric_key, :indicator_id, :metric_id, :source_id,
                :release_id, :source_vintage, :retrieved_at, :raw_sha256,
                :geography_basis, :geography_type, :geography_code, :geography_name,
                :is_bay_area, :period, :frequency, :period_type, :period_sort_key, :time_index,
                :period_end_date, :subgroup, :industry_code, :adjustment, :value,
                :unit, :margin_of_error, :value_status, :dataset_status, :notes
            )
            """,
            row,
        )
        current_rows.append(row)
    return current_rows


def _absolute_change(current: float | None, reference: float | None) -> float | None:
    if current is None or reference is None:
        return None
    return current - reference


def _percent_change(current: float | None, reference: float | None) -> float | None:
    if current is None or reference is None or reference == 0:
        return None
    return (current - reference) / reference * 100.0


def _load_trends(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    series: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        series[_series_key(row)].append(row)

    trends: dict[int, dict[str, Any]] = {}
    for series_rows in series.values():
        ordered = sorted(series_rows, key=lambda item: (item["time_index"], item["period_sort_key"]))
        by_index = {row["time_index"]: row for row in ordered}
        if len(by_index) != len(ordered):
            raise ValueError(f"A series contains duplicate analytical time indexes: {_series_key(ordered[0])}")
        for position, row in enumerate(ordered):
            policy = policies[(row["indicator_id"], row["metric_id"])]
            _, _, periods_per_year = _period_attributes(row)
            nearest = ordered[position - 1] if position else None
            previous = by_index.get(row["time_index"] - 1)
            year_ago = by_index.get(row["time_index"] - periods_per_year)
            period_gap = row["time_index"] - nearest["time_index"] if nearest else None
            period_absolute = _absolute_change(
                row["value"], previous["value"] if previous else None
            )
            year_absolute = _absolute_change(
                row["value"], year_ago["value"] if year_ago else None
            )
            period_percent = (
                _percent_change(row["value"], previous["value"] if previous else None)
                if policy["change_method"] == "percent"
                else None
            )
            year_percent = (
                _percent_change(row["value"], year_ago["value"] if year_ago else None)
                if policy["change_method"] == "percent"
                else None
            )
            rolling_rows = [by_index.get(row["time_index"] - offset) for offset in (0, 1, 2)]
            rolling_values = [item["value"] for item in rolling_rows if item and item["value"] is not None]
            rolling_3 = (
                sum(rolling_values) / 3.0
                if len(rolling_values) == 3 and all(rolling_rows)
                else None
            )
            trend = {
                "analysis_observation_key": row["analysis_observation_key"],
                "previous_period": nearest["period"] if nearest else None,
                "previous_value": nearest["value"] if nearest else None,
                "period_gap": period_gap,
                "period_change_absolute": period_absolute,
                "period_change_percent": period_percent,
                "year_ago_period": year_ago["period"] if year_ago else None,
                "year_ago_value": year_ago["value"] if year_ago else None,
                "year_change_absolute": year_absolute,
                "year_change_percent": year_percent,
                "rolling_3_value": rolling_3,
                "change_method": policy["change_method"],
            }
            connection.execute(
                """
                INSERT INTO fact_metric_trend VALUES (
                    :analysis_observation_key, :previous_period, :previous_value,
                    :period_gap, :period_change_absolute, :period_change_percent,
                    :year_ago_period, :year_ago_value, :year_change_absolute,
                    :year_change_percent, :rolling_3_value, :change_method
                )
                """,
                trend,
            )
            trends[row["analysis_observation_key"]] = trend
    return trends


def _rank(values: list[float], value: float, direction: str) -> int | None:
    if len(values) < 2:
        return None
    if direction == "higher_is_better":
        return 1 + sum(candidate > value for candidate in values)
    if direction == "lower_is_better":
        return 1 + sum(candidate < value for candidate in values)
    return None


def _load_benchmarks(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, Any]],
    trends: dict[int, dict[str, Any]],
) -> int:
    groups: dict[tuple[Any, ...], list[tuple[dict[str, Any], float, str]]] = defaultdict(list)
    for row in rows:
        policy = policies[(row["indicator_id"], row["metric_id"])]
        basis = policy["comparison_basis"]
        if row["geography_type"] != "county" or not row["is_bay_area"] or basis == "none":
            continue
        if basis == "level":
            comparison_value = row["value"]
            comparison_unit = row["unit"]
        else:
            trend = trends[row["analysis_observation_key"]]
            if policy["change_method"] == "percentage_point":
                comparison_value = trend["year_change_absolute"]
                comparison_unit = "percentage points"
            else:
                comparison_value = trend["year_change_percent"]
                comparison_unit = "percent"
        if comparison_value is None:
            continue
        group_key = (
            row["analysis_metric_key"],
            row["source_id"],
            row["period"],
            row["frequency"],
            row["subgroup"],
            row["industry_code"],
            row["adjustment"],
        )
        groups[group_key].append((row, float(comparison_value), comparison_unit))

    benchmark_count = 0
    for members in groups.values():
        values = [member[1] for member in members]
        median_value = float(statistics.median(values))
        minimum, maximum = min(values), max(values)
        for row, value, unit in members:
            policy = policies[(row["indicator_id"], row["metric_id"])]
            county_rank = _rank(values, value, policy["rank_direction"])
            percentile = (
                (len(values) - county_rank) / (len(values) - 1) * 100.0
                if county_rank is not None and len(values) > 1
                else None
            )
            benchmark = {
                "analysis_observation_key": row["analysis_observation_key"],
                "comparison_basis": policy["comparison_basis"],
                "comparison_value": value,
                "comparison_unit": unit,
                "county_count": len(values),
                "county_coverage_pct": len(values) / 9.0 * 100.0,
                "benchmark_method": policy["benchmark_method"],
                "benchmark_value": median_value,
                "minimum_value": minimum,
                "maximum_value": maximum,
                "gap_to_benchmark": value - median_value,
                "county_rank": county_rank,
                "percentile": percentile,
            }
            connection.execute(
                """
                INSERT INTO fact_county_benchmark VALUES (
                    :analysis_observation_key, :comparison_basis,
                    :comparison_value, :comparison_unit, :county_count,
                    :county_coverage_pct, :benchmark_method, :benchmark_value,
                    :minimum_value, :maximum_value, :gap_to_benchmark,
                    :county_rank, :percentile
                )
                """,
                benchmark,
            )
            benchmark_count += 1
    return benchmark_count


def _age_months(period_end_date: str, as_of: date) -> int | None:
    if not period_end_date:
        return None
    end = date.fromisoformat(period_end_date)
    if end >= as_of:
        return 0
    months = (as_of.year - end.year) * 12 + as_of.month - end.month
    if as_of.day < end.day:
        months -= 1
    return max(months, 0)


def _recency_status(frequency: str, age_months: int | None) -> str:
    if age_months is None:
        return "unknown"
    thresholds = {
        "monthly": (2, 4),
        "quarterly": (8, 12),
        "annual": (24, 36),
    }
    if frequency not in thresholds:
        return "unknown"
    current, delayed = thresholds[frequency]
    if age_months <= current:
        return "current"
    if age_months <= delayed:
        return "delayed"
    return "stale"


def _trend_signal(change: float | None, policy: dict[str, Any]) -> str:
    if change is None:
        return "insufficient_history"
    if abs(change) <= policy["stable_threshold"]:
        return "stable"
    direction = policy["rank_direction"]
    if direction == "higher_is_better":
        return "improving" if change > 0 else "worsening"
    if direction == "lower_is_better":
        return "improving" if change < 0 else "worsening"
    return "rising" if change > 0 else "falling"


def _load_latest_signals(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, Any]],
    trends: dict[int, dict[str, Any]],
    as_of: date,
) -> int:
    series: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        series[_series_key(row)].append(row)
    latest = [
        max(series_rows, key=lambda item: (item["time_index"], item["retrieved_at"]))
        for series_rows in series.values()
    ]
    for signal_key, row in enumerate(sorted(latest, key=_series_key), start=1):
        trend = trends[row["analysis_observation_key"]]
        policy = policies[(row["indicator_id"], row["metric_id"])]
        if policy["change_method"] == "percentage_point":
            year_change = trend["year_change_absolute"]
            period_change = trend["period_change_absolute"]
            change_unit = "percentage points"
        else:
            year_change = trend["year_change_percent"]
            period_change = trend["period_change_percent"]
            change_unit = "percent"
        if year_change is not None:
            signal_basis, signal_change = "year_over_year", year_change
        elif period_change is not None:
            signal_basis, signal_change = "period_over_period", period_change
        else:
            signal_basis, signal_change = "insufficient_history", None
        age = _age_months(row["period_end_date"], as_of)
        signal = {
            "signal_key": signal_key,
            "analysis_observation_key": row["analysis_observation_key"],
            "as_of_date": as_of.isoformat(),
            "age_months": age,
            "recency_status": _recency_status(row["frequency"], age),
            "trend_signal": _trend_signal(signal_change, policy),
            "signal_basis": signal_basis,
            "signal_change": signal_change,
            "signal_change_unit": change_unit,
            "interpretation_caution": (
                "Mechanical directional signal, not a causal claim or statistical-significance test. "
                + policy["interpretation_note"]
            ),
        }
        connection.execute(
            """
            INSERT INTO fact_latest_signal VALUES (
                :signal_key, :analysis_observation_key, :as_of_date,
                :age_months, :recency_status, :trend_signal, :signal_basis,
                :signal_change, :signal_change_unit, :interpretation_caution
            )
            """,
            signal,
        )
    return len(latest)


def _load_readiness(
    connection: sqlite3.Connection,
    catalog: list[dict[str, Any]],
    policies: dict[tuple[str, str], dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    policies_by_indicator: dict[str, set[str]] = defaultdict(set)
    for indicator_id, metric_id in policies:
        policies_by_indicator[indicator_id].add(metric_id)
    observations_by_indicator: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        observations_by_indicator[row["indicator_id"]].append(row)

    for indicator in catalog:
        indicator_rows = observations_by_indicator.get(indicator["indicator_id"], [])
        metric_count = len(policies_by_indicator.get(indicator["indicator_id"], set()))
        observed_metrics = len({row["metric_id"] for row in indicator_rows})
        geography_count = len(
            {(row["geography_type"], row["geography_code"]) for row in indicator_rows}
        )
        first = min(indicator_rows, key=lambda row: row["period_sort_key"]) if indicator_rows else None
        latest = max(indicator_rows, key=lambda row: row["period_sort_key"]) if indicator_rows else None
        if indicator_rows:
            status = "active"
            note = "Validated current observations are available in the analytical layer."
        elif metric_count:
            status = "model_ready"
            note = "Analytical metric policies are configured; no current observations are loaded."
        else:
            status = "planned"
            note = "Indicator remains in the catalog but requires metric and source activation."
        connection.execute(
            """
            INSERT INTO indicator_readiness VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                indicator["indicator_id"],
                indicator["pillar"],
                indicator["indicator_name"],
                indicator["priority"],
                indicator["phase4_pilot"],
                indicator["planned_frequency"],
                metric_count,
                observed_metrics,
                len(indicator_rows),
                geography_count,
                first["period"] if first else None,
                latest["period"] if latest else None,
                status,
                note,
            ),
        )


def _quality_checks(
    connection: sqlite3.Connection,
    *,
    source_integrity: str,
    source_foreign_key_violations: int,
    expected_current_count: int,
    expected_signal_count: int,
    include_fixtures: bool,
) -> list[dict[str, Any]]:
    current_count = connection.execute("SELECT COUNT(*) FROM current_observation").fetchone()[0]
    trend_count = connection.execute("SELECT COUNT(*) FROM fact_metric_trend").fetchone()[0]
    signal_count = connection.execute("SELECT COUNT(*) FROM fact_latest_signal").fetchone()[0]
    duplicate_count = connection.execute(
        """
        SELECT COALESCE(SUM(row_count - 1), 0)
        FROM (
            SELECT COUNT(*) row_count
            FROM current_observation
            GROUP BY analysis_metric_key, source_id, geography_type, geography_code,
                     period, frequency, subgroup, industry_code, adjustment
            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    observed_without_policy = connection.execute(
        """
        SELECT COUNT(*)
        FROM current_observation AS o
        LEFT JOIN dim_analysis_metric AS m USING (analysis_metric_key)
        WHERE m.analysis_metric_key IS NULL
        """
    ).fetchone()[0]
    fixture_count = connection.execute(
        "SELECT COUNT(*) FROM current_observation WHERE dataset_status = 'fixture'"
    ).fetchone()[0]
    readiness_count = connection.execute("SELECT COUNT(*) FROM indicator_readiness").fetchone()[0]
    education_ready = connection.execute(
        """
        SELECT COUNT(*) FROM indicator_readiness
        WHERE indicator_id IN ('E1', 'E2', 'E3', 'E4')
          AND readiness_status IN ('active', 'model_ready')
        """
    ).fetchone()[0]
    provenance_errors = connection.execute(
        """
        SELECT COUNT(*) FROM current_observation
        WHERE release_id = '' OR retrieved_at = '' OR raw_sha256 = ''
        """
    ).fetchone()[0]
    benchmark_errors = connection.execute(
        """
        SELECT COUNT(*) FROM fact_county_benchmark
        WHERE county_count < 1
           OR county_coverage_pct <= 0 OR county_coverage_pct > 100
           OR (county_rank IS NOT NULL AND (county_rank < 1 OR county_rank > county_count))
           OR (percentile IS NOT NULL AND (percentile < 0 OR percentile > 100))
        """
    ).fetchone()[0]
    analysis_foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    checks = [
        {
            "check_name": "source_database_integrity",
            "passed": int(source_integrity == "ok"),
            "affected_count": int(source_integrity != "ok"),
            "message": "The Phase 5 source warehouse must pass SQLite integrity_check.",
        },
        {
            "check_name": "source_foreign_key_integrity",
            "passed": int(source_foreign_key_violations == 0),
            "affected_count": source_foreign_key_violations,
            "message": "The Phase 5 source warehouse must have no foreign-key violations.",
        },
        {
            "check_name": "current_observation_count",
            "passed": int(current_count == expected_current_count),
            "affected_count": abs(current_count - expected_current_count),
            "message": "Every resolved current source observation must load once.",
        },
        {
            "check_name": "unique_current_grain",
            "passed": int(duplicate_count == 0),
            "affected_count": duplicate_count,
            "message": "Current observations must be unique across metric, source, geography, period, and analytic slice.",
        },
        {
            "check_name": "metric_policy_coverage",
            "passed": int(observed_without_policy == 0),
            "affected_count": observed_without_policy,
            "message": "Every observed metric must resolve to a configured or safe inferred analytical policy.",
        },
        {
            "check_name": "trend_completeness",
            "passed": int(trend_count == current_count),
            "affected_count": abs(trend_count - current_count),
            "message": "Every current observation must have one trend record, including insufficient-history states.",
        },
        {
            "check_name": "latest_signal_completeness",
            "passed": int(signal_count == expected_signal_count),
            "affected_count": abs(signal_count - expected_signal_count),
            "message": "Every analytical series must have exactly one latest signal.",
        },
        {
            "check_name": "benchmark_integrity",
            "passed": int(benchmark_errors == 0),
            "affected_count": benchmark_errors,
            "message": "County benchmarks, ranks, coverage, and percentiles must remain internally valid.",
        },
        {
            "check_name": "fixture_exclusion",
            "passed": int(include_fixtures or fixture_count == 0),
            "affected_count": 0 if include_fixtures else fixture_count,
            "message": "Fixture observations are excluded unless explicitly enabled for tests.",
        },
        {
            "check_name": "indicator_readiness_completeness",
            "passed": int(readiness_count == 25),
            "affected_count": abs(readiness_count - 25),
            "message": "Readiness status must cover all 25 catalog indicators.",
        },
        {
            "check_name": "education_model_readiness",
            "passed": int(education_ready == 4),
            "affected_count": abs(education_ready - 4),
            "message": "E1 through E4 must be active or model-ready without fabricated facts.",
        },
        {
            "check_name": "current_provenance",
            "passed": int(provenance_errors == 0),
            "affected_count": provenance_errors,
            "message": "Every analytical observation must retain release, retrieval, and raw-hash provenance.",
        },
        {
            "check_name": "analysis_foreign_key_integrity",
            "passed": int(analysis_foreign_keys == 0),
            "affected_count": analysis_foreign_keys,
            "message": "All analytical fact foreign keys must resolve.",
        },
    ]
    connection.executemany(
        "INSERT INTO analysis_quality_checks VALUES (:check_name, :passed, :affected_count, :message)",
        checks,
    )
    return checks


def build_analysis(
    warehouse_path: Path,
    analysis_path: Path,
    *,
    as_of: str | date | None = None,
    include_fixtures: bool = False,
    schema_path: Path = SCHEMA_PATH,
    policy_path: Path = POLICY_PATH,
) -> dict[str, Any]:
    warehouse_path = warehouse_path.resolve()
    analysis_path = analysis_path.resolve()
    effective_date = _parse_as_of(as_of)
    if not warehouse_path.is_file():
        raise FileNotFoundError(f"Phase 5 warehouse not found: {warehouse_path}")
    if not schema_path.is_file():
        raise FileNotFoundError(f"Phase 7 schema not found: {schema_path}")
    if not policy_path.is_file():
        raise FileNotFoundError(f"Phase 7 metric policy not found: {policy_path}")

    with sqlite3.connect(warehouse_path) as warehouse:
        _required_warehouse_objects(warehouse)
        warehouse_metadata = dict(
            warehouse.execute("SELECT metadata_key, metadata_value FROM warehouse_metadata")
        )
        source_schema_version = str(warehouse_metadata.get("schema_version", ""))
        if not source_schema_version.startswith("5."):
            raise ValueError(f"Expected a Phase 5 warehouse; found {source_schema_version!r}")
        source_integrity = warehouse.execute("PRAGMA integrity_check").fetchone()[0]
        source_foreign_keys = len(warehouse.execute("PRAGMA foreign_key_check").fetchall())
        source_observation_count, observations = _source_observations(
            warehouse, include_fixtures=include_fixtures
        )
        catalog = _rows(
            warehouse,
            """
            SELECT
                indicator_id, pillar, indicator_name, priority,
                phase4_pilot, planned_frequency
            FROM dim_indicator ORDER BY indicator_id
            """,
        )
    if len(catalog) != 25:
        raise ValueError(f"Expected 25 catalog indicators; found {len(catalog)}")

    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix=f"{analysis_path.name}.",
        suffix=".tmp",
        dir=analysis_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    started_at = _utc_now()
    warehouse_hash = _sha256_file(warehouse_path)
    try:
        with sqlite3.connect(temporary_path) as analysis:
            analysis.execute("PRAGMA foreign_keys = ON")
            analysis.execute("PRAGMA journal_mode = DELETE")
            analysis.executescript(schema_path.read_text(encoding="utf-8"))
            metadata = {
                "project": "The Bay Outlook",
                "phase": "7",
                "schema_version": SCHEMA_VERSION,
                "as_of_date": effective_date.isoformat(),
                "source_warehouse_sha256": warehouse_hash,
                "built_at": started_at,
                "analytical_grain": (
                    "current validated source observation with derived trends, comparable county "
                    "benchmarks, and one latest signal per analytical series"
                ),
                "benchmark_definition": "unweighted median across available Bay Area counties",
                "signal_caution": "mechanical directional classification; not causal or significance tested",
            }
            analysis.executemany(
                "INSERT INTO analysis_metadata VALUES (?, ?)", sorted(metadata.items())
            )
            build_key = analysis.execute(
                """
                INSERT INTO analysis_builds (
                    started_at, as_of_date, source_warehouse,
                    source_warehouse_sha256, source_schema_version,
                    include_fixtures, source_observation_count, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    started_at,
                    effective_date.isoformat(),
                    _portable_project_path(warehouse_path),
                    warehouse_hash,
                    source_schema_version,
                    int(include_fixtures),
                    source_observation_count,
                ),
            ).lastrowid
            policies, inferred_count = _load_metric_policies(
                analysis, observations, catalog, policy_path
            )
            current_rows = _load_current_observations(analysis, observations, policies)
            trends = _load_trends(analysis, current_rows, policies)
            _load_benchmarks(analysis, current_rows, policies, trends)
            signal_count = _load_latest_signals(
                analysis, current_rows, policies, trends, effective_date
            )
            _load_readiness(analysis, catalog, policies, current_rows)
            checks = _quality_checks(
                analysis,
                source_integrity=source_integrity,
                source_foreign_key_violations=source_foreign_keys,
                expected_current_count=len(observations),
                expected_signal_count=signal_count,
                include_fixtures=include_fixtures,
            )
            failed = [check for check in checks if not check["passed"]]
            if failed:
                raise ValueError(f"Phase 7 analytical quality checks failed: {failed}")
            completed_at = _utc_now()
            analysis.execute(
                """
                UPDATE analysis_builds
                SET completed_at = ?, current_observation_count = ?, status = 'passed'
                WHERE build_key = ?
                """,
                (completed_at, len(current_rows), build_key),
            )
            analysis.execute(
                "INSERT INTO analysis_metadata VALUES ('inferred_policy_count', ?)",
                (str(inferred_count),),
            )
            analysis.commit()
        os.replace(temporary_path, analysis_path)
        os.chmod(analysis_path, 0o644)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return analysis_summary(analysis_path)


def analysis_summary(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        metadata = dict(
            connection.execute("SELECT metadata_key, metadata_value FROM analysis_metadata")
        )
        counts = {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "dim_analysis_metric",
                "current_observation",
                "fact_metric_trend",
                "fact_county_benchmark",
                "fact_latest_signal",
                "indicator_readiness",
            )
        }
        failed_checks = connection.execute(
            "SELECT COUNT(*) FROM analysis_quality_checks WHERE passed = 0"
        ).fetchone()[0]
        recency = {
            status: count
            for status, count in connection.execute(
                "SELECT recency_status, COUNT(*) FROM fact_latest_signal GROUP BY recency_status"
            )
        }
        signals = {
            status: count
            for status, count in connection.execute(
                "SELECT trend_signal, COUNT(*) FROM fact_latest_signal GROUP BY trend_signal"
            )
        }
        active_indicators = [
            row[0]
            for row in connection.execute(
                "SELECT indicator_id FROM indicator_readiness WHERE readiness_status = 'active' ORDER BY indicator_id"
            )
        ]
        education_readiness = [
            {"indicator_id": row[0], "readiness_status": row[1], "observation_count": row[2]}
            for row in connection.execute(
                """
                SELECT indicator_id, readiness_status, current_observation_count
                FROM indicator_readiness WHERE indicator_id GLOB 'E[0-9]*'
                ORDER BY indicator_id
                """
            )
        ]
    return {
        "database": str(path),
        "schema_version": metadata.get("schema_version"),
        "as_of_date": metadata.get("as_of_date"),
        "analytical_grain": metadata.get("analytical_grain"),
        "table_counts": counts,
        "headline_snapshot_count": _view_count(path, "vw_headline_snapshot"),
        "education_snapshot_count": _view_count(path, "vw_education_snapshot"),
        "failed_quality_check_count": failed_checks,
        "inferred_policy_count": int(metadata.get("inferred_policy_count", "0")),
        "active_indicators": active_indicators,
        "recency_status_counts": recency,
        "trend_signal_counts": signals,
        "education_readiness": education_readiness,
    }


def _view_count(path: Path, view: str) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0])


def _write_query_csv(connection: sqlite3.Connection, query: str, path: Path) -> int:
    cursor = connection.execute(query)
    fields = [column[0] for column in cursor.description]
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        for row in cursor:
            writer.writerow(row)
            count += 1
    os.replace(temporary, path)
    return count


def export_analysis(path: Path, output_dir: Path) -> dict[str, Any]:
    path = path.resolve()
    output_dir = output_dir.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Phase 7 analytical database not found: {path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    exports: dict[str, dict[str, Any]] = {}
    with sqlite3.connect(path) as connection:
        for filename, (view, order_by) in EXPORT_VIEWS.items():
            destination = output_dir / filename
            count = _write_query_csv(
                connection,
                f"SELECT * FROM {view} ORDER BY {order_by}",
                destination,
            )
            exports[filename] = {"path": str(destination), "row_count": count}
    manifest = {
        "generated_at": _utc_now(),
        "analysis_database": str(path),
        "analysis_database_sha256": _sha256_file(path),
        "summary": analysis_summary(path),
        "exports": exports,
    }
    manifest_path = output_dir / "analysis_manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    return {"output_dir": str(output_dir), "exports": exports, "manifest": str(manifest_path)}


def print_analysis_summary(path: Path) -> None:
    print(json.dumps(analysis_summary(path), indent=2, sort_keys=True))
