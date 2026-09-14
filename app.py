"""Navi Product Analytics - Streamlit dashboard.

Reads only the curated warehouse built by src/etl_pipeline.py. The sidebar date
range and segment filters are applied to every chart AND passed to the Analytics
Agent as its dashboard context, so a stakeholder can reproduce any agent answer
from the visuals on screen.

Launch:  streamlit run app.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import metrics as M  # noqa: E402  (needs the sys.path line above)
DB_PATH = Path(os.environ.get("NAVI_DB_PATH", PROJECT_ROOT / "warehouse" / "navi_analytics.db"))

st.set_page_config(page_title="Navi Product Analytics", layout="wide", page_icon="📱")

# --------------------------------------------------------------------------
# Chart system
# --------------------------------------------------------------------------
# Categorical hues are assigned in fixed order and never cycled; a chart that
# would need a ninth series folds the tail into "Other". Sequential magnitude
# uses a single hue. No chart uses two y-axes.

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
STATUS = {"good": "#008300", "warning": "#eda100", "serious": "#eb6834", "critical": "#e34948"}
INK, INK_SOFT, INK_MUTED = "#0b0b0b", "#52514e", "#8a8984"
GRID = "rgba(11,11,11,0.08)"
SURFACE = "#ffffff"

SEGMENT_LABELS = M.SEGMENT_LABELS


def style(fig: go.Figure, height: int = 320, percent_y: bool = False, y_title: str = "") -> go.Figure:
    """Recessive axes, one y-axis, legend only when there are two or more series."""
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=28, b=8),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family="system-ui, -apple-system, sans-serif", size=12, color=INK_SOFT),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(size=11)),
        showlegend=len(fig.data) > 1,
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, ticks="outside", tickcolor=GRID,
                     tickfont=dict(size=11, color=INK_MUTED))
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False, linecolor="rgba(0,0,0,0)",
                     tickfont=dict(size=11, color=INK_MUTED), title=y_title,
                     tickformat=".0%" if percent_y else None)
    return fig


def line_chart(df: pd.DataFrame, x: str, series: dict[str, str], percent_y=False, y_title="",
               height=320, muted: set[str] | None = None):
    """Two-pixel lines, no per-point markers, crosshair tooltip from hovermode.

    A column named in `muted` is drawn thin and faded, for showing a noisy raw
    series behind its own smoothed line without competing with it.
    """
    fig = go.Figure()
    for i, (col, label) in enumerate(series.items()):
        is_muted = bool(muted and col in muted)
        fig.add_trace(go.Scatter(
            x=df[x], y=df[col], name=label, mode="lines",
            line=dict(color="rgba(11,11,11,0.20)" if is_muted else SERIES[i % len(SERIES)],
                      width=1 if is_muted else 2),
            hovertemplate=f"<b>{label}</b>: %{{y:{'.1%' if percent_y else ',.0f'}}}<extra></extra>",
        ))
    return style(fig, height, percent_y, y_title)


def bar_chart(df: pd.DataFrame, x: str, y: str, percent=False, y_title="", height=320, color=None):
    """Single-measure ranked bars: one hue, 4px rounded data-end, direct labels."""
    fig = go.Figure(go.Bar(
        x=df[x], y=df[y],
        marker=dict(color=color or SERIES[0], cornerradius=4, line=dict(width=2, color=SURFACE)),
        text=[f"{v:.1%}" if percent else f"{v:,.0f}" for v in df[y]],
        textposition="outside", textfont=dict(size=11, color=INK_SOFT),
        hovertemplate=f"<b>%{{x}}</b><br>%{{y:{'.1%' if percent else ',.0f'}}}<extra></extra>",
    ))
    fig.update_layout(hovermode="closest")
    return style(fig, height, percent, y_title)


def stacked_bar(df: pd.DataFrame, x: str, series: dict[str, str], y_title="", height=320):
    """Stacked composition with a 2px surface gap between segments."""
    fig = go.Figure()
    for i, (col, label) in enumerate(series.items()):
        fig.add_trace(go.Bar(
            x=df[x], y=df[col], name=label,
            marker=dict(color=SERIES[i % len(SERIES)], line=dict(width=2, color=SURFACE)),
            hovertemplate=f"<b>{label}</b>: %{{y:,.0f}}<extra></extra>",
        ))
    fig.update_layout(barmode="stack")
    return style(fig, height, False, y_title)


def table(df: pd.DataFrame, formats: dict[str, str] | None = None, key: str | None = None) -> None:
    """Render a table with native column formatting.

    Streamlit's column_config keeps the underlying values numeric, so a reader can
    still sort by them - which a pre-formatted string column would break. It also
    avoids the pandas Styler path and its jinja2 version requirement.
    """
    cfg = {}
    for col, kind in (formats or {}).items():
        if col not in df.columns:
            continue
        if kind == "percent":
            cfg[col] = st.column_config.NumberColumn(col, format="percent")
        elif kind == "dollar":
            cfg[col] = st.column_config.NumberColumn(col, format="dollar")
        elif kind == "int":
            cfg[col] = st.column_config.NumberColumn(col, format="localized")
        elif kind == "decimal":
            cfg[col] = st.column_config.NumberColumn(col, format="%.2f")
        elif kind == "decimal1":
            cfg[col] = st.column_config.NumberColumn(col, format="%.1f")
    st.dataframe(df, width="stretch", hide_index=True, column_config=cfg, key=key)


def kpi(label: str, value: str, caption: str = "", tone: str | None = None) -> None:
    color = STATUS.get(tone or "", INK)
    st.markdown(
        f"""<div style="border:1px solid rgba(11,11,11,.10);border-radius:12px;padding:14px 16px;
                        background:{SURFACE};height:100%">
              <div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;
                          color:{INK_MUTED};margin-bottom:6px">{label}</div>
              <div style="font-size:26px;font-weight:600;color:{color};line-height:1.1">{value}</div>
              <div style="font-size:11px;color:{INK_MUTED};margin-top:5px">{caption}</div>
            </div>""",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Warehouse access
# --------------------------------------------------------------------------

@st.cache_resource
def _conn() -> sqlite3.Connection:
    return M.connect(DB_PATH)


@st.cache_data(ttl=600)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Only for the Data Quality tab, which reads the dq_* tables directly."""
    return pd.read_sql_query(sql, _conn(), params=params)


