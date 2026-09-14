# Data quality note

**Warehouse:** `warehouse/navi_analytics.db` · **Source:** `data/raw/*.csv`
**Coverage:** app activity 2026-01-01 → 2026-04-30 (120 days); signups 2026-01-01 → 2026-03-20
**Validation:** 15/15 automated checks pass (`dq_validation_results`)

Every decision below is also stored in the warehouse itself, in `dq_decisions`, and rendered
in the dashboard's **Data Quality** tab. Nothing here is hand-maintained prose that can drift
from the code — the ETL emits it.

---

## 1. Raw profile

| Table | Rows | Primary key unique? | Duplicate rows | Null cells |
|---|---|---|---|---|
| `users` | 5,000 | yes | 0 | 0 |
| `app_events` | 91,130 | **no** — 90,497 distinct `event_id` | 623 | 729 |
| `agent_interactions` | 7,810 | yes | 0 | 4,794 |
| `subscriptions` | 5,000 | yes | 0 | 17,585 |

Most nulls are structurally expected: `satisfaction_score` is optional (4,748 unrated), and
subscription lifecycle timestamps are null for the 4,236 users who never subscribed. The
`app_events` key violation and the two source disagreements in §3 are the real problems.

---

## 2. Decisions taken, and their measured impact

### 2.1 Duplicate event IDs — deduplicated, with a collision trap avoided

633 `event_id` values appear more than once (0.69% of rows). 1,246 of the duplicate rows are
byte-identical replays, which is ordinary client retry behaviour.

**Four are not.** In those four cases the same `event_id` is shared by a real product row and a
`TEST_*` row:

```
E000051815  U001365     prompt_submit  is_test_event=0
E000051815  TEST_00129  prompt_submit  is_test_event=1   <- same id, different user
```

A naive `drop_duplicates(keep='first')` would sometimes keep the test row and throw away the
real user's event. The ETL therefore **orders rows to prefer product traffic from a known user
before deduplicating**, so a collision always resolves in favour of the real event.

**Impact:** 633 extra copies removed; `fact_app_events` holds 90,497 rows with a genuinely unique
key. The dropped copies are written to `dq_quarantine_events`, not discarded, so the exclusion is
auditable from SQL. A validation check asserts
`fact_app_events + dq_quarantine_events = 91,130 raw rows`.

### 2.2 Test and unknown-user traffic — excluded from metrics, retained in the fact table

273 raw rows carry `is_test_event = 1`. Every one belongs to a `TEST_*` identifier that does not
exist in `users.csv`, and **no product row has an unknown user**. The two conditions coincide
exactly, which is a useful sign that the test flag is trustworthy.

**Policy:** `included_in_metrics = 0` when `is_test_event = 1` **or** the user is absent from
`dim_users`. Rows stay in `fact_app_events` rather than being deleted, so a reviewer can see what
was excluded and why.

**Impact:** 269 events excluded from the metric layer (the other 4 were already removed as
duplicate copies in §2.1). A validation check asserts zero test events reach the metric layer.

### 2.3 Latency outliers — flagged and kept; p95 is the headline, the mean is never shown

Latency is **bimodal in the tail**, not smoothly long-tailed:

| p50 | p75 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| 2,902 ms | 3,925 ms | 6,183 ms | 9,333 ms | 83,366 ms | 89,518 ms |

There is a clean gap: p99 sits at 9.3s, then 41 interactions jump above 30s. Those 41 look like
instrumentation or hung-connection artifacts rather than user-visible response times.

**Policy:** flag them with `is_latency_outlier = 1`, keep them in the metric layer, and report
**p50 and p95 only**. The mean is deliberately never published — these 41 rows (0.52% of traffic)
drag it to 3,580 ms against a median of 2,902 ms, a 23% inflation. The latency histogram in the
dashboard excludes them so the shape of normal performance stays readable, and states the count
it excluded.

### 2.4 CSAT — averaged over rated interactions only, always with its response rate

3,062 of 7,810 interactions carry a rating (39.2%). No rating is imputed, and unrated
interactions are **not** treated as neutral or as zero.

**Policy:** CSAT = mean `satisfaction_score` over rated interactions. Every CSAT figure in the
dashboard and every agent answer ships with `csat_response_rate` beside it, because a 4.10 built
on 39% response is a different claim from a 4.10 built on 95%.

### 2.5 Resolution and escalation — separate rates, never complements

The profile confirms they are mutually exclusive but **not exhaustive**:

| Status | neither | escalated | resolved |
|---|---|---|---|
| success | 1,686 | 426 | 5,340 |
| timeout | 0 | 267 | 0 |
| error | 0 | 91 | 0 |

1,686 successful interactions are neither resolved nor escalated — the user got a response and
simply moved on. **Every timeout and every error escalates**, so escalation rate carries
reliability failures as well as difficulty.

**Policy:** report resolution and escalation independently, plus the
`unresolved_not_escalated` remainder, so the three sum to the total. A validation check asserts
`resolution_rate + escalation_rate <= 1` on every day.

### 2.6 Missing categorical values — labelled, not dropped

- 729 events have a null `screen_name` → `'unknown'`. The event is still valid activity, so
  dropping it would understate DAU.
- 46 interactions have a null `tool_used` → `'unrecorded'`.
- The literal string `'none'` (3,868 rows) is renamed `'no_tool'`, so "the agent answered
  directly" is never confused with "instrumentation failed to record a tool".

### 2.7 Retention — day-N return with an explicit eligibility window

**Definition:** a user is D*N*-retained if they are active on **exactly** `first_activity_date + N`.
A user is only **eligible** if that date falls on or before the last observed day (2026-04-30).

