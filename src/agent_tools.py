"""Read-only data tools for the Navi Analytics Agent.

Security model
--------------
The LLM never sees the database and never writes SQL. It sees only the JSON
schemas in TOOLS and may request one of them with arguments. Every call then
passes through this module, which:

  * accepts only allow-listed tool names (dispatch_tool);
  * accepts only allow-listed grouping dimensions and filter keys;
  * validates every date against a strict format and a sane range;
  * binds every value as a SQL parameter, never string-formats it into SQL;
  * opens SQLite in read-only mode (file:...?mode=ro) so a write is impossible
    even if a query were somehow malformed;
  * returns aggregates only, with a row cap, so no user-level record escapes.

Column names that reach SQL (the GROUP BY dimension) are never taken from the
model's string directly; they are resolved through VALID_GROUPS, so a value
outside that list raises before any SQL is built.
"""
from __future__ import annotations

import calendar
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(
    os.environ.get("NAVI_DB_PATH", PROJECT_ROOT / "warehouse" / "navi_analytics.db")
)

# Dimensions the agent may group by or filter on. Any other value is rejected.
VALID_GROUPS = ["country_code", "device_os", "acquisition_channel", "app_version"]

# Hard ceiling on returned rows. Aggregates over these dimensions are small by
# construction; the cap is a backstop against an unexpectedly wide grouping.
MAX_ROWS = 200

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIN_DATE = date(2020, 1, 1)
MAX_DATE = date(2035, 12, 31)


# --------------------------------------------------------------------------
# Tool schemas advertised to the model
# --------------------------------------------------------------------------

def _segment_properties() -> dict[str, Any]:
    return {
        dim: {
            "type": ["string", "null"],
            "description": f"Optional exact-match filter on {dim}. Null means no filter.",
        }
        for dim in VALID_GROUPS
    }


