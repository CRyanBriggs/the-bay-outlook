from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import COUNTY_BY_FIPS, PROJECT_ROOT
from .phase14_access import verify_phase14_access


PHASE14_PRODUCTION_VERSION = "1.3.0"
PRODUCTION_YEARS = tuple(range(2018, 2026))
REQUIRED_PRODUCTION_DOMAINS = {
    "accessory_dwelling_units",
    "housing_applications",
    "housing_element_compliance",
    "permitting_timelines",
    "production_pipeline",
    "rhna_delivery",
    "zoning_and_rezoning",
}

COUNTY_FIPS_BY_NAME = {name.casefold(): fips for fips, name in COUNTY_BY_FIPS.items()}
ZONING_COUNTY_CODES = {
    "06001": "ALA",
    "06013": "CCO",
    "06041": "MRN",
    "06055": "NAP",
    "06075": "SFR",
    "06081": "SMA",
    "06085": "SCL",
    "06095": "SOL",
    "06097": "SON",
}

APR_TABLE_A_URL = (
    "https://data.ca.gov/dataset/81b0841f-2802-403e-b48e-2ef4b751f77c/"
    "resource/c78b769d-cc02-4050-91ef-79ded665b5a8/download/tablea.csv"
)
APR_TABLE_A2_URL = (
    "https://data.ca.gov/dataset/81b0841f-2802-403e-b48e-2ef4b751f77c/"
    "resource/fe505d9b-8c36-42ba-ba30-08bc4f34e022/download/tablea2.csv"
)
APR_TABLE_C_URL = (
    "https://data.ca.gov/dataset/81b0841f-2802-403e-b48e-2ef4b751f77c/"
    "resource/a07dcd90-56bd-4a5e-9ce6-809e1f3fc121/download/tablec.csv"
)
COMPLIANCE_URL = (
    "https://data.ca.gov/dataset/55537b9f-0c54-456d-b76a-90c157718975/"
    "resource/2dcd1cd4-1348-4fc5-9c9c-219f82daac00/download/housing_element.csv"
)
ZONING_QUERY_URL = (
    "https://services8.arcgis.com/Xr1lDrwMv89PhjD9/arcgis/rest/services/"
    "California_Statewide_Zoning_North/FeatureServer/1/query"
)
RHNA_PACKAGE_URL = "https://data.ca.gov/api/3/action/package_show?id=rhna-progress-report"
RHNA_DATA_URL = (
    "https://data.ca.gov/api/3/action/datastore_search?"
    "resource_id=1e80a9cf-724c-432d-8374-e9708a6a92dc&limit=1000"
)

PRODUCTION_SOURCE_REGISTRY = {
    "CA_HCD_APR_TABLE_A": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Housing Element Annual Progress Report — Table A",
        "source_tier": 1,
        "source_class": "government administrative data",
        "frequency": "annual with portal updates",
        "geography_basis": "reporting jurisdiction aggregated to county",
        "landing_url": "https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard",
    },
    "CA_HCD_APR_TABLE_A2": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Housing Element Annual Progress Report — Table A2",
        "source_tier": 1,
        "source_class": "government administrative data",
        "frequency": "annual with portal updates",
        "geography_basis": "reporting jurisdiction aggregated to county",
        "landing_url": "https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard",
    },
    "CA_HCD_APR_TABLE_C": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Housing Element Annual Progress Report — Table C",
        "source_tier": 1,
        "source_class": "government administrative data",
        "frequency": "annual with portal updates",
        "geography_basis": "conditionally reported jurisdiction records aggregated to county",
        "landing_url": "https://www.hcd.ca.gov/housing-open-data-tools/apr-dashboard",
    },
    "CA_HCD_RHNA": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Sixth-Cycle Regional Housing Needs Allocation",
        "source_tier": 1,
        "source_class": "government planning allocation",
        "frequency": "planning cycle",
        "geography_basis": "jurisdiction allocation aggregated to county",
        "landing_url": "https://www.hcd.ca.gov/rhna",
    },
    "CA_HCD_HOUSING_ELEMENT_COMPLIANCE": {
        "publisher": "California Department of Housing and Community Development",
        "title": "Housing Element Compliance Report",
        "source_tier": 1,
        "source_class": "government administrative status",
        "frequency": "current status with portal updates",
        "geography_basis": "reporting jurisdiction aggregated to county",
        "landing_url": (
            "https://www.hcd.ca.gov/housing-open-data-tools/"
            "housing-element-review-compliance-report"
        ),
    },
    "CA_LCI_STATEWIDE_ZONING": {
        "publisher": "California Office of Land Use and Climate Innovation",
        "title": "California Statewide Zoning North",
        "source_tier": 1,
        "source_class": "government compiled geospatial inventory",
        "frequency": "irregular",
        "geography_basis": "local zoning polygons grouped to county",
        "landing_url": "https://data.ca.gov/dataset/california-statewide-zoning-north",
    },
}

SNAPSHOT_FIELDS = (
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
    "row_count",
)

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
    "source_ids",
    "source_releases",
    "retrieved_at",
    "raw_sha256s",
    "calculation",
    "notes",
    "comparability_status",
)

REQUIRED_PRODUCTION_FILES = (
    "config/phase14_housing_production.json",
    "metadata/housing_production_indicator_catalog.csv",
    "metadata/housing_production_source_registry.csv",
    "docs/housing-production/README.md",
    "docs/housing-production/METHODOLOGY.md",
    "docs/housing-production/DATA_DICTIONARY.md",
    "docs/housing-production/LIMITATIONS.md",
    "docs/housing-production/RUNBOOK.md",
    "src/bay_outlook/phase14_production.py",
    "tests/test_phase14_production.py",
    ".github/workflows/housing-production-update.yml",
    "data/phase14/production/exports/production_time_series.csv",
    "data/phase14/production/exports/production_snapshot.csv",
    "data/phase14/production/exports/timeline_coverage.csv",
    "data/phase14/production/exports/rhna_delivery.csv",
    "data/phase14/production/exports/zoning_composition.csv",
    "data/phase14/production/exports/rezone_breakdown.csv",
    "data/phase14/production/exports/compliance_records.csv",
    "data/phase14/production/exports/source_freshness.csv",
    "data/phase14/production/exports/source_snapshots.csv",
    "data/phase14/production/exports/quality_checks.csv",
    "data/phase14/production/public/housing-production-policy-data.json",
)


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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
            stream, fieldnames=names, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def _snake(value: str) -> str:
    output = []
    for character in value:
        if character.isupper() and output:
            output.append("_")
        output.append(character.lower())
    return "".join(output)


