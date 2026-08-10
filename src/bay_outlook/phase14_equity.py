from __future__ import annotations

import csv
import gzip
import io
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import COUNTY_BY_FIPS, PROJECT_ROOT
from .phase14_production import verify_phase14_production


PHASE14_EQUITY_VERSION = "1.4.0"
ACS_YEARS = (2022, 2023, 2024)
ACS_COST_BURDEN_YEARS = (2023, 2024)
LODES_YEARS = (2021, 2022, 2023)
LATEST_ACS_YEAR = ACS_YEARS[-1]
LATEST_LODES_YEAR = LODES_YEARS[-1]

REQUIRED_EQUITY_DOMAINS = {
    "age_and_tenure",
    "commuting_and_job_access",
    "education_and_earnings",
    "household_type_and_housing",
    "occupation_and_affordability",
    "race_ethnicity_and_housing",
    "transportation_and_housing",
}

RACE_GROUPS = {
    "A": ("white_alone", "White alone"),
    "B": ("black_alone", "Black or African American alone"),
    "C": ("aian_alone", "American Indian and Alaska Native alone"),
    "D": ("asian_alone", "Asian alone"),
    "E": ("nhpi_alone", "Native Hawaiian and Other Pacific Islander alone"),
    "F": ("some_other_race_alone", "Some other race alone"),
    "G": ("two_or_more_races", "Two or more races"),
    "H": ("white_non_hispanic", "White alone, not Hispanic or Latino"),
    "I": ("hispanic_or_latino", "Hispanic or Latino"),
}

COMMON_ACS_GROUPS = tuple(
    [f"B25003{suffix}" for suffix in RACE_GROUPS]
    + [
        "B25007",
        "B25013",
        "B25115",
        "B20004",
        "B24011",
        "B08303",
        "B08006",
        "B25044",
    ]
)
COST_ACS_GROUPS = tuple(f"B25140{suffix}" for suffix in RACE_GROUPS)

SOURCE_REGISTRY = {
    "CENSUS_ACS5_DETAIL": {
        "publisher": "U.S. Census Bureau",
        "dataset": "American Community Survey 5-Year Detailed Tables",
        "source_tier": 1,
        "source_class": "government survey estimate",
        "frequency": "annual rolling five-year estimate",
        "geography_basis": "household or person residence",
        "landing_url": "https://www.census.gov/programs-surveys/acs/data.html",
    },
    "CENSUS_LEHD_LODES": {
        "publisher": "U.S. Census Bureau",
        "dataset": "LEHD Origin-Destination Employment Statistics, LODES8",
        "source_tier": 1,
        "source_class": "government administrative job estimate with disclosure protection",
        "frequency": "annual",
        "geography_basis": "workplace and residence census block, aggregated to county",
        "landing_url": (
            "https://lehd.ces.census.gov/data/"
            "#lodes"
        ),
    },
    "HUD_FMR": {
        "publisher": "U.S. Department of Housing and Urban Development",
        "dataset": "FY 2026 Fair Market Rents, Revised",
        "source_tier": 1,
        "source_class": "government modeled rent standard",
        "frequency": "federal fiscal year",
        "geography_basis": "HUD FMR area assigned to county",
        "landing_url": "https://www.huduser.gov/portal/datasets/fmr.html",
    },
}

OBSERVATION_FIELDS = (
    "domain",
    "metric_id",
    "metric_name",
    "county_fips",
    "county_name",
    "period",
    "period_end",
    "frequency",
    "value",
    "unit",
    "margin_of_error_90",
    "numerator",
    "denominator",
    "benchmark_value",
    "subgroup_type",
    "subgroup_id",
    "subgroup_label",
    "tenure",
    "sex",
    "geography_basis",
    "universe",
    "source_ids",
    "source_releases",
    "retrieved_at",
    "raw_sha256s",
    "calculation",
    "notes",
    "reliability_flag",
    "comparability_status",
)

SNAPSHOT_FIELDS = (
    "source_id",
    "source_url",
    "source_release",
    "retrieved_at",
    "snapshot_sha256",
    "byte_count",
    "snapshot_kind",
    "raw_path",
    "row_count",
)

REQUIRED_EQUITY_FILES = (
    "config/phase14_housing_equity.json",
    "metadata/housing_equity_indicator_catalog.csv",
    "metadata/housing_equity_source_registry.csv",
    "docs/housing-equity/README.md",
    "docs/housing-equity/METHODOLOGY.md",
    "docs/housing-equity/DATA_DICTIONARY.md",
    "docs/housing-equity/LIMITATIONS.md",
    "docs/housing-equity/RUNBOOK.md",
    "src/bay_outlook/phase14_equity.py",
    "tests/test_phase14_equity.py",
    ".github/workflows/phase14-v1-4-audit.yml",
    ".github/workflows/housing-equity-update.yml",
    "data/phase14/equity/exports/equity_observations.csv",
    "data/phase14/equity/exports/equity_snapshot.csv",
    "data/phase14/equity/exports/race_housing.csv",
    "data/phase14/equity/exports/education_occupation_affordability.csv",
    "data/phase14/equity/exports/commuting_connections.csv",
    "data/phase14/equity/exports/source_freshness.csv",
    "data/phase14/equity/exports/source_snapshots.csv",
    "data/phase14/equity/exports/quality_checks.csv",
    "data/phase14/equity/public/housing-equity-connections-data.json",
)

METRIC_INTERPRETATIONS = {
    "homeownership_pct": (
        "Owner-occupied units divided by owner- plus renter-occupied units for the stated subgroup."
    ),
    "housing_cost_burden_over_30_pct": (
        "Units with housing costs over 30% of household income divided by units with a computable ratio."
    ),
    "housing_cost_burden_over_50_pct": (
        "Units with housing costs over 50% of household income divided by units with a computable ratio."
    ),
    "median_annual_earnings": (
        "ACS median annual earnings for the stated resident population; not household income or local-job pay."
    ),
    "earnings_coverage_2br_fmr_pct": (
        "Median annual earnings divided by the annual income required for FY2026 two-bedroom FMR at 30%."
    ),
    "monthly_gap_to_2br_fmr": (
        "FY2026 two-bedroom FMR minus 30% of median annual earnings divided by 12."
    ),
    "commute_under_30_minutes_pct": (
        "Workers with travel time under 30 minutes divided by workers who did not work from home."
    ),
    "commute_30_to_59_minutes_pct": (
        "Workers with travel time from 30 through 59 minutes divided by workers who did not work from home."
    ),
    "commute_60_plus_minutes_pct": (
        "Workers with travel time of 60 minutes or more divided by workers who did not work from home."
    ),
    "worked_from_home_pct": "People working from home divided by workers age 16 and over.",
    "public_transport_commute_pct": (
        "Public-transportation commuters divided by workers age 16 and over."
    ),
    "zero_vehicle_households_pct": (
        "Occupied units with no vehicle available divided by occupied units for the stated tenure."
    ),
    "primary_jobs": "LODES JT01 primary jobs located in the county.",
    "local_resident_primary_jobs": (
        "Primary jobs whose workplace and home geocodes are in the same county."
    ),
    "other_california_inbound_primary_jobs": (
        "Primary jobs located in the county whose home geocode is in another California county."
    ),
    "outside_california_inbound_primary_jobs": (
        "Primary jobs located in the county whose home geocode is outside California."
    ),
    "local_resident_job_share_pct": (
        "Same-county home-and-work primary jobs divided by primary jobs located in the county."
    ),
    "inbound_job_share_pct": (
        "Primary jobs located in the county with a home geocode outside the county divided by county primary jobs."
    ),
    "low_monthly_earnings_job_share_pct": (
        "LODES primary jobs in the $1,250-per-month-or-less earnings band."
    ),
    "middle_monthly_earnings_job_share_pct": (
        "LODES primary jobs in the $1,251-to-$3,333 monthly earnings band."
    ),
    "high_monthly_earnings_job_share_pct": (
        "LODES primary jobs in the over-$3,333 monthly earnings band."
    ),
    "age_29_or_younger_job_share_pct": "LODES primary jobs held by people age 29 or younger.",
    "age_30_to_54_job_share_pct": "LODES primary jobs held by people age 30 through 54.",
    "age_55_plus_job_share_pct": "LODES primary jobs held by people age 55 or older.",
}


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: Iterable[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(fieldnames or (rows[0].keys() if rows else []))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=names,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _download_file(url: str, target: Path, *, attempts: int = 4) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "The-Bay-Outlook/1.4 (evidence refresh)"},
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        temporary: Path | None = None
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
            if not temporary or temporary.stat().st_size == 0:
                raise RuntimeError(f"zero-byte response from {url}")
            os.replace(temporary, target)
            return target
        except Exception as error:  # pragma: no cover - network-specific branch
            last_error = error
            if temporary:
                temporary.unlink(missing_ok=True)
            time.sleep(min(8, 1 + 2**attempt))
    raise RuntimeError(f"unable to retrieve {url}: {last_error}")


def _number(value: Any) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= -100000000:
        return None
    return result


def _cell(row: dict[str, str], variable: str) -> tuple[float | None, float | None]:
    return _number(row.get(f"{variable}E")), _number(row.get(f"{variable}M"))