def _base_params(extra: dict[str, Any] | None = None, group_required: bool = True) -> dict[str, Any]:
    props: dict[str, Any] = {
        "start_date": {"type": "string", "description": "Inclusive start date, YYYY-MM-DD."},
        "end_date": {"type": "string", "description": "Inclusive end date, YYYY-MM-DD."},
    }
    if group_required:
        props["group_by"] = {
            "type": ["string", "null"],
            "enum": VALID_GROUPS + [None],
            "description": "Segment dimension to break the metric out by. Null returns a single total.",
        }
    props.update(_segment_properties())
    if extra:
        props.update(extra)
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_dau_by_group",
        "description": (
            "Daily active users over a date range, optionally broken out by one segment. "
            "DAU is distinct users with at least one non-test app event on a calendar date. "
            "Returns average, peak and total-distinct DAU per segment, not a per-day series. "
            "Use for questions about active users, audience size, or which markets or devices drive usage."
        ),
        "strict": True,
        "parameters": _base_params(),
    },
    {
        "type": "function",
        "name": "get_dau_trend",
        "description": (
            "Per-day DAU and agent adoption time series over a date range, at daily, weekly or "
            "monthly granularity. Use for questions about trend, change over time, or growth."
        ),
        "strict": True,
        "parameters": _base_params(
            extra={
                "granularity": {
                    "type": "string",
                    "enum": ["day", "week", "month"],
                    "description": "Time bucket for the series.",
                }
            },
            group_required=False,
        ),
    },
    {
        "type": "function",
        "name": "get_agent_health_by_group",
        "description": (
            "AI study companion quality metrics over a date range: interaction volume, users, "
            "p50 and p95 latency, success / timeout / error mix, resolution rate, escalation rate, "
            "CSAT with its response rate, and estimated token cost. Can group by a user segment or "
            "by intent, tool_used, model_name or response_status. Use for questions about agent "
            "quality, latency, reliability, resolution, escalation, satisfaction, or agent cost."
        ),
        "strict": True,
        "parameters": _base_params(
            extra={
                "group_by": {
                    "type": ["string", "null"],
                    "enum": VALID_GROUPS
                    + ["intent", "tool_used", "model_name", "response_status", "month", "week", None],
                    "description": (
                        "Dimension to break agent metrics out by. Use 'month' or 'week' to compare "
                        "periods over time, e.g. month-over-month latency. Null returns a single total."
                    ),
                },
                "intent": {
                    "type": ["string", "null"],
                    "description": "Optional exact-match filter on request intent, e.g. 'Homework Help'.",
                },
            },
            group_required=False,
        ),
    },
    {
        "type": "function",
        "name": "get_subscription_metrics_by_group",
        "description": (
            "Trial starts, paid conversions, cancellations and MRR, optionally by segment. "
            "date_basis controls what the date range means, and the two answer different "
            "questions: 'signup' (default) selects users who SIGNED UP in the range and reports "
            "rates against that cohort - use it for 'which channel converts best'. 'event' counts "
            "trials, conversions and cancellations that OCCURRED in the range - use it for "
            "'how many trials started in April'. Rates are only meaningful with 'signup'."
        ),
        "strict": True,
        "parameters": _base_params(
            extra={
                "date_basis": {
                    "type": ["string", "null"],
                    "enum": ["signup", "event", None],
                    "description": (
                        "'signup' (default) filters on signup date and reports cohort rates. "
                        "'event' counts lifecycle events that happened inside the range."
                    ),
                }
            }
        ),
    },
    {
        "type": "function",
        "name": "get_retention_by_group",
        "description": (
            "D1 and D7 return retention for users whose first app activity falls inside the date "
            "range, optionally by segment. Only users whose day-N date is inside the observed data "
            "window are counted in the denominator. Use for questions about retention or stickiness."
        ),
        "strict": True,
        "parameters": _base_params(),
    },
    {
        "type": "function",
        "name": "get_metric_definition",
        "description": (
            "The exact implemented definition of a metric in this warehouse, including its "
            "warehouse source column and any caveat. Use whenever the user asks what a metric "
            "means, how it is calculated, or why two numbers differ. Call with no name to list "
            "every documented metric."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "metric_name": {
                    "type": ["string", "null"],
                    "description": "Metric name, e.g. 'DAU' or 'Agent adoption rate'. Null lists all metrics.",
                }
            },
            "required": ["metric_name"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_data_coverage",
        "description": (
            "The date range the warehouse actually covers, plus row counts and the data-quality "
            "check summary. Call this first when a question says 'last week', 'recently' or "
            "'latest' so relative dates resolve against real data rather than today's calendar date."
        ),
        "strict": True,
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
]


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

class ToolInputError(ValueError):
    """Raised when the model supplies arguments that fail validation."""


def _connect() -> sqlite3.Connection:
    """Open the warehouse strictly read-only."""
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Warehouse not found at {DB_PATH}. Run: python src/etl_pipeline.py"
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_date(value: str, field: str) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ToolInputError(f"{field} must be a YYYY-MM-DD string, got {value!r}.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ToolInputError(f"{field} is not a real calendar date: {value!r}.") from exc
    if not (MIN_DATE <= parsed <= MAX_DATE):
        raise ToolInputError(f"{field} is outside the supported range: {value!r}.")
    return value


def _validate_range(start: str, end: str) -> tuple[str, str]:
    start = _validate_date(start, "start_date")
    end = _validate_date(end, "end_date")
    if start > end:
        raise ToolInputError(f"start_date ({start}) is after end_date ({end}).")
    return start, end


def _validate_group(group_by: str | None, allowed: list[str]) -> str | None:
    if group_by in (None, "", "none", "null"):
        return None
    if group_by not in allowed:
        raise ToolInputError(
            f"Unsupported group_by {group_by!r}. Allowed: {', '.join(allowed)}."
        )
    return group_by


def _build_filters(
    kwargs: dict[str, Any], allowed: list[str], prefix: str = ""
) -> tuple[list[str], list[Any], dict[str, str]]:
    """Turn allow-listed filter arguments into parameterized WHERE clauses."""
    clauses: list[str] = []
    params: list[Any] = []
    applied: dict[str, str] = {}
    for key in allowed:
        value = kwargs.get(key)
        if value in (None, "", "all", "All"):
            continue
        if not isinstance(value, str):
            raise ToolInputError(f"Filter {key} must be a string, got {type(value).__name__}.")
        if len(value) > 64:
            raise ToolInputError(f"Filter {key} value is too long.")
        clauses.append(f"{prefix}{key} = ?")
        params.append(value)
        applied[key] = value
    return clauses, params, applied


def _meta(
    metric: str, start: str, end: str, group_by: str | None,
    filters: dict[str, str], source: str, caveat: str | None = None,
) -> dict[str, Any]:
    out = {
        "metric": metric,
        "date_range": {"start": start, "end": end},
        "group_by": group_by or "none (overall total)",
        "filters_applied": filters or "none",
        "warehouse_source": source,
    }
    if caveat:
        out["caveat"] = caveat
    return out


def _rows(cursor) -> list[dict[str, Any]]:
    out = []
    for i, row in enumerate(cursor):
        if i >= MAX_ROWS:
            break
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
        out.append(d)
    return out


def _expected_days(granularity: str, period: str) -> int:
    """Days in a complete bucket. Months vary, so the period label decides."""
    if granularity == "day":
        return 1
    if granularity == "week":
        return 7
    try:
        year, month = int(period[:4]), int(period[5:7])
        return calendar.monthrange(year, month)[1]
    except (ValueError, IndexError):
        return 28


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile, matching numpy's default method.

    SQLite has no percentile function. Latency values are read with read-only
    parameterized SQL and reduced here, inside the validated tool layer, so the
    model still only ever receives the aggregate.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)), 1)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