def _snake_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{_snake(key): value for key, value in row.items()} for row in rows]


def _download(url: str, target: Path, *, attempts: int = 3) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "The-Bay-Outlook/1.3"})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    shutil.copyfileobj(response, handle, length=1024 * 1024)
            if temporary.stat().st_size == 0:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"zero-byte response from {url}")
            os.replace(temporary, target)
            return target
        except Exception as error:  # pragma: no cover - network-specific branch
            last_error = error
            time.sleep(1 + attempt)
    raise RuntimeError(f"unable to retrieve {url}: {last_error}")


def _metadata_row(
    *,
    root: Path,
    path: Path,
    metadata_path: Path,
    source_id: str,
    source_url: str,
    source_release: str,
    retrieved_at: str,
    upstream_sha256: str,
    upstream_byte_count: int,
    snapshot_kind: str,
    row_count: int,
) -> dict[str, Any]:
    snapshot_sha256 = _sha256(path)
    metadata = {
        "byte_count": path.stat().st_size,
        "dataset_status": "official",
        "retrieved_at": retrieved_at,
        "row_count": row_count,
        "scope": "nine-county San Francisco Bay Area",
        "sha256": snapshot_sha256,
        "snapshot_kind": snapshot_kind,
        "source_id": source_id,
        "source_release": source_release,
        "source_url": source_url,
        "upstream_byte_count": upstream_byte_count,
        "upstream_sha256": upstream_sha256,
    }
    _write_json(metadata_path, metadata)
    return {
        "source_id": source_id,
        "source_url": source_url,
        "source_release": source_release,
        "retrieved_at": retrieved_at,
        "snapshot_sha256": snapshot_sha256,
        "upstream_sha256": upstream_sha256,
        "byte_count": path.stat().st_size,
        "upstream_byte_count": upstream_byte_count,
        "snapshot_kind": snapshot_kind,
        "raw_path": path.relative_to(root).as_posix(),
        "metadata_path": metadata_path.relative_to(root).as_posix(),
        "row_count": row_count,
    }


def _annual_slices(
    *,
    root: Path,
    source: Path,
    raw_dir: Path,
    source_id: str,
    source_url: str,
    table_name: str,
    retrieved_at: str,
) -> list[dict[str, Any]]:
    upstream_sha256 = _sha256(source)
    upstream_byte_count = source.stat().st_size
    destinations = {
        year: raw_dir / source_id / retrieved_at[:10] / f"{table_name.lower()}-{year}-bay-counties.csv"
        for year in PRODUCTION_YEARS
    }
    for path in destinations.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    counts = {year: 0 for year in PRODUCTION_YEARS}
    with source.open(newline="", encoding="utf-8-sig") as input_stream, ExitStack() as stack:
        reader = csv.DictReader(input_stream)
        if not reader.fieldnames:
            raise ValueError(f"missing CSV header in {source}")
        handles = {
            year: stack.enter_context(path.open("w", newline="", encoding="utf-8"))
            for year, path in destinations.items()
        }
        writers = {
            year: csv.DictWriter(handle, fieldnames=reader.fieldnames)
            for year, handle in handles.items()
        }
        for writer in writers.values():
            writer.writeheader()
        for row in reader:
            try:
                year = int(str(row.get("YEAR", "")).strip())
            except ValueError:
                continue
            county = str(row.get("CNTY_NAME", "")).strip().casefold()
            if year in writers and county in COUNTY_FIPS_BY_NAME:
                writers[year].writerow(row)
                counts[year] += 1
    results = []
    for year, path in destinations.items():
        release = f"HCD-APR-{table_name.upper()}-{year}-portal-current"
        results.append(
            _metadata_row(
                root=root,
                path=path,
                metadata_path=path.with_suffix(path.suffix + ".metadata.json"),
                source_id=source_id,
                source_url=source_url,
                source_release=release,
                retrieved_at=retrieved_at,
                upstream_sha256=upstream_sha256,
                upstream_byte_count=upstream_byte_count,
                snapshot_kind="source_slice",
                row_count=counts[year],
            )
        )
    return results


