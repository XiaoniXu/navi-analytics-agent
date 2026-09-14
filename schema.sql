-- =====================================================================
-- Navi Analytics warehouse - SQLite
-- =====================================================================
-- The ETL (src/etl_pipeline.py) materialises every table with pandas
-- to_sql, then applies this file. The CREATE TABLE statements below are
-- therefore no-ops at runtime and serve as the authoritative written
-- contract for each table's columns; the indexes and views below are
-- what this file actually creates.
--
-- Layering
--   dimensions : dim_users, dim_date
--   facts      : fact_app_events, fact_agent_interactions, fact_subscriptions
--   marts      : mart_user_day, mart_user_retention, mart_subscription_user,
--                daily_product_metrics
--   quality    : dq_quarantine_events, dq_profile_raw, dq_decisions,
--                dq_validation_results
--   reference  : metric_definitions
--
-- The dashboard and the Analytics Agent read the mart and reference
-- layers only. Neither reads raw CSV files, so a chart and an agent
-- answer cannot diverge.
-- =====================================================================

-- ------------------------- dimensions -------------------------------

CREATE TABLE IF NOT EXISTS dim_users (
    user_id               TEXT PRIMARY KEY,
    signup_ts             TEXT,
    signup_date           TEXT,
    country_code          TEXT,
    device_os             TEXT,
    acquisition_channel   TEXT,
    app_version_at_signup TEXT,
    marketing_consent     INTEGER
);

CREATE TABLE IF NOT EXISTS dim_date (
    date_key        TEXT PRIMARY KEY,
    year            INTEGER,
    month           INTEGER,
    year_month      TEXT,
    month_name      TEXT,
    iso_week        TEXT,
    week_start_date TEXT,
    day_of_week     INTEGER,
    day_name        TEXT,
    is_weekend      INTEGER
);

-- --------------------------- facts ----------------------------------

-- included_in_metrics = 0 marks test traffic and unknown users. Rows are
-- retained so every exclusion stays auditable from the warehouse itself.
CREATE TABLE IF NOT EXISTS fact_app_events (
    event_id            TEXT PRIMARY KEY,
    user_id             TEXT,
    session_id          TEXT,
    event_ts            TEXT,
    event_date          TEXT,
    event_name          TEXT,
    screen_name         TEXT,
    app_version         TEXT,
    device_os           TEXT,
    is_test_event       INTEGER,
    is_known_user       INTEGER,
    included_in_metrics INTEGER,
    source_file         TEXT
);

-- is_latency_outlier flags latency above 30s. Such rows stay in the
-- metric layer; the dashboard reports p50/p95 and never the mean.
CREATE TABLE IF NOT EXISTS fact_agent_interactions (
    interaction_id         TEXT PRIMARY KEY,
    user_id                TEXT,
    session_id             TEXT,
    request_ts             TEXT,
    request_date           TEXT,
    intent                 TEXT,
    tool_used              TEXT,
    model_name             TEXT,
    input_tokens           INTEGER,
    output_tokens          INTEGER,
    estimated_cost_usd     REAL,
    latency_ms             INTEGER,
    is_latency_outlier     INTEGER,
    response_status        TEXT,
    resolved_by_agent      INTEGER,
    escalated_to_human     INTEGER,
    satisfaction_score     REAL,
    has_satisfaction_score INTEGER,
    included_in_metrics    INTEGER,
    source_file            TEXT
);

CREATE TABLE IF NOT EXISTS fact_subscriptions (
    subscription_id     TEXT PRIMARY KEY,
    user_id             TEXT,
    trial_started_at    TEXT,
    trial_started_date  TEXT,
    paid_started_at     TEXT,
    paid_started_date   TEXT,
    cancelled_at        TEXT,
    cancelled_date      TEXT,
    monthly_price_usd   REAL,
    subscription_status TEXT,
    has_trial           INTEGER,
    has_paid            INTEGER,
    has_cancelled       INTEGER,
    trial_to_paid_days  REAL,
    source_file         TEXT
);

-- --------------------------- marts ----------------------------------

-- Grain: one row per user per active calendar day. Both DAU and agent
-- adoption are derived here so they share a denominator.
CREATE TABLE IF NOT EXISTS mart_user_day (
    user_id                           TEXT,
    metric_date                       TEXT,
    event_count                       INTEGER,
    session_count                     INTEGER,
    paywall_views                     INTEGER,
    app_version                       TEXT,
    has_app_activity                  INTEGER,
    agent_interaction_count           INTEGER,
    successful_agent_interaction_count INTEGER,
    resolved_agent_interaction_count  INTEGER,
    escalated_agent_interaction_count INTEGER,
    agent_latency_ms_sum              INTEGER,
    agent_cost_usd                    REAL,
    has_agent_request                 INTEGER,
    country_code                      TEXT,
    device_os                         TEXT,
    acquisition_channel               TEXT,
    PRIMARY KEY (user_id, metric_date)
);