if not DB_PATH.exists():
    st.title("Navi Product Analytics")
    st.error(
        f"No warehouse found at `{DB_PATH}`.\n\n"
        "Build it first:\n```bash\npython src/etl_pipeline.py\n```"
    )
    st.stop()


@st.cache_data(ttl=600)
def coverage() -> dict:
    return M.coverage(_conn())


@st.cache_data(ttl=600)
def dimension_values(dim: str) -> list[str]:
    return M.dimension_values(_conn(), dim)


COV = coverage()
DIMS = {d: ["All"] + dimension_values(d) for d in SEGMENT_LABELS}

# --------------------------------------------------------------------------
# Shared filters
# --------------------------------------------------------------------------

st.sidebar.markdown("### Navi Product Analytics")
st.sidebar.caption(f"Warehouse covers {COV['lo']} → {COV['hi']}")

section = st.sidebar.radio(
    "Explore",
    ["Overview", "Active Users", "Agent Health", "Subscriptions & Retention",
     "Ask Navi Analytics Agent", "Data Quality"],
)

st.sidebar.markdown("---")
st.sidebar.markdown("**Date range**")
preset = st.sidebar.selectbox(
    "Preset", ["All data", "Last 7 days", "Last 30 days", "Last 90 days", "Custom"], index=0
)
hi = pd.to_datetime(COV["hi"]).date()
lo = pd.to_datetime(COV["lo"]).date()
presets = {"Last 7 days": 6, "Last 30 days": 29, "Last 90 days": 89}
if preset in presets:
    start_date, end_date = max(lo, hi - pd.Timedelta(days=presets[preset]).to_pytimedelta()), hi
elif preset == "Custom":
    picked = st.sidebar.date_input("Range", (lo, hi), min_value=lo, max_value=hi)
    start_date, end_date = (picked if isinstance(picked, tuple) and len(picked) == 2 else (lo, hi))
else:
    start_date, end_date = lo, hi

st.sidebar.markdown("**Segments**")
selected = {d: st.sidebar.selectbox(SEGMENT_LABELS[d], DIMS[d], key=f"f_{d}") for d in SEGMENT_LABELS}
active_filters = {d: v for d, v in selected.items() if v != "All"}

START, END = str(start_date), str(end_date)


filter_note = M.Filters(START, END, active_filters).describe()
st.sidebar.markdown("---")
st.sidebar.caption(f"Applied to every chart and to the Agent:\n\n**{START} → {END}**\n\n{filter_note}")


# --------------------------------------------------------------------------
# Metric access - every query lives in src/metrics.py, shared with the agent
# --------------------------------------------------------------------------