def _filtered_slice(
    *,
    root: Path,
    source: Path,
    path: Path,
    source_id: str,
    source_url: str,
    source_release: str,
    retrieved_at: str,
    county_field: str,
    year_field: str | None = None,
) -> dict[str, Any]:
    upstream_sha256 = _sha256(source)
    upstream_byte_count = source.stat().st_size
    path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with source.open(newline="", encoding="utf-8-sig") as input_stream, path.open(
        "w", newline="", encoding="utf-8"
    ) as output_stream:
        reader = csv.DictReader(input_stream)
        if not reader.fieldnames:
            raise ValueError(f"missing CSV header in {source}")
        writer = csv.DictWriter(output_stream, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            county = str(row.get(county_field, "")).strip().casefold()
            if county not in COUNTY_FIPS_BY_NAME:
                continue
            if year_field:
                try:
                    year = int(str(row.get(year_field, "")).strip())
                except ValueError:
                    continue
                if year not in PRODUCTION_YEARS:
                    continue
            writer.writerow(row)
            row_count += 1
    return _metadata_row(
        root=root,
        path=path,
        metadata_path=path.with_suffix(path.suffix + ".metadata.json"),
        source_id=source_id,
        source_url=source_url,
        source_release=source_release,
        retrieved_at=retrieved_at,
        upstream_sha256=upstream_sha256,
        upstream_byte_count=upstream_byte_count,
        snapshot_kind="source_slice",
        row_count=row_count,
    )


def _zoning_snapshot(root: Path, raw_dir: Path, retrieved_at: str, cache: Path | None) -> dict[str, Any]:
    codes = ",".join(f"'{code}'" for code in ZONING_COUNTY_CODES.values())
    parameters = {
        "where": f"County IN ({codes})",
        "outStatistics": json.dumps(
            [
                {
                    "statisticType": "count",
                    "onStatisticField": "OBJECTID",
                    "outStatisticFieldName": "feature_count",
                },
                {
                    "statisticType": "sum",
                    "onStatisticField": "Shape__Area",
                    "outStatisticFieldName": "area_sum",
                },
            ],
            separators=(",", ":"),
        ),
        "groupByFieldsForStatistics": "County,New_Class,Date",
        "orderByFields": "County,New_Class,Date",
        "returnGeometry": "false",
        "f": "json",
    }
    source_url = f"{ZONING_QUERY_URL}?{urllib.parse.urlencode(parameters)}"
    if cache and cache.is_file():
        raw_bytes = cache.read_bytes()
    else:
        request = urllib.request.Request(source_url, headers={"User-Agent": "The-Bay-Outlook/1.3"})
        with urllib.request.urlopen(request, timeout=180) as response:
            raw_bytes = response.read()
    value = json.loads(raw_bytes.decode("utf-8"))
    if value.get("error") or value.get("exceededTransferLimit"):
        raise ValueError(f"zoning query was incomplete: {value.get('error') or value}")
    attributes = [feature["attributes"] for feature in value.get("features", [])]
    if not attributes:
        raise ValueError("zoning query returned no records")
    payload = {
        "query": parameters,
        "records": attributes,
        "source_item_vintage": "2024-12-19",
    }
    path = raw_dir / "CA_LCI_STATEWIDE_ZONING" / retrieved_at[:10] / "zoning-bay-county-class-date.json"
    _write_json(path, payload)
    return _metadata_row(
        root=root,
        path=path,
        metadata_path=path.with_suffix(path.suffix + ".metadata.json"),
        source_id="CA_LCI_STATEWIDE_ZONING",
        source_url=source_url,
        source_release="California-Statewide-Zoning-North-v2024-12-19",
        retrieved_at=retrieved_at,
        upstream_sha256=_sha256_bytes(raw_bytes),
        upstream_byte_count=len(raw_bytes),
        snapshot_kind="official_query_aggregate",
        row_count=len(attributes),
    )


def _rhna_snapshots(root: Path, raw_dir: Path, retrieved_at: str) -> list[dict[str, Any]]:
    inherited = root / "data" / "phase14" / "raw" / "CA_HCD_RHNA" / "2026-08-08"
    inherited_files = (
        inherited / "rhna-package-metadata.json",
        inherited / "sixth-cycle-rhna-progress.json",
    )
    if all(path.is_file() and path.with_suffix(path.suffix + ".metadata.json").is_file() for path in inherited_files):
        results = []
        for inherited_path in inherited_files:
            inherited_metadata = _read_json(
                inherited_path.with_suffix(inherited_path.suffix + ".metadata.json")
            )
            path = raw_dir / "CA_HCD_RHNA" / inherited_metadata["retrieved_at"][:10] / inherited_path.name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inherited_path, path)
            snapshot_sha256 = _sha256(path)
            results.append(
                _metadata_row(
                    root=root,
                    path=path,
                    metadata_path=path.with_suffix(path.suffix + ".metadata.json"),
                    source_id="CA_HCD_RHNA",
                    source_url=inherited_metadata["source_url"],
                    source_release=inherited_metadata["source_release"],
                    retrieved_at=inherited_metadata["retrieved_at"],
                    upstream_sha256=snapshot_sha256,
                    upstream_byte_count=path.stat().st_size,
                    snapshot_kind="complete_official_response",
                    row_count=1,
                )
            )
        return results
    results = []
    for filename, source_url, release in (
        ("rhna-package-metadata.json", RHNA_PACKAGE_URL, "HCD-RHNA-package-metadata"),
        ("sixth-cycle-rhna-progress.json", RHNA_DATA_URL, "HCD-RHNA-sixth-cycle"),
    ):
        path = raw_dir / "CA_HCD_RHNA" / retrieved_at[:10] / filename
        _download(source_url, path)
        results.append(
            _metadata_row(
                root=root,
                path=path,
                metadata_path=path.with_suffix(path.suffix + ".metadata.json"),
                source_id="CA_HCD_RHNA",
                source_url=source_url,
                source_release=release,
                retrieved_at=retrieved_at,
                upstream_sha256=_sha256(path),
                upstream_byte_count=path.stat().st_size,
                snapshot_kind="complete_official_response",
                row_count=1,
            )
        )
    return results


