# Phase 8 dashboard data contract

## Source requirements

The builder accepts a Phase 7 SQLite database only when:

- `analysis_metadata.schema_version` starts with `7.`;
- all required tables and views exist;
- SQLite `integrity_check` returns `ok`;
- `foreign_key_check` returns no row;
- every Phase 7 analytical quality check passes;
- the current facts contain no fixture rows, unless test mode is explicit.

The Phase 8 builder does not open the Phase 4 staging or Phase 5 warehouse databases.

## Embedded payload

The generated HTML assigns one escaped object to `window.BAY_OUTLOOK_DATA`.

| Key | Grain | Dashboard use |
|---|---|---|
| `meta` | One build | Versions, dates, benchmark definition, signal caution, fixture state |
| `overview` | One precomputed summary | Headline values, coverage counts, freshness, and signal inventory |
| `counties` | Nine configured counties | County selector and order |
| `metrics` | One row per Phase 7 analytical policy | Labels, comparison basis, direction, display precision, interpretation |
| `latest` | One row per latest county analytical series | Cards, comparisons, ranks, signals, freshness, and evidence details |
| `series` | One row per current county-period observation | County history charts and recent-value tables |
| `readiness` | One row per catalog indicator | Education pathway and all-indicator coverage matrix |
| `sources` | One row per active source | Geography concept, latest period, series count, and freshness mix |
| `qualityChecks` | One row per Phase 7 check | Data Health quality inventory |

## Overview summaries

### Unemployment

- Value: unweighted median of the nine latest county unemployment rates.
- Change: median county year-over-year percentage-point change.
- Breadth: count of latest county signals classified as improving.
- The result is labeled “median county unemployment,” never “Bay Area unemployment.”

### Real GDP

- Value: unweighted median of county year-over-year real GDP growth.
- Breadth: count of counties with positive annual growth.
- Raw county GDP size is not used for the headline or rank.

### Covered employment

- Value: count of counties with an exact prior-year growth comparison.
- When only one quarter is loaded, the dashboard reports `0/9` history-ready and withholds growth.
- Current raw job totals do not determine rank.

### Education

- Value: model-ready education indicators out of E1–E4.
- Supporting state: current live education observation count.
- Zero observations means absent validated source coverage, not a zero educational outcome.

## Comparison contract

A metric appears in the Overview comparison selector only when:

1. its Phase 7 policy has `comparison_basis` other than `none`;
2. at least two latest county rows have a non-null `comparison_value`.

The application renders `comparison_value`, `comparison_unit`, `benchmark_value`, `county_rank`, `benchmark_county_count`, and `county_coverage_pct` from Phase 7. It does not recalculate the benchmark or substitute the raw observation value.

## Time-series contract

The county history chart uses Phase 7 `vw_county_time_series` rows ordered by `period_sort_key`.

- Null values are excluded from the plotted line and remain absent from the table.
- A series with fewer than two valid observations receives an insufficient-history state.
- Year change comes from Phase 7 exact-lag fields.
- The latest median line is contextual; it does not imply that the same benchmark applied to every historical period.

## Publicly retained provenance

The browser payload includes the source ID, source vintage, retrieval timestamp, geography basis, adjustment, value status, notes, and metric interpretation. It omits raw file paths and raw-file hashes because those are audit fields for the warehouse/database layer rather than necessary public-interface content.

## Safe embedding

- JSON is serialized without executable object construction.
- Any `</` sequence is escaped before entering the inline script.
- Dynamic HTML strings pass source-derived labels and notes through entity escaping.
- No source value becomes a URL, script path, CSS rule, or event handler.

## Manifest

`dashboard_manifest.json` records:

- Phase 8 and Phase 7 schema versions;
- build and evidence as-of dates;
- portable source analysis path and SHA-256;
- fixture permission state;
- output filename and SHA-256;
- counts for counties, indicators, metrics, latest series, time-series points, and checks;
- public view inventory.

`inspect-dashboard` recalculates the HTML hash and reports whether it matches the manifest.