F = M.Filters(start=START, end=END, segments=active_filters)


@st.cache_data(ttl=600)
def daily_series() -> pd.DataFrame:
    return M.daily_series(_conn(), F)


@st.cache_data(ttl=600)
def segment_dau(dim: str) -> pd.DataFrame:
    return M.segment_dau(_conn(), F, dim)


@st.cache_data(ttl=600)
def agent_rollup(group_by: str | None = None) -> pd.DataFrame:
    return M.agent_rollup(_conn(), F, group_by)


@st.cache_data(ttl=600)
def latency_percentiles(bucket: str = "request_date") -> pd.DataFrame:
    return M.latency_percentiles(_conn(), F, bucket)


@st.cache_data(ttl=600)
def subs_rollup(group_by: str | None = None) -> pd.DataFrame:
    return M.subs_rollup(_conn(), F, group_by)


@st.cache_data(ttl=600)
def retention_rollup(group_by: str | None = None) -> pd.DataFrame:
    return M.retention_rollup(_conn(), F, group_by)


def header(title: str, subtitle: str) -> None:
    st.markdown(f"## {title}")
    st.caption(f"{subtitle} · **{START} → {END}** · {filter_note}")


def empty_guard(df: pd.DataFrame, what: str) -> bool:
    if df.empty or (len(df) == 1 and (df.iloc[0].get("interactions", 1) == 0)):
        st.info(f"No {what} match the current filters. Widen the date range or clear a segment filter.")
        return True
    return False


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------

if section == "Overview":
    header("Overview", "Product health at a glance")
    series = daily_series()
    agent = agent_rollup().iloc[0] if not agent_rollup().empty else None
    subs = subs_rollup().iloc[0] if not subs_rollup().empty else None
    ret = retention_rollup().iloc[0] if not retention_rollup().empty else None

    if empty_guard(series, "active users"):
        st.stop()

    lat = latency_percentiles()
    p95 = lat["p95"].quantile(.95) if not lat.empty else None
    adoption = series.agent_users.sum() / series.dau.sum() if series.dau.sum() else 0

    c = st.columns(6)
    with c[0]:
        kpi("Avg DAU", f"{series.dau.mean():,.0f}", f"peak {series.dau.max():,.0f}")
    with c[1]:
        kpi("Agent adoption", f"{adoption:.1%}", "of daily active users")
    with c[2]:
        overall_p95 = M.latency_values(_conn(), F)
        val = overall_p95.quantile(.95) if len(overall_p95) else 0
        kpi("p95 agent latency", f"{val:,.0f} ms",
            f"p50 {overall_p95.quantile(.50):,.0f} ms" if len(overall_p95) else "",
            tone="warning" if val > 6000 else "good")
    with c[3]:
        if agent is not None and agent.interactions:
            kpi("Agent resolution", f"{agent.resolved / agent.interactions:.1%}",
                f"escalation {agent.escalated / agent.interactions:.1%}")
        else:
            kpi("Agent resolution", "n/a", "no interactions")
    with c[4]:
        if subs is not None and subs.cohort:
            kpi("Paid conversion", f"{subs.paid_rate:.1%}", f"of {subs.cohort:,.0f} signups in range")
        else:
            kpi("Paid conversion", "n/a", "no signups in range")
    with c[5]:
        if ret is not None and ret.d7_elig:
            kpi("D7 retention", f"{ret.d7_rate:.1%}", f"{ret.d7_ret:,.0f}/{ret.d7_elig:,.0f} eligible")
        else:
            kpi("D7 retention", "n/a", "no cohort in range")

    st.markdown("")
    dq = M.dq_summary(_conn())
    passed, total = int(dq.checks_passed), int(dq.checks_total)
    st.markdown(
        f"""<div style="border:1px solid rgba(11,11,11,.10);border-left:3px solid
             {STATUS['good'] if passed == total else STATUS['critical']};border-radius:8px;
             padding:10px 14px;font-size:12px;color:{INK_SOFT};background:{SURFACE}">
             <b>Data quality</b> &nbsp;·&nbsp; {passed}/{total} validation checks passed
             &nbsp;·&nbsp; {dq.metric_events:,} events in metric layer
             &nbsp;·&nbsp; {dq.excluded_events:,} excluded as test / unknown user
             &nbsp;·&nbsp; {dq.quarantined_events:,} duplicate copies quarantined
             &nbsp;·&nbsp; {dq.latency_outliers:,} latency outliers flagged
        </div>""", unsafe_allow_html=True)

    st.markdown("")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Daily active users**")
        st.plotly_chart(line_chart(series, "metric_date", {"dau": "DAU"}, y_title="users"),
                        width="stretch")
    with right:
        st.markdown("**Agent adoption rate**")
        st.caption("Share of daily active users who sent at least one agent request. "
                   "Smoothed on a 7-day rolling mean — the daily series swings on small "
                   "denominators and hides the trend.")
        smooth = series.assign(
            rolling=lambda d: d.agent_adoption_rate.rolling(7, min_periods=3).mean())
        st.plotly_chart(
            line_chart(smooth, "metric_date",
                       {"agent_adoption_rate": "Daily", "rolling": "Adoption (7-day mean)"},
                       percent_y=True, y_title="of DAU", muted={"agent_adoption_rate"}),
            width="stretch")

    st.markdown("**Where to look next**")
    a, b, c2 = st.columns(3)
    top_country = segment_dau("country_code")
    top_channel = subs_rollup("acquisition_channel")
    worst_intent = agent_rollup("intent")
    with a:
        if not top_country.empty:
            st.caption(f"Largest market: **{top_country.iloc[0].seg}** at "
                       f"{top_country.iloc[0].avg_dau:,.0f} avg DAU")
    with b:
        if not top_channel.empty:
            st.caption(f"Best converting channel: **{top_channel.iloc[0].seg}** at "
                       f"{top_channel.iloc[0].paid_rate:.1%}")
    with c2:
        if not worst_intent.empty and worst_intent.interactions.sum():
            w = worst_intent.assign(esc=lambda d: d.escalated / d.interactions).sort_values("esc").iloc[-1]
            st.caption(f"Highest escalation intent: **{w.seg}** at {w.esc:.1%}")


