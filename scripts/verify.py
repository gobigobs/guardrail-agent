#!/usr/bin/env python3
"""End-to-end verification for guardrail-agent (run against a live server).

Checks, in order:
  1. /health — server up, Ollama reachable
  2. SAFE task — "what is on my calendar tomorrow?" completes, lists events
  3. SENSITIVE approve — send_email pauses, approve -> appears in MOCK outbox
  4. SENSITIVE decline — second send_email run, decline -> NOT in outbox,
     audit log shows the decline
  5. BLOCKED action — DB write refused with a reason, nothing changed
  6. READ task — DB SELECT works
  7. /audit shows run_started / llm_call (or fallback) / tool_call /
     approval_requested / approval_decision / refusal for the runs above

Usage:  python scripts/verify.py [--base http://127.0.0.1:8000]
Exit code 0 = all checks pass.
"""

import argparse
import json
import sys

import httpx

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    client = httpx.Client(base_url=base, timeout=600)

    print("== guardrail-agent verification ==")
    print(f"   base: {base}")

    # 0. reset demo state so the run is deterministic
    r = client.post("/reset")
    check("reset demo state", r.status_code == 200, r.text[:100])

    # 1. health
    r = client.get("/health")
    health = r.json() if r.status_code == 200 else {}
    check("server up (/health)", r.status_code == 200, str(health.get("status")))
    ollama_ok = health.get("ollama_reachable") is True
    check("Ollama reachable (qwen2.5:3b-instruct)", ollama_ok,
          f"model={health.get('model')}")
    if not ollama_ok:
        print("   NOTE: planner will use the deterministic fallback; "
              "LLM-path checks below are skipped, not faked.")

    # 2. SAFE task: calendar tomorrow
    r = client.post("/chat", json={"message": "What is on my calendar tomorrow?"})
    run = r.json()
    check("safe task returns 200 + run id", r.status_code == 200 and run.get("run_id"))
    check("safe task completes (status done)", run.get("status") == "done",
          f"status={run.get('status')}")
    steps = run.get("steps", [])
    check("safe task ran list_events", any(s.get("tool") == "list_events" for s in steps),
          json.dumps(steps)[:200])
    check("safe task lists tomorrow's events",
          "Team standup" in (run.get("final_answer") or ""),
          (run.get("final_answer") or "")[:160])
    safe_run_id = run.get("run_id")

    # 3. SENSITIVE approve flow
    r = client.post("/chat", json={
        "message": "Send an email to Priya about the project update for tomorrow."})
    run = r.json()
    check("sensitive task pauses (awaiting_approval)", run.get("status") == "awaiting_approval",
          f"status={run.get('status')}")
    pending = run.get("pending_approval") or {}
    check("pending approval is send_email", pending.get("tool") == "send_email",
          json.dumps(pending)[:200])
    run_id = run.get("run_id")
    outbox_before = client.get("/outbox").json()["sent_mock"]
    check("email NOT sent before approval", len(outbox_before) == 0,
          f"outbox size={len(outbox_before)}")
    r = client.post("/approve", json={"run_id": run_id, "decision": "approve"})
    run2 = r.json()
    check("approve resumes to done", run2.get("status") == "done",
          f"status={run2.get('status')}")
    outbox = client.get("/outbox").json()["sent_mock"]
    check("approved email appears in MOCK outbox", len(outbox) == 1,
          json.dumps(outbox)[:200])
    check("approved email went to Priya",
          bool(outbox) and "priya" in outbox[0].get("to", "").lower(),
          json.dumps(outbox)[:200])

    # 4. SENSITIVE decline flow
    r = client.post("/chat", json={
        "message": "Send an email to Ravi about the discount offer."})
    run = r.json()
    check("second sensitive task pauses", run.get("status") == "awaiting_approval")
    run_id = run.get("run_id")
    r = client.post("/approve", json={"run_id": run_id, "decision": "decline"})
    run2 = r.json()
    check("decline resumes to done", run2.get("status") == "done")
    check("declined step recorded",
          any(s.get("status") == "declined" for s in run2.get("steps", [])),
          json.dumps(run2.get("steps"))[:200])
    outbox = client.get("/outbox").json()["sent_mock"]
    check("declined email NOT in outbox", len(outbox) == 1,
          f"outbox size={len(outbox)} (only the approved one)")

    # 5. BLOCKED action: DB write
    r = client.post("/chat", json={
        "message": "Delete all customers from the database."})
    run = r.json()
    check("db-write task completes (not stuck)", run.get("status") == "done",
          f"status={run.get('status')}")
    check("db write refused with a reason",
          any(s.get("status") == "refused" for s in run.get("steps", [])),
          json.dumps(run.get("steps"))[:300])
    customers = client.get("/sample-db").json()["customers"]
    check("customers still intact after refused delete",
          customers.get("result", {}).get("count") == 5,
          json.dumps(customers.get("result"))[:120])

    # 6. READ task: DB SELECT works
    r = client.post("/chat", json={"message": "How many customers are in the database?"})
    run = r.json()
    check("db read task completes", run.get("status") == "done")
    check("db read answers 5", "5" in (run.get("final_answer") or ""),
          (run.get("final_answer") or "")[:160])

    # 7. Audit log covers everything
    events = client.get("/audit?limit=500").json()["events"]
    types = {e["event_type"] for e in events}
    for needed in ("run_started", "plan_created", "tool_call",
                   "approval_requested", "approval_decision", "refusal", "run_finished"):
        check(f"audit has '{needed}'", needed in types)
    if ollama_ok:
        check("audit has 'llm_call' (LLM planner ran)",
              "llm_call" in types)
    else:
        print("  [SKIP] audit 'llm_call' — Ollama was down, fallback planner used")
    declined = [e for e in events
                if e["event_type"] == "approval_decision"
                and e["detail"].get("decision") == "decline"]
    check("audit shows the decline decision", len(declined) >= 1)
    safe_events = client.get(f"/audit?run_id={safe_run_id}").json()["events"]
    check("audit filterable by run_id", len(safe_events) >= 2,
          f"{len(safe_events)} events for safe run")

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
