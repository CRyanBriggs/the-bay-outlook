from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import COUNTY_BY_FIPS, PROJECT_ROOT


PHASE14_PUBLIC_USE_VERSION = "1.5.0"

MODULES = (
    {
        "module_id": "observatory",
        "label": "Housing Observatory",
        "version": "1.1.0",
        "path": "data/phase14/public_use/inherited/housing-data-v1.1.json",
        "expected_observations": 3753,
        "expected_metrics": 29,
        "expected_snapshots": 35,
    },
    {
        "module_id": "access",
        "label": "Housing Access & Displacement",
        "version": "1.2.0",
        "path": "data/phase14/public_use/inherited/housing-access-data-v1.2.json",
        "expected_observations": 1116,
        "expected_metrics": 38,
        "expected_snapshots": 49,
    },
    {
        "module_id": "production",
        "label": "Housing Production & Policy",
        "version": "1.3.0",
        "path": "data/phase14/production/public/housing-production-policy-data.json",
        "expected_observations": 1575,
        "expected_metrics": 42,
        "expected_snapshots": 21,
    },
    {
        "module_id": "equity",
        "label": "Housing Equity & Economic Connections",
        "version": "1.4.0",
        "path": "data/phase14/equity/public/housing-equity-connections-data.json",
        "expected_observations": 3780,
        "expected_metrics": 30,
        "expected_snapshots": 76,
    },
)

PROFILE_MEASURES = (
    ("observatory", "rental_trends", "median_gross_rent", "Market"),
    ("observatory", "home_sales", "median_sale_price", "Market"),
    ("observatory", "affordability_ratios", "home_value_to_income_ratio", "Market"),
    ("observatory", "cost_burden", "renter_cost_burden_30_plus_pct", "Access"),
    (
        "access",
        "eviction_filings",
        "unlawful_detainer_filings_per_1000_renter_households",
        "Access",
    ),
    ("access", "displacement_pressure", "renter_severe_cost_burden_50_plus_pct", "Access"),
    ("access", "homelessness", "pit_overall_homeless", "Access"),
    (
        "access",
        "worker_housing_access",
        "median_worker_earnings_coverage_2br_pct",
        "Economic connection",
    ),
    ("production", "production_pipeline", "completed_units", "Production"),
    ("production", "rhna_delivery", "rhna_completion_progress_pct", "Production"),
    (
        "production",
        "permitting_timelines",
        "matched_application_to_completion_median_days",
        "Production",
    ),
    ("equity", "commuting_and_job_access", "worked_from_home_pct", "Economic connection"),
    ("equity", "commuting_and_job_access", "public_transport_commute_pct", "Economic connection"),
    ("equity", "commuting_and_job_access", "inbound_job_share_pct", "Economic connection"),
    ("equity", "commuting_and_job_access", "local_resident_job_share_pct", "Economic connection"),
)

COMPARISON_MEASURES = (
    ("observatory", "rental_trends", "median_gross_rent"),
    ("observatory", "home_sales", "median_sale_price"),
    ("observatory", "affordability_ratios", "home_value_to_income_ratio"),
    ("observatory", "cost_burden", "renter_cost_burden_30_plus_pct"),
    ("access", "eviction_filings", "unlawful_detainer_filings_per_1000_renter_households"),
    ("access", "homelessness", "pit_overall_homeless"),
    ("access", "worker_housing_access", "median_worker_earnings_coverage_2br_pct"),
    ("production", "production_pipeline", "completed_units"),
    ("production", "rhna_delivery", "rhna_completion_progress_pct"),
    ("production", "permitting_timelines", "timeline_match_rate_pct"),
    ("equity", "commuting_and_job_access", "worked_from_home_pct"),
    ("equity", "commuting_and_job_access", "inbound_job_share_pct"),
)