# --------------------------------------------------------------------------
# Active Users
# --------------------------------------------------------------------------

elif section == "Active Users":
    header("Active Users", "Who is active, and where growth is coming from")
    series = daily_series()
    if empty_guard(series, "active users"):
        st.stop()

    granularity = st.radio("Granularity", ["Day", "Week", "Month"], horizontal=True, index=0)
    plot = series.copy()
    plot["metric_date"] = pd.to_datetime(plot.metric_date)
    if granularity != "Day":
        rule = {"Week": "W-MON", "Month": "MS"}[granularity]
        plot = (plot.set_index("metric_date")
                .resample(rule)
                .agg(dau=("dau", "mean"), agent_users=("agent_users", "mean"),
                     agent_interactions=("agent_interactions", "sum"))
                .reset_index())
        plot["agent_adoption_rate"] = plot.agent_users / plot.dau

    st.plotly_chart(line_chart(plot, "metric_date", {"dau": f"DAU ({granularity.lower()} average)"},
                               y_title="users", height=340), width="stretch")

    trough, peak = plot.loc[plot.dau.idxmin()], plot.loc[plot.dau.idxmax()]
    st.caption(
        f"Peak {peak.dau:,.0f} on {peak.metric_date:%Y-%m-%d} · "
        f"trough {trough.dau:,.0f} on {trough.metric_date:%Y-%m-%d} · "
        f"{(plot.dau.iloc[-1] / plot.dau.iloc[0] - 1):+.0%} first to last period"
    )

    st.markdown("---")
    st.markdown("**Segment comparison**")
    cols = st.columns(2)
    for i, dim in enumerate(["country_code", "device_os", "acquisition_channel", "app_version"]):
        seg = segment_dau(dim)
        with cols[i % 2]:
            st.markdown(f"*Average DAU by {SEGMENT_LABELS[dim].lower()}*")
            if seg.empty:
                st.info("No data for this cut.")
            else:
                st.plotly_chart(bar_chart(seg, "seg", "avg_dau", y_title="avg DAU", height=280,
                                          color=SERIES[i]), width="stretch",
                                key=f"dau_{dim}")

    st.markdown("---")
    st.markdown("**Agent adoption by segment**")
    st.caption("Distinct users with an agent request divided by DAU, same date and segment.")
    tabs = st.tabs([SEGMENT_LABELS[d] for d in SEGMENT_LABELS])
    for tab, dim in zip(tabs, SEGMENT_LABELS):
        with tab:
            seg = segment_dau(dim).sort_values("adoption", ascending=False)
            if seg.empty:
                st.info("No data for this cut.")
            else:
                st.plotly_chart(bar_chart(seg, "seg", "adoption", percent=True,
                                          y_title="adoption", height=280, color=SERIES[2]),
                                width="stretch", key=f"adopt_{dim}")
                table(
                    seg.rename(columns={"seg": SEGMENT_LABELS[dim], "avg_dau": "Avg DAU",
                                        "peak_dau": "Peak DAU", "adoption": "Agent adoption"}),
                    {"Avg DAU": "decimal1", "Peak DAU": "int", "Agent adoption": "percent"},
                    key=f"tbl_adopt_{dim}")


