"""Correctness and safety tests for the Navi Analytics Agent tool layer.

Two things are being proven here:

1. Correctness - every tool's numbers match an independent pandas recomputation
   straight from the raw CSV files, not from the warehouse. If the ETL and the
   tools ever disagree with the source data, these fail.
2. Safety - the tool layer rejects anything outside its allow-list, refuses
   malformed input, and cannot write to the database.

Run:  python tests/test_agent_tools.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import agent_tools as T  # noqa: E402

RAW = PROJECT_ROOT / "data" / "raw"
PASSED, FAILED = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def approx(a, b, tol=1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return abs(float(a) - float(b)) <= tol


# --------------------------------------------------------------------------
# Independent recomputation from raw CSVs
# --------------------------------------------------------------------------

def load_clean_raw():
    """Rebuild the metric layer from raw files using the documented policy."""
    users = pd.read_csv(RAW / "users.csv")
    events = pd.read_csv(RAW / "app_events.csv")
    ix = pd.read_csv(RAW / "agent_interactions.csv")
    subs = pd.read_csv(RAW / "subscriptions.csv")

    known = set(users.user_id)
    events["is_known"] = events.user_id.isin(known).astype(int)
    events["_rank"] = (1 - events["is_known"]) * 2 + events["is_test_event"]
    events = events.sort_values(["event_id", "_rank"], kind="mergesort")
    events = events[~events.event_id.duplicated(keep="first")]
    events = events[(events.is_test_event == 0) & (events.is_known == 1)].copy()
    events["event_date"] = pd.to_datetime(events.event_ts).dt.strftime("%Y-%m-%d")

    ix["request_date"] = pd.to_datetime(ix.request_ts).dt.strftime("%Y-%m-%d")
    ix = ix[ix.user_id.isin(known)].copy()
    return users, events, ix, subs


USERS, EVENTS, IX, SUBS = load_clean_raw()
WINDOW = ("2026-03-01", "2026-03-31")


def test_dau_total():
    lo, hi = WINDOW
    sel = EVENTS[(EVENTS.event_date >= lo) & (EVENTS.event_date <= hi)]
    daily = sel.groupby("event_date").user_id.nunique()
    res = T.get_dau_by_group(start_date=lo, end_date=hi, group_by=None,
                             country_code=None, device_os=None,
                             acquisition_channel=None, app_version=None)
    row = res["results"][0]
    check("DAU avg matches raw recomputation",
          approx(row["avg_dau"], round(daily.mean(), 1), 0.05), f"{row['avg_dau']} vs {daily.mean():.1f}")
    check("DAU peak matches raw recomputation", row["peak_dau"] == int(daily.max()))
    check("DAU distinct users matches raw", row["distinct_users_in_period"] == sel.user_id.nunique())


def test_dau_by_country():
    lo, hi = WINDOW
    sel = EVENTS[(EVENTS.event_date >= lo) & (EVENTS.event_date <= hi)].merge(
        USERS[["user_id", "country_code"]], on="user_id")
    daily = sel.groupby(["country_code", "event_date"]).user_id.nunique().groupby("country_code").mean()
    res = T.get_dau_by_group(start_date=lo, end_date=hi, group_by="country_code",
                             country_code=None, device_os=None,
                             acquisition_channel=None, app_version=None)
    ok = all(approx(r["avg_dau"], round(daily[r["segment"]], 1), 0.05) for r in res["results"])
    check("DAU by country matches raw for every segment", ok)
    check("DAU by country returns all 4 markets", len(res["results"]) == 4)


def test_dau_filter_combination():
    lo, hi = WINDOW
    # Segments are user-level attributes from dim_users. app_events carries its own
    # device_os reported by the client; the warehouse deliberately segments on the
    # user attribute, so the recomputation must use the same one.
    seg = USERS[["user_id", "country_code", "device_os"]].rename(
        columns={"device_os": "user_device_os"})
    sel = EVENTS[(EVENTS.event_date >= lo) & (EVENTS.event_date <= hi)].merge(seg, on="user_id")
    sel = sel[(sel.country_code == "US") & (sel.user_device_os == "iOS")]
    expected = sel.groupby("event_date").user_id.nunique().mean()
    res = T.get_dau_by_group(start_date=lo, end_date=hi, group_by=None,
                             country_code="US", device_os="iOS",
                             acquisition_channel=None, app_version=None)
    check("DAU with two stacked filters matches raw",
          approx(res["results"][0]["avg_dau"], round(expected, 1), 0.05))
    check("applied filters are echoed in metadata",
          res["metadata"]["filters_applied"] == {"country_code": "US", "device_os": "iOS"})


def test_agent_health():
    lo, hi = WINDOW
    sel = IX[(IX.request_date >= lo) & (IX.request_date <= hi)]
    res = T.get_agent_health_by_group(start_date=lo, end_date=hi, group_by=None,
                                      intent=None, country_code=None, device_os=None,
                                      acquisition_channel=None, app_version=None)
    row = res["results"][0]
    check("agent interaction count matches raw", row["interactions"] == len(sel))
    check("p95 latency matches numpy",
          approx(row["p95_latency_ms"], round(float(np.percentile(sel.latency_ms, 95)), 1), 0.2))
    check("p50 latency matches numpy",
          approx(row["p50_latency_ms"], round(float(np.percentile(sel.latency_ms, 50)), 1), 0.2))
    check("resolution rate matches raw",
          approx(row["resolution_rate"], round(sel.resolved_by_agent.mean(), 4), 1e-4))
    check("escalation rate matches raw",
          approx(row["escalation_rate"], round(sel.escalated_to_human.mean(), 4), 1e-4))
    check("CSAT uses rated interactions only",
          approx(row["csat"], round(sel.satisfaction_score.mean(), 2), 0.01))
    check("CSAT response rate matches raw",
          approx(row["csat_response_rate"], round(sel.satisfaction_score.notna().mean(), 4), 1e-4))
    check("resolution + escalation + remainder = total",
          row["resolved"] + row["escalated"] + row["unresolved_not_escalated"] == row["interactions"])


def test_agent_health_by_intent():
    lo, hi = WINDOW
    sel = IX[(IX.request_date >= lo) & (IX.request_date <= hi)]
    res = T.get_agent_health_by_group(start_date=lo, end_date=hi, group_by="intent",
                                      intent=None, country_code=None, device_os=None,
                                      acquisition_channel=None, app_version=None)
    expected = sel.intent.value_counts().to_dict()
    ok = all(r["interactions"] == expected[r["segment"]] for r in res["results"])
    check("agent health by intent matches raw counts", ok)
    check("intent filter narrows to one segment",
          T.get_agent_health_by_group(start_date=lo, end_date=hi, group_by=None,
                                      intent="Homework Help", country_code=None, device_os=None,
                                      acquisition_channel=None, app_version=None
                                      )["results"][0]["interactions"]
          == int((sel.intent == "Homework Help").sum()))


def test_subscriptions():
    lo, hi = "2026-01-01", "2026-03-20"
    u = USERS.copy()
    u["signup_date"] = pd.to_datetime(u.signup_ts).dt.strftime("%Y-%m-%d")
    cohort = u[(u.signup_date >= lo) & (u.signup_date <= hi)].merge(SUBS, on="user_id")
    res = T.get_subscription_metrics_by_group(start_date=lo, end_date=hi, group_by=None,
                                              country_code=None, device_os=None,
                                              acquisition_channel=None, app_version=None)
    row = res["results"][0]
    check("signup cohort size matches raw", row["cohort_users"] == len(cohort))
    check("paid conversions match raw", row["paid_conversions"] == int(cohort.paid_started_at.notna().sum()))
    check("paid conversion rate uses the signup-cohort denominator",
          approx(row["paid_conversion_rate"], round(cohort.paid_started_at.notna().mean(), 4), 1e-4))
    check("subscription caveat is surfaced to the model", "caveat" in res["metadata"])


def test_retention():
    res = T.get_retention_by_group(start_date="2026-01-01", end_date="2026-01-31",
                                   group_by="acquisition_channel", country_code=None,
                                   device_os=None, acquisition_channel=None, app_version=None)
    ok = all(0 <= r["d7_retention_rate"] <= 1 and r["d7_retained"] <= r["d7_eligible"]
             for r in res["results"])
    check("retention rates are valid proportions with retained <= eligible", ok)
    check("retention groups by all 5 channels", len(res["results"]) == 5)


def test_trend_excludes_partial_periods():
    """A change computed across clipped edge buckets is a calendar artifact.

    April 2026 starts mid-week and ends mid-week, so the first and last weekly
    buckets hold 5 and 4 days. Comparing them yields -75.4%; comparing complete
    weeks yields -53.0%. The tool must report the latter and label the former.
    """
    res = T.get_dau_trend(start_date="2026-04-01", end_date="2026-04-30",
                          granularity="week", **{s: None for s in T.VALID_GROUPS})
    rows = res["results"]
    partial = [r for r in rows if not r["is_complete_period"]]
    check("partial weeks are flagged, not silently included", len(partial) == 2,
          f"{len(partial)} flagged")
    check("partial weeks are named in the result",
          set(res["partial_periods_excluded"]) == {"2026-03-30", "2026-04-27"})
    check("change compares complete periods only",
          res["change_compares"]["complete_periods_only"] is True)

    complete = [r for r in rows if r["is_complete_period"]]
    expected = round((complete[-1]["avg_dau"] - complete[0]["avg_dau"])
                     / complete[0]["avg_dau"] * 100, 1)
    check("change matches a complete-period recomputation",
          approx(res["first_to_last_change_pct"], expected, 0.05),
          f"{res['first_to_last_change_pct']} vs {expected}")

    naive = round((rows[-1]["avg_dau"] - rows[0]["avg_dau"]) / rows[0]["avg_dau"] * 100, 1)
    check("the naive edge-to-edge change is NOT what gets reported",
          abs(res["first_to_last_change_pct"] - naive) > 10,
          f"reported {res['first_to_last_change_pct']}, naive {naive}")
    check("a caveat warns the caller off recomputing it",
          "partial" in (res["metadata"].get("caveat") or "").lower())

    # A range aligned to whole weeks should flag nothing.
    clean = T.get_dau_trend(start_date="2026-02-02", end_date="2026-03-01",
                            granularity="week", **{s: None for s in T.VALID_GROUPS})
    check("no partial flags when the range aligns to whole weeks",
          clean["partial_periods_excluded"] == [], str(clean["partial_periods_excluded"]))


def test_metric_definitions():
    res = T.get_metric_definition(metric_name="DAU")
    check("metric definition returns the implemented rule",
          "implemented_definition" in res["results"][0])
    check("fuzzy metric lookup works", "results" in T.get_metric_definition(metric_name="adoption"))
    miss = T.get_metric_definition(metric_name="revenue per llama")
    check("unknown metric returns an error plus the valid list",
          "error" in miss and len(miss["available_metrics"]) > 5)
    check("null metric name lists the glossary",
          len(T.get_metric_definition(metric_name=None)["available_metrics"]) >= 10)


def test_coverage():
    res = T.get_data_coverage()
    check("coverage reports the real last date", res["last_date"] == "2026-04-30")
    check("coverage reports the real first date", res["first_date"] == "2026-01-01")
    check("coverage exposes DQ status", res["data_quality"]["validation_checks_passed"].endswith("/15"))


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

def test_rejects_bad_input():
    bad_group = T.dispatch_tool("get_dau_by_group", {
        "start_date": "2026-03-01", "end_date": "2026-03-31",
        "group_by": "user_id; DROP TABLE dim_users", "country_code": None,
        "device_os": None, "acquisition_channel": None, "app_version": None})
    check("SQL injection via group_by is rejected", "error" in bad_group)

    inj = T.dispatch_tool("get_dau_by_group", {
        "start_date": "2026-03-01", "end_date": "2026-03-31", "group_by": None,
        "country_code": "US' OR '1'='1", "device_os": None,
        "acquisition_channel": None, "app_version": None})
    check("SQL injection via a filter value is bound, not executed",
          "error" not in inj and inj["results"] == [])

    for args, label in [
        ({"start_date": "March 1st", "end_date": "2026-03-31"}, "non-ISO date"),
        ({"start_date": "2026-13-45", "end_date": "2026-03-31"}, "impossible date"),
        ({"start_date": "2026-03-31", "end_date": "2026-03-01"}, "reversed range"),
        ({"start_date": "1999-01-01", "end_date": "2026-03-31"}, "out-of-range date"),
    ]:
        full = {"group_by": None, "country_code": None, "device_os": None,
                "acquisition_channel": None, "app_version": None, **args}
        check(f"{label} is rejected", "error" in T.dispatch_tool("get_dau_by_group", full))

    check("unknown tool name is refused",
          "error" in T.dispatch_tool("drop_everything", {}))
    check("oversized filter value is refused",
          "error" in T.dispatch_tool("get_dau_by_group", {
              "start_date": "2026-03-01", "end_date": "2026-03-31", "group_by": None,
              "country_code": "x" * 200, "device_os": None,
              "acquisition_channel": None, "app_version": None}))


def test_database_is_read_only():
    conn = T._connect()
    try:
        conn.execute("DELETE FROM dim_users")
        check("warehouse connection rejects writes", False, "a DELETE succeeded")
    except sqlite3.OperationalError:
        check("warehouse connection rejects writes", True)
    finally:
        conn.close()


def test_returns_aggregates_only():
    res = T.get_dau_by_group(start_date="2026-03-01", end_date="2026-03-31",
                             group_by="country_code", country_code=None, device_os=None,
                             acquisition_channel=None, app_version=None)
    blob = str(res)
    check("no user_id leaks into a tool result", "U00" not in blob)
    check("every result row is small and aggregate", len(res["results"]) <= T.MAX_ROWS)


def main() -> int:
    print(f"Testing against warehouse: {T.DB_PATH}\n")
    for fn in [
        test_dau_total, test_dau_by_country, test_dau_filter_combination,
        test_trend_excludes_partial_periods,
        test_agent_health, test_agent_health_by_intent, test_subscriptions,
        test_retention, test_metric_definitions, test_coverage,
        test_rejects_bad_input, test_database_is_read_only, test_returns_aggregates_only,
    ]:
        print(f"\n{fn.__name__}")
        fn()
    print(f"\n{'=' * 60}\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
