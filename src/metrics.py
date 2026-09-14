"""Shared metric queries for the Navi warehouse.

Both surfaces read metrics through this module:

    app.py            renders these results as charts and tables
    agent_tools.py    exposes the same marts to the LLM as validated tools

Keeping the SQL in one place is what makes the dashboard and the Analytics Agent
agree by construction rather than by coincidence. tests/test_dashboard_agreement.py
asserts that the two paths return identical numbers for the same filter state.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("NAVI_DB_PATH", PROJECT_ROOT / "warehouse" / "navi_analytics.db"))

SEGMENT_LABELS = {
    "country_code": "Country",
    "device_os": "Device OS",
    "acquisition_channel": "Acquisition channel",
    "app_version": "App version",
}


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open the warehouse read-only."""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"Warehouse not found at {path}. Run: python src/etl_pipeline.py")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


@dataclass
class Filters:
    """The dashboard's filter state - the same object the agent receives as context."""

    start: str
    end: str
    segments: dict[str, str] = field(default_factory=dict)

    def where(self, prefix: str = "", date_col: str = "metric_date") -> tuple[str, list]:
        clauses = [f"{prefix}{date_col} BETWEEN ? AND ?"]
        params: list = [self.start, self.end]
        for dim, value in self.segments.items():
            if dim not in SEGMENT_LABELS:
                raise ValueError(f"Unsupported segment dimension: {dim}")
            clauses.append(f"{prefix}{dim} = ?")
            params.append(value)
        return " AND ".join(clauses), params

    def describe(self) -> str:
        segs = ", ".join(f"{SEGMENT_LABELS[d]} = {v}" for d, v in self.segments.items())
        return segs or "no segment filters"


def _q(conn, sql: str, params: list) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=tuple(params))


def coverage(conn) -> dict[str, str]:
    row = _q(conn, "SELECT MIN(metric_date) lo, MAX(metric_date) hi "
                   "FROM daily_product_metrics WHERE dau > 0", []).iloc[0]
    return {"lo": row["lo"], "hi": row["hi"]}


def dimension_values(conn, dim: str) -> list[str]:
    if dim not in SEGMENT_LABELS:
        raise ValueError(f"Unsupported segment dimension: {dim}")
    return _q(conn, f"SELECT DISTINCT {dim} v FROM mart_user_day "
                    f"WHERE {dim} IS NOT NULL ORDER BY 1", [])["v"].tolist()


def daily_series(conn, f: Filters) -> pd.DataFrame:
    """DAU, agent users and agent interactions per calendar day."""
    clause, params = f.where("m.")
    df = _q(conn, f"""
        SELECT m.metric_date,
               COUNT(DISTINCT m.user_id) AS dau,
               COUNT(DISTINCT CASE WHEN m.has_agent_request=1 THEN m.user_id END) AS agent_users,
               SUM(m.agent_interaction_count) AS agent_interactions
        FROM mart_user_day m
        WHERE m.has_app_activity = 1 AND {clause}
        GROUP BY m.metric_date ORDER BY m.metric_date
    """, params)
    if df.empty:
        return df.assign(agent_adoption_rate=None)
    return df.assign(agent_adoption_rate=lambda d: (d.agent_users / d.dau).where(d.dau > 0))


def segment_dau(conn, f: Filters, dim: str) -> pd.DataFrame:
    """Average / peak DAU and agent adoption for each value of one dimension."""
    if dim not in SEGMENT_LABELS:
        raise ValueError(f"Unsupported segment dimension: {dim}")
    clause, params = f.where("m.")
    return _q(conn, f"""
        WITH d AS (
            SELECT m.{dim} seg, m.metric_date,
                   COUNT(DISTINCT m.user_id) dau,
                   COUNT(DISTINCT CASE WHEN m.has_agent_request=1 THEN m.user_id END) au
            FROM mart_user_day m WHERE m.has_app_activity=1 AND {clause}
            GROUP BY seg, m.metric_date
        )
        SELECT seg, ROUND(AVG(dau),1) avg_dau, MAX(dau) peak_dau,
               CAST(SUM(au) AS REAL)/NULLIF(SUM(dau),0) adoption
        FROM d GROUP BY seg ORDER BY avg_dau DESC
    """, params)


def agent_rollup(conn, f: Filters, group_by: str | None = None) -> pd.DataFrame:
    """Agent volume, reliability, resolution, CSAT and cost, optionally by dimension."""
    allowed = list(SEGMENT_LABELS) + ["intent", "tool_used", "model_name", "response_status"]
    if group_by is not None and group_by not in allowed:
        raise ValueError(f"Unsupported group_by: {group_by}")
    clause, params = f.where("", "request_date")
    seg = group_by or "'all'"
    return _q(conn, f"""
        SELECT {seg} AS seg, COUNT(*) interactions, COUNT(DISTINCT user_id) users,
               SUM(response_status='success') successes,
               SUM(response_status='timeout') timeouts,
               SUM(response_status='error') errors,
               SUM(resolved_by_agent) resolved, SUM(escalated_to_human) escalated,
               SUM(has_satisfaction_score) rated, AVG(satisfaction_score) csat,
               SUM(estimated_cost_usd) cost_usd, SUM(is_latency_outlier) outliers
        FROM v_agent_interactions_enriched WHERE {clause}
        GROUP BY seg ORDER BY interactions DESC
    """, params)


