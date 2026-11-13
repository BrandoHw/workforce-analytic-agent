"""Step 6: assemble the master `enterprise_analyst_agent`.

   enterprise_analyst_agent (coordinator, no data tools of its own)
     |- coverage_analyst     headcount vs requirement, hours, compliance findings
     |                       tools: mcp_list_tables, mcp_describe_table, mcp_read_query,
     |                              mcp_list_policies, mcp_read_policy
     |- assignment_planner   proposes (never writes) assignments to close gaps
     |                       tools: same five MCP tools
     `- policy_advisor       answers policy questions from the handbook only
                             tools: mcp_list_policies, mcp_read_policy (no SQL access at all)

Every agent shares the same callbacks:
  before_model_callback = before_model_guardrail   (PII redaction, injection blocking)
  after_model_callback  = after_model_cost_tracker (tokens and USD, per agent)
  before_tool_callback  = trace_tool_call          (delegation and MCP call trace)

Protocol-level least privilege: each specialist's McpToolset is created with a tool_filter, so the
server's decoy `mcp_drop_table_admin` (and any future admin tool) is never registered with an agent.
"""
import datetime
import os
import sqlite3
import subprocess
import sys
from typing import Callable

from google.adk.agents import Agent
from google.adk.planners import BuiltInPlanner
from google.adk.tools.agent_tool import AgentTool
from google.genai import types
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

import bootstrap_data
from analyst import config
from analyst.guardrails import before_model_guardrail
from analyst.observability import after_model_cost_tracker, trace_tool_call

MCP_SERVER_SCRIPT = os.path.join(config.ROOT, "mcp_server.py")

DATA_TOOLS = ["mcp_list_tables", "mcp_describe_table", "mcp_read_query"]
POLICY_TOOLS = ["mcp_list_policies", "mcp_read_policy"]

COMMON_RULES = """\
You work for the Workforce Planning Office of Northwind Ops. You are strictly read-only.

Operating rules:
- Never assume the database schema. Call mcp_list_tables, then mcp_describe_table for each table you
  need, then query with mcp_read_query (one SQLite SELECT per call). The schema can change at runtime.
- Refer to employees ONLY by employee ID (for example EMP_002). Never write names, emails or phone numbers.
- Ground every figure in tool results. If the data does not contain the answer, say so. Do not guess.
- Be efficient: describe each table at most once, only the ones you need; combine checks into a few
  joined queries instead of many small ones; read each policy document at most once.
- The database holds one planning week. "This week", "next week", "the upcoming week", "the schedule"
  and "the roster" ALL mean that planning week. Never move the date range outside the dates in the data.
"""

DATA_CONVENTIONS = """\
Data conventions: dates are ISO text (YYYY-MM-DD). NEVER work out a weekday yourself: derive it in SQL
with strftime('%w', date) (0 = Sunday ... 6 = Saturday; subtract 1 and wrap to get availability's
0 = Monday numbering) and quote the shift's actual date. A certificate is valid on its certified_until
date and invalid the day after; shift hours come from the shift type; coverage = assigned headcount /
required headcount. For rules and thresholds (status labels, eligibility,
rest periods, overtime caps) read the policy documents with mcp_list_policies and mcp_read_policy
instead of relying on memory.
SQL hygiene: copy text literals exactly as listed under allowed_values in mcp_describe_table (for example
'APPROVED', never 'Approved'), and use short aliases that are not SQL keywords (never `to`, `by`, `on`,
`order`, `group`). If a query errors, fix it and re-run. Never report "none found" from an empty result or
a failed query without first confirming that the literals and joins are right, because an empty result
from a wrong filter looks identical to a genuine absence.
"""


