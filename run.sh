#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8765}"
echo "agent-usage-manager → http://${HOST}:${PORT}"
exec ./.venv/bin/uvicorn app:app --host "$HOST" --port "$PORT"