def _build_snapshots(
    root: Path,
    built_at: str,
    *,
    refresh: bool,
    source_cache: Path | None,
) -> list[dict[str, Any]]:
    output = root / "data" / "phase14" / "production"
    existing = output / "exports" / "source_snapshots.csv"
    if not refresh and existing.is_file():
        with existing.open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))
    raw_dir = output / "raw"
    cache_dir = source_cache or (output / ".source-cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        "tablea.csv": APR_TABLE_A_URL,
        "tablea2.csv": APR_TABLE_A2_URL,
        "tablec.csv": APR_TABLE_C_URL,
        "housing_element.csv": COMPLIANCE_URL,
    }
    for filename, url in sources.items():
        path = cache_dir / filename
        if not path.is_file():
            _download(url, path)
    snapshots = []
    snapshots.extend(
        _annual_slices(
            root=root,
            source=cache_dir / "tablea.csv",
            raw_dir=raw_dir,
            source_id="CA_HCD_APR_TABLE_A",
            source_url=APR_TABLE_A_URL,
            table_name="TABLE-A",
            retrieved_at=built_at,
        )
    )
    snapshots.extend(
        _annual_slices(
            root=root,
            source=cache_dir / "tablea2.csv",
            raw_dir=raw_dir,
            source_id="CA_HCD_APR_TABLE_A2",
            source_url=APR_TABLE_A2_URL,
            table_name="TABLE-A2",
            retrieved_at=built_at,
        )
    )
    table_c_path = raw_dir / "CA_HCD_APR_TABLE_C" / built_at[:10] / "tablec-2018-2025-bay-counties.csv"
    snapshots.append(
        _filtered_slice(
            root=root,
            source=cache_dir / "tablec.csv",
            path=table_c_path,
            source_id="CA_HCD_APR_TABLE_C",
            source_url=APR_TABLE_C_URL,
            source_release="HCD-APR-TABLE-C-portal-current",
            retrieved_at=built_at,
            county_field="COUNTY",
            year_field="YEAR",
        )
    )
    compliance_path = (
        raw_dir
        / "CA_HCD_HOUSING_ELEMENT_COMPLIANCE"
        / built_at[:10]
        / "housing-element-compliance-bay-counties.csv"
    )
    snapshots.append(
        _filtered_slice(
            root=root,
            source=cache_dir / "housing_element.csv",
            path=compliance_path,
            source_id="CA_HCD_HOUSING_ELEMENT_COMPLIANCE",
            source_url=COMPLIANCE_URL,
            source_release=f"HCD-housing-element-compliance-{built_at[:10]}",
            retrieved_at=built_at,
            county_field="County",
        )
    )
    snapshots.append(
        _zoning_snapshot(
            root,
            raw_dir,
            built_at,
            (cache_dir / "zoning-aggregate.json") if (cache_dir / "zoning-aggregate.json").is_file() else None,
        )
    )
    snapshots.extend(_rhna_snapshots(root, raw_dir, built_at))
    snapshots.sort(key=lambda row: (row["source_id"], row["source_release"]))
    if len(snapshots) != 21:
        raise ValueError(f"expected 21 source snapshots, built {len(snapshots)}")
    return snapshots


