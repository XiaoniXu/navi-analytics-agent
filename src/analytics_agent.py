"""LLM orchestration for the Navi Analytics Agent.

Flow
----
    question + dashboard context
      -> the model picks one of the approved tools (it never writes SQL)
      -> agent_tools validates the arguments and runs read-only parameterized SQL
      -> the aggregate result goes back to the model
      -> the model explains it in plain language
      -> the UI shows a compact audit of every tool call

Two backends implement the same interface:

    OpenAIAgent    the production path, using the OpenAI Responses API.
    OfflineAgent   a deterministic rule-based planner used when no API key is
                   configured. It calls the exact same validated tools and
                   produces the same audit trail, so the dashboard stays usable
                   and the tool layer stays testable without network access.

run_analytics_agent() picks the backend automatically.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_tools import TOOLS, dispatch_tool, get_data_coverage  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
MAX_TOOL_ROUNDS = 4
MAX_OUTPUT_TOKENS = 700

# Per-1M-token prices used only to show the reviewer what a question costs.
AGENT_MODEL_PRICING = {
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}

SYSTEM_INSTRUCTIONS = """
You are Navi Analytics Agent, an in-dashboard assistant for product analytics.

Rules you must follow:
- Every number you state must come from a tool result. Never estimate, never
  recall a figure from earlier in the conversation, never do arithmetic the
  tools did not do. If a tool returns an error, say so plainly.
- The data does not cover the present day. For any relative phrase such as
  "last week", "recently", "this month" or "latest", call get_data_coverage
  first and anchor the range to the warehouse's last_date.
- Prefer one well-chosen tool call. Add a second only when the question truly
  needs it, such as a month-over-month comparison.
- Use the dashboard context as the default date range and filters when the
  question does not specify its own. Say which range and filters you used.
- Every data-backed answer states: the metric, the date range, the filters,
  and the result. Lead with the answer, then the supporting numbers.
- Surface any caveat the tool returns in its metadata. These matter: CSAT
  covers only rated interactions, resolution and escalation do not sum to 1,
  and paid conversion in this dataset has a known artifact.
- If a question is genuinely ambiguous, ask one short clarifying question
  instead of guessing.
- If no tool can answer the question, say what the warehouse does and does not
  contain. Never answer from outside the warehouse.
- You return aggregate product analytics only. Refuse requests for individual
  user records or personal data.
