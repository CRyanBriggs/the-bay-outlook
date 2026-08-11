from __future__ import annotations

import csv
import json
import math
import sqlite3
import unittest
from collections import defaultdict

from bay_outlook.constants import COUNTY_BY_FIPS, PROJECT_ROOT
from bay_outlook.phase14_equity import (
    ACS_COST_BURDEN_YEARS,
    ACS_YEARS,
    LODES_YEARS,
    PHASE14_EQUITY_VERSION,
    RACE_GROUPS,
    REQUIRED_EQUITY_DOMAINS,
    verify_phase14_equity,
)


class Phase14EquityArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.output = PROJECT_ROOT / "data" / "phase14" / "equity"
        cls.payload_path = (
            cls.output / "public" / "housing-equity-connections-data.json"
        )
        if not cls.payload_path.is_file():
            raise unittest.SkipTest("Version 1.4 artifacts have not been built")
        cls.payload = json.loads(cls.payload_path.read_text(encoding="utf-8"))
        cls.rows = cls.payload["observations"]

    def test_identity_scope_and_interpretation_boundaries(self) -> None:
        self.assertEqual(self.payload["version"], PHASE14_EQUITY_VERSION)
        self.assertEqual(self.payload["countyCount"], 9)
        self.assertEqual(set(self.payload["domains"]), REQUIRED_EQUITY_DOMAINS)
        boundaries = self.payload["interpretationBoundaries"]
        self.assertFalse(boundaries["raceCategoriesAdditive"])
        self.assertFalse(boundaries["countyRankingProduced"])
        self.assertFalse(boundaries["equityScoreProduced"])
        self.assertFalse(boundaries["causalInferenceProduced"])
        self.assertFalse(boundaries["acsAndLodesUniversesMerged"])
        self.assertFalse(boundaries["automatedNarrativeProduced"])
        self.assertTrue(boundaries["acsMarginsOfErrorRetained"])
        self.assertTrue(boundaries["subgroupDenominatorsPublished"])

    def test_race_tenure_coverage_and_nonadditive_categories(self) -> None:
        rows = [
            row
            for row in self.rows
            if row["domain"] == "race_ethnicity_and_housing"
            and row["metricId"] == "homeownership_pct"
        ]
        expected = {
            (fips, str(year), subgroup_id)
            for fips in COUNTY_BY_FIPS
            for year in ACS_YEARS
            for subgroup_id, _ in RACE_GROUPS.values()
        }
        actual = {
            (row["countyFips"], row["period"], row["subgroupId"])
            for row in rows
        }
        self.assertEqual(actual, expected)
        self.assertIn("white_alone", {row["subgroupId"] for row in rows})
        self.assertIn("white_non_hispanic", {row["subgroupId"] for row in rows})
        self.assertIn("hispanic_or_latino", {row["subgroupId"] for row in rows})

    def test_acs_margins_denominators_and_burden_recompute(self) -> None:
        acs_rows = [
            row
            for row in self.rows
            if "CENSUS_ACS5_DETAIL" in row["sourceIds"]
            and row["value"] is not None
        ]
        self.assertTrue(acs_rows)
        self.assertTrue(
            all(row["marginOfError90"] is not None for row in acs_rows)
        )
        burden = [
            row
            for row in self.rows
            if row["metricId"].startswith("housing_cost_burden")
        ]
        self.assertEqual(
            {row["period"] for row in burden},
            {str(year) for year in ACS_COST_BURDEN_YEARS},
        )
        for row in burden:
            if row["value"] is None:
                continue
            self.assertGreater(row["denominator"], 0)
            expected = row["numerator"] / row["denominator"] * 100
            self.assertAlmostEqual(row["value"], expected, places=6)

    def test_education_and_occupation_fmr_connections_recompute(self) -> None:
        derived = [
            row
            for row in self.rows
            if row["metricId"]
            in {"earnings_coverage_2br_fmr_pct", "monthly_gap_to_2br_fmr"}
            and row["value"] is not None
        ]
        self.assertTrue(derived)
        self.assertEqual(
            {row["domain"] for row in derived},
            {"education_and_earnings", "occupation_and_affordability"},
        )
        for row in derived:
            earnings = row["numerator"]
            fmr = row["benchmarkValue"]
            if row["metricId"] == "earnings_coverage_2br_fmr_pct":
                expected = earnings / (fmr * 40) * 100
            else:
                expected = fmr - earnings * 0.30 / 12
            self.assertTrue(
                math.isclose(row["value"], expected, rel_tol=1e-7, abs_tol=1e-6)
            )

    def test_lodes_primary_job_flows_and_components_reconcile(self) -> None:
        lodes = [
            row
            for row in self.rows
            if row["sourceIds"] == "CENSUS_LEHD_LODES"
        ]
        self.assertEqual(
            {(row["countyFips"], row["period"]) for row in lodes},
            {
                (fips, str(year))
                for fips in COUNTY_BY_FIPS
                for year in LODES_YEARS
            },
        )
        values: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        for row in lodes:
            values[(row["countyFips"], row["period"])][row["metricId"]] = row[
                "value"
            ]
        for metrics in values.values():
            total = metrics["primary_jobs"]
            self.assertEqual(
                total,
                metrics["local_resident_primary_jobs"]
                + metrics["other_california_inbound_primary_jobs"]
                + metrics["outside_california_inbound_primary_jobs"],
            )
            self.assertAlmostEqual(
                metrics["local_resident_job_share_pct"]
                + metrics["inbound_job_share_pct"],
                100.0,
                places=6,
            )
            self.assertAlmostEqual(
                metrics["low_monthly_earnings_job_share_pct"]
                + metrics["middle_monthly_earnings_job_share_pct"]
                + metrics["high_monthly_earnings_job_share_pct"],
                100.0,
                places=6,
            )
            self.assertAlmostEqual(
                metrics["age_29_or_younger_job_share_pct"]
                + metrics["age_30_to_54_job_share_pct"]
                + metrics["age_55_plus_job_share_pct"],
                100.0,
                places=6,
            )

    def test_metric_catalog_names_tenure_dimension(self) -> None:
        catalog_path = (
            PROJECT_ROOT / "metadata" / "housing_equity_indicator_catalog.csv"
        )
        with catalog_path.open(newline="", encoding="utf-8") as stream:
            catalog = list(csv.DictReader(stream))
        vehicle_access = [
            row
            for row in catalog
            if row["domain"] == "transportation_and_housing"
            and row["metric_id"] == "zero_vehicle_households_pct"
        ]
        self.assertEqual(len(vehicle_access), 1)
        self.assertEqual(vehicle_access[0]["subgroup_dimension"], "tenure")

    def test_source_snapshots_and_quality_checks(self) -> None:
        with (self.output / "exports" / "source_snapshots.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            snapshots = list(csv.DictReader(stream))
        with (self.output / "exports" / "quality_checks.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            checks = list(csv.DictReader(stream))
        self.assertEqual(len(snapshots), 76)
        self.assertTrue(all(len(row["snapshot_sha256"]) == 64 for row in snapshots))
        self.assertTrue(all(int(row["byte_count"]) > 0 for row in snapshots))
        self.assertTrue(all(int(row["row_count"]) > 0 for row in snapshots))
        self.assertTrue(checks)
        self.assertTrue(all(row["passed"] == "1" for row in checks))

    def test_database_or_public_export_reconciles(self) -> None:
        database = self.output / "housing_equity.sqlite"
        if database.is_file():
            with sqlite3.connect(
                f"file:{database.resolve()}?mode=ro", uri=True
            ) as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM equity_observation"
                ).fetchone()[0]
                failed = connection.execute(
                    "SELECT COUNT(*) FROM quality_check WHERE passed=0"
                ).fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(integrity, "ok")
        else:
            with (self.output / "exports" / "equity_observations.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                count = sum(1 for _ in csv.DictReader(stream))
            failed = 0
        self.assertEqual(count, self.payload["observationCount"])
        self.assertEqual(failed, 0)

    def test_version_1_4_public_verifier_passes(self) -> None:
        result = verify_phase14_equity()
        self.assertTrue(result["complete"], result["failed"])
        self.assertEqual(result["passing"], result["total"])


if __name__ == "__main__":
    unittest.main()