# --------------------------------------------------------------------------
# Agent Health
# --------------------------------------------------------------------------

elif section == "Agent Health":
    header("Agent Health", "Reliability, speed, resolution and cost of the AI study companion")
    overall = agent_rollup()
    if empty_guard(overall, "agent interactions"):
        st.stop()
    o = overall.iloc[0]

    c = st.columns(6)
    with c[0]:
        kpi("Interactions", f"{o.interactions:,.0f}", f"{o.users:,.0f} distinct users")
    with c[1]:
        kpi("Success rate", f"{o.successes / o.interactions:.1%}",
            f"{o.timeouts:,.0f} timeouts · {o.errors:,.0f} errors",
            tone="good" if o.successes / o.interactions > .95 else "warning")
    with c[2]:
        kpi("Resolution rate", f"{o.resolved / o.interactions:.1%}",
            f"{o.interactions - o.resolved - o.escalated:,.0f} neither resolved nor escalated")
    with c[3]:
        kpi("Escalation rate", f"{o.escalated / o.interactions:.1%}", "handed to a human",
            tone="warning" if o.escalated / o.interactions > .10 else "good")
    with c[4]:
        kpi("CSAT", f"{o.csat:.2f}/5" if pd.notna(o.csat) else "n/a",
            f"{o.rated / o.interactions:.0%} of interactions rated")
    with c[5]:
        kpi("Estimated cost", f"${o.cost_usd:,.2f}",
            f"{o.outliers:,.0f} latency outliers flagged")

    st.markdown("")
    left, right = st.columns(2)
    with left:
        st.markdown("**Latency over time**")
        st.caption("p50 and p95 in milliseconds. The mean is deliberately not shown: "
                   "a small cluster of >30s outliers inflates it.")
        lat = latency_percentiles()
        if not lat.empty:
            st.plotly_chart(line_chart(lat, "period", {"p50": "p50", "p95": "p95"},
                                       y_title="latency (ms)"), width="stretch")
    with right:
        st.markdown("**Response status mix**")
        st.caption("Every timeout and error is escalated to a human.")
        mix = M.status_mix(_conn(), F)
        if not mix.empty:
            st.plotly_chart(
                stacked_bar(mix, "period", {"success": "Success", "timeout": "Timeout", "error": "Error"},
                            y_title="interactions"), width="stretch")

    st.markdown("---")
    st.markdown("**Quality by request intent**")
    by_intent = agent_rollup("intent")
    if not by_intent.empty:
        by_intent = by_intent.assign(
            resolution=lambda d: d.resolved / d.interactions,
            escalation=lambda d: d.escalated / d.interactions,
        )
        a, b = st.columns(2)
        with a:
            st.plotly_chart(bar_chart(by_intent.sort_values("resolution"), "seg", "resolution",
                                      percent=True, y_title="resolution", height=300, color=SERIES[2]),
                            width="stretch", key="res_intent")
            st.caption("Resolution rate by intent (higher is better)")
        with b:
            st.plotly_chart(bar_chart(by_intent.sort_values("escalation", ascending=False), "seg",
                                      "escalation", percent=True, y_title="escalation", height=300,
                                      color=SERIES[1]), width="stretch", key="esc_intent")
            st.caption("Escalation rate by intent (lower is better)")
        table(
            by_intent.rename(columns={
                "seg": "Intent", "interactions": "Interactions", "users": "Users",
                "resolution": "Resolution", "escalation": "Escalation", "csat": "CSAT",
                "cost_usd": "Est. cost"})[
                ["Intent", "Interactions", "Users", "Resolution", "Escalation", "CSAT", "Est. cost"]],
            {"Interactions": "int", "Users": "int", "Resolution": "percent",
             "Escalation": "percent", "CSAT": "decimal", "Est. cost": "dollar"},
            key="tbl_intent")

    st.markdown("---")
    a, b = st.columns(2)
    with a:
        st.markdown("**Tool use mix**")
        tools = agent_rollup("tool_used")
        if not tools.empty:
            st.plotly_chart(bar_chart(tools, "seg", "interactions", y_title="interactions",
                                      height=300, color=SERIES[6]), width="stretch",
                            key="tool_mix")
            st.caption("`no_tool` means the agent answered directly; `unrecorded` means the "
                       "tool field was missing in the source data.")
    with b:
        st.markdown("**Latency distribution**")
        raw = M.latency_raw(_conn(), F)
        if not raw.empty:
            inliers = raw[raw.is_latency_outlier == 0]
            fig = go.Figure(go.Histogram(
                x=inliers.latency_ms, nbinsx=40,
                marker=dict(color=SERIES[0], line=dict(width=1, color=SURFACE)),
                hovertemplate="%{x} ms<br>%{y} interactions<extra></extra>"))
            fig.update_layout(hovermode="closest")
            st.plotly_chart(style(fig, 300, y_title="interactions"), width="stretch")
            st.caption(f"Excludes {int(raw.is_latency_outlier.sum())} flagged outliers above 30,000 ms "
                       f"so the shape of normal performance stays readable.")


