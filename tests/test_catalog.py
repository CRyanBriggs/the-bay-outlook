from __future__ import annotations

import csv
import unittest

from bay_outlook.constants import BAY_AREA_FIPS, PROJECT_ROOT


class CatalogTests(unittest.TestCase):
    def test_nine_counties_are_configured(self):
        self.assertEqual(len(BAY_AREA_FIPS), 9)
        self.assertEqual(BAY_AREA_FIPS, {"06001", "06013", "06041", "06055", "06075", "06081", "06085", "06095", "06097"})

    def test_indicator_catalog_contract(self):
        with (PROJECT_ROOT / "metadata" / "indicator_catalog.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 25)
        self.assertEqual(len({row["indicator_id"] for row in rows}), 25)
        pilots = {row["indicator_id"] for row in rows if row["phase4_pilot"] == "Yes"}
        self.assertEqual(pilots, {"L1", "L2", "O1", "H1", "E2"})


if __name__ == "__main__":
    unittest.main()
