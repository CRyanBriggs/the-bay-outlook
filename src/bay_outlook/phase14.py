from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import COUNTY_BY_FIPS, FIPS_BY_COUNTY, PROJECT_ROOT
from .storage import USER_AGENT, save_snapshot, sha256_bytes


PHASE14_VERSION = "1.1.0"
ACS_YEARS = (2021, 2022, 2023, 2024)
BPS_YEARS = (2021, 2022, 2023, 2024, 2025)
REDFIN_START_PERIOD = "2021-01-01"

REQUIRED_DOMAINS = {
    "rental_trends",
    "home_sales",
    "housing_permits",
    "affordability_ratios",
    "vacancy",
    "cost_burden",
    "production_targets",
}

CRITICAL_METRICS = {
    "rental_trends": "median_gross_rent",
    "home_sales": "homes_sold",
    "housing_permits": "permitted_units_total",
    "affordability_ratios": "rent_to_income_pct",
    "vacancy": "housing_vacancy_rate",
    "cost_burden": "renter_cost_burden_30_plus_pct",
    "production_targets": "rhna_progress_pct",
}

ACS_TABLES: dict[str, tuple[str, ...]] = {
    "B25064": ("B25064_E001", "B25064_M001"),
    "B25077": ("B25077_E001", "B25077_M001"),
    "B19013": ("B19013_E001", "B19013_M001"),
    "B25002": (
        "B25002_E001",
        "B25002_M001",
        "B25002_E002",
        "B25002_M002",
        "B25002_E003",
        "B25002_M003",
    ),
    "B25070": tuple(
        [f"B25070_E{index:03d}" for index in range(1, 12)]
        + [f"B25070_M{index:03d}" for index in range(1, 12)]
    ),
    "B25091": tuple(
        [f"B25091_E{index:03d}" for index in range(1, 24)]
        + [f"B25091_M{index:03d}" for index in range(1, 24)]
    ),
}

ACS_BASE = (
    "https://www2.census.gov/programs-surveys/acs/summary_file/"
    "{year}/table-based-SF/data/5YRData/acsdt5y{year}-{table}.dat"
)
BPS_BASE = "https://www2.census.gov/econ/bps/County/co{year}a.txt"
REDFIN_URL = (
    "https://redfin-public-data.s3.us-west-2.amazonaws.com/"
    "redfin_data_center/housing_market/monthly/all_counties.csv"
)
HCD_PACKAGE_API = "https://data.ca.gov/api/3/action/package_show"
HCD_DATASTORE_API = "https://data.ca.gov/api/3/action/datastore_search"
HCD_SQL_API = "https://data.ca.gov/api/3/action/datastore_search_sql"
HCD_APR_RESOURCE = "fe505d9b-8c36-42ba-ba30-08bc4f34e022"
HCD_RHNA_RESOURCE = "1e80a9cf-724c-432d-8374-e9708a6a92dc"

SOURCE_REGISTRY = {
    "CENSUS_ACS5_BULK": {
        "publisher": "U.S. Census Bureau",
        "title": "American Community Survey 5-Year Detailed Tables",
        "source_tier": 1,
        "source_class": "government",
        "frequency": "annual",
        "geography_basis": "household residence",
        "landing_url": "https://www.census.gov/programs-surveys/acs/data.html",
    },
    "CENSUS_BPS": {
        "publisher": "U.S. Census Bureau",
        "title": "Building Permits Survey",
        "source_tier": 1,
        "source_class": "government",
        "frequency": "annual final",
        "geography_basis": "permit-issuing location",
        "landing_url": "https://www.census.gov/construction/bps/",
    },
    "REDFIN_HMT": {
        "publisher": "Redfin",
        "title": "Housing Market Tracker — Monthly County Data",
        "source_tier": 2,
        "source_class": "private primary-market source",
        "frequency": "monthly",
        "geography_basis": "listed and sold homes represented in Redfin market data",
        "landing_url": "https://www.redfin.com/news/data-center/housing-market/",
    },
    "CA_HCD_APR": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Housing Element Annual Progress Report — Table A2",
        "source_tier": 1,
        "source_class": "government",
        "frequency": "annual with portal updates",
        "geography_basis": "reporting jurisdiction and county",
        "landing_url": "https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard",
    },
    "CA_HCD_RHNA": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Sixth-Cycle RHNA Progress Report",
        "source_tier": 1,
        "source_class": "government",
        "frequency": "periodic",
        "geography_basis": "housing-element jurisdiction",
        "landing_url": "https://www.hcd.ca.gov/rhna",
    },
}

OBSERVATION_FIELDS = (
    "domain",
    "indicator_id",
    "metric_id",
    "metric_name",
    "county_fips",
    "county_name",
    "period",
    "period_end",
    "frequency",
    "value",
    "unit",
    "margin_of_error",
    "source_id",
    "source_tier",
    "source_release",
    "retrieved_at",
    "raw_sha256",
    "calculation",
    "notes",
)

