from __future__ import annotations

import json
import os
import urllib.parse

from ..constants import COUNTY_BY_FIPS
from ..models import Observation
from ..storage import download_bytes
from .common import parse_number


SOURCE_ID = "CENSUS_ACS5"
RENTER_ESTIMATES = [f"B25070_{index:03d}E" for index in range(1, 12)]
RENTER_MOES = [value[:-1] + "M" for value in RENTER_ESTIMATES]
OWNER_ESTIMATES = [f"B25091_{index:03d}E" for index in range(1, 24)]
OWNER_MOES = [value[:-1] + "M" for value in OWNER_ESTIMATES]
VARIABLES = RENTER_ESTIMATES + RENTER_MOES + OWNER_ESTIMATES + OWNER_MOES


def source_url(year: int, api_key: str) -> str:
    query = urllib.parse.urlencode(
        {
            "get": ",".join(["NAME", *VARIABLES]),
            "for": "county:*",
            "in": "state:06",
            "key": api_key,
        }
    )
    return f"https://api.census.gov/data/{year}/acs/acs5?{query}"


def fetch(year: int, api_key: str | None = None) -> tuple[bytes, dict[str, str], str]:
    key = api_key or os.getenv("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY is required for live ACS data queries.")
    return download_bytes(source_url(year, key), timeout=120)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * 100.0


def _sum(values: list[float | None]) -> float | None:
    return sum(value for value in values if value is not None) if any(value is not None for value in values) else None


def normalize(
    content: bytes,
    *,
    year: int,
    raw_sha256: str,
    retrieved_at: str,
    source_release: str,
    dataset_status: str = "official",
) -> list[Observation]:
    payload = json.loads(content)
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("Unexpected ACS response structure")
    headers = payload[0]
    observations: list[Observation] = []
    for values in payload[1:]:
        row = dict(zip(headers, values, strict=True))
        fips = f"{row.get('state', '')}{row.get('county', '')}"
        if fips not in COUNTY_BY_FIPS:
            continue
        estimates = {variable: parse_number(row.get(variable))[0] for variable in RENTER_ESTIMATES + OWNER_ESTIMATES}
        moes = {variable: parse_number(row.get(variable))[0] for variable in RENTER_MOES + OWNER_MOES}
        common = dict(
            indicator_id="H1",
            source_id=SOURCE_ID,
            geography_type="county",
            geography_code=fips,
            geography_name=COUNTY_BY_FIPS[fips],
            period=str(year),
            frequency="annual",
            subgroup="all",
            industry_code="all",
            adjustment="ACS 5-year estimate",
            dataset_status=dataset_status,
            source_release=source_release,
            retrieved_at=retrieved_at,
            raw_sha256=raw_sha256,
        )
        for variable, value in estimates.items():
            status = "final" if value is not None else "missing"
            observations.append(
                Observation(
                    **common,
                    metric_id=f"acs_{variable.casefold()}",
                    value=value,
                    unit="housing units",
                    margin_of_error=moes.get(variable[:-1] + "M"),
                    value_status=status,
                    notes="Published ACS component count retained for auditability.",
                )
            )

        renter_denominator = (
            estimates["B25070_001E"] - estimates["B25070_011E"]
            if estimates["B25070_001E"] is not None and estimates["B25070_011E"] is not None
            else None
        )
        renter_burden = _sum([estimates[f"B25070_{index:03d}E"] for index in range(7, 11)])
        renter_severe = estimates["B25070_010E"]
        owner_denominator = None
        if all(estimates[key] is not None for key in ("B25091_002E", "B25091_012E", "B25091_013E", "B25091_023E")):
            owner_denominator = (
                estimates["B25091_002E"] - estimates["B25091_012E"]
                + estimates["B25091_013E"] - estimates["B25091_023E"]
            )
        owner_burden = _sum(
            [estimates[f"B25091_{index:03d}E"] for index in (8, 9, 10, 11, 19, 20, 21, 22)]
        )
        owner_severe = _sum([estimates["B25091_011E"], estimates["B25091_022E"]])
        derived = {
            "renter_cost_burden_30_plus_pct": _ratio(renter_burden, renter_denominator),
            "renter_severe_cost_burden_50_plus_pct": _ratio(renter_severe, renter_denominator),
            "owner_cost_burden_30_plus_pct": _ratio(owner_burden, owner_denominator),
            "owner_severe_cost_burden_50_plus_pct": _ratio(owner_severe, owner_denominator),
        }
        for metric_id, value in derived.items():
            observations.append(
                Observation(
                    **common,
                    metric_id=metric_id,
                    value=value,
                    unit="percent",
                    margin_of_error=None,
                    value_status="final" if value is not None else "not_computed",
                    notes="Derived from published component counts; a derived MOE is not fabricated in Phase 4.",
                )
            )
    return observations
