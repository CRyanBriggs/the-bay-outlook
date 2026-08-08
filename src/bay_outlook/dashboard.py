from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import PROJECT_ROOT


SCHEMA_VERSION = "8.0.0"
TEMPLATE_PATH = PROJECT_ROOT / "dashboard" / "index.template.html"
STYLE_PATH = PROJECT_ROOT / "dashboard" / "styles.css"
SCRIPT_PATH = PROJECT_ROOT / "dashboard" / "app.js"
COUNTY_PATH = PROJECT_ROOT / "config" / "counties.csv"
REQUIRED_ANALYSIS_OBJECTS = {
    "analysis_metadata",
    "analysis_quality_checks",
    "current_observation",
    "dim_analysis_metric",
    "indicator_readiness",
    "vw_analytical_coverage",
    "vw_county_time_series",
    "vw_latest_county_snapshot",
}
SOURCE_LABELS = {
    "BLS_LAUS": "BLS Local Area Unemployment Statistics",
    "BLS_QCEW": "BLS Quarterly Census of Employment and Wages",
    "BEA_CAGDP1": "BEA County GDP",
    "CENSUS_ACS5": "Census American Community Survey",
    "CDE_CGR12": "California Department of Education college-going rate",
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


def _rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, tuple(parameters))
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _require_analysis_contract(connection: sqlite3.Connection) -> dict[str, str]:
    objects = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    missing = REQUIRED_ANALYSIS_OBJECTS - objects
    if missing:
        raise ValueError(f"Phase 7 analysis database is missing objects: {sorted(missing)}")
    metadata = dict(connection.execute("SELECT metadata_key, metadata_value FROM analysis_metadata"))
    schema_version = str(metadata.get("schema_version", ""))
    if not schema_version.startswith("7."):
        raise ValueError(f"Expected a Phase 7 analysis database; found {schema_version!r}")
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"Phase 7 analysis database integrity check failed: {integrity}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise ValueError(f"Phase 7 analysis database has {len(foreign_keys)} foreign-key violations")
    failed_checks = connection.execute(
        "SELECT COUNT(*) FROM analysis_quality_checks WHERE passed = 0"
    ).fetchone()[0]
    if failed_checks:
        raise ValueError(f"Phase 7 analysis database has {failed_checks} failed quality checks")
    return metadata


def _configured_counties() -> list[dict[str, str]]:
    with COUNTY_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {"countyCode": row["county_fips"], "countyName": row["county_name"]}
        for row in rows
        if row["region_member"] == "1"
    ]