- Be concise: two to five sentences, plus a small table when comparing segments.
""".strip()


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _audit_entry(name: str, args: dict, result: dict, ms: float) -> dict[str, Any]:
    """One compact, reviewable record of a tool call."""
    if isinstance(result, dict) and "error" in result:
        summary = f"ERROR: {result['error']}"
    elif isinstance(result, dict) and "results" in result:
        summary = f"{result.get('row_count', len(result['results']))} aggregate row(s)"
    else:
        summary = "ok"
    return {
        "tool": name,
        "arguments": {k: v for k, v in args.items() if v not in (None, "")},
        "result_summary": summary,
        "metric": (result.get("metadata") or {}).get("metric") if isinstance(result, dict) else None,
        "date_range": (result.get("metadata") or {}).get("date_range") if isinstance(result, dict) else None,
        "latency_ms": round(ms, 1),
        "result": result,
    }


def _context_preamble(dashboard_context: dict[str, Any]) -> str:
    if not dashboard_context:
        return ""
    return (
        "Current dashboard state (use as the default range and filters unless the "
        f"question overrides them): {json.dumps(dashboard_context, default=str)}"
    )


# --------------------------------------------------------------------------
# Production backend: OpenAI tool calling
# --------------------------------------------------------------------------

def _run_openai(
    question: str, dashboard_context: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    from openai import OpenAI

    client = OpenAI()
    audit: list[dict[str, Any]] = []
    started = time.perf_counter()
    usage = {"input_tokens": 0, "output_tokens": 0, "model": MODEL, "api_calls": 0}

    parts = [p for p in (_context_preamble(dashboard_context), f"Question: {question}") if p]
    input_items: list[Any] = [{"role": "user", "content": "\n".join(parts)}]

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.responses.create(
            model=MODEL,
            instructions=SYSTEM_INSTRUCTIONS,
            tools=TOOLS,
            input=input_items,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        usage["api_calls"] += 1
        if getattr(response, "usage", None):
            usage["input_tokens"] += getattr(response.usage, "input_tokens", 0) or 0
            usage["output_tokens"] += getattr(response.usage, "output_tokens", 0) or 0

        input_items.extend(response.output)
        calls = [item for item in response.output if item.type == "function_call"]

        if not calls:
            audit.append(_finalize(usage, started))
            return response.output_text, audit

        for call in calls:
            t0 = time.perf_counter()
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                result = {"error": f"Model sent malformed arguments: {exc}"}
                arguments = {"_raw": call.arguments}
            else:
                result = dispatch_tool(call.name, arguments)
            audit.append(_audit_entry(call.name, arguments, result, (time.perf_counter() - t0) * 1000))
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, default=str),
                }
            )

    audit.append(_finalize(usage, started))
    return (
        "I reached the tool-call limit for this question. Try narrowing it, for example "
        "by naming a single metric and date range.",
        audit,
    )


def _finalize(usage: dict[str, Any], started: float) -> dict[str, Any]:
    price = AGENT_MODEL_PRICING.get(usage["model"], {"input": 0.0, "output": 0.0})
    cost = (
        usage["input_tokens"] / 1_000_000 * price["input"]
        + usage["output_tokens"] / 1_000_000 * price["output"]
    )
    return {
        "summary": "run complete",
        "backend": "openai",
        "model": usage["model"],
        "api_calls": usage["api_calls"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "estimated_cost_usd": round(cost, 6),
        "total_latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


# --------------------------------------------------------------------------
# Offline backend: deterministic rule-based planner
# --------------------------------------------------------------------------
#
# This is not a hard-coded FAQ. It resolves a question to the same validated
# tools, with the same argument validation and the same audit trail; only the
# tool-selection step is rules instead of a language model. It exists so the
# dashboard degrades gracefully without an API key, and so the whole pipeline
# can be tested without network access.

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

GROUP_PATTERNS = [
    (r"\bcountr(y|ies)\b|\bmarket", "country_code"),
    (r"\bdevice|\boperating system|\bos\b|\bios\b|\bandroid\b", "device_os"),
    (r"\bchannel|\bacquisition|\bsource\b", "acquisition_channel"),
    (r"\bapp version|\bversion\b", "app_version"),
    (r"\bintent\b|\brequest type", "intent"),
    (r"\btool\b|\btool use|\btool mix", "tool_used"),
    (r"\bmodel\b|\brouting", "model_name"),
    (r"\bstatus\b|\btimeout|\berror\b", "response_status"),
]

FILTER_VALUES = {
    "country_code": {"us": "US", "united states": "US", "uk": "UK", "united kingdom": "UK",
                     "ca": "CA", "canada": "CA", "au": "AU", "australia": "AU"},
    "device_os": {"ios": "iOS", "iphone": "iOS", "android": "Android"},
    "acquisition_channel": {"organic": "Organic", "paid social": "Paid Social", "search": "Search",
                            "referral": "Referral", "influencer": "Influencer"},
    "intent": {"homework help": "Homework Help", "summarization": "Summarization",
               "quiz generation": "Quiz Generation", "study plan": "Study Plan",
               "general q&a": "General Q&A", "flashcards": "Flashcards"},
}


def _resolve_dates(question: str, context: dict[str, Any], coverage: dict[str, Any]) -> tuple[str, str]:
    """Resolve a date range from the question, then the dashboard, then coverage."""
    q = question.lower()
    first = datetime.strptime(coverage["first_date"], "%Y-%m-%d").date()
    last = datetime.strptime(coverage["last_date"], "%Y-%m-%d").date()

    explicit = re.findall(r"\d{4}-\d{2}-\d{2}", question)
    if len(explicit) >= 2:
        return explicit[0], explicit[1]

    for name, num in MONTHS.items():
        if re.search(rf"\b{name}\b", q):
            year = last.year
            start = date(year, num, 1)
            end = date(year + (num == 12), (num % 12) + 1, 1) - timedelta(days=1)
            return start.isoformat(), min(end, last).isoformat()

    if "last week" in q or "past week" in q or "previous week" in q:
        return (last - timedelta(days=6)).isoformat(), last.isoformat()
    if "last month" in q or "past month" in q:
        return (last - timedelta(days=29)).isoformat(), last.isoformat()
    if "last 30" in q or "past 30" in q:
        return (last - timedelta(days=29)).isoformat(), last.isoformat()
    if "yesterday" in q or "last day" in q:
        return last.isoformat(), last.isoformat()

    if context.get("start_date") and context.get("end_date"):
        return str(context["start_date"]), str(context["end_date"])
    return first.isoformat(), last.isoformat()


def _detect_group(question: str, allowed: list[str]) -> str | None:
    q = question.lower()
    if not re.search(r"\bby\b|\bwhich\b|\bcompare|\bhighest|\blowest|\bbest|\bworst|\bper\b|\bmix\b", q):
        return None
    for pattern, dim in GROUP_PATTERNS:
        if re.search(pattern, q) and dim in allowed:
            return dim
    return None


def _detect_filters(question: str, group_by: str | None) -> dict[str, str]:
    q = question.lower()
    out: dict[str, str] = {}
    for dim, mapping in FILTER_VALUES.items():
        if dim == group_by:
            continue
        for token, value in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{re.escape(token)}\b", q):
                out[dim] = value
                break
    return out


# Requests for individual records or personal data are out of scope by design:
# the tool layer returns aggregates only, so these are refused before any query.
OUT_OF_SCOPE = re.compile(
    r"\bemail|\bphone number|\baddress(es)?\b|\bpersonal data\b|\bpii\b|\bindividual users?\b|"
    r"\bnames? of (the )?users?\b|\bwho (is|are) (the|our)\b|\blist (the|our|all)? ?users?\b|"
    r"\bspecific users?\b|\buser ids?\b|\braw (event|record|row)s?\b|\bexport .* users?\b|"
    r"\b(show|give|get) me (all|the|a list of) users?\b|\ball users who\b|\bwhich users\b|"
    r"\bevery user who\b|\buser[- ]level\b",
    re.IGNORECASE,
)

# Forecasting is outside the tool set. Saying so is the correct answer; guessing is not.
FORECAST = re.compile(
    r"\bforecast|\bpredict|\bprojection|\bproject(ed)? \w+ (for|in|next)|\bwill .* be (next|in)\b|"
    r"\bnext (month|week|quarter|year)\b|\bexpected .* (next|future)|\bhow many .* will\b",
    re.IGNORECASE,
)

# A question with no metric and no dimension cannot be routed. One short clarifying
# question beats silently answering something adjacent.
VAGUE = re.compile(
    r"^\s*(what|which|who)\s+(is|are|was|were)\s+the\s+"
    r"(best|worst|top|biggest|strongest|weakest|good|bad)\s+"
    r"(segment|one|cut|group|option|country|channel|device|market)?\s*\??\s*$",
    re.IGNORECASE,
)


def _plan(question: str, context: dict[str, Any], coverage: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Choose one tool and its arguments from the question text."""
    q = question.lower()

    if OUT_OF_SCOPE.search(q):
        return "_refuse", {}
    if FORECAST.search(q):
        return "_no_forecast", {}
    if VAGUE.search(question.strip()):
        return "_clarify", {}

    if re.search(r"what does .* mean|\bdefinition\b|\bdefine\b|how is .* (calculated|computed)|"
                 r"what do you mean by|\bmetric definition\b", q):
        name = None
        for candidate in ["agent adoption rate", "paid conversion rate", "trial start rate",
                          "agent resolution rate", "escalation rate", "d1 retention",
                          "d7 retention", "csat", "dau", "p95 agent latency"]:
            if candidate in q:
                name = candidate
                break
        return "get_metric_definition", {"metric_name": name}

    if re.search(r"\bcoverage\b|what data|date range of the data|how much data", q):
        return "get_data_coverage", {}

    segments = ["country_code", "device_os", "acquisition_channel", "app_version"]

    # Whether the question names its own period at all. If it does not, the right
    # default depends on the topic: engagement questions follow the dashboard's
    # current range, but cohort questions (subscriptions, retention) and
    # over-time questions need the full window or they land on an empty or
    # one-bucket cohort.
    names_a_period = bool(
        re.search(r"\d{4}-\d{2}-\d{2}|last week|past week|previous week|last month|past month|"
                  r"last 30|past 30|yesterday|last day", q)
        or any(re.search(rf"\b{m}\b", q) for m in MONTHS)
    )
    # "Paid Social" is a channel name, not the word "paid" - strip it before topic detection
    # so "DAU from Paid Social users" is an engagement question, not a revenue question.
    qt = re.sub(r"paid social", "__channel__", q)
    cohort_topic = re.search(
        r"\btrial\b|\bconversion\b|\bconvert\b|\bpaid\b|\bsubscri|\bcancel|\bchurn\b|"
        r"\brevenue\b|\bmrr\b|\bretention\b|\bretained\b|\bd1\b|\bd7\b|\bcohort\b", qt)
    over_time = re.search(r"\bmonth over month\b|\bmom\b|\bweek over week\b|\bwow\b|"
                          r"\bover time\b|\btrend\b|\bby month\b|\bby week\b", q)

    if not names_a_period and (cohort_topic or over_time):
        start, end = coverage["first_date"], coverage["last_date"]
    else:
        start, end = _resolve_dates(question, context, coverage)
    base = {"start_date": start, "end_date": end}

    if re.search(r"\bretention\b|\bretained\b|\bd1\b|\bd7\b|\bcome back\b|\bsticky\b|\bstickiness\b", qt):
        group = _detect_group(question, segments)
        args = {**base, "group_by": group, **{s: None for s in segments}}
        args.update(_detect_filters(question, group))
        return "get_retention_by_group", args

    if re.search(r"\btrial\b|\bconversion\b|\bconvert\b|\bpaid\b|\bsubscri|\bcancel|\bchurn\b"
                 r"|\brevenue\b|\bmrr\b|\bpaywall\b", qt):
        group = _detect_group(question, segments)
        # "How many trials started in April" asks about events in the window; "which
        # channel converts best" asks about a signup cohort. Counting words decide.
        counting = re.search(r"\bhow many\b|\bnumber of\b|\bcount\b|\btotal\b|\bhow much\b", q)
        basis = "event" if counting and names_a_period else "signup"
        args = {**base, "group_by": group, "date_basis": basis, **{s: None for s in segments}}
        args.update(_detect_filters(question, group))
        return "get_subscription_metrics_by_group", args

    agent_topic = re.search(
        r"\blatency\b|\bp95\b|\bp50\b|\bresolution\b|\bresolved\b|\bescalat|\bcsat\b|"
        r"\bsatisfaction\b|\btimeout\b|\berror\b|\bagent (health|quality|volume|cost)\b|"
        r"\bintent\b|\btool use\b|\btoken\b|\bhandoff\b", q)
    adoption_topic = re.search(r"\badoption\b|\bengage\b|\buse the (ai|agent|companion)\b", q)

    if agent_topic or adoption_topic:
        allowed = segments + ["intent", "tool_used", "model_name", "response_status", "month", "week"]
        group = _detect_group(question, allowed)
        if over_time and agent_topic:
            # "How did p95 latency change month over month?" needs time buckets,
            # not a single total for the whole range.
            group = "week" if re.search(r"\bweek\b", q) else "month"
        if adoption_topic and not agent_topic:
            args = {**base, "group_by": group if group in segments else None,
                    **{s: None for s in segments}}
            args.update(_detect_filters(question, group))
            args["_focus"] = "adoption"
            return "get_dau_by_group", args
        args = {**base, "group_by": group, "intent": None, **{s: None for s in segments}}
        args.update(_detect_filters(question, group))
        # Rank by what was asked, not by volume: "which intent has the highest escalation
        # rate" must not be answered with "the one with the most interactions".
        for pattern, metric in [
            (r"\bescalat", "escalation_rate"),
            (r"\bresolution\b|\bresolved\b", "resolution_rate"),
            (r"\bp95\b|\blatency\b|\bslow", "p95_latency_ms"),
            (r"\bcsat\b|\bsatisfaction\b", "csat"),
            (r"\btimeout|\berror\b|\bfail", "success_rate"),
            (r"\bcost\b|\btoken", "estimated_cost_usd"),
        ]:
            if re.search(pattern, q):
                args["_focus"] = metric
                break
        if re.search(r"\bpercentage\b|\bwhat (share|percent)\b|\bhow many .* rated\b", q) and \
                re.search(r"\bsatisfaction\b|\brated\b|\bcsat\b", q):
            args["_focus"] = "csat_response_rate"
        return "get_agent_health_by_group", args

    if re.search(r"\btrend\b|\bover time\b|\bmonth over month\b|\bmom\b|\bweek over week\b|"
                 r"\bgrow|\bchange(d)? (over|by|from)\b|\bby month\b|\bby week\b", q):
        gran = "month" if re.search(r"month", q) else "week" if re.search(r"week", q) else "day"
        args = {**base, "granularity": gran, **{s: None for s in segments}}
        args.update(_detect_filters(question, None))
        return "get_dau_trend", args

    group = _detect_group(question, segments)
    args = {**base, "group_by": group, **{s: None for s in segments}}
    args.update(_detect_filters(question, group))
    return "get_dau_by_group", args


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if abs(value) < 1000 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _cell(column: str, value: Any) -> str:
    """Rates render as percentages so a reader never misreads 0.29 as 29 users."""
    if value is None:
        return "n/a"
    if ("_rate" in column or column.endswith("_pct")) and isinstance(value, (int, float)):
        return f"{value:.1%}"
    if column.endswith("_usd") and isinstance(value, (int, float)):
        return f"${value:,.2f}"
    return _fmt(value)