def _observation_rows(payload: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    release_hash = {row["source_release"]: row["snapshot_sha256"] for row in snapshots}
    rows = []
    for item in payload["observations"]:
        releases = list(item["sourceReleases"])
        missing = [release for release in releases if release not in release_hash]
        if missing:
            raise ValueError(f"observation references missing releases: {missing}")
        rows.append(
            {
                "domain": item["domain"],
                "metric_id": item["metricId"],
                "metric_name": next(
                    metric["metricName"]
                    for metric in payload["metricCatalog"]
                    if metric["metricId"] == item["metricId"]
                ),
                "county_fips": item["countyFips"],
                "county_name": item["countyName"],
                "period": item["period"],
                "period_end": item["periodEnd"],
                "frequency": next(
                    metric["frequency"]
                    for metric in payload["metricCatalog"]
                    if metric["metricId"] == item["metricId"]
                ),
                "value": item["value"],
                "unit": item["unit"],
                "source_ids": ";".join(item["sourceIds"]),
                "source_releases": ";".join(releases),
                "retrieved_at": payload["builtAt"],
                "raw_sha256s": ";".join(release_hash[release] for release in releases),
                "calculation": next(
                    metric["calculation"]
                    for metric in payload["metricCatalog"]
                    if metric["metricId"] == item["metricId"]
                ),
                "notes": next(
                    metric["notes"]
                    for metric in payload["metricCatalog"]
                    if metric["metricId"] == item["metricId"]
                ),
                "comparability_status": item["comparabilityStatus"],
            }
        )
    return rows


def _quality_checks(payload: dict[str, Any], snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: Any) -> None:
        checks.append({"check_name": name, "passed": int(bool(passed)), "detail": detail})

    observations = payload["observations"]
    domains = {row["domain"] for row in observations}
    add("seven_domain_scope", domains == REQUIRED_PRODUCTION_DOMAINS, sorted(domains))

    critical = {
        "accessory_dwelling_units": "adu_building_permit_units",
        "housing_applications": "application_project_records",
        "housing_element_compliance": "housing_element_jurisdictions",
        "permitting_timelines": "timeline_match_rate_pct",
        "production_pipeline": "building_permit_units",
        "rhna_delivery": "rhna_permit_progress_pct",
        "zoning_and_rezoning": "mapped_zoning_area_sq_km",
    }
    coverage = {
        domain: sorted(
            {row["countyFips"] for row in observations if row["metricId"] == metric}
        )
        for domain, metric in critical.items()
    }
    add(
        "nine_county_critical_coverage",
        all(set(value) == set(COUNTY_BY_FIPS) for value in coverage.values()),
        coverage,
    )

    history = defaultdict(set)
    for row in observations:
        if len(row["period"]) == 4 and row["period"].isdigit():
            history[(row["metricId"], row["countyFips"])].add(row["period"])
    annual_metrics = {
        row["metricId"]
        for row in observations
        if len(row["period"]) == 4 and row["period"].isdigit()
    }
    history_errors = {
        f"{metric}:{fips}": len(history[(metric, fips)])
        for metric in annual_metrics
        for fips in COUNTY_BY_FIPS
        if len(history[(metric, fips)]) != 8
    }
    add("eight_year_annual_history", not history_errors, history_errors)

    keys = defaultdict(int)
    for row in observations:
        keys[(row["metricId"], row["countyFips"], row["period"])] += 1
    duplicates = [list(key) for key, count in keys.items() if count != 1]
    add("natural_key_uniqueness", not duplicates, duplicates[:20])

    open_ended_ratio_metrics = {
        "annual_completion_to_permit_flow_pct",
        "rhna_completion_progress_pct",
        "rhna_completion_to_permit_flow_pct",
        "rhna_permit_progress_pct",
    }
    invalid_percent = [
        {"metric": row["metricId"], "county": row["countyFips"], "value": row["value"]}
        for row in observations
        if row["unit"] == "percent"
        and row["value"] is not None
        and (
            not math.isfinite(float(row["value"]))
            or float(row["value"]) < 0
            or (
                row["metricId"] not in open_ended_ratio_metrics
                and float(row["value"]) > 100
            )
        )
    ]
    add("bounded_ratio_ranges", not invalid_percent, invalid_percent[:20])

    source_ids = {source for row in observations for source in row["sourceIds"]}
    releases = {release for row in observations for release in row["sourceReleases"]}
    snapshot_releases = {row["source_release"] for row in snapshots}
    add(
        "complete_source_lineage",
        source_ids == set(PRODUCTION_SOURCE_REGISTRY) and releases <= snapshot_releases,
        {"sources": sorted(source_ids), "unresolved_releases": sorted(releases - snapshot_releases)},
    )

    boundaries = payload["interpretationBoundaries"]
    add(
        "stage_flow_boundaries",
        boundaries.get("stageFunnelProduced") is False
        and boundaries.get("cohortCompletionRateProduced") is False
        and boundaries.get("countyRankingProduced") is False
        and boundaries.get("policyScoreProduced") is False,
        boundaries,
    )

    timeline_errors = []
    for row in payload["timelineCoverage"]:
        expected = row["exact_tracking_id_matches"] + row["exact_apn_matches"]
        expected_rate = row["matched_projects"] / row["eligible_application_projects"] * 100
        if expected != row["matched_projects"] or abs(expected_rate - row["match_rate_pct"]) > 1e-9:
            timeline_errors.append(row["county_fips"])
    add(
        "deterministic_timeline_coverage",
        not timeline_errors
        and sum(row["matched_projects"] for row in payload["timelineCoverage"]) == 28814,
        timeline_errors,
    )

    zoning_errors = []
    grouped = defaultdict(float)
    for row in payload["zoningComposition"]:
        grouped[row["county_fips"]] += float(row["share_of_mapped_area_pct"])
    for fips, value in grouped.items():
        if abs(value - 100.0) > 1e-6:
            zoning_errors.append({"county": fips, "share": value})
    add("zoning_composition_reproducibility", not zoning_errors and len(grouped) == 9, zoning_errors)

    rhna_errors = []
    for row in payload["rhnaDelivery"]:
        permit_pct = row["permitted_2023_2025"] / row["rhna_allocation"] * 100
        completion_pct = row["completed_2023_2025"] / row["rhna_allocation"] * 100
        if (
            abs(permit_pct - row["permit_progress_pct"]) > 1e-9
            or abs(completion_pct - row["completion_progress_pct"]) > 1e-9
            or row["permit_gap_units"] != row["rhna_allocation"] - row["permitted_2023_2025"]
            or row["completion_gap_units"] != row["rhna_allocation"] - row["completed_2023_2025"]
        ):
            rhna_errors.append(row["county_fips"])
    add("rhna_calculation_reproducibility", not rhna_errors and len(payload["rhnaDelivery"]) == 9, rhna_errors)
    return checks


def _build_database(
    path: Path,
    payload: dict[str, Any],
    observations: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    checks: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="phase14-production-", suffix=".sqlite", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with sqlite3.connect(temporary) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE build_metadata (metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL);
                CREATE TABLE source_registry (
                    source_id TEXT PRIMARY KEY, publisher TEXT NOT NULL, title TEXT NOT NULL,
                    source_tier INTEGER NOT NULL, source_class TEXT NOT NULL,
                    frequency TEXT NOT NULL, geography_basis TEXT NOT NULL, landing_url TEXT NOT NULL
                );
                CREATE TABLE source_snapshot (
                    snapshot_id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source_registry(source_id),
                    source_url TEXT NOT NULL, source_release TEXT NOT NULL UNIQUE, retrieved_at TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL, upstream_sha256 TEXT NOT NULL,
                    byte_count INTEGER NOT NULL, upstream_byte_count INTEGER NOT NULL,
                    snapshot_kind TEXT NOT NULL, raw_path TEXT NOT NULL UNIQUE, metadata_path TEXT NOT NULL UNIQUE,
                    row_count INTEGER NOT NULL
                );
                CREATE TABLE production_observation (
                    observation_id INTEGER PRIMARY KEY, domain TEXT NOT NULL, metric_id TEXT NOT NULL,
                    metric_name TEXT NOT NULL, county_fips TEXT NOT NULL, county_name TEXT NOT NULL,
                    period TEXT NOT NULL, period_end TEXT NOT NULL, frequency TEXT NOT NULL,
                    value REAL, unit TEXT NOT NULL, calculation TEXT NOT NULL, notes TEXT NOT NULL,
                    comparability_status TEXT NOT NULL, UNIQUE(metric_id, county_fips, period)
                );
                CREATE TABLE observation_source (
                    observation_id INTEGER NOT NULL REFERENCES production_observation(observation_id),
                    source_id TEXT NOT NULL REFERENCES source_registry(source_id),
                    source_release TEXT NOT NULL REFERENCES source_snapshot(source_release),
                    raw_sha256 TEXT NOT NULL, source_order INTEGER NOT NULL,
                    PRIMARY KEY(observation_id, source_release)
                );
                CREATE TABLE compliance_record (
                    jurisdiction TEXT NOT NULL, county_fips TEXT NOT NULL, compliance_status TEXT NOT NULL,
                    review_status TEXT NOT NULL, record_type TEXT NOT NULL,
                    planning_period TEXT NOT NULL, cycle TEXT NOT NULL,
                    date_received TEXT NOT NULL, reviewed_date TEXT NOT NULL,
                    is_in_compliance INTEGER NOT NULL CHECK(is_in_compliance IN (0,1))
                );
                CREATE TABLE quality_check (
                    check_name TEXT PRIMARY KEY, passed INTEGER NOT NULL CHECK(passed IN (0,1)), detail TEXT NOT NULL
                );
                CREATE INDEX idx_production_metric_county_period
                    ON production_observation(metric_id, county_fips, period_end);
                """
            )
            connection.executemany(
                "INSERT INTO build_metadata(metadata_key, metadata_value) VALUES (?, ?)",
                [
                    ("project", "The Bay Outlook"),
                    ("phase", "14"),
                    ("version", PHASE14_PRODUCTION_VERSION),
                    ("product", "Housing Production & Policy"),
                    ("built_at", payload["builtAt"]),
                    ("publication_authority", "named human only"),
                    ("policy_score", "not calculated"),
                    ("county_ranking", "not calculated"),
                ],
            )
            connection.executemany(
                """INSERT INTO source_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    for source_id, row in PRODUCTION_SOURCE_REGISTRY.items()
                ],
            )
            connection.executemany(
                """INSERT INTO source_snapshot(
                    source_id, source_url, source_release, retrieved_at, snapshot_sha256,
                    upstream_sha256, byte_count, upstream_byte_count, snapshot_kind,
                    raw_path, metadata_path, row_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [tuple(row[field] for field in SNAPSHOT_FIELDS) for row in snapshots],
            )
            connection.executemany(
                """INSERT INTO production_observation(
                    domain, metric_id, metric_name, county_fips, county_name, period,
                    period_end, frequency, value, unit, calculation, notes, comparability_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row["domain"], row["metric_id"], row["metric_name"], row["county_fips"],
                        row["county_name"], row["period"], row["period_end"], row["frequency"],
                        row["value"], row["unit"], row["calculation"], row["notes"],
                        row["comparability_status"],
                    )
                    for row in observations
                ],
            )
            release_map = {row["source_release"]: row for row in snapshots}
            for observation in observations:
                observation_id = connection.execute(
                    "SELECT observation_id FROM production_observation WHERE metric_id=? AND county_fips=? AND period=?",
                    (observation["metric_id"], observation["county_fips"], observation["period"]),
                ).fetchone()[0]
                for order, release in enumerate(observation["source_releases"].split(";"), start=1):
                    snapshot = release_map[release]
                    connection.execute(
                        "INSERT INTO observation_source VALUES (?, ?, ?, ?, ?)",
                        (observation_id, snapshot["source_id"], release, snapshot["snapshot_sha256"], order),
                    )
            connection.executemany(
                "INSERT INTO compliance_record VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        row["jurisdiction"], row["county_fips"], row["compliance_status"],
                        row["review_status"], row["record_type"], row["planning_period"],
                        row["cycle"], row["date_received"], row["reviewed_date"],
                        int(row["is_in_compliance"]),
                    )
                    for row in payload["complianceRecords"]
                ],
            )
            connection.executemany(
                "INSERT INTO quality_check VALUES (?, ?, ?)",
                [(row["check_name"], row["passed"], json.dumps(row["detail"], sort_keys=True)) for row in checks],
            )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"database integrity failure: {integrity}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "metric_id": row["metricId"],
            "domain": row["domain"],
            "metric_name": row["metricName"],
            "unit": row["unit"],
            "frequency": row["frequency"],
            "source_ids": ";".join(row["sourceIds"]),
            "calculation": row["calculation"],
            "comparability_status": row["comparabilityStatus"],
            "notes": row["notes"],
        }
        for row in payload["metricCatalog"]
    ]


