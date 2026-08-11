# Data dictionary

## Observation fields

- `module_id`, `module_version`: originating verified release.
- `domain`, `measure_key`, `metric_id`, `metric_name`: namespaced measure identity.
- `county_fips`, `county_name`: county geography.
- `period`, `period_end`: source observation period.
- `value`, `unit`, `margin_of_error_90`: estimate and uncertainty.
- `subgroup_type`, `subgroup_id`, `subgroup_label`, `tenure`, `sex`: subgroup dimensions when applicable.
- `source_ids`, `source_releases`: retained lineage identifiers.
- `derivation`, `formula`: published, aggregated, or derived status and method.
- `comparability_status`, `geography_basis`, `universe`, `notes`: interpretation controls.

## Public index

The lightweight index contains module summaries, county profile indicators, same-period comparisons, definitions, sources, download links, quality checks, and publication boundaries. The complete normalized observation table is a separate downloadable CSV and JSON package.