def get_data_coverage(**kwargs: Any) -> dict[str, Any]:
    """Report the warehouse's real date coverage so relative dates resolve correctly."""
    with _connect() as conn:
        cov = conn.execute(
            "SELECT MIN(metric_date) first_date, MAX(metric_date) last_date, "
            "COUNT(*) days, SUM(dau) total_active_user_days "
            "FROM daily_product_metrics WHERE dau > 0"
        ).fetchone()
        dq = conn.execute("SELECT * FROM v_dq_summary").fetchone()
        users = conn.execute("SELECT COUNT(*) c FROM dim_users").fetchone()["c"]
    first, last = cov["first_date"], cov["last_date"]
    last_dt = datetime.strptime(last, "%Y-%m-%d").date()
    return {
        "metadata": {
            "metric": "warehouse coverage",
            "note": (
                "Resolve relative phrases such as 'last week' against last_date below, "
                "not against today's calendar date."
            ),
        },
        "first_date": first,
        "last_date": last,
        "days_covered": cov["days"],
        "registered_users": users,
        "last_full_week": {
            "start": (last_dt - __import__("datetime").timedelta(days=6)).isoformat(),
            "end": last,
        },
        "data_quality": {
            "events_in_metric_layer": dq["metric_events"],
            "events_excluded_as_test_or_unknown_user": dq["excluded_events"],
            "duplicate_event_copies_quarantined": dq["quarantined_events"],
            "latency_outliers_flagged": dq["latency_outliers"],
            "validation_checks_passed": f"{dq['checks_passed']}/{dq['checks_total']}",
        },
    }


def get_dau_by_group(**kwargs: Any) -> dict[str, Any]:
    """Average, peak and distinct DAU over a range, optionally by one segment."""
    start, end = _validate_range(kwargs.get("start_date"), kwargs.get("end_date"))
    group_by = _validate_group(kwargs.get("group_by"), VALID_GROUPS)
    where, params, applied = _build_filters(kwargs, VALID_GROUPS)

    seg_sql = group_by if group_by else "'all users'"
    clause = ("AND " + " AND ".join(where)) if where else ""

    sql = f"""
        WITH daily AS (
            SELECT {seg_sql} AS segment,
                   metric_date,
                   COUNT(DISTINCT user_id) AS dau,
                   COUNT(DISTINCT CASE WHEN has_agent_request = 1 THEN user_id END) AS agent_users
            FROM mart_user_day
            WHERE has_app_activity = 1 AND metric_date BETWEEN ? AND ? {clause}
            GROUP BY segment, metric_date
        )
        SELECT segment,
               ROUND(AVG(dau), 1)                                              AS avg_dau,
               MAX(dau)                                                        AS peak_dau,
               MIN(dau)                                                        AS min_dau,
               SUM(dau)                                                        AS active_user_days,
               COUNT(*)                                                        AS days_with_data,
               ROUND(CAST(SUM(agent_users) AS REAL) / NULLIF(SUM(dau), 0), 4)  AS agent_adoption_rate
        FROM daily
        GROUP BY segment
        ORDER BY avg_dau DESC
    """
    distinct_sql = f"""
        SELECT {seg_sql} AS segment, COUNT(DISTINCT user_id) AS distinct_users
        FROM mart_user_day
        WHERE has_app_activity = 1 AND metric_date BETWEEN ? AND ? {clause}
        GROUP BY segment
    """
    with _connect() as conn:
        rows = _rows(conn.execute(sql, [start, end, *params]))
        distinct = {r["segment"]: r["distinct_users"] for r in _rows(conn.execute(distinct_sql, [start, end, *params]))}
    for r in rows:
        r["distinct_users_in_period"] = distinct.get(r["segment"])

    return {
        "metadata": _meta(
            "DAU (distinct users with a non-test app event on a calendar date)",
            start, end, group_by, applied, "mart_user_day",
        ),
        "results": rows,
        "row_count": len(rows),
    }


