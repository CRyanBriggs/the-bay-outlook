PRAGMA foreign_keys = ON;

CREATE TABLE analysis_metadata (
    metadata_key TEXT PRIMARY KEY,
    metadata_value TEXT NOT NULL
);

CREATE TABLE analysis_builds (
    build_key INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    as_of_date TEXT NOT NULL,
    source_warehouse TEXT NOT NULL,
    source_warehouse_sha256 TEXT NOT NULL,
    source_schema_version TEXT NOT NULL,
    include_fixtures INTEGER NOT NULL CHECK (include_fixtures IN (0, 1)),
    source_observation_count INTEGER NOT NULL DEFAULT 0,
    current_observation_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed')),
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE dim_analysis_metric (
    analysis_metric_key INTEGER PRIMARY KEY,
    indicator_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    analysis_label TEXT NOT NULL,
    analysis_class TEXT NOT NULL CHECK (analysis_class IN ('rate', 'level', 'currency', 'index')),
    primary_metric INTEGER NOT NULL CHECK (primary_metric IN (0, 1)),
    comparison_basis TEXT NOT NULL CHECK (comparison_basis IN ('none', 'level', 'year_change')),
    rank_direction TEXT NOT NULL CHECK (rank_direction IN ('higher_is_better', 'lower_is_better', 'neutral')),
    change_method TEXT NOT NULL CHECK (change_method IN ('percent', 'percentage_point')),
    stable_threshold REAL NOT NULL CHECK (stable_threshold >= 0),
    benchmark_method TEXT NOT NULL,
    display_precision INTEGER NOT NULL CHECK (display_precision BETWEEN 0 AND 6),
    interpretation_note TEXT NOT NULL,
    policy_status TEXT NOT NULL CHECK (policy_status IN ('configured', 'inferred')),
    UNIQUE (indicator_id, metric_id)
);

CREATE TABLE current_observation (
    analysis_observation_key INTEGER PRIMARY KEY,
    warehouse_observation_key INTEGER NOT NULL,
    analysis_metric_key INTEGER NOT NULL REFERENCES dim_analysis_metric(analysis_metric_key),
    indicator_id TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    release_id TEXT NOT NULL,
    source_vintage TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    geography_basis TEXT NOT NULL,
    geography_type TEXT NOT NULL,
    geography_code TEXT NOT NULL,
    geography_name TEXT NOT NULL,
    is_bay_area INTEGER NOT NULL CHECK (is_bay_area IN (0, 1)),
    period TEXT NOT NULL,
    frequency TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_sort_key INTEGER NOT NULL,
    time_index INTEGER NOT NULL,
    period_end_date TEXT NOT NULL,
    subgroup TEXT NOT NULL,
    industry_code TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL,
    margin_of_error REAL,
    value_status TEXT NOT NULL,
    dataset_status TEXT NOT NULL,
    notes TEXT NOT NULL,
    UNIQUE (
        analysis_metric_key, source_id, geography_type, geography_code, period, frequency,
        subgroup, industry_code, adjustment
    )
);

CREATE TABLE fact_metric_trend (
    analysis_observation_key INTEGER PRIMARY KEY REFERENCES current_observation(analysis_observation_key),
    previous_period TEXT,
    previous_value REAL,
    period_gap INTEGER,
    period_change_absolute REAL,
    period_change_percent REAL,
    year_ago_period TEXT,
    year_ago_value REAL,
    year_change_absolute REAL,
    year_change_percent REAL,
    rolling_3_value REAL,
    change_method TEXT NOT NULL
);

CREATE TABLE fact_county_benchmark (
    analysis_observation_key INTEGER PRIMARY KEY REFERENCES current_observation(analysis_observation_key),
    comparison_basis TEXT NOT NULL,
    comparison_value REAL NOT NULL,
    comparison_unit TEXT NOT NULL,
    county_count INTEGER NOT NULL CHECK (county_count > 0),
    county_coverage_pct REAL NOT NULL CHECK (county_coverage_pct > 0 AND county_coverage_pct <= 100),
    benchmark_method TEXT NOT NULL,
    benchmark_value REAL NOT NULL,
    minimum_value REAL NOT NULL,
    maximum_value REAL NOT NULL,
    gap_to_benchmark REAL NOT NULL,
    county_rank INTEGER,
    percentile REAL CHECK (percentile IS NULL OR percentile BETWEEN 0 AND 100)
);

CREATE TABLE fact_latest_signal (
    signal_key INTEGER PRIMARY KEY,
    analysis_observation_key INTEGER NOT NULL UNIQUE REFERENCES current_observation(analysis_observation_key),
    as_of_date TEXT NOT NULL,
    age_months INTEGER,
    recency_status TEXT NOT NULL CHECK (recency_status IN ('current', 'delayed', 'stale', 'unknown')),
    trend_signal TEXT NOT NULL CHECK (
        trend_signal IN ('improving', 'worsening', 'rising', 'falling', 'stable', 'insufficient_history')
    ),
    signal_basis TEXT NOT NULL CHECK (signal_basis IN ('year_over_year', 'period_over_period', 'insufficient_history')),
    signal_change REAL,
    signal_change_unit TEXT NOT NULL,
    interpretation_caution TEXT NOT NULL
);

CREATE TABLE indicator_readiness (
    indicator_id TEXT PRIMARY KEY,
    pillar TEXT NOT NULL,
    indicator_name TEXT NOT NULL,
    priority TEXT NOT NULL,
    phase4_pilot INTEGER NOT NULL CHECK (phase4_pilot IN (0, 1)),
    planned_frequency TEXT NOT NULL,
    configured_metric_count INTEGER NOT NULL,
    observed_metric_count INTEGER NOT NULL,
    current_observation_count INTEGER NOT NULL,
    geography_count INTEGER NOT NULL,
    first_period TEXT,
    latest_period TEXT,
    readiness_status TEXT NOT NULL CHECK (readiness_status IN ('active', 'model_ready', 'planned')),
    readiness_note TEXT NOT NULL
);

CREATE TABLE analysis_quality_checks (
    check_name TEXT PRIMARY KEY,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    affected_count INTEGER NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX idx_current_metric_period
ON current_observation(analysis_metric_key, time_index);

CREATE INDEX idx_current_geography_period
ON current_observation(geography_type, geography_code, time_index);

CREATE INDEX idx_latest_signal_recency
ON fact_latest_signal(recency_status, trend_signal);

CREATE VIEW vw_current_observation_enriched AS
SELECT
    o.*,
    m.analysis_label,
    m.analysis_class,
    m.primary_metric,
    m.comparison_basis,
    m.rank_direction,
    m.change_method,
    m.stable_threshold,
    m.display_precision,
    m.interpretation_note,
    m.policy_status
FROM current_observation AS o
JOIN dim_analysis_metric AS m USING (analysis_metric_key);

CREATE VIEW vw_county_time_series AS
SELECT
    o.*,
    t.previous_period,
    t.previous_value,
    t.period_gap,
    t.period_change_absolute,
    t.period_change_percent,
    t.year_ago_period,
    t.year_ago_value,
    t.year_change_absolute,
    t.year_change_percent,
    t.rolling_3_value,
    b.comparison_value,
    b.comparison_unit,
    b.county_count AS benchmark_county_count,
    b.county_coverage_pct,
    b.benchmark_value,
    b.gap_to_benchmark,
    b.county_rank,
    b.percentile
FROM vw_current_observation_enriched AS o
JOIN fact_metric_trend AS t USING (analysis_observation_key)
LEFT JOIN fact_county_benchmark AS b USING (analysis_observation_key)
WHERE o.geography_type = 'county'
  AND o.is_bay_area = 1;

CREATE VIEW vw_latest_county_snapshot AS
SELECT
    ts.*,
    s.as_of_date,
    s.age_months,
    s.recency_status,
    s.trend_signal,
    s.signal_basis,
    s.signal_change,
    s.signal_change_unit,
    s.interpretation_caution
FROM vw_county_time_series AS ts
JOIN fact_latest_signal AS s USING (analysis_observation_key);

CREATE VIEW vw_headline_snapshot AS
SELECT *
FROM vw_latest_county_snapshot
WHERE primary_metric = 1;

CREATE VIEW vw_education_snapshot AS
SELECT *
FROM vw_latest_county_snapshot
WHERE indicator_id GLOB 'E[0-9]*';

CREATE VIEW vw_analytical_coverage AS
SELECT
    m.indicator_id,
    m.metric_id,
    m.analysis_label,
    m.primary_metric,
    m.policy_status,
    COUNT(o.analysis_observation_key) AS observation_count,
    COUNT(DISTINCT o.geography_code) AS geography_count,
    MIN(o.period_sort_key) AS first_period_sort_key,
    MAX(o.period_sort_key) AS latest_period_sort_key,
    COUNT(s.signal_key) AS latest_series_count
FROM dim_analysis_metric AS m
LEFT JOIN current_observation AS o USING (analysis_metric_key)
LEFT JOIN fact_latest_signal AS s USING (analysis_observation_key)
GROUP BY
    m.analysis_metric_key, m.indicator_id, m.metric_id,
    m.analysis_label, m.primary_metric, m.policy_status;