def _sum_cells(
    row: dict[str, str],
    variables: Iterable[str],
) -> tuple[float | None, float | None]:
    estimates: list[float] = []
    margins: list[float] = []
    for variable in variables:
        estimate, margin = _cell(row, variable)
        if estimate is None:
            return None, None
        estimates.append(estimate)
        if margin is not None:
            margins.append(margin)
    margin = math.sqrt(sum(value * value for value in margins)) if len(margins) == len(estimates) else None
    return sum(estimates), margin


def _ratio_moe(
    numerator: float,
    numerator_moe: float | None,
    denominator: float,
    denominator_moe: float | None,
) -> float | None:
    if denominator <= 0 or numerator_moe is None or denominator_moe is None:
        return None
    proportion = numerator / denominator
    radicand = numerator_moe**2 - (proportion**2 * denominator_moe**2)
    if radicand < 0:
        radicand = numerator_moe**2 + (proportion**2 * denominator_moe**2)
    return math.sqrt(radicand) / denominator * 100.0


def _rate_reliability(
    value: float | None,
    margin: float | None,
    denominator: float | None,
) -> str:
    if value is None:
        return "missing_or_suppressed"
    if denominator is None or denominator < 100:
        return "project_flag_low_denominator"
    if margin is None:
        return "project_flag_moe_unavailable"
    relative = margin / max(abs(value), 0.1)
    if margin > 10 or relative > 0.50:
        return "project_flag_high_uncertainty"
    if margin > 5 or relative > 0.30:
        return "project_flag_moderate_uncertainty"
    return "project_flag_standard"


def _median_reliability(value: float | None, margin: float | None) -> str:
    if value is None:
        return "missing_or_suppressed"
    if margin is None:
        return "project_flag_moe_unavailable"
    ratio = margin / max(abs(value), 1.0)
    if ratio > 0.30:
        return "project_flag_high_uncertainty"
    if ratio > 0.15:
        return "project_flag_moderate_uncertainty"
    return "project_flag_standard"


