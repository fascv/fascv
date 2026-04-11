#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

HOST="${CORE_GUI_HOST:-127.0.0.1}"
PORT="${CORE_GUI_PORT:-8130}"
CONTROL_URL="${CORE_GUI_CONTROL_URL:-http://127.0.0.1:8100}"
TIMEOUT_SEC="${CORE_GUI_TIMEOUT_SEC:-2.0}"
JOURNAL_LINES="${CORE_GUI_JOURNAL_LINES:-800}"

# Ensure repo-root is on PYTHONPATH so `trading/` imports work even when started from modules/core.
export PYTHONPATH="${REPO_ROOT}:${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting Core GUI on http://${HOST}:${PORT} (control=${CONTROL_URL})"
exec python3 -m trading_core.gui \
  --host "${HOST}" \
  --port "${PORT}" \
  --control-url "${CONTROL_URL}" \
  --timeout-sec "${TIMEOUT_SEC}" \
  --journal-lines "${JOURNAL_LINES}" \
  "$@"