def _source_registry_rows() -> list[dict[str, Any]]:
    return [{"source_id": source_id, **row} for source_id, row in PRODUCTION_SOURCE_REGISTRY.items()]


def _latest_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in observations:
        key = (row["metric_id"], row["county_fips"])
        if key not in latest or row["period_end"] > latest[key]["period_end"]:
            latest[key] = row
    return sorted(latest.values(), key=lambda row: (row["metric_id"], row["county_fips"]))


def build_phase14_production(
    *,
    built_at: str | None = None,
    refresh: bool = True,
    source_cache: Path | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or PROJECT_ROOT).resolve()
    output = root / "data" / "phase14" / "production"
    payload_path = output / "public" / "housing-production-policy-data.json"
    if not payload_path.is_file():
        raise FileNotFoundError(payload_path)
    payload = _read_json(payload_path)
    built_at = built_at or payload["builtAt"] or _utc_now()
    if built_at != payload["builtAt"]:
        raise ValueError("built_at must match the verified Version 1.3 public payload")
    snapshots = _build_snapshots(root, built_at, refresh=refresh, source_cache=source_cache)
    observations = _observation_rows(payload, snapshots)
    checks = _quality_checks(payload, snapshots)
    exports = output / "exports"
    _write_csv(exports / "production_time_series.csv", observations, OBSERVATION_FIELDS)
    _write_csv(exports / "production_snapshot.csv", _latest_observations(observations), OBSERVATION_FIELDS)
    _write_csv(exports / "timeline_coverage.csv", _snake_rows(payload["timelineCoverage"]))
    _write_csv(exports / "rhna_delivery.csv", _snake_rows(payload["rhnaDelivery"]))
    _write_csv(exports / "zoning_composition.csv", _snake_rows(payload["zoningComposition"]))
    _write_csv(exports / "rezone_breakdown.csv", _snake_rows(payload["rezoneBreakdown"]))
    _write_csv(exports / "compliance_records.csv", _snake_rows(payload["complianceRecords"]))
    _write_csv(exports / "source_freshness.csv", _snake_rows(payload["sources"]))
    _write_csv(exports / "source_snapshots.csv", snapshots, SNAPSHOT_FIELDS)
    _write_csv(
        exports / "quality_checks.csv",
        [
            {"check_name": row["check_name"], "passed": row["passed"], "detail": json.dumps(row["detail"], sort_keys=True)}
            for row in checks
        ],
        ("check_name", "passed", "detail"),
    )
    _write_csv(
        root / "metadata" / "housing_production_indicator_catalog.csv",
        _catalog_rows(payload),
    )
    _write_csv(
        root / "metadata" / "housing_production_source_registry.csv",
        _source_registry_rows(),
    )
    _build_database(output / "housing_production.sqlite", payload, observations, snapshots, checks)
    return build_phase14_production_manifest(root=root)


