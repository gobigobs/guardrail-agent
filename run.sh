#!/usr/bin/env bash
# guardrail-agent — start Ollama (if needed) + the FastAPI server.
# Local only. No paid keys. Model must already be in ~/.ollama/models
# (qwen2.5:3b-instruct); this script never pulls a model.
set -e
cd "$(dirname "$0")"

# This VM's proxy env breaks httpx inside the ollama client (bracketed IPv6
# entries in no_proxy). Scrub them for this process tree.
export no_proxy=localhost,127.0.0.1
export NO_PROXY=localhost,127.0.0.1

OLLAMA_BIN="${OLLAMA_BIN:-$HOME/.local/ollama-dist/bin/ollama}"
if ! curl -sf -m 3 http://localhost:11434/api/version >/dev/null 2>&1; then
  if [ -x "$OLLAMA_BIN" ]; then
    echo "Starting Ollama ($OLLAMA_BIN) ..."
    nohup "$OLLAMA_BIN" serve > ollama.log 2>&1 &
    for _ in $(seq 1 30); do
      curl -sf -m 2 http://localhost:11434/api/version >/dev/null 2>&1 && break
      sleep 1
    done
  else
    echo "WARNING: Ollama not running and no binary at $OLLAMA_BIN"
    echo "The server will still start; the planner falls back to rules and"
    echo "the audit log will show planner:fallback. See README for setup."
  fi
fi
curl -s -m 5 http://localhost:11434/api/tags | head -c 300; echo

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Starting guardrail-agent on http://localhost:${PORT:-8000} ..."
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
