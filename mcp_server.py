"""Step 4: Model Context Protocol (MCP) tools and data-tier security guardrails.

A real MCP server (stdio / JSON-RPC) that exposes the workforce database and policy library:

  mcp_list_tables()           discover tables at runtime (no static schema in any prompt)
  mcp_describe_table(name)    columns, keys and foreign keys of one table
  mcp_read_query(sql)         read-only SELECT, verified by an AST check before execution
  mcp_list_policies()         discover policy documents
  mcp_read_policy(name)       read one policy document (allow-listed, no path traversal)

Data-tier guardrails, layered (defence in depth):
  1. Protocol-level least privilege: agents register only the tools they need through
     ADK `tool_filter`. The decoy `mcp_drop_table_admin` below exists to prove it is stripped.
  2. AST verification: `is_sql_mutation` parses the SQL with sqlparse and rejects anything that is
     not exactly one SELECT statement, however the request was phrased.
  3. Physical read-only lock: connections use `file:enterprise.db?mode=ro` (URI), plus
     `PRAGMA query_only` and a SQLite authorizer that only allows reads.

Run standalone:  python mcp_server.py [db_path] [policy_dir]
"""
import json
import os
import re
import sqlite3
import sys

import sqlparse
from sqlparse import tokens as T
from mcp.server.mcpserver import MCPServer

import bootstrap_data

DB_FILE = bootstrap_data.DB_FILE
POLICY_DIR = bootstrap_data.POLICY_DIR
MAX_ROWS = 100

mcp = MCPServer("WorkforcePlanningMCPServer")


# ------------------------------------------------------------------------------
# Data-tier guardrail: AST verification
# ------------------------------------------------------------------------------
# Keywords that must never appear as SQL keywords in a read query.
_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "REPLACE",
    "PRAGMA", "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE", "RENAME", "GRANT",
    "REVOKE", "UPSERT", "MERGE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
}


def is_sql_mutation(sql_query: str) -> bool:
    """Return True if `sql_query` is anything other than a single read-only SELECT.

    Uses the sqlparse token tree, not substring matching, so string literals such as
    WHERE leave_type = 'DELETE' are fine while `DROP TABLE x` is rejected however it is
    spaced, cased, commented or chained (`SELECT 1; DROP TABLE x`).
    The check fails closed: empty or unparseable input is treated as unsafe.
    """
    if not sql_query or not sql_query.strip():
        return True

    # Comments can hide intent from a naive reader; remove them before analysing.
    cleaned = sqlparse.format(sql_query, strip_comments=True).strip()
    statements = [s for s in sqlparse.parse(cleaned) if str(s).strip().strip(";").strip()]
    if len(statements) != 1:
        return True  # nothing to run, or stacked statements

    stmt = statements[0]
    if stmt.get_type() != "SELECT":  # also covers WITH ... SELECT; rejects WITH ... DELETE
        return True

    for token in stmt.flatten():
        # Only inspect real keywords, so literals and identifiers cannot trigger false positives.
        if token.ttype is None or token.ttype not in T.Keyword:  # `in` also matches Keyword.DML / DDL
            continue
        if token.value.upper().strip() in _FORBIDDEN_KEYWORDS:
            return True
        if token.ttype in (T.Keyword.DML, T.Keyword.DDL) and token.value.upper() != "SELECT":
            return True
    return False


# ------------------------------------------------------------------------------
# Data-tier guardrail: physical read-only connection
# ------------------------------------------------------------------------------
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


# Schema introspection used by mcp_describe_table. No other PRAGMA is permitted.
_ALLOWED_PRAGMAS = {"table_info", "foreign_key_list"}


def _authorizer(action, arg1, arg2, db_name, trigger):
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _ALLOWED_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect_read_only() -> sqlite3.Connection:
    """Open the database through a read-only URI. Writes fail at the SQLite layer."""
    path = os.path.abspath(DB_FILE).replace("\\", "/")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ------------------------------------------------------------------------------
# MCP tools: database discovery and querying
# ------------------------------------------------------------------------------
@mcp.tool()
def mcp_list_tables() -> str:
    """Discover every table currently in the workforce database, with row counts.
    Always call this first; the schema can change at runtime."""
    conn = connect_read_only()
    try:
        tables = [
            {"table": t, "rows": conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(t)}").fetchone()[0]}
            for t in _table_names(conn)
        ]
        return json.dumps({"status": "success", "tables": tables})
    finally:
        conn.close()


# Columns whose values are identifiers, dates, free text or personal data are never listed as categories.
_NOT_CATEGORICAL = re.compile(r"(_id$|^id$|date|time|name|email|phone|address|reason|note|comment|desc)", re.I)
_MAX_CATEGORIES = 8


def _allowed_values(conn: sqlite3.Connection, table: str, column: dict, fk_columns: set[str]) -> list[str] | None:
    """Distinct values of a low-cardinality text column (e.g. status), or None if it is not categorical.

    Agents otherwise guess literals ('Approved' vs 'APPROVED') and a wrong literal silently returns zero
    rows, which a compliance analyst could misread as "no problem found".
    """
    name = column["name"]
    if (column["primary_key"] or name in fk_columns or _NOT_CATEGORICAL.search(name)
            or "CHAR" not in column["type"].upper() and "TEXT" not in column["type"].upper()):
        return None
    rows = conn.execute(
        f"SELECT DISTINCT {_quote_ident(name)} FROM {_quote_ident(table)} "
        f"WHERE {_quote_ident(name)} IS NOT NULL LIMIT {_MAX_CATEGORIES + 1}"
    ).fetchall()
    values = [str(r[0]) for r in rows]
    if not values or len(values) > _MAX_CATEGORIES or any(len(v) > 30 for v in values):
        return None
    return sorted(values)