def _clean_number(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if abs(value - round(value)) < 1e-10:
        return int(round(value))
    return round(value, 8)


def _observation(
    *,
    domain: str,
    metric_id: str,
    metric_name: str,
    county_fips: str,
    period: int | str,
    value: float | int | None,
    unit: str,
    frequency: str,
    subgroup_type: str = "",
    subgroup_id: str = "",
    subgroup_label: str = "",
    tenure: str = "",
    sex: str = "",
    margin_of_error_90: float | None = None,
    numerator: float | None = None,
    denominator: float | None = None,
    benchmark_value: float | None = None,
    geography_basis: str,
    universe: str,
    source_ids: str,
    source_releases: str,
    retrieved_at: str,
    raw_sha256s: str,
    calculation: str,
    notes: str,
    reliability_flag: str,
    comparability_status: str,
) -> dict[str, Any]:
    period_text = str(period)
    if period_text.startswith("FY"):
        period_end = "2026-09-30"
    else:
        period_end = f"{period_text}-12-31"
    return {
        "domain": domain,
        "metric_id": metric_id,
        "metric_name": metric_name,
        "county_fips": county_fips,
        "county_name": COUNTY_BY_FIPS[county_fips],
        "period": period_text,
        "period_end": period_end,
        "frequency": frequency,
        "value": _clean_number(value),
        "unit": unit,
        "margin_of_error_90": _clean_number(margin_of_error_90),
        "numerator": _clean_number(numerator),
        "denominator": _clean_number(denominator),
        "benchmark_value": _clean_number(benchmark_value),
        "subgroup_type": subgroup_type,
        "subgroup_id": subgroup_id,
        "subgroup_label": subgroup_label,
        "tenure": tenure,
        "sex": sex,
        "geography_basis": geography_basis,
        "universe": universe,
        "source_ids": source_ids,
        "source_releases": source_releases,
        "retrieved_at": retrieved_at,
        "raw_sha256s": raw_sha256s,
        "calculation": calculation,
        "notes": notes,
        "reliability_flag": reliability_flag,
        "comparability_status": comparability_status,
    }


def _rate_observation(
    *,
    row: dict[str, str],
    numerator_variables: Iterable[str],
    denominator_variables: Iterable[str],
    source: dict[str, Any],
    **kwargs: Any,
) -> dict[str, Any]:
    numerator, numerator_moe = _sum_cells(row, numerator_variables)
    denominator, denominator_moe = _sum_cells(row, denominator_variables)
    if numerator is None or denominator is None or denominator <= 0:
        value = None
        margin = None
    else:
        value = numerator / denominator * 100.0
        margin = _ratio_moe(numerator, numerator_moe, denominator, denominator_moe)
    return _observation(
        value=value,
        margin_of_error_90=margin,
        numerator=numerator,
        denominator=denominator,
        retrieved_at=source["retrieved_at"],
        raw_sha256s=source["snapshot_sha256"],
        reliability_flag=_rate_reliability(value, margin, denominator),
        **kwargs,
    )


def _fetch_acs_group(
    *,
    root: Path,
    raw_dir: Path,
    year: int,
    group: str,
    retrieved_at: str,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    url = (
        "https://www2.census.gov/programs-surveys/acs/summary_file/"
        f"{year}/table-based-SF/data/5YRData/acsdt5y{year}-{group.lower()}.dat"
    )
    target = (
        raw_dir
        / "CENSUS_ACS5_DETAIL"
        / retrieved_at[:10]
        / f"acs5-{year}-{group}-bay-counties-summary-file.json"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "The-Bay-Outlook/1.4 (evidence refresh)"},
    )
    expected_counties = set(COUNTY_BY_FIPS)
    selected: dict[str, dict[str, str]] = {}
    response_headers: dict[str, str] = {}
    scanned_rows = 0
    last_error: Exception | None = None

    for attempt in range(4):
        try:
            selected = {}
            scanned_rows = 0
            with urllib.request.urlopen(request, timeout=300) as response:
                response_headers = {
                    "content_length": response.headers.get("Content-Length", ""),
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                }
                stream = io.TextIOWrapper(response, encoding="utf-8-sig", newline="")
                try:
                    reader = csv.DictReader(stream, delimiter="|")
                    if not reader.fieldnames or "GEO_ID" not in reader.fieldnames:
                        raise ValueError(
                            f"ACS {year} {group} summary file has no GEO_ID field"
                        )
                    for row in reader:
                        scanned_rows += 1
                        geo_id = str(row.get("GEO_ID", "")).strip()
                        prefix = "0500000US"
                        if not geo_id.startswith(prefix):
                            continue
                        county_fips = geo_id[len(prefix) :]
                        if county_fips not in expected_counties:
                            continue

                        normalized = {
                            "GEO_ID": geo_id,
                            "state": county_fips[:2],
                            "county": county_fips[2:],
                        }
                        variable_prefix = f"{group}_"
                        for field, value in row.items():
                            if field is None or not field.startswith(variable_prefix):
                                continue
                            suffix = field[len(variable_prefix) :]
                            if (
                                len(suffix) == 4
                                and suffix[0] in {"E", "M"}
                                and suffix[1:].isdigit()
                            ):
                                normalized[
                                    f"{group}_{suffix[1:]}{suffix[0]}"
                                ] = value
                        selected[county_fips] = normalized
                        if set(selected) == expected_counties:
                            break
                finally:
                    stream.detach()

            if set(selected) != expected_counties:
                raise ValueError(
                    f"ACS {year} {group} county coverage {sorted(selected)} "
                    "does not match Bay Area"
                )
            break
        except Exception as error:  # pragma: no cover - network-specific branch
            last_error = error
            time.sleep(min(8, 1 + 2**attempt))
    else:
        raise RuntimeError(f"unable to retrieve {url}: {last_error}")

    source_release = f"ACS5-{year}-{group}-table-based-summary-file"
    extract = {
        "format": "ACS Table-Based Summary File county extract",
        "source_file_url": url,
        "source_release": source_release,
        "response_headers": response_headers,
        "selection": {
            "summary_level": "050",
            "state_fips": "06",
            "county_fips": sorted(expected_counties),
            "rows_scanned_before_complete": scanned_rows,
        },
        "rows": [selected[fips] for fips in sorted(selected)],
    }
    _write_json(target, extract)
    snapshot = {
        "source_id": "CENSUS_ACS5_DETAIL",
        "source_url": url,
        "source_release": source_release,
        "retrieved_at": retrieved_at,
        "snapshot_sha256": _sha256(target),
        "byte_count": target.stat().st_size,
        "snapshot_kind": "official_summary_file_bay_county_extract",
        "raw_path": target.relative_to(root).as_posix(),
        "row_count": len(selected),
    }
    return selected, snapshot


def _source_reference(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_release": snapshot["source_release"],
        "retrieved_at": snapshot["retrieved_at"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
    }


def _race_tenure_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    notes = (
        "Race and Hispanic-origin categories overlap and must not be summed. "
        "White alone includes Hispanic householders; White alone, not Hispanic is separate."
    )
    for year in ACS_YEARS:
        for suffix, (subgroup_id, subgroup_label) in RACE_GROUPS.items():
            group = f"B25003{suffix}"
            source = refs[(year, group)]
            for county_fips, row in acs[(year, group)].items():
                output.append(
                    _rate_observation(
                        row=row,
                        numerator_variables=(f"{group}_002",),
                        denominator_variables=(f"{group}_001",),
                        source=source,
                        domain="race_ethnicity_and_housing",
                        metric_id="homeownership_pct",
                        metric_name="Homeownership share",
                        county_fips=county_fips,
                        period=year,
                        unit="percent",
                        frequency="annual rolling five-year estimate",
                        subgroup_type="race_ethnicity_of_householder",
                        subgroup_id=subgroup_id,
                        subgroup_label=subgroup_label,
                        geography_basis="household residence",
                        universe="occupied housing units in the stated householder group",
                        source_ids="CENSUS_ACS5_DETAIL",
                        source_releases=source["source_release"],
                        calculation="owner-occupied units divided by occupied units",
                        notes=notes,
                        comparability_status="comparable_within_table_and_vintage",
                    )
                )
    return output


def _race_burden_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    tenure_cells = {
        "owner_with_mortgage": ("Owned units with a mortgage", 2, 3, 4, 5),
        "owner_without_mortgage": ("Owned units without a mortgage", 6, 7, 8, 9),
        "renter": ("Rented units", 10, 11, 12, 13),
    }
    notes = (
        "The denominator excludes units where the housing-cost-to-income ratio was not calculated. "
        "Race and Hispanic-origin categories overlap and are not additive."
    )
    for year in ACS_COST_BURDEN_YEARS:
        for suffix, (subgroup_id, subgroup_label) in RACE_GROUPS.items():
            group = f"B25140{suffix}"
            source = refs[(year, group)]
            for county_fips, row in acs[(year, group)].items():
                for tenure, (tenure_label, total_index, over30_index, over50_index, not_calc_index) in tenure_cells.items():
                    total, total_moe = _cell(row, f"{group}_{total_index:03d}")
                    not_calculated, not_calculated_moe = _cell(
                        row, f"{group}_{not_calc_index:03d}"
                    )
                    if total is None or not_calculated is None:
                        computed = None
                        computed_moe = None
                    else:
                        computed = total - not_calculated
                        computed_moe = (
                            math.hypot(total_moe, not_calculated_moe)
                            if total_moe is not None and not_calculated_moe is not None
                            else None
                        )
                    for metric_id, metric_name, index in (
                        (
                            "housing_cost_burden_over_30_pct",
                            "Housing costs over 30% of household income",
                            over30_index,
                        ),
                        (
                            "housing_cost_burden_over_50_pct",
                            "Housing costs over 50% of household income",
                            over50_index,
                        ),
                    ):
                        numerator, numerator_moe = _cell(row, f"{group}_{index:03d}")
                        if (
                            numerator is None
                            or computed is None
                            or computed <= 0
                        ):
                            value = None
                            margin = None
                        else:
                            value = numerator / computed * 100.0
                            margin = _ratio_moe(
                                numerator,
                                numerator_moe,
                                computed,
                                computed_moe,
                            )
                        output.append(
                            _observation(
                                domain="race_ethnicity_and_housing",
                                metric_id=metric_id,
                                metric_name=metric_name,
                                county_fips=county_fips,
                                period=year,
                                value=value,
                                unit="percent",
                                frequency="annual rolling five-year estimate",
                                margin_of_error_90=margin,
                                numerator=numerator,
                                denominator=computed,
                                subgroup_type="race_ethnicity_of_householder",
                                subgroup_id=subgroup_id,
                                subgroup_label=subgroup_label,
                                tenure=tenure,
                                geography_basis="household residence",
                                universe=f"{tenure_label} with a computable housing-cost ratio",
                                source_ids="CENSUS_ACS5_DETAIL",
                                source_releases=source["source_release"],
                                retrieved_at=source["retrieved_at"],
                                raw_sha256s=source["snapshot_sha256"],
                                calculation=(
                                    f"{metric_name.lower()} count divided by "
                                    "tenure total less not-calculated cases"
                                ),
                                notes=notes,
                                reliability_flag=_rate_reliability(
                                    value, margin, computed
                                ),
                                comparability_status=(
                                    "series_begins_2023_table_introduction"
                                ),
                            )
                        )
    return output


def _homeownership_from_pair(
    *,
    row: dict[str, str],
    owner_variables: Iterable[str],
    renter_variables: Iterable[str],
    source: dict[str, Any],
    domain: str,
    county_fips: str,
    year: int,
    subgroup_type: str,
    subgroup_id: str,
    subgroup_label: str,
    universe: str,
    notes: str,
) -> dict[str, Any]:
    owner, owner_moe = _sum_cells(row, owner_variables)
    renter, renter_moe = _sum_cells(row, renter_variables)
    if owner is None or renter is None:
        total = None
        total_moe = None
        value = None
        margin = None
    else:
        total = owner + renter
        total_moe = (
            math.hypot(owner_moe, renter_moe)
            if owner_moe is not None and renter_moe is not None
            else None
        )
        value = owner / total * 100.0 if total > 0 else None
        margin = (
            _ratio_moe(owner, owner_moe, total, total_moe)
            if total > 0
            else None
        )
    return _observation(
        domain=domain,
        metric_id="homeownership_pct",
        metric_name="Homeownership share",
        county_fips=county_fips,
        period=year,
        value=value,
        unit="percent",
        frequency="annual rolling five-year estimate",
        margin_of_error_90=margin,
        numerator=owner,
        denominator=total,
        subgroup_type=subgroup_type,
        subgroup_id=subgroup_id,
        subgroup_label=subgroup_label,
        geography_basis="household residence",
        universe=universe,
        source_ids="CENSUS_ACS5_DETAIL",
        source_releases=source["source_release"],
        retrieved_at=source["retrieved_at"],
        raw_sha256s=source["snapshot_sha256"],
        calculation="owner-occupied units divided by owner- plus renter-occupied units",
        notes=notes,
        reliability_flag=_rate_reliability(value, margin, total),
        comparability_status="comparable_within_table_and_vintage",
    )


def _age_tenure_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    age_bands = {
        "15_24": ("Householder age 15 to 24", (3,), (13,)),
        "25_34": ("Householder age 25 to 34", (4,), (14,)),
        "35_44": ("Householder age 35 to 44", (5,), (15,)),
        "45_54": ("Householder age 45 to 54", (6,), (16,)),
        "55_64": ("Householder age 55 to 64", (7, 8), (17, 18)),
        "65_74": ("Householder age 65 to 74", (9,), (19,)),
        "75_plus": ("Householder age 75 or older", (10, 11), (20, 21)),
    }
    output: list[dict[str, Any]] = []
    for year in ACS_YEARS:
        source = refs[(year, "B25007")]
        for county_fips, row in acs[(year, "B25007")].items():
            for subgroup_id, (label, owner_indices, renter_indices) in age_bands.items():
                output.append(
                    _homeownership_from_pair(
                        row=row,
                        owner_variables=tuple(
                            f"B25007_{index:03d}" for index in owner_indices
                        ),
                        renter_variables=tuple(
                            f"B25007_{index:03d}" for index in renter_indices
                        ),
                        source=source,
                        domain="age_and_tenure",
                        county_fips=county_fips,
                        year=year,
                        subgroup_type="age_of_householder",
                        subgroup_id=subgroup_id,
                        subgroup_label=label,
                        universe="occupied housing units by age of householder",
                        notes="Age bands are aggregated only where stated; ACS vintages overlap.",
                    )
                )
    return output


def _education_tenure_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = {
        "less_than_high_school": ("Less than high school graduate", 3, 8),
        "high_school": ("High school graduate or equivalent", 4, 9),
        "some_college_associate": ("Some college or associate degree", 5, 10),
        "bachelors_plus": ("Bachelor's degree or higher", 6, 11),
    }
    output: list[dict[str, Any]] = []
    for year in ACS_YEARS:
        source = refs[(year, "B25013")]
        for county_fips, row in acs[(year, "B25013")].items():
            for subgroup_id, (label, owner_index, renter_index) in groups.items():
                output.append(
                    _homeownership_from_pair(
                        row=row,
                        owner_variables=(f"B25013_{owner_index:03d}",),
                        renter_variables=(f"B25013_{renter_index:03d}",),
                        source=source,
                        domain="education_and_earnings",
                        county_fips=county_fips,
                        year=year,
                        subgroup_type="educational_attainment_of_householder",
                        subgroup_id=subgroup_id,
                        subgroup_label=label,
                        universe=(
                            "occupied housing units by educational attainment "
                            "of householder"
                        ),
                        notes=(
                            "Householder education is a housing-unit characteristic "
                            "and is not the same universe as individual earnings."
                        ),
                    )
                )
    return output


def _household_tenure_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    groups = {
        "married_with_children": (
            "Married-couple family with own children under 18",
            (5,),
            (18,),
        ),
        "married_no_children": (
            "Married-couple family without own children under 18",
            (6,),
            (19,),
        ),
        "no_spouse_with_children": (
            "Householder with no spouse present and own children under 18",
            (9, 12),
            (22, 25),
        ),
        "other_family_no_children": (
            "Other family without own children under 18",
            (10, 13),
            (23, 26),
        ),
        "nonfamily": ("Nonfamily household", (14,), (27,)),
    }
    output: list[dict[str, Any]] = []
    for year in ACS_YEARS:
        source = refs[(year, "B25115")]
        for county_fips, row in acs[(year, "B25115")].items():
            for subgroup_id, (label, owner_indices, renter_indices) in groups.items():
                output.append(
                    _homeownership_from_pair(
                        row=row,
                        owner_variables=tuple(
                            f"B25115_{index:03d}" for index in owner_indices
                        ),
                        renter_variables=tuple(
                            f"B25115_{index:03d}" for index in renter_indices
                        ),
                        source=source,
                        domain="household_type_and_housing",
                        county_fips=county_fips,
                        year=year,
                        subgroup_type="household_type",
                        subgroup_id=subgroup_id,
                        subgroup_label=label,
                        universe="occupied housing units by household type",
                        notes=(
                            "No-spouse categories combine male and female householders. "
                            "They do not identify marital history or all parenting arrangements."
                        ),
                    )
                )
    return output


def _load_fmr_benchmarks(root: Path, retrieved_at: str) -> tuple[dict[str, float], dict[str, Any]]:
    path = root / "data" / "phase14" / "access" / "exports" / "access_snapshot.csv"
    if not path.is_file():
        raise FileNotFoundError(
            "Version 1.2 access snapshot is required for inherited FY2026 FMR"
        )
    rows = _read_csv(path)
    selected: dict[str, float] = {}
    release = ""
    inherited_retrieved = ""
    for row in rows:
        if row.get("metric_id") == "fmr_2br" and row.get("period") == "FY2026":
            value = _number(row.get("value"))
            if value is not None:
                selected[row["county_fips"]] = value
                release = row.get("source_releases", release)
                inherited_retrieved = row.get("retrieved_at", inherited_retrieved)
    if set(selected) != set(COUNTY_BY_FIPS):
        raise ValueError(f"FY2026 two-bedroom FMR coverage is incomplete: {sorted(selected)}")
    snapshot = {
        "source_id": "HUD_FMR",
        "source_url": SOURCE_REGISTRY["HUD_FMR"]["landing_url"],
        "source_release": release or "HUD-FMR-FY2026-revised",
        "retrieved_at": inherited_retrieved or retrieved_at,
        "snapshot_sha256": _sha256(path),
        "byte_count": path.stat().st_size,
        "snapshot_kind": "inherited_verified_version_1_2_public_export",
        "raw_path": path.relative_to(root).as_posix(),
        "row_count": len(selected),
    }
    return selected, snapshot


def _earnings_observation_set(
    *,
    domain: str,
    county_fips: str,
    year: int,
    subgroup_type: str,
    subgroup_id: str,
    subgroup_label: str,
    sex: str,
    earnings: float | None,
    earnings_moe: float | None,
    fmr: float,
    source: dict[str, Any],
    fmr_source: dict[str, Any],
    universe: str,
) -> list[dict[str, Any]]:
    output = [
        _observation(
            domain=domain,
            metric_id="median_annual_earnings",
            metric_name="Median annual earnings",
            county_fips=county_fips,
            period=year,
            value=earnings,
            unit="current dollars per year",
            frequency="annual rolling five-year estimate",
            margin_of_error_90=earnings_moe,
            subgroup_type=subgroup_type,
            subgroup_id=subgroup_id,
            subgroup_label=subgroup_label,
            sex=sex,
            geography_basis="person residence",
            universe=universe,
            source_ids="CENSUS_ACS5_DETAIL",
            source_releases=source["source_release"],
            retrieved_at=source["retrieved_at"],
            raw_sha256s=source["snapshot_sha256"],
            calculation="published ACS median",
            notes=(
                "Residence-based individual earnings; not household income, "
                "a wage offer, or a workplace-based job measure."
            ),
            reliability_flag=_median_reliability(earnings, earnings_moe),
            comparability_status="current_dollars_and_overlapping_acs_vintages",
        )
    ]
    required_income = fmr * 12.0 / 0.30
    coverage = earnings / required_income * 100.0 if earnings is not None else None
    coverage_moe = (
        earnings_moe / required_income * 100.0 if earnings_moe is not None else None
    )
    gap = fmr - (earnings * 0.30 / 12.0) if earnings is not None else None
    gap_moe = earnings_moe * 0.30 / 12.0 if earnings_moe is not None else None
    combined_release = f"{source['source_release']};{fmr_source['source_release']}"
    combined_hash = f"{source['snapshot_sha256']};{fmr_source['snapshot_sha256']}"
    combined_retrieved = max(source["retrieved_at"], fmr_source["retrieved_at"])
    output.append(
        _observation(
            domain=domain,
            metric_id="earnings_coverage_2br_fmr_pct",
            metric_name="Median earnings coverage of two-bedroom FMR income requirement",
            county_fips=county_fips,
            period=year,
            value=coverage,
            unit="percent",
            frequency="cross-vintage benchmark comparison",
            margin_of_error_90=coverage_moe,
            numerator=earnings,
            denominator=required_income,
            benchmark_value=fmr,
            subgroup_type=subgroup_type,
            subgroup_id=subgroup_id,
            subgroup_label=subgroup_label,
            sex=sex,
            geography_basis="person residence compared with HUD FMR area",
            universe=universe,
            source_ids="CENSUS_ACS5_DETAIL;HUD_FMR",
            source_releases=combined_release,
            retrieved_at=combined_retrieved,
            raw_sha256s=combined_hash,
            calculation="ACS median annual earnings divided by FY2026 2BR FMR times 40",
            notes=(
                "A descriptive benchmark using the conventional 30% threshold. "
                "It does not measure an actual household budget or conclude that a unit is available."
            ),
            reliability_flag=_median_reliability(coverage, coverage_moe),
            comparability_status="acs_current_dollars_compared_with_fy2026_fmr",
        )
    )
    output.append(
        _observation(
            domain=domain,
            metric_id="monthly_gap_to_2br_fmr",
            metric_name="Monthly gap between two-bedroom FMR and 30% of median earnings",
            county_fips=county_fips,
            period=year,
            value=gap,
            unit="current dollars per month",
            frequency="cross-vintage benchmark comparison",
            margin_of_error_90=gap_moe,
            numerator=earnings,
            denominator=required_income,
            benchmark_value=fmr,
            subgroup_type=subgroup_type,
            subgroup_id=subgroup_id,
            subgroup_label=subgroup_label,
            sex=sex,
            geography_basis="person residence compared with HUD FMR area",
            universe=universe,
            source_ids="CENSUS_ACS5_DETAIL;HUD_FMR",
            source_releases=combined_release,
            retrieved_at=combined_retrieved,
            raw_sha256s=combined_hash,
            calculation="FY2026 2BR FMR minus 30% of ACS median annual earnings divided by 12",
            notes=(
                "Positive values mean the FMR benchmark exceeds 30% of median individual earnings. "
                "No work-hour, household-size, or unit-availability assumption is added."
            ),
            reliability_flag=_median_reliability(gap, gap_moe),
            comparability_status="acs_current_dollars_compared_with_fy2026_fmr",
        )
    )
    return output


def _education_earnings_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
    fmr: dict[str, float],
    fmr_source: dict[str, Any],
) -> list[dict[str, Any]]:
    groups = {
        "less_than_high_school": ("Less than high school graduate", 2, 8, 14),
        "high_school": ("High school graduate or equivalent", 3, 9, 15),
        "some_college_associate": ("Some college or associate degree", 4, 10, 16),
        "bachelors": ("Bachelor's degree", 5, 11, 17),
        "graduate_professional": ("Graduate or professional degree", 6, 12, 18),
    }
    output: list[dict[str, Any]] = []
    for year in ACS_YEARS:
        source = refs[(year, "B20004")]
        for county_fips, row in acs[(year, "B20004")].items():
            for subgroup_id, (label, all_index, male_index, female_index) in groups.items():
                for sex, index in (
                    ("all", all_index),
                    ("male", male_index),
                    ("female", female_index),
                ):
                    earnings, earnings_moe = _cell(row, f"B20004_{index:03d}")
                    output.extend(
                        _earnings_observation_set(
                            domain="education_and_earnings",
                            county_fips=county_fips,
                            year=year,
                            subgroup_type="educational_attainment",
                            subgroup_id=subgroup_id,
                            subgroup_label=label,
                            sex=sex,
                            earnings=earnings,
                            earnings_moe=earnings_moe,
                            fmr=fmr[county_fips],
                            source=source,
                            fmr_source=fmr_source,
                            universe=(
                                "population age 25 and over with earnings, "
                                "by sex and educational attainment"
                            ),
                        )
                    )
    return output


def _occupation_earnings_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
    fmr: dict[str, float],
    fmr_source: dict[str, Any],
) -> list[dict[str, Any]]:
    groups = {
        "management_business_science_arts": (
            "Management, business, science, and arts occupations",
            2,
        ),
        "service": ("Service occupations", 18),
        "sales_office": ("Sales and office occupations", 26),
        "natural_resources_construction_maintenance": (
            "Natural resources, construction, and maintenance occupations",
            29,
        ),
        "production_transportation_material_moving": (
            "Production, transportation, and material moving occupations",
            33,
        ),
    }
    output: list[dict[str, Any]] = []
    for year in ACS_YEARS:
        source = refs[(year, "B24011")]
        for county_fips, row in acs[(year, "B24011")].items():
            for subgroup_id, (label, index) in groups.items():
                earnings, earnings_moe = _cell(row, f"B24011_{index:03d}")
                output.extend(
                    _earnings_observation_set(
                        domain="occupation_and_affordability",
                        county_fips=county_fips,
                        year=year,
                        subgroup_type="broad_occupation",
                        subgroup_id=subgroup_id,
                        subgroup_label=label,
                        sex="all",
                        earnings=earnings,
                        earnings_moe=earnings_moe,
                        fmr=fmr[county_fips],
                        source=source,
                        fmr_source=fmr_source,
                        universe=(
                            "civilian employed population age 16 and over, "
                            "classified by occupation and residence"
                        ),
                    )
                )
    return output