REQUIRED_FILES = (
    "config/phase14_housing.json",
    "metadata/housing_indicator_catalog.csv",
    "metadata/housing_source_registry.csv",
    "docs/housing-observatory/README.md",
    "docs/housing-observatory/METHODOLOGY.md",
    "docs/housing-observatory/DATA_DICTIONARY.md",
    "docs/housing-observatory/LIMITATIONS.md",
    "docs/housing-observatory/RUNBOOK.md",
    "src/bay_outlook/phase14.py",
    "tests/test_phase14.py",
    ".github/workflows/housing-observatory-update.yml",
    "data/phase14/exports/housing_time_series.csv",
    "data/phase14/exports/housing_snapshot.csv",
    "data/phase14/exports/rhna_progress.csv",
    "data/phase14/exports/source_freshness.csv",
    "data/phase14/public/housing-data.json",
    "data/phase14/housing_observatory.sqlite",
)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.casefold() in {"na", "n/a", "null", "none", "-"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= -10_000_000:
        return None
    return number


def _sum(values: Iterable[float | None]) -> float | None:
    material = [value for value in values if value is not None]
    return sum(material) if material else None


def _ratio(numerator: float | None, denominator: float | None, multiplier: float = 100.0) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * multiplier


def _observation(
    *,
    domain: str,
    indicator_id: str,
    metric_id: str,
    metric_name: str,
    county_fips: str,
    period: str,
    period_end: str,
    frequency: str,
    value: float | None,
    unit: str,
    source_id: str,
    source_release: str,
    retrieved_at: str,
    raw_sha256: str,
    margin_of_error: float | None = None,
    calculation: str = "published",
    notes: str = "",
) -> dict[str, Any]:
    return {
        "domain": domain,
        "indicator_id": indicator_id,
        "metric_id": metric_id,
        "metric_name": metric_name,
        "county_fips": county_fips,
        "county_name": COUNTY_BY_FIPS[county_fips],
        "period": period,
        "period_end": period_end,
        "frequency": frequency,
        "value": value,
        "unit": unit,
        "margin_of_error": margin_of_error,
        "source_id": source_id,
        "source_tier": SOURCE_REGISTRY[source_id]["source_tier"],
        "source_release": source_release,
        "retrieved_at": retrieved_at,
        "raw_sha256": raw_sha256,
        "calculation": calculation,
        "notes": notes,
    }


def _request(url: str, timeout: int = 180) -> tuple[bytes, dict[str, str], str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), dict(response.headers.items()), response.geturl()


def _patch_snapshot_metadata(path: Path, **extra: Any) -> dict[str, Any]:
    metadata = _read_json(path)
    metadata.update(extra)
    _write_json(path, metadata)
    return metadata


def _snapshot_record(snapshot: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": snapshot.source_id,
        "source_url": snapshot.source_url,
        "source_release": snapshot.source_release,
        "retrieved_at": snapshot.retrieved_at,
        "snapshot_sha256": snapshot.sha256,
        "upstream_sha256": metadata.get("upstream_sha256", snapshot.sha256),
        "byte_count": metadata.get("byte_count"),
        "upstream_byte_count": metadata.get("upstream_byte_count", metadata.get("byte_count")),
        "snapshot_kind": metadata.get("snapshot_kind", "complete_response"),
        "raw_path": str(snapshot.path.relative_to(PROJECT_ROOT)),
        "metadata_path": str(snapshot.metadata_path.relative_to(PROJECT_ROOT)),
    }


def _acs_url(year: int, table: str) -> str:
    return ACS_BASE.format(year=year, table=table.casefold())


def _fetch_acs_extract(
    *,
    data_root: Path,
    year: int,
    table: str,
    retrieved_at: str,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    url = _acs_url(year, table)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    byte_count = 0
    selected: list[bytes] = []
    wanted = {f"0500000US{fips}".encode("ascii") for fips in COUNTY_BY_FIPS}
    headers: dict[str, str] = {}
    final_url = url
    with urllib.request.urlopen(request, timeout=180) as response:
        headers = dict(response.headers.items())
        final_url = response.geturl()
        for index, line in enumerate(response):
            digest.update(line)
            byte_count += len(line)
            if index == 0 or line.split(b"|", 1)[0] in wanted:
                selected.append(line)
    if len(selected) != len(COUNTY_BY_FIPS) + 1:
        raise ValueError(
            f"ACS {year} {table} extract expected {len(COUNTY_BY_FIPS)} county rows; "
            f"found {max(0, len(selected) - 1)}"
        )
    content = b"".join(selected)
    snapshot = save_snapshot(
        data_root,
        "CENSUS_ACS5_BULK",
        f"acs5-{year}-{table.casefold()}-bay-counties.dat",
        content,
        final_url,
        f"ACS5-{year}-{table}",
        response_headers=headers,
        retrieved_at=retrieved_at,
    )
    metadata = _patch_snapshot_metadata(
        snapshot.metadata_path,
        snapshot_kind="source_slice",
        upstream_sha256=digest.hexdigest(),
        upstream_byte_count=byte_count,
        selection={
            "geography": "summary level 050 county rows for the nine configured Bay Area FIPS codes",
            "preserved_header": True,
            "row_count": len(COUNTY_BY_FIPS),
        },
    )
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    rows: dict[str, dict[str, str]] = {}
    for row in reader:
        fips = row["GEO_ID"][-5:]
        rows[fips] = row
    return rows, _snapshot_record(snapshot, metadata)


def _acs_observations(
    *,
    data_root: Path,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    for year in ACS_YEARS:
        tables: dict[str, dict[str, dict[str, str]]] = {}
        table_hashes: dict[str, str] = {}
        for table in ACS_TABLES:
            tables[table], record = _fetch_acs_extract(
                data_root=data_root,
                year=year,
                table=table,
                retrieved_at=retrieved_at,
            )
            snapshots.append(record)
            table_hashes[table] = record["snapshot_sha256"]

        for fips in COUNTY_BY_FIPS:
            rent = _number(tables["B25064"][fips].get("B25064_E001"))
            rent_moe = _number(tables["B25064"][fips].get("B25064_M001"))
            home_value = _number(tables["B25077"][fips].get("B25077_E001"))
            home_value_moe = _number(tables["B25077"][fips].get("B25077_M001"))
            income = _number(tables["B19013"][fips].get("B19013_E001"))
            income_moe = _number(tables["B19013"][fips].get("B19013_M001"))
            occupancy = tables["B25002"][fips]
            housing_units = _number(occupancy.get("B25002_E001"))
            vacant_units = _number(occupancy.get("B25002_E003"))

            renter = tables["B25070"][fips]
            renter_total = _number(renter.get("B25070_E001"))
            renter_not_computed = _number(renter.get("B25070_E011"))
            renter_denominator = (
                renter_total - renter_not_computed
                if renter_total is not None and renter_not_computed is not None
                else None
            )
            renter_burden = _sum(
                _number(renter.get(f"B25070_E{index:03d}")) for index in range(7, 11)
            )
            renter_severe = _number(renter.get("B25070_E010"))

            owner = tables["B25091"][fips]
            owner_total = _number(owner.get("B25091_E001"))
            owner_not_computed = _sum(
                [_number(owner.get("B25091_E012")), _number(owner.get("B25091_E023"))]
            )
            owner_denominator = (
                owner_total - owner_not_computed
                if owner_total is not None and owner_not_computed is not None
                else None
            )
            owner_burden = _sum(
                _number(owner.get(f"B25091_E{index:03d}"))
                for index in (8, 9, 10, 11, 19, 20, 21, 22)
            )
            owner_severe = _sum(
                [_number(owner.get("B25091_E011")), _number(owner.get("B25091_E022"))]
            )

            common = {
                "county_fips": fips,
                "period": str(year),
                "period_end": f"{year}-12-31",
                "frequency": "annual five-year estimate",
                "retrieved_at": retrieved_at,
            }
            observations.extend(
                [
                    _observation(
                        domain="rental_trends",
                        indicator_id="H2",
                        metric_id="median_gross_rent",
                        metric_name="Median gross rent",
                        value=rent,
                        unit="current dollars per month",
                        margin_of_error=rent_moe,
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25064",
                        raw_sha256=table_hashes["B25064"],
                        notes="ACS five-year estimate; consecutive vintages contain overlapping survey years.",
                        **common,
                    ),
                    _observation(
                        domain="affordability_ratios",
                        indicator_id="H2",
                        metric_id="median_home_value",
                        metric_name="Median owner-occupied home value",
                        value=home_value,
                        unit="current dollars",
                        margin_of_error=home_value_moe,
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25077",
                        raw_sha256=table_hashes["B25077"],
                        notes="Owner-reported ACS value, not a transaction price.",
                        **common,
                    ),
                    _observation(
                        domain="affordability_ratios",
                        indicator_id="O3",
                        metric_id="median_household_income",
                        metric_name="Median household income",
                        value=income,
                        unit="current dollars per year",
                        margin_of_error=income_moe,
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B19013",
                        raw_sha256=table_hashes["B19013"],
                        **common,
                    ),
                    _observation(
                        domain="vacancy",
                        indicator_id="H4",
                        metric_id="vacant_housing_units",
                        metric_name="Vacant housing units",
                        value=vacant_units,
                        unit="housing units",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25002",
                        raw_sha256=table_hashes["B25002"],
                        **common,
                    ),
                    _observation(
                        domain="vacancy",
                        indicator_id="H4",
                        metric_id="housing_vacancy_rate",
                        metric_name="Housing vacancy rate",
                        value=_ratio(vacant_units, housing_units),
                        unit="percent",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25002",
                        raw_sha256=table_hashes["B25002"],
                        calculation="B25002 vacant units divided by total housing units",
                        notes="Derived point estimate; no derived margin of error is asserted.",
                        **common,
                    ),
                    _observation(
                        domain="cost_burden",
                        indicator_id="H1",
                        metric_id="renter_cost_burden_30_plus_pct",
                        metric_name="Renters spending at least 30% of income on rent",
                        value=_ratio(renter_burden, renter_denominator),
                        unit="percent",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25070",
                        raw_sha256=table_hashes["B25070"],
                        calculation="B25070 categories 30–34.9%, 35–39.9%, 40–49.9%, and 50%+ divided by computed cases",
                        notes="Derived point estimate; no derived margin of error is asserted.",
                        **common,
                    ),
                    _observation(
                        domain="cost_burden",
                        indicator_id="H1",
                        metric_id="renter_severe_cost_burden_50_plus_pct",
                        metric_name="Renters spending at least 50% of income on rent",
                        value=_ratio(renter_severe, renter_denominator),
                        unit="percent",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25070",
                        raw_sha256=table_hashes["B25070"],
                        calculation="B25070 50%+ category divided by computed cases",
                        notes="Derived point estimate; no derived margin of error is asserted.",
                        **common,
                    ),
                    _observation(
                        domain="cost_burden",
                        indicator_id="H1",
                        metric_id="owner_cost_burden_30_plus_pct",
                        metric_name="Owners spending at least 30% of income on housing",
                        value=_ratio(owner_burden, owner_denominator),
                        unit="percent",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25091",
                        raw_sha256=table_hashes["B25091"],
                        calculation="B25091 mortgage and nonmortgage 30%+ categories divided by computed cases",
                        notes="Derived point estimate; no derived margin of error is asserted.",
                        **common,
                    ),
                    _observation(
                        domain="cost_burden",
                        indicator_id="H1",
                        metric_id="owner_severe_cost_burden_50_plus_pct",
                        metric_name="Owners spending at least 50% of income on housing",
                        value=_ratio(owner_severe, owner_denominator),
                        unit="percent",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25091",
                        raw_sha256=table_hashes["B25091"],
                        calculation="B25091 mortgage and nonmortgage 50%+ categories divided by computed cases",
                        notes="Derived point estimate; no derived margin of error is asserted.",
                        **common,
                    ),
                    _observation(
                        domain="affordability_ratios",
                        indicator_id="H2",
                        metric_id="rent_to_income_pct",
                        metric_name="Median gross rent to median household income",
                        value=_ratio(rent * 12 if rent is not None else None, income),
                        unit="percent",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25064+B19013",
                        raw_sha256=sha256_bytes(
                            (table_hashes["B25064"] + table_hashes["B19013"]).encode("ascii")
                        ),
                        calculation="annualized median gross rent divided by median household income",
                        notes="Ratio of two medians; not the share of a typical household's actual income spent on rent.",
                        **common,
                    ),
                    _observation(
                        domain="affordability_ratios",
                        indicator_id="H2",
                        metric_id="home_value_to_income_ratio",
                        metric_name="Median home value to median household income",
                        value=_ratio(home_value, income, multiplier=1.0),
                        unit="ratio",
                        source_id="CENSUS_ACS5_BULK",
                        source_release=f"ACS5-{year}-B25077+B19013",
                        raw_sha256=sha256_bytes(
                            (table_hashes["B25077"] + table_hashes["B19013"]).encode("ascii")
                        ),
                        calculation="median owner-reported home value divided by median household income",
                        notes="Ratio of two medians; excludes mortgage rates, taxes, insurance, and down payments.",
                        **common,
                    ),
                ]
            )
    return observations, snapshots


def _bps_observations(
    *,
    data_root: Path,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    observations: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    for year in BPS_YEARS:
        url = BPS_BASE.format(year=year)
        content, headers, final_url = _request(url)
        snapshot = save_snapshot(
            data_root,
            "CENSUS_BPS",
            f"co{year}a.txt",
            content,
            final_url,
            f"BPS-annual-final-{year}",
            response_headers=headers,
            retrieved_at=retrieved_at,
        )
        metadata = _read_json(snapshot.metadata_path)
        snapshots.append(_snapshot_record(snapshot, metadata))
        lines = content.decode("latin-1").splitlines()
        reader = csv.reader(lines[3:])
        found: set[str] = set()
        for row in reader:
            if len(row) < 18 or row[1].strip() != "06":
                continue
            fips = row[1].strip() + row[2].strip().zfill(3)
            if fips not in COUNTY_BY_FIPS:
                continue
            found.add(fips)
            unit_values = {
                "permitted_units_single_family": _number(row[7]),
                "permitted_units_two_unit": _number(row[10]),
                "permitted_units_three_four_unit": _number(row[13]),
                "permitted_units_five_plus": _number(row[16]),
            }
            total = _sum(unit_values.values())
            metrics = {
                "permitted_units_total": ("Permitted housing units", total),
                **{
                    key: (
                        {
                            "permitted_units_single_family": "Permitted units in single-family structures",
                            "permitted_units_two_unit": "Permitted units in two-unit structures",
                            "permitted_units_three_four_unit": "Permitted units in three- and four-unit structures",
                            "permitted_units_five_plus": "Permitted units in structures with five or more units",
                        }[key],
                        value,
                    )
                    for key, value in unit_values.items()
                },
            }
            for metric_id, (metric_name, value) in metrics.items():
                observations.append(
                    _observation(
                        domain="housing_permits",
                        indicator_id="H3",
                        metric_id=metric_id,
                        metric_name=metric_name,
                        county_fips=fips,
                        period=str(year),
                        period_end=f"{year}-12-31",
                        frequency="annual final",
                        value=value,
                        unit="housing units",
                        source_id="CENSUS_BPS",
                        source_release=f"BPS-annual-final-{year}",
                        retrieved_at=retrieved_at,
                        raw_sha256=snapshot.sha256,
                        notes="New privately owned residential units authorized by building permits.",
                    )
                )
        if found != set(COUNTY_BY_FIPS):
            raise ValueError(f"BPS {year} is missing configured counties: {sorted(set(COUNTY_BY_FIPS) - found)}")
    return observations, snapshots


def _redfin_observations(
    *,
    data_root: Path,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    request = urllib.request.Request(REDFIN_URL, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    headers: dict[str, str] = {}
    final_url = REDFIN_URL
    byte_count = 0
    with tempfile.NamedTemporaryFile(prefix="bay-outlook-redfin-", suffix=".csv", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                headers = dict(response.headers.items())
                final_url = response.geturl()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    byte_count += len(chunk)
                    temporary.write(chunk)

            selected = io.StringIO()
            writer: csv.DictWriter[str] | None = None
            selected_rows: list[dict[str, str]] = []
            expected_names = {f"{name} County, CA" for name in COUNTY_BY_FIPS.values()}
            with temporary_path.open(newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames is None:
                    raise ValueError("Redfin download is missing its header")
                writer = csv.DictWriter(selected, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    if (
                        row.get("REGION NAME") in expected_names
                        and (row.get("PERIOD BEGIN") or "") >= REDFIN_START_PERIOD
                    ):
                        writer.writerow(row)
                        selected_rows.append(row)
            found_names = {row["REGION NAME"] for row in selected_rows}
            if found_names != expected_names:
                raise ValueError(f"Redfin extract is missing counties: {sorted(expected_names - found_names)}")
            content = selected.getvalue().encode("utf-8")
        finally:
            temporary_path.unlink(missing_ok=True)

    latest_updated = max(row.get("LAST UPDATED", "") for row in selected_rows)
    snapshot = save_snapshot(
        data_root,
        "REDFIN_HMT",
        "redfin-monthly-bay-counties.csv",
        content,
        final_url,
        f"Redfin-HMT-updated-{latest_updated}",
        response_headers=headers,
        retrieved_at=retrieved_at,
        dataset_status="documented-private-source",
    )
    metadata = _patch_snapshot_metadata(
        snapshot.metadata_path,
        snapshot_kind="source_slice",
        upstream_sha256=digest.hexdigest(),
        upstream_byte_count=byte_count,
        selection={
            "region_type": "County",
            "counties": sorted(expected_names),
            "period_begin_on_or_after": REDFIN_START_PERIOD,
            "row_count": len(selected_rows),
        },
        source_class="private primary-market source",
    )

    observations: list[dict[str, Any]] = []
    metrics = {
        "HOMES SOLD": ("homes_sold", "Homes sold", "homes"),
        "MEDIAN SALE PRICE NSA ($)": (
            "median_sale_price",
            "Median sale price, not seasonally adjusted",
            "current dollars",
        ),
        "MEDIAN DAYS ON MARKET (DAYS)": (
            "median_days_on_market",
            "Median days on market",
            "days",
        ),
        "ACTIVE LISTINGS": ("active_listings", "Active listings", "listings"),
        "MONTHS OF SUPPLY": ("months_of_supply", "Months of supply", "months"),
    }
    for row in selected_rows:
        county_name = row["REGION NAME"].removesuffix(" County, CA")
        fips = FIPS_BY_COUNTY[county_name.casefold()]
        period_begin = row["PERIOD BEGIN"]
        period_end = row["PERIOD END"]
        period = period_begin[:7]
        for source_column, (metric_id, metric_name, unit) in metrics.items():
            observations.append(
                _observation(
                    domain="home_sales",
                    indicator_id="H2",
                    metric_id=metric_id,
                    metric_name=metric_name,
                    county_fips=fips,
                    period=period,
                    period_end=period_end,
                    frequency="monthly",
                    value=_number(row.get(source_column)),
                    unit=unit,
                    source_id="REDFIN_HMT",
                    source_release=f"Redfin-HMT-updated-{row.get('LAST UPDATED', latest_updated)}",
                    retrieved_at=retrieved_at,
                    raw_sha256=snapshot.sha256,
                    notes=(
                        "Redfin market data are subject to revision and reflect Redfin's documented market coverage; "
                        "this is not a government statistic."
                    ),
                )
            )
    return observations, [_snapshot_record(snapshot, metadata)]


def _hcd_sql() -> tuple[str, str]:
    county_names = [COUNTY_BY_FIPS[fips] for fips in COUNTY_BY_FIPS]
    county_values = ",".join(repr(name) for name in county_names)
    mapping_sql = f'''SELECT "JURIS_NAME", "CNTY_NAME", max("YEAR") AS "LATEST_YEAR"
FROM "{HCD_APR_RESOURCE}"
WHERE "CNTY_NAME" IN ({county_values})
GROUP BY "JURIS_NAME", "CNTY_NAME"
ORDER BY "CNTY_NAME", "JURIS_NAME"'''

    def numeric(field: str) -> str:
        return (
            f'''CASE WHEN "{field}" ~ '^[0-9]+([.][0-9]+)?$' '''
            f'''THEN "{field}"::numeric ELSE 0 END'''
        )

    vli_fields = (
        "BP_ACUTELY_LOW_INCOME_DR",
        "BP_ACUTELY_LOW_INCOME_NDR",
        "BP_EXTREMELY_LOW_INCOME_DR",
        "BP_EXTREMELY_LOW_INCOME_NDR",
        "BP_VLOW_INCOME_DR",
        "BP_VLOW_INCOME_NDR",
    )
    low_fields = ("BP_LOW_INCOME_DR", "BP_LOW_INCOME_NDR")
    mod_fields = ("BP_MOD_INCOME_DR", "BP_MOD_INCOME_NDR")
    vli = " + ".join(numeric(field) for field in vli_fields)
    low = " + ".join(numeric(field) for field in low_fields)
    moderate = " + ".join(numeric(field) for field in mod_fields)
    above = numeric("BP_ABOVE_MOD_INCOME")
    reported = numeric("NO_BUILDING_PERMITS")
    apr_sql = f'''SELECT "CNTY_NAME", "YEAR",
SUM({vli}) AS "VLI_UNITS",
SUM({low}) AS "LI_UNITS",
SUM({moderate}) AS "MOD_UNITS",
SUM({above}) AS "ABOVE_MOD_UNITS",
SUM({reported}) AS "REPORTED_TOTAL",
COUNT(*) AS "PROJECT_ROWS"
FROM "{HCD_APR_RESOURCE}"
WHERE "CNTY_NAME" IN ({county_values}) AND "YEAR" >= '2023'
GROUP BY "CNTY_NAME", "YEAR"
ORDER BY "CNTY_NAME", "YEAR"'''
    return mapping_sql, apr_sql


def _fetch_hcd_json(
    *,
    data_root: Path,
    source_id: str,
    filename: str,
    url: str,
    source_release: str,
    retrieved_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    content, headers, final_url = _request(url)
    payload = json.loads(content)
    if payload.get("success") is not True:
        raise ValueError(f"HCD API request failed for {filename}")
    snapshot = save_snapshot(
        data_root,
        source_id,
        filename,
        content,
        final_url,
        source_release,
        response_headers=headers,
        retrieved_at=retrieved_at,
    )
    return payload, _snapshot_record(snapshot, _read_json(snapshot.metadata_path))


def _normalize_jurisdiction(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def _hcd_observations(
    *,
    data_root: Path,
    retrieved_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mapping_sql, apr_sql = _hcd_sql()
    mapping_url = HCD_SQL_API + "?" + urllib.parse.urlencode({"sql": mapping_sql})
    apr_url = HCD_SQL_API + "?" + urllib.parse.urlencode({"sql": apr_sql})
    rhna_url = HCD_DATASTORE_API + "?" + urllib.parse.urlencode(
        {"resource_id": HCD_RHNA_RESOURCE, "limit": 1000}
    )
    apr_package_url = HCD_PACKAGE_API + "?" + urllib.parse.urlencode(
        {"id": "housing-element-annual-progress-report-apr-data-by-jurisdiction-and-year"}
    )
    rhna_package_url = HCD_PACKAGE_API + "?" + urllib.parse.urlencode(
        {"id": "rhna-progress-report"}
    )

    mapping, mapping_snapshot = _fetch_hcd_json(
        data_root=data_root,
        source_id="CA_HCD_APR",
        filename="bay-jurisdiction-county-map.json",
        url=mapping_url,
        source_release="HCD-APR-portal-current",
        retrieved_at=retrieved_at,
    )
    apr, apr_snapshot = _fetch_hcd_json(
        data_root=data_root,
        source_id="CA_HCD_APR",
        filename="bay-apr-permits-by-county-year.json",
        url=apr_url,
        source_release="HCD-APR-through-2025",
        retrieved_at=retrieved_at,
    )
    rhna, rhna_snapshot = _fetch_hcd_json(
        data_root=data_root,
        source_id="CA_HCD_RHNA",
        filename="sixth-cycle-rhna-progress.json",
        url=rhna_url,
        source_release="HCD-RHNA-sixth-cycle",
        retrieved_at=retrieved_at,
    )
    apr_package, apr_package_snapshot = _fetch_hcd_json(
        data_root=data_root,
        source_id="CA_HCD_APR",
        filename="apr-package-metadata.json",
        url=apr_package_url,
        source_release="HCD-APR-package-metadata",
        retrieved_at=retrieved_at,
    )
    rhna_package, rhna_package_snapshot = _fetch_hcd_json(
        data_root=data_root,
        source_id="CA_HCD_RHNA",
        filename="rhna-package-metadata.json",
        url=rhna_package_url,
        source_release="HCD-RHNA-package-metadata",
        retrieved_at=retrieved_at,
    )
    snapshots = [
        mapping_snapshot,
        apr_snapshot,
        rhna_snapshot,
        apr_package_snapshot,
        rhna_package_snapshot,
    ]

    mapping_rows = mapping["result"]["records"]
    jurisdiction_to_county = {
        _normalize_jurisdiction(row["JURIS_NAME"]): row["CNTY_NAME"] for row in mapping_rows
    }
    if len(jurisdiction_to_county) != 109:
        raise ValueError(f"Expected 109 Bay Area housing jurisdictions; found {len(jurisdiction_to_county)}")

    target_by_county: dict[str, dict[str, float]] = defaultdict(
        lambda: {"vli": 0.0, "li": 0.0, "mod": 0.0, "above": 0.0}
    )
    jurisdiction_counts: dict[str, int] = defaultdict(int)
    planning_periods: dict[str, set[str]] = defaultdict(set)
    matched_jurisdictions: set[str] = set()
    for row in rhna["result"]["records"]:
        key = _normalize_jurisdiction(row["Jurisdiction"])
        county = jurisdiction_to_county.get(key)
        if county is None:
            continue
        matched_jurisdictions.add(key)
        jurisdiction_counts[county] += 1
        planning_periods[county].add(row.get("Planning Period") or "")
        target_by_county[county]["vli"] += _number(row.get("RHNA VLI")) or 0.0
        target_by_county[county]["li"] += _number(row.get("RHNA LI")) or 0.0
        target_by_county[county]["mod"] += _number(row.get("RHNA MOD")) or 0.0
        target_by_county[county]["above"] += _number(row.get("RHNA ABOVE MOD")) or 0.0
    if matched_jurisdictions != set(jurisdiction_to_county):
        raise ValueError(
            "HCD RHNA mapping missed Bay jurisdictions: "
            f"{sorted(set(jurisdiction_to_county) - matched_jurisdictions)}"
        )

    apr_rows = apr["result"]["records"]
    annual_permits: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    cumulative: dict[str, dict[str, float]] = defaultdict(
        lambda: {"vli": 0.0, "li": 0.0, "mod": 0.0, "above": 0.0, "total": 0.0}
    )
    for row in apr_rows:
        county = row["CNTY_NAME"]
        annual = {
            "vli": _number(row.get("VLI_UNITS")) or 0.0,
            "li": _number(row.get("LI_UNITS")) or 0.0,
            "mod": _number(row.get("MOD_UNITS")) or 0.0,
            "above": _number(row.get("ABOVE_MOD_UNITS")) or 0.0,
            "total": _number(row.get("REPORTED_TOTAL")) or 0.0,
        }
        category_total = annual["vli"] + annual["li"] + annual["mod"] + annual["above"]
        if abs(category_total - annual["total"]) > 0.01:
            raise ValueError(f"HCD APR category total mismatch for {county} {row['YEAR']}")
        annual_permits[county][row["YEAR"]] = annual
        for key, value in annual.items():
            cumulative[county][key] += value

    county_to_fips = {name: fips for fips, name in COUNTY_BY_FIPS.items()}
    observations: list[dict[str, Any]] = []
    progress_rows: list[dict[str, Any]] = []
    for county, fips in county_to_fips.items():
        for year, annual in sorted(annual_permits[county].items()):
            for category, value in annual.items():
                metric_id = "hcd_apr_permitted_units" if category == "total" else f"hcd_apr_{category}_permitted_units"
                observations.append(
                    _observation(
                        domain="production_targets",
                        indicator_id="H3",
                        metric_id=metric_id,
                        metric_name=(
                            "HCD-reported permitted units"
                            if category == "total"
                            else f"HCD-reported {category.upper()} permitted units"
                        ),
                        county_fips=fips,
                        period=year,
                        period_end=f"{year}-12-31",
                        frequency="annual",
                        value=value,
                        unit="housing units",
                        source_id="CA_HCD_APR",
                        source_release="HCD-APR-through-2025",
                        retrieved_at=retrieved_at,
                        raw_sha256=apr_snapshot["snapshot_sha256"],
                        notes="Jurisdiction-reported building permits aggregated to county by The Bay Outlook.",
                    )
                )

        target = target_by_county[county]
        goal = sum(target.values())
        permitted = cumulative[county]["total"]
        progress = _ratio(permitted, goal)
        periods = sorted(period for period in planning_periods[county] if period)
        progress_row = {
            "county_fips": fips,
            "county_name": county,
            "jurisdictions": jurisdiction_counts[county],
            "planning_period": "; ".join(periods),
            "rhna_vli": target["vli"],
            "rhna_li": target["li"],
            "rhna_mod": target["mod"],
            "rhna_above_mod": target["above"],
            "rhna_total": goal,
            "permitted_2023_2025": permitted,
            "progress_pct": progress,
            "source_target": "CA_HCD_RHNA",
            "source_progress": "CA_HCD_APR",
        }
        progress_rows.append(progress_row)
        target_metrics = {
            "rhna_target_total": ("Sixth-cycle RHNA allocation", goal, "housing units"),
            "rhna_permitted_cycle_to_date": (
                "Permitted units reported for 2023–2025",
                permitted,
                "housing units",
            ),
            "rhna_progress_pct": ("Progress toward sixth-cycle RHNA allocation", progress, "percent"),
        }
        for metric_id, (metric_name, value, unit) in target_metrics.items():
            source_id = "CA_HCD_RHNA" if metric_id == "rhna_target_total" else "CA_HCD_APR"
            raw_hash = (
                rhna_snapshot["snapshot_sha256"]
                if source_id == "CA_HCD_RHNA"
                else apr_snapshot["snapshot_sha256"]
            )
            observations.append(
                _observation(
                    domain="production_targets",
                    indicator_id="H3",
                    metric_id=metric_id,
                    metric_name=metric_name,
                    county_fips=fips,
                    period="2023-2025" if source_id == "CA_HCD_APR" else "sixth cycle",
                    period_end="2025-12-31",
                    frequency="cycle-to-date",
                    value=value,
                    unit=unit,
                    source_id=source_id,
                    source_release=(
                        "HCD-RHNA-sixth-cycle"
                        if source_id == "CA_HCD_RHNA"
                        else "HCD-APR-through-2025"
                    ),
                    retrieved_at=retrieved_at,
                    raw_sha256=raw_hash,
                    calculation=(
                        "sum of jurisdiction RHNA allocations within county"
                        if metric_id == "rhna_target_total"
                        else (
                            "sum of 2023–2025 jurisdiction-reported permits within county"
                            if metric_id == "rhna_permitted_cycle_to_date"
                            else "2023–2025 reported permits divided by sixth-cycle RHNA allocation"
                        )
                    ),
                    notes=(
                        "Targets and progress use separate HCD datasets; county values sum constituent jurisdictions."
                    ),
                )
            )

    package_metadata = {
        "apr_modified": apr_package["result"].get("metadata_modified"),
        "rhna_modified": rhna_package["result"].get("metadata_modified"),
    }
    for row in progress_rows:
        row.update(package_metadata)
    return observations, snapshots, progress_rows


def _quality_checks(observations: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"check_name": name, "passed": int(bool(passed)), "detail": detail})

    domains = {row["domain"] for row in observations}
    add("seven_domain_scope", domains == REQUIRED_DOMAINS, sorted(domains))

    critical_coverage: dict[str, list[str]] = {}
    for domain, metric_id in CRITICAL_METRICS.items():
        counties = sorted({row["county_fips"] for row in observations if row["metric_id"] == metric_id})
        critical_coverage[domain] = counties
    add(
        "nine_county_critical_coverage",
        all(set(counties) == set(COUNTY_BY_FIPS) for counties in critical_coverage.values()),
        critical_coverage,
    )

    periods_by_metric_county: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in observations:
        if row["value"] is not None:
            periods_by_metric_county[(row["metric_id"], row["county_fips"])].add(row["period"])
    acs_history = {
        fips: len(periods_by_metric_county[("median_gross_rent", fips)]) for fips in COUNTY_BY_FIPS
    }
    redfin_history = {fips: len(periods_by_metric_county[("homes_sold", fips)]) for fips in COUNTY_BY_FIPS}
    bps_history = {
        fips: len(periods_by_metric_county[("permitted_units_total", fips)]) for fips in COUNTY_BY_FIPS
    }
    add("acs_history_minimum", all(value >= 4 for value in acs_history.values()), acs_history)
    add("redfin_history_minimum", all(value >= 60 for value in redfin_history.values()), redfin_history)
    add("bps_history_minimum", all(value >= 5 for value in bps_history.values()), bps_history)

    duplicate_counter: dict[tuple[Any, ...], int] = defaultdict(int)
    for row in observations:
        key = (
            row["metric_id"],
            row["county_fips"],
            row["period"],
            row["source_id"],
            row["source_release"],
        )
        duplicate_counter[key] += 1
    duplicates = [key for key, count in duplicate_counter.items() if count > 1]
    add("natural_key_uniqueness", not duplicates, [list(key) for key in duplicates[:20]])

    rate_metrics = {
        "housing_vacancy_rate",
        "renter_cost_burden_30_plus_pct",
        "renter_severe_cost_burden_50_plus_pct",
        "owner_cost_burden_30_plus_pct",
        "owner_severe_cost_burden_50_plus_pct",
        "rent_to_income_pct",
    }
    invalid_rates = [
        {"metric": row["metric_id"], "county": row["county_fips"], "period": row["period"], "value": row["value"]}
        for row in observations
        if row["metric_id"] in rate_metrics
        and row["value"] is not None
        and not (0 <= float(row["value"]) <= 100)
    ]
    add("rate_value_ranges", not invalid_rates, invalid_rates[:20])

    source_ids = {row["source_id"] for row in observations}
    snapshot_sources = {row["source_id"] for row in snapshots}
    add(
        "source_snapshot_lineage",
        source_ids <= snapshot_sources and source_ids == set(SOURCE_REGISTRY),
        {"observation_sources": sorted(source_ids), "snapshot_sources": sorted(snapshot_sources)},
    )
    return checks


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: Iterable[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(fieldnames or (rows[0].keys() if rows else []))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _latest_snapshot(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in observations:
        key = (row["metric_id"], row["county_fips"])
        current = latest.get(key)
        if current is None or (row["period_end"], row["period"]) > (
            current["period_end"],
            current["period"],
        ):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: (row["metric_id"], row["county_name"]))


def _source_freshness(
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_period: dict[str, str] = {}
    for row in observations:
        source_id = row["source_id"]
        latest_period[source_id] = max(latest_period.get(source_id, ""), row["period_end"])
    snapshot_counts: dict[str, int] = defaultdict(int)
    latest_retrieval: dict[str, str] = {}
    for row in snapshots:
        snapshot_counts[row["source_id"]] += 1
        latest_retrieval[row["source_id"]] = max(
            latest_retrieval.get(row["source_id"], ""), row["retrieved_at"]
        )
    rows = []
    for source_id, source in SOURCE_REGISTRY.items():
        rows.append(
            {
                "source_id": source_id,
                "publisher": source["publisher"],
                "dataset": source["title"],
                "source_tier": source["source_tier"],
                "source_class": source["source_class"],
                "frequency": source["frequency"],
                "latest_period_end": latest_period.get(source_id, ""),
                "retrieved_at": latest_retrieval.get(source_id, ""),
                "snapshot_count": snapshot_counts[source_id],
                "status": "current for documented release cycle",
                "landing_url": source["landing_url"],
            }
        )
    return rows


def _build_database(
    path: Path,
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    checks: list[dict[str, Any]],
    built_at: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="phase14-", suffix=".sqlite", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with sqlite3.connect(temporary) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE build_metadata (
                    metadata_key TEXT PRIMARY KEY,
                    metadata_value TEXT NOT NULL
                );
                CREATE TABLE source_registry (
                    source_id TEXT PRIMARY KEY,
                    publisher TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_tier INTEGER NOT NULL,
                    source_class TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    geography_basis TEXT NOT NULL,
                    landing_url TEXT NOT NULL
                );
                CREATE TABLE source_snapshot (
                    snapshot_id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES source_registry(source_id),
                    source_url TEXT NOT NULL,
                    source_release TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    upstream_sha256 TEXT NOT NULL,
                    byte_count INTEGER,
                    upstream_byte_count INTEGER,
                    snapshot_kind TEXT NOT NULL,
                    raw_path TEXT NOT NULL UNIQUE,
                    metadata_path TEXT NOT NULL UNIQUE
                );
                CREATE TABLE housing_observation (
                    observation_id INTEGER PRIMARY KEY,
                    domain TEXT NOT NULL,
                    indicator_id TEXT NOT NULL,
                    metric_id TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    county_fips TEXT NOT NULL,
                    county_name TEXT NOT NULL,
                    period TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    frequency TEXT NOT NULL,
                    value REAL,
                    unit TEXT NOT NULL,
                    margin_of_error REAL,
                    source_id TEXT NOT NULL REFERENCES source_registry(source_id),
                    source_tier INTEGER NOT NULL,
                    source_release TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    calculation TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    UNIQUE(metric_id, county_fips, period, source_id, source_release)
                );
                CREATE TABLE quality_check (
                    check_name TEXT PRIMARY KEY,
                    passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
                    detail TEXT NOT NULL
                );
                CREATE INDEX idx_housing_metric_county_period
                    ON housing_observation(metric_id, county_fips, period_end);
                CREATE INDEX idx_housing_domain_period
                    ON housing_observation(domain, period_end);
                """
            )
            connection.executemany(
                "INSERT INTO build_metadata(metadata_key, metadata_value) VALUES (?, ?)",
                [
                    ("project", "The Bay Outlook"),
                    ("phase", "14"),
                    ("version", PHASE14_VERSION),
                    ("built_at", built_at),
                    ("publication_authority", "named human only"),
                ],
            )
            connection.executemany(
                """INSERT INTO source_registry(
                    source_id, publisher, title, source_tier, source_class,
                    frequency, geography_basis, landing_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        source_id,
                        row["publisher"],
                        row["title"],
                        row["source_tier"],
                        row["source_class"],
                        row["frequency"],
                        row["geography_basis"],
                        row["landing_url"],
                    )
                    for source_id, row in SOURCE_REGISTRY.items()
                ],
            )
            connection.executemany(
                """INSERT INTO source_snapshot(
                    source_id, source_url, source_release, retrieved_at,
                    snapshot_sha256, upstream_sha256, byte_count, upstream_byte_count,
                    snapshot_kind, raw_path, metadata_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    tuple(
                        row[key]
                        for key in (
                            "source_id",
                            "source_url",
                            "source_release",
                            "retrieved_at",
                            "snapshot_sha256",
                            "upstream_sha256",
                            "byte_count",
                            "upstream_byte_count",
                            "snapshot_kind",
                            "raw_path",
                            "metadata_path",
                        )
                    )
                    for row in snapshots
                ],
            )
            connection.executemany(
                """INSERT INTO housing_observation(
                    domain, indicator_id, metric_id, metric_name, county_fips,
                    county_name, period, period_end, frequency, value, unit,
                    margin_of_error, source_id, source_tier, source_release,
                    retrieved_at, raw_sha256, calculation, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [tuple(row[field] for field in OBSERVATION_FIELDS) for row in observations],
            )
            connection.executemany(
                "INSERT INTO quality_check(check_name, passed, detail) VALUES (?, ?, ?)",
                [
                    (row["check_name"], row["passed"], json.dumps(row["detail"], sort_keys=True))
                    for row in checks
                ],
            )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise ValueError(
                    f"Phase 14 database failed integrity: integrity={integrity}, foreign_keys={foreign_keys}"
                )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _public_payload(
    *,
    observations: list[dict[str, Any]],
    progress_rows: list[dict[str, Any]],
    freshness: list[dict[str, Any]],
    built_at: str,
) -> dict[str, Any]:
    metric_catalog: dict[str, dict[str, Any]] = {}
    for row in observations:
        metric_catalog.setdefault(
            row["metric_id"],
            {
                "metricId": row["metric_id"],
                "metricName": row["metric_name"],
                "domain": row["domain"],
                "unit": row["unit"],
                "frequency": row["frequency"],
                "sourceId": row["source_id"],
                "sourceTier": row["source_tier"],
                "calculation": row["calculation"],
                "notes": row["notes"],
            },
        )
    public_observations = [
        {
            "domain": row["domain"],
            "metricId": row["metric_id"],
            "countyFips": row["county_fips"],
            "countyName": row["county_name"],
            "period": row["period"],
            "periodEnd": row["period_end"],
            "value": row["value"],
            "unit": row["unit"],
            "marginOfError": row["margin_of_error"],
            "sourceId": row["source_id"],
            "sourceRelease": row["source_release"],
        }
        for row in observations
    ]
    return {
        "project": "The Bay Outlook",
        "product": "Housing Observatory",
        "version": PHASE14_VERSION,
        "builtAt": built_at,
        "evidenceAsOf": max(row["period_end"] for row in observations),
        "countyCount": len(COUNTY_BY_FIPS),
        "domainCount": len(REQUIRED_DOMAINS),
        "metricCount": len(metric_catalog),
        "observationCount": len(observations),
        "counties": [
            {"countyFips": fips, "countyName": name} for fips, name in COUNTY_BY_FIPS.items()
        ],
        "domains": sorted(REQUIRED_DOMAINS),
        "metricCatalog": sorted(metric_catalog.values(), key=lambda item: item["metricId"]),
        "observations": public_observations,
        "rhnaProgress": progress_rows,
        "sources": freshness,
        "limitations": [
            "ACS values are five-year pooled estimates; adjacent releases overlap and are not independent annual samples.",
            "Redfin is a documented private primary-market source, not an official government statistic, and its data are revised.",
            "Building permits authorize construction; they do not prove that a unit was started or completed.",
            "RHNA progress sums jurisdiction-reported permits and compares them with allocations; it is not a housing-needs forecast.",
            "Affordability ratios are descriptive ratios of medians and omit financing terms, taxes, insurance, and household-level variation.",
        ],
        "publicationBoundary": {
            "dataStatus": "public descriptive evidence",
            "automatedNarrative": False,
            "newAnalysisRequiresHumanApproval": True,
            "phase10ReportStatus": "human_approval_hold",
        },
    }


def build_phase14(
    *,
    root: Path | None = None,
    built_at: str | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    root = (root or PROJECT_ROOT).resolve()
    built_at = built_at or _utc_now()
    output = root / "data" / "phase14"
    exports = output / "exports"
    manifest_path = output / "phase14_manifest.json"
    if not refresh:
        if not (exports / "housing_time_series.csv").is_file():
            raise FileNotFoundError("Phase 14 offline build requires existing housing exports")
        return _build_phase14_manifest(root=root, built_at=built_at)

    observations: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    acs_rows, acs_snapshots = _acs_observations(data_root=output, retrieved_at=built_at)
    observations.extend(acs_rows)
    snapshots.extend(acs_snapshots)
    bps_rows, bps_snapshots = _bps_observations(data_root=output, retrieved_at=built_at)
    observations.extend(bps_rows)
    snapshots.extend(bps_snapshots)
    redfin_rows, redfin_snapshots = _redfin_observations(data_root=output, retrieved_at=built_at)
    observations.extend(redfin_rows)
    snapshots.extend(redfin_snapshots)
    hcd_rows, hcd_snapshots, progress_rows = _hcd_observations(
        data_root=output,
        retrieved_at=built_at,
    )
    observations.extend(hcd_rows)
    snapshots.extend(hcd_snapshots)
    observations.sort(
        key=lambda row: (row["domain"], row["metric_id"], row["county_fips"], row["period_end"])
    )
    snapshots.sort(key=lambda row: (row["source_id"], row["source_release"], row["raw_path"]))

    checks = _quality_checks(observations, snapshots)
    failed_checks = [row for row in checks if not row["passed"]]
    if failed_checks:
        raise ValueError(f"Phase 14 quality checks failed: {failed_checks}")

    snapshot_rows = _latest_snapshot(observations)
    freshness = _source_freshness(observations, snapshots)
    _write_csv(exports / "housing_time_series.csv", observations, OBSERVATION_FIELDS)
    _write_csv(exports / "housing_snapshot.csv", snapshot_rows, OBSERVATION_FIELDS)
    _write_csv(exports / "rhna_progress.csv", progress_rows)
    _write_csv(exports / "source_freshness.csv", freshness)
    _write_csv(exports / "source_snapshots.csv", snapshots)
    _write_csv(exports / "quality_checks.csv", checks)

    database_path = output / "housing_observatory.sqlite"
    _build_database(database_path, observations, snapshots, checks, built_at)
    payload = _public_payload(
        observations=observations,
        progress_rows=progress_rows,
        freshness=freshness,
        built_at=built_at,
    )
    payload_path = output / "public" / "housing-data.json"
    _write_json(payload_path, payload)

    manifest_result = _build_phase14_manifest(root=root, built_at=built_at)
    manifest_result["build"] = {
        "observations": len(observations),
        "metrics": len({row["metric_id"] for row in observations}),
        "snapshots": len(snapshots),
        "quality_checks": len(checks),
    }
    return manifest_result


def _build_phase14_manifest(*, root: Path, built_at: str) -> dict[str, Any]:
    output = root / "data" / "phase14"
    manifest_path = output / "phase14_manifest.json"
    time_series_path = output / "exports" / "housing_time_series.csv"
    with time_series_path.open(newline="", encoding="utf-8") as stream:
        observations = list(csv.DictReader(stream))
    snapshot_path = output / "exports" / "source_snapshots.csv"
    with snapshot_path.open(newline="", encoding="utf-8") as stream:
        snapshots = list(csv.DictReader(stream))
    quality_path = output / "exports" / "quality_checks.csv"
    with quality_path.open(newline="", encoding="utf-8") as stream:
        quality = list(csv.DictReader(stream))
    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    files = {
        relative: _sha256(root / relative)
        for relative in REQUIRED_FILES
        if (root / relative).is_file()
    }
    manifest = {
        "project": "The Bay Outlook",
        "phase": 14,
        "product_version": PHASE14_VERSION,
        "built_at": built_at,
        "completion_state": "complete" if not missing else "incomplete",
        "scope": sorted(REQUIRED_DOMAINS),
        "counts": {
            "counties": len({row["county_fips"] for row in observations}),
            "domains": len({row["domain"] for row in observations}),
            "metrics": len({row["metric_id"] for row in observations}),
            "observations": len(observations),
            "sources": len({row["source_id"] for row in observations}),
            "source_snapshots": len(snapshots),
            "quality_checks": len(quality),
        },
        "publication_boundary": {
            "automated_narrative": False,
            "new_analysis_requires_human_approval": True,
            "phase10_report_status": "human_approval_hold",
        },
        "missing_required_files": missing,
        "files": files,
    }
    _write_json(manifest_path, manifest)
    verification = verify_phase14(manifest_path=manifest_path, root=root)
    manifest["verification"] = {
        "complete": verification["complete"],
        "passing": verification["passing"],
        "total": verification["total"],
        "failed": verification["failed"],
    }
    _write_json(manifest_path, manifest)
    return {"manifest": str(manifest_path), "verification": verification}


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def verify_phase14(
    manifest_path: Path | None = None,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or PROJECT_ROOT).resolve()
    manifest_path = manifest_path or root / "data" / "phase14" / "phase14_manifest.json"
    manifest = _read_json(manifest_path)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "manifest_identity",
            manifest.get("project") == "The Bay Outlook"
            and manifest.get("phase") == 14
            and manifest.get("product_version") == PHASE14_VERSION,
            {
                "project": manifest.get("project"),
                "phase": manifest.get("phase"),
                "version": manifest.get("product_version"),
            },
        )
    )

    missing = [relative for relative in REQUIRED_FILES if not (root / relative).is_file()]
    checks.append(_check("required_public_files", not missing, missing))

    payload_path = root / "data" / "phase14" / "public" / "housing-data.json"
    payload = _read_json(payload_path) if payload_path.is_file() else {}
    checks.append(
        _check(
            "seven_housing_domains",
            set(payload.get("domains", [])) == REQUIRED_DOMAINS,
            payload.get("domains", []),
        )
    )
    checks.append(
        _check(
            "nine_county_coverage",
            payload.get("countyCount") == 9
            and {row.get("countyFips") for row in payload.get("counties", [])} == set(COUNTY_BY_FIPS),
            payload.get("counties", []),
        )
    )

    observation_rows = payload.get("observations", [])
    history: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in observation_rows:
        if row.get("value") is not None:
            history[(row["metricId"], row["countyFips"])].add(row["period"])
    historical_passed = all(
        len(history[(metric, fips)]) >= minimum
        for metric, minimum in (("median_gross_rent", 4), ("homes_sold", 60), ("permitted_units_total", 5))
        for fips in COUNTY_BY_FIPS
    )
    checks.append(_check("three_plus_year_history", historical_passed, {"required": {"acs": 4, "redfin_months": 60, "bps": 5}}))

    source_ids = {row.get("sourceId") for row in observation_rows}
    source_passed = source_ids == set(SOURCE_REGISTRY) and len(payload.get("sources", [])) == len(SOURCE_REGISTRY)
    checks.append(_check("documented_source_lineage", source_passed, sorted(source_ids)))

    raw_errors: list[dict[str, str]] = []
    raw_root = root / "data" / "phase14" / "raw"
    for metadata_path in raw_root.rglob("*.metadata.json"):
        metadata = _read_json(metadata_path)
        raw_path = metadata_path.with_suffix("").with_suffix(metadata_path.suffix.replace(".json", ""))
        # .metadata.json is appended to the original suffix by save_snapshot.
        raw_path = Path(str(metadata_path).removesuffix(".metadata.json"))
        if not raw_path.is_file():
            raw_errors.append({"file": str(raw_path), "reason": "missing"})
        elif _sha256(raw_path) != metadata.get("sha256"):
            raw_errors.append({"file": str(raw_path), "reason": "hash_mismatch"})
        if metadata.get("snapshot_kind") == "source_slice" and not metadata.get("upstream_sha256"):
            raw_errors.append({"file": str(metadata_path), "reason": "missing_upstream_digest"})
    checks.append(_check("immutable_raw_evidence", not raw_errors and len(list(raw_root.rglob("*.metadata.json"))) >= 35, raw_errors))

    database = root / "data" / "phase14" / "housing_observatory.sqlite"
    database_detail: dict[str, Any] = {"exists": database.is_file()}
    database_passed = False
    if database.is_file():
        with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            failed_quality = connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed = 0").fetchone()[0]
            count = connection.execute("SELECT COUNT(*) FROM housing_observation").fetchone()[0]
        database_detail.update(
            {"integrity": integrity, "foreign_keys": len(foreign_keys), "failed_quality": failed_quality, "observations": count}
        )
        database_passed = integrity == "ok" and not foreign_keys and failed_quality == 0 and count == payload.get("observationCount")
    checks.append(_check("database_integrity", database_passed, database_detail))

    recomputation_errors: list[dict[str, Any]] = []
    by_key = {(row["metricId"], row["countyFips"], row["period"]): row for row in observation_rows}
    for fips in COUNTY_BY_FIPS:
        for year in map(str, ACS_YEARS):
            rent = by_key.get(("median_gross_rent", fips, year), {}).get("value")
            income = by_key.get(("median_household_income", fips, year), {}).get("value")
            ratio = by_key.get(("rent_to_income_pct", fips, year), {}).get("value")
            expected = _ratio(_number(rent) * 12 if _number(rent) is not None else None, _number(income))
            if expected is None or ratio is None or abs(float(ratio) - expected) > 1e-9:
                recomputation_errors.append({"county": fips, "period": year, "metric": "rent_to_income_pct"})
    checks.append(_check("calculation_reproducibility", not recomputation_errors, recomputation_errors))

    workflow = root / ".github" / "workflows" / "housing-observatory-update.yml"
    workflow_text = workflow.read_text(encoding="utf-8") if workflow.is_file() else ""
    workflow_tokens = (
        "workflow_dispatch",
        "schedule:",
        "verify-phase14",
        "human-review-required",
        "contents: read",
        "No public deployment",
    )
    missing_tokens = [token for token in workflow_tokens if token not in workflow_text]
    checks.append(_check("responsible_update_workflow", not missing_tokens, missing_tokens))

    boundary = manifest.get("publication_boundary", {})
    boundary_passed = (
        boundary.get("automated_narrative") is False
        and boundary.get("new_analysis_requires_human_approval") is True
        and boundary.get("phase10_report_status") == "human_approval_hold"
    )
    checks.append(_check("human_publication_boundary", boundary_passed, boundary))

    hash_errors: list[dict[str, str]] = []
    for relative, expected in manifest.get("files", {}).items():
        path = root / relative
        if not path.is_file():
            hash_errors.append({"file": relative, "reason": "missing"})
        elif _sha256(path) != expected:
            hash_errors.append({"file": relative, "reason": "hash_mismatch"})
    checks.append(_check("manifest_file_hashes", not hash_errors, hash_errors))

    failed = [item["check"] for item in checks if not item["passed"]]
    return {
        "phase": 14,
        "product_version": PHASE14_VERSION,
        "complete": not failed,
        "passing": len(checks) - len(failed),
        "total": len(checks),
        "failed": failed,
        "checks": checks,
    }