def get_dau_trend(**kwargs: Any) -> dict[str, Any]:
    """DAU and agent adoption as a time series at day, week or month granularity."""
    start, end = _validate_range(kwargs.get("start_date"), kwargs.get("end_date"))
    granularity = kwargs.get("granularity") or "day"
    if granularity not in ("day", "week", "month"):
        raise ToolInputError(f"granularity must be day, week or month, got {granularity!r}.")
    where, params, applied = _build_filters(kwargs, VALID_GROUPS, prefix="m.")
    clause = ("AND " + " AND ".join(where)) if where else ""
    bucket = {"day": "m.metric_date", "week": "d.week_start_date", "month": "d.year_month"}[granularity]

    sql = f"""
        WITH daily AS (
            SELECT {bucket} AS period,
                   m.metric_date,
                   COUNT(DISTINCT m.user_id) AS dau,
                   COUNT(DISTINCT CASE WHEN m.has_agent_request = 1 THEN m.user_id END) AS agent_users,
                   SUM(m.agent_interaction_count) AS agent_interactions
            FROM mart_user_day m
            JOIN dim_date d ON d.date_key = m.metric_date
            WHERE m.has_app_activity = 1 AND m.metric_date BETWEEN ? AND ? {clause}
            GROUP BY period, m.metric_date
        )
        SELECT period,
               ROUND(AVG(dau), 1)                                             AS avg_dau,
               MAX(dau)                                                       AS peak_dau,
               SUM(agent_interactions)                                        AS agent_interactions,
               ROUND(CAST(SUM(agent_users) AS REAL) / NULLIF(SUM(dau), 0), 4) AS agent_adoption_rate,
               COUNT(*)                                                       AS days_in_period
        FROM daily
        GROUP BY period
        ORDER BY period
    """
    with _connect() as conn:
        rows = _rows(conn.execute(sql, [start, end, *params]))

    # Mark partial buckets. The first and last period of a range are usually clipped
    # by the range boundary, so comparing them to full periods manufactures a dramatic
    # change that is an artifact of the calendar, not the product. The comparison below
    # therefore uses complete periods only, and the partial ones are labelled so a
    # caller cannot quote them by accident.
    for r in rows:
        r["is_complete_period"] = int(r["days_in_period"] >= _expected_days(granularity, r["period"]))

    complete = [r for r in rows if r["is_complete_period"]]
    basis = complete if len(complete) >= 2 else rows
    change = None
    if len(basis) >= 2 and basis[0]["avg_dau"]:
        change = round((basis[-1]["avg_dau"] - basis[0]["avg_dau"]) / basis[0]["avg_dau"] * 100, 1)

    partial = [r["period"] for r in rows if not r["is_complete_period"]]
    caveat = None
    if partial:
        caveat = (
            f"{len(partial)} partial {granularity}(s) at the range edges ({', '.join(partial)}) are "
            f"clipped by the date range and are NOT used in the change calculation. Quote "
            f"first_to_last_change_pct, which compares complete periods only; do not compute a "
            f"change from the first and last rows of the table yourself."
        )

    return {
        "metadata": _meta(
            f"DAU trend by {granularity}", start, end, None, applied, "mart_user_day + dim_date",
            caveat=caveat,
        ),
        "results": rows,
        "row_count": len(rows),
        "first_to_last_change_pct": change,
        "change_compares": (
            {"from": basis[0]["period"], "to": basis[-1]["period"],
             "complete_periods_only": bool(len(complete) >= 2)}
            if len(basis) >= 2 else None
        ),
        "partial_periods_excluded": partial,
    }