@mcp.tool()
def mcp_describe_table(table_name: str) -> str:
    """Describe one table: column names, types, primary keys, foreign keys and, for category-like text
    columns (such as status), the exact allowed values. Use this before writing SQL so column names and
    text literals (their exact spelling and case) are never guessed."""
    conn = connect_read_only()
    try:
        requested = table_name.strip().strip("'\"` ")
        match = next((t for t in _table_names(conn) if t.lower() == requested.lower()), None)
        if match is None:  # allow-list check: the name never reaches SQL unless it is a real table
            return json.dumps({
                "status": "error",
                "message": f"Table '{requested}' not found.",
                "available_tables": _table_names(conn),
            })
        q = _quote_ident(match)
        columns = [
            {"name": r[1], "type": r[2], "not_null": bool(r[3]), "primary_key": bool(r[5])}
            for r in conn.execute(f"PRAGMA table_info({q})").fetchall()
        ]
        foreign_keys = [
            {"column": r[3], "references_table": r[2], "references_column": r[4]}
            for r in conn.execute(f"PRAGMA foreign_key_list({q})").fetchall()
        ]
        fk_columns = {fk["column"] for fk in foreign_keys}
        for col in columns:
            values = _allowed_values(conn, match, col, fk_columns)
            if values is not None:
                col["allowed_values"] = values
        return json.dumps({"status": "success", "table": match, "columns": columns, "foreign_keys": foreign_keys})
    finally:
        conn.close()


@mcp.tool()
def mcp_read_query(sql_query: str) -> str:
    """Run one read-only SQL SELECT statement (SQLite dialect) and return rows as JSON.
    Mutations (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE ...) are blocked."""
    if is_sql_mutation(sql_query):
        return json.dumps({
            "status": "blocked",
            "error": "Blocked by data-tier guardrail: only a single read-only SELECT statement is allowed.",
        })
    conn = connect_read_only()
    try:
        cur = conn.execute(sql_query.strip())
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        return json.dumps({
            "status": "success",
            "columns": columns,
            "rows": [list(r) for r in rows[:MAX_ROWS]],
            "row_count": min(len(rows), MAX_ROWS),
            "truncated": truncated,
        })
    except sqlite3.Error as exc:
        return json.dumps({"status": "error", "message": str(exc)})
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# MCP tools and resources: policy knowledge base
# ------------------------------------------------------------------------------
_POLICY_NAME = re.compile(r"^[A-Za-z0-9_\-]+$")


def _policy_files() -> dict[str, str]:
    """Allow-list: {document name without extension: absolute path} for policies/*.md only."""
    if not os.path.isdir(POLICY_DIR):
        return {}
    return {
        os.path.splitext(f)[0]: os.path.join(POLICY_DIR, f)
        for f in sorted(os.listdir(POLICY_DIR))
        if f.lower().endswith(".md")
    }


def _read_policy(name: str) -> dict:
    cleaned = name.strip()
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    files = _policy_files()
    if not _POLICY_NAME.match(cleaned) or cleaned not in files:
        return {"status": "error", "message": f"Policy '{name}' not found.", "available_policies": sorted(files)}
    with open(files[cleaned], encoding="utf-8") as f:
        return {"status": "success", "policy": cleaned, "content": f.read()}


@mcp.tool()
def mcp_list_policies() -> str:
    """List the enterprise policy documents (scheduling, shifts, labor compliance)."""
    policies = []
    for name, path in _policy_files().items():
        with open(path, encoding="utf-8") as f:
            title = f.readline().lstrip("# ").strip()
        policies.append({"policy": name, "title": title})
    return json.dumps({"status": "success", "policies": policies})


@mcp.tool()
def mcp_read_policy(policy_name: str) -> str:
    """Read the full text of one policy document by name (see mcp_list_policies).
    Cite the policy name when answering compliance questions."""
    return json.dumps(_read_policy(policy_name))


@mcp.resource("policy://{name}", mime_type="text/markdown")
def policy_resource(name: str) -> str:
    """The same policy library exposed as an MCP resource template."""
    result = _read_policy(name)
    return result["content"] if result["status"] == "success" else result["message"]


# ------------------------------------------------------------------------------
# Decoy: proves protocol-level least privilege
# ------------------------------------------------------------------------------
@mcp.tool()
def mcp_drop_table_admin(table_name: str) -> str:
    """DECOY admin tool. It is registered on the server but agents must never receive it:
    ADK `tool_filter` strips it. It performs no action even if called."""
    return f"DECOY: refused to drop {table_name}. This tool must never be exposed to an agent."


def main() -> None:
    global DB_FILE, POLICY_DIR
    if len(sys.argv) > 1:
        DB_FILE = sys.argv[1]
    if len(sys.argv) > 2:
        POLICY_DIR = sys.argv[2]
    # Ground truth is created on first start (writable connection, outside the agent's reach).
    bootstrap_data.create_database(DB_FILE)
    bootstrap_data.write_policies(POLICY_DIR)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