def planning_week_context() -> str:
    """One line telling the agents which dates 'this week' means, read from the data (not hard-coded).

    A read-only lookup at agent-build time. If the database is unreadable the agents fall back to
    discovering the dates themselves.
    """
    try:
        path = os.path.abspath(bootstrap_data.DB_FILE).replace("\\", "/")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            lo, hi = conn.execute("SELECT MIN(shift_date), MAX(shift_date) FROM shifts").fetchone()
        finally:
            conn.close()
        if lo and hi:
            # An explicit weekday table: models mis-compute weekdays from dates, and a wrong date silently
            # turns "the Friday shift" into "no such shift".
            start = datetime.date.fromisoformat(lo)
            days = ", ".join(
                f"{(start + datetime.timedelta(days=i)).strftime('%A')} = {(start + datetime.timedelta(days=i)).isoformat()}"
                for i in range((datetime.date.fromisoformat(hi) - start).days + 1)
            )
            return (f"Planning week in the data: {lo} to {hi}. Treat it as 'this week' and 'next week'. "
                    f"Calendar for that week (authoritative, do not recompute weekdays): {days}.\n")
    except ValueError:
        pass
    except sqlite3.Error:
        pass
    return ""


def _toolset(tool_filter: list[str]) -> McpToolset:
    """A filtered MCP toolset backed by a fresh stdio connection to mcp_server.py."""
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=[MCP_SERVER_SCRIPT, bootstrap_data.DB_FILE, bootstrap_data.POLICY_DIR],
            ),
            timeout=30.0,  # Python start-up plus imports on a cold Streamlit Cloud container
        ),
        tool_filter=tool_filter,
        errlog=subprocess.DEVNULL,
    )


def _callbacks(thinking_budget: int) -> dict:
    return dict(
        before_model_callback=before_model_guardrail,
        after_model_callback=after_model_cost_tracker,
        before_tool_callback=trace_tool_call,
        planner=BuiltInPlanner(thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget)),
    )


