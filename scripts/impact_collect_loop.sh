#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMPACT_DIR="$ROOT_DIR/modules/impact_console"
PY_BIN="$IMPACT_DIR/.venv/bin/python"
INTERVAL_SECONDS="${IMPACT_COLLECT_INTERVAL_SECONDS:-300}"
LOCK_FILE="${IMPACT_COLLECT_LOCK_FILE:-/tmp/impact_collect_loop.lock}"

if [[ ! -x "$PY_BIN" ]]; then
  PY_BIN="python3"
fi

if [[ "${1:-}" != "--no-lock" ]] && command -v flock >/dev/null 2>&1; then
  exec flock -n "$LOCK_FILE" "$0" --no-lock
fi

cd "$IMPACT_DIR"
while true; do
  "$PY_BIN" -m btc_news_arrow.cli collect || true
  sleep "$INTERVAL_SECONDS"
done