def _transportation_observations(
    acs: dict[tuple[int, str], dict[str, dict[str, str]]],
    refs: dict[tuple[int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    commute_groups = (
        (
            "commute_under_30_minutes_pct",
            "Workers commuting under 30 minutes",
            tuple(range(2, 8)),
        ),
        (
            "commute_30_to_59_minutes_pct",
            "Workers commuting 30 to 59 minutes",
            tuple(range(8, 12)),
        ),
        (
            "commute_60_plus_minutes_pct",
            "Workers commuting 60 minutes or more",
            (12, 13),
        ),
    )
    for year in ACS_YEARS:
        time_source = refs[(year, "B08303")]
        mode_source = refs[(year, "B08006")]
        vehicle_source = refs[(year, "B25044")]
        for county_fips in COUNTY_BY_FIPS:
            time_row = acs[(year, "B08303")][county_fips]
            for metric_id, name, indices in commute_groups:
                output.append(
                    _rate_observation(
                        row=time_row,
                        numerator_variables=tuple(
                            f"B08303_{index:03d}" for index in indices
                        ),
                        denominator_variables=("B08303_001",),
                        source=time_source,
                        domain="commuting_and_job_access",
                        metric_id=metric_id,
                        metric_name=name,
                        county_fips=county_fips,
                        period=year,
                        unit="percent",
                        frequency="annual rolling five-year estimate",
                        geography_basis="person residence",
                        universe="workers age 16 and over who did not work from home",
                        source_ids="CENSUS_ACS5_DETAIL",
                        source_releases=time_source["source_release"],
                        calculation="selected travel-time categories divided by B08303 total",
                        notes=(
                            "Travel time excludes people who worked from home. "
                            "Consecutive ACS five-year vintages overlap."
                        ),
                        comparability_status="comparable_within_series",
                    )
                )
            mode_row = acs[(year, "B08006")][county_fips]
            for metric_id, name, numerator in (
                ("worked_from_home_pct", "Workers working from home", "B08006_017"),
                (
                    "public_transport_commute_pct",
                    "Workers using public transportation",
                    "B08006_008",
                ),
            ):
                output.append(
                    _rate_observation(
                        row=mode_row,
                        numerator_variables=(numerator,),
                        denominator_variables=("B08006_001",),
                        source=mode_source,
                        domain="commuting_and_job_access",
                        metric_id=metric_id,
                        metric_name=name,
                        county_fips=county_fips,
                        period=year,
                        unit="percent",
                        frequency="annual rolling five-year estimate",
                        geography_basis="person residence",
                        universe="workers age 16 and over",
                        source_ids="CENSUS_ACS5_DETAIL",
                        source_releases=mode_source["source_release"],
                        calculation=f"{numerator} divided by B08006_001",
                        notes="Means of transportation describe the usual commute in the ACS reference period.",
                        comparability_status="comparable_within_series",
                    )
                )
            vehicle_row = acs[(year, "B25044")][county_fips]
            for tenure, numerator, denominator in (
                ("owner", "B25044_003", "B25044_002"),
                ("renter", "B25044_010", "B25044_009"),
            ):
                output.append(
                    _rate_observation(
                        row=vehicle_row,
                        numerator_variables=(numerator,),
                        denominator_variables=(denominator,),
                        source=vehicle_source,
                        domain="transportation_and_housing",
                        metric_id="zero_vehicle_households_pct",
                        metric_name="Occupied units with no vehicle available",
                        county_fips=county_fips,
                        period=year,
                        unit="percent",
                        frequency="annual rolling five-year estimate",
                        tenure=tenure,
                        geography_basis="household residence",
                        universe=f"{tenure}-occupied housing units",
                        source_ids="CENSUS_ACS5_DETAIL",
                        source_releases=vehicle_source["source_release"],
                        calculation=f"{numerator} divided by {denominator}",
                        notes=(
                            "Vehicle availability does not identify transit quality, "
                            "household preference, disability, or affordability."
                        ),
                        comparability_status="comparable_within_series",
                    )
                )
    return output


def _lodes_url(year: int, part: str) -> str:
    return (
        "https://lehd.ces.census.gov/data/lodes/LODES8/ca/od/"
        f"ca_od_{part}_JT01_{year}.csv.gz"
    )


def _build_lodes(
    *,
    root: Path,
    raw_dir: Path,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    fields = ("S000", "SA01", "SA02", "SA03", "SE01", "SE02", "SE03")
    for year in LODES_YEARS:
        aggregate = {
            county_fips: defaultdict(int)
            for county_fips in COUNTY_BY_FIPS
        }
        year_snapshots: list[dict[str, Any]] = []
        for part in ("main", "aux"):
            url = _lodes_url(year, part)
            path = raw_dir / "CENSUS_LEHD_LODES" / retrieved_at[:10] / Path(url).name
            _download_file(url, path)
            row_count = 0
            with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                expected = {
                    "w_geocode",
                    "h_geocode",
                    "S000",
                    "SA01",
                    "SA02",
                    "SA03",
                    "SE01",
                    "SE02",
                    "SE03",
                }
                if not reader.fieldnames or not expected <= set(reader.fieldnames):
                    raise ValueError(f"unexpected LODES header for {url}: {reader.fieldnames}")
                for row in reader:
                    row_count += 1
                    work_county = str(row["w_geocode"])[:5]
                    if work_county not in aggregate:
                        continue
                    values = {field: int(row[field]) for field in fields}
                    bucket = aggregate[work_county]
                    for field, value in values.items():
                        bucket[field] += value
                    if part == "main":
                        if str(row["h_geocode"])[:5] == work_county:
                            bucket["same_county"] += values["S000"]
                        else:
                            bucket["other_california"] += values["S000"]
                    else:
                        bucket["outside_california"] += values["S000"]
            snapshot = {
                "source_id": "CENSUS_LEHD_LODES",
                "source_url": url,
                "source_release": f"LODES8-OD-JT01-{year}-{part}",
                "retrieved_at": retrieved_at,
                "snapshot_sha256": _sha256(path),
                "byte_count": path.stat().st_size,
                "snapshot_kind": "official_compressed_csv",
                "raw_path": path.relative_to(root).as_posix(),
                "row_count": row_count,
            }
            snapshots.append(snapshot)
            year_snapshots.append(snapshot)
        release = ";".join(row["source_release"] for row in year_snapshots)
        hashes = ";".join(row["snapshot_sha256"] for row in year_snapshots)
        for county_fips, values in aggregate.items():
            total = values["S000"]
            summary = {
                "county_fips": county_fips,
                "county_name": COUNTY_BY_FIPS[county_fips],
                "period": year,
                "primary_jobs": total,
                "local_resident_primary_jobs": values["same_county"],
                "other_california_inbound_primary_jobs": values["other_california"],
                "outside_california_inbound_primary_jobs": values["outside_california"],
                "age_total": values["SA01"] + values["SA02"] + values["SA03"],
                "earnings_total": values["SE01"] + values["SE02"] + values["SE03"],
            }
            summaries.append(summary)
            count_metrics = (
                ("primary_jobs", "Primary jobs located in county", total),
                (
                    "local_resident_primary_jobs",
                    "Primary jobs held by same-county residents",
                    values["same_county"],
                ),
                (
                    "other_california_inbound_primary_jobs",
                    "Primary jobs held by residents of another California county",
                    values["other_california"],
                ),
                (
                    "outside_california_inbound_primary_jobs",
                    "Primary jobs held by residents outside California",
                    values["outside_california"],
                ),
            )
            for metric_id, name, value in count_metrics:
                observations.append(
                    _observation(
                        domain="commuting_and_job_access",
                        metric_id=metric_id,
                        metric_name=name,
                        county_fips=county_fips,
                        period=year,
                        value=value,
                        unit="primary jobs",
                        frequency="annual",
                        geography_basis="workplace county with home-work relation",
                        universe="LODES JT01 primary jobs located in the county",
                        source_ids="CENSUS_LEHD_LODES",
                        source_releases=release,
                        retrieved_at=retrieved_at,
                        raw_sha256s=hashes,
                        calculation="sum of LODES OD primary-job records",
                        notes=(
                            "Jobs are not people or households. LODES values are "
                            "noise-infused for disclosure protection and may not equal ACS estimates."
                        ),
                        reliability_flag="administrative_noise_applied",
                        comparability_status="lodes8_jt01_within_series",
                    )
                )
            rate_metrics = (
                (
                    "local_resident_job_share_pct",
                    "Same-county resident share of primary jobs",
                    values["same_county"],
                ),
                (
                    "inbound_job_share_pct",
                    "Inbound share of primary jobs",
                    values["other_california"] + values["outside_california"],
                ),
                (
                    "low_monthly_earnings_job_share_pct",
                    "Primary jobs paying $1,250 per month or less",
                    values["SE01"],
                ),
                (
                    "middle_monthly_earnings_job_share_pct",
                    "Primary jobs paying $1,251 to $3,333 per month",
                    values["SE02"],
                ),
                (
                    "high_monthly_earnings_job_share_pct",
                    "Primary jobs paying more than $3,333 per month",
                    values["SE03"],
                ),
                (
                    "age_29_or_younger_job_share_pct",
                    "Primary jobs held by people age 29 or younger",
                    values["SA01"],
                ),
                (
                    "age_30_to_54_job_share_pct",
                    "Primary jobs held by people age 30 to 54",
                    values["SA02"],
                ),
                (
                    "age_55_plus_job_share_pct",
                    "Primary jobs held by people age 55 or older",
                    values["SA03"],
                ),
            )
            for metric_id, name, numerator in rate_metrics:
                value = numerator / total * 100.0 if total > 0 else None
                observations.append(
                    _observation(
                        domain="commuting_and_job_access",
                        metric_id=metric_id,
                        metric_name=name,
                        county_fips=county_fips,
                        period=year,
                        value=value,
                        unit="percent",
                        frequency="annual",
                        numerator=numerator,
                        denominator=total,
                        geography_basis="workplace county with home-work relation",
                        universe="LODES JT01 primary jobs located in the county",
                        source_ids="CENSUS_LEHD_LODES",
                        source_releases=release,
                        retrieved_at=retrieved_at,
                        raw_sha256s=hashes,
                        calculation="selected LODES job component divided by S000 primary jobs",
                        notes=(
                            "LODES earnings bands are monthly job-earnings categories, "
                            "not annual worker income or ACS median earnings."
                        ),
                        reliability_flag="administrative_noise_applied",
                        comparability_status="lodes8_jt01_within_series",
                    )
                )
    return observations, snapshots, summaries


def _metric_catalog(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    for row in observations:
        key = (row["domain"], row["metric_id"])
        if key not in catalog:
            catalog[key] = {
                "domain": row["domain"],
                "metric_id": row["metric_id"],
                "metric_name": row["metric_name"],
                "unit": row["unit"],
                "subgroup_dimension": row["subgroup_type"] or row["tenure"] or "none",
                "geography_basis": row["geography_basis"],
                "history_start": min(
                    candidate["period"]
                    for candidate in observations
                    if candidate["domain"] == row["domain"]
                    and candidate["metric_id"] == row["metric_id"]
                ),
                "interpretation": METRIC_INTERPRETATIONS.get(
                    row["metric_id"], row["notes"]
                ),
            }
    return [catalog[key] for key in sorted(catalog)]


def _source_registry_rows() -> list[dict[str, Any]]:
    return [
        {"source_id": source_id, **metadata}
        for source_id, metadata in SOURCE_REGISTRY.items()
    ]


def _observation_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["domain"],
        row["metric_id"],
        row["county_fips"],
        row["period"],
        row["subgroup_type"],
        row["subgroup_id"],
        row["tenure"],
        row["sex"],
    )


def _quality_checks(
    *,
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    lodes_summaries: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, observed: Any, expected: Any, detail: str) -> None:
        checks.append(
            {
                "check_id": check_id,
                "passed": 1 if passed else 0,
                "observed": observed,
                "expected": expected,
                "detail": detail,
            }
        )

    counties = {row["county_fips"] for row in observations}
    add(
        "nine_county_coverage",
        counties == set(COUNTY_BY_FIPS),
        len(counties),
        9,
        "Every public observation must resolve to a Bay Area county.",
    )
    domains = {row["domain"] for row in observations}
    add(
        "required_domain_coverage",
        domains == REQUIRED_EQUITY_DOMAINS,
        len(domains),
        len(REQUIRED_EQUITY_DOMAINS),
        "All seven Version 1.4 connection domains are required.",
    )
    keys = [_observation_key(row) for row in observations]
    add(
        "observation_grain_unique",
        len(keys) == len(set(keys)),
        len(keys) - len(set(keys)),
        0,
        "No duplicate county-period-subgroup-tenure-sex metric rows.",
    )
    acs_rows = [
        row for row in observations if "CENSUS_ACS5_DETAIL" in row["source_ids"]
    ]
    add(
        "acs_moe_preserved",
        all(
            row["value"] is None or row["margin_of_error_90"] is not None
            for row in acs_rows
        ),
        sum(
            1
            for row in acs_rows
            if row["value"] is not None and row["margin_of_error_90"] is not None
        ),
        sum(1 for row in acs_rows if row["value"] is not None),
        "All nonmissing ACS and ACS-derived estimates retain a 90% margin of error.",
    )
    percentage_rows = [
        row
        for row in acs_rows
        if row["unit"] == "percent" and row["metric_id"] != "monthly_gap_to_2br_fmr"
    ]
    add(
        "subgroup_denominators_positive",
        all(
            row["value"] is None
            or row["denominator"] is None
            or row["denominator"] > 0
            for row in percentage_rows
        ),
        sum(1 for row in percentage_rows if row["denominator"] and row["denominator"] > 0),
        sum(1 for row in percentage_rows if row["value"] is not None),
        "Published subgroup rates expose a positive denominator.",
    )
    race_tenure = [
        row
        for row in observations
        if row["domain"] == "race_ethnicity_and_housing"
        and row["metric_id"] == "homeownership_pct"
    ]
    race_cells = defaultdict(set)
    for row in race_tenure:
        race_cells[(row["county_fips"], row["period"])].add(row["subgroup_id"])
    add(
        "race_category_coverage",
        len(race_cells) == 9 * len(ACS_YEARS)
        and all(values == {item[0] for item in RACE_GROUPS.values()} for values in race_cells.values()),
        len(race_tenure),
        9 * len(ACS_YEARS) * len(RACE_GROUPS),
        "Nine documented race/Hispanic-origin table iterations are retained without add-up logic.",
    )
    burden_years = {
        row["period"]
        for row in observations
        if row["metric_id"].startswith("housing_cost_burden")
    }
    add(
        "cost_burden_table_history",
        burden_years == {str(year) for year in ACS_COST_BURDEN_YEARS},
        ",".join(sorted(burden_years)),
        "2023,2024",
        "B25140A-I begins with the 2023 table introduction.",
    )
    add(
        "lodes_county_year_coverage",
        len(lodes_summaries) == 9 * len(LODES_YEARS),
        len(lodes_summaries),
        9 * len(LODES_YEARS),
        "Each LODES primary-job vintage covers all nine workplace counties.",
    )
    flow_failures = [
        row
        for row in lodes_summaries
        if row["primary_jobs"]
        != row["local_resident_primary_jobs"]
        + row["other_california_inbound_primary_jobs"]
        + row["outside_california_inbound_primary_jobs"]
    ]
    add(
        "lodes_flow_identity",
        not flow_failures,
        len(flow_failures),
        0,
        "Workplace primary jobs reconcile to same-county, other-California, and outside-California homes.",
    )
    component_failures = [
        row
        for row in lodes_summaries
        if row["primary_jobs"] != row["age_total"]
        or row["primary_jobs"] != row["earnings_total"]
    ]
    add(
        "lodes_component_identity",
        not component_failures,
        len(component_failures),
        0,
        "LODES age and earnings components each reconcile to S000.",
    )
    fmr_failures = 0
    for row in observations:
        if row["metric_id"] not in {
            "earnings_coverage_2br_fmr_pct",
            "monthly_gap_to_2br_fmr",
        } or row["value"] is None:
            continue
        earnings = float(row["numerator"])
        fmr = float(row["benchmark_value"])
        if row["metric_id"] == "earnings_coverage_2br_fmr_pct":
            expected = earnings / (fmr * 40.0) * 100.0
        else:
            expected = fmr - earnings * 0.30 / 12.0
        if not math.isclose(float(row["value"]), expected, rel_tol=1e-8, abs_tol=1e-7):
            fmr_failures += 1
    add(
        "fmr_benchmark_recomputes",
        fmr_failures == 0,
        fmr_failures,
        0,
        "Every education and occupation benchmark recomputes from ACS earnings and FY2026 2BR FMR.",
    )
    snapshot_failures = [
        row
        for row in snapshots
        if len(str(row["snapshot_sha256"])) != 64
        or int(row["byte_count"]) <= 0
        or int(row["row_count"]) <= 0
    ]
    add(
        "source_snapshot_integrity",
        not snapshot_failures,
        len(snapshot_failures),
        0,
        "Every accepted official release has a SHA-256, byte count, and row count.",
    )
    boundaries = config["method"]
    add(
        "interpretation_boundaries",
        not any(
            boundaries[key]
            for key in (
                "race_categories_additive",
                "county_ranking",
                "equity_score",
                "causal_inference",
                "automated_narrative",
            )
        ),
        "all prohibited outputs false",
        "all prohibited outputs false",
        "No score, ranking, causal claim, additive race total, or automated narrative is produced.",
    )
    add(
        "reliability_flags_present",
        all(bool(row["reliability_flag"]) for row in observations),
        sum(1 for row in observations if row["reliability_flag"]),
        len(observations),
        "Every observation carries either a project uncertainty flag or the LODES disclosure-protection flag.",
    )
    return checks


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {_camel(key): value for key, value in row.items()}


def _write_database(
    path: Path,
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.sqlite")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as connection:
        connection.execute(
            """
            CREATE TABLE equity_observation (
                domain TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                metric_name TEXT NOT NULL,
                county_fips TEXT NOT NULL,
                county_name TEXT NOT NULL,
                period TEXT NOT NULL,
                value REAL,
                unit TEXT NOT NULL,
                margin_of_error_90 REAL,
                numerator REAL,
                denominator REAL,
                benchmark_value REAL,
                subgroup_type TEXT NOT NULL,
                subgroup_id TEXT NOT NULL,
                subgroup_label TEXT NOT NULL,
                tenure TEXT NOT NULL,
                sex TEXT NOT NULL,
                geography_basis TEXT NOT NULL,
                universe TEXT NOT NULL,
                source_ids TEXT NOT NULL,
                source_releases TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                raw_sha256s TEXT NOT NULL,
                calculation TEXT NOT NULL,
                notes TEXT NOT NULL,
                reliability_flag TEXT NOT NULL,
                comparability_status TEXT NOT NULL,
                PRIMARY KEY (
                    domain, metric_id, county_fips, period,
                    subgroup_type, subgroup_id, tenure, sex
                )
            )
            """
        )
        columns = [
            "domain",
            "metric_id",
            "metric_name",
            "county_fips",
            "county_name",
            "period",
            "value",
            "unit",
            "margin_of_error_90",
            "numerator",
            "denominator",
            "benchmark_value",
            "subgroup_type",
            "subgroup_id",
            "subgroup_label",
            "tenure",
            "sex",
            "geography_basis",
            "universe",
            "source_ids",
            "source_releases",
            "retrieved_at",
            "raw_sha256s",
            "calculation",
            "notes",
            "reliability_flag",
            "comparability_status",
        ]
        connection.executemany(
            f"INSERT INTO equity_observation ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [tuple(row[column] for column in columns) for row in observations],
        )
        connection.execute(
            """
            CREATE TABLE source_snapshot (
                source_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_release TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                byte_count INTEGER NOT NULL,
                snapshot_kind TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                PRIMARY KEY (source_id, source_release, snapshot_sha256)
            )
            """
        )
        connection.executemany(
            f"INSERT INTO source_snapshot ({','.join(SNAPSHOT_FIELDS)}) VALUES ({','.join('?' for _ in SNAPSHOT_FIELDS)})",
            [tuple(row[field] for field in SNAPSHOT_FIELDS) for row in snapshots],
        )
        connection.execute(
            """
            CREATE TABLE quality_check (
                check_id TEXT PRIMARY KEY,
                passed INTEGER NOT NULL,
                observed TEXT NOT NULL,
                expected TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO quality_check VALUES (?, ?, ?, ?, ?)",
            [
                (
                    row["check_id"],
                    row["passed"],
                    str(row["observed"]),
                    str(row["expected"]),
                    row["detail"],
                )
                for row in checks
            ],
        )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("housing equity database integrity check failed")
    os.replace(temporary, path)


def _write_public_outputs(
    *,
    root: Path,
    output: Path,
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    config: dict[str, Any],
    built_at: str,
) -> dict[str, Any]:
    exports = output / "exports"
    public = output / "public"
    observations = sorted(
        observations,
        key=lambda row: (
            row["domain"],
            row["county_fips"],
            row["period"],
            row["subgroup_type"],
            row["subgroup_id"],
            row["tenure"],
            row["sex"],
            row["metric_id"],
        ),
    )
    snapshots = sorted(
        snapshots,
        key=lambda row: (
            row["source_id"],
            row["source_release"],
            row["snapshot_sha256"],
        ),
    )
    _write_csv(exports / "equity_observations.csv", observations, OBSERVATION_FIELDS)
    latest = [
        row
        for row in observations
        if (
            "CENSUS_LEHD_LODES" in row["source_ids"]
            and row["period"] == str(LATEST_LODES_YEAR)
        )
        or (
            "CENSUS_LEHD_LODES" not in row["source_ids"]
            and row["period"] == str(LATEST_ACS_YEAR)
        )
    ]
    _write_csv(exports / "equity_snapshot.csv", latest, OBSERVATION_FIELDS)
    _write_csv(
        exports / "race_housing.csv",
        [row for row in latest if row["domain"] == "race_ethnicity_and_housing"],
        OBSERVATION_FIELDS,
    )
    _write_csv(
        exports / "education_occupation_affordability.csv",
        [
            row
            for row in latest
            if row["domain"] in {
                "education_and_earnings",
                "occupation_and_affordability",
            }
        ],
        OBSERVATION_FIELDS,
    )
    _write_csv(
        exports / "commuting_connections.csv",
        [
            row
            for row in latest
            if row["domain"] in {
                "commuting_and_job_access",
                "transportation_and_housing",
            }
        ],
        OBSERVATION_FIELDS,
    )
    _write_csv(exports / "source_snapshots.csv", snapshots, SNAPSHOT_FIELDS)
    freshness = [
        {
            "source_id": row["source_id"],
            "source_release": row["source_release"],
            "retrieved_at": row["retrieved_at"],
            "snapshot_sha256": row["snapshot_sha256"],
            "byte_count": row["byte_count"],
            "row_count": row["row_count"],
            "status": "accepted_official_release",
        }
        for row in snapshots
    ]
    _write_csv(exports / "source_freshness.csv", freshness)
    _write_csv(
        exports / "quality_checks.csv",
        checks,
        ("check_id", "passed", "observed", "expected", "detail"),
    )
    metric_catalog = _metric_catalog(observations)
    _write_csv(
        root / "metadata" / "housing_equity_indicator_catalog.csv",
        metric_catalog,
    )
    _write_csv(
        root / "metadata" / "housing_equity_source_registry.csv",
        _source_registry_rows(),
    )
    reliability_counts: dict[str, int] = defaultdict(int)
    for row in observations:
        reliability_counts[row["reliability_flag"]] += 1
    payload = {
        "product": "Housing Equity & Economic Connections",
        "version": PHASE14_EQUITY_VERSION,
        "inherits": "Housing Production & Policy 1.3.0",
        "builtAt": built_at,
        "countyCount": len(COUNTY_BY_FIPS),
        "domainCount": len(REQUIRED_EQUITY_DOMAINS),
        "metricCount": len(metric_catalog),
        "observationCount": len(observations),
        "sourceSnapshotCount": len(snapshots),
        "counties": [
            {"countyFips": fips, "countyName": name}
            for fips, name in COUNTY_BY_FIPS.items()
        ],
        "domains": sorted(REQUIRED_EQUITY_DOMAINS),
        "metricCatalog": [_public_row(row) for row in metric_catalog],
        "observations": [_public_row(row) for row in observations],
        "sourceRegistry": [
            _public_row(row) for row in _source_registry_rows()
        ],
        "reliabilityFlagCounts": dict(sorted(reliability_counts.items())),
        "interpretationBoundaries": {
            "raceCategoriesAdditive": False,
            "countyRankingProduced": False,
            "equityScoreProduced": False,
            "causalInferenceProduced": False,
            "acsAndLodesUniversesMerged": False,
            "lodesJobsTreatedAsPeople": False,
            "automatedNarrativeProduced": False,
            "acsMarginsOfErrorRetained": True,
            "subgroupDenominatorsPublished": True,
        },
        "publicationBoundary": {
            "automatedNarrative": False,
            "namedHumanReviewRequired": True,
            "automaticPublicDeployment": False,
            "phase10ReportStatus": "human_approval_hold",
        },
        "methodNotes": [
            "ACS five-year vintages overlap and are not independent annual samples.",
            "Race and Hispanic-origin table iterations overlap and are not additive.",
            "Project reliability flags are transparent screening heuristics, not Census designations.",
            "LODES JT01 records primary jobs, not households, and uses workplace-based geography.",
            "Earnings-to-rent comparisons are descriptive benchmarks, not household budgets or availability measures.",
        ],
    }
    _write_json(public / "housing-equity-connections-data.json", payload)
    return payload


def _manifest_files(root: Path) -> list[dict[str, Any]]:
    files = []
    for relative in REQUIRED_EQUITY_FILES:
        path = root / relative
        if path.is_file():
            files.append(
                {
                    "path": relative,
                    "sha256": _sha256(path),
                    "byte_count": path.stat().st_size,
                }
            )
    return files


def _write_manifest(
    *,
    root: Path,
    output: Path,
    built_at: str,
    payload: dict[str, Any],
    snapshots: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> Path:
    manifest_path = output / "phase14_v1_4_manifest.json"
    manifest = {
        "product": payload["product"],
        "version": PHASE14_EQUITY_VERSION,
        "inherits": payload["inherits"],
        "built_at": built_at,
        "county_count": payload["countyCount"],
        "domain_count": payload["domainCount"],
        "metric_count": payload["metricCount"],
        "observation_count": payload["observationCount"],
        "source_count": len(SOURCE_REGISTRY),
        "source_snapshot_count": len(snapshots),
        "quality_check_count": len(checks),
        "quality_check_failures": sum(1 for row in checks if row["passed"] != 1),
        "files": _manifest_files(root),
        "publication": {
            "site_status": "candidate_pending_external_verification",
            "github_status": "candidate_pending_merge",
            "automatic_narrative": False,
            "phase10_report_status": "human_approval_hold",
        },
    }
    _write_json(manifest_path, manifest)
    return manifest_path


def build_phase14_equity(
    *,
    built_at: str | None = None,
    refresh: bool = True,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    output = root / "data" / "phase14" / "equity"
    manifest_path = output / "phase14_v1_4_manifest.json"
    if not refresh:
        verification = verify_phase14_equity(manifest_path=manifest_path, root=root)
        return {"mode": "offline", "verification": verification}

    built_at = built_at or _utc_now()
    if not built_at.endswith("Z"):
        built_at = (
            datetime.fromisoformat(built_at.replace("Z", "+00:00"))
            .astimezone(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    config_path = root / "config" / "phase14_housing_equity.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("version") != PHASE14_EQUITY_VERSION:
        raise ValueError("Version 1.4 configuration identity mismatch")

    raw_dir = output / "raw"
    observations: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    acs: dict[tuple[int, str], dict[str, dict[str, str]]] = {}
    refs: dict[tuple[int, str], dict[str, Any]] = {}

    for year in ACS_YEARS:
        groups = list(COMMON_ACS_GROUPS)
        if year in ACS_COST_BURDEN_YEARS:
            groups.extend(COST_ACS_GROUPS)
        for group in groups:
            rows, snapshot = _fetch_acs_group(
                root=root,
                raw_dir=raw_dir,
                year=year,
                group=group,
                retrieved_at=built_at,
            )
            acs[(year, group)] = rows
            refs[(year, group)] = _source_reference(snapshot)
            snapshots.append(snapshot)

    fmr, fmr_source = _load_fmr_benchmarks(root, built_at)
    snapshots.append(fmr_source)
    observations.extend(_race_tenure_observations(acs, refs))
    observations.extend(_race_burden_observations(acs, refs))
    observations.extend(_age_tenure_observations(acs, refs))
    observations.extend(_education_tenure_observations(acs, refs))
    observations.extend(_household_tenure_observations(acs, refs))
    observations.extend(
        _education_earnings_observations(acs, refs, fmr, fmr_source)
    )
    observations.extend(
        _occupation_earnings_observations(acs, refs, fmr, fmr_source)
    )
    observations.extend(_transportation_observations(acs, refs))
    lodes_observations, lodes_snapshots, lodes_summaries = _build_lodes(
        root=root,
        raw_dir=raw_dir,
        retrieved_at=built_at,
    )
    observations.extend(lodes_observations)
    snapshots.extend(lodes_snapshots)

    checks = _quality_checks(
        observations=observations,
        snapshots=snapshots,
        lodes_summaries=lodes_summaries,
        config=config,
    )
    payload = _write_public_outputs(
        root=root,
        output=output,
        observations=observations,
        snapshots=snapshots,
        checks=checks,
        config=config,
        built_at=built_at,
    )
    _write_database(
        output / "housing_equity.sqlite",
        observations,
        snapshots,
        checks,
    )
    _write_manifest(
        root=root,
        output=output,
        built_at=built_at,
        payload=payload,
        snapshots=snapshots,
        checks=checks,
    )
    verification = verify_phase14_equity(manifest_path=manifest_path, root=root)
    return {
        "mode": "live",
        "built_at": built_at,
        "observation_count": len(observations),
        "metric_count": payload["metricCount"],
        "source_snapshot_count": len(snapshots),
        "quality_checks": {
            "passing": sum(1 for row in checks if row["passed"] == 1),
            "total": len(checks),
        },
        "verification": verification,
    }


def verify_phase14_equity(
    *,
    manifest_path: Path | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    manifest_path = manifest_path or (
        root / "data" / "phase14" / "equity" / "phase14_v1_4_manifest.json"
    )
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append(
            {"check_id": check_id, "passed": bool(passed), "detail": detail}
        )

    add(
        "version_1_3_inheritance",
        verify_phase14_production()["complete"],
        "The verified Version 1.3 public evidence layer remains intact.",
    )
    missing = [
        relative for relative in REQUIRED_EQUITY_FILES if not (root / relative).is_file()
    ]
    add(
        "required_public_files",
        not missing,
        "missing: " + ", ".join(missing) if missing else "all required files exist",
    )
    if not manifest_path.is_file():
        add("manifest_identity", False, "Version 1.4 manifest is missing")
        return {
            "version": PHASE14_EQUITY_VERSION,
            "total": len(checks),
            "passing": sum(row["passed"] for row in checks),
            "failed": [row["check_id"] for row in checks if not row["passed"]],
            "checks": checks,
            "complete": False,
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    add(
        "manifest_identity",
        manifest.get("version") == PHASE14_EQUITY_VERSION,
        f"manifest version {manifest.get('version')}",
    )
    output = root / "data" / "phase14" / "equity"
    observations = _read_csv(output / "exports" / "equity_observations.csv")
    quality = _read_csv(output / "exports" / "quality_checks.csv")
    snapshots = _read_csv(output / "exports" / "source_snapshots.csv")
    payload = json.loads(
        (output / "public" / "housing-equity-connections-data.json").read_text(
            encoding="utf-8"
        )
    )
    add(
        "observation_count",
        len(observations) == manifest.get("observation_count") == payload.get("observationCount"),
        f"{len(observations)} rows",
    )
    add(
        "county_and_domain_scope",
        {row["county_fips"] for row in observations} == set(COUNTY_BY_FIPS)
        and {row["domain"] for row in observations} == REQUIRED_EQUITY_DOMAINS,
        "nine counties and seven domains",
    )
    keys = [
        (
            row["domain"],
            row["metric_id"],
            row["county_fips"],
            row["period"],
            row["subgroup_type"],
            row["subgroup_id"],
            row["tenure"],
            row["sex"],
        )
        for row in observations
    ]
    add(
        "observation_grain",
        len(keys) == len(set(keys)),
        f"{len(keys) - len(set(keys))} duplicate keys",
    )
    acs_nonmissing = [
        row
        for row in observations
        if "CENSUS_ACS5_DETAIL" in row["source_ids"] and row["value"] != ""
    ]
    add(
        "acs_uncertainty",
        all(row["margin_of_error_90"] != "" for row in acs_nonmissing),
        f"{sum(row['margin_of_error_90'] != '' for row in acs_nonmissing)}/{len(acs_nonmissing)} ACS rows retain MOE",
    )
    burden_years = {
        row["period"]
        for row in observations
        if row["metric_id"].startswith("housing_cost_burden")
    }
    add(
        "table_history_boundaries",
        burden_years == {"2023", "2024"},
        f"B25140 years {sorted(burden_years)}",
    )
    add(
        "quality_checks",
        bool(quality) and all(row["passed"] == "1" for row in quality),
        f"{sum(row['passed'] == '1' for row in quality)}/{len(quality)} passed",
    )
    add(
        "source_lineage",
        len(snapshots) == manifest.get("source_snapshot_count")
        and all(
            len(row["snapshot_sha256"]) == 64
            and int(row["byte_count"]) > 0
            and int(row["row_count"]) > 0
            for row in snapshots
        ),
        f"{len(snapshots)} registered snapshots",
    )
    boundaries = payload["interpretationBoundaries"]
    add(
        "no_score_ranking_or_causation",
        not boundaries["raceCategoriesAdditive"]
        and not boundaries["countyRankingProduced"]
        and not boundaries["equityScoreProduced"]
        and not boundaries["causalInferenceProduced"]
        and not boundaries["acsAndLodesUniversesMerged"]
        and not boundaries["automatedNarrativeProduced"],
        "all prohibited outputs remain false",
    )
    hash_failures = []
    for record in manifest.get("files", []):
        path = root / record["path"]
        if not path.is_file() or _sha256(path) != record["sha256"]:
            hash_failures.append(record["path"])
    add(
        "manifest_hashes",
        not hash_failures,
        "hash failures: " + ", ".join(hash_failures)
        if hash_failures
        else f"{len(manifest.get('files', []))} file hashes verified",
    )
    workflow = (
        root / ".github" / "workflows" / "housing-equity-update.yml"
    ).read_text(encoding="utf-8")
    add(
        "responsible_refresh_workflow",
        "No public deployment is performed" in workflow
        and "contents: read" in workflow
        and "upload-artifact" in workflow,
        "scheduled refresh creates a review artifact and cannot deploy",
    )
    add(
        "publication_boundary",
        payload["publicationBoundary"]["namedHumanReviewRequired"]
        and not payload["publicationBoundary"]["automaticPublicDeployment"]
        and payload["publicationBoundary"]["phase10ReportStatus"]
        == "human_approval_hold",
        "named-human review and Phase 10 hold preserved",
    )
    failed = [row["check_id"] for row in checks if not row["passed"]]
    return {
        "version": PHASE14_EQUITY_VERSION,
        "total": len(checks),
        "passing": len(checks) - len(failed),
        "failed": failed,
        "checks": checks,
        "complete": not failed,
    }
