from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from bay_outlook.cli import main
from bay_outlook.database import summary


class PipelineTests(unittest.TestCase):
    def test_fixture_pipeline_builds_database(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "demo"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["demo", "--output-dir", str(output)]), 0)
            report = summary(output / "database" / "bay_outlook.sqlite")
            self.assertEqual(report["release_count"], 5)
            self.assertEqual(report["indicator_count"], 5)
            self.assertEqual(report["failed_validation_count"], 0)
            self.assertGreater(report["observation_count"], 0)


if __name__ == "__main__":
    unittest.main()