def build_phase14_production_manifest(*, root: Path | None = None) -> dict[str, Any]:
    root = (root or PROJECT_ROOT).resolve()
    output = root / "data" / "phase14" / "production"
    payload = _read_json(output / "public" / "housing-production-policy-data.json")
    publication_path = root / "docs" / "phase14" / "v1.3" / "PUBLICATION_STATUS.json"
    publication = _read_json(publication_path) if publication_path.is_file() else {}
    phase10_path = root / "data" / "phase10" / "phase10_manifest.json"
    phase10 = _read_json(phase10_path) if phase10_path.is_file() else {
        "report": {
            "status": "human_approval_hold",
            "human_signoff": 0,
            "approved_by": None,
            "published_at": None,
        }
    }
    missing = [relative for relative in REQUIRED_PRODUCTION_FILES if not (root / relative).is_file()]
    files = {
        relative: _sha256(root / relative)
        for relative in REQUIRED_PRODUCTION_FILES
        if (root / relative).is_file()
    }
    manifest = {
        "project": "The Bay Outlook",
        "phase": 14,
        "product": "Housing Production & Policy",
        "product_version": PHASE14_PRODUCTION_VERSION,
        "built_at": payload["builtAt"],
        "completion_state": "complete" if not missing else "incomplete",
        "scope": sorted(REQUIRED_PRODUCTION_DOMAINS),
        "counts": {
            "counties": payload["countyCount"],
            "domains": payload["domainCount"],
            "metrics": payload["metricCount"],
            "observations": payload["observationCount"],
            "sources": len(payload["sources"]),
            "source_snapshots": 21,
            "quality_checks": 10,
            "deterministic_timeline_matches": sum(
                row["matched_projects"] for row in payload["timelineCoverage"]
            ),
            "housing_element_records": len(payload["complianceRecords"]),
        },
        "design_boundary": payload["interpretationBoundaries"],
        "phase10_hold": phase10.get("report"),
        "publication": publication,
        "missing_required_files": missing,
        "files": files,
    }
    manifest_path = output / "phase14_v1_3_manifest.json"
    _write_json(manifest_path, manifest)
    verification = verify_phase14_production(manifest_path=manifest_path, root=root)
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


