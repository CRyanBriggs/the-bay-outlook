# Methodology

## Geography and periods

All outputs use the nine configured Bay Area county FIPS codes. HCD jurisdiction records are aggregated to county. Annual Progress Report histories cover reporting years 2018–2025; missing values remain missing rather than becoming zero unless the source record explicitly reports zero.

## Applications and production stages

HCD APR Table A supplies project applications, proposed units, decision status, SB 35 flags, entitlement dates, permit dates, and completion dates. Table A2 supplies annual housing-unit activity, including accessory dwelling units. Counts of applications, entitlements, permits, and completions are annual stage flows. They are not presented as a cohort funnel because the projects counted at each stage can differ.

The annual completion-to-permit flow percentage divides same-year completed units by same-year building-permit units. It is a descriptive flow ratio, not the completion rate of a permit cohort, and can exceed 100 percent.

## Deterministic timelines

Application projects are matched to later records within the same jurisdiction. The first rule is exact tracking ID; the fallback is exact assessor parcel number when a tracking-ID match is unavailable. Fuzzy strings and addresses are not used. Negative or structurally invalid intervals are excluded. Every median is paired with eligible-project, matched-project, and interval-specific sample counts.

## Housing-element compliance

The HCD Housing Element Review and Compliance Report is filtered to the nine counties and sixth-cycle records. The source's compliance and review labels are preserved. `Enforcement Out` remains a distinct source status and is counted as out of compliance in the county summary.

## RHNA delivery

County sixth-cycle allocations are inherited from the hash-verified Version 1.1 HCD RHNA response. Reported 2023–2025 permits and completions are summed separately. Permit progress is permits divided by allocation; completion progress is completions divided by allocation. The gaps are allocation minus each respective numerator. These are partial-cycle administrative indicators, not forecasts of cycle-end performance.

## Zoning and rezoning

The California Statewide Zoning North layer is queried with a grouped count and mapped-area sum by county, standardized zoning class, and source date. County shares divide each class's mapped polygon area by total mapped area. They describe the compiled map only; they do not estimate parcel-level legal capacity, allowed density, development feasibility, or entitled units.

HCD APR Table C supplies jurisdiction-reported rezoning acres, lower-income capacity, realistic capacity, and site records for years in which a row is present. These conditionally reported rows are not imputed into a complete county-year panel.

## Lineage

The build preserves 21 immutable evidence snapshots: eight annual Table A county slices, eight annual Table A2 county slices, one Table C slice, one compliance slice, one grouped zoning response, and two complete official RHNA responses. Every normalized observation carries source IDs, release identifiers, retrieval time, and the corresponding snapshot SHA-256.
