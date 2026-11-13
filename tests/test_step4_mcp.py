"""Step 4 verification: data-tier guardrails and the MCP server over real stdio JSON-RPC.

Run:  .\\.venv\\Scripts\\python.exe tests\\test_step4_mcp.py
Exits non-zero if any check fails.
"""
import asyncio
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

import bootstrap_data  # noqa: E402
import mcp_server  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# ------------------------------------------------------------------------------
# A. AST verification: is_sql_mutation
# ------------------------------------------------------------------------------
def test_is_sql_mutation() -> None:
    print("\nA. is_sql_mutation (sqlparse AST check)")
    safe = [
        "SELECT * FROM employees",
        "select employee_id from employees where site_id = 'SITE_KL';",
        "SELECT leave_type FROM time_off WHERE leave_type = 'DELETE'",          # keyword inside a literal
        "SELECT replace(full_name, ' ', '_') FROM employees",                    # REPLACE() function
        "WITH g AS (SELECT shift_id FROM shifts) SELECT * FROM g",               # CTE
        "SELECT s.shift_id, COUNT(a.employee_id) FROM shifts s "
        "LEFT JOIN assignments a ON a.shift_id = s.shift_id GROUP BY s.shift_id HAVING COUNT(*) < 3",
        "SELECT * FROM shifts /* harmless comment */",
        "SELECT updated_at, drop_reason FROM employees",                         # identifiers that contain keywords
    ]
    unsafe = [
        "DROP TABLE employees",
        "drop   table employees",
        "DeLeTe FROM employees",
        "UPDATE employees SET max_weekly_hours = 99",
        "INSERT INTO sites VALUES ('X','Y','Z')",
        "ALTER TABLE employees ADD COLUMN x TEXT",
        "TRUNCATE TABLE employees",
        "REPLACE INTO sites VALUES ('X','Y','Z')",
        "CREATE TABLE t (x INT)",
        "PRAGMA table_info(employees)",
        "ATTACH DATABASE 'x.db' AS x",
        "VACUUM",
        "SELECT 1; DROP TABLE employees",                                        # stacked statements
        "SELECT 1; SELECT 2",                                                    # any stacking is rejected
        "SELECT 1 /* hide */ ; /* hide */ DELETE FROM employees",
        "WITH g AS (SELECT 1) DELETE FROM employees",                            # mutation behind a CTE
        "",
        "   ",
        ";",
    ]
    for q in safe:
        check(f"allows : {q[:70]!r}", not mcp_server.is_sql_mutation(q))
    for q in unsafe:
        check(f"blocks : {q[:70]!r}", mcp_server.is_sql_mutation(q))


# ------------------------------------------------------------------------------
# B. Physical read-only lock (works even if the AST check were bypassed)
# ------------------------------------------------------------------------------
def test_read_only_connection() -> None:
    print("\nB. Physical read-only connection (file:...?mode=ro)")
    conn = mcp_server.connect_read_only()
    for label, sql in [
        ("INSERT is refused by SQLite", "INSERT INTO sites VALUES ('X','Y','Z')"),
        ("DROP is refused by SQLite", "DROP TABLE employees"),
        ("CREATE is refused by SQLite", "CREATE TABLE t (x INT)"),
        ("UPDATE is refused by SQLite", "UPDATE employees SET max_weekly_hours = 99"),
        ("arbitrary PRAGMA is refused by authorizer", "PRAGMA writable_schema = 1"),
        ("ATTACH is refused by authorizer", "ATTACH DATABASE ':memory:' AS x"),
    ]:
        try:
            conn.execute(sql)
            check(label, False, "statement executed")
        except sqlite3.Error as exc:
            check(label, True, str(exc))
    conn.close()
    n = sqlite3.connect(bootstrap_data.DB_FILE).execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    check("employees table untouched (12 rows)", n == 12, str(n))


# ------------------------------------------------------------------------------
# C. End to end over MCP stdio
# ------------------------------------------------------------------------------
def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def call(session, tool: str, **args) -> dict:
    raw = text_of(await session.call_tool(tool, args))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"{tool} returned non-JSON output: {raw[:300]!r}") from None