def get_agent_health_by_group(**kwargs: Any) -> dict[str, Any]:
    """Agent volume, latency, reliability, resolution, escalation, CSAT and cost."""
    start, end = _validate_range(kwargs.get("start_date"), kwargs.get("end_date"))
    extra_groups = ["intent", "tool_used", "model_name", "response_status"]
    time_groups = {
        "month": "substr(request_date, 1, 7)",
        "week": "strftime('%Y-W%W', request_date)",
    }
    group_by = _validate_group(
        kwargs.get("group_by"), VALID_GROUPS + extra_groups + list(time_groups)
    )
    where, params, applied = _build_filters(kwargs, VALID_GROUPS + ["intent"])
    clause = ("AND " + " AND ".join(where)) if where else ""
    # seg_sql is resolved from the allow-list above, never from raw model input.
    seg_sql = time_groups.get(group_by, group_by) if group_by else "'all interactions'"

    sql = f"""
        SELECT {seg_sql} AS segment,
               COUNT(*)                             AS interactions,
               COUNT(DISTINCT user_id)              AS distinct_users,
               SUM(response_status = 'success')     AS successes,
               SUM(response_status = 'timeout')     AS timeouts,
               SUM(response_status = 'error')       AS errors,
               SUM(resolved_by_agent)               AS resolved,
               SUM(escalated_to_human)              AS escalated,
               SUM(has_satisfaction_score)          AS rated,
               SUM(satisfaction_score)              AS csat_sum,
               SUM(is_latency_outlier)              AS latency_outliers,
               ROUND(SUM(estimated_cost_usd), 4)    AS estimated_cost_usd,
               SUM(input_tokens + output_tokens)    AS total_tokens
        FROM v_agent_interactions_enriched
        WHERE request_date BETWEEN ? AND ? {clause}
        GROUP BY segment
        ORDER BY {"segment" if group_by in time_groups else "interactions DESC"}
    """
    lat_sql = f"""
        SELECT {seg_sql} AS segment, latency_ms
        FROM v_agent_interactions_enriched
        WHERE request_date BETWEEN ? AND ? {clause}
    """
    with _connect() as conn:
        rows = _rows(conn.execute(sql, [start, end, *params]))
        buckets: dict[Any, list[float]] = {}
        for r in conn.execute(lat_sql, [start, end, *params]):
            buckets.setdefault(r["segment"], []).append(r["latency_ms"])

    for r in rows:
        n = r["interactions"] or 1
        lat = buckets.get(r["segment"], [])
        r["p50_latency_ms"] = _percentile(lat, 50)
        r["p95_latency_ms"] = _percentile(lat, 95)
        r["success_rate"] = round(r["successes"] / n, 4)
        r["resolution_rate"] = round(r["resolved"] / n, 4)
        r["escalation_rate"] = round(r["escalated"] / n, 4)
        r["unresolved_not_escalated"] = n - r["resolved"] - r["escalated"]
        r["csat"] = round(r.pop("csat_sum") / r["rated"], 2) if r["rated"] else None
        r["csat_response_rate"] = round(r["rated"] / n, 4)

    return {
        "metadata": _meta(
            "Agent health (volume, latency, reliability, resolution, escalation, CSAT, cost)",
            start, end, group_by, applied, "v_agent_interactions_enriched",
            caveat=(
                "Resolution and escalation are mutually exclusive but not exhaustive, so the two "
                "rates do not sum to 1; unresolved_not_escalated holds the remainder. CSAT covers "
                "rated interactions only - always read it with csat_response_rate. Cost is a "
                "planning estimate from token volume, not a billed amount."
            ),
        ),
        "results": rows,
        "row_count": len(rows),
    }


