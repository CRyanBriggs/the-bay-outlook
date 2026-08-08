# Housing Observatory Runbook

## Local refresh

```bash
PYTHONPATH=src python -m bay_outlook.cli build-phase14
PYTHONPATH=src python -m bay_outlook.cli verify-phase14
PYTHONPATH=src python -m unittest discover -s tests -v
```

The live build needs HTTPS access to the documented Census, Redfin, and
California open-data endpoints. It does not require a Census API key because it
uses official table-based summary files.

## Review a candidate

1. Inspect `source_freshness.csv` for changed releases and retrieval times.
2. Compare the new `quality_checks.csv` and manifest with the previous accepted
   build.
3. Review material revisions, missing values, and unexpected county changes.
4. Confirm the public JSON is internally consistent with the exports and database.
5. Run the complete test suite and Phase 14 verifier.
6. Obtain a named human's approval before publishing new narrative analysis.

## Scheduled workflow

The monthly GitHub workflow produces a candidate artifact only. Its permissions
are read-only, and it contains no commit, push, Site deployment, or narrative
publication step. A human must review and deliberately publish an accepted
candidate.

## Failure handling

Do not replace missing observations with zero. Preserve the failed retrieval or
validation evidence, keep the last verified public build in place, document the
upstream condition, and rerun only after the source or adapter is understood.
