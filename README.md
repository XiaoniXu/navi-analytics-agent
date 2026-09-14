# Navi Analytics Agent

Mobile app ETL → SQLite warehouse → Streamlit dashboard → tool-calling Analytics Agent.

Raw mobile, AI-companion and subscription logs are turned into a curated analytics layer. A
Streamlit dashboard reads that layer, and an in-dashboard Analytics Agent answers natural-language
questions from the **same** curated tables through a small set of validated, read-only Python tools:

> **Q:** What was DAU by country last week?
> **A:** Over 2026-04-24 to 2026-04-30, US led on average DAU with 28.4, ahead of CA at 6.7 …
> *(tools: `get_data_coverage`, `get_dau_by_group`)*

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env               # Windows: Copy-Item .env.example .env
# add your OpenAI key to .env (see "The Analytics Agent" below)

python src/etl_pipeline.py         # builds warehouse/navi_analytics.db (~20s)
streamlit run app.py
```

The dashboard runs **without** an API key — the agent falls back to a deterministic planner that
uses the same tools. Add a key for live LLM tool calling.

### Verify the build

```bash
python tests/test_agent_tools.py          # 41 checks: tool output vs raw CSVs, plus safety
python tests/test_dashboard_agreement.py  # 77 checks: dashboard numbers == agent numbers
python src/analytics_agent.py --offline    # 9 sample questions, end to end, no API cost
python src/analytics_agent.py             # same questions against the live LLM (~$0.01)
```

---

## Architecture

```
data/raw/*.csv
     │
     ▼  src/etl_pipeline.py        profile → clean → load → build marts → validate (15 checks)
warehouse/navi_analytics.db
     │
     ▼  src/metrics.py             ONE definition per metric, shared by both surfaces
     ├─────────────────────────────┐
     ▼                             ▼
app.py                        src/agent_tools.py    validated, read-only, aggregate-only tools
(charts & tables)                  ▼
                              src/analytics_agent.py   LLM picks a tool → explains the result
                                   ▼
                              app.py "Ask Navi Analytics Agent" tab + tool audit
```

`src/metrics.py` is the reason a chart and an agent answer cannot disagree: both read metrics
from it, and `tests/test_dashboard_agreement.py` asserts across 77 comparisons that they return
identical values for the same filter state.

### Warehouse layers

| Layer | Tables |
|---|---|
| Dimensions | `dim_users`, `dim_date` |
| Facts | `fact_app_events`, `fact_agent_interactions`, `fact_subscriptions` |
| Marts | `mart_user_day`, `mart_user_retention`, `mart_subscription_user`, `daily_product_metrics` |
| Quality | `dq_quarantine_events`, `dq_profile_raw`, `dq_decisions`, `dq_validation_results` |
| Reference | `metric_definitions` |
| Views | `v_agent_interactions_enriched`, `v_dq_summary` |

`mart_user_day` (one row per user per active day) is the shared grain that makes DAU and agent
adoption use the same denominator. Schema and indexes: `schema.sql`.

**Note on rebuilding:** the ETL always builds the database on local temp disk and copies the
finished file into `warehouse/`. Some network and synced folders do not support SQLite's file
locking, and this keeps the build working on them. Set `NAVI_DB_PATH` to build or read a
warehouse elsewhere:

```bash
NAVI_DB_PATH=/tmp/navi.db python src/etl_pipeline.py
NAVI_DB_PATH=/tmp/navi.db streamlit run app.py
```

---

## Dashboard

Six sections, all driven by one shared date range and segment filter set in the sidebar. The
active filter state is displayed in the sidebar and passed to the agent as its context, so any
agent answer can be reproduced from the visuals.

| Section | Contents |
|---|---|
| **Overview** | DAU, agent adoption, p95 latency, resolution, paid conversion, D7 retention, plus a data-quality status strip |
| **Active Users** | DAU trend at day/week/month granularity, and DAU + adoption by country, device, channel, app version |
| **Agent Health** | Latency p50/p95 over time, response-status mix, resolution and escalation by intent, tool-use mix, latency distribution, estimated cost |
| **Subscriptions & Retention** | Signup-cohort trial / conversion / cancellation by segment, active MRR, D1 and D7 retention |
| **Ask Navi Analytics Agent** | Chat interface with a compact tool audit for every answer |
| **Data Quality** | Raw profile, every cleaning decision and its impact, all 15 validation checks, quarantined records, metric glossary — rendered from the warehouse, not hand-written |

---

## The Analytics Agent

### Flow

```
question + dashboard filters
  → the LLM selects ONE approved tool          (it never writes SQL)
  → Python validates every argument            (allow-lists, date parsing, length limits)
  → parameterized read-only SQL hits the curated warehouse
  → an aggregate result returns to the LLM
  → the LLM explains it: metric, date range, filters, caveats
  → the UI shows which tool ran with which arguments
```

### Tools

| Tool | Answers |
|---|---|
| `get_dau_by_group` | Active users and agent adoption, overall or by segment |
| `get_dau_trend` | DAU as a day / week / month time series |
| `get_agent_health_by_group` | Latency, reliability, resolution, escalation, CSAT, cost — by segment, intent, tool, model, status, or month |
| `get_subscription_metrics_by_group` | Trial, paid conversion, cancellation, MRR for a signup cohort |
| `get_retention_by_group` | D1 / D7 return retention |
| `get_metric_definition` | The implemented definition and source column of any metric |
| `get_data_coverage` | The warehouse's real date range — called first so "last week" resolves against the data, not today's date |

### Guardrails

- The model never writes SQL and never receives database access.
- Grouping dimensions and filter keys come from a fixed allow-list; the column name reaching SQL is
  resolved through that list, never taken from model output.
- Dates are format- and range-validated; every value is bound as a SQL parameter.
- The connection is opened `mode=ro`, so a write is impossible even given a malformed query.
- Results are aggregates only, capped at 200 rows. No user-level record is ever returned, and a
  test asserts no `user_id` appears in any tool output.
- Requests for personal data are refused before any query runs.

`tests/test_agent_tools.py` proves these: SQL injection via `group_by` and via filter values,
malformed and out-of-range dates, unknown tool names, oversized inputs, and an attempted write.

### Setup and cost

1. Create an account at **platform.openai.com** (separate from ChatGPT; a Plus subscription does
   not include API usage) and add prepaid credit.
2. Create an API key, then put it in `.env`:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5-mini
```

3. Keep `.env` out of version control — `.gitignore` already covers it.

A tool-backed question costs roughly **$0.002–0.003** with `gpt-5-mini` (two model turns). 100
questions ≈ $0.25; **$3–5 of prepaid credit is ample** for building, testing and demoing. The agent
displays the measured cost and reasoning-token count of every answer beneath it.

**Reasoning-token budget.** `gpt-5-mini` is a reasoning model: its internal reasoning tokens are
billed as output *and* count against `max_output_tokens`. A budget sized only for the visible
answer gets consumed by reasoning, and the API returns a truncated response with **empty text** —
a silent blank answer rather than an error. The defaults account for this:

| Setting | Default | Override |
|---|---|---|
| Output budget (reasoning + answer) | 2000 tokens | `OPENAI_MAX_OUTPUT_TOKENS` |
| Reasoning effort | `low` | `OPENAI_REASONING_EFFORT` |

`reasoning` is only sent for models that accept it (gpt-5 and o-series), so `gpt-4o-mini` still
works. If a response is still truncated, the agent retries once with a doubled budget, and if the
model returns no text at all it says so and points at the tool audit rather than showing a blank
answer. Cost is otherwise kept low by a small tool set, aggregate-only returns, and no web-search tools.

If no key is present, the agent runs its **deterministic planner** — the same validated tools and
the same audit trail, with rule-based tool selection instead of a language model. This keeps the
dashboard demoable offline and lets the whole pipeline be tested without network access.

---

## Deploying to Streamlit Community Cloud

The app is deployment-ready: the warehouse is a **build artifact, not a committed file**, so on a
fresh deployment `app.py` detects the missing database and runs the ETL once at first boot (about
20 seconds, cached per container). The raw CSVs are in the repo, so the deployed warehouse is
byte-identical to a local one.

1. Go to **share.streamlit.io** and sign in with GitHub.
2. **New app** → pick this repository, branch `main`, main file `app.py`.
3. Under **Advanced settings**, set Python to **3.11**.
4. Still in Advanced settings, paste into **Secrets**:

   ```toml
   OPENAI_API_KEY = "sk-..."
   OPENAI_MODEL = "gpt-5-mini"
   ```

5. **Deploy.**

`app.py` promotes `st.secrets` into environment variables before importing the agent, so the same
code reads `.env` locally and Streamlit secrets in the cloud with no branching.

### Before you deploy with a key

**A public app with a live API key means anyone who opens the URL can spend your OpenAI credit.**
Each question costs about $0.002, so casual traffic is cheap, but the exposure is unbounded and
the app has no rate limiting. Pick one:

| Option | Cost risk | Demo quality |
|---|---|---|
| Deploy **without** a key | None | Agent runs the deterministic planner; tool calls and audit still work, but it is not a live LLM |
| Deploy **with** a key + a hard limit in the OpenAI dashboard | Capped at whatever you set | Full live tool calling |
| Keep the app **private** (invite viewers by email) | None from strangers | Full live tool calling |

If you deploy with a key, set a hard cap first: **platform.openai.com → Settings → Limits**, and
keep the prepaid balance small. Rotate the key if the URL is ever shared more widely than intended.

## Repository

```
app.py                            Streamlit dashboard (6 sections)
schema.sql                        Warehouse DDL, indexes, views
requirements.txt
.env.example
src/etl_pipeline.py               ETL: profile → clean → load → marts → 15 validations
src/metrics.py                    Shared metric definitions (dashboard + agent)
src/agent_tools.py                Read-only, validated, aggregate-only LLM tools
src/analytics_agent.py            LLM orchestration + audit + offline fallback
tests/test_agent_tools.py         41 correctness & safety checks against raw CSVs
tests/test_dashboard_agreement.py 77 dashboard-vs-agent agreement checks
data/raw/                         Source CSVs and data dictionary
data/metric_definitions.csv       Suggested metric definitions
docs/data_quality_note.md         Every cleaning decision, its impact, and known source defects
docs/business_memo.md             Findings and recommendations
docs/screenshots/                 Dashboard screenshots
warehouse/                        SQLite database (gitignored; rebuild with the ETL)
```

## Metric definitions

The exact implemented definitions live in the `metric_definitions` warehouse table, are shown in
the dashboard's Data Quality tab, and are returned by the `get_metric_definition` tool — so the
chart, the glossary and the agent all quote the same wording. Highlights:

- **DAU** — distinct users with ≥1 app event on a calendar date, after duplicate event IDs are
  removed and test / unknown-user traffic is excluded.
- **Agent adoption rate** — distinct users with an agent request ÷ DAU, same date and segment.
- **Resolution / escalation rate** — reported separately, never as complements; 1,686 successful
  interactions are neither, and every timeout and error escalates.
- **CSAT** — mean score over *rated* interactions only (39.2% of them), always shown with its
  response rate.
- **D1 / D7 retention** — active on exactly `first_activity_date + N`, counting only users whose
  day-N date falls inside the observed window.
- **Paid conversion** — measured against the **signup cohort**, not trial starters. See the warning
  in `docs/data_quality_note.md` §3.2: every trial in this dataset converts in exactly 7 days, so
  trial-to-paid conversion is a data artifact rather than a result.

## Known source defects

Two defects in the raw data limit what can be concluded. Both are surfaced in the dashboard rather
than quietly resolved — full detail in `docs/data_quality_note.md` §3:

1. **`trial_start` events contradict the subscriptions table.** 404 users emit the event, 764 have
   a trial in the table, only 104 appear in both. `subscriptions.csv` is treated as the system of
   record.
2. **Trial→paid conversion is 100% with zero variance** — exactly 7 days for all 764 trials. The
   field is a scheduled billing date, not an observed conversion.