# --------------------------------------------------------------------------
# Subscriptions & Retention
# --------------------------------------------------------------------------

elif section == "Subscriptions & Retention":
    header("Subscriptions & Retention", "Trial, conversion, cancellation and return behaviour")
    st.info(
        "**Cohort note.** Subscription figures below describe users who **signed up** inside the "
        "selected range (signups run to 2026-03-20), not users active in it. Retention describes "
        "users whose **first app activity** falls in the range.",
        icon="ℹ️",
    )

    subs = subs_rollup()
    if subs.empty or not subs.iloc[0].cohort:
        st.warning("No users signed up inside the selected range, so there is no cohort to measure. "
                   "Widen the date range - signups run 2026-01-01 to 2026-03-20.")
    else:
        s = subs.iloc[0]
        c = st.columns(5)
        with c[0]:
            kpi("Signup cohort", f"{s.cohort:,.0f}", f"{START} → {END}")
        with c[1]:
            kpi("Trial starts", f"{s.trials:,.0f}", f"{s.trial_rate:.1%} of cohort")
        with c[2]:
            kpi("Paid conversions", f"{s.paid:,.0f}", f"{s.paid_rate:.1%} of cohort")
        with c[3]:
            kpi("Cancellations", f"{s.cancelled:,.0f}",
                f"{s.cancel_rate:.1%} of paid" if pd.notna(s.cancel_rate) else "",
                tone="warning" if pd.notna(s.cancel_rate) and s.cancel_rate > .15 else None)
        with c[4]:
            kpi("Active MRR", f"${s.mrr:,.0f}", f"{s.still_paying:,.0f} still paying")

        st.warning(
            "**Known data artifact.** Every trial in this dataset has a `paid_started_at` exactly "
            "7 days after its `trial_started_at`, with zero variance — so trial-to-paid conversion "
            "computes to 100%. That is not a product result. Conversion is therefore measured "
            "against the **signup cohort**, and the 7-day trial field should be treated as a "
            "scheduled date rather than an observed conversion until the source is confirmed.",
            icon="⚠️",
        )

        st.markdown("---")
        st.markdown("**Conversion by segment**")
        tabs = st.tabs([SEGMENT_LABELS[d] for d in SEGMENT_LABELS])
        for tab, dim in zip(tabs, SEGMENT_LABELS):
            with tab:
                seg = subs_rollup(dim)
                if seg.empty:
                    st.info("No data for this cut.")
                    continue
                a, b = st.columns(2)
                with a:
                    st.plotly_chart(bar_chart(seg, "seg", "paid_rate", percent=True,
                                              y_title="paid conversion", height=300, color=SERIES[0]),
                                    width="stretch", key=f"conv_{dim}")
                    st.caption("Paid conversion rate of the signup cohort")
                with b:
                    st.plotly_chart(bar_chart(seg.sort_values("cancel_rate", ascending=False), "seg",
                                              "cancel_rate", percent=True, y_title="cancellation",
                                              height=300, color=SERIES[1]),
                                    width="stretch", key=f"canc_{dim}")
                    st.caption("Cancellation rate among paid users")
                small = seg[seg.cohort < 30]
                if not small.empty:
                    st.caption(f"⚠️ {', '.join(small.seg.astype(str))} have fewer than 30 users in "
                               "the cohort; treat those rates as provisional.")
                table(
                    seg.rename(columns={"seg": SEGMENT_LABELS[dim], "cohort": "Cohort",
                                        "trials": "Trials", "paid": "Paid", "cancelled": "Cancelled",
                                        "trial_rate": "Trial rate", "paid_rate": "Paid conversion",
                                        "cancel_rate": "Cancel rate of paid", "mrr": "Active MRR"})[
                        [SEGMENT_LABELS[dim], "Cohort", "Trials", "Paid", "Cancelled",
                         "Trial rate", "Paid conversion", "Cancel rate of paid", "Active MRR"]],
                    {"Cohort": "int", "Trials": "int", "Paid": "int", "Cancelled": "int",
                     "Trial rate": "percent", "Paid conversion": "percent",
                     "Cancel rate of paid": "percent", "Active MRR": "dollar"},
                    key=f"tbl_subs_{dim}")

    st.markdown("---")
    st.markdown("**Return retention**")
    st.caption("Day-N return: active on exactly first activity date + N. Users whose day-N date "
               "falls outside the observed window are excluded from the denominator, not counted as churned.")
    ret = retention_rollup()
    if ret.empty or not ret.iloc[0].users:
        st.info("No users had their first activity inside the selected range.")
    else:
        r = ret.iloc[0]
        c = st.columns(3)
        with c[0]:
            kpi("Cohort", f"{r.users:,.0f}", "users first active in range")
        with c[1]:
            kpi("D1 retention", f"{r.d1_rate:.1%}" if pd.notna(r.d1_rate) else "n/a",
                f"{r.d1_ret:,.0f} of {r.d1_elig:,.0f} eligible")
        with c[2]:
            kpi("D7 retention", f"{r.d7_rate:.1%}" if pd.notna(r.d7_rate) else "n/a",
                f"{r.d7_ret:,.0f} of {r.d7_elig:,.0f} eligible")

        tabs = st.tabs([SEGMENT_LABELS[d] for d in SEGMENT_LABELS])
        for tab, dim in zip(tabs, SEGMENT_LABELS):
            with tab:
                seg = retention_rollup(dim)
                if seg.empty:
                    st.info("No data for this cut.")
                    continue
                fig = go.Figure()
                for i, (col, label) in enumerate({"d1_rate": "D1", "d7_rate": "D7"}.items()):
                    fig.add_trace(go.Bar(
                        x=seg.seg, y=seg[col], name=label,
                        marker=dict(color=SERIES[i], cornerradius=4, line=dict(width=2, color=SURFACE)),
                        hovertemplate=f"<b>%{{x}}</b><br>{label}: %{{y:.1%}}<extra></extra>"))
                fig.update_layout(barmode="group", hovermode="closest")
                st.plotly_chart(style(fig, 300, percent_y=True, y_title="retention"),
                                width="stretch", key=f"ret_{dim}")
                table(
                    seg.rename(columns={"seg": SEGMENT_LABELS[dim], "users": "Cohort",
                                        "d1_rate": "D1 retention", "d7_rate": "D7 retention",
                                        "d7_elig": "D7 eligible"})[
                        [SEGMENT_LABELS[dim], "Cohort", "D1 retention", "D7 retention", "D7 eligible"]],
                    {"Cohort": "int", "D1 retention": "percent", "D7 retention": "percent",
                     "D7 eligible": "int"},
                    key=f"tbl_ret_{dim}")