def latency_values(conn, f: Filters) -> pd.Series:
    """Raw latency values under the current filters, for percentile calculation."""
    clause, params = f.where("", "request_date")
    return _q(conn, f"SELECT latency_ms FROM v_agent_interactions_enriched WHERE {clause}",
              params)["latency_ms"]


def latency_percentiles(conn, f: Filters, bucket: str = "request_date") -> pd.DataFrame:
    """p50 / p95 latency per period.

    Percentiles are computed in pandas with linear interpolation so they match the
    ETL's numpy calculation and the agent tool's percentile helper exactly. SQLite
    has no percentile function, and an approximation here would put three slightly
    different p95 values in front of the same stakeholder.
    """
    if bucket not in ("request_date", "substr(request_date,1,7)"):
        raise ValueError(f"Unsupported latency bucket: {bucket}")
    clause, params = f.where("", "request_date")
    raw = _q(conn, f"SELECT {bucket} AS period, latency_ms "
                   f"FROM v_agent_interactions_enriched WHERE {clause}", params)
    if raw.empty:
        return pd.DataFrame(columns=["period", "p50", "p95", "n"])
    return (raw.groupby("period")["latency_ms"]
            .agg(p50=lambda s: s.quantile(.50), p95=lambda s: s.quantile(.95), n="count")
            .reset_index())


def status_mix(conn, f: Filters) -> pd.DataFrame:
    clause, params = f.where("", "request_date")
    return _q(conn, f"""
        SELECT substr(request_date,1,7) period,
               SUM(response_status='success') success,
               SUM(response_status='timeout') timeout,
               SUM(response_status='error') error
        FROM v_agent_interactions_enriched WHERE {clause}
        GROUP BY period ORDER BY period
    """, params)


def latency_raw(conn, f: Filters) -> pd.DataFrame:
    clause, params = f.where("", "request_date")
    return _q(conn, f"""SELECT latency_ms, is_latency_outlier
                        FROM v_agent_interactions_enriched WHERE {clause}""", params)


def subs_rollup(conn, f: Filters, group_by: str | None = None) -> pd.DataFrame:
    """Signup-cohort trial, conversion and cancellation metrics."""
    if group_by is not None and group_by not in SEGMENT_LABELS:
        raise ValueError(f"Unsupported group_by: {group_by}")
    clause, params = f.where("", "signup_date")
    seg = group_by or "'all'"
    return _q(conn, f"""
        SELECT {seg} AS seg, COUNT(*) cohort, SUM(has_trial) trials, SUM(has_paid) paid,
               SUM(has_cancelled) cancelled, SUM(is_retained_paid) still_paying,
               CAST(SUM(has_trial) AS REAL)/NULLIF(COUNT(*),0) trial_rate,
               CAST(SUM(has_paid) AS REAL)/NULLIF(COUNT(*),0) paid_rate,
               CAST(SUM(has_cancelled) AS REAL)/NULLIF(SUM(has_paid),0) cancel_rate,
               SUM(CASE WHEN has_paid=1 AND has_cancelled=0 THEN monthly_price_usd ELSE 0 END) mrr
        FROM mart_subscription_user WHERE {clause}
        GROUP BY seg ORDER BY paid_rate DESC
    """, params)


def retention_rollup(conn, f: Filters, group_by: str | None = None) -> pd.DataFrame:
    """D1 / D7 return retention with eligibility-aware denominators."""
    if group_by is not None and group_by not in SEGMENT_LABELS:
        raise ValueError(f"Unsupported group_by: {group_by}")
    clause, params = f.where("", "first_activity_date")
    seg = group_by or "'all'"
    return _q(conn, f"""
        SELECT {seg} AS seg, COUNT(*) users,
               SUM(d1_eligible) d1_elig, SUM(d1_retained) d1_ret,
               SUM(d7_eligible) d7_elig, SUM(d7_retained) d7_ret,
               CAST(SUM(d1_retained) AS REAL)/NULLIF(SUM(d1_eligible),0) d1_rate,
               CAST(SUM(d7_retained) AS REAL)/NULLIF(SUM(d7_eligible),0) d7_rate
        FROM mart_user_retention WHERE {clause}
        GROUP BY seg ORDER BY d7_rate DESC
    """, params)


def dq_summary(conn) -> pd.Series:
    return _q(conn, "SELECT * FROM v_dq_summary", []).iloc[0]
