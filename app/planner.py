"""
Planner — the agent's "brain".

LLM-first: local Ollama (qwen2.5:3b-instruct) via LangChain's ChatOllama,
asked for a strict JSON plan of tool calls. The model only *proposes*
actions; guardrails.py (code, not the prompt) decides what may run.

Deterministic fallback: if Ollama is down, slow, or returns unparseable
JSON, a rule-based planner handles the demo intents. The audit log records
which planner decided each run ("llm" vs "fallback"), so a fallback is
visible, never silent.

VM quirk (documented in the workspace AGENTS.md): this VM's default
no_proxy contains bracketed IPv6 entries (`[::1]`) that make httpx raise
`InvalidURL` at import time inside the ollama client. We scrub those
entries here, before langchain_ollama is imported.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, timedelta

from . import audit, config


def _scrub_proxy_env() -> None:
    for key in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(key, "")
        if "[" in val:
            kept = [p for p in val.split(",") if "[" not in p]
            os.environ[key] = ",".join(kept)


_scrub_proxy_env()

try:
    from langchain_ollama import ChatOllama

    _LLM_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment dependent
    ChatOllama = None
    _LLM_IMPORT_ERROR = exc

SYSTEM_PROMPT = """You are the planner of a guarded AI agent. You NEVER act directly;
you output a JSON plan of tool calls. The system (not you) enforces safety:
sending email and creating events need human approval, and the database is
read-only — so never claim you already sent something or changed data.

TOOLS (use exactly these names):
- list_events: {"day": "today" | "tomorrow" | "YYYY-MM-DD"} — read the MOCK calendar
- create_event: {"title": "...", "time": "YYYY-MM-DD HH:MM"} — MOCK calendar (needs approval)
- draft_email: {"to": "...", "subject": "...", "body": "..."} — draft only, MOCK outbox
- send_email: {"to": "...", "subject": "...", "body": "..."} — MOCK send (needs approval)
- query_db: {"sql": "SELECT ..."} — read-only SELECT on the SAMPLE database (customers, orders).
  Only SELECT. Never INSERT/UPDATE/DELETE/DROP — those are refused.

RULES:
- Output ONLY a JSON object, no other text:
  {"actions": [{"tool": "<name>", "args": {...}, "reason": "<one line>"}], "answer_hint": "<what the final answer should say>"}
- "actions" may be empty [] if the user only asks a question with no tool needed.
- For "send an email to X about Y": plan draft_email THEN send_email with the same content.
  ALWAYS plan BOTH — never draft-only when the user said "send".
- For "what is on my calendar tomorrow / today": one list_events action.
- For "how many customers / list orders / show customers": one query_db SELECT.
- For "delete / update / insert ... in the database": still plan a query_db action with
  the SQL the user asked for — the guardrail will refuse it and explain why. Do NOT
  silently rewrite it into a SELECT.
- Dates: today is {today}, tomorrow is {tomorrow}. Use ISO dates in args.
- Keep bodies short (2-3 sentences). Never invent email addresses: if the user
  gives no address, use the name as given (e.g. "priya@example.com" only if the
  user said Priya and no address — prefer the literal name they used).
