# Navi product analytics: what the data says, and what I would do about it

**To:** Navi product leadership
**Re:** First read of the Jan–Apr 2026 warehouse
**Data:** 5,000 users · 90,497 product events · 7,810 agent interactions · 2026-01-01 → 2026-04-30
**Caveat up front:** two source defects limit what can be concluded about the subscription
funnel. They are described in §5 and in `docs/data_quality_note.md`. Everything else below is
reproducible from the dashboard with the stated filters.

---

## The headline

Navi's engagement growth through mid-March was **entirely acquisition-fed, and it reversed the
moment acquisition stopped.** DAU peaked at **371 on 2026-03-14**, then fell to **35 by
2026-04-30 — a 91% decline**. The last recorded signup is **2026-03-20**, three days before the
decline begins.

| Week | Avg DAU |
|---|---|
| 2026-03-09 | **339** ← peak |
| 2026-03-16 | 334 |
| 2026-03-23 | 253 |
| 2026-03-30 | 169 |
| 2026-04-06 | 116 |
| 2026-04-13 | 82 |
| 2026-04-20 | 55 |
| 2026-04-27 | **41** |

Retention explains why nothing caught the fall: **D1 is 21.8% and D7 is 16.4%**. About four in
five new users are gone within a week, so the active base is a thin layer of recent signups. With
no new cohorts arriving, the base drains at close to the rate of its own churn.

**This is the finding that matters most.** Every other number below is a lever on it or a
distraction from it. If acquisition restarts without retention improving, the same decay repeats
at whatever the new spend level supports.

---

## 1. Agent engagement is the strongest available lever — but the evidence is correlational

Users who engage the AI study companion convert far better:

| Agent requests | Users | Paid conversion | Avg active days |
|---|---|---|---|
| 0 | 3,092 | **10.9%** | 4.2 |
| 1–2 | 633 | 14.2% | 4.7 |
| 3–5 | 779 | **24.8%** | 6.8 |
| 6+ | 496 | **29.0%** | 9.6 |

Conversion is **2.7× higher** for users with 6+ agent requests than for users with none.

**Read this honestly: it is not proof that the agent causes conversion.** Active days rise in
lockstep (4.2 → 9.6), so agent use may simply be a marker of an engaged user who would have
converted anyway. The two explanations have very different implications and the data cannot
separate them.

**What I would do:** run this as an experiment rather than acting on the correlation. Prompt a
random half of new users toward their first agent interaction during onboarding and measure
conversion against the held-back half. The step from 1–2 to 3–5 requests is where the
correlation jumps hardest (14.2% → 24.8%), so **"reach 3 agent requests in the first session"** is
the specific behaviour worth testing. If the effect survives randomisation, it is the cheapest
retention lever available, because the product already has the feature.

**The ceiling is real, though:** only **38.2%** of users (1,908 of 5,000) ever send an agent
request, and adoption among active users sits at **28.0%** — drifting *down* from 29.9% in January
to 26.7% in April.

---

## 2. Agent adoption is flat across every segment — so stop looking for a segment story

| Dimension | Spread in adoption rate |
|---|---|
| Country | 27.8% (US) → 28.9% (AU) |
| Device OS | 27.9% (iOS) → 28.1% (Android) |
| Channel | 26.2% (Referral) → 29.0% (Search) |
| App version | 26.8% (3.4.2) → 28.7% (3.5.0) |

Every cut lands within **2.7 percentage points**. Adoption is a product-wide constant, not a
segment phenomenon.

**Implication:** there is no underperforming market or device to target, and segment-level
campaigns will not move this number. Whatever is capping adoption at roughly 28% is in the core
experience — discovery, placement, or perceived usefulness of the companion — and the fix is a
product change tested on everyone, not a targeted one.

---

## 3. The agent is getting slower in the tail as volume grows, and reliability is slipping with it

| Month | Interactions | p50 | **p95** | Failure rate | >30s outliers |
|---|---|---|---|---|---|
| 2026-01 | 1,794 | 2,883 ms | **5,875 ms** | 3.9% | 6 |
| 2026-02 | 2,496 | 2,894 ms | **6,141 ms** | 4.3% | 19 |
| 2026-03 | 2,770 | 2,916 ms | **6,397 ms** | 5.2% | 13 |
| 2026-04 | 750 | 2,928 ms | 6,313 ms | 5.1% | 3 |

As volume grew 54% from January to March, **p95 rose 8.9% while p50 moved 1.1%.** Typical
response time is stable; the tail is degrading. That is the signature of a capacity or
concurrency limit, not a general slowdown — and the tail is what users actually notice.

Failure rate rose alongside it, from 3.9% to 5.2%. **Every timeout and every error escalates to a
human** (267 timeouts + 91 errors, all escalated), so reliability failures are also a support-cost
line, not just a quality one.

**What I would do:** treat p95 as a tracked SLO with a threshold near the current 6.4s and alert
on it, before the next growth push. Volume in April was only 27% of March's, so the capacity
question has not been tested at scale since the decline began — a restart in acquisition will
re-expose it immediately.

---

## 4. Two concrete, quantified fixes

### 4.1 The `navi-pro` model costs 17× more per resolved interaction and performs no better

