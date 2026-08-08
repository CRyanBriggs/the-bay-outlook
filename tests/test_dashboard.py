from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bay_outlook.analysis import build_analysis
from bay_outlook.cli import main
from bay_outlook.dashboard import build_dashboard, dashboard_payload, dashboard_summary
from bay_outlook.warehouse import build_warehouse


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        demo = cls.root / "demo"
        with contextlib.redirect_stdout(io.StringIO()):
            if main(["demo", "--output-dir", str(demo)]) != 0:
                raise RuntimeError("Fixture staging build failed")
        cls.warehouse = cls.root / "warehouse.sqlite"
        cls.analysis = cls.root / "analysis.sqlite"
        cls.dashboard = cls.root / "dashboard"
        build_warehouse(
            demo / "database" / "bay_outlook.sqlite",
            cls.warehouse,
            include_fixtures=True,
        )
        build_analysis(
            cls.warehouse,
            cls.analysis,
            include_fixtures=True,
            as_of="2026-08-07",
        )
        build_dashboard(
            cls.analysis,
            cls.dashboard,
            allow_fixtures=True,
        )

    def test_dashboard_build_contract(self):
        report = dashboard_summary(self.dashboard)
        self.assertEqual(report["schema_version"], "8.0.0")
        self.assertEqual(report["source_analysis_schema_version"], "7.0.0")
        self.assertTrue(report["allow_fixtures"])
        self.assertTrue(report["hash_matches_manifest"])
        self.assertEqual(report["counts"]["counties"], 9)
        self.assertEqual(report["counts"]["indicator_readiness"], 25)
        self.assertEqual(report["counts"]["latest_series"], 106)
        self.assertEqual(report["counts"]["time_series_points"], 108)
        self.assertEqual(report["counts"]["quality_checks"], 13)
        self.assertEqual(report["views"], ["overview", "county", "education", "health"])

    def test_dashboard_payload_preserves_education_and_comparison_truth(self):
        payload = dashboard_payload(
            self.analysis,
            allow_fixtures=True,
            built_at="2026-08-07T00:00:00Z",
        )
        education = {
            row["indicatorId"]: (row["readinessStatus"], row["currentObservationCount"])
            for row in payload["readiness"]
            if row["indicatorId"].startswith("E")
        }
        self.assertEqual(education["E2"], ("active", 14))
        self.assertEqual(education["E1"][0], "model_ready")
        self.assertEqual(education["E3"][0], "model_ready")
        self.assertEqual(education["E4"][0], "model_ready")
        self.assertEqual(payload["overview"]["education"]["observationCount"], 14)
        stopout = next(
            metric for metric in payload["metrics"]
            if metric["metricId"] == "first_year_stopout_rate"
        )
        self.assertEqual(stopout["rankDirection"], "lower_is_better")
        counts = [
            row for row in payload["latest"]
            if row["metricId"] == "high_school_completers"
        ]
        self.assertTrue(counts)
        self.assertTrue(all(row["countyRank"] is None for row in counts))

    def test_dashboard_blocks_fixture_publication_by_default(self):
        destination = self.root / "blocked-dashboard"
        with self.assertRaisesRegex(ValueError, "fixture rows"):
            build_dashboard(self.analysis, destination)
        self.assertFalse(destination.exists())

    def test_offline_app_has_accessibility_and_design_contract(self):
        html = (self.dashboard / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("{{DASHBOARD_", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("https://", html)
        self.assertIn('class="skip-link"', html)
        self.assertIn('id="main-content"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn('id="view-overview"', html)
        self.assertIn('id="view-county"', html)
        self.assertIn('id="view-education"', html)
        self.assertIn('id="view-health"', html)
        self.assertIn("window.BAY_OUTLOOK_DATA", html)

    def test_failed_rebuild_leaves_existing_dashboard_unchanged(self):
        before = _sha256(self.dashboard / "index.html")
        invalid_template = self.root / "invalid-dashboard.html"
        invalid_template.write_text("<html>missing required tokens</html>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing tokens"):
            build_dashboard(
                self.analysis,
                self.dashboard,
                allow_fixtures=True,
                template_path=invalid_template,
            )
        self.assertEqual(_sha256(self.dashboard / "index.html"), before)

    def test_manifest_matches_generated_file(self):
        manifest = json.loads(
            (self.dashboard / "dashboard_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["output_sha256"], _sha256(self.dashboard / "index.html"))
        self.assertEqual(manifest["phase"], 8)
        self.assertEqual(manifest["counts"]["metric_policies"], 62)


if __name__ == "__main__":
    unittest.main()
