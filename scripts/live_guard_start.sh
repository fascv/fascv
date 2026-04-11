#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="${PID_FILE:-logs/live_guard.pid}"
LOG_FILE="${GUARD_LOG:-logs/live_guard.log}"
DISABLE_FILE="${DISABLE_FILE:-logs/live_guard.disabled}"

mkdir -p logs
rm -f "$DISABLE_FILE"

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "live_guard already running (pid=$pid)"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if command -v setsid >/dev/null 2>&1; then
  # Detach into a new session to survive parent-shell/process cleanup.
  setsid env MODE="${MODE:-live}" CONFIG="${CONFIG:-configs/live_binance_eth_usdc.yaml}" \
    ./scripts/live_guard.sh >> "$LOG_FILE" 2>&1 < /dev/null &
else
  nohup env MODE="${MODE:-live}" CONFIG="${CONFIG:-configs/live_binance_eth_usdc.yaml}" \
    ./scripts/live_guard.sh >> "$LOG_FILE" 2>&1 < /dev/null &
fi

sleep 1
if [[ -f "$PID_FILE" ]]; then
  echo "live_guard started (pid=$(cat "$PID_FILE"))"
else
  echo "live_guard start requested; check $LOG_FILE"
fi