def get_subscription_metrics_by_group(**kwargs: Any) -> dict[str, Any]:
    """Trial, paid conversion and cancellation metrics, by signup cohort or by event date."""
    start, end = _validate_range(kwargs.get("start_date"), kwargs.get("end_date"))
    group_by = _validate_group(kwargs.get("group_by"), VALID_GROUPS)
    where, params, applied = _build_filters(kwargs, VALID_GROUPS)
    basis = kwargs.get("date_basis") or "signup"
    if basis not in ("signup", "event"):
        raise ToolInputError(f"date_basis must be 'signup' or 'event', got {basis!r}.")
    clause = ("AND " + " AND ".join(where)) if where else ""
    seg_sql = group_by if group_by else "'all users'"

    if basis == "event":
        return _subscription_events(start, end, group_by, seg_sql, clause, params, applied)

    sql = f"""
        SELECT {seg_sql} AS segment,
               COUNT(*)                                                           AS cohort_users,
               SUM(has_trial)                                                     AS trial_starts,
               SUM(has_paid)                                                      AS paid_conversions,
               SUM(has_cancelled)                                                 AS cancellations,
               SUM(is_retained_paid)                                              AS still_paying,
               ROUND(CAST(SUM(has_trial) AS REAL) / NULLIF(COUNT(*), 0), 4)       AS trial_start_rate,
               ROUND(CAST(SUM(has_paid)  AS REAL) / NULLIF(COUNT(*), 0), 4)       AS paid_conversion_rate,
               ROUND(CAST(SUM(has_cancelled) AS REAL) / NULLIF(SUM(has_paid), 0), 4) AS cancellation_rate_of_paid,
               ROUND(SUM(CASE WHEN has_paid = 1 AND has_cancelled = 0
                              THEN monthly_price_usd ELSE 0 END), 2)              AS active_mrr_usd
        FROM mart_subscription_user
        WHERE signup_date BETWEEN ? AND ? {clause}
        GROUP BY segment
        ORDER BY paid_conversion_rate DESC
    """
    with _connect() as conn:
        rows = _rows(conn.execute(sql, [start, end, *params]))

    return {
        "metadata": _meta(
            "Subscription funnel for the signup cohort in the date range",
            start, end, group_by, applied, "mart_subscription_user",
            caveat=(
                "The denominator is users who SIGNED UP in this range, not users active in it. "
                "paid_conversion_rate is measured against that signup cohort. Note that in this "
                "dataset every trial has a paid start exactly 7 days later, so conversion measured "
                "against trial starters alone would be 100% and is not decision-grade."
            ),
        ),
        "results": rows,
        "row_count": len(rows),
    }


def _subscription_events(
    start: str, end: str, group_by: str | None, seg_sql: str,
    clause: str, params: list, applied: dict[str, str],
) -> dict[str, Any]:
    """Count lifecycle events that occurred inside the range.

    Each event type carries its own date, so this is three independent counts over
    the same window rather than a funnel: a cancellation in April can belong to a
    trial that started in January. No rates are returned, because there is no
    single denominator these counts share.
    """
    sql = f"""
        SELECT {seg_sql} AS segment,
               SUM(CASE WHEN trial_started_date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS trials_started,
               SUM(CASE WHEN paid_started_date  BETWEEN ? AND ? THEN 1 ELSE 0 END) AS paid_started,
               SUM(CASE WHEN cancelled_date     BETWEEN ? AND ? THEN 1 ELSE 0 END) AS cancellations,
               SUM(CASE WHEN paid_started_date BETWEEN ? AND ? THEN monthly_price_usd ELSE 0 END) AS new_mrr_usd
        FROM mart_subscription_user
        WHERE 1=1 {clause.replace('AND ', 'AND ', 1) if clause else ''}
        GROUP BY segment
        ORDER BY trials_started DESC
    """
    date_params = [start, end] * 4
    with _connect() as conn:
        rows = _rows(conn.execute(sql, [*date_params, *params]))
    rows = [r for r in rows if any(r[k] for k in
                                   ("trials_started", "paid_started", "cancellations"))] or rows

    return {
        "metadata": _meta(
            "Subscription lifecycle events occurring in the date range",
            start, end, group_by, applied, "mart_subscription_user",
            caveat=(
                "These are counts of events that HAPPENED in this window, not a cohort funnel: a "
                "cancellation here may belong to a trial that started before the window. No rates "
                "are returned because the three counts share no denominator. For conversion rates, "
                "call this tool again with date_basis='signup'."
            ),
        ),
        "results": rows,
        "row_count": len(rows),
    }


