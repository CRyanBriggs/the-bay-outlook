# Methodology

## Evidence inheritance

Version 1.5 reads the verified public payloads from Versions 1.1 through 1.4. It performs no live-source retrieval and does not replace any inherited value. Each observation receives a module identifier and a namespaced measure key so similarly named measures remain distinguishable.

## County profiles

Profiles use a fixed, documented set of measures. For each measure, the builder selects the most recent period shared by all nine counties. Profile cards remain descriptive and retain period, unit, derivation type, source identifiers, and any available 90% margin of error.

## Cross-county comparisons

Every comparison uses one common period. Counties are displayed alphabetically, never ordered by value. The interface therefore supports comparison without turning descriptive evidence into a ranking.

## Derived indicators

No composite indicator is created. Existing ratios, shares, gaps, medians, and progress measures are labeled `derived` and expose their formula or exact method. Published source values are labeled `published`; deterministic sums and counts are labeled `aggregated`.

## Statistical universes

ACS household/person residence measures and LODES workplace-job records remain separate. Race and Hispanic-origin table iterations remain overlapping categories and are never summed. Version 1.4 ACS estimates retain their 90% margins of error.

## Publication

The layer provides evidence organization, not automated interpretation. Narrative conclusions, causal language, forecasts, and policy recommendations require named-human review.
