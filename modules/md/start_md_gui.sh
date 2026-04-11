#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

HOST="${MD_GUI_HOST:-127.0.0.1}"
PORT="${MD_GUI_PORT:-8140}"
CONTROL_URL="${MD_GUI_CONTROL_URL:-http://127.0.0.1:8100}"
TIMEOUT_SEC="${MD_GUI_TIMEOUT_SEC:-2.0}"
JOURNAL_LINES="${MD_GUI_JOURNAL_LINES:-1200}"

export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting MD GUI on http://${HOST}:${PORT} (control=${CONTROL_URL})"
exec python3 -m trading_md.gui \
  --host "${HOST}" \
  --port "${PORT}" \
  --control-url "${CONTROL_URL}" \
  --timeout-sec "${TIMEOUT_SEC}" \
  --journal-lines "${JOURNAL_LINES}" \
  "$@"