CREATE TABLE IF NOT EXISTS mart_user_retention (
    user_id              TEXT PRIMARY KEY,
    first_activity_date  TEXT,
    d1_eligible          INTEGER,
    d1_retained          INTEGER,
    d7_eligible          INTEGER,
    d7_retained          INTEGER,
    first_activity_month TEXT,
    first_activity_week  TEXT,
    country_code         TEXT,
    device_os            TEXT,
    acquisition_channel  TEXT,
    app_version          TEXT
);

CREATE TABLE IF NOT EXISTS mart_subscription_user (
    user_id             TEXT PRIMARY KEY,
    signup_date         TEXT,
    country_code        TEXT,
    device_os           TEXT,
    acquisition_channel TEXT,
    app_version         TEXT,
    subscription_id     TEXT,
    trial_started_date  TEXT,
    paid_started_date   TEXT,
    cancelled_date      TEXT,
    monthly_price_usd   REAL,
    subscription_status TEXT,
    has_trial           INTEGER,
    has_paid            INTEGER,
    has_cancelled       INTEGER,
    trial_to_paid_days  REAL,
    signup_month        TEXT,
    is_retained_paid    INTEGER
);

CREATE TABLE IF NOT EXISTS daily_product_metrics (
    metric_date               TEXT PRIMARY KEY,
    year_month                TEXT,
    iso_week                  TEXT,
    week_start_date           TEXT,
    is_weekend                INTEGER,
    dau                       INTEGER,
    active_app_users          INTEGER,
    agent_users               INTEGER,
    app_events                INTEGER,
    sessions                  INTEGER,
    paywall_views             INTEGER,
    agent_interactions        INTEGER,
    agent_cost_usd            REAL,
    p50_latency_ms            REAL,
    p95_latency_ms            REAL,
    resolved                  INTEGER,
    escalated                 INTEGER,
    successes                 INTEGER,
    timeouts                  INTEGER,
    errors                    INTEGER,
    rated                     INTEGER,
    trial_starts              INTEGER,
    paid_conversions          INTEGER,
    cancellations             INTEGER,
    agent_adoption_rate       REAL,
    agent_resolution_rate     REAL,
    agent_escalation_rate     REAL,
    agent_success_rate        REAL,
    csat                      REAL,
    csat_response_rate        REAL,
    interactions_per_agent_user REAL
);

-- ------------------------- indexes ----------------------------------

CREATE INDEX IF NOT EXISTS idx_events_date        ON fact_app_events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_user_date   ON fact_app_events(user_id, event_date);
CREATE INDEX IF NOT EXISTS idx_events_metrics     ON fact_app_events(included_in_metrics, event_date);
CREATE INDEX IF NOT EXISTS idx_agent_date         ON fact_agent_interactions(request_date);
CREATE INDEX IF NOT EXISTS idx_agent_user_date    ON fact_agent_interactions(user_id, request_date);
CREATE INDEX IF NOT EXISTS idx_agent_intent       ON fact_agent_interactions(intent, request_date);
CREATE INDEX IF NOT EXISTS idx_user_day_date      ON mart_user_day(metric_date);
CREATE INDEX IF NOT EXISTS idx_user_day_user      ON mart_user_day(user_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_sub_user_signup    ON mart_subscription_user(signup_date);
CREATE INDEX IF NOT EXISTS idx_retention_first    ON mart_user_retention(first_activity_date);

-- -------------------------- views -----------------------------------

-- Agent interactions with the segment attributes the agent tools are
-- allowed to group by. app_version is the client-reported version for
-- that user on that date, falling back to the version recorded at signup.
CREATE VIEW IF NOT EXISTS v_agent_interactions_enriched AS
SELECT
    i.*,
    u.country_code,
    u.device_os,
    u.acquisition_channel,
    COALESCE(d.app_version, u.app_version_at_signup) AS app_version
FROM fact_agent_interactions i
JOIN dim_users u      ON u.user_id = i.user_id
LEFT JOIN mart_user_day d ON d.user_id = i.user_id AND d.metric_date = i.request_date
WHERE i.included_in_metrics = 1;

-- Convenience view for the data-quality status strip on the Overview tab.
CREATE VIEW IF NOT EXISTS v_dq_summary AS
SELECT
    (SELECT COUNT(*) FROM dq_quarantine_events)                                   AS quarantined_events,
    (SELECT COUNT(*) FROM fact_app_events WHERE included_in_metrics = 0)          AS excluded_events,
    (SELECT COUNT(*) FROM fact_app_events WHERE included_in_metrics = 1)          AS metric_events,
    (SELECT COUNT(*) FROM fact_agent_interactions WHERE is_latency_outlier = 1)   AS latency_outliers,
    (SELECT COUNT(*) FROM dq_validation_results WHERE status = 'PASS')            AS checks_passed,
    (SELECT COUNT(*) FROM dq_validation_results)                                  AS checks_total;
