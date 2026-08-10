# Runbook

## Live candidate refresh

~~~bash
python -m pip install -e .
PYTHONPATH=src python -m bay_outlook.cli build-phase14-equity
PYTHONPATH=src python -m bay_outlook.cli verify-phase14-equity
PYTHONPATH=src python -m unittest discover -s tests -v
~~~

The live build retrieves official ACS API responses and LODES compressed OD files, hashes each accepted release, writes normalized exports and a private SQLite warehouse, then verifies the public package.

## Offline public verification

~~~bash
PYTHONPATH=src python -m bay_outlook.cli build-phase14-equity --offline
PYTHONPATH=src python -m bay_outlook.cli verify-phase14-equity
~~~

Offline mode does not retrieve sources. It checks the published exports, payload, methods, workflow boundary, and manifest hashes.

## Review gate

Before publication, a named human must review:

1. all quality checks;
2. low-denominator and high-uncertainty subgroup flags;
3. source vintages and hashes;
4. LODES component reconciliation;
5. FMR benchmark recomputation;
6. public labels and universes;
7. accessibility and rendered interaction behavior;
8. GitHub and Site release identity; and
9. the absence of rankings, scores, causal language, or automated narrative.

Raw ACS responses, compressed statewide LODES files, and the SQLite warehouse belong in the sealed checkpoint, not the recruiter-safe public repository. The Phase 10 baseline report remains on human_approval_hold.