DERIVED_METRIC_IDS = {
    "home_value_to_income_ratio",
    "rent_to_income_pct",
    "housing_vacancy_rate",
    "owner_cost_burden_30_plus_pct",
    "owner_severe_cost_burden_50_plus_pct",
    "renter_cost_burden_30_plus_pct",
    "renter_severe_cost_burden_50_plus_pct",
    "rhna_progress_pct",
    "renter_overcrowding_pct",
    "renter_severe_overcrowding_pct",
    "unlawful_detainer_filings_per_1000_renter_households",
    "income_required_1br_fmr",
    "income_required_2br_fmr",
    "median_worker_earnings_coverage_1br_pct",
    "median_worker_earnings_coverage_2br_pct",
    "monthly_affordability_gap_1br",
    "monthly_affordability_gap_2br",
    "application_decision_approval_share_pct",
    "housing_element_compliance_share_pct",
    "matched_application_to_completion_median_days",
    "matched_application_to_entitlement_median_days",
    "matched_entitlement_to_permit_median_days",
    "matched_permit_to_completion_median_days",
    "timeline_match_rate_pct",
    "annual_completion_to_permit_flow_pct",
    "rhna_completion_gap_units",
    "rhna_completion_progress_pct",
    "rhna_completion_to_permit_flow_pct",
    "rhna_permit_gap_units",
    "rhna_permit_progress_pct",
    "lower_intensity_share_of_residential_zoning_pct",
    "mixed_use_share_of_mapped_zoning_pct",
    "multifamily_share_of_residential_zoning_pct",
    "homeownership_pct",
    "housing_cost_burden_over_30_pct",
    "housing_cost_burden_over_50_pct",
    "zero_vehicle_households_pct",
    "earnings_coverage_2br_fmr_pct",
    "monthly_gap_to_2br_fmr",
    "age_29_or_younger_job_share_pct",
    "age_30_to_54_job_share_pct",
    "age_55_plus_job_share_pct",
    "commute_30_to_59_minutes_pct",
    "commute_60_plus_minutes_pct",
    "commute_under_30_minutes_pct",
    "high_monthly_earnings_job_share_pct",
    "inbound_job_share_pct",
    "local_resident_job_share_pct",
    "low_monthly_earnings_job_share_pct",
    "middle_monthly_earnings_job_share_pct",
    "public_transport_commute_pct",
    "worked_from_home_pct",
}

OUTPUT_ROOT = PROJECT_ROOT / "data" / "phase14" / "public_use"
EXPORT_ROOT = OUTPUT_ROOT / "exports"
PUBLIC_ROOT = OUTPUT_ROOT / "public"
SITE_ROOT = PROJECT_ROOT / "site_v1_5"
SITE_DATA_ROOT = SITE_ROOT / "app" / "housing" / "explorer"
SITE_DOWNLOAD_ROOT = SITE_ROOT / "public" / "downloads" / "housing" / "v1.5"

REQUIRED_STATIC_FILES = (
    "README.md",
    ".github/workflows/housing-public-use-update.yml",
    "config/phase14_housing_public_use.json",
    "data/phase14/public_use/inherited/housing-data-v1.1.json",
    "data/phase14/public_use/inherited/housing-access-data-v1.2.json",
    "data/phase14/production/public/housing-production-policy-data.json",
    "data/phase14/equity/public/housing-equity-connections-data.json",
    "docs/housing-public-use/README.md",
    "docs/housing-public-use/METHODOLOGY.md",
    "docs/housing-public-use/DATA_DICTIONARY.md",
    "docs/housing-public-use/LIMITATIONS.md",
    "docs/housing-public-use/RUNBOOK.md",
    "src/bay_outlook/phase14_public_use.py",
    "src/bay_outlook/cli.py",
    "tests/test_phase14_public_use.py",
    "pyproject.toml",
)

OBSERVATION_FIELDS = (
    "module_id",
    "module_version",
    "domain",
    "measure_key",
    "metric_id",
    "metric_name",
    "county_fips",
    "county_name",
    "period",
    "period_end",
    "value",
    "unit",
    "margin_of_error_90",
    "subgroup_type",
    "subgroup_id",
    "subgroup_label",
    "tenure",
    "sex",
    "source_ids",
    "source_releases",
    "derivation",
    "formula",
    "comparability_status",
    "geography_basis",
    "universe",
    "notes",
)

CATALOG_FIELDS = (
    "measure_key",
    "module_id",
    "module_version",
    "module_label",
    "domain",
    "metric_id",
    "metric_name",
    "unit",
    "frequency",
    "derivation",
    "formula",
    "source_ids",
    "source_tier",
    "subgroup_dimension",
    "geography_basis",
    "history_start",
    "notes",
)

SOURCE_FIELDS = (
    "registry_key",
    "module_id",
    "module_version",
    "source_id",
    "publisher",
    "dataset",
    "source_tier",
    "source_class",
    "frequency",
    "geography_basis",
    "latest_period_end",
    "retrieved_at",
    "snapshot_count",
    "status",
    "landing_url",
)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_compact_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [item for item in str(value).split(";") if item]


