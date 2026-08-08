# Methodology

## Geography

All outputs use the nine configured Bay Area county FIPS codes. The HUD
Continuums of Care used here map one-to-one to those counties. HUD FMRs can be
shared because an FMR area does not always equal one county.

## Court process measures

The Judicial Council file is filtered to `Unlawful Detainer` and the nine
counties. Fiscal-year label `2023` is rendered as `FY2023-24`, ending June 30,
2024. Filing rates use the ACS renter-household estimate for the fiscal-year
start and multiply by 1,000. The denominator is a pooled five-year estimate.

## Pressure components

Renter overcrowding uses ACS B25014 renter categories above 1.00 occupants per
room; severe overcrowding uses categories above 1.50. Severe rent burden and
median gross rent are inherited from hash-verified Version 1.1 exports.
Components are displayed together but never combined into a score.

## Homelessness and system inventory

PIT overall, sheltered, and unsheltered estimates cover 2022–2025. `Count Types`
is retained for every CoC-year. During a sheltered-only cycle, the unsheltered
component may come from an earlier full count, so the interface marks that cycle
before showing change. HIC measures include ES/TH/SH, PSH, and RRH beds.

## Income-targeted production

Annual HCD APR permit counts by income category are aggregated from jurisdictions
to counties. Sixth-cycle RHNA allocations use the same categories. Progress is
the sum of 2023–2025 permits divided by the full sixth-cycle allocation. Permits
and allocations retain separate source records.

## HUD-assisted housing

For each county workbook, the `Summary of All HUD Programs` row is selected.
Units, reported occupancy, people, average household income, extremely-low-income
share, and waiting time are retained. Negative waiting-time sentinels become
missing values.

## Worker housing access

ACS B20002 supplies median annual earnings for people age 16+ with earnings. HUD
FY2026 revised FMR supplies one- and two-bedroom monthly standards. Annual income
required is `12 × FMR ÷ 0.30`; the monthly gap is
`FMR − (0.30 × annual earnings ÷ 12)`. No full-time-hours assumption is made.

## Lineage

Single-source and multi-source observations use an observation-to-source join.
Complete upstream files are preserved for court and HUD workbooks. ACS slices
preserve selected county rows plus the upstream file SHA-256 and byte count.
