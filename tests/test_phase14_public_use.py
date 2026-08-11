from __future__ import annotations

import csv
import json
import sqlite3
import unittest
from collections import defaultdict

from bay_outlook.constants import PROJECT_ROOT
from bay_outlook.phase14_public_use import (
    COMPARISON_MEASURES,
    MODULES,
    PHASE14_PUBLIC_USE_VERSION,
    PROFILE_MEASURES,
    verify_phase14_public_use,
)


class Phase14PublicUseArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.output = PROJECT_ROOT / "data" / "phase14" / "public_use"
        cls.index_path = cls.output / "public" / "housing-public-use-index.json"
        cls.full_path = cls.output / "public" / "housing-public-use-data.json"
        if not cls.index_path.is_file():
            raise unittest.SkipTest("Version 1.5 artifacts have not been built")
        cls.index = json.loads(cls.index_path.read_text(encoding="utf-8"))
        cls.full = json.loads(cls.full_path.read_text(encoding="utf-8"))

    def test_cumulative_identity_and_publication_boundaries(self) -> None:
        self.assertEqual(self.index["version"], PHASE14_PUBLIC_USE_VERSION)
        self.assertEqual(self.index["countyCount"], 9)
        self.assertEqual(self.index["moduleCount"], 4)
        self.assertEqual(self.index["metricCount"], 139)
        self.assertEqual(self.index["observationCount"], 10224)
        self.assertEqual(self.index["sourceSnapshotCount"], 181)
        boundary = self.index["publicationBoundary"]
        self.assertFalse(boundary["automaticNarrative"])
        self.assertFalse(boundary["causalInference"])
        self.assertFalse(boundary["countyRankingProduced"])
        self.assertIsNone(boundary["compositeScore"])
        self.assertEqual(boundary["phase10ReportStatus"], "human_approval_hold")

    def test_inherited_module_counts_are_exact(self) -> None:
        modules = {row["moduleId"]: row for row in self.index["modules"]}
        self.assertEqual(set(modules), {module["module_id"] for module in MODULES})
        for expected in MODULES:
            row = modules[expected["module_id"]]
            self.assertEqual(row["version"], expected["version"])
            self.assertEqual(row["observationCount"], expected["expected_observations"])
            self.assertEqual(row["metricCount"], expected["expected_metrics"])
            self.assertEqual(row["sourceSnapshotCount"], expected["expected_snapshots"])

    def test_profiles_and_comparisons_cover_all_counties_without_ranking(self) -> None:
        profiles: dict[str, list[dict]] = defaultdict(list)
        for row in self.index["profileIndicators"]:
            profiles[row["measureKey"]].append(row)
        self.assertEqual(len(profiles), len(PROFILE_MEASURES))
        self.assertTrue(all(len(rows) == 9 for rows in profiles.values()))

        comparisons: dict[str, list[dict]] = defaultdict(list)
        for row in self.index["comparisonRows"]:
            comparisons[row["measureKey"]].append(row)
        self.assertEqual(len(comparisons), len(COMPARISON_MEASURES))
        for rows in comparisons.values():
            self.assertEqual(len(rows), 9)
            self.assertEqual([row["countyName"] for row in rows], sorted(row["countyName"] for row in rows))
            self.assertEqual(len({row["periodEnd"] for row in rows}), 1)

    def test_catalog_exposes_namespaces_formulas_sources_and_universes(self) -> None:
        catalog = self.index["metricCatalog"]
        self.assertEqual(len(catalog), 139)
        self.assertEqual(len({row["measure_key"] for row in catalog}), 139)
        self.assertTrue(all(row["measure_key"].startswith(f"{row['module_id']}.") for row in catalog))
        self.assertTrue(all(row["formula"] for row in catalog if row["derivation"] == "derived"))
        self.assertTrue(all(row["source_ids"] for row in catalog))
        self.assertTrue(any(row["subgroup_dimension"] != "none" for row in catalog))

    def test_full_observations_preserve_uncertainty_and_source_linkage(self) -> None:
        observations = self.full["observations"]
        self.assertEqual(len(observations), 10224)
        self.assertTrue(all(row["source_ids"] for row in observations))
        acs_v14 = [
            row for row in observations
            if row["module_id"] == "equity"
            and "CENSUS_ACS5_DETAIL" in row["source_ids"]
            and row["value"] is not None
        ]
        self.assertEqual(len(acs_v14), 3448)
        self.assertTrue(all(row["margin_of_error_90"] is not None for row in acs_v14))

    def test_download_contract_matches_verified_exports(self) -> None:
        expected = {
            "public_use_observations.csv": self.output / "exports" / "public_use_observations.csv",
            "public_use_measure_catalog.csv": self.output / "exports" / "public_use_measure_catalog.csv",
            "public_use_source_registry.csv": self.output / "exports" / "public_use_source_registry.csv",
            "county_profiles.csv": self.output / "exports" / "county_profiles.csv",
            "county_comparisons.csv": self.output / "exports" / "county_comparisons.csv",
            "housing-public-use-data.json": self.output / "public" / "housing-public-use-data.json",
        }
        self.assertEqual({item["href"].rsplit("/", 1)[-1] for item in self.index["downloads"]}, set(expected))
        self.assertTrue(all(path.is_file() for path in expected.values()))
        with (self.output / "exports" / "quality_checks.csv").open(newline="", encoding="utf-8") as stream:
            quality = list(csv.DictReader(stream))
        self.assertEqual(len(quality), 16)
        self.assertTrue(all(row["passed"] == "1" for row in quality))

    def test_database_and_structural_verifier(self) -> None:
        database = self.output / "housing_public_use.sqlite"
        if database.is_file():
            with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM observation").fetchone()[0], 10224)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM measure").fetchone()[0], 139)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed=0").fetchone()[0], 0)
        result = verify_phase14_public_use()
        self.assertTrue(result["complete"], result["failed"])


if __name__ == "__main__":
    unittest.main()