def _median(values: Iterable[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return float(statistics.median(present)) if present else None


def _metric_rows(rows: list[dict[str, Any]], metric_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if row["metricId"] == metric_id]


def _overview(
    latest: list[dict[str, Any]],
    readiness: list[dict[str, Any]],
    current_observation_count: int,
) -> dict[str, Any]:
    unemployment = _metric_rows(latest, "unemployment_rate")
    gdp = _metric_rows(latest, "real_gdp")
    qcew = _metric_rows(latest, "average_monthly_covered_employment")
    education = [row for row in readiness if row["indicatorId"].startswith("E")]
    readiness_counts = Counter(row["readinessStatus"] for row in readiness)
    recency_counts = Counter(row["recencyStatus"] for row in latest)
    signal_counts = Counter(row["trendSignal"] for row in latest)
    return {
        "currentObservationCount": current_observation_count,
        "activeIndicatorCount": readiness_counts.get("active", 0),
        "modelReadyIndicatorCount": readiness_counts.get("model_ready", 0),
        "plannedIndicatorCount": readiness_counts.get("planned", 0),
        "totalIndicatorCount": len(readiness),
        "latestSeriesCount": len(latest),
        "recencyCounts": dict(sorted(recency_counts.items())),
        "signalCounts": dict(sorted(signal_counts.items())),
        "unemployment": {
            "medianLevel": _median(row["value"] for row in unemployment),
            "medianYearChange": _median(row["yearChangeAbsolute"] for row in unemployment),
            "improvingCount": sum(row["trendSignal"] == "improving" for row in unemployment),
            "countyCount": len(unemployment),
            "period": unemployment[0]["period"] if unemployment else None,
            "recencyStatus": unemployment[0]["recencyStatus"] if unemployment else "unknown",
        },
        "gdp": {
            "medianGrowth": _median(row["yearChangePercent"] for row in gdp),
            "positiveCount": sum(
                row["yearChangePercent"] is not None and row["yearChangePercent"] > 0
                for row in gdp
            ),
            "countyCount": len(gdp),
            "period": gdp[0]["period"] if gdp else None,
            "recencyStatus": gdp[0]["recencyStatus"] if gdp else "unknown",
        },
        "coveredEmployment": {
            "historyReadyCount": sum(row["yearChangePercent"] is not None for row in qcew),
            "countyCount": len(qcew),
            "period": qcew[0]["period"] if qcew else None,
            "recencyStatus": qcew[0]["recencyStatus"] if qcew else "unknown",
        },
        "education": {
            "activeCount": sum(row["readinessStatus"] == "active" for row in education),
            "modelReadyCount": sum(row["readinessStatus"] == "model_ready" for row in education),
            "observationCount": sum(row["currentObservationCount"] for row in education),
            "indicatorCount": len(education),
        },
    }


def _source_summary(latest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in latest:
        grouped[row["sourceId"]].append(row)
    result = []
    for source_id, rows in sorted(grouped.items()):
        newest = max(rows, key=lambda row: (row["periodSortKey"], row["period"]))
        result.append(
            {
                "sourceId": source_id,
                "sourceLabel": SOURCE_LABELS.get(source_id, source_id.replace("_", " ")),
                "geographyBasis": newest["geographyBasis"],
                "latestPeriod": newest["period"],
                "publicationDate": newest.get("publicationDate"),
                "retrievedAt": newest.get("retrievedAt"),
                "releaseStatus": newest.get("releaseStatus"),
                "nextExpectedUpdate": newest.get("nextExpectedUpdate"),
                "seriesCount": len(rows),
                "recencyCounts": dict(
                    sorted(Counter(row["recencyStatus"] for row in rows).items())
                ),
            }
        )
    return result


def _optional_release_metadata(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dim_source_release'"
        ).fetchone()
        if not exists:
            return {}
        return {
            row["release_id"]: dict(row)
            for row in connection.execute(
                """
                SELECT release_id, source_release_date, retrieved_at,
                       next_expected_release, source_vintage
                FROM dim_source_release
                """
            )
        }


def _optional_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _default_indicator_freshness(
    readiness: list[dict[str, Any]],
    latest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in latest:
        grouped[row["indicatorId"]].append(row)
    rows = []
    for indicator in readiness:
        candidates = grouped.get(indicator["indicatorId"], [])
        newest = max(
            candidates,
            key=lambda row: (row["periodSortKey"], row.get("retrievedAt") or ""),
        ) if candidates else None
        rows.append(
            {
                "indicatorId": indicator["indicatorId"],
                "indicatorName": indicator["indicatorName"],
                "sourceId": newest["sourceId"] if newest else None,
                "observationPeriod": newest["period"] if newest else None,
                "publicationDate": newest.get("publicationDate") if newest else None,
                "retrievalDate": newest.get("retrievedAt") if newest else None,
                "releaseStatus": newest.get("releaseStatus", "not_available") if newest else "not_available",
                "nextExpectedUpdate": newest.get("nextExpectedUpdate") if newest else None,
                "recencyStatus": newest.get("recencyStatus", "unknown") if newest else "unknown",
                "coverageStatus": "active" if newest else "planned",
                "manualReviewRequired": "Significant changes require named-human review",
            }
        )
    return rows


def _normalize_indicator_freshness(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "indicatorId": row.get("indicator_id"),
            "indicatorName": row.get("indicator_name"),
            "sourceId": row.get("source_id") or None,
            "observationPeriod": row.get("observation_period") or None,
            "publicationDate": row.get("publication_date") or None,
            "retrievalDate": row.get("retrieval_date") or None,
            "releaseStatus": row.get("release_status") or "not_available",
            "nextExpectedUpdate": row.get("next_expected_update") or None,
            "recencyStatus": row.get("recency_status") or "unknown",
            "coverageStatus": row.get("coverage_status") or "planned",
            "manualReviewRequired": row.get("manual_review_required") or "Named-human review required for significant changes",
        }
        for row in rows
    ]


def dashboard_payload(
    analysis_path: Path,
    *,
    allow_fixtures: bool = False,
    built_at: str | None = None,
    warehouse_path: Path | None = None,
    update_calendar_path: Path | None = None,
    indicator_freshness_path: Path | None = None,
) -> dict[str, Any]:
    analysis_path = analysis_path.resolve()
    if not analysis_path.is_file():
        raise FileNotFoundError(f"Phase 7 analysis database not found: {analysis_path}")
    with sqlite3.connect(analysis_path) as connection:
        metadata = _require_analysis_contract(connection)
        fixture_count = connection.execute(
            "SELECT COUNT(*) FROM current_observation WHERE dataset_status = 'fixture'"
        ).fetchone()[0]
        if fixture_count and not allow_fixtures:
            raise ValueError(
                f"Dashboard publication blocked: the analysis database contains {fixture_count} fixture rows"
            )
        current_observation_count = connection.execute(
            "SELECT COUNT(*) FROM current_observation"
        ).fetchone()[0]
        latest = _rows(
            connection,
            """
            SELECT
                indicator_id AS indicatorId,
                metric_id AS metricId,
                analysis_label AS metricLabel,
                primary_metric AS primaryMetric,
                analysis_class AS analysisClass,
                geography_code AS countyCode,
                geography_name AS countyName,
                period,
                frequency,
                period_sort_key AS periodSortKey,
                period_end_date AS periodEndDate,
                value,
                unit,
                margin_of_error AS marginOfError,
                value_status AS valueStatus,
                source_id AS sourceId,
                release_id AS releaseId,
                source_vintage AS sourceVintage,
                retrieved_at AS retrievedAt,
                geography_basis AS geographyBasis,
                adjustment,
                notes,
                comparison_basis AS comparisonBasis,
                rank_direction AS rankDirection,
                change_method AS changeMethod,
                display_precision AS displayPrecision,
                interpretation_note AS interpretationNote,
                policy_status AS policyStatus,
                previous_period AS previousPeriod,
                previous_value AS previousValue,
                period_change_absolute AS periodChangeAbsolute,
                period_change_percent AS periodChangePercent,
                year_ago_period AS yearAgoPeriod,
                year_ago_value AS yearAgoValue,
                year_change_absolute AS yearChangeAbsolute,
                year_change_percent AS yearChangePercent,
                rolling_3_value AS rolling3Value,
                comparison_value AS comparisonValue,
                comparison_unit AS comparisonUnit,
                benchmark_county_count AS benchmarkCountyCount,
                county_coverage_pct AS countyCoveragePct,
                benchmark_value AS benchmarkValue,
                gap_to_benchmark AS gapToBenchmark,
                county_rank AS countyRank,
                percentile,
                as_of_date AS asOfDate,
                age_months AS ageMonths,
                recency_status AS recencyStatus,
                trend_signal AS trendSignal,
                signal_basis AS signalBasis,
                signal_change AS signalChange,
                signal_change_unit AS signalChangeUnit,
                interpretation_caution AS interpretationCaution
            FROM vw_latest_county_snapshot
            ORDER BY indicator_id, metric_id, geography_code
            """,
        )
        series = _rows(
            connection,
            """
            SELECT
                indicator_id AS indicatorId,
                metric_id AS metricId,
                analysis_label AS metricLabel,
                primary_metric AS primaryMetric,
                analysis_class AS analysisClass,
                geography_code AS countyCode,
                geography_name AS countyName,
                period,
                frequency,
                period_sort_key AS periodSortKey,
                period_end_date AS periodEndDate,
                value,
                unit,
                value_status AS valueStatus,
                source_id AS sourceId,
                release_id AS releaseId,
                source_vintage AS sourceVintage,
                retrieved_at AS retrievedAt,
                geography_basis AS geographyBasis,
                adjustment,
                previous_period AS previousPeriod,
                previous_value AS previousValue,
                period_change_absolute AS periodChangeAbsolute,
                period_change_percent AS periodChangePercent,
                year_ago_period AS yearAgoPeriod,
                year_ago_value AS yearAgoValue,
                year_change_absolute AS yearChangeAbsolute,
                year_change_percent AS yearChangePercent,
                rolling_3_value AS rolling3Value,
                comparison_value AS comparisonValue,
                comparison_unit AS comparisonUnit,
                benchmark_value AS benchmarkValue,
                county_rank AS countyRank,
                county_coverage_pct AS countyCoveragePct
            FROM vw_county_time_series
            ORDER BY indicator_id, metric_id, geography_code, period_sort_key
            """,
        )
        metrics = _rows(
            connection,
            """
            SELECT
                m.indicator_id AS indicatorId,
                m.metric_id AS metricId,
                m.analysis_label AS metricLabel,
                m.analysis_class AS analysisClass,
                m.primary_metric AS primaryMetric,
                m.comparison_basis AS comparisonBasis,
                m.rank_direction AS rankDirection,
                m.change_method AS changeMethod,
                m.stable_threshold AS stableThreshold,
                m.display_precision AS displayPrecision,
                m.interpretation_note AS interpretationNote,
                m.policy_status AS policyStatus,
                c.observation_count AS observationCount,
                c.geography_count AS geographyCount,
                c.first_period_sort_key AS firstPeriodSortKey,
                c.latest_period_sort_key AS latestPeriodSortKey,
                c.latest_series_count AS latestSeriesCount
            FROM dim_analysis_metric AS m
            LEFT JOIN vw_analytical_coverage AS c
              ON c.indicator_id = m.indicator_id AND c.metric_id = m.metric_id
            ORDER BY m.indicator_id, m.metric_id
            """,
        )
        readiness = _rows(
            connection,
            """
            SELECT
                indicator_id AS indicatorId,
                pillar,
                indicator_name AS indicatorName,
                priority,
                phase4_pilot AS phase4Pilot,
                planned_frequency AS plannedFrequency,
                configured_metric_count AS configuredMetricCount,
                observed_metric_count AS observedMetricCount,
                current_observation_count AS currentObservationCount,
                geography_count AS geographyCount,
                first_period AS firstPeriod,
                latest_period AS latestPeriod,
                readiness_status AS readinessStatus,
                readiness_note AS readinessNote
            FROM indicator_readiness
            ORDER BY pillar, indicator_id
            """,
        )
        quality_checks = _rows(
            connection,
            """
            SELECT
                check_name AS checkName,
                passed,
                affected_count AS affectedCount,
                message
            FROM analysis_quality_checks
            ORDER BY check_name
            """,
        )
    releases = _optional_release_metadata(warehouse_path)
    calendar_rows = _optional_csv(update_calendar_path)
    calendar_by_source = {row.get("source_id", ""): row for row in calendar_rows}
    for row in latest:
        release = releases.get(row.get("releaseId", ""), {})
        calendar = calendar_by_source.get(row.get("sourceId", ""), {})
        row["publicationDate"] = release.get("source_release_date") or None
        row["retrievedAt"] = release.get("retrieved_at") or row.get("retrievedAt")
        row["releaseStatus"] = (
            "preliminary" if row.get("valueStatus") == "preliminary" else
            "final" if row.get("valueStatus") in {"final", "missing", "suppressed"} else
            row.get("valueStatus", "unknown")
        )
        row["nextExpectedUpdate"] = (
            release.get("next_expected_release")
            or calendar.get("next_expected_update")
            or None
        )
    for row in series:
        release = releases.get(row.get("releaseId", ""), {})
        calendar = calendar_by_source.get(row.get("sourceId", ""), {})
        row["publicationDate"] = release.get("source_release_date") or None
        row["retrievedAt"] = release.get("retrieved_at") or row.get("retrievedAt")
        row["releaseStatus"] = (
            "preliminary" if row.get("valueStatus") == "preliminary" else
            "final" if row.get("valueStatus") in {"final", "missing", "suppressed"} else
            row.get("valueStatus", "unknown")
        )
        row["nextExpectedUpdate"] = (
            release.get("next_expected_release")
            or calendar.get("next_expected_update")
            or None
        )
    provided_freshness = _optional_csv(indicator_freshness_path)
    indicator_freshness = (
        _normalize_indicator_freshness(provided_freshness)
        if provided_freshness
        else _default_indicator_freshness(readiness, latest)
    )
    phase11_enhanced = bool(
        releases
        and calendar_rows
        and provided_freshness
        and len(indicator_freshness) == len(readiness) == 25
    )
    generated_at = built_at or _utc_now()
    return {
        "meta": {
            "project": "The Bay Outlook",
            "tagline": "Economic Research · Policy Analysis · Regional Futures",
            "dashboardSchemaVersion": SCHEMA_VERSION,
            "analysisSchemaVersion": metadata["schema_version"],
            "asOfDate": metadata["as_of_date"],
            "analysisBuiltAt": metadata["built_at"],
            "dashboardBuiltAt": generated_at,
            "benchmarkDefinition": metadata["benchmark_definition"],
            "signalCaution": metadata["signal_caution"],
            "allowFixtures": bool(allow_fixtures),
            "fixtureObservationCount": int(fixture_count),
            "phase11FreshnessEnhanced": phase11_enhanced,
        },
        "overview": _overview(latest, readiness, current_observation_count),
        "counties": _configured_counties(),
        "metrics": metrics,
        "latest": latest,
        "series": series,
        "readiness": readiness,
        "sources": _source_summary(latest),
        "updateCalendar": calendar_rows,
        "indicatorFreshness": indicator_freshness,
        "qualityChecks": quality_checks,
    }


def _read_dashboard_sources(
    template_path: Path,
    style_path: Path,
    script_path: Path,
) -> tuple[str, str, str]:
    missing = [path for path in (template_path, style_path, script_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Dashboard source file not found: {missing[0]}")
    template = template_path.read_text(encoding="utf-8")
    style = style_path.read_text(encoding="utf-8")
    script = script_path.read_text(encoding="utf-8")
    required = ("{{DASHBOARD_STYLE}}", "{{DASHBOARD_DATA}}", "{{DASHBOARD_SCRIPT}}")
    absent = [token for token in required if token not in template]
    if absent:
        raise ValueError(f"Dashboard template is missing tokens: {absent}")
    return template, style, script


def _render_dashboard(template: str, style: str, script: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (
        template.replace("{{DASHBOARD_STYLE}}", style)
        .replace("{{DASHBOARD_DATA}}", encoded)
        .replace("{{DASHBOARD_SCRIPT}}", script)
    )
    if "{{DASHBOARD_" in html:
        raise ValueError("Dashboard rendering left an unresolved template token")
    required_markers = (
        'id="dashboard-app"',
        'id="view-overview"',
        'id="view-county"',
        'id="view-education"',
        'id="view-health"',
        "window.BAY_OUTLOOK_DATA",
    )
    missing = [marker for marker in required_markers if marker not in html]
    if missing:
        raise ValueError(f"Rendered dashboard is missing required markers: {missing}")
    return html


def _safe_output_directory(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == Path(resolved.anchor) or resolved == PROJECT_ROOT.resolve():
        raise ValueError(f"Unsafe dashboard output directory: {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Dashboard output path is not a directory: {resolved}")
    return resolved


def _publish_directory(temporary: Path, destination: Path) -> None:
    backup: Path | None = None
    try:
        if destination.exists():
            backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
            os.replace(destination, backup)
        try:
            os.replace(temporary, destination)
        except Exception:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def build_dashboard(
    analysis_path: Path,
    output_dir: Path,
    *,
    allow_fixtures: bool = False,
    template_path: Path = TEMPLATE_PATH,
    style_path: Path = STYLE_PATH,
    script_path: Path = SCRIPT_PATH,
    warehouse_path: Path | None = None,
    update_calendar_path: Path | None = None,
    indicator_freshness_path: Path | None = None,
) -> dict[str, Any]:
    analysis_path = analysis_path.resolve()
    output_dir = _safe_output_directory(output_dir)
    built_at = _utc_now()
    payload = dashboard_payload(
        analysis_path,
        allow_fixtures=allow_fixtures,
        built_at=built_at,
        warehouse_path=warehouse_path,
        update_calendar_path=update_calendar_path,
        indicator_freshness_path=indicator_freshness_path,
    )
    template, style, script = _read_dashboard_sources(template_path, style_path, script_path)
    html = _render_dashboard(template, style, script, payload)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.build-", dir=output_dir.parent)
    )
    try:
        index_path = temporary / "index.html"
        index_path.write_text(html, encoding="utf-8")
        index_hash = _sha256_file(index_path)
        manifest = {
            "project": "The Bay Outlook",
            "phase": 8,
            "schema_version": SCHEMA_VERSION,
            "built_at": built_at,
            "as_of_date": payload["meta"]["asOfDate"],
            "source_analysis": _portable_project_path(analysis_path),
            "source_analysis_sha256": _sha256_file(analysis_path),
            "source_analysis_schema_version": payload["meta"]["analysisSchemaVersion"],
            "allow_fixtures": bool(allow_fixtures),
            "phase11_freshness_enhanced": payload["meta"]["phase11FreshnessEnhanced"],
            "output_file": "index.html",
            "output_sha256": index_hash,
            "counts": {
                "counties": len(payload["counties"]),
                "indicator_readiness": len(payload["readiness"]),
                "metric_policies": len(payload["metrics"]),
                "latest_series": len(payload["latest"]),
                "time_series_points": len(payload["series"]),
                "quality_checks": len(payload["qualityChecks"]),
                "indicator_freshness": len(payload["indicatorFreshness"]),
                "update_calendar": len(payload["updateCalendar"]),
            },
            "views": ["overview", "county", "education", "health"],
        }
        (temporary / "dashboard_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _publish_directory(temporary, output_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return dashboard_summary(output_dir)


def dashboard_summary(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    manifest_path = output_dir / "dashboard_manifest.json"
    index_path = output_dir / "index.html"
    if not manifest_path.is_file() or not index_path.is_file():
        raise FileNotFoundError(f"Phase 8 dashboard output is incomplete: {output_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_hash = _sha256_file(index_path)
    return {
        "dashboard": str(index_path),
        "manifest": str(manifest_path),
        "schema_version": manifest["schema_version"],
        "as_of_date": manifest["as_of_date"],
        "built_at": manifest["built_at"],
        "source_analysis_schema_version": manifest["source_analysis_schema_version"],
        "allow_fixtures": manifest["allow_fixtures"],
        "phase11_freshness_enhanced": bool(manifest.get("phase11_freshness_enhanced", False)),
        "counts": manifest["counts"],
        "views": manifest["views"],
        "output_sha256": actual_hash,
        "hash_matches_manifest": actual_hash == manifest["output_sha256"],
    }


def print_dashboard_summary(output_dir: Path) -> None:
    print(json.dumps(dashboard_summary(output_dir), indent=2, sort_keys=True))
