# Methodology

## Unit of analysis

The primary public grain is county × period × metric × subgroup × tenure × sex. All nine Association of Bay Area Governments counties are required.

## Source systems

1. Census ACS 5-Year Detailed Tables, 2022–2024, for residence-based household and person estimates.
2. Census LEHD LODES8 JT01, 2021–2023, for primary-job home/work flows aggregated from census blocks to workplace county.
3. HUD FY2026 revised two-bedroom Fair Market Rent, inherited file-for-file from the verified Version 1.2 evidence layer.

The ACS and LODES universes remain separate. ACS describes sampled residents, householders, workers, or housing units according to each table. LODES describes primary jobs and uses workplace plus home geocodes. A job is not treated as a person or household.

## ACS table contract

- B25003A–I: tenure by race or Hispanic origin of householder.
- B25140A–I: housing costs over 30% and over 50% of income, by race or Hispanic origin and tenure. This family begins in 2023.
- B25007: tenure by age of householder.
- B25013: tenure by educational attainment of householder.
- B25115: tenure by household type and presence of own children.
- B20004: median earnings by educational attainment and sex.
- B24011: median earnings by broad occupation.
- B08303: commute duration for workers who did not work from home.
- B08006: work-from-home and public-transportation commute counts.
- B25044: tenure by vehicles available.

Race and Hispanic-origin iterations overlap. White alone, White alone not Hispanic, and Hispanic or Latino are explicitly retained as separate published universes and are never added together.

## Rates and uncertainty

A subgroup rate is numerator ÷ denominator × 100. Published point estimates and 90% margins of error are retained. For a sum, component margins are combined by root-sum-of-squares. For a subset proportion, the project uses the Census approximation:

MOE(p) = 100 ÷ D × sqrt(MOE(N)^2 − p^2 × MOE(D)^2)

If the radicand is negative, the conservative sum form is used. These derived margins are screening estimates, not new Census publications.

The project reliability flag is transparent and deterministic:

- low denominator: denominator below 100;
- high uncertainty: margin above 10 percentage points or more than 50% of the estimate;
- moderate uncertainty: margin above 5 points or more than 30% of the estimate;
- standard: none of those conditions.

These are project review flags, not Census reliability classifications.

## Housing-cost burden

For B25140A–I, the denominator is the stated tenure total minus “not calculated” cases. Rates are produced separately for owners with a mortgage, owners without a mortgage, and renters. Over-30% and over-50% measures remain separate.

## Earnings and rent benchmark

Annual income required for a two-bedroom FMR at 30% is FMR × 12 ÷ 0.30, equivalent to FMR × 40. Coverage is median individual earnings divided by that benchmark. The monthly gap is FMR − 30% of annual earnings ÷ 12.

This is a descriptive comparison. It is not a household budget, wage requirement, unit-availability measure, or conclusion that a worker can or cannot live in a county.

## LODES aggregation

JT01 primary-job OD files are streamed without loading the statewide file into memory. Workplace block and home block are reduced to five-digit county FIPS. County primary jobs reconcile to:

- same-county home and workplace;
- home in another California county; and
- home outside California.

Age and monthly job-earnings components must independently reconcile to S000. LODES disclosure-protection noise and rounding remain material limitations.
