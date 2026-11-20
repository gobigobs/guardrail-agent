"""
Agent graph — real LangGraph (StateGraph + checkpointer + interrupt).

Flow (the classic plan → act → review → approve shape, as a state machine):

    START → plan → dispatch ─┬─▶ act        (safe: run now)
                             ├─▶ approval   (consequential: interrupt(), wait
                             │                for a human, run only on approve)
                             ├─▶ refuse     (denied by policy: record + explain)
                             └─▶ finalize → END

  act / approval / refuse all loop back to dispatch until the plan is done.

Human-in-the-loop uses LangGraph's native `interrupt()` + `Command(resume=…)`:
the approval node pauses mid-run, the checkpointer (InMemorySaver) saves the
state under the run's thread_id, and the API resumes the exact same run with
the human's decision. Nothing before the interrupt() call has side effects
(the node re-runs from the top on resume), and the irreversible tool call
happens only AFTER interrupt() returns "approve".

The graph never decides safety itself — every dispatch goes through
guardrails.classify() (code, not the prompt).
"""

import operator
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import audit, config, guardrails, planner, tools


class AgentState(TypedDict):
    user_message: str
    run_id: str
    planner: str
    actions: list
    idx: int
    steps: Annotated[list, operator.add]
    status: str
    final_answer: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def plan_node(state: AgentState) -> dict:
    plan, planner_name = planner.make_plan(state["run_id"], state["user_message"])
    actions = plan["actions"][: config.MAX_ACTIONS_PER_RUN]
    if len(plan["actions"]) > config.MAX_ACTIONS_PER_RUN:
        audit.log(state["run_id"], "refusal", "guardrails",
                  {"reason": f"plan truncated to {config.MAX_ACTIONS_PER_RUN} actions "
                             f"(max actions per run)"})
    return {
        "planner": planner_name,
        "actions": actions,
        "idx": 0,
        "status": "running",
    }


def dispatch_node(state: AgentState) -> dict:
    return {}  # routing only; see route()


def route(state: AgentState) -> Literal["act", "approval", "refuse", "finalize"]:
    if state["idx"] >= len(state["actions"]):
        return "finalize"
    action = state["actions"][state["idx"]]
    decision, _reason = guardrails.classify(action)
    return {"allow": "act", "needs_approval": "approval", "deny": "refuse"}[decision]


def _step(action: dict, status: str, detail: str, result=None) -> dict:
    return {
        "tool": action.get("tool"),
        "args": action.get("args"),
        "reason": action.get("reason", ""),
        "status": status,  # done | approved+done | declined | refused | error
        "detail": detail,
        "result": result,
    }


def act_node(state: AgentState) -> dict:
    action = state["actions"][state["idx"]]
    _decision, reason = guardrails.classify(action)
    outcome = tools.execute_tool(action["tool"], action.get("args") or {})
    audit.log(state["run_id"], "tool_call", "agent", {
        "tool": action["tool"], "args": action.get("args"),
        "guardrail": reason, "ok": outcome.get("ok"),
        "result_preview": str(outcome.get("result", outcome.get("error")))[:400],
    })
    if outcome.get("ok"):
        step = _step(action, "done", f"Ran {action['tool']}.", outcome.get("result"))
    else:
        step = _step(action, "error", f"{action['tool']} failed: {outcome.get('error')}")
    return {"steps": [step], "idx": state["idx"] + 1}


def refuse_node(state: AgentState) -> dict:
    action = state["actions"][state["idx"]]
    _decision, reason = guardrails.classify(action)
    audit.log(state["run_id"], "refusal", "guardrails", {
        "tool": action["tool"], "args": action.get("args"), "reason": reason,
    })
    step = _step(action, "refused",
                 f"Refused by guardrail: {reason}")
    return {"steps": [step], "idx": state["idx"] + 1}


