# Enterprise Workforce Planning Virtual Analyst

A multi-agent analyst that answers shift-planning questions (coverage gaps, overtime, leave conflicts, who can
cover a shift, policy) from a SQLite database and policy documents, through a read-only MCP server.
Built with Google ADK + Gemini, MCP, Streamlit and sqlparse. The company and all data are fictional.

## Architecture

```
Streamlit app.py
  -> before_model_guardrail (PII masking, prompt-injection and mutation-intent blocking)
  -> enterprise_analyst_agent (coordinator)
       -> coverage_analyst      data + policy tools
       -> assignment_planner    data + policy tools
       -> policy_advisor        policy tools only
            -> MCP server (stdio): mcp_list_tables, mcp_describe_table, mcp_read_query,
                                   mcp_list_policies, mcp_read_policy
                 -> enterprise.db (opened read-only) + policies/*.md
  -> after_model_cost_tracker (tokens and estimated USD, per call and per agent)
```

| Pillar | Where |
| --- | --- |
| Multi-agent coordinator and specialists | `analyst/agents.py` |
| MCP discovery primitives (SQLite + docs) | `mcp_server.py` |
| Input guardrail (PII, injection) | `analyst/guardrails.py` |
| Data guardrail (`is_sql_mutation` AST check, `mode=ro`, authorizer, table allow-list) | `mcp_server.py` |
| Token and USD telemetry | `analyst/observability.py` |
| Golden cases and LLM-as-a-Judge | `eval/capstone_golden.json`, `analyst/evaluation.py` |
| Streamlit app, `st.secrets` | `app.py` |

Agents are never given the schema in a prompt. They discover tables, columns and exact status values through
MCP at run time, so tables added by the **Live Migration Simulator** (sidebar) are usable immediately with no
code change.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   # then put your Gemini key in it
.\.venv\Scripts\python.exe -m streamlit run app.py
```

`enterprise.db` and `policies/` are (re)created automatically by `bootstrap_data.py` if missing.

## Tests

Offline (no API key, no cost): `python tests/test_step4_mcp.py`, `test_step5_callbacks.py`,
`test_step6_agents.py`, `test_step7_eval.py`, `test_step8_migration.py`, `test_step9_app.py`.

Live evaluation (about 5 minutes, a few cents): `python eval/run_eval.py` (optionally `--only id1,id2`), or use
the **Evaluation** view in the app. A case passes with a judge score of 4 or more (rubric 1 to 5) and every
deterministic check green. Model output varies between runs, so scores can differ by a point.

## Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repository (`.streamlit/secrets.toml` is git-ignored; commit `enterprise.db`
   and `policies/`).
2. On [share.streamlit.io](https://share.streamlit.io) choose **Create app**, pick the repository and branch,
   and set the main file to `app.py`. Under **Advanced settings** pick Python 3.12 or 3.13.
3. In **Advanced settings -> Secrets** (or later **App settings -> Secrets**) add:

   ```toml
   GOOGLE_API_KEY = "your_gemini_api_key_here"
   ```

4. Deploy. The app reads the key from `st.secrets`, falling back to the environment locally.

Notes: the database is shared by all visitors of a deployed app, and Community Cloud storage is ephemeral, so
the migration simulator's changes reset when the app restarts. Cost figures use list prices set in
`analyst/observability.py` and are estimates, not a bill.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKFORCE_MODEL` | `gemini-4-hydrogen` | Model for the agents |
| `WORKFORCE_JUDGE_MODEL` | `gemini-4-hydrogen` | Model for the evaluation judge |
| `WORKFORCE_THINK_COORDINATOR` / `WORKFORCE_THINK_SPECIALIST` | `0` / `4096` | Thinking-token budgets per agent role |
