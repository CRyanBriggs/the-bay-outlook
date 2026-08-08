# Phase 4 methodology

## Common interpretation rules

1. County FIPS codes are stored as five-character strings, including leading zeroes.
2. Rates are stored in percentage points unless the unit explicitly says `proportion`.
3. Suppressed values are stored as null with status `suppressed`; they are never converted to zero.
4. Preliminary observations retain status `preliminary`.
5. Raw source files are immutable and identified by SHA-256.
6. Fixture data have dataset status `fixture` and may not be published.
7. The Bay Area aggregate is not created unless the metric has a defensible aggregation method.

## L1 — BLS LAUS

- Geography represents worker residence.
- County series are generally not seasonally adjusted.
- Series suffixes: `003` unemployment rate, `004` unemployment, `005` employment, `006` labor force.
- Monthly periods `M01`–`M12` are retained; `M13` is the annual average.

## L2 — BLS QCEW

- Geography represents establishment location, not worker residence.
- The Phase 4 headline uses ownership `0`, industry `10`, and aggregation level `70`: total covered employment across ownerships and industries.
- Quarterly employment is the arithmetic mean of the three published monthly employment levels.

## O1 — BEA CAGDP1

- Line code `1` is real county GDP in thousands of chained 2017 dollars in the current source vintage.
- GDP is a measure of production by location, not resident income.
- BEA revisions can change the full historical series. Every vintage must therefore be retained.

## H1 — ACS housing-cost burden

- Renter burden uses table B25070 and excludes units for which gross rent as a percentage of income is not computed.
- Owner burden uses table B25091 and includes owners with and without a mortgage, excluding not-computed cases.
- Burden means at least 30 percent of household income. Severe burden means at least 50 percent.
- The Phase 4 adapter retains component counts and published component margins of error. A derived ratio is not assigned a fabricated margin of error.

## E2 — California college-going rate

- County totals select aggregate level `C`, charter status `All`, alternative-school status `All`, reporting category `TA`, and completer type `TA`.
- A–G readiness is calculated from `AGY` completers divided by total completers for the same county and academic year.
- A no-enrollment record is not necessarily proof that a student did not attend college because FERPA-blocked directory information can affect National Student Clearinghouse matches.
- CDE suppressed cells remain null.

## Source URLs

- https://www.bls.gov/lau/
- https://www.bls.gov/cew/
- https://www.bea.gov/data/gdp/gdp-county
- https://www.census.gov/programs-surveys/acs/data.html
- https://www.cde.ca.gov/ds/ad/cgrinfo.asp
