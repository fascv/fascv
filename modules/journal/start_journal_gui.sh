#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

HOST="${JOURNAL_GUI_HOST:-127.0.0.1}"
PORT="${JOURNAL_GUI_PORT:-8120}"
JSON_PATH="${JOURNAL_GUI_JSON_PATH:-logs/journal_events.jsonl}"
DB_PATH="${JOURNAL_GUI_DB_PATH:-logs/journal.db}"
LINES="${JOURNAL_GUI_LINES:-400}"
MAX_BYTES="${JOURNAL_GUI_MAX_BYTES:-2000000}"

# Ensure repo-root is on PYTHONPATH so `trading/` imports work even when started from modules/journal.
export PYTHONPATH="${REPO_ROOT}:${SCRIPT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting Journal GUI on http://${HOST}:${PORT} (json=${JSON_PATH} db=${DB_PATH})"
exec python3 -m trading_journal.gui \
  --host "${HOST}" \
  --port "${PORT}" \
  --json-path "${JSON_PATH}" \
  --db-path "${DB_PATH}" \
  --lines "${LINES}" \
  --max-bytes "${MAX_BYTES}" \
  "$@"
