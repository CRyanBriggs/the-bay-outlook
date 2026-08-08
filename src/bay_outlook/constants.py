from __future__ import annotations

import csv
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_counties(path: Path | None = None) -> tuple[dict[str, str], ...]:
    county_path = path or PROJECT_ROOT / "config" / "counties.csv"
    with county_path.open(newline="", encoding="utf-8") as handle:
        return tuple(dict(row) for row in csv.DictReader(handle))


COUNTIES = load_counties()
COUNTY_BY_FIPS = {row["county_fips"]: row["county_name"] for row in COUNTIES}
FIPS_BY_COUNTY = {row["county_name"].casefold(): row["county_fips"] for row in COUNTIES}
CDE_CODE_BY_FIPS = {row["county_fips"]: row["county_code_cde"] for row in COUNTIES}
BAY_AREA_FIPS = frozenset(COUNTY_BY_FIPS)
