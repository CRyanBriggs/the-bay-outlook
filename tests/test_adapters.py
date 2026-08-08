from __future__ import annotations

import io
import unittest
import zipfile

from bay_outlook.constants import PROJECT_ROOT
from bay_outlook.sources import acs, bea, cde, laus, qcew
from bay_outlook.storage import sha256_bytes


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
RETRIEVED = "2026-08-07T00:00:00Z"


class AdapterTests(unittest.TestCase):
    def test_laus_normalizer(self):
        content = (FIXTURES / "laus.json").read_bytes()
        rows = laus.normalize(content, raw_sha256=sha256_bytes(content), retrieved_at=RETRIEVED, source_release="fixture", dataset_status="fixture")
        self.assertEqual(len(rows), 8)
        alameda_rate = next(row for row in rows if row.geography_code == "06001" and row.metric_id == "unemployment_rate")
        self.assertEqual(alameda_rate.value, 4.2)
        self.assertEqual(alameda_rate.value_status, "preliminary")

    def test_qcew_normalizer(self):
        content = (FIXTURES / "qcew_06001.csv").read_bytes()
        rows = qcew.normalize(content, county_fips="06001", raw_sha256=sha256_bytes(content), retrieved_at=RETRIEVED, source_release="fixture", dataset_status="fixture")
        employment = next(row for row in rows if row.metric_id == "average_monthly_covered_employment")
        self.assertEqual(employment.value, 783000)
        self.assertEqual(employment.industry_code, "10")

    def test_bea_normalizer(self):
        csv_bytes = (FIXTURES / "bea_cagdp1_ca.csv").read_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("CAGDP1_CA_2001_2024.csv", csv_bytes)
        content = buffer.getvalue()
        rows = bea.normalize(content, raw_sha256=sha256_bytes(content), retrieved_at=RETRIEVED, source_release="fixture", dataset_status="fixture")
        self.assertEqual(len(rows), 4)
        latest = next(row for row in rows if row.geography_code == "06001" and row.period == "2024")
        self.assertEqual(latest.value, 153000000)

    def test_acs_normalizer(self):
        content = (FIXTURES / "acs_housing.json").read_bytes()
        rows = acs.normalize(content, year=2024, raw_sha256=sha256_bytes(content), retrieved_at=RETRIEVED, source_release="fixture", dataset_status="fixture")
        renter = next(row for row in rows if row.geography_code == "06001" and row.metric_id == "renter_cost_burden_30_plus_pct")
        self.assertAlmostEqual(renter.value, 600 / 950 * 100, places=6)
        component = next(row for row in rows if row.geography_code == "06001" and row.metric_id == "acs_b25070_001e")
        self.assertEqual(component.margin_of_error, 20)

    def test_cde_normalizer(self):
        content = (FIXTURES / "cde_cgr12.tsv").read_bytes()
        rows = cde.normalize(content, raw_sha256=sha256_bytes(content), retrieved_at=RETRIEVED, source_release="fixture", dataset_status="fixture")
        rate = next(row for row in rows if row.geography_code == "06001" and row.metric_id == "college_going_rate_12mo")
        ready = next(row for row in rows if row.geography_code == "06001" and row.metric_id == "ag_ready_share")
        self.assertEqual(rate.value, 72.5)
        self.assertEqual(ready.value, 50.0)

    def test_cde_release_page_discovers_latest_file(self):
        page = b"""
        <table>
          <tr><td>2021&ndash;22</td><td><a href='https://www3.cde.ca.gov/cgr12mo22.txt'>file</a>
          (Posted 17-Jun-2024)</td></tr>
          <tr><td>2022&ndash;23</td><td><a href='https://www3.cde.ca.gov/cgr12mo23.txt'>file</a>
          (Posted 17-Sep-2025)</td></tr>
        </table>
        """
        release = cde.parse_release_page(page)
        self.assertEqual(release.academic_year, "2022-23")
        self.assertEqual(release.release_date, "2025-09-17")
        self.assertEqual(release.source_vintage, "2022-23-posted-2025-09-17")
        self.assertTrue(release.data_url.endswith("cgr12mo23.txt"))


if __name__ == "__main__":
    unittest.main()
