"""Starter tools for the Navi Analytics Agent.

The LLM receives only these tool definitions. Each function should validate
arguments and query the curated SQLite warehouse using parameterized SQL.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "warehouse" / "navi_analytics.db"

VALID_GROUPS = ["country_code", "device_os", "acquisition_channel", "app_version"]

TOOLS = [
    {
        "type": "function",
        "name": "get_dau_by_group",
        "description": "Return daily active users by an approved segment over a date range.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "group_by": {"type": "string", "enum": VALID_GROUPS},
                "country_code": {"type": ["string", "null"]},
                "device_os": {"type": ["string", "null"]},
                "acquisition_channel": {"type": ["string", "null"]},
                "app_version": {"type": ["string", "null"]},
            },
            "required": [
                "start_date", "end_date", "group_by", "country_code",
                "device_os", "acquisition_channel", "app_version"
            ],
            "additionalProperties": False,
        },
    },
    # TODO: Add at least two additional, narrow, read-only tools.
    # Examples: get_agent_health_by_group, get_subscription_metrics_by_group,
    # get_retention_by_group, get_metric_definition.
]


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError("Warehouse not found. Run your ETL pipeline first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_group(group_by: str) -> str:
    if group_by not in VALID_GROUPS:
        raise ValueError(f"Unsupported group_by: {group_by}")
    return group_by


def get_dau_by_group(**kwargs: Any) -> dict[str, Any]:
    """Return aggregate DAU results using parameterized SQL.

    Suggested source: mart_user_day joined to dim_users, or another curated
    layer that reflects your documented DAU definition.
    """
    # TODO: validate dates, validate group_by, build only approved filter clauses,
    # run parameterized SQL, and return a small JSON-serializable result.
    raise NotImplementedError("Implement get_dau_by_group.")


def dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route only allow-listed tool names to local functions."""
    handlers = {
        "get_dau_by_group": get_dau_by_group,
        # TODO: add your other tool handlers here.
    }
    if name not in handlers:
        return {"error": f"Unsupported tool: {name}"}
    try:
        return handlers[name](**arguments)
    except Exception as exc:
        return {"error": str(exc)}
