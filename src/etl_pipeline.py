"""Starter ETL scaffold for the Navi Analytics Agent project.

Suggested workflow:
1. Read raw CSV files from data/raw.
2. Profile data quality and document decisions.
3. Produce cleaned data frames.
4. Load curated tables into warehouse/navi_analytics.db.
5. Create marts that the Streamlit dashboard and Analytics Agent can share.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw"
WAREHOUSE_DIR = PROJECT_ROOT / "warehouse"
DB_PATH = WAREHOUSE_DIR / "navi_analytics.db"
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"


def extract_raw_data() -> dict[str, pd.DataFrame]:
    """Read the four raw source files into a dictionary of data frames."""
    return {
        "users": pd.read_csv(RAW_DIR / "users.csv"),
        "app_events": pd.read_csv(RAW_DIR / "app_events.csv"),
        "agent_interactions": pd.read_csv(RAW_DIR / "agent_interactions.csv"),
        "subscriptions": pd.read_csv(RAW_DIR / "subscriptions.csv"),
    }


def profile_raw_data(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Create a compact profiling summary for your data-quality note."""
    rows = []
    for name, df in raw.items():
        rows.append({
            "table": name,
            "row_count": len(df),
            "column_count": len(df.columns),
            "null_cells": int(df.isna().sum().sum()),
            "duplicate_rows": int(df.duplicated().sum()),
        })
    return pd.DataFrame(rows)


def transform_users(users: pd.DataFrame) -> pd.DataFrame:
    """Standardize user-level attributes and derive signup_date."""
    # TODO: parse signup_ts, derive signup_date, and apply any category standardization.
    # TODO: select columns that match dim_users in schema.sql.
    return users.copy()


def transform_app_events(events: pd.DataFrame) -> pd.DataFrame:
    """Prepare application events for fact_app_events.

    Think through duplicate event identifiers, test traffic, timestamp parsing,
    missing screen names, and user IDs that do not map to dim_users.
    """
    # TODO: parse event_ts and derive event_date.
    # TODO: create included_in_metrics based on a documented policy.
    # TODO: avoid double-counting duplicate event IDs in KPI logic.
    return events.copy()


def transform_agent_interactions(interactions: pd.DataFrame) -> pd.DataFrame:
    """Prepare agent requests for fact_agent_interactions.

    Consider nullable tool fields, long-tail latency values, optional ratings,
    response statuses, and the relationship between resolution and escalation.
    """
    # TODO: parse request_ts and derive request_date.
    # TODO: define included_in_metrics and any quality flags.
    return interactions.copy()


def transform_subscriptions(subscriptions: pd.DataFrame) -> pd.DataFrame:
    """Prepare subscription lifecycle data for fact_subscriptions."""
    # TODO: parse lifecycle timestamp columns and retain lifecycle semantics.
    return subscriptions.copy()


def build_user_day_mart(conn: sqlite3.Connection) -> None:
    """Create a user-day mart that supports DAU and agent-adoption metrics.

    The exact definition is intentionally open. Keep it aligned with your
    documented event-quality and metric rules.
    """
    # TODO: replace this placeholder with a CREATE TABLE AS SELECT statement.
    pass


def initialize_database(conn: sqlite3.Connection) -> None:
    """Apply the suggested schema."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_table(df: pd.DataFrame, table_name: str, conn: sqlite3.Connection) -> None:
    """Load a transformed data frame into SQLite."""
    df.to_sql(table_name, conn, if_exists="replace", index=False)


def run_pipeline() -> None:
    WAREHOUSE_DIR.mkdir(exist_ok=True)
    raw = extract_raw_data()
    profile = profile_raw_data(raw)
    print("Raw profile:\n", profile.to_string(index=False))

    users = transform_users(raw["users"])
    events = transform_app_events(raw["app_events"])
    interactions = transform_agent_interactions(raw["agent_interactions"])
    subscriptions = transform_subscriptions(raw["subscriptions"])

    with sqlite3.connect(DB_PATH) as conn:
        initialize_database(conn)
        # TODO: choose a load pattern. The starter schema is a suggestion.
        # load_table(users, "dim_users", conn)
        # load_table(events, "fact_app_events", conn)
        # load_table(interactions, "fact_agent_interactions", conn)
        # load_table(subscriptions, "fact_subscriptions", conn)
        # build_user_day_mart(conn)
        pass

    print(f"ETL scaffold completed. Build your curated warehouse at: {DB_PATH}")


if __name__ == "__main__":
    run_pipeline()