"""

MAX_PLAN_ACTIONS = 6


def _validate_plan(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    actions = raw.get("actions")
    if not isinstance(actions, list):
        return None
    clean = []
    for item in actions[:MAX_PLAN_ACTIONS]:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        args = item.get("args") or {}
        if tool not in config.TOOLS or not isinstance(args, dict):
            continue
        clean.append(
            {
                "tool": tool,
                "args": {str(k): v for k, v in args.items()},
                "reason": str(item.get("reason", ""))[:200],
            }
        )
    hint = raw.get("answer_hint")
    return {"actions": clean, "answer_hint": hint if isinstance(hint, str) else ""}


# --- Hard timeout around the LLM call -------------------------------------
# The installed langchain-ollama silently drops a `timeout=` kwarg, so a
# looping 3B generation could otherwise hang a run forever (observed: one
# planning call ran 8+ minutes). We enforce the bound ourselves: one worker
# thread, result(timeout=...). On timeout the run falls back to the
# deterministic planner; if a previous call is still running in the worker,
# we go straight to fallback instead of piling up generations.
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-plan")
_LLM_PENDING = None


def _invoke_llm(llm, messages):
    global _LLM_PENDING
    if _LLM_PENDING is not None and not _LLM_PENDING.done():
        raise RuntimeError("previous LLM call still running — using fallback")
    fut = _LLM_EXECUTOR.submit(llm.invoke, messages)
    _LLM_PENDING = fut
    try:
        return fut.result(timeout=config.LLM_HARD_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        raise TimeoutError(
            f"LLM planning exceeded {config.LLM_HARD_TIMEOUT_SECONDS}s — using fallback"
        )


def plan_with_llm(run_id: str, user_message: str) -> tuple[dict, str, str]:
    """Returns (plan, planner_name, raw_llm_text). Raises on any LLM failure."""
    if ChatOllama is None:
        raise RuntimeError(f"ChatOllama unavailable: {_LLM_IMPORT_ERROR}")
    today = date.today()
    tomorrow = today + timedelta(days=1)
    llm = ChatOllama(
        model=config.OLLAMA_MODEL,
        base_url=config.OLLAMA_HOST,
        format="json",
        temperature=config.LLM_TEMPERATURE,
        # NOTE: the installed langchain-ollama silently drops a `timeout=`
        # kwarg, so the hard bound is enforced by _invoke_llm() below.
        # Bound the damage when the small local model loops: a 3B planner can
        # fall into token repetition in JSON mode; without a cap Ollama burns
        # minutes before aborting. 400 tokens is plenty for a short plan.
        num_predict=400,
        repeat_penalty=1.1,
    )
    # NOTE: plain .replace, NOT str.format — the prompt is full of JSON braces
    # ({...}) that .format() would try to substitute (KeyError: 'day').
    prompt = SYSTEM_PROMPT.replace("{today}", today.isoformat()).replace(
        "{tomorrow}", tomorrow.isoformat()
    )
    response = _invoke_llm(llm, [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_message},
    ])
    raw = response.content if isinstance(response.content, str) else str(response.content)
    audit.log(run_id, "llm_call", "planner:llm", {
        "model": config.OLLAMA_MODEL,
        "prompt_chars": len(prompt) + len(user_message),
        "response_chars": len(raw),
        "response_preview": raw[:500],
    })
    plan = None
    try:
        plan = _validate_plan(json.loads(raw))
    except (ValueError, TypeError):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                plan = _validate_plan(json.loads(match.group(0)))
            except (ValueError, TypeError):
                plan = None
    if plan is None:
        raise ValueError(f"LLM did not return a valid JSON plan: {raw[:300]!r}")
    return plan, "llm", raw


# ---------------------------------------------------------------------------
# Deterministic fallback planner (rule-based; used only if the LLM fails)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(
    r"(?:send|mail|email)\b.{0,80}?\bto\s+([A-Za-z][\w.+-]*)", re.IGNORECASE
)
_SUBJECT_RE = re.compile(r"about\s+(.+?)(?:\.|$)", re.IGNORECASE)


def _guess_email(name: str) -> str:
    name = (name or "").strip()
    if "@" in name:
        return name
    return f"{re.sub(r'[^a-z0-9]', '', name.lower())}@example.com"


def fallback_plan(user_message: str) -> dict:
    msg = user_message or ""
    low = msg.lower()
    today = date.today()
    tomorrow = today + timedelta(days=1)
    actions: list[dict] = []
    hint = ""

    # --- database intents (including write attempts: guardrail refuses) ---
    if re.search(r"\b(delete|update|insert|drop)\b", low) and re.search(
        r"\b(customer|order|database|db|record|row|table)\b", low
    ):
        # Reconstruct the SQL the user asked for, so the guardrail can refuse it.
        m = re.search(r"(delete|update|insert|drop)\b.{0,120}", msg, re.IGNORECASE)
        sql = (m.group(0).strip() if m else msg.strip())
        actions.append({"tool": "query_db", "args": {"sql": sql},
                        "reason": "User asked for a database write; guardrail will refuse it."})
        hint = "Database writes are not allowed; I will explain why."
    elif re.search(r"\bhow many customers\b", low):
        actions.append({"tool": "query_db",
                        "args": {"sql": "SELECT COUNT(*) AS customer_count FROM customers"},
                        "reason": "Count customers in the SAMPLE database."})
        hint = "Count the customers."
    elif re.search(r"\border", low) and re.search(r"\b(list|show|all|total|sales)\b", low):
        actions.append({"tool": "query_db",
                        "args": {"sql": "SELECT id, customer_id, item, amount_inr, status FROM orders ORDER BY id"},
                        "reason": "List orders from the SAMPLE database."})
        hint = "List the orders."
    elif re.search(r"\bcustomer", low):
        actions.append({"tool": "query_db",
                        "args": {"sql": "SELECT id, name, city, email FROM customers ORDER BY id"},
                        "reason": "List customers from the SAMPLE database."})
        hint = "List the customers."

    # --- email intents ---
    elif re.search(r"\b(send|mail|email)\b", low):
        m = _EMAIL_RE.search(msg)
        who = m.group(1) if m else "team"
        sm = _SUBJECT_RE.search(msg)
        topic = sm.group(1).strip() if sm else "update"
        subject = topic[:80]
        body = f"Hi {who},\n\n{topic}.\n\nThanks,\nGopi"
        actions.append({"tool": "draft_email",
                        "args": {"to": _guess_email(who), "subject": subject, "body": body},
                        "reason": "Draft the email first (reversible)."})
        actions.append({"tool": "send_email",
                        "args": {"to": _guess_email(who), "subject": subject, "body": body},
                        "reason": "Send it — needs human approval."})
        hint = "Draft and send the email after your approval."

    # --- calendar intents ---
    elif re.search(r"\bcalendar\b", low) or re.search(r"\bwhat'?s on\b", low):
        day = "tomorrow" if "tomorrow" in low else "today"
        actions.append({"tool": "list_events", "args": {"day": day},
                        "reason": f"List MOCK calendar events for {day}."})
        hint = f"Show what's on the calendar {day}."
    elif re.search(r"\b(create|schedule|book|add)\b.{0,40}\b(event|meeting|appointment)\b", low) or \
            re.search(r"\bschedule\b", low):
        title_m = re.search(r"(?:create|schedule|book|add)\s+(?:an?\s+|a\s+)?(.+?)(?:\s+on\s+|\s+at\s+|\s+tomorrow|\s+today|$)",
                            msg, re.IGNORECASE)
        title = title_m.group(1).strip() if title_m else "New event"
        when = tomorrow.isoformat() + " 10:00"
        tm = re.search(r"(\d{1,2}:\d{2})", msg)
        if tm:
            day = tomorrow if "tomorrow" in low else today
            when = f"{day.isoformat()} {tm.group(1)}"
        actions.append({"tool": "create_event", "args": {"title": title, "time": when},
                        "reason": "Create the MOCK calendar event — needs human approval."})
        hint = "Create the event after your approval."

    return {"actions": actions, "answer_hint": hint}


def _complete_send_intent(plan: dict, user_message: str) -> tuple[dict, bool]:
    """Deterministic intent completion (not model-dependent).

    A 3B planner is inconsistent: for "send an email to X" it sometimes plans
    only draft_email and stops, so the run never reaches the approval gate the
    user asked for. If the user asked to SEND and the plan has a draft but no
    send, append the matching send_email. This only *proposes* the action —
    guardrails.classify() still routes it through human approval, so safety
    is unchanged; the plan now faithfully covers the requested action.
    """
    if not re.search(r"\bsend\b.{0,60}\b(e-?mail|mail)\b", user_message or "", re.IGNORECASE):
        return plan, False
    actions = plan.get("actions", [])
    if not any(a.get("tool") == "draft_email" for a in actions):
        return plan, False
    if any(a.get("tool") == "send_email" for a in actions):
        return plan, False
    draft = [a for a in actions if a.get("tool") == "draft_email"][-1]
    actions.append({
        "tool": "send_email",
        "args": dict(draft.get("args") or {}),
        "reason": "User asked to SEND this email — send step added (still needs human approval).",
    })
    return plan, True


def make_plan(run_id: str, user_message: str) -> tuple[dict, str]:
    """Plan with the LLM, falling back to rules. Returns (plan, planner_name)."""
    try:
        plan, planner, _raw = plan_with_llm(run_id, user_message)
        plan, completed = _complete_send_intent(plan, user_message)
        if completed:
            audit.log(run_id, "plan_created", "planner:intent-completion",
                      {"note": "send_email step appended to a draft-only plan "
                               "(user asked to send; approval still required)",
                       "actions": plan["actions"]})
        audit.log(run_id, "plan_created", f"planner:{planner}",
                  {"actions": plan["actions"], "answer_hint": plan["answer_hint"]})
        return plan, planner
    except Exception as exc:
        audit.log(run_id, "llm_call", "planner:fallback",
                  {"reason": f"LLM planner failed, using deterministic fallback: {exc}"})
        plan = fallback_plan(user_message)
        audit.log(run_id, "plan_created", "planner:fallback",
                  {"actions": plan["actions"], "answer_hint": plan["answer_hint"]})
        return plan, "fallback"