def _measure_key(module_id: str, domain: str, metric_id: str) -> str:
    return f"{module_id}.{domain}.{metric_id}"


def _catalog_formula(row: dict[str, Any]) -> str:
    return str(row.get("calculation") or row.get("interpretation") or "published")


def _source_rows(module: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_sources = payload.get("sources") or payload.get("sourceRegistry") or []
    equity_counts: Counter[str] = Counter()
    if module["module_id"] == "equity":
        path = PROJECT_ROOT / "data/phase14/equity/exports/source_snapshots.csv"
        with path.open(newline="", encoding="utf-8") as stream:
            equity_counts.update(row["source_id"] for row in csv.DictReader(stream))
    rows = []
    for raw in raw_sources:
        source_id = str(raw.get("source_id") or raw.get("sourceId"))
        rows.append(
            {
                "registry_key": f"{module['module_id']}.{source_id}",
                "module_id": module["module_id"],
                "module_version": module["version"],
                "source_id": source_id,
                "publisher": raw.get("publisher", ""),
                "dataset": raw.get("dataset", ""),
                "source_tier": raw.get("source_tier", raw.get("sourceTier", "")),
                "source_class": raw.get("source_class", raw.get("sourceClass", "")),
                "frequency": raw.get("frequency", ""),
                "geography_basis": raw.get("geography_basis", raw.get("geographyBasis", "")),
                "latest_period_end": raw.get("latest_period_end", ""),
                "retrieved_at": raw.get("retrieved_at", payload.get("builtAt", "")),
                "snapshot_count": raw.get("snapshot_count", equity_counts.get(source_id, 0)),
                "status": raw.get("status", "accepted official release"),
                "landing_url": raw.get("landing_url", raw.get("landingUrl", "")),
            }
        )
    return rows


def _normalize_module(module: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _read_json(PROJECT_ROOT / module["path"])
    if payload.get("version") != module["version"]:
        raise ValueError(f"{module['module_id']} version mismatch: {payload.get('version')}")
    if payload.get("countyCount") != 9:
        raise ValueError(f"{module['module_id']} does not cover nine counties")
    if payload.get("observationCount") != module["expected_observations"]:
        raise ValueError(f"{module['module_id']} observation count changed")
    if payload.get("metricCount") != module["expected_metrics"]:
        raise ValueError(f"{module['module_id']} metric count changed")

    raw_catalog = payload.get("metricCatalog", [])
    catalog_lookup: dict[tuple[str, str], dict[str, Any]] = {
        (str(row["domain"]), str(row["metricId"])): row for row in raw_catalog
    }
    observed_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    observations: list[dict[str, Any]] = []
    for raw in payload.get("observations", []):
        domain = str(raw["domain"])
        metric_id = str(raw["metricId"])
        catalog = catalog_lookup[(domain, metric_id)]
        sources = _as_list(raw.get("sourceIds", raw.get("sourceId")))
        observed_sources[(domain, metric_id)].update(sources)
        formula = str(raw.get("calculation") or _catalog_formula(catalog))
        observations.append(
            {
                "module_id": module["module_id"],
                "module_version": module["version"],
                "domain": domain,
                "measure_key": _measure_key(module["module_id"], domain, metric_id),
                "metric_id": metric_id,
                "metric_name": catalog.get("metricName", raw.get("metricName", metric_id)),
                "county_fips": str(raw["countyFips"]),
                "county_name": raw["countyName"],
                "period": str(raw["period"]),
                "period_end": str(raw["periodEnd"]),
                "value": raw.get("value"),
                "unit": raw.get("unit", catalog.get("unit", "")),
                "margin_of_error_90": raw.get("marginOfError90", raw.get("marginOfError")),
                "subgroup_type": raw.get("subgroupType", ""),
                "subgroup_id": raw.get("subgroupId", ""),
                "subgroup_label": raw.get("subgroupLabel", ""),
                "tenure": raw.get("tenure", ""),
                "sex": raw.get("sex", ""),
                "source_ids": ";".join(sources),
                "source_releases": ";".join(_as_list(raw.get("sourceReleases", raw.get("sourceRelease")))),
                "derivation": "derived" if metric_id in DERIVED_METRIC_IDS else ("published" if formula.casefold() == "published" else "aggregated"),
                "formula": formula,
                "comparability_status": raw.get("comparabilityStatus", "documented within source series"),
                "geography_basis": raw.get("geographyBasis", catalog.get("geographyBasis", "")),
                "universe": raw.get("universe", ""),
                "notes": raw.get("notes", catalog.get("notes", catalog.get("interpretation", ""))),
            }
        )

    source_rows = _source_rows(module, payload)
    source_tiers = {row["source_id"]: row["source_tier"] for row in source_rows}
    catalog_rows: list[dict[str, Any]] = []
    for raw in raw_catalog:
        domain = str(raw["domain"])
        metric_id = str(raw["metricId"])
        formula = _catalog_formula(raw)
        source_ids = sorted(observed_sources[(domain, metric_id)])
        tiers = [source_tiers.get(source_id) for source_id in source_ids if source_tiers.get(source_id) not in (None, "")]
        catalog_rows.append(
            {
                "measure_key": _measure_key(module["module_id"], domain, metric_id),
                "module_id": module["module_id"],
                "module_version": module["version"],
                "module_label": module["label"],
                "domain": domain,
                "metric_id": metric_id,
                "metric_name": raw.get("metricName", metric_id),
                "unit": raw.get("unit", ""),
                "frequency": raw.get("frequency", ""),
                "derivation": "derived" if metric_id in DERIVED_METRIC_IDS else ("published" if formula.casefold() == "published" else "aggregated"),
                "formula": formula,
                "source_ids": ";".join(source_ids),
                "source_tier": min(tiers) if tiers else "",
                "subgroup_dimension": raw.get("subgroupDimension", "none"),
                "geography_basis": raw.get("geographyBasis", ""),
                "history_start": raw.get("historyStart", ""),
                "notes": raw.get("notes", raw.get("interpretation", "")),
            }
        )
    return payload, observations, catalog_rows, source_rows


def _latest_rows(observations: list[dict[str, Any]], measure_key: str) -> list[dict[str, Any]]:
    candidates = [
        row
        for row in observations
        if row["measure_key"] == measure_key
        and not row["subgroup_type"]
        and not row["subgroup_id"]
        and not row["tenure"]
        and not row["sex"]
        and row["value"] is not None
    ]
    if not candidates:
        return []
    common_periods = set.intersection(
        *(
            {row["period_end"] for row in candidates if row["county_fips"] == fips}
            for fips in sorted(COUNTY_BY_FIPS)
        )
    )
    if not common_periods:
        return []
    period = max(common_periods)
    return sorted(
        [row for row in candidates if row["period_end"] == period],
        key=lambda row: row["county_name"],
    )


def _profile_rows(observations: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for module_id, domain, metric_id, section in PROFILE_MEASURES:
        key = _measure_key(module_id, domain, metric_id)
        for row in _latest_rows(observations, key):
            output.append(
                {
                    "countyFips": row["county_fips"],
                    "countyName": row["county_name"],
                    "section": section,
                    "measureKey": key,
                    "metricName": row["metric_name"],
                    "value": row["value"],
                    "unit": row["unit"],
                    "period": row["period"],
                    "periodEnd": row["period_end"],
                    "marginOfError90": row["margin_of_error_90"],
                    "derivation": catalog[key]["derivation"],
                    "sourceIds": _as_list(row["source_ids"]),
                }
            )
    return output


def _comparison_rows(observations: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for module_id, domain, metric_id in COMPARISON_MEASURES:
        key = _measure_key(module_id, domain, metric_id)
        for row in _latest_rows(observations, key):
            output.append(
                {
                    "measureKey": key,
                    "metricName": row["metric_name"],
                    "countyFips": row["county_fips"],
                    "countyName": row["county_name"],
                    "value": row["value"],
                    "unit": row["unit"],
                    "period": row["period"],
                    "periodEnd": row["period_end"],
                    "marginOfError90": row["margin_of_error_90"],
                    "derivation": catalog[key]["derivation"],
                    "formula": catalog[key]["formula"],
                    "sourceIds": _as_list(row["source_ids"]),
                }
            )
    return output


def _quality_rows(
    payloads: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    comparison_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_observations = sum(module["expected_observations"] for module in MODULES)
    expected_metrics = sum(module["expected_metrics"] for module in MODULES)
    expected_snapshots = sum(module["expected_snapshots"] for module in MODULES)
    counties = {row["county_fips"] for row in observations}
    identities = {
        (
            row["module_id"], row["domain"], row["metric_id"], row["county_fips"],
            row["period"], row["subgroup_type"], row["subgroup_id"], row["tenure"], row["sex"],
        )
        for row in observations
    }
    v14 = next(payload for payload in payloads if payload["version"] == "1.4.0")
    v14_acs = [
        row for row in observations
        if row["module_id"] == "equity" and "CENSUS_ACS5_DETAIL" in row["source_ids"] and row["value"] is not None
    ]
    checks = (
        ("four_verified_modules", len(payloads) == 4, len(payloads), 4, "Versions 1.1 through 1.4 are present."),
        ("nine_county_coverage", counties == set(COUNTY_BY_FIPS), len(counties), 9, "All public rows resolve to the nine-county Bay Area."),
        ("cumulative_observation_count", len(observations) == expected_observations, len(observations), expected_observations, "No inherited observation is dropped or invented."),
        ("cumulative_metric_count", len(catalog_rows) == expected_metrics, len(catalog_rows), expected_metrics, "Every inherited measure has a namespaced definition."),
        ("cumulative_snapshot_count", sum(module["expected_snapshots"] for module in MODULES) == expected_snapshots, expected_snapshots, 181, "Snapshot counts are inherited without deduplicating distinct release records."),
        ("observation_identity_unique", len(identities) == len(observations), len(observations) - len(identities), 0, "The public-use grain is unique."),
        ("source_linkage_complete", all(row["source_ids"] for row in observations), sum(not row["source_ids"] for row in observations), 0, "Every observation retains one or more source identifiers."),
        ("derived_formulas_exposed", all(row["formula"] for row in catalog_rows if row["derivation"] == "derived"), sum(not row["formula"] for row in catalog_rows if row["derivation"] == "derived"), 0, "Every derived measure exposes its formula or exact method."),
        ("source_urls_valid", all(str(row["landing_url"]).startswith("https://") for row in source_rows), sum(not str(row["landing_url"]).startswith("https://") for row in source_rows), 0, "Every source drill-down uses an official or documented HTTPS landing page."),
        ("profile_coverage", len(profile_rows) == len(PROFILE_MEASURES) * 9, len(profile_rows), len(PROFILE_MEASURES) * 9, "Every curated profile measure covers every county at one common period."),
        ("comparison_coverage", len(comparison_rows) == len(COMPARISON_MEASURES) * 9, len(comparison_rows), len(COMPARISON_MEASURES) * 9, "Every comparison uses one common period and alphabetical county order."),
        ("comparison_order_not_ranked", all([row["countyName"] for row in comparison_rows if row["measureKey"] == key] == sorted(row["countyName"] for row in comparison_rows if row["measureKey"] == key) for key in {row["measureKey"] for row in comparison_rows}), "alphabetical", "alphabetical", "County rows are never sorted by indicator value."),
        ("v1_4_uncertainty_preserved", len(v14_acs) == 3448 and all(row["margin_of_error_90"] is not None for row in v14_acs), sum(row["margin_of_error_90"] is not None for row in v14_acs), 3448, "All applicable ACS rows retain 90% margins of error."),
        ("v1_4_boundaries_preserved", not any([v14["interpretationBoundaries"]["countyRankingProduced"], v14["interpretationBoundaries"]["equityScoreProduced"], v14["interpretationBoundaries"]["causalInferenceProduced"]]), "all prohibited outputs false", "all prohibited outputs false", "No equity score, county ranking, or causal inference is introduced."),
        ("phase10_hold_preserved", all(payload["publicationBoundary"]["phase10ReportStatus"] == "human_approval_hold" for payload in payloads), "human_approval_hold", "human_approval_hold", "The held narrative report is not published."),
        ("automatic_narrative_disabled", all(not payload["publicationBoundary"].get("automatedNarrative", False) for payload in payloads), False, False, "The public-use layer contains descriptive interfaces, not automated conclusions."),
    )
    return [
        {"check_id": check_id, "passed": int(bool(passed)), "observed": observed, "expected": expected, "detail": detail}
        for check_id, passed, observed, expected, detail in checks
    ]


def _build_database(
    path: Path,
    observations: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    quality_rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE measure (measure_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE source (registry_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE observation (observation_id INTEGER PRIMARY KEY, measure_key TEXT NOT NULL REFERENCES measure(measure_key), county_fips TEXT NOT NULL, period TEXT NOT NULL, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE quality_check (check_id TEXT PRIMARY KEY, passed INTEGER NOT NULL CHECK(passed IN (0,1)), payload TEXT NOT NULL)")
        connection.executemany("INSERT INTO measure VALUES (?, ?)", ((row["measure_key"], json.dumps(row, sort_keys=True)) for row in catalog_rows))
        connection.executemany("INSERT INTO source VALUES (?, ?)", ((row["registry_key"], json.dumps(row, sort_keys=True)) for row in source_rows))
        connection.executemany("INSERT INTO observation(measure_key, county_fips, period, payload) VALUES (?, ?, ?, ?)", ((row["measure_key"], row["county_fips"], row["period"], json.dumps(row, sort_keys=True)) for row in observations))
        connection.executemany("INSERT INTO quality_check VALUES (?, ?, ?)", ((row["check_id"], row["passed"], json.dumps(row, sort_keys=True)) for row in quality_rows))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise ValueError(f"public-use database failed integrity checks: {integrity}, {foreign_keys}")


def build_phase14_public_use(*, built_at: str | None = None) -> dict[str, Any]:
    built_at = built_at or _utc_now()
    payloads: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    catalog_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    module_summaries = []
    for module in MODULES:
        payload, module_observations, module_catalog, module_sources = _normalize_module(module)
        payloads.append(payload)
        observations.extend(module_observations)
        catalog_rows.extend(module_catalog)
        source_rows.extend(module_sources)
        module_summaries.append(
            {
                "moduleId": module["module_id"],
                "label": module["label"],
                "version": module["version"],
                "observationCount": module["expected_observations"],
                "metricCount": module["expected_metrics"],
                "sourceSnapshotCount": module["expected_snapshots"],
                "route": {
                    "observatory": "/housing",
                    "access": "/housing/access",
                    "production": "/housing/production",
                    "equity": "/housing/equity",
                }[module["module_id"]],
            }
        )
    observations.sort(key=lambda row: (row["module_id"], row["domain"], row["metric_id"], row["county_name"], row["period_end"], row["subgroup_label"], row["tenure"], row["sex"]))
    catalog_rows.sort(key=lambda row: (row["module_id"], row["domain"], row["metric_name"], row["measure_key"]))
    source_rows.sort(key=lambda row: (row["publisher"], row["dataset"], row["module_id"]))
    catalog = {row["measure_key"]: row for row in catalog_rows}
    profiles = _profile_rows(observations, catalog)
    comparisons = _comparison_rows(observations, catalog)
    quality = _quality_rows(payloads, observations, catalog_rows, source_rows, profiles, comparisons)
    failures = [row["check_id"] for row in quality if not row["passed"]]
    if failures:
        raise ValueError(f"Version 1.5 quality checks failed: {failures}")

    _write_csv(EXPORT_ROOT / "public_use_observations.csv", observations, OBSERVATION_FIELDS)
    _write_csv(EXPORT_ROOT / "public_use_measure_catalog.csv", catalog_rows, CATALOG_FIELDS)
    _write_csv(EXPORT_ROOT / "public_use_source_registry.csv", source_rows, SOURCE_FIELDS)
    _write_csv(EXPORT_ROOT / "county_profiles.csv", profiles, ("countyFips", "countyName", "section", "measureKey", "metricName", "value", "unit", "period", "periodEnd", "marginOfError90", "derivation", "sourceIds"))
    _write_csv(EXPORT_ROOT / "county_comparisons.csv", comparisons, ("measureKey", "metricName", "countyFips", "countyName", "value", "unit", "period", "periodEnd", "marginOfError90", "derivation", "formula", "sourceIds"))
    _write_csv(EXPORT_ROOT / "quality_checks.csv", quality, ("check_id", "passed", "observed", "expected", "detail"))

    full_payload = {
        "product": "Housing Analytical & Public-Use Layer",
        "version": PHASE14_PUBLIC_USE_VERSION,
        "builtAt": built_at,
        "countyCount": 9,
        "moduleCount": 4,
        "metricCount": len(catalog_rows),
        "observationCount": len(observations),
        "sourceRegistryCount": len(source_rows),
        "sourceSnapshotCount": sum(module["expected_snapshots"] for module in MODULES),
        "counties": [{"countyFips": fips, "countyName": COUNTY_BY_FIPS[fips]} for fips in sorted(COUNTY_BY_FIPS, key=lambda item: COUNTY_BY_FIPS[item])],
        "modules": module_summaries,
        "metricCatalog": catalog_rows,
        "sourceRegistry": source_rows,
        "observations": observations,
        "publicationBoundary": {
            "automaticNarrative": False,
            "causalInference": False,
            "compositeScore": None,
            "countyRankingProduced": False,
            "newAnalysisRequiresNamedHumanApproval": True,
            "phase10ReportStatus": "human_approval_hold",
        },
    }
    index_payload = {
        key: full_payload[key]
        for key in (
            "product", "version", "builtAt", "countyCount", "moduleCount", "metricCount",
            "observationCount", "sourceRegistryCount", "sourceSnapshotCount", "counties", "modules",
            "metricCatalog", "sourceRegistry", "publicationBoundary",
        )
    }
    index_payload.update(
        {
            "profileIndicators": profiles,
            "comparisonRows": comparisons,
            "qualityChecks": quality,
            "downloads": [
                {"label": "Complete normalized observations", "format": "CSV", "href": "/downloads/housing/v1.5/public_use_observations.csv"},
                {"label": "Measure definitions", "format": "CSV", "href": "/downloads/housing/v1.5/public_use_measure_catalog.csv"},
                {"label": "Source registry", "format": "CSV", "href": "/downloads/housing/v1.5/public_use_source_registry.csv"},
                {"label": "County profile snapshot", "format": "CSV", "href": "/downloads/housing/v1.5/county_profiles.csv"},
                {"label": "Cross-county comparisons", "format": "CSV", "href": "/downloads/housing/v1.5/county_comparisons.csv"},
                {"label": "Complete public-use package", "format": "JSON", "href": "/downloads/housing/v1.5/housing-public-use-data.json"},
            ],
            "methodNotes": [
                "County comparisons use one common period per measure and retain alphabetical county order.",
                "Every measure is namespaced to its originating release; similarly named measures are not silently merged.",
                "Derived indicators expose their formula and source inputs. No composite score is produced.",
                "ACS residence estimates and LODES workplace-job records remain separate statistical universes.",
            ],
        }
    )
    _write_json(PUBLIC_ROOT / "housing-public-use-data.json", full_payload)
    _write_json(PUBLIC_ROOT / "housing-public-use-index.json", index_payload)
    equity_payload = next(payload for payload in payloads if payload["version"] == "1.4.0")
    equity_site_payload = {
        key: equity_payload[key]
        for key in (
            "builtAt", "counties", "countyCount", "domainCount", "domains",
            "interpretationBoundaries", "methodNotes", "metricCatalog", "metricCount",
            "observationCount", "product", "publicationBoundary", "sourceRegistry",
            "sourceSnapshotCount", "version",
        )
    }
    equity_site_payload["observations"] = [
        {
            key: row.get(key)
            for key in (
                "countyFips", "countyName", "denominator", "domain", "geographyBasis",
                "marginOfError90", "metricId", "metricName", "notes", "period", "periodEnd",
                "sex", "sourceIds", "sourceReleases", "subgroupId", "subgroupLabel",
                "subgroupType", "tenure", "unit", "universe", "value",
            )
        }
        for row in equity_payload["observations"]
    ]
    _build_database(OUTPUT_ROOT / "housing_public_use.sqlite", observations, catalog_rows, source_rows, quality)

    if SITE_ROOT.is_dir():
        _write_compact_json(PUBLIC_ROOT / "housing-equity-site-data.json", equity_site_payload)
        SITE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _write_json(SITE_DATA_ROOT / "housing-public-use-index.json", index_payload)
        equity_site_path = SITE_ROOT / "app" / "housing" / "equity" / "housing-equity-connections-data.json"
        _write_compact_json(equity_site_path, equity_site_payload)
        SITE_DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        download_sources = {
            EXPORT_ROOT / "public_use_observations.csv": SITE_DOWNLOAD_ROOT / "public_use_observations.csv",
            EXPORT_ROOT / "public_use_measure_catalog.csv": SITE_DOWNLOAD_ROOT / "public_use_measure_catalog.csv",
            EXPORT_ROOT / "public_use_source_registry.csv": SITE_DOWNLOAD_ROOT / "public_use_source_registry.csv",
            EXPORT_ROOT / "county_profiles.csv": SITE_DOWNLOAD_ROOT / "county_profiles.csv",
            EXPORT_ROOT / "county_comparisons.csv": SITE_DOWNLOAD_ROOT / "county_comparisons.csv",
            PUBLIC_ROOT / "housing-public-use-data.json": SITE_DOWNLOAD_ROOT / "housing-public-use-data.json",
        }
        for source, target in download_sources.items():
            target.write_bytes(source.read_bytes())

    required_paths = [
        *(PROJECT_ROOT / path for path in REQUIRED_STATIC_FILES),
        EXPORT_ROOT / "public_use_observations.csv",
        EXPORT_ROOT / "public_use_measure_catalog.csv",
        EXPORT_ROOT / "public_use_source_registry.csv",
        EXPORT_ROOT / "county_profiles.csv",
        EXPORT_ROOT / "county_comparisons.csv",
        EXPORT_ROOT / "quality_checks.csv",
        PUBLIC_ROOT / "housing-public-use-data.json",
        PUBLIC_ROOT / "housing-public-use-index.json",
    ]
    manifest = {
        "product": "Housing Analytical & Public-Use Layer",
        "version": PHASE14_PUBLIC_USE_VERSION,
        "built_at": built_at,
        "inherits": [module["version"] for module in MODULES],
        "county_count": 9,
        "module_count": 4,
        "metric_count": len(catalog_rows),
        "observation_count": len(observations),
        "source_registry_count": len(source_rows),
        "source_snapshot_count": sum(module["expected_snapshots"] for module in MODULES),
        "quality_check_count": len(quality),
        "quality_check_failures": 0,
        "publication": {
            "automatic_narrative": False,
            "composite_score": False,
            "county_ranking": False,
            "github_status": "candidate_pending_merge",
            "site_status": "candidate_pending_external_verification",
            "phase10_report_status": "human_approval_hold",
        },
        "files": [
            {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "byte_count": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in sorted(required_paths)
        ],
    }
    _write_json(OUTPUT_ROOT / "phase14_v1_5_manifest.json", manifest)
    verification = verify_phase14_public_use()
    return {"manifest": str(OUTPUT_ROOT / "phase14_v1_5_manifest.json"), "verification": verification}


def verify_phase14_public_use(manifest_path: Path | None = None) -> dict[str, Any]:
    manifest_path = manifest_path or OUTPUT_ROOT / "phase14_v1_5_manifest.json"
    manifest = _read_json(manifest_path)
    checks: list[tuple[str, bool]] = []
    checks.append(("version", manifest.get("version") == PHASE14_PUBLIC_USE_VERSION))
    checks.append(("nine_counties", manifest.get("county_count") == 9))
    checks.append(("four_modules", manifest.get("module_count") == 4))
    checks.append(("metric_count", manifest.get("metric_count") == 139))
    checks.append(("observation_count", manifest.get("observation_count") == 10224))
    checks.append(("snapshot_count", manifest.get("source_snapshot_count") == 181))
    checks.append(("quality_checks", manifest.get("quality_check_count") == 16 and manifest.get("quality_check_failures") == 0))
    for item in manifest.get("files", []):
        path = PROJECT_ROOT / item["path"]
        checks.append((f"file:{item['path']}", path.is_file() and path.stat().st_size == item["byte_count"] and _sha256(path) == item["sha256"]))
    payload = _read_json(PUBLIC_ROOT / "housing-public-use-index.json")
    checks.append(("comparison_not_ranked", not payload["publicationBoundary"]["countyRankingProduced"]))
    checks.append(("composite_absent", payload["publicationBoundary"]["compositeScore"] is None))
    checks.append(("phase10_hold", payload["publicationBoundary"]["phase10ReportStatus"] == "human_approval_hold"))
    if SITE_ROOT.is_dir():
        checks.append(("site_data_matches", _sha256(PUBLIC_ROOT / "housing-public-use-index.json") == _sha256(SITE_DATA_ROOT / "housing-public-use-index.json")))
    database = OUTPUT_ROOT / "housing_public_use.sqlite"
    if database.is_file():
        with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
            checks.append(("database_integrity", connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"))
            checks.append(("database_observations", connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0] == 10224))
            checks.append(("database_failed_checks", connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed=0").fetchone()[0] == 0))
    failed = [name for name, passed in checks if not passed]
    return {"complete": not failed, "passing": len(checks) - len(failed), "total": len(checks), "failed": failed}
