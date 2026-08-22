-- Suggested SQLite warehouse schema for the Navi Analytics Agent project.
-- Adapt this schema as your ETL and metric definitions evolve.

CREATE TABLE IF NOT EXISTS dim_users (
    user_id TEXT PRIMARY KEY,
    signup_ts TEXT,
    signup_date TEXT,
    country_code TEXT,
    device_os TEXT,
    acquisition_channel TEXT,
    app_version_at_signup TEXT,
    marketing_consent INTEGER
);

CREATE TABLE IF NOT EXISTS fact_app_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT,
    session_id TEXT,
    event_ts TEXT,
    event_date TEXT,
    event_name TEXT,
    screen_name TEXT,
    app_version TEXT,
    device_os TEXT,
    is_test_event INTEGER,
    included_in_metrics INTEGER,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS fact_agent_interactions (
    interaction_id TEXT PRIMARY KEY,
    user_id TEXT,
    session_id TEXT,
    request_ts TEXT,
    request_date TEXT,
    intent TEXT,
    tool_used TEXT,
    model_name TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    response_status TEXT,
    resolved_by_agent INTEGER,
    escalated_to_human INTEGER,
    satisfaction_score REAL,
    included_in_metrics INTEGER,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS fact_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    user_id TEXT,
    trial_started_at TEXT,
    paid_started_at TEXT,
    cancelled_at TEXT,
    monthly_price_usd REAL,
    subscription_status TEXT,
    source_file TEXT
);

CREATE TABLE IF NOT EXISTS mart_user_day (
    user_id TEXT,
    metric_date TEXT,
    country_code TEXT,
    device_os TEXT,
    acquisition_channel TEXT,
    has_app_activity INTEGER,
    has_agent_request INTEGER,
    agent_interaction_count INTEGER,
    successful_agent_interaction_count INTEGER,
    resolved_agent_interaction_count INTEGER,
    escalated_agent_interaction_count INTEGER,
    PRIMARY KEY (user_id, metric_date)
);

CREATE INDEX IF NOT EXISTS idx_events_date ON fact_app_events(event_date);
CREATE INDEX IF NOT EXISTS idx_events_user_date ON fact_app_events(user_id, event_date);
CREATE INDEX IF NOT EXISTS idx_agent_date ON fact_agent_interactions(request_date);
CREATE INDEX IF NOT EXISTS idx_agent_user_date ON fact_agent_interactions(user_id, request_date);
CREATE INDEX IF NOT EXISTS idx_user_day_date ON mart_user_day(metric_date);
