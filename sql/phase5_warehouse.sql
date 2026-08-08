PRAGMA foreign_keys = ON;

CREATE TABLE warehouse_metadata (
    metadata_key TEXT PRIMARY KEY,
    metadata_value TEXT NOT NULL
);

CREATE TABLE warehouse_loads (
    load_key INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source_database TEXT NOT NULL,
    source_database_sha256 TEXT NOT NULL,
    include_fixtures INTEGER NOT NULL CHECK (include_fixtures IN (0, 1)),
    source_observation_count INTEGER NOT NULL DEFAULT 0,
    loaded_fact_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed')),
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE dim_indicator (
    indicator_key INTEGER PRIMARY KEY,
    indicator_id TEXT NOT NULL UNIQUE,
    pillar TEXT NOT NULL,
    indicator_name TEXT NOT NULL,
    primary_geography TEXT NOT NULL,
    planned_frequency TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('A', 'B', 'C')),
    phase4_pilot INTEGER NOT NULL CHECK (phase4_pilot IN (0, 1)),
    headline_measure TEXT NOT NULL
);

CREATE TABLE dim_metric (
    metric_key INTEGER PRIMARY KEY,
    indicator_key INTEGER NOT NULL REFERENCES dim_indicator(indicator_key),
    metric_id TEXT NOT NULL,
    metric_label TEXT NOT NULL,
    unit TEXT NOT NULL,
    frequency TEXT NOT NULL,
    UNIQUE (indicator_key, metric_id)
);

CREATE TABLE dim_source (
    source_key INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    dataset_name TEXT NOT NULL,
    agency TEXT NOT NULL,
    source_tier INTEGER NOT NULL,
    landing_url TEXT NOT NULL,
    access_method TEXT NOT NULL,
    credential_required INTEGER NOT NULL CHECK (credential_required IN (0, 1)),
    geographic_level TEXT NOT NULL,
    geography_basis TEXT NOT NULL,
    frequency TEXT NOT NULL,
    historical_coverage TEXT NOT NULL,
    typical_lag TEXT NOT NULL,
    license_or_terms TEXT NOT NULL,
    authority_score INTEGER NOT NULL,
    geographic_precision_score INTEGER NOT NULL,
    update_frequency_score INTEGER NOT NULL,
    historical_depth_score INTEGER NOT NULL,
    transparency_score INTEGER NOT NULL,
    reproducibility_score INTEGER NOT NULL,
    quality_total INTEGER NOT NULL CHECK (quality_total BETWEEN 0 AND 18),
    publishability_class TEXT NOT NULL CHECK (
        publishability_class IN ('Core', 'Caveat', 'Experimental', 'Exclude')
    ),
    phase4_status TEXT NOT NULL,
    notes TEXT NOT NULL
);

CREATE TABLE dim_geography (
    geography_key INTEGER PRIMARY KEY,
    geography_type TEXT NOT NULL,
    geography_code TEXT NOT NULL,
    geography_name TEXT NOT NULL,
    state_fips TEXT,
    county_fips TEXT,
    cde_county_code TEXT,
    is_bay_area INTEGER NOT NULL CHECK (is_bay_area IN (0, 1)),
    region_name TEXT,
    UNIQUE (geography_type, geography_code)
);

CREATE TABLE dim_period (
    period_key INTEGER PRIMARY KEY,
    period_label TEXT NOT NULL,
    frequency TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK (
        period_type IN ('calendar_year', 'quarter', 'month', 'academic_year', 'other')
    ),
    calendar_year INTEGER,
    quarter INTEGER CHECK (quarter IS NULL OR quarter BETWEEN 1 AND 4),
    month INTEGER CHECK (month IS NULL OR month BETWEEN 1 AND 12),
    academic_start_year INTEGER,
    academic_end_year INTEGER,
    sort_key INTEGER NOT NULL,
    UNIQUE (period_label, frequency)
);

CREATE TABLE dim_subgroup (
    subgroup_key INTEGER PRIMARY KEY,
    subgroup_code TEXT NOT NULL UNIQUE,
    subgroup_label TEXT NOT NULL,
    is_total INTEGER NOT NULL CHECK (is_total IN (0, 1))
);

CREATE TABLE dim_industry (
    industry_key INTEGER PRIMARY KEY,
    industry_code TEXT NOT NULL UNIQUE,
    industry_label TEXT NOT NULL,
    classification_system TEXT NOT NULL,
    is_total INTEGER NOT NULL CHECK (is_total IN (0, 1))
);

CREATE TABLE dim_adjustment (
    adjustment_key INTEGER PRIMARY KEY,
    adjustment_code TEXT NOT NULL UNIQUE,
    adjustment_label TEXT NOT NULL,
    is_seasonally_adjusted INTEGER NOT NULL CHECK (is_seasonally_adjusted IN (0, 1)),
    is_inflation_adjusted INTEGER NOT NULL CHECK (is_inflation_adjusted IN (0, 1))
);

CREATE TABLE dim_value_status (
    value_status_key INTEGER PRIMARY KEY,
    value_status TEXT NOT NULL,
    dataset_status TEXT NOT NULL,
    is_publishable INTEGER NOT NULL CHECK (is_publishable IN (0, 1)),
    is_suppressed INTEGER NOT NULL CHECK (is_suppressed IN (0, 1)),
    is_missing INTEGER NOT NULL CHECK (is_missing IN (0, 1)),
    UNIQUE (value_status, dataset_status)
);

