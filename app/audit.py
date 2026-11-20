"""
Audit log — the guardrail you can prove held.

Every LLM call, tool call, approval request, approval decision and refusal is
appended to a SQLite table with a run id, timestamp and JSON detail. Nothing
is ever deleted or updated by the app (append-only by convention; the table
has no UPDATE/DELETE path in code).

This is what makes the guardrails real instead of a claim: after any run you
can reconstruct exactly what the agent tried, what was allowed, what was
stopped, and who approved what.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()


def _connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.AUDIT_DB_PATH, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            detail TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts)")
    conn.commit()
    return conn


def log(run_id: str, event_type: str, actor: str, detail: dict) -> int:
    """Append one audit event. Returns the event id."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO audit_events (ts, run_id, event_type, actor, detail)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    run_id,
                    event_type,
                    actor,
                    json.dumps(detail, default=str),
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_events(run_id: str | None = None, limit: int = 500) -> list[dict]:
    """Newest-first audit events, optionally filtered to one run."""
    conn = _connect()
    try:
        if run_id:
            rows = conn.execute(
                "SELECT id, ts, run_id, event_type, actor, detail FROM audit_events"
                " WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, ts, run_id, event_type, actor, detail FROM audit_events"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        detail = row[5]
        try:
            detail = json.loads(detail)
        except (TypeError, ValueError):
            pass
        out.append(
            {
                "id": row[0],
                "ts": row[1],
                "run_id": row[2],
                "event_type": row[3],
                "actor": row[4],
                "detail": detail,
            }
        )
    return out


def count() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    finally:
        conn.close()
