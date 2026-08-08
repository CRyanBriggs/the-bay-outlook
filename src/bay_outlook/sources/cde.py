from __future__ import annotations

import csv
import html
import io
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from ..constants import FIPS_BY_COUNTY
from ..models import Observation
from ..storage import download_bytes
from .common import first_present, parse_number


SOURCE_ID = "CDE_CGR12"
LANDING_URL = "https://www.cde.ca.gov/ds/ad/filescgr12.asp"


ALIASES = {
    "academic_year": ("Academic Year",),
    "aggregate_level": ("Aggregate Level",),
    "county_name": ("County Name",),
    "charter": ("Charter School (Y/N)", "Charter School"),
    "alternative": (
        "Alternative School Accountability Status (ASAS) (All/Y/N)",
        "Alternative School Accountability Status (ASAS)",
    ),
    "reporting_category": ("Reporting Category",),
    "completer_type": ("Completer Type",),
    "completers": ("High School Completers",),
    "enrolled": ("Enrolled In College (12 Months)", "Enrolled In College 12 Months"),
    "rate": ("College Going rate (12 Months)", "College Going Rate 12 Months"),
    "uc": ("Enrolled UC (12 Months)",),
    "csu": ("Enrolled CSU (12 Months)",),
    "ccc": ("Enrolled CCC (12 Months)",),
}


@dataclass(frozen=True, slots=True)
class CDERelease:
    academic_year: str
    release_date: str
    source_vintage: str
    data_url: str
    page_content: bytes
    page_headers: dict[str, str]
    page_url: str


def parse_release_page(
    content: bytes,
    *,
    page_url: str = LANDING_URL,
    page_headers: dict[str, str] | None = None,
) -> CDERelease:
    text = content.decode("utf-8", errors="replace")
    candidates: list[tuple[int, str, str, str, str]] = []
    for table_row in re.findall(r"<tr\b[^>]*>.*?</tr>", text, flags=re.IGNORECASE | re.DOTALL):
        link = re.search(
            r"href=[\"']([^\"']*cgr12mo(\d{2})\.txt[^\"']*)[\"']",
            table_row,
            flags=re.IGNORECASE,
        )
        if not link:
            continue
        row_text = html.unescape(re.sub(r"<[^>]+>", " ", table_row))
        academic = re.search(r"(20\d{2})\s*[–—-]\s*(\d{2})", row_text)
        suffix = int(link.group(2))
        end_year = 2000 + suffix
        academic_year = f"{end_year - 1}-{suffix:02d}"
        if academic:
            end_year = int(academic.group(1)[:2] + academic.group(2))
            academic_year = f"{academic.group(1)}-{academic.group(2)}"
        release = re.search(
            r"\b(Posted|Revised)\s+(\d{1,2}-[A-Za-z]{3}-\d{4})\b",
            row_text,
            flags=re.IGNORECASE,
        )
        release_kind = release.group(1).casefold() if release else "published"
        release_date = ""
        if release:
            release_date = datetime.strptime(release.group(2), "%d-%b-%Y").date().isoformat()
        data_url = urllib.parse.urljoin(page_url, html.unescape(link.group(1)))
        candidates.append((end_year, academic_year, release_date, release_kind, data_url))

    if not candidates:
        for href, suffix_text in re.findall(
            r"href=[\"']([^\"']*cgr12mo(\d{2})\.txt[^\"']*)[\"']",
            text,
            flags=re.IGNORECASE,
        ):
            end_year = 2000 + int(suffix_text)
            candidates.append(
                (
                    end_year,
                    f"{end_year - 1}-{int(suffix_text):02d}",
                    "",
                    "published",
                    urllib.parse.urljoin(page_url, html.unescape(href)),
                )
            )
    if not candidates:
        raise ValueError("No CDE college-going-rate data links were found on the release page")

    _, academic_year, release_date, release_kind, data_url = max(candidates, key=lambda row: row[0])
    source_vintage = f"{academic_year}-{release_kind}"
    if release_date:
        source_vintage += f"-{release_date}"
    return CDERelease(
        academic_year=academic_year,
        release_date=release_date,
        source_vintage=source_vintage,
        data_url=data_url,
        page_content=content,
        page_headers=dict(page_headers or {}),
        page_url=page_url,
    )


