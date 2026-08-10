# Data Dictionary

## `production_time_series.csv`

| Field | Meaning |
|---|---|
| `domain` | One of the seven Version 1.3 evidence domains. |
| `metric_id` | Stable machine-readable measure identifier. |
| `metric_name` | Public measure label. |
| `county_fips`, `county_name` | Configured county identity. |
| `period`, `period_end` | Source-native display period and sortable end date. |
| `frequency` | Annual or current-snapshot cadence. |
| `value`, `unit` | Numeric observation and unit. |
| `source_ids` | Semicolon-delimited official source identifiers. |
| `source_releases` | Semicolon-delimited releases aligned with source IDs. |
| `retrieved_at` | UTC evidence-build timestamp. |
| `raw_sha256s` | Semicolon-delimited snapshot hashes aligned with releases. |
| `calculation` | Direct-source or explicit derivation rule. |
| `notes` | Definition and interpretation constraints. |
| `comparability_status` | Machine-readable timing or comparability warning. |

## Supporting exports

- `production_snapshot.csv`: latest available observation per county and metric.
- `timeline_coverage.csv`: eligible projects, exact-match counts, match rate, and interval sample counts.
- `rhna_delivery.csv`: allocation, permit and completion numerators, progress, and gaps.
- `zoning_composition.csv`: mapped area and share by county and standardized class.
- `rezone_breakdown.csv`: conditionally reported Table C acreage, capacity, and site counts.
- `compliance_records.csv`: jurisdiction-level sixth-cycle HCD status records.
- `source_freshness.csv`: source cadence, latest evidence, retrieval time, and snapshot count.
- `source_snapshots.csv`: immutable raw paths, release identifiers, and upstream hashes.
- `quality_checks.csv`: build-time gates; zero failed rows are permitted.

The complete 42-measure catalog is maintained at `metadata/housing_production_indicator_catalog.csv`.
