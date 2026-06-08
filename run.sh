#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -e .
fi

HOST="${HOST:-127.0.0.1}" PORT="${PORT:-8765}" ./.venv/bin/agent-usage-manager "$@"
