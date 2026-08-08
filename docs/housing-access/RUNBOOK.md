# Refresh and Review Runbook

## Prerequisites

- Python 3.11+ with project dependencies
- Node.js and npm/npx
- Network access to Census, California Courts, California HCD, and HUD User

HUD publishes PIT history as XLSB. The refresh invokes pinned `xlsx-cli@1.1.3`
through npx. Other workbooks use `openpyxl`; one malformed non-data timestamp in
the FY2026 FMR workbook is repaired in memory while the untouched workbook stays
the archived raw snapshot.

## Refresh

```bash
python -m bay_outlook.cli build-phase14
python -m bay_outlook.cli build-phase14-access
python -m bay_outlook.cli verify-phase14-access
```

The first command establishes the verified Version 1.1 baseline. The second
retrieves live files, preserves snapshots, and rebuilds exports, public JSON, and
SQLite. The verifier checks scope, history, lineage, raw hashes, calculations,
database integrity, workflow controls, and manifest hashes.

## Review checklist

1. Investigate any failed quality check; do not publish around it.
2. Compare release names and snapshot byte counts with the previous build.
3. Confirm all 36 PIT county-year count-type records remain present.
4. Review any changed PIT count design before discussing year-to-year movement.
5. Verify income-category labels and progress denominators.
6. Require named-human review before any new narrative conclusion.

## Offline manifest rebuild

After a successful build, recompute hashes without retrieving sources:

```bash
python -m bay_outlook.cli build-phase14-access --offline
```

The automation workflow creates a review artifact only. It contains no public
deployment step.