def verify_phase14_production(
    manifest_path: Path | None = None,
    *,
    root: Path | None = None,
    publication_required: bool | None = None,
) -> dict[str, Any]:
    root = (root or PROJECT_ROOT).resolve()
    output = root / "data" / "phase14" / "production"
    manifest_path = manifest_path or (output / "phase14_v1_3_manifest.json")
    manifest = _read_json(manifest_path)
    payload_path = output / "public" / "housing-production-policy-data.json"
    payload = _read_json(payload_path)
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "manifest_identity",
            manifest.get("project") == "The Bay Outlook"
            and manifest.get("phase") == 14
            and manifest.get("product") == "Housing Production & Policy"
            and manifest.get("product_version") == PHASE14_PRODUCTION_VERSION,
            {key: manifest.get(key) for key in ("project", "phase", "product", "product_version")},
        )
    )
    inherited_manifest = root / "data" / "phase14" / "access" / "phase14_v1_2_manifest.json"
    if inherited_manifest.is_file():
        inherited = verify_phase14_access(root=root)
        inherited_passed = inherited["complete"] and inherited["passing"] == inherited["total"]
        inherited_detail = {
            "mode": "complete_checkpoint",
            "passing": inherited["passing"],
            "total": inherited["total"],
            "failed": inherited["failed"],
        }
    else:
        public_baseline = (
            "config/phase14_housing_access.json",
            "metadata/housing_access_indicator_catalog.csv",
            "src/bay_outlook/phase14_access.py",
        )
        missing_baseline = [relative for relative in public_baseline if not (root / relative).is_file()]
        inherited_passed = not missing_baseline
        inherited_detail = {"mode": "publication_safe_baseline", "missing": missing_baseline}
    checks.append(
        _check(
            "verified_version_1_2_baseline",
            inherited_passed,
            inherited_detail,
        )
    )
    missing = [relative for relative in REQUIRED_PRODUCTION_FILES if not (root / relative).is_file()]
    checks.append(_check("required_version_1_3_files", not missing, missing))
    checks.append(
        _check(
            "seven_production_policy_domains",
            set(payload.get("domains", [])) == REQUIRED_PRODUCTION_DOMAINS,
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
    annual_history = defaultdict(set)
    for row in payload["observations"]:
        if len(row["period"]) == 4 and row["period"].isdigit():
            annual_history[(row["metricId"], row["countyFips"])].add(row["period"])
    history_errors = {
        f"{metric}:{fips}": len(periods)
        for (metric, fips), periods in annual_history.items()
        if len(periods) != 8
    }
    checks.append(_check("eight_year_annual_histories", not history_errors, history_errors))
    with (output / "exports" / "quality_checks.csv").open(newline="", encoding="utf-8") as stream:
        quality = list(csv.DictReader(stream))
    failed_quality = [row["check_name"] for row in quality if row["passed"] != "1"]
    checks.append(_check("all_build_quality_checks", len(quality) == 10 and not failed_quality, failed_quality))
    source_ids = {source for row in payload["observations"] for source in row["sourceIds"]}
    checks.append(
        _check(
            "documented_source_lineage",
            source_ids == set(PRODUCTION_SOURCE_REGISTRY) and len(payload["sources"]) == 6,
            sorted(source_ids),
        )
    )
    raw_errors = []
    with (output / "exports" / "source_snapshots.csv").open(newline="", encoding="utf-8") as stream:
        snapshots = list(csv.DictReader(stream))
    for row in snapshots:
        raw_path = root / row["raw_path"]
        metadata_path = root / row["metadata_path"]
        if raw_path.is_file() and metadata_path.is_file():
            if _sha256(raw_path) != row["snapshot_sha256"]:
                raw_errors.append({"file": row["raw_path"], "reason": "hash_mismatch"})
            elif not _read_json(metadata_path).get("upstream_sha256"):
                raw_errors.append({"file": row["metadata_path"], "reason": "missing_upstream_digest"})
        elif not (
            len(row.get("snapshot_sha256", "")) == 64
            and len(row.get("upstream_sha256", "")) == 64
            and row.get("source_id") in PRODUCTION_SOURCE_REGISTRY
            and row.get("snapshot_kind") in {"source_slice", "official_query_aggregate", "complete_official_response"}
        ):
            raw_errors.append({"file": row["raw_path"], "reason": "invalid_public_snapshot_register"})
    checks.append(_check("immutable_raw_evidence", len(snapshots) == 21 and not raw_errors, raw_errors))
    database = output / "housing_production.sqlite"
    database_detail: dict[str, Any] = {"exists": database.is_file()}
    database_passed = False
    if database.is_file():
        with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            observations = connection.execute("SELECT COUNT(*) FROM production_observation").fetchone()[0]
            links = connection.execute("SELECT COUNT(*) FROM observation_source").fetchone()[0]
            failed = connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed=0").fetchone()[0]
            compliance = connection.execute("SELECT COUNT(*) FROM compliance_record").fetchone()[0]
        database_detail.update(
            {"integrity": integrity, "foreign_keys": len(foreign_keys), "observations": observations,
             "source_links": links, "failed_quality": failed, "compliance_records": compliance}
        )
        database_passed = (
            integrity == "ok" and not foreign_keys and failed == 0
            and observations == 1575 and links >= observations and compliance == 109
        )
    else:
        with (output / "exports" / "production_time_series.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            exported_observations = sum(1 for _ in csv.DictReader(stream))
        database_detail.update(
            {"publication_safe_export_rows": exported_observations, "expected": payload["observationCount"]}
        )
        database_passed = exported_observations == payload["observationCount"] == 1575
    checks.append(_check("database_or_public_export_integrity", database_passed, database_detail))
    boundaries = payload["interpretationBoundaries"]
    checks.append(
        _check(
            "annual_stages_not_cohort_funnel",
            boundaries.get("stageFunnelProduced") is False
            and boundaries.get("cohortCompletionRateProduced") is False,
            boundaries,
        )
    )
    timeline = payload["timelineCoverage"]
    checks.append(
        _check(
            "deterministic_timeline_matching_with_coverage",
            sum(row["matched_projects"] for row in timeline) == 28814
            and all(row["matched_projects"] == row["exact_tracking_id_matches"] + row["exact_apn_matches"] for row in timeline)
            and "no fuzzy" in boundaries.get("timelineMatching", "").lower(),
            {"matches": sum(row["matched_projects"] for row in timeline), "rule": boundaries.get("timelineMatching")},
        )
    )
    zoning_share = defaultdict(float)
    for row in payload["zoningComposition"]:
        zoning_share[row["county_fips"]] += float(row["share_of_mapped_area_pct"])
    checks.append(
        _check(
            "zoning_composition_not_capacity_score",
            len(zoning_share) == 9
            and all(abs(value - 100.0) <= 1e-6 for value in zoning_share.values())
            and "not legal development capacity" in boundaries.get("zoningRule", ""),
            {"shares": zoning_share, "rule": boundaries.get("zoningRule")},
        )
    )
    compliance_summaries = payload["complianceSummaries"]
    checks.append(
        _check(
            "housing_element_status_disclosure",
            len(payload["complianceRecords"]) == 109
            and len(compliance_summaries) == 9
            and next(row for row in compliance_summaries if row["county_fips"] == "06081")["out_of_compliance"] == 2,
            {"records": len(payload["complianceRecords"]), "summaries": compliance_summaries},
        )
    )
    rhna_errors = []
    for row in payload["rhnaDelivery"]:
        if abs(row["permit_progress_pct"] - row["permitted_2023_2025"] / row["rhna_allocation"] * 100) > 1e-9:
            rhna_errors.append(row["county_fips"])
    checks.append(_check("rhna_permit_and_completion_separation", not rhna_errors and len(payload["rhnaDelivery"]) == 9, rhna_errors))
    site_data = root / "site" / "app" / "housing" / "production" / "housing-production-policy-data.json"
    if site_data.is_file():
        payload_alignment_passed = _sha256(site_data) == _sha256(payload_path)
        payload_alignment_detail: Any = _sha256(site_data)
    else:
        public_payload = root / "data" / "phase14" / "production" / "public" / "housing-production-policy-data.json"
        payload_alignment_passed = public_payload.is_file() and payload["observationCount"] == 1575
        payload_alignment_detail = {
            "mode": "publication_safe_payload",
            "sha256": _sha256(public_payload) if public_payload.is_file() else None,
        }
    checks.append(
        _check(
            "published_payload_alignment",
            payload_alignment_passed,
            payload_alignment_detail,
        )
    )
    workflow = root / ".github" / "workflows" / "housing-production-update.yml"
    workflow_text = workflow.read_text(encoding="utf-8") if workflow.is_file() else ""
    workflow_tokens = (
        "workflow_dispatch", "schedule:", "verify-phase14-production", "human-review-required",
        "contents: read", "No public deployment",
    )
    missing_tokens = [token for token in workflow_tokens if token not in workflow_text]
    checks.append(_check("responsible_update_workflow", not missing_tokens, missing_tokens))
    report = manifest.get("phase10_hold", {})
    hold_passed = (
        report.get("status") == "human_approval_hold" and report.get("human_signoff") == 0
        and report.get("approved_by") is None and report.get("published_at") is None
    )
    checks.append(_check("phase10_human_approval_hold", hold_passed, report))
    publication_path = root / "docs" / "phase14" / "v1.3" / "PUBLICATION_STATUS.json"
    publication_required = publication_path.is_file() if publication_required is None else publication_required
    publication = _read_json(publication_path) if publication_path.is_file() else {}
    if publication_required:
        site_passed = (
            publication.get("site_deployment_verified") is True
            and publication.get("site_access_mode") == "public"
            and str(publication.get("housing_production_url", "")).startswith("https://")
            and publication.get("site_version_number") == 6
        )
        checks.append(_check("public_housing_production_experience", site_passed, publication))
        github_passed = (
            publication.get("github_update_verified") is True
            and str(publication.get("github_repository_url", "")).startswith("https://github.com/")
            and len(str(publication.get("github_commit_sha", ""))) == 40
        )
        checks.append(_check("github_version_1_3_publication", github_passed, publication))
    hash_errors = []
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
        "product": "Housing Production & Policy",
        "product_version": PHASE14_PRODUCTION_VERSION,
        "complete": not failed,
        "passing": len(checks) - len(failed),
        "total": len(checks),
        "failed": failed,
        "checks": checks,
    }
