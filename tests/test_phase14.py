from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from bay_outlook.constants import PROJECT_ROOT
from bay_outlook.phase14 import (
    PHASE14_VERSION,
    REQUIRED_DOMAINS,
    _hcd_sql,
    _number,
    _ratio,
    verify_phase14,
)


class Phase14UnitTests(unittest.TestCase):
    def test_number_parses_source_values_and_rejects_sentinels(self) -> None:
        self.assertEqual(_number("1,234"), 1234.0)
        self.assertEqual(_number("0"), 0.0)
        self.assertIsNone(_number("N/A"))
        self.assertIsNone(_number("-666666666"))

    def test_ratio_guards_denominators(self) -> None:
        self.assertEqual(_ratio(25, 100), 25.0)
        self.assertEqual(_ratio(4, 2, multiplier=1.0), 2.0)
        self.assertIsNone(_ratio(1, 0))
        self.assertIsNone(_ratio(None, 5))

    def test_hcd_queries_are_limited_to_bay_counties_and_apr_years(self) -> None:
        mapping, permits = _hcd_sql()
        self.assertIn('"CNTY_NAME" IN', mapping)
        self.assertIn('"YEAR" >= \'2023\'', permits)
        self.assertIn('"NO_BUILDING_PERMITS"', permits)


class Phase14ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload_path = PROJECT_ROOT / "data" / "phase14" / "public" / "housing-data.json"
        if not cls.payload_path.is_file():
            raise unittest.SkipTest("Phase 14 artifacts have not been built")
        cls.payload = json.loads(cls.payload_path.read_text(encoding="utf-8"))

    def test_public_payload_identity_and_scope(self) -> None:
        self.assertEqual(self.payload["version"], PHASE14_VERSION)
        self.assertEqual(self.payload["countyCount"], 9)
        self.assertEqual(set(self.payload["domains"]), REQUIRED_DOMAINS)
        self.assertFalse(self.payload["publicationBoundary"]["automatedNarrative"])

    def test_public_payload_has_history_for_every_county(self) -> None:
        history: dict[tuple[str, str], set[str]] = {}
        for row in self.payload["observations"]:
            if row["value"] is not None:
                history.setdefault((row["metricId"], row["countyFips"]), set()).add(row["period"])
        for county in self.payload["counties"]:
            fips = county["countyFips"]
            self.assertGreaterEqual(len(history[("median_gross_rent", fips)]), 4)
            self.assertGreaterEqual(len(history[("homes_sold", fips)]), 60)
            self.assertGreaterEqual(len(history[("permitted_units_total", fips)]), 5)

    def test_database_matches_public_observation_count(self) -> None:
        database = PROJECT_ROOT / "data" / "phase14" / "housing_observatory.sqlite"
        with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
            count = connection.execute("SELECT COUNT(*) FROM housing_observation").fetchone()[0]
            failed = connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed = 0").fetchone()[0]
        self.assertEqual(count, self.payload["observationCount"])
        self.assertEqual(failed, 0)

    def test_phase14_verifier_passes_all_publication_gates(self) -> None:
        result = verify_phase14()
        self.assertTrue(result["complete"], result["failed"])
        self.assertEqual(result["passing"], result["total"])


if __name__ == "__main__":
    unittest.main()
