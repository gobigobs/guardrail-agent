"""
Guardrails — enforced in code, never in the prompt.

classify(action) is the single choke point every proposed tool call passes
through BEFORE anything executes. It returns one of:
  ("allow", reason)            — run it now
  ("needs_approval", reason)  — pause the graph, ask a human, run only on approve
  ("deny", reason)             — refuse; record the refusal in the audit log

Rules:
  1. Unknown tool            -> deny (allowlist, not denylist)
  2. DB write / non-SELECT   -> deny (read-only policy, parameter-level check)
  3. Consequential tier      -> needs_approval (send_email, create_event)
  4. Read / reversible tier  -> allow
"""

import re

from . import config

# A query is a read only if it is exactly ONE statement starting with SELECT
# (or WITH ... SELECT). Anything else — INSERT/UPDATE/DELETE/DROP/PRAGMA,
# stacked statements, comments smuggling a second statement — is refused.
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|attach|detach"
    r"|pragma|vacuum|reindex|grant|revoke)\b",
    re.IGNORECASE,
)


def _is_read_only_sql(sql: str) -> tuple[bool, str]:
    if not isinstance(sql, str) or not sql.strip():
        return False, "empty SQL"
    text = sql.strip()
    # Strip a trailing semicolon; more than one statement is not allowed.
    if text.count(";") > 1 or (text.count(";") == 1 and not text.endswith(";")):
        return False, "only a single statement is allowed"
    text = text.rstrip(";").strip()
    if _SQL_FORBIDDEN.search(text):
        return False, "write/DDL keywords are not allowed — the SAMPLE database is read-only"
    if not re.match(r"^(select|with\b)", text, re.IGNORECASE):
        return False, "only SELECT queries are allowed — the SAMPLE database is read-only"
    return True, "read-only SELECT"


def classify(action: dict) -> tuple[str, str]:
    """Classify a proposed tool call. `action` = {"tool": ..., "args": {...}}."""
    tool = (action or {}).get("tool")
    args = (action or {}).get("args") or {}

    spec = config.TOOLS.get(tool)
    if spec is None:
        return "deny", f"unknown tool '{tool}' — not on the allowlist"

    if tool == "query_db":
        ok, reason = _is_read_only_sql(str(args.get("sql", "")))
        if not ok:
            return "deny", reason

    tier = spec["tier"]
    if tier in config.SENSITIVE_TIERS:
        return (
            "needs_approval",
            f"'{tool}' is a consequential action ({tier}) — human approval required before it runs",
        )
    return "allow", f"'{tool}' is a {tier} action — allowed and logged"