def _table(rows: list[dict], columns: list[str], limit: int = 8) -> str:
    cols = [c for c in columns if any(c in r for r in rows)]
    if not cols or not rows:
        return ""
    head = "| " + " | ".join(c.replace("_", " ") for c in cols) + " |"
    rule = "| " + " | ".join("---" for _ in cols) + " |"
    body = ["| " + " | ".join(_cell(c, r.get(c)) for c in cols) + " |" for r in rows[:limit]]
    return "\n".join([head, rule, *body])


def _small_sample_note(rows: list[dict], denominator: str, threshold: int = 30) -> str:
    """Warn when a segment comparison rests on too few users to be decision-grade."""
    small = [r["segment"] for r in rows if (r.get(denominator) or 0) < threshold]
    if not small:
        return ""
    if len(small) == len(rows):
        return (f"\n\n⚠️ Every segment here has fewer than {threshold} users in the denominator, "
                "so these rates are noise rather than signal. Widen the date range.")
    return (f"\n\n⚠️ {', '.join(small)} " + ("has" if len(small) == 1 else "have")
            + f" fewer than {threshold} users in the denominator; treat "
            + ("that rate" if len(small) == 1 else "those rates") + " as provisional.")


def _explain(tool: str, args: dict, result: dict, question: str, focus: str | None = None) -> str:
    """Compose a grounded answer from the aggregate result."""
    if "error" in result:
        return f"I could not answer that: {result['error']}"

    meta = result.get("metadata", {})
    rng = meta.get("date_range", {})
    window = f"{rng.get('start')} to {rng.get('end')}" if rng else "the full data window"
    filters = meta.get("filters_applied")
    filter_text = ""
    if isinstance(filters, dict) and filters:
        filter_text = " (filtered to " + ", ".join(f"{k.replace('_',' ')} = {v}" for k, v in filters.items()) + ")"
    rows = result.get("results", [])

    if tool == "get_metric_definition":
        if "available_metrics" in result and not rows:
            return "Documented metrics in this warehouse: " + ", ".join(result["available_metrics"]) + "."
        r = rows[0]
        out = f"**{r['metric_name']}** — {r['implemented_definition']} (source: `{r['warehouse_source']}`)"
        if r.get("notes"):
            out += f"\n\nNote: {r['notes']}"
        return out

    if tool == "get_data_coverage":
        return (
            f"The warehouse covers {result['first_date']} to {result['last_date']} "
            f"({result['days_covered']} days) across {result['registered_users']:,} registered users. "
            f"{result['data_quality']['events_in_metric_layer']:,} events are in the metric layer; "
            f"{result['data_quality']['events_excluded_as_test_or_unknown_user']:,} were excluded as test "
            f"or unknown-user traffic and {result['data_quality']['duplicate_event_copies_quarantined']:,} "
            f"duplicate copies were quarantined. Validation: "
            f"{result['data_quality']['validation_checks_passed']} checks passed."
        )

    if not rows:
        if tool == "get_subscription_metrics_by_group":
            return (
                f"No users signed up between {window}{filter_text}, so there is no cohort to "
                "measure conversion for. This tool measures the signup cohort in the range, not "
                "users active in it - try a range inside the signup window."
            )
        if tool == "get_retention_by_group":
            return (
                f"No users had their first app activity between {window}{filter_text}, so there "
                "is no cohort to measure retention for."
            )
        return f"No data matched that request for {window}{filter_text}."

    if tool == "get_dau_by_group":
        if len(rows) == 1 and rows[0]["segment"] == "all users":
            r = rows[0]
            return (
                f"DAU averaged **{_fmt(r['avg_dau'])}** over {window}{filter_text}, peaking at "
                f"{_fmt(r['peak_dau'])}. {_fmt(r['distinct_users_in_period'])} distinct users were active "
                f"at least once, and {r['agent_adoption_rate']:.1%} of active users engaged the AI "
                f"study companion."
            )
        if focus == "adoption":
            ranked = sorted(rows, key=lambda r: r.get("agent_adoption_rate") or 0, reverse=True)
            top = ranked[0]
            body = (
                f"Over {window}{filter_text}, **{top['segment']}** had the highest agent adoption "
                f"rate at {top['agent_adoption_rate']:.1%} of its daily active users"
            )
            if len(ranked) > 1:
                body += f", against {ranked[1]['agent_adoption_rate']:.1%} for {ranked[1]['segment']}"
            return body + ".\n\n" + _table(
                ranked, ["segment", "agent_adoption_rate", "avg_dau", "distinct_users_in_period"]
            ) + _small_sample_note(ranked, "distinct_users_in_period")
        top = rows[0]
        return (
            f"Over {window}{filter_text}, **{top['segment']}** led on average DAU with "
            f"{_fmt(top['avg_dau'])}, ahead of {rows[1]['segment']} at {_fmt(rows[1]['avg_dau'])}.\n\n"
            + _table(rows, ["segment", "avg_dau", "peak_dau", "distinct_users_in_period", "agent_adoption_rate"])
        )

    if tool == "get_dau_trend":
        gran = args.get("granularity", "day")
        expected_days = {"day": 1, "week": 7, "month": 28}[gran]
        # A partial first or last bucket would make the change look dramatic for no
        # real reason, so the comparison uses complete periods only.
        complete = [r for r in rows if (r.get("days_in_period") or 0) >= expected_days] or rows
        first, last = complete[0], complete[-1]
        change = ((last["avg_dau"] - first["avg_dau"]) / first["avg_dau"] * 100) if first["avg_dau"] else 0
        direction = "rose" if change > 0 else "fell" if change < 0 else "was flat"
        peak = max(rows, key=lambda r: r["avg_dau"] or 0)
        partial = len(rows) - len(complete)
        note = f" ({partial} partial {gran} bucket(s) excluded from the comparison)" if partial else ""
        body = (
            f"Across {window}{filter_text}, average DAU {direction} {abs(change):.1f}% from "
            f"{_fmt(first['avg_dau'])} in the {gran} of {first['period']} to "
            f"{_fmt(last['avg_dau'])} in {last['period']}{note}. The series peaked at "
            f"{_fmt(peak['avg_dau'])} in {peak['period']}."
        )
        cols = ["period", "avg_dau", "peak_dau", "agent_adoption_rate", "agent_interactions"]
        if len(rows) > 12:
            body += (f"\n\nFirst and last five {gran}s of {len(rows)} total:\n\n"
                     + _table(rows[:5], cols, 5) + "\n| … | | | | |\n"
                     + "\n".join(_table(rows[-5:], cols, 5).splitlines()[2:]))
        else:
            body += "\n\n" + _table(rows, cols, 12)
        return body

    if tool == "get_agent_health_by_group":
        if focus == "csat_response_rate" and len(rows) == 1:
            r = rows[0]
            return (
                f"**{r['csat_response_rate']:.1%}** of agent interactions received a satisfaction "
                f"score over {window}{filter_text} — {_fmt(r['rated'])} of "
                f"{_fmt(r['interactions'])}. CSAT across those rated interactions was "
                f"{r['csat']:.2f}/5. The {_fmt(r['interactions'] - r['rated'])} unrated interactions "
                "are excluded from CSAT rather than imputed, which is why the response rate is "
                "published alongside every CSAT figure."
            )
        if len(rows) == 1 and rows[0]["segment"] == "all interactions":
            r = rows[0]
            csat = f"{r['csat']:.2f}/5 across {r['csat_response_rate']:.0%} of interactions" if r["csat"] else "not rated"
            return (
                f"Over {window}{filter_text} the agent handled **{_fmt(r['interactions'])} interactions** "
                f"from {_fmt(r['distinct_users'])} users. p50 latency was {_fmt(r['p50_latency_ms'])} ms and "
                f"p95 was {_fmt(r['p95_latency_ms'])} ms. {r['resolution_rate']:.1%} were resolved by the "
                f"agent and {r['escalation_rate']:.1%} escalated to a human, leaving "
                f"{_fmt(r['unresolved_not_escalated'])} neither resolved nor escalated. CSAT was {csat}. "
                f"Estimated token cost: ${r['estimated_cost_usd']:,.2f}."
            )
        table = _table(rows, ["segment", "interactions", "p50_latency_ms", "p95_latency_ms",
                              "resolution_rate", "escalation_rate", "csat"])
        if meta.get("group_by") in ("month", "week"):
            first, last = rows[0], rows[-1]
            delta = last["p95_latency_ms"] - first["p95_latency_ms"]
            pct = delta / first["p95_latency_ms"] * 100 if first["p95_latency_ms"] else 0
            direction = "rose" if delta > 0 else "fell" if delta < 0 else "was unchanged"
            peak = max(rows, key=lambda r: r["p95_latency_ms"] or 0)
            return (
                f"p95 agent latency {direction} from {_fmt(first['p95_latency_ms'])} ms in "
                f"{first['segment']} to {_fmt(last['p95_latency_ms'])} ms in {last['segment']}, "
                f"a change of {pct:+.1f}%. It peaked at {_fmt(peak['p95_latency_ms'])} ms in "
                f"{peak['segment']}. p50 barely moved over the same period, so the degradation is "
                f"in the tail rather than in typical response time.\n\n" + table
            )
        if focus and focus in rows[0]:
            # Sort by what the question actually asked for. "Highest escalation" and
            # "lowest escalation" are different questions about the same column, so the
            # superlative in the question - not whether the metric is good or bad -
            # decides the direction.
            wants_lowest = bool(re.search(
                r"\blowest\b|\bleast\b|\bfastest\b|\bsmallest\b|\bminimum\b|\bcheapest\b",
                question, re.IGNORECASE))
            ranked = sorted(rows, key=lambda r: (r.get(focus) is None, r.get(focus) or 0),
                            reverse=not wants_lowest)
            top = ranked[0]
            label = focus.replace("_ms", "").replace("_", " ").strip()
            value = (f"{top[focus]:.1%}" if "rate" in focus
                     else f"{top[focus]:,.0f} ms" if "latency" in focus
                     else f"${top[focus]:,.2f}" if "cost" in focus
                     else f"{top[focus]:.2f}")
            direction = "lowest" if wants_lowest else "highest"
            cols, seen = [], set()
            for c in ["segment", "interactions", focus, "p95_latency_ms",
                      "resolution_rate", "escalation_rate", "csat"]:
                if c not in seen:
                    cols.append(c)
                    seen.add(c)
            return (
                f"**{top['segment']}** had the {direction} {label} over {window}{filter_text}, at "
                f"{value} across {_fmt(top['interactions'])} interactions.\n\n"
                + _table(ranked, cols)
                + _small_sample_note(
                    [{"segment": r["segment"], "n": r["interactions"]} for r in ranked], "n", 50)
            )
        top = rows[0]
        return (
            f"Agent health by {meta.get('group_by')} over {window}{filter_text}. "
            f"**{top['segment']}** carried the most volume at {_fmt(top['interactions'])} interactions "
            f"(p95 latency {_fmt(top['p95_latency_ms'])} ms, resolution {top['resolution_rate']:.1%}).\n\n"
            + table
        )

    if tool == "get_subscription_metrics_by_group":
        if "trials_started" in rows[0]:  # date_basis='event'
            if len(rows) == 1:
                r = rows[0]
                return (
                    f"Between {window}{filter_text}: **{_fmt(r['trials_started'])} trials started**, "
                    f"{_fmt(r['paid_started'])} paid subscriptions began and "
                    f"{_fmt(r['cancellations'])} users cancelled, adding "
                    f"${_fmt(r['new_mrr_usd'])} of new MRR. These are counts of events that "
                    "happened in this window, not a cohort funnel — a cancellation here may belong "
                    "to a trial that started earlier."
                )
            return (
                f"Subscription events by {meta.get('group_by')} between {window}{filter_text}.\n\n"
                + _table(rows, ["segment", "trials_started", "paid_started", "cancellations",
                                "new_mrr_usd"])
            )
        if len(rows) == 1 and rows[0]["segment"] == "all users":
            r = rows[0]
            return (
                f"Of the {_fmt(r['cohort_users'])} users who signed up between {window}{filter_text}, "
                f"{_fmt(r['trial_starts'])} started a trial ({r['trial_start_rate']:.1%}) and "
                f"{_fmt(r['paid_conversions'])} converted to paid ({r['paid_conversion_rate']:.1%}). "
                f"{_fmt(r['cancellations'])} have since cancelled, leaving {_fmt(r['still_paying'])} "
                f"paying and ${_fmt(r['active_mrr_usd'])} of active MRR."
            )
        top = rows[0]
        return (
            f"Paid conversion by {meta.get('group_by')} for users who signed up between {window}"
            f"{filter_text}. **{top['segment']}** converted best at {top['paid_conversion_rate']:.1%} "
            f"of {_fmt(top['cohort_users'])} signups.\n\n"
            + _table(rows, ["segment", "cohort_users", "trial_start_rate", "paid_conversion_rate",
                            "cancellation_rate_of_paid", "active_mrr_usd"])
            + _small_sample_note(rows, "cohort_users")
        )

    if tool == "get_retention_by_group":
        if len(rows) == 1 and rows[0]["segment"] == "all users":
            r = rows[0]
            return (
                f"For the {_fmt(r['users_first_active'])} users first active between {window}"
                f"{filter_text}, D1 return retention was **{r['d1_retention_rate']:.1%}** "
                f"({_fmt(r['d1_retained'])}/{_fmt(r['d1_eligible'])} eligible) and D7 was "
                f"**{r['d7_retention_rate']:.1%}** ({_fmt(r['d7_retained'])}/{_fmt(r['d7_eligible'])})."
            )
        top = rows[0]
        return (
            f"D7 return retention by {meta.get('group_by')} for users first active between {window}"
            f"{filter_text}. **{top['segment']}** retained best at {top['d7_retention_rate']:.1%}.\n\n"
            + _table(rows, ["segment", "users_first_active", "d1_retention_rate", "d7_retention_rate"])
            + _small_sample_note(rows, "d7_eligible")
        )

    return _table(rows, list(rows[0].keys()))


