from __future__ import annotations

import json
from collections.abc import Iterable

from ..constants import COUNTY_BY_FIPS
from ..models import Observation
from ..storage import post_json
from .common import parse_number


SOURCE_ID = "BLS_LAUS"
API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
MEASURES = {
    "003": ("unemployment_rate", "percent"),
    "004": ("unemployed_people", "persons"),
    "005": ("employed_people", "persons"),
    "006": ("labor_force", "persons"),
}


def series_id(county_fips: str, measure_code: str) -> str:
    # LAUS county series use a seven-zero padding block before the
    # three-character measure code (for example ...0000000003).
    return f"LAUCN{county_fips}0000000{measure_code}"


def series_map() -> dict[str, tuple[str, str, str]]:
    return {
        series_id(fips, code): (fips, metric, unit)
        for fips in COUNTY_BY_FIPS
        for code, (metric, unit) in MEASURES.items()
    }


def fetch_payloads(start_year: int, end_year: int, chunk_size: int = 25) -> list[bytes]:
    mapping = series_map()
    identifiers = sorted(mapping)
    payloads: list[bytes] = []
    for index in range(0, len(identifiers), chunk_size):
        payload = {
            "seriesid": identifiers[index : index + chunk_size],
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        content, _, _ = post_json(API_URL, payload)
        payloads.append(content)
    return payloads


def normalize(
    content: bytes,
    *,
    raw_sha256: str,
    retrieved_at: str,
    source_release: str,
    dataset_status: str = "official",
) -> list[Observation]:
    payload = json.loads(content)
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError(f"BLS request failed: {payload.get('message')}")
    mapping = series_map()
    observations: list[Observation] = []
    series_rows: Iterable[dict[str, object]] = payload.get("Results", {}).get("series", [])
    for series in series_rows:
        identifier = str(series.get("seriesID", ""))
        if identifier not in mapping:
            continue
        fips, metric_id, unit = mapping[identifier]
        for item in series.get("data", []):
            period_code = str(item.get("period", ""))
            if period_code == "M13":
                period = str(item.get("year"))
                frequency = "annual"
            elif period_code.startswith("M") and period_code[1:].isdigit() and 1 <= int(period_code[1:]) <= 12:
                period = f"{item.get('year')}-{period_code[1:]}"
                frequency = "monthly"
            else:
                continue
            value, parsed_status = parse_number(item.get("value"))
            footnotes = item.get("footnotes") or []
            preliminary = any(str(note.get("code", "")).upper() == "P" for note in footnotes if isinstance(note, dict))
            status = "preliminary" if preliminary else parsed_status
            observations.append(
                Observation(
                    indicator_id="L1",
                    metric_id=metric_id,
                    source_id=SOURCE_ID,
                    geography_type="county",
                    geography_code=fips,
                    geography_name=COUNTY_BY_FIPS[fips],
                    period=period,
                    frequency=frequency,
                    value=value,
                    unit=unit,
                    adjustment="not seasonally adjusted",
                    value_status=status,
                    dataset_status=dataset_status,
                    source_release=source_release,
                    retrieved_at=retrieved_at,
                    raw_sha256=raw_sha256,
                    notes="LAUS county series; place-of-residence concept.",
                )
            )
    return observations
