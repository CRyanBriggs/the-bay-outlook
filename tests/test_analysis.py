from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bay_outlook.analysis import (
    _period_attributes,
    analysis_summary,
    build_analysis,
    export_analysis,
)
from bay_outlook.cli import main
from bay_outlook.warehouse import build_warehouse


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        demo = cls.root / "demo"
        with contextlib.redirect_stdout(io.StringIO()):
            if main(["demo", "--output-dir", str(demo)]) != 0:
                raise RuntimeError("Fixture staging build failed")
        cls.staging = demo / "database" / "bay_outlook.sqlite"
        cls.warehouse = cls.root / "warehouse.sqlite"
        cls.analysis = cls.root / "analysis.sqlite"
        build_warehouse(cls.staging, cls.warehouse, include_fixtures=True)
        build_analysis(
            cls.warehouse,
            cls.analysis,
            include_fixtures=True,
            as_of="2026-08-07",
        )

    def test_fixture_analysis_builds_with_integrity(self):
        report = analysis_summary(self.analysis)
        self.assertEqual(report["schema_version"], "7.0.0")
        self.assertEqual(report["table_counts"]["current_observation"], 108)
        self.assertEqual(report["table_counts"]["fact_metric_trend"], 108)
        self.assertEqual(report["table_counts"]["fact_latest_signal"], 106)
        self.assertEqual(report["table_counts"]["indicator_readiness"], 25)
        self.assertEqual(report["failed_quality_check_count"], 0)
        with sqlite3.connect(self.analysis) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM analysis_quality_checks").fetchone()[0],
                13,
            )

    def test_growth_benchmarks_rank_change_not_raw_size(self):
        with sqlite3.connect(self.analysis) as connection:
            rows = connection.execute(
                """
                SELECT geography_name, value, year_change_percent,
                       comparison_value, county_rank, trend_signal
                FROM vw_latest_county_snapshot
                WHERE metric_id = 'real_gdp'
                ORDER BY county_rank
                """
            ).fetchall()
        self.assertEqual(rows[0][0], "Contra Costa")
        self.assertAlmostEqual(rows[0][2], 2.3529411764705883)
        self.assertEqual(rows[0][3], rows[0][2])
        self.assertGreater(rows[1][1], rows[0][1])
        self.assertEqual(rows[0][5], "improving")

    def test_lower_unemployment_rate_ranks_better(self):
        with sqlite3.connect(self.analysis) as connection:
            rows = connection.execute(
                """
                SELECT geography_name, value, benchmark_value, county_rank
                FROM vw_latest_county_snapshot
                WHERE metric_id = 'unemployment_rate'
                ORDER BY county_rank
                """
            ).fetchall()
        self.assertEqual(rows[0], ("Alameda", 4.2, 4.35, 1))
        self.assertEqual(rows[1], ("Contra Costa", 4.5, 4.35, 2))

    def test_education_rates_are_comparable_but_counts_are_not(self):
        with sqlite3.connect(self.analysis) as connection:
            college = connection.execute(
                """
                SELECT geography_name, value, benchmark_value, county_rank
                FROM vw_education_snapshot
                WHERE metric_id = 'college_going_rate_12mo'
                ORDER BY county_rank
                """
            ).fetchall()
            counts = connection.execute(
                """
                SELECT COUNT(*)
                FROM vw_education_snapshot
                WHERE metric_id = 'high_school_completers'
                  AND county_rank IS NOT NULL
                """
            ).fetchone()[0]
            readiness = dict(
                connection.execute(
                    """
                    SELECT indicator_id, readiness_status
                    FROM indicator_readiness WHERE indicator_id GLOB 'E[0-9]*'
                    """
                )
            )
            stopout_policy = connection.execute(
                """
                SELECT comparison_basis, rank_direction, change_method, policy_status
                FROM dim_analysis_metric
                WHERE indicator_id = 'E3' AND metric_id = 'first_year_stopout_rate'
                """
            ).fetchone()
        self.assertEqual(college[0], ("Alameda", 72.5, 68.75, 1))
        self.assertEqual(college[1], ("Contra Costa", 65.0, 68.75, 2))
        self.assertEqual(counts, 0)
        self.assertEqual(
            readiness,
            {"E1": "model_ready", "E2": "active", "E3": "model_ready", "E4": "model_ready"},
        )
        self.assertEqual(
            stopout_policy,
            ("level", "lower_is_better", "percentage_point", "configured"),
        )

    def test_unknown_component_metrics_receive_safe_inferred_policy(self):
        report = analysis_summary(self.analysis)
        self.assertGreater(report["inferred_policy_count"], 0)
        with sqlite3.connect(self.analysis) as connection:
            inferred = connection.execute(
                """
                SELECT comparison_basis, rank_direction, primary_metric
                FROM dim_analysis_metric
                WHERE metric_id = 'acs_b25070_001e'
                """
            ).fetchone()
        self.assertEqual(inferred, ("none", "neutral", 0))

    def test_fixtures_are_excluded_by_default(self):
        output = self.root / "analysis-no-fixtures.sqlite"
        report = build_analysis(self.warehouse, output, as_of="2026-08-07")
        self.assertEqual(report["table_counts"]["current_observation"], 0)
        self.assertEqual(report["education_snapshot_count"], 0)
        self.assertEqual(report["failed_quality_check_count"], 0)
        self.assertEqual(
            {row["readiness_status"] for row in report["education_readiness"]},
            {"model_ready"},
        )

    def test_exports_have_stable_rows_and_empty_views_keep_headers(self):
        output_dir = self.root / "exports"
        report = export_analysis(self.analysis, output_dir)
        self.assertEqual(report["exports"]["county_time_series.csv"]["row_count"], 108)
        self.assertEqual(report["exports"]["education_snapshot.csv"]["row_count"], 14)
        readiness_path = output_dir / "indicator_readiness.csv"
        with readiness_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 25)
        self.assertIn("readiness_status", rows[0])

    def test_failed_rebuild_leaves_existing_analysis_unchanged(self):
        before = _sha256(self.analysis)
        invalid_policy = self.root / "invalid-policy.csv"
        invalid_policy.write_text("indicator_id,metric_id\nL1,unemployment_rate\n", encoding="utf-8")
        with self.assertRaises(KeyError):
            build_analysis(
                self.warehouse,
                self.analysis,
                include_fixtures=True,
                as_of="2026-08-07",
                policy_path=invalid_policy,
            )
        self.assertEqual(_sha256(self.analysis), before)

    def test_period_indexes_support_exact_year_matching(self):
        january_2025 = _period_attributes(
            {"frequency": "monthly", "period_type": "month", "period": "2025-01", "period_sort_key": 202501}
        )
        january_2026 = _period_attributes(
            {"frequency": "monthly", "period_type": "month", "period": "2026-01", "period_sort_key": 202601}
        )
        quarter = _period_attributes(
            {"frequency": "quarterly", "period_type": "quarter", "period": "2025-Q4", "period_sort_key": 202512}
        )
        academic = _period_attributes(
            {"frequency": "annual", "period_type": "academic_year", "period": "2022-23", "period_sort_key": 202399}
        )
        self.assertEqual(january_2026[0] - january_2025[0], 12)
        self.assertEqual(january_2026[2], 12)
        self.assertEqual(quarter[1], "2025-12-31")
        self.assertEqual(academic[1], "2023-06-30")


if __name__ == "__main__":
    unittest.main()