| | navi-mini | navi-pro |
|---|---|---|
| Interactions | 6,089 (78%) | 1,721 (22%) |
| **Share of cost** | **17%** | **83%** |
| Resolution rate | 68.5% | 67.9% |
| CSAT | 4.12 | 4.05 |
| Success rate | 95.3% | 95.8% |
| Mean latency | 3,345 ms | 4,412 ms |
| **Cost per resolved interaction** | **$0.00055** | **$0.00931** |

`navi-pro` handles 22% of traffic and consumes 83% of estimated token spend, while resolving
slightly *fewer* interactions, scoring slightly *lower* on CSAT, and running 32% slower. Average
token counts are nearly identical (869 vs 863), so it is not doing more work — it is the same work
at a higher price.

Routing also looks **undifferentiated**: `navi-pro` takes 19.7%–23.7% of every single intent. It
is not being reserved for hard cases; it looks like a flat split.

**What I would do:** route by measured need rather than by a fixed percentage. On these numbers,
moving `navi-pro` traffic to `navi-mini` for the intents where it shows no advantage would cut
agent model spend by roughly **80% with no measurable quality loss**.
*Caveat:* this is observational, and `navi-pro`'s CSAT rests on 677 ratings against 2,385 for
`navi-mini`. Validate with a routing A/B before switching wholesale.

### 4.2 `web_search` is the worst-performing tool, and it fully explains the worst intent

| Tool | Uses | Escalation | Success | Resolution |
|---|---|---|---|---|
| **web_search** | 534 | **13.7%** | **92.7%** | 67.0% |
| calendar_planner | 548 | 11.1% | 94.9% | 68.8% |
| no_tool | 3,868 | 10.1% | 95.5% | 68.7% |
| document_reader | 761 | 8.9% | 95.8% | 70.2% |
| **calculator** | 975 | **8.0%** | **96.6%** | 69.7% |

`web_search` escalates at **1.7× the rate of `calculator`** and has the lowest success rate of any
tool. And it is used by exactly one intent: **General Q&A** (534 of its 1,057 requests) — which is
also the worst intent, at 13.5% escalation and 93.1% success against 8.4% / 96.1% for
Summarization.

**General Q&A does not have a difficulty problem. It has a `web_search` problem.** The two
findings are the same finding, which makes it an unusually tractable fix: improving or
constraining one tool path addresses the weakest intent in the product.

---

## 5. Where I would not let you draw a conclusion

Two source defects genuinely limit the analysis, and both should be raised with the teams that
own the pipelines:

**The subscription funnel cannot be measured at the trial→paid step.** Every one of the 764 trials
has a paid start exactly 7 days later, with zero variance and no failures. That is a scheduled
billing date, not an observed conversion. Any "trial-to-paid conversion" figure from this data
would read 100% and mean nothing. All conversion figures in this memo and in the dashboard
therefore use the **signup cohort** as the denominator.

**The `trial_start` event and the subscriptions table disagree badly.** 404 users emit the event,
764 have a trial in the table, and only **104 appear in both.** One of the two pipelines is
dropping records. Until that is resolved, no event-based funnel step should be trusted.

One methodological point on channel ranking, which changes the answer:

| Channel | Conversion | Cancellation of paid | **Net still-paying** |
|---|---|---|---|
| **Organic** | 15.8% | 14.7% | **13.5%** ← best |
| Paid Social | 15.4% | 15.4% | 13.0% |
| **Search** | **16.2%** ← best | **20.6%** ← worst | 12.8% |
| Referral | 14.3% | 16.3% | 12.0% |
| Influencer | 13.4% | 13.9% | 11.6% |

**Search converts best and churns worst.** Ranked on conversion alone it is the top channel;
ranked on users still paying, it is third. Optimising acquisition spend on conversion rate would
push money toward the channel with the least durable revenue. Channel decisions should use the
net column.

Smaller note: **Android converts better than iOS (16.3% vs 14.6%) and cancels less (13.9% vs
17.9%)**, despite iOS carrying 35% more users. Worth a look at whether iOS monetisation has a
specific problem, though the gap is modest and I would not act on it alone.

---

## Recommended next steps, in priority order

1. **Fix retention before restarting acquisition.** D7 at 16.4% means paid growth rents users
   rather than buying them. Restarting spend without moving retention reproduces the same decay.
2. **Run the agent-onboarding experiment** (§1). Target 3 agent requests in the first session.
   Randomised, not correlational — and it is the cheapest lever available because the feature ships
   already.
3. **Re-route `navi-pro` traffic** (§4.1). Roughly 80% of agent model spend, validated with an A/B.
4. **Fix the `web_search` path** (§4.2). One tool fix addresses the worst intent in the product.
5. **Put p95 latency under an SLO with alerting** (§3) before volume returns.
6. **Escalate both instrumentation defects** (§5). Until they are fixed, the company's most
   important funnel metric is unmeasurable.
7. **Diagnose the ~28% adoption ceiling as a product question** (§2), not a targeting one.

---

### Reproducing anything in this memo

Every figure comes from `warehouse/navi_analytics.db`. Launch the dashboard, set the date range to
**2026-01-01 → 2026-04-30** with no segment filters, and the Overview, Agent Health and
Subscriptions tabs carry these numbers. Or ask the Analytics Agent directly — it reads the same
curated tables through the same metric definitions, and `tests/test_dashboard_agreement.py`
asserts across 77 comparisons that the two paths return identical values.
