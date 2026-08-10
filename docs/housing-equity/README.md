# Housing Equity & Economic Connections

Version 1.4 extends the Housing Observatory with transparent demographic and economic connections across all nine Bay Area counties.

The layer covers:

- housing tenure and housing-cost burden by race and Hispanic origin;
- homeownership by age, educational attainment of the householder, and household type;
- resident earnings by educational attainment and broad occupation compared with the verified FY2026 two-bedroom HUD Fair Market Rent benchmark;
- ACS commute duration, work-from-home, public-transit, and household vehicle-access measures; and
- LODES primary-job inflows, same-county home/work connections, monthly job-earnings bands, and worker-age bands by workplace county.

The product does not create an equity score, rank counties, infer causation, treat overlapping race categories as additive, or merge ACS household estimates with LODES job records into one statistical universe.

Build and verify:

~~~bash
python -m pip install -e .
PYTHONPATH=src python -m bay_outlook.cli build-phase14-equity
PYTHONPATH=src python -m bay_outlook.cli verify-phase14-equity
PYTHONPATH=src python -m unittest discover -s tests -v
~~~

The scheduled workflow produces a review artifact only. Public deployment and narrative analysis require named-human approval.
