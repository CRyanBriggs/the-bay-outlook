from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path

from .constants import PROJECT_ROOT
from .database import print_summary
from .pipeline import persist
from .phase14 import build_phase14, verify_phase14
from .sources import acs, bea, cde, laus, qcew
from .storage import save_snapshot


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "processed",
        root / "database" / "bay_outlook.sqlite",
        root / "data_release_log.csv",
    )


def _persist_source(
    root: Path,
    source_id: str,
    observations,
    retrieved_at: str,
    source_release_date: str,
    raw_path: str,
    dataset_status: str = "fixture",
):
    processed_dir, database_path, release_log = _paths(root)
    release_id = f"{source_id}-fixture"
    return persist(
        observations=observations,
        release_id=release_id,
        source_id=source_id,
        source_release_date=source_release_date,
        retrieved_at=retrieved_at,
        raw_path=raw_path,
        processed_path=processed_dir / f"{source_id.casefold()}-fixture.csv",
        database_path=database_path,
        release_log_path=release_log,
        dataset_status=dataset_status,
    )


def _fixture_root() -> Path:
    return PROJECT_ROOT / "tests" / "fixtures"


def _fixture_snapshot(root: Path, source_id: str, filename: str, content: bytes, release: str):
    return save_snapshot(
        root,
        source_id,
        filename,
        content,
        source_url=f"fixture://{filename}",
        source_release=release,
        retrieved_at="2026-08-07T00:00:00Z",
        dataset_status="fixture",
    )


def command_demo(args: argparse.Namespace) -> int:
    root = Path(args.output_dir).resolve()
    fixtures = _fixture_root()
    root.mkdir(parents=True, exist_ok=True)
    retrieved = "2026-08-07T00:00:00Z"

    content = (fixtures / "laus.json").read_bytes()
    snapshot = _fixture_snapshot(root, laus.SOURCE_ID, "laus.json", content, "fixture-2026")
    rows = laus.normalize(
        content,
        raw_sha256=snapshot.sha256,
        retrieved_at=retrieved,
        source_release="fixture-2026",
        dataset_status="fixture",
    )
    _persist_source(root, laus.SOURCE_ID, rows, retrieved, "fixture", str(snapshot.path))

    qcew_rows = []
    qcew_paths = []
    for path in sorted(fixtures.glob("qcew_*.csv")):
        fips = path.stem.split("_")[-1]
        content = path.read_bytes()
        snapshot = _fixture_snapshot(root, qcew.SOURCE_ID, path.name, content, "fixture-2025-Q1")
        qcew_paths.append(str(snapshot.path))
        qcew_rows.extend(
            qcew.normalize(
                content,
                county_fips=fips,
                raw_sha256=snapshot.sha256,
                retrieved_at=retrieved,
                source_release="fixture-2025-Q1",
                dataset_status="fixture",
            )
        )
    _persist_source(root, qcew.SOURCE_ID, qcew_rows, retrieved, "fixture", ";".join(qcew_paths))

    csv_bytes = (fixtures / "bea_cagdp1_ca.csv").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CAGDP1_CA_2001_2024.csv", csv_bytes)
    content = buffer.getvalue()
    snapshot = _fixture_snapshot(root, bea.SOURCE_ID, "CAGDP1.zip", content, "fixture-2024")
    rows = bea.normalize(
        content,
        raw_sha256=snapshot.sha256,
        retrieved_at=retrieved,
        source_release="fixture-2024",
        dataset_status="fixture",
    )
    _persist_source(root, bea.SOURCE_ID, rows, retrieved, "fixture", str(snapshot.path))

    content = (fixtures / "acs_housing.json").read_bytes()
    snapshot = _fixture_snapshot(root, acs.SOURCE_ID, "acs_housing.json", content, "fixture-2024")
    rows = acs.normalize(
        content,
        year=2024,
        raw_sha256=snapshot.sha256,
        retrieved_at=retrieved,
        source_release="fixture-2024",
        dataset_status="fixture",
    )
    _persist_source(root, acs.SOURCE_ID, rows, retrieved, "fixture", str(snapshot.path))

    content = (fixtures / "cde_cgr12.tsv").read_bytes()
    snapshot = _fixture_snapshot(root, cde.SOURCE_ID, "cde_cgr12.tsv", content, "fixture-2022-23")
    rows = cde.normalize(
        content,
        raw_sha256=snapshot.sha256,
        retrieved_at=retrieved,
        source_release="fixture-2022-23",
        dataset_status="fixture",
    )
    _persist_source(root, cde.SOURCE_ID, rows, retrieved, "fixture", str(snapshot.path))
    print_summary(root / "database" / "bay_outlook.sqlite")
    return 0


def command_build_phase14(args: argparse.Namespace) -> int:
    report = build_phase14(built_at=args.built_at, refresh=not args.offline)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["verification"]["complete"] else 1


def command_verify_phase14(args: argparse.Namespace) -> int:
    report = verify_phase14(manifest_path=Path(args.manifest))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["complete"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bay-outlook-public")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="Run the core pipeline with non-publishable fixtures")
    demo.add_argument("--output-dir", default="build/demo")
    demo.set_defaults(func=command_demo)
    housing = subparsers.add_parser(
        "build-phase14",
        help="Build the Version 1.1 Housing Observatory from documented live sources",
    )
    housing.add_argument("--built-at", help="Optional deterministic UTC retrieval/build time")
    housing.add_argument(
        "--offline",
        action="store_true",
        help="Rebuild the manifest from existing verified housing exports",
    )
    housing.set_defaults(func=command_build_phase14)
    verify_housing = subparsers.add_parser(
        "verify-phase14",
        help="Verify housing coverage, lineage, calculations, update controls, and hashes",
    )
    verify_housing.add_argument("--manifest", default="data/phase14/phase14_manifest.json")
    verify_housing.set_defaults(func=command_verify_phase14)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
