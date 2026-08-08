# Housing Observatory Methodology

## Geography and time

The observatory reports the nine counties configured in `config/counties.csv`.
No “Bay Area total” is asserted where the underlying measure is a county median
or ratio. Periods retain each source's own cadence: monthly Redfin observations,
annual-final Building Permits Survey counts, annual ACS five-year estimates, and
annual or cycle-to-date HCD records.

## Ingestion and evidence preservation

Each retrieval records the source URL, release label, UTC retrieval time,
response metadata, file hash, and raw path. Small government responses are
preserved completely. Large ACS and Redfin files are streamed and hashed in
full; an exact nine-county source slice is preserved with the upstream file's
SHA-256 digest and byte count. The transformed exports never replace raw
evidence.

## Measures

- **Rent, value, income, and vacancy:** ACS tables B25064, B25077, B19013, and
  B25002.
- **Cost burden:** renter table B25070 and owner table B25091. Denominators
  exclude cases where the percentage was not computed.
- **Affordability ratios:** annualized median gross rent divided by median
  household income; median owner-reported home value divided by median household
  income.
- **Permits:** annual-final BPS units by structure size; total units are the sum
  of the four published structure categories.
- **Sales market:** Redfin county monthly homes sold, median sale price, days on
  market, active listings, and months of supply.
- **Production targets:** sixth-cycle jurisdiction RHNA allocations are summed
  to county; HCD APR Table A2 permits for 2023–2025 are summed to the same county
  geography. Progress is reported permits divided by allocation.

## Quality controls

The build rejects missing critical county coverage, short history, duplicate
natural keys, out-of-range rates, missing source snapshots, HCD category-total
mismatches, database integrity failures, and foreign-key failures. The verifier
independently recomputes every county-year rent-to-income ratio and checks that
the exact public JSON used by the Site matches the verified export.

## Interpretation boundary

Values are descriptive. No causal inference, forecast, policy effect, or claim
about a representative household is implied. Derived ACS point estimates do not
receive a margin of error unless one is explicitly calculated under Census
guidance; this release therefore leaves those derived margins blank.
