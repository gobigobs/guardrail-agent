"""
guardrail-agent — FastAPI server.

Endpoints:
  GET  /                demo UI (chat + approvals + live audit log)
  GET  /health          server + Ollama reachability
  POST /chat            {message} -> starts a run; may return awaiting_approval
  POST /approve         {run_id, decision: approve|decline} -> resumes a paused run
  GET  /runs            run snapshots (status, steps, final answer)
  GET  /runs/{run_id}   one run
  GET  /audit           audit log (newest first; ?run_id=…&limit=…)
  GET  /outbox          MOCK email outbox
  GET  /calendar        MOCK calendar snapshot
  GET  /sample-db       SAMPLE database contents
  POST /reset           reseed demo data (MOCK calendar, outbox, SAMPLE db)

Everything here is local. No real email is ever sent; the "outbox" is a list
in memory. The database the agent queries is a labelled SAMPLE.
"""

import asyncio
import os
import threading
import time
import uuid
from collections import deque

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import audit, config, graph, tools

app = FastAPI(
    title="guardrail-agent",
    description="LangGraph agent with guardrails: human approval + audit log. "
                "All tools are MOCK/SAMPLE — nothing real is ever touched.",
    version="1.0.0",
)

_GRAPH_LOCK = threading.Lock()
_RUNS: dict[str, dict] = {}
_CHAT_TIMES: deque = deque(maxlen=100)  # timestamps of recent /chat calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot(run_id: str, state: dict) -> dict:
    """Turn LangGraph state (+ interrupt marker) into an API-friendly snapshot."""
    interrupts = state.get("__interrupt__") or []
    pending = None
    status = state.get("status", "running")
    if interrupts:
        pending = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
        status = "awaiting_approval"
    snap = {
        "run_id": run_id,
        "status": status,
        "planner": state.get("planner"),
        "steps": state.get("steps", []),
        "final_answer": state.get("final_answer"),
        "pending_approval": pending,
        "actions_planned": state.get("actions", []),
    }
    _RUNS[run_id] = snap
    return snap


def _ollama_ok() -> bool:
    try:
        resp = httpx.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pages / health
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "static", "index.html"), encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": config.OLLAMA_MODEL,
        "ollama_host": config.OLLAMA_HOST,
        "ollama_reachable": _ollama_ok(),
        "audit_events": audit.count(),
        "runs": len(_RUNS),
    }


# ---------------------------------------------------------------------------
# Chat + approval — the core loop
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class ApproveRequest(BaseModel):
    run_id: str
    decision: str = Field(..., pattern="^(approve|decline)$")


@app.post("/chat")
async def chat(req: ChatRequest):
    now = time.time()
    _CHAT_TIMES.append(now)
    recent = [t for t in _CHAT_TIMES if now - t < 60]
    if len(recent) > config.MAX_RUNS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="rate limit: too many runs per minute")

    run_id = uuid.uuid4().hex[:12]
    audit.log(run_id, "run_started", "user", {"message": req.message})
    initial = {
        "user_message": req.message,
        "run_id": run_id,
        "planner": "",
        "actions": [],
        "idx": 0,
        "steps": [],
        "status": "running",
        "final_answer": "",
    }
    with _GRAPH_LOCK:
        state = await asyncio.to_thread(graph.invoke_graph, run_id, initial)
    snap = _snapshot(run_id, state)
    if snap["pending_approval"] is not None:
        audit.log(run_id, "approval_requested", "guardrails",
                  {"pending": snap["pending_approval"]})
    return JSONResponse(snap)


@app.post("/approve")
async def approve(req: ApproveRequest):
    snap = _RUNS.get(req.run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id '{req.run_id}'")
    if snap["status"] != "awaiting_approval":
        raise HTTPException(
            status_code=409,
            detail=f"run {req.run_id} is '{snap['status']}', not awaiting approval",
        )
    with _GRAPH_LOCK:
        state = await asyncio.to_thread(graph.resume_graph, req.run_id, req.decision)
    snap = _snapshot(req.run_id, state)
    if snap["pending_approval"] is not None:
        audit.log(req.run_id, "approval_requested", "guardrails",
                  {"pending": snap["pending_approval"]})
    return JSONResponse(snap)


# ---------------------------------------------------------------------------
# Inspection endpoints (audit, runs, mock state)
# ---------------------------------------------------------------------------

@app.get("/runs")
def list_runs():
    return {"runs": list(_RUNS.values())}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    snap = _RUNS.get(run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
    return snap


@app.get("/audit")
def get_audit(run_id: str | None = None, limit: int = 200):
    return {"events": audit.list_events(run_id=run_id, limit=min(limit, 1000))}


@app.get("/outbox")
def get_outbox():
    return {"drafts": tools.get_drafts(), "sent_mock": tools.get_outbox()}


@app.get("/calendar")
def get_calendar():
    return {"mock_calendar": tools.get_calendar_snapshot()}


@app.get("/sample-db")
def get_sample_db():
    customers = tools.query_db("SELECT id, name, city, email FROM customers ORDER BY id")
    orders = tools.query_db("SELECT id, customer_id, item, amount_inr, status FROM orders ORDER BY id")
    return {"sample_database": True, "customers": customers, "orders": orders}


@app.post("/reset")
def reset_demo():
    tools.reset_demo()
    return {"status": "reset", "detail": "MOCK calendar, MOCK outbox and SAMPLE db reseeded"}
