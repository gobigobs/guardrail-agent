# guardrail-agent — the agent acts. You decide.

![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-StateGraph%20%2B%20interrupt%28%29-purple)
![Local only](https://img.shields.io/badge/Local%20only-Ollama%20%E2%80%A2%20no%20API%20keys-green)
![Verification: 32/32](https://img.shields.io/badge/Verification-32%2F32%20passed-brightgreen)

Most AI agent demos let a model call tools with no safety net — one hallucinated `send_email` and the damage is done. **guardrail-agent is the other way:** a real LangGraph agent running on a local Ollama model, where sending email or creating an event **pauses for human approval**, database writes are **refused by policy**, and **every step lands in an audit log**. Nothing real is ever touched — all tools are MOCK / SAMPLE, labelled in code, UI and docs.

![Demo — the approval gate, live](docs/screenshots/02-approval.png)

## See it work

Real captures of the running app (headless Chromium driving the live UI — not mockups).

| | |
|---|---|
| **Home — the demo is the hero** | **Approval requested — `send_email` paused** |
| ![Home](docs/screenshots/01-home.png) | ![Approval](docs/screenshots/02-approval.png) |
| **Approved — email in the MOCK outbox** | **Calendar answer from the agent** |
| ![Approved](docs/screenshots/03-approved.png) | ![Calendar](docs/screenshots/01b-calendar-answer.png) |

## Quick start — 30 seconds

```bash
cd guardrail-agent
./run.sh            # starts Ollama if needed, then the server on :8000
```

Open **http://127.0.0.1:8000** and try it:

| Try this | What happens |
|---|---|
| `What is on my calendar tomorrow?` | Safe task — completes immediately, lists the 3 events. |
| `Send an email to Priya about the project update` | Agent drafts it, then **pauses** for your approval. Approve → MOCK outbox. Decline → never sent, decline logged. |
| `Delete all customers from the database` | **Refused** with a reason. The 5 SAMPLE customers stay intact. |
| `How many customers are in the database?` | Read-only SELECT — answered: 5. |

Port taken? `PORT=8002 ./run.sh`. Model `qwen2.5:3b-instruct` must already be in Ollama (`ollama list`) — this repo never pulls a model and never needs an API key. Manual setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && uvicorn app.main:app --host 127.0.0.1 --port 8000`.

## How it works

```
POST /chat ──► plan ──► dispatch (guardrails.classify)
{message}      (LLM    ├─▶ act       safe → run now, logged
               planner)├─▶ approval  consequential → interrupt() → wait for human
                       ├─▶ refuse    denied → log + say why
                       └─▶ finalize  → answer
POST /approve ──► Command(resume=approve|decline) resumes the paused run
```

| Node | Does |
|---|---|
| `plan` | LLM (ChatOllama, JSON mode, temp 0) proposes a short tool-call plan. On any LLM failure, a deterministic rule-based planner takes over — and the audit log records which planner decided. |
| `dispatch` | Routes each planned action through `guardrails.classify()` (code, not prompt). |
| `act` | Runs a safe tool, logs the call + result. |
| `approval` | Calls LangGraph `interrupt()` with the proposed action. The checkpointer saves the run; it resumes only via `POST /approve`. The tool runs **after** the human says yes — never before. |
| `refuse` | Records the refusal + reason in the audit log and tells the user why. |
| `finalize` | Composes the final answer from the executed steps. |

**Limits:** max 6 actions per run, 20 runs/minute, per-call token cap (`num_predict=400`) so a looping 3B model can't burn minutes.

## Guardrails — all in code, never in the prompt

| Control | How it works here |
|---|---|
| **Risk tiers** | Every tool is tiered in `app/config.py`: `read` (act alone, logged) · `reversible` (draft email — act alone, logged) · `consequential` (send email, create event — human approval first) · `denied` (DB writes — refused). Follows the standard production pattern (OWASP LLM06 "Excessive Agency", NIST AI 600-1, Anthropic/LangGraph HITL guidance). |
| **Permissions / allowlist** | `guardrails.classify()` is the single choke point. Unknown tools are denied; the LLM can only *propose* from the allowlist. |
| **Parameter-level checks** | `query_db` accepts exactly one `SELECT` (or `WITH…SELECT`). INSERT/UPDATE/DELETE/DROP, stacked statements and DDL keywords are refused — and the DB connection itself is opened read-only (`mode=ro`) as defence in depth. |
| **Human approval** | LangGraph `interrupt()` + `Command(resume=…)` with an `InMemorySaver` checkpointer. The approver sees the pending action (tool + exact args), not a summary — polished summaries are exactly how operators get talked into approving harmful actions (OWASP ASI09). |
| **Audit log** | Append-only SQLite (`data/audit.db`): `run_started`, `llm_call`, `plan_created`, `tool_call`, `approval_requested`, `approval_decision`, `refusal`, `run_finished` — each with run id, timestamp, actor and JSON detail. `GET /audit` (filterable by `run_id`). |
| **Safe failure** | If the LLM is down, loops, or returns bad JSON, the deterministic fallback planner completes the run and the audit log says `planner:fallback`. A failure never becomes a silent action. |

## Tools — all MOCK / SAMPLE

| Tool | What it really does |
|---|---|
| `list_events(day)` | Reads an **in-memory MOCK calendar** seeded relative to today (standup, lunch, dentist tomorrow). |
| `create_event(title, time)` | Adds to the MOCK calendar — **only after approval**. |
| `draft_email(to, subject, body)` | Appends to the MOCK drafts list. Reversible, no approval needed. |
| `send_email(to, subject, body)` | Appends to the **MOCK outbox** — **only after approval**. No SMTP, no network, no real recipient. |
| `query_db(sql)` | Read-only `SELECT` on a **SAMPLE** SQLite DB (`customers`, `orders` — fictional data). Writes refused by policy. |

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Demo UI (chat, approval card, live audit log, outbox, calendar) |
| `GET /health` | Server + Ollama reachability, audit count |
| `POST /chat` | `{"message": "…"}` → `{run_id, status: done\|awaiting_approval, steps, final_answer, pending_approval}` |
| `POST /approve` | `{"run_id": "…", "decision": "approve"\|"decline"}` → resumed run snapshot |
| `GET /runs`, `GET /runs/{id}` | Run snapshots |
| `GET /audit?run_id=…&limit=…` | Audit log, newest first |
| `GET /outbox`, `GET /calendar`, `GET /sample-db` | Inspect MOCK/SAMPLE state |
| `POST /reset` | Reseed demo data |

## Verification — 32/32, twice in a row

Run it yourself: `python scripts/verify.py --base http://127.0.0.1:8002` (needs the server up; ~2.5 minutes — each LLM planning call takes 30–100 s on the CPU-bound 3B model).

**Final run, 2026-11-17 (server :8002, Ollama 0.40.16, model qwen2.5:3b-instruct, LLM planner on every run): `32 passed, 0 failed` — `ALL CHECKS PASSED`.** The second consecutive 32/32 pass, re-run after the hard-timeout fix below, so the shipped code is the verified code. Full log: `verify-final2.log`.

What it proves: safe calendar task completes and lists the 3 events · `send_email` pauses, outbox empty before approval, approve → email in MOCK outbox · second `send_email`, decline → not in outbox, audit shows the decline · "Delete all customers" refused, 5 customers intact · DB read answers 5 · audit log contains all 8 event types, filterable by `run_id`.

**Hard-won fixes baked in:** a `num_predict=400` cap + `repeat_penalty=1.1` stops a looping 3B model fast · a hard 150 s wall-clock bound on planner calls — the installed `langchain-ollama` silently drops a `timeout=` kwarg, so the bound is enforced here · a deterministic step appends the matching `send_email` when the user asked to send and the plan has only a draft — still gated by human approval.

> **VM quirk, documented:** this VM's default `no_proxy` contains bracketed IPv6 entries (`[::1]`) that make `httpx` raise `InvalidURL` when the Ollama client is imported. The app scrubs those entries at startup (`app/planner.py`) and `run.sh` exports a clean `no_proxy`.

## Who would pay for this (honest — no income promised)

- **Jobs:** "AI agent with guardrails" is exactly what companies hiring for agentic-AI roles ask to see — LangGraph, human-in-the-loop, tool design and auditability in one repo. Portfolio evidence, not a product.
- **Clients / freelancing:** the same pattern is the sellable core of real client work — a support-ticket, order-lookup or lead-qualification agent where the business wants the AI to *act* but never to refund, delete, or email a customer unsupervised. The demo (approval gate + audit log) is the pitch: "your team stays in control".
- **Product:** a hosted "guardrailed agent" starter (multi-tenant approvals + audit + evals) is a real direction, but this repo is a demo, not that product — no auth, no multi-user isolation, in-memory checkpointer. No revenue is promised or projected.

## Limitations (honest)

- The 3B local model is slow on CPU (~30–100 s per plan on this VM) and sometimes plans an extra harmless read or loops into repetition — the token cap + fallback planner exist for exactly that.
- The checkpointer is in-memory: restarting the server loses paused runs (the audit log survives — SQLite on disk). Production would use PostgresSaver + auth.
- Tools are MOCK/SAMPLE by design. Wiring real Gmail/Calendar/DB access is a deliberate later step, with per-user OAuth scopes as the permission layer.
- Single-user demo server: no auth, no TLS, localhost only.

## Knowledge used

Built from several sources, not one video: LangGraph docs — `interrupt()` + `Command(resume=…)` with a checkpointer as *the* human-in-the-loop primitive, and the gotcha that a resumed node re-runs from the top, so nothing before `interrupt()` may have side effects · guardrail best practice — risk tiers, least-privilege narrow tools, parameter-level constraints on top of allowlisting, guardrails in code not prompts, approval showing the *actual* change (OWASP ASI09), audit everything including refusals (OWASP LLM06 / ASI01/02/09, NIST AI 600-1) · local-LLM practice — small-model JSON-mode planning is unreliable: temperature 0, `format="json"`, short prompts, a token cap, and an *audited* deterministic fallback.
