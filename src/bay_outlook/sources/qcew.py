from __future__ import annotations

import csv
import io

from ..constants import COUNTY_BY_FIPS
from ..models import Observation
from ..storage import download_bytes
from .common import parse_number


SOURCE_ID = "BLS_QCEW"
URL_TEMPLATE = "https://data.bls.gov/cew/data/api/{year}/{quarter}/area/{county_fips}.csv"


def source_url(year: int, quarter: int, county_fips: str) -> str:
    return URL_TEMPLATE.format(year=year, quarter=quarter, county_fips=county_fips)


def fetch(year: int, quarter: int, county_fips: str) -> tuple[bytes, dict[str, str], str]:
    return download_bytes(source_url(year, quarter, county_fips))


def normalize(
    content: bytes,
    *,
    county_fips: str,
    raw_sha256: str,
    retrieved_at: str,
    source_release: str,
    dataset_status: str = "official",
) -> list[Observation]:
    if county_fips not in COUNTY_BY_FIPS:
        raise ValueError(f"Unsupported county FIPS: {county_fips}")
    rows = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    target = next(
        (
            row
            for row in rows
            if row.get("area_fips") == county_fips
            and row.get("own_code") == "0"
            and row.get("industry_code") == "10"
            and row.get("agglvl_code") == "70"
        ),
        None,
    )
    if target is None:
        raise ValueError(f"QCEW county-total row was not found for {county_fips}")
    employment_values = [parse_number(target.get(f"month{month}_emplvl"))[0] for month in (1, 2, 3)]
    valid_employment = [value for value in employment_values if value is not None]
    average_employment = sum(valid_employment) / len(valid_employment) if valid_employment else None
    status = "final" if average_employment is not None else "missing"
    period = f"{target['year']}-Q{target['qtr']}"
    common = dict(
        indicator_id="L2",
        source_id=SOURCE_ID,
        geography_type="county",
        geography_code=county_fips,
        geography_name=COUNTY_BY_FIPS[county_fips],
        period=period,
        frequency="quarterly",
        subgroup="all",
        industry_code="10",
        adjustment="not seasonally adjusted",
        dataset_status=dataset_status,
        source_release=source_release,
        retrieved_at=retrieved_at,
        raw_sha256=raw_sha256,
    )
    establishments, establishments_status = parse_number(target.get("qtrly_estabs"))
    weekly_wage, wage_status = parse_number(target.get("avg_wkly_wage"))
    return [
        Observation(
            **common,
            metric_id="average_monthly_covered_employment",
            value=average_employment,
            unit="jobs",
            value_status=status,
            notes="Mean of the three QCEW monthly employment levels; establishment-location concept.",
        ),
        Observation(
            **common,
            metric_id="covered_establishments",
            value=establishments,
            unit="establishments",
            value_status=establishments_status,
            notes="Quarterly establishment count for total covered employment.",
        ),
        Observation(
            **common,
            metric_id="average_weekly_wage",
            value=weekly_wage,
            unit="current dollars per week",
            value_status=wage_status,
            notes="Supporting L2 pilot measure; will also support L3.",
        ),
    ]
