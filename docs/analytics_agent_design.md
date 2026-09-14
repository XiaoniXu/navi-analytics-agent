# Analytics Agent design — as built

## The separation that matters

The LLM is the **interface**. It is not the data layer.

```
                 ┌──────────────────────────────────────────┐
  question  ───► │  LLM: read question, choose ONE tool     │  never sees the database
                 │       and its arguments                  │  never writes SQL
                 └───────────────────┬──────────────────────┘
                                     │ tool name + JSON args
                 ┌───────────────────▼──────────────────────┐
                 │  agent_tools.dispatch_tool               │  allow-list on the tool name
                 │  • validate dates (format, order, range) │  allow-list on dimensions
                 │  • validate dimensions against a list    │  length caps on values
                 │  • bind every value as a SQL parameter   │
                 └───────────────────┬──────────────────────┘
                                     │ parameterized SQL, mode=ro
                 ┌───────────────────▼──────────────────────┐
                 │  warehouse/navi_analytics.db (read-only) │
                 └───────────────────┬──────────────────────┘
                                     │ aggregate rows only (≤200), + metadata + caveat
                 ┌───────────────────▼──────────────────────┐
  answer   ◄──── │  LLM: explain metric, range, filters,    │
                 │       result, and the tool's caveat      │
                 └──────────────────────────────────────────┘
                                     │
                          UI tool audit: what ran, with what
```

The column name that reaches the `GROUP BY` is **resolved through `VALID_GROUPS`**, never taken
from the model's string. So even a model that emits `user_id; DROP TABLE dim_users` produces a
validation error, not SQL. `tests/test_agent_tools.py` asserts exactly that case.

## Why seven tools rather than one flexible one

A single "query anything" tool would push the analytical decision back into the model — which
denominator, which date basis, which exclusions. Each tool here owns one metric family and
encodes those decisions in SQL, so the answer is right by construction:

| Tool | Owns the decision that… |
|---|---|
| `get_dau_by_group` | DAU counts app activity, and adoption is measured against it |
| `get_dau_trend` | partial first/last buckets are not compared against complete ones |
| `get_agent_health_by_group` | p95 is reported and the mean is not; resolution and escalation are separate rates |
| `get_subscription_metrics_by_group` | the denominator is the signup cohort, and `date_basis='event'` is a different question from `date_basis='signup'` |
| `get_retention_by_group` | day-N eligibility excludes users whose window has not closed |
| `get_metric_definition` | the glossary the dashboard shows is the glossary the agent quotes |
| `get_data_coverage` | "last week" resolves against the data's last date, not today's |

`get_data_coverage` exists because the data ends 2026-04-30 while the calendar does not. Without
it, "last week" silently returns an empty range — the single most likely way for a grounded agent
to produce a confidently wrong answer.

Every tool returns a `metadata` block naming the metric, the resolved date range, the filters
applied, and the warehouse source. Tools whose numbers are easy to misread also return a
`caveat` string, which the system prompt requires the model to surface. That is how the
100%-trial-conversion artifact reaches the reader instead of being quietly repeated as a result.

## Boundary behaviour

Both backends handle these; the tested transcripts are below.

| Case | Behaviour |
|---|---|
| Personal or user-level data (`"show me all users who cancelled"`) | Refuses, explains the warehouse holds no personal data, offers the aggregate cut |
| Forecasting (`"what will DAU be next month?"`) | Declines to extrapolate, states the observed window, offers the historical trend |
| Ambiguous (`"what is the best segment?"`) | Asks one clarifying question, and names the metrics it could rank by |
| Unsupported metric (`"revenue per llama"`) | Returns an error plus the list of documented metrics |
| Empty range (signup cohort in April) | Explains *why* it is empty — the denominator is signups, which stop 2026-03-20 |
| Small denominators | Appends a warning naming the segments below the threshold |

The refusal on personal data is enforced in two independent places: the planner refuses before any
query runs, and the tool layer only ever emits aggregates — with a test asserting no `user_id`
appears in any tool output. A prompt-level instruction alone would not be sufficient.

## Two backends, one interface

`run_analytics_agent()` dispatches to whichever is available:

- **`OpenAIAgent`** (production) — Responses API with tool calling, up to 4 tool rounds, 700-token
  output cap. Reports measured token cost per answer.
- **`OfflineAgent`** (fallback) — deterministic rule-based tool selection over the *same* validated
  tools, producing the *same* audit trail.

The offline path is not a mock. It exists because (a) the dashboard should stay demoable when a key
is missing or the API is down, and (b) it makes the whole question→tool→answer loop testable
without network access, which is how the six routing bugs found during development were caught.
A live OpenAI failure falls back to it automatically and records the exception in the audit.

## Answer contract

Every data-backed answer states the metric, the date range, the filters, and the result, then any
caveat. Numbers come only from tool results — the system prompt forbids arithmetic the tools did
not do, and forbids reusing a figure from earlier in the conversation.

## Known limitations

- **Rates are cohort-scoped, not arbitrary.** Conversion is always measured against a signup
  cohort or an event count; the tools will not compute an arbitrary custom denominator.
- **No cross-metric correlation tool.** The agent-engagement-vs-conversion finding in the business
  memo was computed ad hoc in SQL, not through a tool. A `get_metric_correlation` tool would need
  care to avoid inviting causal claims the data cannot support.
- **The offline planner is keyword-driven.** It handles the tested question set and its boundary
  cases; a question phrased far outside those patterns falls back to overall DAU. The LLM path
  generalises properly, which is why it is the production backend.
- **`app_version` segments are user-day modal values,** so a user who upgrades mid-day is assigned
  to one version for that day rather than being split across two.
