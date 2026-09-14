# Dashboard screenshots

Captured at 1680×1050, 2× device scale, full page. Filter state for every shot:
**2026-01-01 → 2026-04-30, no segment filters.**

| File | Section | What it shows |
|---|---|---|
| `01_overview.png` | Overview | Six KPI cards, the data-quality status strip, the DAU arc (peak 371 on 2026-03-14 → 35 by 2026-04-30), and agent adoption smoothed on a 7-day mean |
| `02_active_users.png` | Active Users | DAU trend with day/week/month granularity, and DAU + adoption by country, device, channel and app version |
| `03_agent_health.png` | Agent Health | p50/p95 latency over time, response-status mix, resolution and escalation by intent, tool-use mix, latency distribution |
| `04_subscriptions_retention.png` | Subscriptions & Retention | Signup-cohort funnel, the known-artifact warning, conversion and cancellation by segment, D1/D7 retention |
| `05a_agent_tab.png` | Ask Navi Analytics Agent | The agent tab before a question: guardrail explainer and the dashboard context being passed in |
| `05b_agent_answer.png` | Ask Navi Analytics Agent | "What was DAU by country last week?" answered with a segment table |
| `05c_agent_tool_audit.png` | Ask Navi Analytics Agent | The same answer with its tool audit expanded — both tool calls, their arguments, the resolved date range, and the run summary |
| `05d_agent_boundary.png` | Ask Navi Analytics Agent | "Show me all users who cancelled." refused, with an aggregate alternative offered |
| `06_data_quality.png` | Data Quality | Raw source profile, every cleaning decision with its impact, all 15 validation checks, quarantined records, metric glossary |

## Note on the agent screenshots

`05b`–`05d` were captured with the **offline deterministic planner**, which the run summary in the
audit states explicitly. The tool calls, arguments, validation and audit trail are identical on
both backends — only tool *selection* differs (rules vs. the LLM).

To retake them against the live OpenAI backend, put your key in `.env` and re-run the dashboard:

```bash
streamlit run app.py
```

Then ask the same questions in the Agent tab. The run summary will show
`"backend": "openai"` with the model, token counts and measured cost.
