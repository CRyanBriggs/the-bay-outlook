from __future__ import annotations

import csv
import json
import sqlite3
import unittest
from collections import defaultdict

from bay_outlook.constants import PROJECT_ROOT
from bay_outlook.phase14_production import (
    PHASE14_PRODUCTION_VERSION,
    REQUIRED_PRODUCTION_DOMAINS,
    verify_phase14_production,
)


class Phase14ProductionArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.output = PROJECT_ROOT / "data" / "phase14" / "production"
        cls.payload_path = cls.output / "public" / "housing-production-policy-data.json"
        if not cls.payload_path.is_file():
            raise unittest.SkipTest("Version 1.3 artifacts have not been built")
        cls.payload = json.loads(cls.payload_path.read_text(encoding="utf-8"))

    def test_public_payload_identity_scope_and_boundaries(self) -> None:
        self.assertEqual(self.payload["version"], PHASE14_PRODUCTION_VERSION)
        self.assertEqual(self.payload["countyCount"], 9)
        self.assertEqual(set(self.payload["domains"]), REQUIRED_PRODUCTION_DOMAINS)
        boundaries = self.payload["interpretationBoundaries"]
        self.assertFalse(boundaries["stageFunnelProduced"])
        self.assertFalse(boundaries["cohortCompletionRateProduced"])
        self.assertFalse(boundaries["countyRankingProduced"])
        self.assertFalse(boundaries["policyScoreProduced"])
        self.assertFalse(self.payload["publicationBoundary"]["automatedNarrative"])

    def test_annual_histories_and_stage_separation(self) -> None:
        histories: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in self.payload["observations"]:
            if len(row["period"]) == 4 and row["period"].isdigit():
                histories[(row["metricId"], row["countyFips"])].add(row["period"])
        self.assertTrue(histories)
        self.assertTrue(all(periods == {str(year) for year in range(2018, 2026)} for periods in histories.values()))
        metric_ids = {row["metricId"] for row in self.payload["metricCatalog"]}
        self.assertTrue({"entitled_units", "building_permit_units", "completed_units"} <= metric_ids)

    def test_timeline_totals_and_deterministic_matching(self) -> None:
        rows = self.payload["timelineCoverage"]
        self.assertEqual(sum(row["matched_projects"] for row in rows), 28814)
        self.assertTrue(
            all(
                row["matched_projects"]
                == row["exact_tracking_id_matches"] + row["exact_apn_matches"]
                for row in rows
            )
        )
        self.assertIn("no fuzzy", self.payload["interpretationBoundaries"]["timelineMatching"].lower())

    def test_zoning_and_rhna_calculations_recompute(self) -> None:
        zoning: dict[str, float] = defaultdict(float)
        for row in self.payload["zoningComposition"]:
            zoning[row["county_fips"]] += row["share_of_mapped_area_pct"]
        self.assertEqual(len(zoning), 9)
        self.assertTrue(all(abs(value - 100.0) <= 1e-6 for value in zoning.values()))
        for row in self.payload["rhnaDelivery"]:
            expected = row["permitted_2023_2025"] / row["rhna_allocation"] * 100
            self.assertAlmostEqual(row["permit_progress_pct"], expected, places=9)
            expected = row["completed_2023_2025"] / row["rhna_allocation"] * 100
            self.assertAlmostEqual(row["completion_progress_pct"], expected, places=9)

    def test_warehouse_snapshots_and_compliance_reconcile(self) -> None:
        database = self.output / "housing_production.sqlite"
        if database.is_file():
            with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
                count = connection.execute("SELECT COUNT(*) FROM production_observation").fetchone()[0]
                links = connection.execute("SELECT COUNT(*) FROM observation_source").fetchone()[0]
                compliance = connection.execute("SELECT COUNT(*) FROM compliance_record").fetchone()[0]
                failed = connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed=0").fetchone()[0]
        else:
            with (self.output / "exports" / "production_time_series.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                count = sum(1 for _ in csv.DictReader(stream))
            links = count
            compliance = len(self.payload["complianceRecords"])
            with (self.output / "exports" / "quality_checks.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                failed = sum(1 for row in csv.DictReader(stream) if row["passed"] != "1")
        with (self.output / "exports" / "source_snapshots.csv").open(newline="", encoding="utf-8") as stream:
            snapshots = list(csv.DictReader(stream))
        self.assertEqual(count, self.payload["observationCount"])
        self.assertGreaterEqual(links, count)
        self.assertEqual(compliance, len(self.payload["complianceRecords"]))
        self.assertEqual(len(snapshots), 21)
        self.assertEqual(failed, 0)

    def test_version_1_3_public_reproduction_verifier_passes(self) -> None:
        result = verify_phase14_production()
        self.assertTrue(result["complete"], result["failed"])
        self.assertEqual(result["passing"], result["total"])


if __name__ == "__main__":
    unittest.main()