def approval_node(state: AgentState) -> dict:
    """Pause for a human. Code before interrupt() is pure (it re-runs on resume)."""
    action = state["actions"][state["idx"]]
    _decision, reason = guardrails.classify(action)
    payload = {
        "tool": action["tool"],
        "args": action.get("args") or {},
        "reason": reason,
        "proposed_by": action.get("reason", ""),
    }
    # --- PAUSE HERE until POST /approve resumes this run -------------------
    decision = interrupt(payload)
    # --- RESUMED: decision is "approve" or "decline" ----------------------
    audit.log(state["run_id"], "approval_decision", "human", {
        "tool": action["tool"], "args": action.get("args"), "decision": decision,
    })
    if str(decision).lower() == "approve":
        outcome = tools.execute_tool(action["tool"], action.get("args") or {})
        audit.log(state["run_id"], "tool_call", "agent", {
            "tool": action["tool"], "args": action.get("args"),
            "guardrail": "approved by human", "ok": outcome.get("ok"),
            "result_preview": str(outcome.get("result", outcome.get("error")))[:400],
        })
        if outcome.get("ok"):
            step = _step(action, "approved+done",
                         f"Approved by you, then ran {action['tool']}.",
                         outcome.get("result"))
        else:
            step = _step(action, "error",
                         f"Approved by you, but {action['tool']} failed: {outcome.get('error')}")
    else:
        step = _step(action, "declined",
                     f"You declined {action['tool']} — it was NOT run.")
    return {"steps": [step], "idx": state["idx"] + 1}


def _describe_result(tool: str, result) -> str:
    """One-line human summary of a tool result for the final answer."""
    if not isinstance(result, dict):
        return str(result)
    if tool == "list_events":
        events = result.get("events", [])
        if not events:
            return f"No events on {result.get('day')}."
        lines = [f"{e['time']} — {e['title']}" for e in events]
        return f"On {result.get('day')}: " + "; ".join(lines) + "."
    if tool == "create_event":
        return f"Created '{result.get('title')}' on {result.get('day')} at {result.get('time')}."
    if tool == "draft_email":
        return f"Drafted an email to {result.get('to')} — '{result.get('subject')}' (MOCK, not sent)."
    if tool == "send_email":
        return f"Sent (MOCK outbox) to {result.get('to')} — '{result.get('subject')}'."
    if tool == "query_db":
        rows = result.get("rows", [])
        cols = result.get("columns", [])
        if not rows:
            return "Query returned no rows."
        if len(cols) == 1 and len(rows) == 1 and len(rows[0]) == 1:
            return f"Result: {cols[0]} = {rows[0][0]}."
        lines = [" | ".join(str(c) for c in row) for row in rows[:10]]
        return "Rows (" + ", ".join(cols) + "): " + "; ".join(lines) + ("" if len(rows) <= 10 else f" …and {len(rows) - 10} more.")
    return str(result)


def finalize_node(state: AgentState) -> dict:
    parts: list[str] = []
    for step in state.get("steps", []):
        if step["status"] in ("done", "approved+done") and step.get("result") is not None:
            parts.append(_describe_result(step["tool"], step["result"]))
        elif step["status"] == "declined":
            parts.append(f"Declined: {step['tool']} was not run.")
        elif step["status"] == "refused":
            parts.append(step["detail"])
        elif step["status"] == "error":
            parts.append(step["detail"])
    if not parts:
        hint = ""
        for action in state.get("actions", []):
            pass
        parts.append(
            "I couldn't do anything with that. I can: check your MOCK calendar "
            "(today/tomorrow), create a MOCK event (needs your approval), draft or "
            "send a MOCK email (sending needs your approval), or run a read-only "
            "SELECT on the SAMPLE database. Database writes are always refused."
        )
    final = " ".join(parts)
    audit.log(state["run_id"], "run_finished", "agent",
              {"status": "done", "steps": len(state.get("steps", [])),
               "final_answer": final[:500]})
    return {"final_answer": final, "status": "done"}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("dispatch", dispatch_node)
    builder.add_node("act", act_node)
    builder.add_node("approval", approval_node)
    builder.add_node("refuse", refuse_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "dispatch")
    builder.add_conditional_edges(
        "dispatch", route,
        {"act": "act", "approval": "approval", "refuse": "refuse", "finalize": "finalize"},
    )
    builder.add_edge("act", "dispatch")
    builder.add_edge("approval", "dispatch")
    builder.add_edge("refuse", "dispatch")
    builder.add_edge("finalize", END)

    # Checkpointer = what makes pause/resume possible. Without it, interrupt()
    # cannot save the paused run.
    return builder.compile(checkpointer=InMemorySaver())


GRAPH = build_graph()


def run_config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def invoke_graph(run_id: str, payload):
    """Run (or resume) the graph for one run. Runs synchronously; callers
    should run this in a thread so the API stays responsive."""
    return GRAPH.invoke(payload, run_config(run_id))


def resume_graph(run_id: str, decision: str):
    return GRAPH.invoke(Command(resume=decision), run_config(run_id))
