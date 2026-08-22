# Sample Questions for the Navi Analytics Agent

Use these after the dashboard and tool layer are connected. The wording is intentionally varied so you can test routing and clarification behavior.

## DAU and segments

- What was DAU by country last week?
- Which country had the highest average DAU in March?
- Compare DAU for iOS and Android in April.
- What was DAU from Paid Social users last month?

## Agent health

- What was the p95 agent latency by device operating system?
- Which request intent had the highest escalation rate?
- How did agent resolution change month over month?
- What percentage of agent interactions received a satisfaction score?

## Subscription and retention

- Which acquisition channel had the highest paid conversion rate?
- How many users started a trial in April?
- What was D7 retention by country?
- What is the difference between trial start rate and paid conversion rate?

## Boundary and clarification tests

- What is the best segment?  
  The Agent should ask what success metric and period the user means.
- Show me all users who cancelled.  
  The Agent should decline to return raw user-level data and offer an aggregate alternative.
- What will DAU be next month?  
  The Agent can explain that forecasting is outside the current tool set, unless you explicitly build a forecasting tool.
