"""Prove the dashboard and the Analytics Agent report identical numbers.

The plan's consistency requirement is that a stakeholder can reproduce an agent
answer from the dashboard. These tests take a filter state, compute each metric
through the dashboard's path (src/metrics.py) and through the agent's path
(src/agent_tools.py), and assert the two match exactly.

Run:  python tests/test_dashboard_agreement.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import agent_tools as T  # noqa: E402
import metrics as M  # noqa: E402

PASSED, FAILED = 0, 0


def check(name: str, a, b, tol: float = 1e-6) -> None:
    """Assert the dashboard value and the agent value agree."""
    global PASSED, FAILED
    if a is None and b is None:
        ok = True
    elif a is None or b is None:
        ok = False
    else:
        ok = abs(float(a) - float(b)) <= tol
    if ok:
        PASSED += 1
        print(f"  PASS  {name}  (both {a})")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  dashboard={a}  agent={b}")


CONN = M.connect()
NO_SEG = {s: None for s in M.SEGMENT_LABELS}

CASES = [
    ("full window, no filters", "2026-01-01", "2026-04-30", {}),
    ("March only", "2026-03-01", "2026-03-31", {}),
    ("last week of data", "2026-04-24", "2026-04-30", {}),
    ("US only", "2026-02-01", "2026-02-28", {"country_code": "US"}),
    ("Android + Organic", "2026-01-01", "2026-04-30",
     {"device_os": "Android", "acquisition_channel": "Organic"}),
]


def run_case(label: str, start: str, end: str, segments: dict) -> None:
    print(f"\n{label}  ({start} → {end}, {segments or 'no segment filters'})")
    f = M.Filters(start, end, segments)
    seg_args = {**NO_SEG, **segments}

    # --- DAU ---
    dash = M.daily_series(CONN, f)
    tool = T.get_dau_by_group(start_date=start, end_date=end, group_by=None, **seg_args)
    trow = tool["results"][0] if tool.get("results") else {}
    check("avg DAU", round(dash.dau.mean(), 1) if len(dash) else None, trow.get("avg_dau"), 0.05)
    check("peak DAU", dash.dau.max() if len(dash) else None, trow.get("peak_dau"))
    check("agent adoption rate",
          round(dash.agent_users.sum() / dash.dau.sum(), 4) if len(dash) and dash.dau.sum() else None,
          trow.get("agent_adoption_rate"), 1e-4)

    # --- Agent health ---
    dash_a = M.agent_rollup(CONN, f).iloc[0]
    tool_a = T.get_agent_health_by_group(start_date=start, end_date=end, group_by=None,
                                        intent=None, **seg_args)["results"][0]
    check("agent interactions", dash_a.interactions, tool_a["interactions"])
    check("resolution rate", round(dash_a.resolved / dash_a.interactions, 4),
          tool_a["resolution_rate"], 1e-4)
    check("escalation rate", round(dash_a.escalated / dash_a.interactions, 4),
          tool_a["escalation_rate"], 1e-4)
    check("CSAT", round(dash_a.csat, 2), tool_a["csat"], 0.01)

    # p95 latency: the dashboard's pandas quantile vs the tool's own percentile helper.
    lat = M.latency_values(CONN, f)
    check("p95 latency", round(lat.quantile(.95), 1), tool_a["p95_latency_ms"], 0.2)
    check("p50 latency", round(lat.quantile(.50), 1), tool_a["p50_latency_ms"], 0.2)

    # --- Subscriptions ---
    # An empty cohort is a real case (signups stop on 2026-03-20), and the two paths
    # must agree that it is empty rather than one of them inventing a row.
    dash_sf = M.subs_rollup(CONN, f)
    tool_s = T.get_subscription_metrics_by_group(start_date=start, end_date=end,
                                                 group_by=None, **seg_args)
    if dash_sf.empty or not tool_s.get("results"):
        check("signup cohort is empty on both paths",
              0 if dash_sf.empty else 1, 0 if not tool_s.get("results") else 1)
    else:
        dash_s, ts = dash_sf.iloc[0], tool_s["results"][0]
        check("signup cohort", dash_s.cohort, ts["cohort_users"])
        check("paid conversion rate", round(dash_s.paid_rate, 4) if dash_s.cohort else None,
              ts["paid_conversion_rate"], 1e-4)

    # --- Retention ---
    dash_rf = M.retention_rollup(CONN, f)
    tool_r = T.get_retention_by_group(start_date=start, end_date=end, group_by=None, **seg_args)
    if dash_rf.empty or not tool_r.get("results"):
        check("retention cohort is empty on both paths",
              0 if dash_rf.empty else 1, 0 if not tool_r.get("results") else 1)
    else:
        dash_r, tr = dash_rf.iloc[0], tool_r["results"][0]
        check("D7 retention rate",
              round(dash_r.d7_rate, 4) if dash_r.d7_elig else None, tr["d7_retention_rate"], 1e-4)
        check("D1 retention rate",
              round(dash_r.d1_rate, 4) if dash_r.d1_elig else None, tr["d1_retention_rate"], 1e-4)


def run_segment_case() -> None:
    """Per-segment breakdowns must agree value by value, not just in total."""
    print("\nper-segment breakdowns (March, by country)")
    f = M.Filters("2026-03-01", "2026-03-31", {})
    dash = M.segment_dau(CONN, f, "country_code").set_index("seg")
    tool = T.get_dau_by_group(start_date="2026-03-01", end_date="2026-03-31",
                              group_by="country_code", **NO_SEG)["results"]
    for row in tool:
        check(f"avg DAU / {row['segment']}", dash.loc[row["segment"], "avg_dau"], row["avg_dau"], 0.05)
        check(f"adoption / {row['segment']}", round(dash.loc[row["segment"], "adoption"], 4),
              row["agent_adoption_rate"], 1e-4)

    print("\nper-segment breakdowns (full window, conversion by channel)")
    f2 = M.Filters("2026-01-01", "2026-04-30", {})
    dash2 = M.subs_rollup(CONN, f2, "acquisition_channel").set_index("seg")
    tool2 = T.get_subscription_metrics_by_group(start_date="2026-01-01", end_date="2026-04-30",
                                                group_by="acquisition_channel", **NO_SEG)["results"]
    for row in tool2:
        check(f"paid conversion / {row['segment']}",
              round(dash2.loc[row["segment"], "paid_rate"], 4), row["paid_conversion_rate"], 1e-4)


def main() -> int:
    print(f"Warehouse: {M.DB_PATH}")
    for case in CASES:
        run_case(*case)
    run_segment_case()
    print(f"\n{'=' * 60}\n{PASSED} agreements, {FAILED} disagreements")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
