#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="${PID_FILE:-logs/live_guard.pid}"
LOG_FILE="${GUARD_LOG:-logs/live_guard.log}"

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "live_guard: running (pid=$pid)"
  else
    echo "live_guard: pid file exists but process is not running"
  fi
else
  echo "live_guard: not running"
fi

if curl -fsS -m 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "control_gui: up (http://127.0.0.1:8000/health)"
else
  echo "control_gui: down"
fi

if [[ -f "$LOG_FILE" ]]; then
  echo "--- last log lines ---"
  tail -n 20 "$LOG_FILE"
fi
