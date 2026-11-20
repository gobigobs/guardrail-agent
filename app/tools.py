"""
MOCK tools — safe local stand-ins, clearly labelled everywhere.

Nothing in this module sends a real email, touches a real calendar, or writes
to a real database:
  * Calendar — in-memory dict, seeded with sample events relative to today.
  * Email    — a MOCK outbox list. "Sending" appends to the list and the
               audit log; no SMTP, no network, no real recipient.
  * Database — a small SAMPLE SQLite DB (customers / orders). Reads only;
               the guardrail in guardrails.py blocks every write before this
               code is even reached, and this module opens SQLite read-only
               as a second layer of defence.

Tool functions return plain dicts: {"ok": bool, "result": ..., "error": ...}.
"""

import os
import sqlite3
from datetime import date, timedelta

from . import config

# --------------------------------------------------------------------------
# MOCK calendar (in-memory)
# --------------------------------------------------------------------------

_calendar: dict[str, list[dict]] = {}


def _seed_calendar() -> None:
    """Seed sample events relative to today so the demo always has content."""
    if _calendar:
        return
    today = date.today()
    tomorrow = today + timedelta(days=1)
    _calendar[today.isoformat()] = [
        {"time": "07:00", "title": "Morning walk"},
        {"time": "15:00", "title": "Project review with team"},
    ]
    _calendar[tomorrow.isoformat()] = [
        {"time": "10:00", "title": "Team standup"},
        {"time": "13:00", "title": "Lunch with Priya"},
        {"time": "17:30", "title": "Dentist appointment"},
    ]


def _resolve_day(day: str) -> str:
    day = (day or "").strip().lower()
    if day in ("today", ""):
        return date.today().isoformat()
    if day == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    if day == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    return day  # assume YYYY-MM-DD; validated by date parse in the caller


def list_events(day: str = "tomorrow") -> dict:
    _seed_calendar()
    key = _resolve_day(day)
    try:
        date.fromisoformat(key)
    except ValueError:
        return {"ok": False, "error": f"'{day}' is not a valid day (use today/tomorrow/YYYY-MM-DD)"}
    events = _calendar.get(key, [])
    return {"ok": True, "result": {"day": key, "events": events, "count": len(events)}}


def create_event(title: str, time: str) -> dict:
    """MOCK create — only ever reached AFTER human approval."""
    _seed_calendar()
    title = (title or "").strip()
    time = (time or "").strip()
    if not title:
        return {"ok": False, "error": "event title is required"}
    # Accept "YYYY-MM-DD HH:MM" or "HH:MM" (assumed tomorrow) or "HH:MM today".
    day_key, clock = None, None
    parts = time.split()
    if len(parts) == 2 and len(parts[0]) == 10:
        day_key, clock = parts[0], parts[1]
    elif len(parts) == 2 and parts[1] in ("today", "tomorrow"):
        day_key, clock = _resolve_day(parts[1]), parts[0]
    elif len(parts) == 1:
        day_key, clock = _resolve_day("tomorrow"), parts[0]
    if day_key is None:
        return {"ok": False, "error": f"could not parse time '{time}' (use 'YYYY-MM-DD HH:MM' or 'HH:MM')"}
    try:
        date.fromisoformat(day_key)
        hh, mm = clock.split(":")
        assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
    except (ValueError, AssertionError):
        return {"ok": False, "error": f"could not parse time '{time}'"}
    _calendar.setdefault(day_key, []).append({"time": clock, "title": title})
    _calendar[day_key].sort(key=lambda e: e["time"])
    return {"ok": True, "result": {"day": day_key, "time": clock, "title": title}}


def get_calendar_snapshot() -> dict:
    _seed_calendar()
    return dict(_calendar)


# --------------------------------------------------------------------------
# MOCK email outbox (in-memory — never a real server)
# --------------------------------------------------------------------------

_outbox: list[dict] = []
_drafts: list[dict] = []