def discover_latest() -> CDERelease:
    content, headers, final_url = download_bytes(LANDING_URL, timeout=120)
    return parse_release_page(content, page_url=final_url, page_headers=headers)


def fetch_latest() -> tuple[CDERelease, bytes, dict[str, str], str]:
    release = discover_latest()
    content, headers, final_url = download_bytes(release.data_url, timeout=240)
    return release, content, headers, final_url


def fetch() -> tuple[bytes, dict[str, str], str]:
    _, content, headers, final_url = fetch_latest()
    return content, headers, final_url


def _value(row: dict[str, str], key: str):
    return first_present(row, ALIASES[key])


def normalize(
    content: bytes,
    *,
    raw_sha256: str,
    retrieved_at: str,
    source_release: str,
    dataset_status: str = "official",
) -> list[Observation]:
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text), delimiter="\t"))
    if not rows:
        raise ValueError("CDE file contained no rows")
    totals: dict[tuple[str, str], dict[str, str]] = {}
    ag_ready: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        aggregate = str(_value(row, "aggregate_level") or "").strip().upper()
        county = str(_value(row, "county_name") or "").strip()
        fips = FIPS_BY_COUNTY.get(county.casefold())
        if aggregate != "C" or not fips:
            continue
        charter = str(_value(row, "charter") or "").strip().casefold()
        alternative = str(_value(row, "alternative") or "").strip().casefold()
        reporting = str(_value(row, "reporting_category") or "").strip().upper()
        completer_type = str(_value(row, "completer_type") or "").strip().upper()
        if charter != "all" or alternative != "all" or reporting != "TA":
            continue
        year = str(_value(row, "academic_year") or "").strip()
        key = (fips, year)
        if completer_type == "TA":
            totals[key] = row
        elif completer_type == "AGY":
            ag_ready[key] = row

    observations: list[Observation] = []
    for (fips, academic_year), row in sorted(totals.items()):
        county_name = next(name.title() for name, code in FIPS_BY_COUNTY.items() if code == fips)
        common = dict(
            indicator_id="E2",
            source_id=SOURCE_ID,
            geography_type="county",
            geography_code=fips,
            geography_name=county_name,
            period=academic_year,
            frequency="annual",
            subgroup="all",
            industry_code="all",
            adjustment="none",
            dataset_status=dataset_status,
            source_release=source_release,
            retrieved_at=retrieved_at,
            raw_sha256=raw_sha256,
        )
        metric_fields = {
            "high_school_completers": ("completers", "students"),
            "enrolled_in_college_12mo": ("enrolled", "students"),
            "college_going_rate_12mo": ("rate", "percent"),
            "enrolled_uc_12mo": ("uc", "students"),
            "enrolled_csu_12mo": ("csu", "students"),
            "enrolled_ccc_12mo": ("ccc", "students"),
        }
        for metric_id, (field, unit) in metric_fields.items():
            raw_value = _value(row, field)
            if raw_value is None:
                continue
            value, status = parse_number(raw_value)
            observations.append(
                Observation(
                    **common,
                    metric_id=metric_id,
                    value=value,
                    unit=unit,
                    value_status=status,
                    notes="CDE county total; National Student Clearinghouse matches can be affected by blocked records.",
                )
            )
        ag_row = ag_ready.get((fips, academic_year))
        if ag_row:
            total, total_status = parse_number(_value(row, "completers"))
            ready, ready_status = parse_number(_value(ag_row, "completers"))
            value = ready / total * 100.0 if ready is not None and total and total > 0 else None
            status = "final" if value is not None else ("suppressed" if "suppressed" in {total_status, ready_status} else "not_computed")
            observations.append(
                Observation(
                    **common,
                    metric_id="ag_ready_share",
                    value=value,
                    unit="percent",
                    value_status=status,
                    notes="A-G completers divided by total high-school completers for the same county and academic year.",
                )
            )
    return observations
