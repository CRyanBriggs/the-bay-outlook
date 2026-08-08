from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

from bay_outlook.constants import PROJECT_ROOT
from bay_outlook.operations import operations_summary
from bay_outlook.orchestration import (
    FetchedPackage,
    Orchestrator,
    OverlappingRunError,
    RawAsset,
    RunLock,
    SourceHandler,
)
from bay_outlook.sources import laus


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _write_config(
    root: Path,
    sources: list[dict[str, object]],
    *,
    max_attempts: int = 3,
) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "orchestration.json"
    payload = {
        "schema_version": "6.0.0",
        "state_database": "data/operations/operations.sqlite",
        "staging_database": "data/database/staging.sqlite",
        "warehouse_database": "data/database/analytics.sqlite",
        "lock": {"path": "data/operations/run.lock", "stale_after_minutes": 60},
        "retries": {
            "max_attempts": max_attempts,
            "initial_delay_seconds": 1,
            "backoff_multiplier": 2,
            "max_delay_seconds": 10,
            "persistent_backoff_minutes": 60,
            "persistent_backoff_max_minutes": 240,
        },
        "sources": sources,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _laus_policy(**overrides: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "source_id": "BLS_LAUS",
        "enabled": True,
        "cadence": "monthly",
        "check_interval_days": 7,
        "resolver": "rolling_year_window",
        "lookback_years": 1,
        "required_environment": [],
        "release_reference": "https://www.bls.gov/schedule/news_release/laus.htm",
        "policy_note": "test",
    }
    policy.update(overrides)
    return policy


def _fixture_handler(fetch_override=None, normalize_override=None) -> SourceHandler:
    content = (FIXTURES / "laus.json").read_bytes()

    def fetch(plan):
        if fetch_override:
            return fetch_override(plan)
        return FetchedPackage(
            source_id=laus.SOURCE_ID,
            target_release=plan.target_release,
            source_vintage=plan.target_release,
            source_release_date="2026-08-07",
            assets=(RawAsset("laus.json", content, "fixture://laus.json"),),
        )

    def normalize(package, retrieved_at):
        if normalize_override:
            return normalize_override(package, retrieved_at)
        asset = package.assets[0]
        return laus.normalize(
            asset.content,
            raw_sha256=asset.sha256,
            retrieved_at=retrieved_at,
            source_release=package.source_vintage,
        )

    return SourceHandler(fetch, normalize)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.fixed_now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    def test_default_release_targets_and_credential_gate(self):
        orchestrator = Orchestrator(
            project_root=PROJECT_ROOT,
            environment={},
            clock=lambda: self.fixed_now,
        )
        report = orchestrator.plan_report(as_of="2026-08-07")
        plans = {row["source_id"]: row for row in report["plans"]}

        self.assertEqual(plans["BLS_LAUS"]["target_release"], "LAUS-rolling-2025-2026")
        self.assertEqual(plans["BLS_QCEW"]["target_release"], "2025-Q4")
        self.assertEqual(plans["CENSUS_ACS5"]["target_release"], "ACS5-2024")
        self.assertEqual(plans["CENSUS_ACS5"]["action"], "blocked_credential")
        self.assertEqual(plans["CDE_CGR12"]["target_release"], "latest-published")

    def test_plan_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _write_config(root, [_laus_policy()])
            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler()},
                environment={},
                clock=lambda: self.fixed_now,
            )
            report = orchestrator.plan_report(as_of="2026-08-07")
            self.assertEqual(report["due_count"], 1)
            self.assertFalse((root / "data" / "operations" / "operations.sqlite").exists())

    def test_success_duplicate_and_single_warehouse_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _write_config(root, [_laus_policy()])
            warehouse_calls: list[tuple[Path, Path]] = []

            def warehouse_builder(staging, warehouse, include_fixtures=False):
                warehouse_calls.append((staging, warehouse))
                return {
                    "table_counts": {"fact_observation": 8},
                    "failed_quality_check_count": 0,
                }

            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler()},
                environment={},
                clock=lambda: self.fixed_now,
                sleep=lambda _: None,
                warehouse_builder=warehouse_builder,
            )
            first = orchestrator.run(as_of="2026-08-07")
            second = orchestrator.run(as_of="2026-08-08", force=True)

            self.assertEqual(first["status"], "succeeded")
            self.assertEqual(first["loaded_count"], 1)
            self.assertTrue(first["warehouse_refreshed"])
            self.assertEqual(second["duplicate_count"], 1)
            self.assertFalse(second["warehouse_refreshed"])
            self.assertEqual(len(warehouse_calls), 1)

            staging = root / "data" / "database" / "staging.sqlite"
            with sqlite3.connect(staging) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_releases").fetchone()[0], 1)
            state = operations_summary(root / "data" / "operations" / "operations.sqlite")
            self.assertEqual(state["run_count"], 2)
            self.assertEqual(state["attempt_count"], 2)
            self.assertEqual(state["fingerprint_count"], 1)

    def test_transient_errors_retry_then_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _write_config(root, [_laus_policy()], max_attempts=3)
            calls = 0
            delays: list[float] = []
            base_handler = _fixture_handler()

            def flaky_fetch(plan):
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise urllib.error.HTTPError(
                        "https://example.gov/data", 503, "unavailable", None, None
                    )
                return base_handler.fetch(plan)

            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler(fetch_override=flaky_fetch)},
                environment={},
                clock=lambda: self.fixed_now,
                sleep=delays.append,
            )
            report = orchestrator.run(as_of="2026-08-07", refresh_warehouse=False)

            self.assertEqual(report["loaded_count"], 1)
            self.assertEqual(report["outcomes"][0]["attempt_count"], 3)
            self.assertEqual(delays, [1, 2])
            state_path = root / "data" / "operations" / "operations.sqlite"
            with sqlite3.connect(state_path) as connection:
                statuses = [
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM source_attempts ORDER BY attempt_id"
                    )
                ]
            self.assertEqual(statuses, ["retry_scheduled", "retry_scheduled", "succeeded"])

    def test_transient_failure_enters_persistent_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _write_config(root, [_laus_policy()], max_attempts=2)

            def failing_fetch(plan):
                raise urllib.error.HTTPError("https://example.gov/data", 502, "bad gateway", None, None)

            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler(fetch_override=failing_fetch)},
                environment={},
                clock=lambda: self.fixed_now,
                sleep=lambda _: None,
            )
            report = orchestrator.run(as_of="2026-08-07", refresh_warehouse=False)
            waiting = orchestrator.plan_report(as_of="2026-08-07T12:30:00Z")
            forced = orchestrator.plan_report(as_of="2026-08-07T12:30:00Z", force=True)

            self.assertEqual(report["outcomes"][0]["status"], "failed_transient")
            self.assertEqual(waiting["plans"][0]["reason"], "retry_backoff")
            self.assertFalse(waiting["plans"][0]["due"])
            self.assertTrue(forced["plans"][0]["due"])

    def test_missing_credential_blocks_before_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = _laus_policy(required_environment=["TEST_API_KEY"])
            config = _write_config(root, [policy])
            called = False

            def forbidden_fetch(plan):
                nonlocal called
                called = True
                raise AssertionError("credential-blocked handler must not run")

            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler(fetch_override=forbidden_fetch)},
                environment={},
                clock=lambda: self.fixed_now,
            )
            report = orchestrator.run(as_of="2026-08-07", refresh_warehouse=False)

            self.assertFalse(called)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["blocked_count"], 1)
            self.assertEqual(report["outcomes"][0]["status"], "blocked_credential")

    def test_secret_values_are_redacted_from_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = _laus_policy(required_environment=["TEST_API_KEY"])
            config = _write_config(root, [policy], max_attempts=1)
            secret = "highly-sensitive-test-value"

            def unsafe_error(plan):
                raise ValueError(f"request failed: https://example.gov/data?key={secret}")

            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler(fetch_override=unsafe_error)},
                environment={"TEST_API_KEY": secret},
                clock=lambda: self.fixed_now,
            )
            report = orchestrator.run(as_of="2026-08-07", refresh_warehouse=False)
            message = report["outcomes"][0]["error"]

            self.assertNotIn(secret, message)
            self.assertIn("[REDACTED]", message)
            state_path = root / "data" / "operations" / "operations.sqlite"
            with sqlite3.connect(state_path) as connection:
                stored = connection.execute("SELECT error_message FROM source_attempts").fetchone()[0]
            self.assertNotIn(secret, stored)

    def test_validation_failure_preserves_raw_but_not_staging_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _write_config(root, [_laus_policy()], max_attempts=1)

            def invalid_normalizer(package, retrieved_at):
                raise ValueError("simulated schema drift")

            orchestrator = Orchestrator(
                project_root=root,
                config_path=config,
                handlers={laus.SOURCE_ID: _fixture_handler(normalize_override=invalid_normalizer)},
                environment={},
                clock=lambda: self.fixed_now,
            )
            report = orchestrator.run(as_of="2026-08-07", refresh_warehouse=False)

            self.assertEqual(report["outcomes"][0]["status"], "failed_permanent")
            raw_files = list((root / "data" / "raw" / laus.SOURCE_ID).rglob("laus.json"))
            self.assertEqual(len(raw_files), 1)
            self.assertFalse((root / "data" / "database" / "staging.sqlite").exists())

    def test_lock_rejects_overlap_and_recovers_stale_file(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "operations" / "run.lock"
            with RunLock(lock_path, stale_after_minutes=60, clock=lambda: self.fixed_now):
                with self.assertRaises(OverlappingRunError):
                    with RunLock(lock_path, stale_after_minutes=60, clock=lambda: self.fixed_now):
                        pass
            self.assertFalse(lock_path.exists())

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"token": "abandoned", "created_at": "2026-08-07T09:00:00Z"}),
                encoding="utf-8",
            )
            with RunLock(lock_path, stale_after_minutes=60, clock=lambda: self.fixed_now):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
