# Data Dictionary

## Observation fields

| Field | Meaning |
|---|---|
| domain | One of the seven Version 1.4 domains |
| metric_id | Stable machine-readable measure identifier |
| metric_name | Public display label |
| county_fips / county_name | Five-digit county identifier and name |
| period / period_end | ACS or LODES vintage and calendar-year end |
| frequency | Annual, rolling five-year, or cross-vintage comparison |
| value / unit | Published or derived point estimate |
| margin_of_error_90 | ACS 90% margin of error; blank for LODES |
| numerator / denominator | Components used for a public rate or benchmark |
| benchmark_value | FY2026 two-bedroom FMR where applicable |
| subgroup_type / subgroup_id / subgroup_label | Explicit comparison population |
| tenure / sex | Separate dimensions when the source table provides them |
| geography_basis | Residence, workplace, or home/work relation |
| universe | Source-defined population, housing-unit, worker, or job universe |
| source_ids / source_releases | Lineage to accepted releases |
| retrieved_at / raw_sha256s | Retrieval timestamp and immutable digests |
| calculation | Reproducible transformation |
| notes | Interpretation boundary |
| reliability_flag | Project uncertainty screen or LODES noise flag |
| comparability_status | Time/source comparison constraint |

## Public exports

- equity_observations.csv: full normalized Version 1.4 layer.
- equity_snapshot.csv: latest 2024 ACS and 2023 LODES measures.
- race_housing.csv: latest race/Hispanic-origin tenure and burden measures.
- education_occupation_affordability.csv: latest education and occupation connections.
- commuting_connections.csv: latest ACS transportation and LODES workplace-job connections.
- source_snapshots.csv: accepted-release hashes and row counts.
- source_freshness.csv: retrieval and release status.
- quality_checks.csv: executable build checks.
- housing-equity-connections-data.json: exact public interface payload.