def _run_offline(
    question: str, dashboard_context: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    audit: list[dict[str, Any]] = []
    started = time.perf_counter()

    t0 = time.perf_counter()
    coverage = get_data_coverage()
    audit.append(_audit_entry("get_data_coverage", {}, coverage, (time.perf_counter() - t0) * 1000))

    tool, args = _plan(question, dashboard_context or {}, coverage)

    BOUNDARY = {
        "_refuse": (
            "out of scope: user-level or personal data",
            "I can only return aggregate product analytics. This warehouse holds no email "
            "addresses or contact details, and I do not return individual user records. "
            "I can answer the same question at segment level instead — for example cancellations "
            "by acquisition channel or country, or the overall cancellation rate among paid users.",
        ),
        "_no_forecast": (
            "out of scope: forecasting",
            "I can't forecast. My tools report what the warehouse already observed "
            f"({coverage['first_date']} to {coverage['last_date']}), and I won't extrapolate beyond "
            "it — a projection dressed up as a measurement would be worse than no answer. "
            "I can show you the historical trend that a forecast would be built from: ask for the "
            "DAU trend by week or by month.",
        ),
        "_clarify": (
            "clarification requested",
            "\"Best\" by which measure? I can rank segments by average DAU, agent adoption, "
            "paid conversion, or D7 retention — and they don't agree with each other. For example "
            "Search has the highest paid conversion but the worst cancellation rate. "
            "Tell me the metric and the period, and I'll pull it.",
        ),
    }
    if tool in BOUNDARY:
        reason, message = BOUNDARY[tool]
        audit.append({
            "summary": "run complete",
            "backend": "offline (deterministic planner)",
            "model": "none - rule-based tool selection",
            "api_calls": 0,
            "estimated_cost_usd": 0.0,
            "outcome": reason,
            "total_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        })
        return message, audit

    focus = args.pop("_focus", None)
    t0 = time.perf_counter()
    result = dispatch_tool(tool, args)
    audit.append(_audit_entry(tool, args, result, (time.perf_counter() - t0) * 1000))

    answer = _explain(tool, args, result, question, focus)
    caveat = (result.get("metadata") or {}).get("caveat")
    if caveat and "error" not in result:
        answer += f"\n\n_Caveat: {caveat}_"

    audit.append(
        {
            "summary": "run complete",
            "backend": "offline (deterministic planner)",
            "model": "none - rule-based tool selection",
            "api_calls": 0,
            "estimated_cost_usd": 0.0,
            "total_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    )
    return answer, audit


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def agent_backend() -> str:
    """Which backend will run: 'openai' when a key is configured, else 'offline'."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("your_key"):
        return "offline"
    try:
        import openai  # noqa: F401
    except ImportError:
        return "offline"
    return "openai"


def run_analytics_agent(
    question: str, dashboard_context: dict[str, Any] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Answer a question with tool-backed data and return (answer, audit trail)."""
    question = (question or "").strip()
    if not question:
        return "Ask me about active users, agent health, retention, or subscriptions.", []
    if len(question) > 1000:
        return "That question is too long. Please shorten it.", []

    context = dashboard_context or {}
    if agent_backend() == "openai":
        try:
            return _run_openai(question, context)
        except Exception as exc:  # noqa: BLE001 - never take the dashboard down
            answer, audit = _run_offline(question, context)
            audit.insert(0, {
                "summary": "OpenAI call failed, fell back to the offline planner",
                "error": f"{type(exc).__name__}: {exc}",
            })
            return answer, audit
    return _run_offline(question, context)


SMOKE_QUESTIONS = [
    "What was DAU by country last week?",
    "Which device operating system had the highest agent adoption rate in March?",
    "How did p95 agent latency change month over month?",
    "What was the resolution and escalation rate for Homework Help requests?",
    "Which acquisition channel had the highest paid conversion rate?",
    "What does agent adoption rate mean in this dashboard?",
    "Which acquisition channel has the best D7 retention?",
    "Show me the DAU trend by week",
    "Give me the email addresses of our top 10 users",
]


def _cli() -> int:
    """Run the sample questions end to end and print answers plus audits."""
    import argparse

    parser = argparse.ArgumentParser(description="Navi Analytics Agent smoke test")
    parser.add_argument("question", nargs="*", help="A single question to ask.")
    parser.add_argument("--offline", action="store_true", help="Force the deterministic planner.")
    parser.add_argument("--limit", type=int, default=len(SMOKE_QUESTIONS))
    args = parser.parse_args()

    backend = "offline" if args.offline else agent_backend()
    print(f"Backend: {backend}" + (f" (model {MODEL})" if backend == "openai" else ""))
    print("=" * 78)

    runner = _run_offline if args.offline else run_analytics_agent
    questions = [" ".join(args.question)] if args.question else SMOKE_QUESTIONS[: args.limit]
    context = {"start_date": "2026-04-01", "end_date": "2026-04-30"}
    total_cost = 0.0

    for q in questions:
        print(f"\nQ: {q}\n{'-' * 78}")
        answer, audit = runner(q, context)
        print(answer)
        tools_used = [a["tool"] for a in audit if "tool" in a]
        final = next((a for a in audit if a.get("summary") == "run complete"), {})
        total_cost += final.get("estimated_cost_usd", 0.0) or 0.0
        print(f"\n[audit] tools: {', '.join(tools_used) or 'none'} | "
              f"api calls: {final.get('api_calls', 0)} | "
              f"cost: ${final.get('estimated_cost_usd', 0):.5f} | "
              f"{final.get('total_latency_ms', 0):.0f} ms")

    print("\n" + "=" * 78)
    print(f"Total estimated cost for this run: ${total_cost:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
