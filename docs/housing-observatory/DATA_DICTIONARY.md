# Housing Observatory Data Dictionary

## Core observation fields

| Field | Meaning |
|---|---|
| `domain` | One of the seven Version 1.1 housing domains. |
| `indicator_id` | Link to the project indicator catalog. |
| `metric_id` | Stable machine-readable measure identifier. |
| `county_fips` / `county_name` | Five-digit county FIPS and display name. |
| `period` / `period_end` | Source-native label and sortable final date. |
| `frequency` | Monthly, annual, five-year estimate, or cycle-to-date. |
| `value` / `unit` | Numeric point estimate and its declared unit. |
| `margin_of_error` | Published ACS margin where directly available; otherwise blank. |
| `source_id` / `source_tier` | Registry key and evidence tier. |
| `source_release` | Exact release or vintage used. |
| `retrieved_at` | UTC evidence-retrieval timestamp. |
| `raw_sha256` | Digest of the supporting snapshot or declared composite digest. |
| `calculation` | Published field or explicit derived formula. |
| `notes` | Interpretation and comparability caveats. |

The complete metric list is machine-readable in
`metadata/housing_indicator_catalog.csv`. Source definitions are in
`metadata/housing_source_registry.csv`.

## RHNA progress fields

`rhna_vli`, `rhna_li`, `rhna_mod`, and `rhna_above_mod` are summed sixth-cycle
allocations. `permitted_2023_2025` is the sum of jurisdiction-reported APR Table
A2 permits for those years. `progress_pct` divides that permit sum by
`rhna_total`. Income-category labels follow HCD's source fields; the dashboard
does not reinterpret them.

## SQLite tables

- `source_registry`: publishers, evidence tiers, cadence, and geography basis.
- `source_snapshot`: raw lineage, complete-upstream digests, and preserved paths.
- `housing_observation`: normalized county-period measures.
- `quality_check`: build-time validation evidence.
- `build_metadata`: product version, build time, and publication authority.