Without the eligibility rule, users who first appeared in late April would be scored as churned
purely because the data ends, biasing recent cohorts downward.

**Impact:** 4,996 of 5,000 users are D7-eligible; the 4 ineligible are excluded from the
denominator rather than counted as failures.

### 2.8 Segment attributes come from `dim_users`

`app_events` carries its own client-reported `device_os`, which can disagree with the user's
signup device. Segments resolve to the **user-level attribute** from `dim_users` so a user
cannot move between segments mid-period. The one exception is `app_version`, which is genuinely
an event-time property: `mart_user_day.app_version` is that user's **modal version for that day**,
falling back to `app_version_at_signup` when they had no client event. This prevents a user who
upgraded mid-day from being counted twice in "DAU by app version".

---

## 3. Two source disagreements a reviewer should know about

These are not cleaning decisions. They are defects in the source data that change what the
numbers can support, and they are surfaced in the dashboard rather than quietly resolved.

### 3.1 `trial_start` events contradict the subscriptions table

| Source | Users with a trial |
|---|---|
| `app_events` where `event_name = 'trial_start'` | 404 |
| `subscriptions` where `trial_started_at` is not null | 764 |
| **Present in both** | **104** |

The two sources agree on only 104 users. 300 users emit a `trial_start` event with no trial in
the subscription table, and 660 have a trial with no event.

**Decision:** `subscriptions.csv` is the system of record for lifecycle state; the `trial_start`
event is **not used for any trial metric**. This should be raised with whoever owns the mobile
instrumentation — one of the two pipelines is dropping records, and until that is resolved no
event-based funnel step should be trusted.

### 3.2 Trial-to-paid conversion is 100% with zero variance — a data artifact

All 764 trials have a `paid_started_at` **exactly 7 days** after `trial_started_at`
(min = max = 7 days, standard deviation 0). No trial fails to convert.

Real trial funnels do not behave this way. The field is almost certainly the **scheduled** first
billing date written at trial start, not an observed conversion event.

**Consequences, applied throughout:**
- "Paid conversion among trial starters" would compute to 100%. It is **not** used as a KPI.
- Conversion is measured against the **signup cohort** instead
  (`mart_subscription_user.has_paid / cohort`), which is a defensible denominator.
- The dashboard's Subscriptions tab carries an explicit warning, and the
  `get_subscription_metrics_by_group` tool returns the caveat in its metadata so the Analytics
  Agent repeats it in any conversion answer.

Minor related issue: 6 users have a `trial_started_at` up to 1 day **before** their `signup_ts`.
Left as-is and flagged; the volume is too small to affect any reported metric.

---

## 4. Validation: 15 checks, SQL against independent pandas

Each check computes the same quantity through two different code paths — warehouse SQL, and a
pandas recomputation that does not touch the marts — then asserts agreement. The ETL exits
non-zero if any check fails, so a broken pipeline cannot silently ship a warehouse.

| Check | Result |
|---|---|
| `app_events` row reconciliation (fact + quarantine = raw) | 91,130 = 91,130 |
| `fact_app_events.event_id` unique | 90,497 = 90,497 |
| `fact_agent_interactions.interaction_id` unique | 7,810 = 7,810 |
| `fact_subscriptions.subscription_id` unique | 5,000 = 5,000 |
| `dim_users.user_id` unique | 5,000 = 5,000 |
| Total DAU: SQL mart vs pandas on the fact table | 26,078 = 26,078 |
| `mart_user_day` has no orphan users | 0 |
| No test events in the metric layer | 0 |
| `agent_adoption_rate` within [0,1] | 0 violations |
| `resolution_rate + escalation_rate <= 1` | 0 violations |
| p95 latency on 2026-01-01: SQL vs numpy | 3,086.6 = 3,086.6 |
| Paid conversions: daily table vs user grain | 764 = 764 |
| D7 retained ≤ eligible ≤ users | 820 ≤ 4,996 ≤ 5,000 |
| Agent interactions: mart vs fact | 7,810 = 7,810 |
| CSAT denominator = rated interactions only | 3,062 = 3,062 |

Two further suites run outside the ETL:

- **`tests/test_agent_tools.py` — 41 checks.** Every tool's output is verified against a pandas
  recomputation **from the raw CSVs**, so the tools are checked against source data rather than
  against the warehouse that produced them. Includes safety tests: SQL injection through
  `group_by` and through filter values, malformed and out-of-range dates, unknown tool names,
  oversized inputs, a write attempt against the read-only connection, and an assertion that no
  `user_id` appears in any tool result.
- **`tests/test_dashboard_agreement.py` — 77 checks.** The dashboard path (`src/metrics.py`) and
  the agent path (`src/agent_tools.py`) are asked for the same metric under the same filters, and
  every pair must match exactly — across five filter states and per-segment breakdowns. This is
  what makes "the dashboard and the agent agree" a tested property rather than a claim.

---

## 5. What I would fix next, given more time

1. **Resolve the trial instrumentation conflict (§3.1)** with the mobile team. Until then, no
   event-based funnel analysis is trustworthy.
2. **Get a real `paid_started_at` (§3.2).** Conversion is the company's most important metric and
   it is currently unmeasurable at the trial→paid step.
3. **Find the source of the 41 latency outliers.** They are almost certainly a monitoring bug
   rather than 90-second user waits; confirming that would let the flag be removed.
4. **Raise the CSAT response rate above 39%,** or accept that segment-level CSAT comparisons rest
   on a few hundred ratings and stop cutting it finely.
5. **Add a `is_test_user` flag to `users.csv`.** Test identity is currently inferred from a naming
   convention plus an event flag; an explicit dimension attribute would be more robust.