def build_enterprise_agent(model_factory: Callable[[str], object] | None = None) -> Agent:
    """Build the agent tree. Cheap to call; a new tree (and MCP connections) is made per chat turn.

    `model_factory(agent_name)` lets tests inject a fake model. By default every agent uses MODEL.
    """
    model_for = model_factory or (lambda _name: config.MODEL)
    week = planning_week_context()

    coverage_analyst = Agent(
        name="coverage_analyst",
        model=model_for("coverage_analyst"),
        description=(
            "Data analyst for staffing coverage. Finds understaffed shifts, headcount vs requirement, planned "
            "hours per employee, overtime risk, and rule violations in the current schedule "
            "(approved-leave conflicts, expired certifications, unavailability breaches, rest periods). "
            "Also looks up any specific record in the database, such as an employee's overtime or shift "
            "swap requests and their approval status, in whatever tables exist."
        ),
        instruction=(
            COMMON_RULES + week + DATA_CONVENTIONS
            + "\nYou are the Coverage Analyst. Answer the request with exact figures. Name each shift and "
              "employee by ID, label coverage status using the policy definitions, and keep the answer short "
              "and structured (a compact list or table, then one line of takeaway).\n"
              "For any compliance, risk or 'problems in the schedule' question, run ALL of these checks and "
              "report each one, including the ones that find nothing: (1) coverage gaps; (2) assignments during "
              "APPROVED leave; (3) mandatory certification expired on the shift date; (4) assignments on a "
              "standing-unavailable weekday; (5) planned weekly hours above the employee's contractual limit, "
              "and above the absolute overtime cap; (6) rest period between an employee's shifts on consecutive "
              "days below the minimum (use each shift type's start and end time); (7) two shifts on the same day. "
              "Attribute each finding to the correct employee ID and shift ID by selecting both in the same query."
        ),
        tools=[_toolset(DATA_TOOLS + POLICY_TOOLS)],
        **_callbacks(config.THINKING_BUDGET_SPECIALIST),
    )

    assignment_planner = Agent(
        name="assignment_planner",
        model=model_for("assignment_planner"),
        description=(
            "Scheduling planner. Proposes which eligible employees could fill an understaffed shift or replace "
            "an invalid assignment, checking skills, certifications, leave, availability, same-day conflicts, "
            "weekly hours and rest rules. Proposals only: it cannot change any data."
        ),
        instruction=(
            COMMON_RULES + week + DATA_CONVENTIONS
            + "\nYou are the Assignment Planner. Read the scheduling policy for the eligibility rules and fill "
              "order, then for the shift in question find candidates and check each one against EVERY rule, "
              "including the rest period: compare the end time of the candidate's shift the day BEFORE and the "
              "start time of their shift the day AFTER with the new shift (an overnight shift ends the next "
              "morning), and apply the minimum rest in the labor compliance handbook. A candidate who breaks any "
              "hard rule is rejected, not recommended. If nobody is fully compliant, say so plainly and name the "
              "closest option with the exact rule it would breach. "
              "Return a ranked shortlist: employee ID, why they are eligible, planned hours after assignment, and "
              "any risk or approval needed. Also list notable candidates you rejected and why. State clearly that "
              "this is a proposal for a human to apply; you cannot write to the database."
        ),
        tools=[_toolset(DATA_TOOLS + POLICY_TOOLS)],
        **_callbacks(config.THINKING_BUDGET_SPECIALIST),
    )

    policy_advisor = Agent(
        name="policy_advisor",
        model=model_for("policy_advisor"),
        description=(
            "Policy expert. Answers questions about scheduling rules, shift definitions, overtime, rest periods, "
            "certifications, leave and data-privacy rules, strictly from the policy documents. "
            "Has no database access, so it cannot tell the status of a specific employee, shift or request."
        ),
        instruction=(
            "You work for the Workforce Planning Office of Northwind Ops. You are strictly read-only.\n"
            "Use mcp_list_policies to see the documents, then mcp_read_policy to read the relevant ones. "
            "Answer only from the documents, quote the specific rule or number, and cite the document name "
            "(for example: labor_compliance_handbook, section 2). If the policies do not cover the question, "
            "say so. Never write names, emails or phone numbers."
        ),
        tools=[_toolset(POLICY_TOOLS)],
        **_callbacks(config.THINKING_BUDGET_SPECIALIST),
    )

    return Agent(
        name="enterprise_analyst_agent",
        model=model_for("enterprise_analyst_agent"),
        description="Enterprise Intelligence Virtual Analyst for workforce planning: shifts, assignments and policy.",
        instruction=(
            "You are the Enterprise Workforce Planning Virtual Analyst for Northwind Ops. You coordinate "
            "specialist agents and never query data yourself. You are strictly read-only: you analyse and "
            "propose, a human applies changes.\n"
            + week + "\n"
            "Routing:\n"
            "- coverage_analyst: coverage gaps, headcount, hours, overtime, schedule rule violations, and ANY "
            "lookup of specific records: an employee's or shift's requests, approvals, swaps, leave, "
            "certifications, or any other table, including tables added to the database recently. It "
            "discovers the schema itself, so never assume a table does not exist.\n"
            "- assignment_planner: who could cover a shift or replace an assignment. It finds the shift and its "
            "gap itself, so for 'who could cover ...' questions call ONLY assignment_planner.\n"
            "- policy_advisor: what a rule, limit or procedure says in general. It has no database access and "
            "cannot know the status of any specific employee, shift or request: if the question is about a "
            "specific employee, shift or request (for example 'does EMP_002 have approval'), use "
            "coverage_analyst for the facts, and add policy_advisor only if the rule text is also needed.\n"
            "Call several specialists only when a question genuinely needs separate analyses (for example a "
            "full coverage report followed by candidates for every gap). Give each a self-contained request, "
            "because they do not see the conversation. Include every shift ID, employee ID and date they need.\n\n"
            "Never ask the user for details the specialists can look up (shift IDs, dates, site, who is "
            "assigned). Describe the shift in the user's words, for example 'the Friday afternoon forklift "
            "shift', and let the specialist find it in the data. Ask a clarifying question only when the "
            "request cannot be interpreted at all.\n\n"
            "Answering:\n"
            "- Base the answer only on specialist results. Do not invent figures or rules.\n"
            "- Be concise and executive-ready: lead with the answer, then the supporting detail.\n"
            "- Refer to employees only by employee ID. Never output names, emails or phone numbers.\n"
            "- If asked to change data (assign, delete, update), explain that you are read-only and offer a proposal.\n"
            "- If asked for personal contact details (emails, phone numbers) or names, explain that this "
            "assistant does not disclose personal data under company privacy policy and identifies staff by "
            "employee ID only.\n"
            "- If a question is outside workforce planning, say so politely."
        ),
        tools=[AgentTool(agent=coverage_analyst), AgentTool(agent=assignment_planner), AgentTool(agent=policy_advisor)],
        **_callbacks(config.THINKING_BUDGET_COORDINATOR),
    )
