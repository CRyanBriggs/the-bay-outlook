from __future__ import annotations

import json
import sqlite3
import unittest

from bay_outlook.constants import PROJECT_ROOT
from bay_outlook.phase14_access import (
    PHASE14_ACCESS_VERSION,
    REQUIRED_ACCESS_DOMAINS,
    _normalise_count_type,
    _ratio,
    verify_phase14_access,
)


class Phase14AccessUnitTests(unittest.TestCase):
    def test_count_type_normalization_preserves_meaning(self) -> None:
        self.assertEqual(_normalise_count_type("Sheltered-Only Count*"), "Sheltered-Only Count")
        self.assertEqual(
            _normalise_count_type("  Sheltered and Unsheltered Count  "),
            "Sheltered and Unsheltered Count",
        )

    def test_rate_calculation_uses_explicit_multiplier(self) -> None:
        self.assertEqual(_ratio(25, 1000, multiplier=1000.0), 25.0)
        self.assertIsNone(_ratio(25, 0, multiplier=1000.0))


class Phase14AccessArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload_path = (
            PROJECT_ROOT
            / "data"
            / "phase14"
            / "access"
            / "public"
            / "housing-access-data.json"
        )
        cls.database = PROJECT_ROOT / "data" / "phase14" / "access" / "housing_access.sqlite"
        if not cls.payload_path.is_file() or not cls.database.is_file():
            raise unittest.SkipTest("Version 1.2 live artifacts have not been built")
        cls.payload = json.loads(cls.payload_path.read_text(encoding="utf-8"))

    def test_public_payload_identity_scope_and_boundary(self) -> None:
        self.assertEqual(self.payload["version"], PHASE14_ACCESS_VERSION)
        self.assertEqual(self.payload["countyCount"], 9)
        self.assertEqual(set(self.payload["domains"]), REQUIRED_ACCESS_DOMAINS)
        self.assertIsNone(self.payload["pressureFramework"]["compositeScore"])
        self.assertFalse(self.payload["pressureFramework"]["rankingProduced"])
        self.assertFalse(self.payload["publicationBoundary"]["automatedNarrative"])

    def test_pit_count_type_is_complete_and_explicit(self) -> None:
        rows = self.payload["pitCountTypes"]
        self.assertEqual(len(rows), 36)
        self.assertTrue(all(row["countType"] for row in rows))
        sheltered_only = {
            row["countyFips"]
            for row in rows
            if row["period"] == "2025" and not row["fullShelteredAndUnshelteredCount"]
        }
        self.assertEqual(sheltered_only, {"06001", "06041", "06075", "06081", "06095"})

    def test_database_matches_payload_and_has_source_links(self) -> None:
        with sqlite3.connect(f"file:{self.database.resolve()}?mode=ro", uri=True) as connection:
            count = connection.execute("SELECT COUNT(*) FROM access_observation").fetchone()[0]
            links = connection.execute("SELECT COUNT(*) FROM observation_source").fetchone()[0]
            failed = connection.execute("SELECT COUNT(*) FROM quality_check WHERE passed = 0").fetchone()[0]
        self.assertEqual(count, self.payload["observationCount"])
        self.assertGreaterEqual(links, count)
        self.assertEqual(failed, 0)

    def test_version_1_2_verifier_passes_all_public_reproduction_gates(self) -> None:
        result = verify_phase14_access()
        self.assertTrue(result["complete"], result["failed"])
        self.assertEqual(result["passing"], result["total"])


if __name__ == "__main__":
    unittest.main()
