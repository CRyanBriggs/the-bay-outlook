# Refresh and Review Runbook

## Prerequisites

- Python 3.11+ with the project installed
- Network access to California HCD open-data files and the Statewide Zoning ArcGIS service
- A complete, verified Version 1.2 checkpoint

## Refresh

```bash
PYTHONPATH=src python -m bay_outlook.cli build-phase14-production
PYTHONPATH=src python -m bay_outlook.cli verify-phase14-production
```

The build retrieves current official files, preserves county/year slices and complete query responses, rebuilds exports and SQLite, and verifies the Site payload against the public JSON. The second command runs the independent 21-gate completion audit.

## Review checklist

1. Investigate any failed quality check; do not publish around it.
2. Compare upstream SHA-256 values, byte counts, source releases, and row counts with the previous build.
3. Confirm every annual county series still has eight observations for 2018–2025.
4. Review the eligible, matched, and interval sample counts before discussing timeline medians.
5. Keep annual stage flows separate; do not label them as a cohort funnel.
6. Confirm RHNA permit and completion progress recompute independently.
7. Confirm mapped zoning shares sum to 100 percent by county and retain the capacity disclaimer.
8. Start the Site preview and inspect desktop and mobile layouts.
9. Require named-human review before publishing any new narrative conclusion.
10. Checkpoint the Site and publish the repository only after verification passes.

## Offline manifest rebuild

After independently recording public Site and GitHub identifiers, recompute the manifest and file hashes without source retrieval:

```bash
PYTHONPATH=src python -m bay_outlook.cli build-phase14-production --offline
```

The automation workflow creates a review artifact only. It contains no public deployment step.