# --------------------------------------------------------------------------
# Ask Navi Analytics Agent
# --------------------------------------------------------------------------

elif section == "Ask Navi Analytics Agent":
    from analytics_agent import MODEL, SMOKE_QUESTIONS, agent_backend, run_analytics_agent

    header("Ask Navi Analytics Agent", "Natural-language questions answered from this same warehouse")

    backend = agent_backend()
    context = {"start_date": START, "end_date": END, **active_filters}

    if backend == "openai":
        st.caption(f"🟢 Live tool calling via OpenAI `{MODEL}`. The model selects an approved "
                   "read-only tool; it never writes SQL and never sees the database.")
    else:
        st.warning(
            "No `OPENAI_API_KEY` found, so the agent is running its **deterministic planner**: "
            "the same validated tools and the same audit trail, with rule-based tool selection "
            "instead of a language model. Add a key to `.env` and restart for live LLM calls.",
            icon="🔌",
        )

    with st.expander("How this works, and what the agent is allowed to do", expanded=False):
        st.markdown("""
**Flow.** question → the model picks one approved tool → Python validates every argument →
parameterized read-only SQL runs against the curated warehouse → the aggregate comes back →
the model explains it → the audit below shows exactly what ran.

**Guardrails.**
- The model never writes SQL and never receives database access.
- Grouping dimensions and filter keys come from a fixed allow-list; anything else is rejected.
- Dates are format- and range-validated; every value is bound as a SQL parameter.
- The connection is opened read-only, so a write is impossible.
- Tools return aggregates only, capped at 200 rows. No user-level record is ever returned.

**Consistency.** The agent receives the sidebar's date range and segment filters as its default
context, and reads the same marts as the charts, so a number it reports can be reproduced here.
        """)

    st.caption(f"Dashboard context passed to the agent: **{START} → {END}** · {filter_note}")

    if "chat" not in st.session_state:
        st.session_state.chat = []

    st.markdown("**Try one of the sample questions**")
    cols = st.columns(3)
    for i, sample in enumerate(SMOKE_QUESTIONS[:6]):
        if cols[i % 3].button(sample, key=f"sample_{i}", width="stretch"):
            st.session_state.pending = sample

    typed = st.chat_input("Ask about DAU, agent health, retention, or subscriptions")
    question = typed or st.session_state.pop("pending", None)

    for turn in st.session_state.chat:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("audit"):
                with st.expander("🔍 Tool audit"):
                    for entry in turn["audit"]:
                        if "tool" in entry:
                            st.markdown(f"**`{entry['tool']}`** — {entry['result_summary']} "
                                        f"· {entry['latency_ms']:.0f} ms")
                            st.code(str(entry["arguments"]), language="python")
                            if entry.get("date_range"):
                                st.caption(f"metric: {entry.get('metric')} · "
                                           f"range: {entry['date_range']['start']} → "
                                           f"{entry['date_range']['end']}")
                        else:
                            st.markdown("**Run summary**")
                            st.json(entry)
                    with st.expander("Raw tool results (JSON)"):
                        st.json([e.get("result") for e in turn["audit"] if "result" in e])

    if question:
        st.session_state.chat.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Selecting a tool and querying the warehouse…"):
                answer, audit = run_analytics_agent(question, context)
            st.markdown(answer)
            final = next((a for a in audit if a.get("summary") == "run complete"), {})
            tools_used = [a["tool"] for a in audit if "tool" in a]
            st.caption(
                f"tools: {', '.join(tools_used) or 'none'} · "
                f"api calls: {final.get('api_calls', 0)} · "
                f"cost: ${final.get('estimated_cost_usd', 0):.5f} · "
                f"{final.get('total_latency_ms', 0):.0f} ms"
            )
        st.session_state.chat.append({"role": "assistant", "content": answer, "audit": audit})
        st.rerun()


