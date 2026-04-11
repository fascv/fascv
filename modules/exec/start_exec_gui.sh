#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST="${EXEC_GUI_HOST:-127.0.0.1}"
PORT="${EXEC_GUI_PORT:-8110}"
CONTROL_URL="${EXEC_GUI_CONTROL_URL:-http://127.0.0.1:8100}"
TIMEOUT_SEC="${EXEC_GUI_TIMEOUT_SEC:-2.0}"
JOURNAL_LINES="${EXEC_GUI_JOURNAL_LINES:-500}"

export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting Exec GUI on http://${HOST}:${PORT} (control=${CONTROL_URL})"
exec python3 -m trading_exec.gui \
  --host "${HOST}" \
  --port "${PORT}" \
  --control-url "${CONTROL_URL}" \
  --timeout-sec "${TIMEOUT_SEC}" \
  --journal-lines "${JOURNAL_LINES}" \
  "$@"