CREATE TABLE dim_source_release (
    release_key INTEGER PRIMARY KEY,
    release_id TEXT NOT NULL UNIQUE,
    source_key INTEGER NOT NULL REFERENCES dim_source(source_key),
    source_vintage TEXT NOT NULL,
    source_release_date TEXT,
    retrieved_at TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    processing_status TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    source_observation_count INTEGER NOT NULL,
    revision_notes TEXT NOT NULL,
    next_expected_release TEXT NOT NULL,
    dataset_status TEXT NOT NULL
);

CREATE TABLE fact_observation (
    observation_key INTEGER PRIMARY KEY,
    release_key INTEGER NOT NULL REFERENCES dim_source_release(release_key),
    metric_key INTEGER NOT NULL REFERENCES dim_metric(metric_key),
    geography_key INTEGER NOT NULL REFERENCES dim_geography(geography_key),
    period_key INTEGER NOT NULL REFERENCES dim_period(period_key),
    subgroup_key INTEGER NOT NULL REFERENCES dim_subgroup(subgroup_key),
    industry_key INTEGER NOT NULL REFERENCES dim_industry(industry_key),
    adjustment_key INTEGER NOT NULL REFERENCES dim_adjustment(adjustment_key),
    value_status_key INTEGER NOT NULL REFERENCES dim_value_status(value_status_key),
    value REAL,
    margin_of_error REAL CHECK (margin_of_error IS NULL OR margin_of_error >= 0),
    notes TEXT NOT NULL,
    UNIQUE (
        release_key, metric_key, geography_key, period_key,
        subgroup_key, industry_key, adjustment_key
    )
);

CREATE TABLE fact_validation_result (
    release_key INTEGER NOT NULL REFERENCES dim_source_release(release_key),
    check_name TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    affected_count INTEGER NOT NULL,
    PRIMARY KEY (release_key, check_name)
);

CREATE TABLE warehouse_quality_checks (
    check_name TEXT PRIMARY KEY,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    affected_count INTEGER NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX idx_fact_metric_period
ON fact_observation(metric_key, period_key);

CREATE INDEX idx_fact_geography_period
ON fact_observation(geography_key, period_key);

CREATE INDEX idx_fact_release
ON fact_observation(release_key);

CREATE INDEX idx_metric_indicator
ON dim_metric(indicator_key);

CREATE INDEX idx_period_sort
ON dim_period(frequency, sort_key);

CREATE VIEW vw_observation_enriched AS
SELECT
    f.observation_key,
    i.indicator_id,
    i.pillar,
    i.indicator_name,
    m.metric_id,
    m.metric_label,
    m.unit,
    m.frequency,
    g.geography_type,
    g.geography_code,
    g.geography_name,
    g.is_bay_area,
    p.period_label AS period,
    p.period_type,
    p.calendar_year,
    p.quarter,
    p.month,
    p.academic_start_year,
    p.academic_end_year,
    p.sort_key AS period_sort_key,
    sg.subgroup_code AS subgroup,
    sg.subgroup_label,
    ind.industry_code,
    ind.industry_label,
    adj.adjustment_code AS adjustment,
    f.value,
    f.margin_of_error,
    vs.value_status,
    vs.dataset_status,
    vs.is_publishable,
    vs.is_suppressed,
    vs.is_missing,
    s.source_id,
    s.dataset_name,
    s.agency,
    s.geography_basis,
    sr.release_id,
    sr.source_vintage,
    sr.source_release_date,
    sr.retrieved_at,
    sr.raw_sha256,
    sr.raw_path,
    sr.processing_status,
    sr.validation_status,
    f.notes
FROM fact_observation AS f
JOIN dim_metric AS m ON m.metric_key = f.metric_key
JOIN dim_indicator AS i ON i.indicator_key = m.indicator_key
JOIN dim_geography AS g ON g.geography_key = f.geography_key
JOIN dim_period AS p ON p.period_key = f.period_key
JOIN dim_subgroup AS sg ON sg.subgroup_key = f.subgroup_key
JOIN dim_industry AS ind ON ind.industry_key = f.industry_key
JOIN dim_adjustment AS adj ON adj.adjustment_key = f.adjustment_key
JOIN dim_value_status AS vs ON vs.value_status_key = f.value_status_key
JOIN dim_source_release AS sr ON sr.release_key = f.release_key
JOIN dim_source AS s ON s.source_key = sr.source_key;

CREATE VIEW vw_publishable_observations AS
SELECT *
FROM vw_observation_enriched
WHERE is_publishable = 1
  AND processing_status = 'processed'
  AND validation_status = 'passed';

CREATE VIEW vw_county_observations AS
SELECT *
FROM vw_publishable_observations
WHERE geography_type = 'county'
  AND is_bay_area = 1;

CREATE VIEW vw_education_observations AS
SELECT *
FROM vw_publishable_observations
WHERE indicator_id GLOB 'E[0-9]*';

CREATE VIEW vw_indicator_coverage AS
SELECT
    i.indicator_id,
    i.pillar,
    i.indicator_name,
    i.priority,
    i.phase4_pilot,
    COUNT(f.observation_key) AS observation_count,
    COUNT(DISTINCT f.geography_key) AS geography_count,
    COUNT(DISTINCT sr.source_key) AS source_count,
    MIN(p.sort_key) AS first_period_sort_key,
    MAX(p.sort_key) AS last_period_sort_key
FROM dim_indicator AS i
LEFT JOIN dim_metric AS m ON m.indicator_key = i.indicator_key
LEFT JOIN fact_observation AS f ON f.metric_key = m.metric_key
LEFT JOIN dim_period AS p ON p.period_key = f.period_key
LEFT JOIN dim_source_release AS sr ON sr.release_key = f.release_key
GROUP BY
    i.indicator_key, i.indicator_id, i.pillar, i.indicator_name,
    i.priority, i.phase4_pilot;