# --------------------------------------------------------------------------
# Data Quality
# --------------------------------------------------------------------------

else:
    header("Data Quality", "Every exclusion, decision and validation check, from the warehouse itself")
    st.caption("This section is generated from the warehouse, not written by hand — "
               "`dq_decisions`, `dq_validation_results` and `dq_profile_raw` are ETL outputs.")

    checks = q("SELECT check_name, expected, actual, status, note FROM dq_validation_results")
    passed = int((checks.status == "PASS").sum())
    c = st.columns(4)
    dq = M.dq_summary(_conn())
    with c[0]:
        kpi("Validation checks", f"{passed}/{len(checks)}", "SQL vs independent pandas",
            tone="good" if passed == len(checks) else "critical")
    with c[1]:
        kpi("Events in metric layer", f"{dq.metric_events:,}", f"{dq.excluded_events:,} excluded")
    with c[2]:
        kpi("Quarantined", f"{dq.quarantined_events:,}", "duplicate event copies")
    with c[3]:
        kpi("Latency outliers", f"{dq.latency_outliers:,}", "flagged, kept, excluded from charts")

    st.markdown("---")
    st.markdown("**Raw source profile**")
    table(q("SELECT * FROM dq_profile_raw"), key="tbl_profile")

    st.markdown("**Decisions taken, and their impact**")
    for _, row in q("SELECT * FROM dq_decisions").iterrows():
        with st.expander(row.decision):
            st.markdown(f"**Rule.** {row.rule}")
            st.markdown(f"**Impact.** {row.impact}")

    st.markdown("**Validation checks**")
    table(checks, key="tbl_checks")

    st.markdown("**Quarantined records** (duplicate event ids, kept for audit)")
    table(q("SELECT * FROM dq_quarantine_events LIMIT 50"), key="tbl_quarantine")

    st.markdown("**Metric glossary** — the definitions the dashboard and the agent share")
    table(q("SELECT metric_name, implemented_definition, warehouse_source FROM metric_definitions"),
          key="tbl_glossary")
