"""Step 8 verification: zero-code schema evolution through MCP runtime discovery.

One long-lived MCP server session runs against a TEMP COPY of the database (the real
enterprise.db is never touched). Mid-session a "data team" adds tables through a separate writable
connection. The same session then discovers, describes and queries them with no restart and no
code change, and the agent-facing read-only guarantees still hold on the new tables.

Run:  .\\.venv\\Scripts\\python.exe tests\\test_step8_migration.py
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

import bootstrap_data  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


async def call(session, tool: str, **args) -> dict:
    res = await session.call_tool(tool, args)
    return json.loads("".join(getattr(c, "text", "") for c in res.content))


async def scenario(db_copy: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "mcp_server.py"), db_copy, bootstrap_data.POLICY_DIR],
    )
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            r = await call(session, "mcp_list_tables")
            before = {t["table"] for t in r["tables"]}
            check("baseline: 9 tables, no demo tables", len(before) == 9 and not set(bootstrap_data.DEMO_TABLES) & before, str(before))
            check("baseline: demo_migration_applied() is False", not bootstrap_data.demo_migration_applied(db_copy))
            r = await call(session, "mcp_describe_table", table_name="overtime_requests")
            check("baseline: unknown table is reported as not found", r["status"] == "error", str(r))

            print("  ... data team migrates the database while the MCP session stays open ...")
            bootstrap_data.apply_demo_migration(db_copy)

            r = await call(session, "mcp_list_tables")
            after = {t["table"]: t["rows"] for t in r["tables"]}
            check("same session discovers the 2 new tables", set(bootstrap_data.DEMO_TABLES) <= set(after), str(after))
            check("row counts are visible on discovery", after.get("overtime_requests") == 3 and after.get("shift_swap_requests") == 3, str(after))
            check("demo_migration_applied() is True", bootstrap_data.demo_migration_applied(db_copy))

            r = await call(session, "mcp_describe_table", table_name="overtime_requests")
            cols = [c["name"] for c in r["columns"]]
            check("new table is describable (columns)", cols == ["request_id", "employee_id", "week_start", "extra_hours", "status", "note"], str(cols))
            check("new table exposes its foreign key", r["foreign_keys"] and r["foreign_keys"][0]["references_table"] == "employees", str(r["foreign_keys"]))

            r = await call(session, "mcp_read_query", sql_query=(
                "SELECT employee_id, extra_hours, status FROM overtime_requests WHERE employee_id = 'EMP_002'"))
            check("new data is queryable and answers a cross-table question (EMP_002 overtime is PENDING)",
                  r["status"] == "success" and r["rows"] == [["EMP_002", 8.0, "PENDING"]], str(r))

            r = await call(session, "mcp_read_query", sql_query=(
                "SELECT s.shift_id, w.status FROM shift_swap_requests w JOIN shifts s ON s.shift_id = w.shift_id "
                "WHERE w.status = 'APPROVED'"))
            check("join across old and new tables works", r["status"] == "success" and r["rows"] == [["SHF_009", "APPROVED"]], str(r))

            r = await call(session, "mcp_read_query", sql_query="DROP TABLE overtime_requests")
            check("AST guard still blocks mutation of a new table", r["status"] == "blocked", str(r))
            ro = sqlite3.connect(f"file:{db_copy.replace(chr(92), '/')}?mode=ro", uri=True)
            try:
                ro.execute("INSERT INTO overtime_requests VALUES ('X','EMP_001','2026-11-16',1,'PENDING','')")
                check("read-only URI still refuses writes on a new table", False, "write succeeded")
            except sqlite3.Error:
                check("read-only URI still refuses writes on a new table", True)
            finally:
                ro.close()

            print("  ... reset to baseline ...")
            bootstrap_data.reset_database(db_copy)
            r = await call(session, "mcp_list_tables")
            names = {t["table"] for t in r["tables"]}
            check("reset removes the demo tables again, same session", len(names) == 9 and not set(bootstrap_data.DEMO_TABLES) & names, str(names))


def main() -> int:
    bootstrap_data.bootstrap()
    real_before = os.path.getmtime(bootstrap_data.DB_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = os.path.join(tmp, "enterprise_copy.db")
        shutil.copy(bootstrap_data.DB_FILE, db_copy)
        asyncio.run(scenario(db_copy))
    check("the real enterprise.db was never modified", os.path.getmtime(bootstrap_data.DB_FILE) == real_before)
    print(f"\n{'ALL CHECKS PASSED' if not FAILURES else 'FAILED: ' + str(len(FAILURES)) + ' check(s)'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