async def test_mcp_stdio() -> None:
    print("\nC. MCP server over stdio JSON-RPC")
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "mcp_server.py"), bootstrap_data.DB_FILE, bootstrap_data.POLICY_DIR],
    )
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = {t.name for t in (await session.list_tools()).tools}
            expected = {"mcp_list_tables", "mcp_describe_table", "mcp_read_query",
                        "mcp_list_policies", "mcp_read_policy"}
            check("server exposes the five discovery tools", expected <= tools, str(tools))
            check("server registers the decoy admin tool (to be stripped by tool_filter)",
                  "mcp_drop_table_admin" in tools)

            r = await call(session, "mcp_list_tables")
            names = {t["table"] for t in r["tables"]}
            check("mcp_list_tables returns all 9 tables", len(names) == 9 and "assignments" in names, str(names))

            r = await call(session, "mcp_describe_table", table_name="employee_skills")
            cols = [c["name"] for c in r["columns"]]
            check("mcp_describe_table returns columns", cols == ["employee_id", "skill_id", "certified_until"], str(cols))
            check("mcp_describe_table returns foreign keys", len(r["foreign_keys"]) == 2, str(r["foreign_keys"]))

            r = await call(session, "mcp_describe_table", table_name="time_off")
            by_col = {c["name"]: c for c in r["columns"]}
            check("describe lists exact allowed values for a status column",
                  by_col["status"].get("allowed_values") == ["APPROVED", "PENDING"], str(by_col["status"]))
            check("describe does not list keys, dates or ids as categories",
                  not any("allowed_values" in by_col[c] for c in ("time_off_id", "employee_id", "start_date")),
                  str(by_col))
            r = await call(session, "mcp_describe_table", table_name="employees")
            check("describe never lists personal-data columns as categories",
                  not any("allowed_values" in c for c in r["columns"] if c["name"] in ("full_name", "email", "phone")),
                  str([c["name"] for c in r["columns"] if "allowed_values" in c]))

            r = await call(session, "mcp_describe_table", table_name="employees); DROP TABLE employees;--")
            check("mcp_describe_table rejects an injected table name", r["status"] == "error", str(r))

            r = await call(session, "mcp_read_query", sql_query=(
                "SELECT s.shift_id, s.required_headcount - COUNT(a.employee_id) AS missing "
                "FROM shifts s LEFT JOIN assignments a ON a.shift_id = s.shift_id "
                "GROUP BY s.shift_id HAVING missing > 0 ORDER BY s.shift_id"))
            gaps = [row[0] for row in r.get("rows", [])]
            check("mcp_read_query finds the 3 coverage gaps", gaps == ["SHF_003", "SHF_010", "SHF_016"], str(r))

            for label, sql in [
                ("mcp_read_query blocks DROP", "DROP TABLE employees"),
                ("mcp_read_query blocks DELETE", "DELETE FROM assignments"),
                ("mcp_read_query blocks stacked SELECT; DROP", "SELECT 1; DROP TABLE employees"),
                ("mcp_read_query blocks PRAGMA", "PRAGMA table_info(employees)"),
            ]:
                r = await call(session, "mcp_read_query", sql_query=sql)
                check(label, r["status"] == "blocked", str(r))

            r = await call(session, "mcp_read_query", sql_query="SELECT * FROM no_such_table")
            check("mcp_read_query reports SQL errors cleanly", r["status"] == "error", str(r))

            r = await call(session, "mcp_list_policies")
            policies = {p["policy"] for p in r["policies"]}
            check("mcp_list_policies returns 3 documents", len(policies) == 3, str(policies))

            r = await call(session, "mcp_read_policy", policy_name="labor_compliance_handbook")
            check("mcp_read_policy returns document text", "11 hours of rest" in r.get("content", ""), str(r)[:120])

            for evil in ["../mcp_server", "..\\bootstrap_data", "../../etc/passwd", "enterprise.db", "nope"]:
                r = await call(session, "mcp_read_policy", policy_name=evil)
                check(f"mcp_read_policy rejects {evil!r}", r["status"] == "error", str(r)[:120])

            templates = [t.uri_template for t in (await session.list_resource_templates()).resource_templates]
            check("policy:// resource template is advertised", "policy://{name}" in templates, str(templates))
            res = await session.read_resource("policy://shift_definitions")
            body = "".join(getattr(c, "text", "") for c in res.contents)
            check("policy://shift_definitions resource is readable", "Required headcount" in body, body[:100])


def main() -> int:
    bootstrap_data.bootstrap()
    test_is_sql_mutation()
    test_read_only_connection()
    asyncio.run(test_mcp_stdio())
    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILED: ' + str(len(FAILURES)) + ' check(s)'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
