# Phase 11 Automation Layer

Phase 11 turns the Phase 6 release-aware orchestrator into a controlled scheduled-maintenance system. It adds an update calendar, isolated candidate builds, release comparisons, significant-change holds, test-gated promotion, rollback evidence, and indicator-level freshness.

## Artifacts

| Artifact | Purpose |
|---|---|
| `config/phase11_automation.json` | Source contracts, thresholds, paths, and authority boundary |
| `.github/workflows/responsible-data-update.yml` | Scheduled and manual GitHub Actions entry point |
| `sql/phase11_automation.sql` | Calendar, run, comparison, review, decision, promotion, and QA schema |
| `data/database/bay_outlook_phase11.sqlite` | Operational control and audit database |
| `data/phase11/update_calendar.csv` | Human-readable Step 41 calendar |
| `data/phase11/indicator_freshness.csv` | Step 44 freshness for all 25 indicators |
| `data/phase11/index.html` | Offline automation desk |
| `data/phase11/phase11_manifest.json` | Hash-complete checkpoint manifest |

## Authority model

Automation may retrieve, preserve, normalize, validate, compare, test, rebuild, and—when no blocking change exists—promote data artifacts. It cannot determine causal meaning, write policy conclusions, approve the baseline report, or authorize external release.

The system records a named-human review decision separately from machine evidence. Review flags, comparisons, decisions, and promotions are append-only or immutable. Each decision binds to the SHA-256 digest of the candidate artifact, and reviewed promotion repeats health and test gates before replacing live files.
