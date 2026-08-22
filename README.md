# Navi Analytics Agent

## Mobile App ETL, Streamlit Dashboard, and LLM Tool-Calling Project

This project starts with raw mobile app, AI study companion, and subscription data. You will build a curated analytics layer, create a Streamlit dashboard, and add an in-dashboard Analytics Agent that can answer warehouse-backed questions such as:

> What was DAU by country last week?

The project is designed for Python and SQL. R is not needed. Streamlit is the intended dashboard framework.

## Suggested workflow

1. Explore the raw files in `data/raw/` and document the data-quality choices you make.
2. Build and run an ETL pipeline that creates `warehouse/navi_analytics.db`.
3. Create dashboard metrics and visuals on top of that curated warehouse.
4. Add an Analytics Agent that calls an LLM plus a small set of read-only Python tools.
5. Use the sample questions to test whether dashboard values and Agent answers agree.

## Quick start

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp .env.example .env
# Add your OpenAI API key to .env when you are ready to test the Agent.

python src/etl_pipeline.py
streamlit run app.py
```

On Windows, copy `.env.example` to `.env` in File Explorer or use:

```powershell
Copy-Item .env.example .env
```

## Project folders

- `data/raw/`: source CSV files and data dictionary
- `data/`: suggested metric definitions
- `src/`: starter ETL and Agent scaffolds
- `warehouse/`: suggested location for the SQLite database created by ETL
- `docs/`: API setup, question ideas, and submission guidance
- `app.py`: Streamlit entry point
- `schema.sql`: suggested warehouse schema

## Using the OpenAI API

The live Agent uses the OpenAI API. ChatGPT Plus is not needed for this project, and a ChatGPT subscription does not include API usage. See `docs/openai_setup_and_cost.md` for setup guidance and a cost estimate.

The safest pattern is:

```text
question in Streamlit
  -> LLM chooses a documented tool
  -> Python validates the tool arguments
  -> parameterized SQL queries the curated warehouse
  -> aggregate result returns to the LLM
  -> answer and tool audit appear in Streamlit
```

## Practical constraints

- The Agent should be grounded in the curated warehouse rather than a hard-coded FAQ.
- Treat the LLM as an interface, not as a database administrator. Avoid arbitrary LLM-generated SQL and keep the tool layer read-only.
- Keep `OPENAI_API_KEY` in `.env`, never in source code, screenshots, or a repository.
- User-facing answers should stay at aggregate metric level. Raw user-level records are outside the project scope.
- Review `docs/sample_questions.md` while testing. Dashboard values and Agent answers should use the same metric definitions.

## Suggested final handoff

A review-ready submission usually includes a reproducible ETL pipeline, a SQLite database or rebuild steps, the Streamlit app, a concise quality report, screenshots, and a short business memo with insights and next actions.
