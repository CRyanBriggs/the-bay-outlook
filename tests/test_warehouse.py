from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bay_outlook.cli import main
from bay_outlook.warehouse import _period_attributes, build_warehouse, warehouse_summary


class WarehouseTests(unittest.TestCase):
    def _fixture_staging(self, root: Path) -> Path:
        output = root / "demo"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["demo", "--output-dir", str(output)]), 0)
        return output / "database" / "bay_outlook.sqlite"

    def test_fixture_warehouse_builds_with_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = self._fixture_staging(root)
            warehouse = root / "analytics.sqlite"
            report = build_warehouse(staging, warehouse, include_fixtures=True)

            self.assertEqual(report["schema_version"], "5.0.0")
            self.assertEqual(report["table_counts"]["dim_indicator"], 25)
            self.assertEqual(report["table_counts"]["dim_source_release"], 5)
            self.assertEqual(report["table_counts"]["fact_observation"], 108)
            self.assertEqual(report["education_fact_count"], 14)
            self.assertEqual(report["publishable_observation_count"], 0)
            self.assertEqual(report["failed_quality_check_count"], 0)

            with sqlite3.connect(warehouse) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM warehouse_quality_checks").fetchone()[0],
                    10,
                )
                education_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT indicator_id FROM dim_indicator WHERE indicator_id LIKE 'E%'"
                    )
                }
                self.assertEqual(education_ids, {"E1", "E2", "E3", "E4"})

    def test_fixture_rows_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = self._fixture_staging(root)
            warehouse = root / "analytics.sqlite"
            report = build_warehouse(staging, warehouse)

            self.assertEqual(report["table_counts"]["fact_observation"], 0)
            self.assertEqual(report["table_counts"]["dim_indicator"], 25)
            self.assertEqual(report["table_counts"]["dim_geography"], 9)
            self.assertEqual(report["failed_quality_check_count"], 0)

    def test_rebuild_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = self._fixture_staging(root)
            warehouse = root / "analytics.sqlite"
            build_warehouse(staging, warehouse, include_fixtures=True)
            build_warehouse(staging, warehouse, include_fixtures=True)
            report = warehouse_summary(warehouse)

            self.assertEqual(report["table_counts"]["fact_observation"], 108)
            with sqlite3.connect(warehouse) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM warehouse_loads").fetchone()[0], 1)

    def test_fact_grain_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = self._fixture_staging(root)
            warehouse = root / "analytics.sqlite"
            build_warehouse(staging, warehouse, include_fixtures=True)

            with sqlite3.connect(warehouse) as connection:
                row = connection.execute("SELECT * FROM fact_observation LIMIT 1").fetchone()
                duplicate = (999999, *row[1:])
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO fact_observation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        duplicate,
                    )

    def test_period_semantics(self):
        self.assertEqual(_period_attributes("2024", "annual")["period_type"], "calendar_year")
        self.assertEqual(_period_attributes("2025-Q1", "quarterly")["quarter"], 1)
        self.assertEqual(_period_attributes("2026-06", "monthly")["month"], 6)
        academic = _period_attributes("2022-23", "annual")
        self.assertEqual(academic["period_type"], "academic_year")
        self.assertEqual(academic["academic_start_year"], 2022)
        self.assertEqual(academic["academic_end_year"], 2023)


if __name__ == "__main__":
    unittest.main()