def draft_email(to: str, subject: str, body: str) -> dict:
    if not (to or "").strip() or not (subject or "").strip():
        return {"ok": False, "error": "draft_email needs 'to' and 'subject'"}
    draft = {"to": to.strip(), "subject": subject.strip(), "body": (body or "").strip(),
             "status": "draft (MOCK — not sent)"}
    _drafts.append(draft)
    return {"ok": True, "result": draft}


def send_email(to: str, subject: str, body: str) -> dict:
    """MOCK send — only ever reached AFTER human approval. Appends to outbox."""
    if not (to or "").strip() or not (subject or "").strip():
        return {"ok": False, "error": "send_email needs 'to' and 'subject'"}
    sent = {"to": to.strip(), "subject": subject.strip(), "body": (body or "").strip(),
            "status": "sent (MOCK outbox — no real email was sent)"}
    _outbox.append(sent)
    return {"ok": True, "result": sent}


def get_outbox() -> list[dict]:
    return list(_outbox)


def get_drafts() -> list[dict]:
    return list(_drafts)


def reset_demo() -> None:
    """Reseed all MOCK/SAMPLE demo state (calendar, outbox, drafts, SAMPLE db)."""
    _calendar.clear()
    _outbox.clear()
    _drafts.clear()
    _seed_calendar()
    init_sample_db()


# --------------------------------------------------------------------------
# SAMPLE database (SQLite, read-only access from the agent)
# --------------------------------------------------------------------------

_SAMPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    amount_inr INTEGER NOT NULL,
    status TEXT NOT NULL
);
"""

_SAMPLE_CUSTOMERS = [
    (1, "Asha Menon", "Salem", "asha@example.com"),
    (2, "Ravi Kumar", "Chennai", "ravi@example.com"),
    (3, "Meera Nair", "Coimbatore", "meera@example.com"),
    (4, "Arjun Rao", "Salem", "arjun@example.com"),
    (5, "Divya Iyer", "Bengaluru", "divya@example.com"),
]

_SAMPLE_ORDERS = [
    (101, 1, "Chicken bucket", 499, "delivered"),
    (102, 1, "Veg combo", 299, "delivered"),
    (103, 2, "Family feast", 999, "preparing"),
    (104, 3, "Chicken bucket", 499, "delivered"),
    (105, 4, "Waffle + falooda", 349, "cancelled"),
    (106, 5, "Veg combo", 299, "delivered"),
]


def init_sample_db() -> None:
    """Create + seed the SAMPLE db (only run at server startup / reset)."""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(config.SAMPLE_DB_PATH)
    try:
        conn.executescript(_SAMPLE_SCHEMA)
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM customers")
        conn.executemany("INSERT INTO customers VALUES (?,?,?,?)", _SAMPLE_CUSTOMERS)
        conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", _SAMPLE_ORDERS)
        conn.commit()
    finally:
        conn.close()


def query_db(sql: str) -> dict:
    """Run a read-only SELECT. Opened read-only (mode=ro) as defence in depth;
    the guardrail already refused non-SELECT statements before this point."""
    if not os.path.exists(config.SAMPLE_DB_PATH):
        init_sample_db()
    try:
        conn = sqlite3.connect(f"file:{config.SAMPLE_DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"SAMPLE db unavailable: {exc}"}
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return {"ok": True, "result": {"columns": columns, "rows": rows, "count": len(rows)}}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"SAMPLE db error: {exc}"}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Dispatcher — the single place the graph executes a tool
# --------------------------------------------------------------------------

def execute_tool(tool: str, args: dict) -> dict:
    """Execute an already-classified, already-approved (if needed) tool call."""
    args = args or {}
    if tool == "list_events":
        return list_events(str(args.get("day", "tomorrow")))
    if tool == "create_event":
        return create_event(str(args.get("title", "")), str(args.get("time", "")))
    if tool == "draft_email":
        return draft_email(str(args.get("to", "")), str(args.get("subject", "")),
                           str(args.get("body", "")))
    if tool == "send_email":
        return send_email(str(args.get("to", "")), str(args.get("subject", "")),
                          str(args.get("body", "")))
    if tool == "query_db":
        return query_db(str(args.get("sql", "")))
    return {"ok": False, "error": f"unknown tool '{tool}'"}
