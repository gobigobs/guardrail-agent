"""
Central configuration for guardrail-agent.

Design principle (from the guardrail literature — OWASP LLM Top 10 "Excessive
Agency", Anthropic / LangGraph human-in-the-loop guidance, NIST AI 600-1):
guardrails live in CODE, not in the prompt. The LLM never decides what is
allowed; this module does. The prompt only decides *what to try*.
"""

import os

# --- LLM -----------------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct")
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "180"))
LLM_TEMPERATURE = 0.0  # planning must be deterministic, not creative
# Hard wall-clock bound on one planning call, enforced by guardrail-agent
# itself (see planner._invoke_llm). ChatOllama in the installed
# langchain-ollama version silently drops a `timeout=` kwarg, so relying on
# it would let one looping 3B generation hang a run forever.
LLM_HARD_TIMEOUT_SECONDS = float(os.environ.get("LLM_HARD_TIMEOUT_SECONDS", "150"))

# --- Agent limits (guardrail layer: rate / scope limits per session) ------
MAX_ACTIONS_PER_RUN = 6   # hard cap: a run can never do more than this
MAX_RUNS_PER_MINUTE = 20  # trivial abuse brake for the demo server

# --- Tool registry --------------------------------------------------------
# Every tool the agent can call, with its risk tier and control.
# Tiers follow the standard production pattern:
#   read            -> agent acts alone, logged
#   reversible      -> agent acts alone, logged (easy to undo / harmless)
#   consequential   -> human approval BEFORE execution, never after
#   denied          -> the tool is not offered for that operation at all
#
# NOTE: `query_db` is a single tool on purpose, but the *policy* below makes
# it read-only: any non-SELECT statement is refused in code (parameter-level
# constraint, not just tool-level allowlisting).

TOOLS = {
    "list_events": {
        "tier": "read",
        "description": "List MOCK calendar events for a day.",
        "args": {"day": "today | tomorrow | YYYY-MM-DD"},
    },
    "create_event": {
        "tier": "consequential",
        "description": "Create a MOCK calendar event. Needs human approval.",
        "args": {"title": "event title", "time": "e.g. 2026-11-18 10:00"},
    },
    "draft_email": {
        "tier": "reversible",
        "description": "Draft an email into the MOCK outbox (not sent).",
        "args": {"to": "recipient", "subject": "subject", "body": "body"},
    },
    "send_email": {
        "tier": "consequential",
        "description": "Send an email from the MOCK outbox. Needs human approval.",
        "args": {"to": "recipient", "subject": "subject", "body": "body"},
    },
    "query_db": {
        "tier": "read",
        "description": "Run a read-only SELECT against the SAMPLE SQLite DB.",
        "args": {"sql": "a single SELECT statement"},
    },
}

# Which tiers need approval, and which are refused outright.
SENSITIVE_TIERS = {"consequential"}

# Policy: the sample database is READ-ONLY. Writes are denied by policy,
# not by asking the model nicely. Any SQL that is not a single SELECT is
# refused with a reason (see guardrails.classify).
DB_READ_ONLY = True

# --- Paths ----------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIT_DB_PATH = os.path.join(DATA_DIR, "audit.db")
SAMPLE_DB_PATH = os.path.join(DATA_DIR, "sample.db")
