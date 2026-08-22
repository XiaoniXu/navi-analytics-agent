"""Starter Streamlit app for the Navi Analytics Agent project."""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "warehouse" / "navi_analytics.db"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

st.set_page_config(page_title="Navi Product Analytics", layout="wide")
st.title("Navi Product Analytics")
st.caption("Mobile app ETL, product dashboard, and tool-using Analytics Agent")

if not DB_PATH.exists():
    st.warning("No warehouse found yet. Complete the ETL pipeline, then launch the dashboard again.")
    st.stop()

# TODO: Create global date and segment filters based on your curated warehouse.
# TODO: Query your warehouse for KPI cards and product-health visuals.
# TODO: Keep metric definitions and date-filter logic consistent across tabs.
# TODO: Add a dedicated Ask Navi Analytics Agent tab with a tool audit.

sections = ["Overview", "Active Users", "Agent Health", "Subscriptions", "Ask Navi Analytics Agent"]
selected = st.sidebar.radio("Explore", sections)

if selected == "Overview":
    st.info("Build KPI cards and a concise product-health view here.")
elif selected == "Active Users":
    st.info("Build DAU and segment views here.")
elif selected == "Agent Health":
    st.info("Build agent quality, latency, resolution, escalation, and CSAT views here.")
elif selected == "Subscriptions":
    st.info("Build trial, paid conversion, cancellation, and retention views here.")
else:
    st.info("Connect your Analytics Agent here after implementing src/agent_tools.py.")
    # Example integration after your tool functions work:
    # from analytics_agent import run_analytics_agent
    # question = st.chat_input("Ask about DAU, agent health, retention, or subscriptions")
    # if question:
    #     answer, audit = run_analytics_agent(question, dashboard_context={})
    #     st.write(answer)
    #     with st.expander("Tool audit"):
    #         st.json(audit)