def get_retention_by_group(**kwargs: Any) -> dict[str, Any]:
    """D1 and D7 return retention for users first active in the date range."""
    start, end = _validate_range(kwargs.get("start_date"), kwargs.get("end_date"))
    group_by = _validate_group(kwargs.get("group_by"), VALID_GROUPS)
    where, params, applied = _build_filters(kwargs, VALID_GROUPS)
    clause = ("AND " + " AND ".join(where)) if where else ""
    seg_sql = group_by if group_by else "'all users'"

    sql = f"""
        SELECT {seg_sql} AS segment,
               COUNT(*)                                                          AS users_first_active,
               SUM(d1_eligible)                                                  AS d1_eligible,
               SUM(d1_retained)                                                  AS d1_retained,
               ROUND(CAST(SUM(d1_retained) AS REAL) / NULLIF(SUM(d1_eligible), 0), 4) AS d1_retention_rate,
               SUM(d7_eligible)                                                  AS d7_eligible,
               SUM(d7_retained)                                                  AS d7_retained,
               ROUND(CAST(SUM(d7_retained) AS REAL) / NULLIF(SUM(d7_eligible), 0), 4) AS d7_retention_rate
        FROM mart_user_retention
        WHERE first_activity_date BETWEEN ? AND ? {clause}
        GROUP BY segment
        ORDER BY d7_retention_rate DESC
    """
    with _connect() as conn:
        rows = _rows(conn.execute(sql, [start, end, *params]))

    return {
        "metadata": _meta(
            "D1 / D7 return retention", start, end, group_by, applied, "mart_user_retention",
            caveat=(
                "Day-N return means active on exactly first_activity_date + N. Users whose day-N "
                "date falls outside the observed window are excluded from the denominator rather "
                "than counted as churned."
            ),
        ),
        "results": rows,
        "row_count": len(rows),
    }


def get_metric_definition(**kwargs: Any) -> dict[str, Any]:
    """Return the implemented definition of one metric, or list them all."""
    name = kwargs.get("metric_name")
    with _connect() as conn:
        if not name:
            rows = _rows(conn.execute("SELECT metric_name FROM metric_definitions ORDER BY metric_name"))
            return {"metadata": {"metric": "metric glossary"},
                    "available_metrics": [r["metric_name"] for r in rows]}
        if not isinstance(name, str) or len(name) > 80:
            raise ToolInputError("metric_name must be a short string.")
        rows = _rows(conn.execute(
            "SELECT * FROM metric_definitions WHERE LOWER(metric_name) = LOWER(?)", [name]
        ))
        if not rows:
            rows = _rows(conn.execute(
                "SELECT * FROM metric_definitions WHERE LOWER(metric_name) LIKE LOWER(?)", [f"%{name}%"]
            ))
        if not rows:
            all_names = [r["metric_name"] for r in _rows(
                conn.execute("SELECT metric_name FROM metric_definitions ORDER BY metric_name"))]
            return {"error": f"No documented metric matching {name!r}.", "available_metrics": all_names}
    return {"metadata": {"metric": "metric definition"}, "results": rows, "row_count": len(rows)}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

HANDLERS = {
    "get_dau_by_group": get_dau_by_group,
    "get_dau_trend": get_dau_trend,
    "get_agent_health_by_group": get_agent_health_by_group,
    "get_subscription_metrics_by_group": get_subscription_metrics_by_group,
    "get_retention_by_group": get_retention_by_group,
    "get_metric_definition": get_metric_definition,
    "get_data_coverage": get_data_coverage,
}


def dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route an allow-listed tool name to its handler and never raise upward.

    A rejected or failed call returns a structured error so the model can
    correct itself or tell the user, rather than crashing the dashboard.
    """
    if name not in HANDLERS:
        return {"error": f"Unsupported tool: {name}", "available_tools": sorted(HANDLERS)}
    if not isinstance(arguments, dict):
        return {"error": "Tool arguments must be an object."}
    try:
        return HANDLERS[name](**arguments)
    except ToolInputError as exc:
        return {"error": f"Invalid arguments: {exc}"}
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except sqlite3.Error as exc:
        return {"error": f"Warehouse query failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 - the dashboard must stay up
        return {"error": f"{type(exc).__name__}: {exc}"}
