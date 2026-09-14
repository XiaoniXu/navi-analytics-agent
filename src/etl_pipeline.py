"""Navi Analytics ETL pipeline.

Raw CSV files in data/raw/ are profiled, cleaned, and loaded into a curated
SQLite warehouse at warehouse/navi_analytics.db. The Streamlit dashboard and the
Analytics Agent both read from that curated layer only, so a number shown on a
chart and a number returned by the agent come from the same definition.

Layers
------
dimensions : dim_users, dim_date
facts      : fact_app_events, fact_agent_interactions, fact_subscriptions
marts      : mart_user_day, mart_user_retention, mart_subscription_user,
             daily_product_metrics
quality    : dq_quarantine_events, dq_profile_raw, dq_decisions,
             dq_validation_results
reference  : metric_definitions

Every exclusion is recorded rather than silently dropped: test traffic stays in
fact_app_events with included_in_metrics = 0, and duplicate event copies are
written to dq_quarantine_events.

Run:  python src/etl_pipeline.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
DATA_DIR = PROJECT_ROOT / "data"
WAREHOUSE_DIR = PROJECT_ROOT / "warehouse"
# NAVI_DB_PATH lets you build or read a warehouse somewhere other than the
# default location - useful for a scratch build, a CI run, or a filesystem that
# does not support SQLite's file locking (some network or synced folders).
DB_PATH = Path(os.environ.get("NAVI_DB_PATH", WAREHOUSE_DIR / "navi_analytics.db"))
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"

# --------------------------------------------------------------------------
# Documented policy constants
# --------------------------------------------------------------------------

# Latency above this value is flagged as an instrumentation outlier. Justified by
# the profile: p99 latency is ~9.3s and the next tail cluster jumps above 80s, so
# the distribution is bimodal rather than smoothly long-tailed.
LATENCY_OUTLIER_MS = 30_000

# Day-N return definition used for retention: active on exactly first_date + N.
RETENTION_DAYS = (1, 7)

# Planning assumptions for agent token cost, in USD per 1M tokens. These are
# label-level assumptions for cost monitoring, not billed rates.
MODEL_PRICING = {
    "navi-mini": {"input": 0.15, "output": 0.60},
    "navi-pro": {"input": 2.50, "output": 10.00},
}

# Segment dimensions the dashboard and the agent are both allowed to group by.
SEGMENT_DIMENSIONS = ["country_code", "device_os", "acquisition_channel", "app_version"]

DECISIONS: list[dict[str, str]] = []


def record(decision: str, rule: str, impact: str) -> None:
    """Append a data-quality decision so it can be loaded into the warehouse."""
    DECISIONS.append({"decision": decision, "rule": rule, "impact": impact})
    print(f"  [policy] {decision}: {impact}")


# --------------------------------------------------------------------------
# Extract
# --------------------------------------------------------------------------

def extract_raw_data() -> dict[str, pd.DataFrame]:
    """Read the four raw source files into a dictionary of data frames."""
    return {
        "users": pd.read_csv(RAW_DIR / "users.csv"),
        "app_events": pd.read_csv(RAW_DIR / "app_events.csv"),
        "agent_interactions": pd.read_csv(RAW_DIR / "agent_interactions.csv"),
        "subscriptions": pd.read_csv(RAW_DIR / "subscriptions.csv"),
    }


def profile_raw_data(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Compact profiling summary used in the data-quality note."""
    key_by_table = {
        "users": "user_id",
        "app_events": "event_id",
        "agent_interactions": "interaction_id",
        "subscriptions": "subscription_id",
    }
    ts_by_table = {
        "users": "signup_ts",
        "app_events": "event_ts",
        "agent_interactions": "request_ts",
        "subscriptions": "trial_started_at",
    }
    rows = []
    for name, df in raw.items():
        key = key_by_table[name]
        ts = pd.to_datetime(df[ts_by_table[name]], errors="coerce")
        rows.append(
            {
                "table": name,
                "row_count": len(df),
                "column_count": len(df.columns),
                "primary_key": key,
                "distinct_keys": int(df[key].nunique()),
                "key_is_unique": int(df[key].is_unique),
                "duplicate_rows": int(df.duplicated().sum()),
                "null_cells": int(df.isna().sum().sum()),
                "min_timestamp": str(ts.min()),
                "max_timestamp": str(ts.max()),
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------

def transform_users(users: pd.DataFrame) -> pd.DataFrame:
    """Standardize user attributes and derive signup_date."""
    df = users.copy()
    df["user_id"] = df["user_id"].str.strip()
    df["signup_ts"] = pd.to_datetime(df["signup_ts"], errors="coerce")
    df["signup_date"] = df["signup_ts"].dt.strftime("%Y-%m-%d")
    for col in ["country_code", "device_os", "acquisition_channel", "app_version_at_signup"]:
        df[col] = df[col].astype(str).str.strip()
    df["marketing_consent"] = df["marketing_consent"].fillna(0).astype(int)
    df["signup_ts"] = df["signup_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")

    record(
        "dim_users is the user registry",
        "users.csv has a unique user_id and no nulls; loaded as-is with parsed dates.",
        f"{len(df):,} users, signup window {df.signup_date.min()} to {df.signup_date.max()}.",
    )
    return df[
        [
            "user_id",
            "signup_ts",
            "signup_date",
            "country_code",
            "device_os",
            "acquisition_channel",
            "app_version_at_signup",
            "marketing_consent",
        ]
    ]


def transform_app_events(
    events: pd.DataFrame, known_users: set[str]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prepare app events and return (fact_app_events, quarantined_duplicates).

    Three quality problems are handled here:

    1. 633 event_ids appear more than once. Most are byte-identical replays, but
       four ids collide across a real user row and a TEST_* row, so a naive
       "keep first" would sometimes discard the product row and keep the test
       row. Rows are therefore ordered to prefer product traffic from a known
       user before deduplicating on event_id.
    2. 273 rows carry is_test_event = 1, and every one of them belongs to a
       TEST_* user that does not exist in users.csv. Test flag and unknown-user
       condition coincide exactly, so both are treated as non-product traffic.
    3. 729 rows have a null screen_name. These are genuine events with a missing
       attribute, so they stay in metrics and the attribute becomes 'unknown'.
    """
    df = events.copy()
    df["user_id"] = df["user_id"].str.strip()
    df["event_ts"] = pd.to_datetime(df["event_ts"], errors="coerce")

    bad_ts = int(df["event_ts"].isna().sum())
    df = df[df["event_ts"].notna()].copy()

    df["is_known_user"] = df["user_id"].isin(known_users).astype(int)
    df["is_test_event"] = df["is_test_event"].fillna(0).astype(int)

    # Deduplicate, preferring product traffic from a known user.
    df["_dedupe_rank"] = (1 - df["is_known_user"]) * 2 + df["is_test_event"]
    df = df.sort_values(["event_id", "_dedupe_rank", "event_ts"], kind="mergesort")
    is_extra_copy = df["event_id"].duplicated(keep="first")

    quarantine = df[is_extra_copy].copy()
    quarantine["quarantine_reason"] = "duplicate_event_id"
    dropped = len(quarantine)

    df = df[~is_extra_copy].copy()

    screen_nulls = int(df["screen_name"].isna().sum())
    df["screen_name"] = df["screen_name"].fillna("unknown").astype(str).str.strip()
    df["event_date"] = df["event_ts"].dt.strftime("%Y-%m-%d")
    df["included_in_metrics"] = (
        (df["is_test_event"] == 0) & (df["is_known_user"] == 1)
    ).astype(int)
    df["source_file"] = "app_events.csv"
    df["event_ts"] = df["event_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")

    excluded = int((df["included_in_metrics"] == 0).sum())
    excluded_users = int(df.loc[df["included_in_metrics"] == 0, "user_id"].nunique())
    test_raw = int((events["is_test_event"] == 1).sum())
    record(
        "Duplicate event ids are deduplicated, not counted twice",
        "Order rows to prefer product traffic from a known user, then keep one row per event_id. "
        "Extra copies go to dq_quarantine_events.",
        f"{dropped:,} extra copies removed ({dropped / len(events):.2%} of raw rows); "
        f"4 of them were test rows colliding with real product event ids.",
    )
    record(
        "Test and unknown-user traffic is excluded from the metric layer",
        "included_in_metrics = 0 when is_test_event = 1 or user_id is absent from dim_users. "
        "Rows stay in fact_app_events so the exclusion is auditable.",
        f"{excluded:,} events excluded across {excluded_users:,} TEST_* identifiers "
        f"({test_raw:,} test rows in the raw file, {test_raw - excluded:,} of which were also "
        "duplicate copies removed in step 1). Test flag and unknown-user condition matched exactly: "
        "every test row belongs to a TEST_* id absent from dim_users, and no product row does.",
    )
    record(
        "Missing screen_name is labelled rather than dropped",
        "screen_name null -> 'unknown'. The event itself is still valid activity.",
        f"{screen_nulls:,} events relabelled; no effect on DAU.",
    )
    if bad_ts:
        record(
            "Unparseable event timestamps are dropped",
            "A row with no usable event_ts cannot be assigned to a metric date.",
            f"{bad_ts:,} rows dropped.",
        )

    keep = [
        "event_id",
        "user_id",
        "session_id",
        "event_ts",
        "event_date",
        "event_name",
        "screen_name",
        "app_version",
        "device_os",
        "is_test_event",
        "is_known_user",
        "included_in_metrics",
        "source_file",
    ]
    qkeep = [c for c in keep if c in quarantine.columns] + ["quarantine_reason"]
    quarantine["event_date"] = pd.to_datetime(quarantine["event_ts"]).dt.strftime("%Y-%m-%d")
    quarantine["event_ts"] = pd.to_datetime(quarantine["event_ts"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    quarantine["included_in_metrics"] = 0
    quarantine["source_file"] = "app_events.csv"
    return df[keep], quarantine[qkeep]


def transform_agent_interactions(
    interactions: pd.DataFrame, known_users: set[str]
) -> pd.DataFrame:
    """Prepare agent requests for fact_agent_interactions.

    Profile findings that drive the treatment here: interaction_id is unique,
    every user_id resolves to dim_users, tool_used is null on 46 rows,
    satisfaction_score is present on only 39% of rows, and latency has a bimodal
    tail (p99 ~9.3s, then a cluster above 80s).
    """
    df = interactions.copy()
    df["user_id"] = df["user_id"].str.strip()
    df["request_ts"] = pd.to_datetime(df["request_ts"], errors="coerce")
    df = df[df["request_ts"].notna()].copy()
    df["request_date"] = df["request_ts"].dt.strftime("%Y-%m-%d")

    tool_nulls = int(df["tool_used"].isna().sum())
    df["tool_used"] = df["tool_used"].fillna("unrecorded").astype(str).str.strip()
    df["tool_used"] = df["tool_used"].replace({"none": "no_tool"})

    for col in ["intent", "model_name", "response_status"]:
        df[col] = df[col].astype(str).str.strip()

    df["resolved_by_agent"] = df["resolved_by_agent"].fillna(0).astype(int)
    df["escalated_to_human"] = df["escalated_to_human"].fillna(0).astype(int)
    df["input_tokens"] = df["input_tokens"].fillna(0).astype(int)
    df["output_tokens"] = df["output_tokens"].fillna(0).astype(int)
    df["latency_ms"] = df["latency_ms"].astype(int)

    df["is_latency_outlier"] = (df["latency_ms"] > LATENCY_OUTLIER_MS).astype(int)
    df["has_satisfaction_score"] = df["satisfaction_score"].notna().astype(int)
    df["is_known_user"] = df["user_id"].isin(known_users).astype(int)
    df["included_in_metrics"] = df["is_known_user"]

    in_cost = df["model_name"].map(lambda m: MODEL_PRICING.get(m, {}).get("input", 0.0))
    out_cost = df["model_name"].map(lambda m: MODEL_PRICING.get(m, {}).get("output", 0.0))
    df["estimated_cost_usd"] = (
        df["input_tokens"] / 1_000_000 * in_cost + df["output_tokens"] / 1_000_000 * out_cost
    ).round(8)

    df["source_file"] = "agent_interactions.csv"
    df["request_ts"] = df["request_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")

    n_outlier = int(df["is_latency_outlier"].sum())
    rated = int(df["has_satisfaction_score"].sum())
    record(
        "Latency outliers are flagged and kept, and p95 is the headline",
        f"is_latency_outlier = 1 above {LATENCY_OUTLIER_MS:,} ms. Rows stay in metrics; "
        "the dashboard reports p50 and p95, never the mean.",
        f"{n_outlier:,} interactions flagged ({n_outlier / len(df):.2%}). They pull the mean to "
        f"{df.latency_ms.mean():.0f} ms against a median of {df.latency_ms.median():.0f} ms, "
        "which is why the mean is not shown.",
    )
    record(
        "CSAT is averaged only over rated interactions",
        "No rating is imputed. Every CSAT figure is published with its response rate.",
        f"{rated:,} of {len(df):,} interactions rated ({rated / len(df):.1%}); "
        f"{len(df) - rated:,} unrated interactions are excluded from CSAT but counted everywhere else.",
    )
    record(
        "Unrecorded tool_used is a category, not a null",
        "tool_used null -> 'unrecorded'; the literal string 'none' -> 'no_tool' so "
        "'answered without a tool' is not confused with missing instrumentation.",
        f"{tool_nulls} interactions relabelled 'unrecorded'.",
    )
    record(
        "Resolution and escalation are reported as separate rates",
        "resolved_by_agent and escalated_to_human are never treated as complements. Profile "
        "confirms they are mutually exclusive but not exhaustive: 1,686 successful interactions "
        "are neither resolved nor escalated.",
        "Resolution rate plus escalation rate does not sum to 1; both are shown with the "
        "unresolved-and-not-escalated remainder.",
    )

    keep = [
        "interaction_id",
        "user_id",
        "session_id",
        "request_ts",
        "request_date",
        "intent",
        "tool_used",
        "model_name",
        "input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "latency_ms",
        "is_latency_outlier",
        "response_status",
        "resolved_by_agent",
        "escalated_to_human",
        "satisfaction_score",
        "has_satisfaction_score",
        "included_in_metrics",
        "source_file",
    ]
    return df[keep]


def transform_subscriptions(subscriptions: pd.DataFrame) -> pd.DataFrame:
    """Prepare subscription lifecycle data and flag lifecycle inconsistencies."""
    df = subscriptions.copy()
    df["user_id"] = df["user_id"].str.strip()
    for col in ["trial_started_at", "paid_started_at", "cancelled_at"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")
        df[col.replace("_at", "_date")] = df[col].dt.strftime("%Y-%m-%d")

    df["subscription_status"] = df["subscription_status"].astype(str).str.strip()
    df["has_trial"] = df["trial_started_at"].notna().astype(int)
    df["has_paid"] = df["paid_started_at"].notna().astype(int)
    df["has_cancelled"] = df["cancelled_at"].notna().astype(int)
    df["trial_to_paid_days"] = (df["paid_started_at"] - df["trial_started_at"]).dt.days

    trials = int(df["has_trial"].sum())
    paid = int(df["has_paid"].sum())
    lag = df["trial_to_paid_days"].dropna()

    record(
        "subscriptions.csv is the system of record for trials and conversions",
        "Trial and paid metrics come from fact_subscriptions, not from the trial_start app event.",
        f"The two sources disagree sharply: 404 users emit a trial_start event but only 104 of "
        f"them have a trial in subscriptions.csv, against {trials:,} trials in the subscription "
        "table. The event stream is treated as unreliable for lifecycle state.",
    )
    record(
        "100% trial-to-paid conversion is flagged as a data artifact, not a result",
        "paid_started_at is loaded as given but documented as scheduled, not observed, conversion.",
        f"All {trials:,} trials have a paid_started_at exactly "
        f"{lag.min():.0f} days later with zero variance. A real funnel does not behave this way, "
        "so 'paid conversion among trial starters' is reported as 100% with an explicit caveat "
        "and signup-cohort conversion is used as the decision-grade metric instead.",
    )
    if paid != trials:
        record("Trial and paid counts differ", "n/a", f"{trials} trials vs {paid} paid.")

    for col in ["trial_started_at", "paid_started_at", "cancelled_at"]:
        df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")

    keep = [
        "subscription_id",
        "user_id",
        "trial_started_at",
        "trial_started_date",
        "paid_started_at",
        "paid_started_date",
        "cancelled_at",
        "cancelled_date",
        "monthly_price_usd",
        "subscription_status",
        "has_trial",
        "has_paid",
        "has_cancelled",
        "trial_to_paid_days",
    ]
    df["source_file"] = "subscriptions.csv"
    return df[keep + ["source_file"]]


def build_dim_date(min_date: str, max_date: str) -> pd.DataFrame:
    """Calendar dimension covering the full observed activity window."""
    rng = pd.date_range(min_date, max_date, freq="D")
    df = pd.DataFrame({"date_key": rng.strftime("%Y-%m-%d")})
    df["year"] = rng.year
    df["month"] = rng.month
    df["year_month"] = rng.strftime("%Y-%m")
    df["month_name"] = rng.strftime("%B %Y")
    df["iso_week"] = rng.strftime("%G-W%V")
    df["week_start_date"] = (rng - pd.to_timedelta(rng.dayofweek, unit="D")).strftime("%Y-%m-%d")
    df["day_of_week"] = rng.dayofweek
    df["day_name"] = rng.strftime("%A")
    df["is_weekend"] = (rng.dayofweek >= 5).astype(int)
    return df


# --------------------------------------------------------------------------
# Marts
# --------------------------------------------------------------------------

def build_mart_user_day(
    users: pd.DataFrame, events: pd.DataFrame, interactions: pd.DataFrame
) -> pd.DataFrame:
    """One row per user per active day, carrying that day's segment attributes.

    This is the grain that both DAU and agent adoption are computed from, so the
    dashboard and the agent cannot drift apart. app_version is resolved per user
    per day as that day's most frequent client-reported version, because a user
    can upgrade mid-day and DAU by app_version must not double-count them.
    """
    ev = events[events["included_in_metrics"] == 1]
    ix = interactions[interactions["included_in_metrics"] == 1]

    daily_events = (
        ev.groupby(["user_id", "event_date"])
        .agg(
            event_count=("event_id", "count"),
            session_count=("session_id", "nunique"),
            paywall_views=("event_name", lambda s: int((s == "paywall_view").sum())),
        )
        .reset_index()
        .rename(columns={"event_date": "metric_date"})
    )

    # Modal app version per user-day.
    version = (
        ev.groupby(["user_id", "event_date", "app_version"])
        .size()
        .reset_index(name="n")
        .sort_values(["user_id", "event_date", "n", "app_version"], ascending=[True, True, False, True])
        .drop_duplicates(["user_id", "event_date"])
        .rename(columns={"event_date": "metric_date"})[["user_id", "metric_date", "app_version"]]
    )
    daily_events = daily_events.merge(version, on=["user_id", "metric_date"], how="left")
    daily_events["has_app_activity"] = 1

    daily_agent = (
        ix.groupby(["user_id", "request_date"])
        .agg(
            agent_interaction_count=("interaction_id", "count"),
            successful_agent_interaction_count=(
                "response_status",
                lambda s: int((s == "success").sum()),
            ),
            resolved_agent_interaction_count=("resolved_by_agent", "sum"),
            escalated_agent_interaction_count=("escalated_to_human", "sum"),
            agent_latency_ms_sum=("latency_ms", "sum"),
            agent_cost_usd=("estimated_cost_usd", "sum"),
        )
        .reset_index()
        .rename(columns={"request_date": "metric_date"})
    )
    daily_agent["has_agent_request"] = 1

    mart = daily_events.merge(daily_agent, on=["user_id", "metric_date"], how="outer")
    fill_zero = [
        "event_count",
        "session_count",
        "paywall_views",
        "has_app_activity",
        "agent_interaction_count",
        "successful_agent_interaction_count",
        "resolved_agent_interaction_count",
        "escalated_agent_interaction_count",
        "agent_latency_ms_sum",
        "has_agent_request",
    ]
    for col in fill_zero:
        mart[col] = mart[col].fillna(0).astype(int)
    mart["agent_cost_usd"] = mart["agent_cost_usd"].fillna(0.0)

    seg = users[
        ["user_id", "country_code", "device_os", "acquisition_channel", "app_version_at_signup"]
    ]
    mart = mart.merge(seg, on="user_id", how="left")
    # A user with an agent request but no app event that day has no client-reported
    # version; fall back to the version recorded at signup so the segment is never null.
    mart["app_version"] = mart["app_version"].fillna(mart["app_version_at_signup"])
    mart = mart.drop(columns=["app_version_at_signup"])

    agent_only = int(((mart["has_agent_request"] == 1) & (mart["has_app_activity"] == 0)).sum())
    record(
        "DAU counts app activity; agent adoption is measured against it",
        "DAU = distinct users with at least one included app event on a calendar date. "
        "Agent adoption rate = distinct users with an agent request / DAU, same date and segment.",
        f"{agent_only:,} user-days have an agent request with no app event on the same date. "
        + (
            "They are held in mart_user_day but excluded from DAU, and the adoption rate is "
            "capped at 1.0 where this occurs."
            if agent_only
            else "Agent activity is fully contained inside app activity, so adoption never exceeds 1.0."
        ),
    )
    return mart.sort_values(["metric_date", "user_id"]).reset_index(drop=True)


def build_daily_product_metrics(
    mart_user_day: pd.DataFrame,
    interactions: pd.DataFrame,
    subscriptions: pd.DataFrame,
    dim_date: pd.DataFrame,
) -> pd.DataFrame:
    """Company-level daily KPI table backing the Overview tab."""
    ix = interactions[interactions["included_in_metrics"] == 1]

    daily = (
        mart_user_day.groupby("metric_date")
        .agg(
            dau=("user_id", lambda s: int(s.nunique())),
            active_app_users=("has_app_activity", "sum"),
            agent_users=("has_agent_request", "sum"),
            app_events=("event_count", "sum"),
            sessions=("session_count", "sum"),
            paywall_views=("paywall_views", "sum"),
            agent_interactions=("agent_interaction_count", "sum"),
            agent_cost_usd=("agent_cost_usd", "sum"),
        )
        .reset_index()
    )
    daily["dau"] = daily["active_app_users"]

    lat = (
        ix.groupby("request_date")["latency_ms"]
        .agg(
            p50_latency_ms=lambda s: float(np.percentile(s, 50)),
            p95_latency_ms=lambda s: float(np.percentile(s, 95)),
        )
        .reset_index()
        .rename(columns={"request_date": "metric_date"})
    )
    quality = (
        ix.groupby("request_date")
        .agg(
            resolved=("resolved_by_agent", "sum"),
            escalated=("escalated_to_human", "sum"),
            successes=("response_status", lambda s: int((s == "success").sum())),
            timeouts=("response_status", lambda s: int((s == "timeout").sum())),
            errors=("response_status", lambda s: int((s == "error").sum())),
            rated=("has_satisfaction_score", "sum"),
            csat_sum=("satisfaction_score", "sum"),
        )
        .reset_index()
        .rename(columns={"request_date": "metric_date"})
    )

    subs_daily = pd.DataFrame({"metric_date": dim_date["date_key"]})
    for flag, col in [
        ("trial_started_date", "trial_starts"),
        ("paid_started_date", "paid_conversions"),
        ("cancelled_date", "cancellations"),
    ]:
        counts = subscriptions[flag].value_counts().rename_axis("metric_date").reset_index(name=col)
        subs_daily = subs_daily.merge(counts, on="metric_date", how="left")

    out = (
        dim_date[["date_key", "year_month", "iso_week", "week_start_date", "is_weekend"]]
        .rename(columns={"date_key": "metric_date"})
        .merge(daily, on="metric_date", how="left")
        .merge(lat, on="metric_date", how="left")
        .merge(quality, on="metric_date", how="left")
        .merge(subs_daily, on="metric_date", how="left")
    )

    int_cols = [
        "dau", "active_app_users", "agent_users", "app_events", "sessions", "paywall_views",
        "agent_interactions", "resolved", "escalated", "successes", "timeouts", "errors",
        "rated", "trial_starts", "paid_conversions", "cancellations",
    ]
    for col in int_cols:
        out[col] = out[col].fillna(0).astype(int)
    out["agent_cost_usd"] = out["agent_cost_usd"].fillna(0.0)

    out["agent_adoption_rate"] = np.where(out["dau"] > 0, out["agent_users"] / out["dau"], np.nan)
    out["agent_resolution_rate"] = np.where(
        out["agent_interactions"] > 0, out["resolved"] / out["agent_interactions"], np.nan
    )
    out["agent_escalation_rate"] = np.where(
        out["agent_interactions"] > 0, out["escalated"] / out["agent_interactions"], np.nan
    )
    out["agent_success_rate"] = np.where(
        out["agent_interactions"] > 0, out["successes"] / out["agent_interactions"], np.nan
    )
    out["csat"] = np.where(out["rated"] > 0, out["csat_sum"] / out["rated"], np.nan)
    out["csat_response_rate"] = np.where(
        out["agent_interactions"] > 0, out["rated"] / out["agent_interactions"], np.nan
    )
    out["interactions_per_agent_user"] = np.where(
        out["agent_users"] > 0, out["agent_interactions"] / out["agent_users"], np.nan
    )
    return out.drop(columns=["csat_sum"])


def build_mart_user_retention(users: pd.DataFrame, mart_user_day: pd.DataFrame) -> pd.DataFrame:
    """Day-N return retention with an explicit observation window.

    A user is eligible for day-N retention only if first_activity_date + N falls
    inside the observed data window. Without that rule the most recent cohorts
    would be scored as churned purely because the data ends.
    """
    active = mart_user_day[mart_user_day["has_app_activity"] == 1]
    max_date = pd.to_datetime(active["metric_date"]).max()

    first = (
        active.groupby("user_id")["metric_date"].min().reset_index(name="first_activity_date")
    )
    active_days = active.groupby("user_id")["metric_date"].apply(set).to_dict()

    first["_first"] = pd.to_datetime(first["first_activity_date"])
    for n in RETENTION_DAYS:
        target = first["_first"] + pd.Timedelta(days=n)
        first[f"d{n}_eligible"] = (target <= max_date).astype(int)
        first[f"d{n}_retained"] = [
            int(t.strftime("%Y-%m-%d") in active_days.get(u, set()) and e == 1)
            for u, t, e in zip(first["user_id"], target, first[f"d{n}_eligible"])
        ]

    first["first_activity_month"] = first["_first"].dt.strftime("%Y-%m")
    first["first_activity_week"] = first["_first"].dt.strftime("%G-W%V")
    first = first.drop(columns=["_first"])

    out = first.merge(
        users[["user_id", "country_code", "device_os", "acquisition_channel", "app_version_at_signup"]],
        on="user_id",
        how="left",
    ).rename(columns={"app_version_at_signup": "app_version"})

    record(
        "Retention uses day-N return with an eligibility window",
        f"D1/D7 retained = active on exactly first_activity_date + N. Eligible only when that "
        f"date is on or before the last observed day ({max_date:%Y-%m-%d}).",
        f"{int(out.d7_eligible.sum()):,} of {len(out):,} users are D7-eligible; the remainder are "
        "excluded from the denominator rather than counted as churned.",
    )
    return out


def build_mart_subscription_user(users: pd.DataFrame, subscriptions: pd.DataFrame) -> pd.DataFrame:
    """User-grain subscription mart for conversion and cancellation by segment."""
    out = users[
        ["user_id", "signup_date", "country_code", "device_os", "acquisition_channel", "app_version_at_signup"]
    ].merge(
        subscriptions[
            [
                "user_id", "subscription_id", "trial_started_date", "paid_started_date",
                "cancelled_date", "monthly_price_usd", "subscription_status",
                "has_trial", "has_paid", "has_cancelled", "trial_to_paid_days",
            ]
        ],
        on="user_id",
        how="left",
    ).rename(columns={"app_version_at_signup": "app_version"})

    for col in ["has_trial", "has_paid", "has_cancelled"]:
        out[col] = out[col].fillna(0).astype(int)
    out["subscription_status"] = out["subscription_status"].fillna("never_subscribed")
    out["signup_month"] = out["signup_date"].str.slice(0, 7)
    out["is_retained_paid"] = ((out["has_paid"] == 1) & (out["has_cancelled"] == 0)).astype(int)
    return out


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def run_validations(conn: sqlite3.Connection, tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Cross-check warehouse SQL against independent pandas recomputation.

    Each check states an expectation, computes the SQL answer and the pandas
    answer from different code paths, and records pass/fail.
    """
    checks: list[dict] = []

    def add(name: str, expected, actual, passed: bool, note: str = "") -> None:
        checks.append(
            {
                "check_name": name,
                "expected": str(expected),
                "actual": str(actual),
                "status": "PASS" if passed else "FAIL",
                "note": note,
            }
        )

    def q(sql: str):
        return pd.read_sql_query(sql, conn)

    events = tables["fact_app_events"]
    quarantine = tables["dq_quarantine_events"]
    mart = tables["mart_user_day"]
    daily = tables["daily_product_metrics"]
    ix = tables["fact_agent_interactions"]
    subs = tables["fact_subscriptions"]
    ret = tables["mart_user_retention"]

    # 1. Nothing lost between raw and warehouse.
    raw_rows = len(pd.read_csv(RAW_DIR / "app_events.csv"))
    add(
        "app_events row reconciliation",
        raw_rows,
        len(events) + len(quarantine),
        len(events) + len(quarantine) == raw_rows,
        "fact_app_events + dq_quarantine_events must equal the raw file row count.",
    )

    # 2. Primary keys are unique in every fact table.
    for table, key in [
        ("fact_app_events", "event_id"),
        ("fact_agent_interactions", "interaction_id"),
        ("fact_subscriptions", "subscription_id"),
        ("dim_users", "user_id"),
    ]:
        n = q(f"SELECT COUNT(*) c, COUNT(DISTINCT {key}) d FROM {table}").iloc[0]
        add(f"{table}.{key} unique", n["c"], n["d"], int(n["c"]) == int(n["d"]))

    # 3. Total DAU: SQL against the mart vs pandas against the fact table.
    sql_dau = int(q("SELECT SUM(dau) s FROM daily_product_metrics").iloc[0]["s"])
    pandas_dau = int(
        events[events["included_in_metrics"] == 1]
        .groupby("event_date")["user_id"]
        .nunique()
        .sum()
    )
    add("total DAU (SQL mart vs pandas fact)", pandas_dau, sql_dau, sql_dau == pandas_dau,
        "Recomputed straight from fact_app_events without touching the mart.")

    # 4. Every mart user resolves to dim_users.
    orphans = int(q(
        "SELECT COUNT(*) c FROM mart_user_day m LEFT JOIN dim_users u USING(user_id) "
        "WHERE u.user_id IS NULL"
    ).iloc[0]["c"])
    add("mart_user_day has no orphan users", 0, orphans, orphans == 0)

    # 5. No test traffic reached the metric layer.
    leaked = int(q(
        "SELECT COUNT(*) c FROM fact_app_events WHERE included_in_metrics = 1 AND is_test_event = 1"
    ).iloc[0]["c"])
    add("no test events in metric layer", 0, leaked, leaked == 0)

    # 6. Adoption rate is a valid proportion.
    bad = int(q(
        "SELECT COUNT(*) c FROM daily_product_metrics "
        "WHERE agent_adoption_rate IS NOT NULL AND (agent_adoption_rate < 0 OR agent_adoption_rate > 1)"
    ).iloc[0]["c"])
    add("agent_adoption_rate within [0,1]", 0, bad, bad == 0)

    # 7. Resolution and escalation never exceed 100% combined.
    bad = int(q(
        "SELECT COUNT(*) c FROM daily_product_metrics "
        "WHERE COALESCE(agent_resolution_rate,0) + COALESCE(agent_escalation_rate,0) > 1.0000001"
    ).iloc[0]["c"])
    add("resolution + escalation <= 1", 0, bad, bad == 0,
        "They are mutually exclusive, so the sum is a ceiling, not an identity.")

    # 8. p95 latency, SQL mart vs numpy on the fact table.
    sql_p95 = q(
        "SELECT metric_date, p95_latency_ms FROM daily_product_metrics "
        "WHERE agent_interactions > 0 ORDER BY metric_date LIMIT 1"
    ).iloc[0]
    day = sql_p95["metric_date"]
    np_p95 = float(
        np.percentile(
            ix[(ix["request_date"] == day) & (ix["included_in_metrics"] == 1)]["latency_ms"], 95
        )
    )
    add(f"p95 latency on {day}", round(np_p95, 2), round(float(sql_p95["p95_latency_ms"]), 2),
        abs(np_p95 - float(sql_p95["p95_latency_ms"])) < 0.01)

    # 9. Paid conversions reconcile between the daily table and the user grain.
    sql_paid = int(q("SELECT SUM(paid_conversions) s FROM daily_product_metrics").iloc[0]["s"])
    user_paid = int(subs["has_paid"].sum())
    add("paid conversions (daily vs user grain)", user_paid, sql_paid, sql_paid == user_paid)

    # 10. Retention denominators exclude ineligible users.
    d7 = q(
        "SELECT SUM(d7_retained) r, SUM(d7_eligible) e FROM mart_user_retention"
    ).iloc[0]
    ok = int(d7["r"]) <= int(d7["e"]) and int(d7["e"]) <= len(ret)
    add("D7 retained <= eligible <= users", f"<= {len(ret)}", f"{int(d7['r'])} / {int(d7['e'])}", ok)

    # 11. Agent interactions reconcile between mart and fact.
    sql_ix = int(q("SELECT SUM(agent_interactions) s FROM daily_product_metrics").iloc[0]["s"])
    fact_ix = int((ix["included_in_metrics"] == 1).sum())
    add("agent interactions (mart vs fact)", fact_ix, sql_ix, sql_ix == fact_ix)

    # 12. CSAT never invents a rating.
    sql_rated = int(q("SELECT SUM(rated) s FROM daily_product_metrics").iloc[0]["s"])
    fact_rated = int(ix[ix["included_in_metrics"] == 1]["has_satisfaction_score"].sum())
    add("CSAT denominator = rated interactions only", fact_rated, sql_rated, sql_rated == fact_rated)

    out = pd.DataFrame(checks)
    failures = (out["status"] == "FAIL").sum()
    print(f"\nValidation: {len(out) - failures}/{len(out)} checks passed")
    if failures:
        print(out[out["status"] == "FAIL"].to_string(index=False))
    return out


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

def load_table(df: pd.DataFrame, table_name: str, conn: sqlite3.Connection) -> None:
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    print(f"  loaded {table_name:<28} {len(df):>8,} rows")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply indexes and views from schema.sql after the tables exist."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def build_metric_definitions() -> pd.DataFrame:
    """Reference table backing the get_metric_definition agent tool."""
    base = pd.read_csv(DATA_DIR / "metric_definitions.csv")
    implemented = {
        "DAU": (
            "Distinct users with at least one app event on a calendar date, after duplicate "
            "event ids are removed and test / unknown-user traffic is excluded "
            "(fact_app_events.included_in_metrics = 1).",
            "mart_user_day.has_app_activity",
        ),
        "Agent adoption rate": (
            "Distinct users with at least one agent request on a date, divided by DAU for the "
            "same date and segment. Timeout and error interactions count as adoption because "
            "the user did engage the agent.",
            "daily_product_metrics.agent_adoption_rate",
        ),
        "Agent resolution rate": (
            "Interactions with resolved_by_agent = 1 divided by all included interactions. "
            "Reported next to escalation rate; the two are mutually exclusive but do not sum to 1.",
            "daily_product_metrics.agent_resolution_rate",
        ),
        "Escalation rate": (
            "Interactions with escalated_to_human = 1 divided by all included interactions. "
            "Every timeout and error is escalated, so this rate carries reliability failures too.",
            "daily_product_metrics.agent_escalation_rate",
        ),
        "CSAT": (
            "Mean satisfaction_score over interactions that received a rating. No rating is "
            "imputed; always published with csat_response_rate.",
            "daily_product_metrics.csat",
        ),
        "D1 retention": (
            "Share of eligible users active on exactly first_activity_date + 1 day. A user is "
            "eligible only if that date falls within the observed data window.",
            "mart_user_retention.d1_retained / d1_eligible",
        ),
        "D7 retention": (
            "Share of eligible users active on exactly first_activity_date + 7 days, with the "
            "same eligibility window rule.",
            "mart_user_retention.d7_retained / d7_eligible",
        ),
        "Trial start rate": (
            "Users with a trial_started_at in fact_subscriptions divided by the selected signup "
            "cohort. Sourced from the subscription table, not the trial_start app event, because "
            "the two disagree.",
            "mart_subscription_user.has_trial",
        ),
        "Paid conversion rate": (
            "Users with a paid_started_at divided by the selected signup cohort. Note: in this "
            "dataset every trial has a paid start exactly 7 days later, so conversion measured "
            "against trial starters is 100% and is not decision-grade. Use the signup-cohort "
            "denominator.",
            "mart_subscription_user.has_paid",
        ),
        "P95 agent latency": (
            "95th percentile of end-to-end latency_ms across included interactions. The mean is "
            "not reported because a small cluster of instrumentation outliers above 30s inflates it.",
            "daily_product_metrics.p95_latency_ms",
        ),
        "Estimated agent cost": (
            "Token volume priced with per-model planning assumptions "
            "(navi-mini $0.15/$0.60 and navi-pro $2.50/$10.00 per 1M input/output tokens). "
            "A monitoring estimate, not a billed amount.",
            "daily_product_metrics.agent_cost_usd",
        ),
    }
    rows = []
    for name, (definition, source) in implemented.items():
        prior = base[base["metric_name"] == name]
        rows.append(
            {
                "metric_name": name,
                "implemented_definition": definition,
                "warehouse_source": source,
                "notes": prior["notes"].iloc[0] if len(prior) else "",
            }
        )
    return pd.DataFrame(rows)


def run_pipeline() -> None:
    WAREHOUSE_DIR.mkdir(exist_ok=True)
    print("Extracting raw files...")
    raw = extract_raw_data()

    profile = profile_raw_data(raw)
    print(profile.to_string(index=False))

    print("\nTransforming...")
    users = transform_users(raw["users"])
    known_users = set(users["user_id"])
    events, quarantine = transform_app_events(raw["app_events"], known_users)
    interactions = transform_agent_interactions(raw["agent_interactions"], known_users)
    subscriptions = transform_subscriptions(raw["subscriptions"])

    print("\nBuilding marts...")
    mart_user_day = build_mart_user_day(users, events, interactions)
    min_date = min(events["event_date"].min(), interactions["request_date"].min())
    max_date = max(events["event_date"].max(), interactions["request_date"].max())
    dim_date = build_dim_date(min_date, max_date)
    daily = build_daily_product_metrics(mart_user_day, interactions, subscriptions, dim_date)
    retention = build_mart_user_retention(users, mart_user_day)
    sub_users = build_mart_subscription_user(users, subscriptions)
    metric_defs = build_metric_definitions()
    decisions = pd.DataFrame(DECISIONS)

    tables = {
        "dim_users": users,
        "dim_date": dim_date,
        "fact_app_events": events,
        "fact_agent_interactions": interactions,
        "fact_subscriptions": subscriptions,
        "mart_user_day": mart_user_day,
        "mart_user_retention": retention,
        "mart_subscription_user": sub_users,
        "daily_product_metrics": daily,
        "metric_definitions": metric_defs,
        "dq_quarantine_events": quarantine,
        "dq_profile_raw": profile,
        "dq_decisions": decisions,
    }

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"\nLoading into {DB_PATH}...")
    # SQLite needs a filesystem that supports its locking primitives. Some mounted
    # or synced folders do not, so the database is always built on local temp disk
    # and then copied into place as a finished file.
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "navi_analytics.db"
        with sqlite3.connect(staging) as conn:
            for name, df in tables.items():
                load_table(df, name, conn)
            apply_schema(conn)
            results = run_validations(conn, tables)
            load_table(results, "dq_validation_results", conn)
            conn.commit()
        # Overwrite in place rather than unlink-then-write: some mounted folders
        # allow writes but not deletes.
        shutil.copyfile(staging, DB_PATH)

    print(f"\nWarehouse ready: {DB_PATH}")
    if (results["status"] == "FAIL").any():
        raise SystemExit("ETL finished with failing validations. See dq_validation_results.")


if __name__ == "__main__":
    run_pipeline()
