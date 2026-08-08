from __future__ import annotations

import csv
import io
import zipfile

from ..constants import COUNTY_BY_FIPS
from ..models import Observation
from ..storage import download_bytes
from .common import parse_number


SOURCE_ID = "BEA_CAGDP1"
BULK_URL = "https://apps.bea.gov/regional/zip/CAGDP1.zip"


def fetch() -> tuple[bytes, dict[str, str], str]:
    return download_bytes(BULK_URL, timeout=120)


def normalize(
    content: bytes,
    *,
    raw_sha256: str,
    retrieved_at: str,
    source_release: str,
    dataset_status: str = "official",
) -> list[Observation]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        candidates = [name for name in archive.namelist() if name.startswith("CAGDP1_CA_") and name.endswith(".csv")]
        if len(candidates) != 1:
            raise ValueError(f"Expected one California CAGDP1 CSV, found {len(candidates)}")
        text = archive.read(candidates[0]).decode("utf-8-sig")
    observations: list[Observation] = []
    for row in csv.DictReader(io.StringIO(text)):
        fips = str(row.get("GeoFIPS", "")).replace('"', "").strip()
        if fips not in COUNTY_BY_FIPS or str(row.get("LineCode", "")).strip() != "1":
            continue
        for column, raw_value in row.items():
            if not column or not column.isdigit() or len(column) != 4:
                continue
            value, status = parse_number(raw_value)
            observations.append(
                Observation(
                    indicator_id="O1",
                    metric_id="real_gdp",
                    source_id=SOURCE_ID,
                    geography_type="county",
                    geography_code=fips,
                    geography_name=COUNTY_BY_FIPS[fips],
                    period=column,
                    frequency="annual",
                    value=value,
                    unit=str(row.get("Unit", "Thousands of chained dollars")).strip(),
                    adjustment="inflation adjusted",
                    value_status=status,
                    dataset_status=dataset_status,
                    source_release=source_release,
                    retrieved_at=retrieved_at,
                    raw_sha256=raw_sha256,
                    notes="BEA CAGDP1 line code 1; production-location concept.",
                )
            )
    return observations
