# Analytics Agent Design Notes

## What counts as an Analytics Agent in this project?

A chat box by itself is not an Agent. The Agent feature combines three pieces:

1. **LLM reasoning and routing**: the model reads the question and decides whether a documented tool is needed.
2. **Controlled data tools**: local Python functions validate inputs and retrieve approved aggregates from the curated warehouse.
3. **Grounded explanation**: the model converts the returned data into a concise answer with context and caveats.

## Suggested tool types

A useful first version can include three or more tools. One should handle DAU by group. Other tools can cover the product questions your dashboard supports.

```text
get_dau_by_group(...)
get_agent_health_by_group(...)
get_subscription_metrics_by_group(...)
get_metric_definition(...)
```

## Safe tool design

A tool should have a narrow responsibility. For example, `get_dau_by_group` can accept a date range, an allow-listed group dimension, and a small set of validated filters. It can then return a small aggregate table.

Good safeguards include:

- allow-listing group-by fields
- validating date formats and date order
- validating filter values
- parameterized SQL queries
- read-only database access
- aggregate results only
- graceful handling of empty results

Avoid giving the model a raw database connection or asking it to generate unrestricted SQL. The goal is a transparent analytics interface, not autonomous database administration.

## Answer quality checklist

A good Agent answer usually states:

- the metric definition
- date range
- segment or filters
- requested result
- an interpretation or caveat when useful

Example:

> From March 1 through March 7, DAU in the US averaged 412 users per day. This uses users with at least one non-test app event after event de-duplication. iOS accounted for 58% of US DAU during the period.

## Suggested Agent test cases

- A direct question with an available metric and date range
- A question that needs the current dashboard filters
- An ambiguous question that should trigger clarification
- An unsupported metric that should receive a helpful boundary message
- A date range with no available data
- A question that attempts to request raw user-level data
