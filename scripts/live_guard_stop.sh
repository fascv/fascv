#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="${PID_FILE:-logs/live_guard.pid}"
CHILD_PID_FILE="${CHILD_PID_FILE:-logs/live_guard.child.pid}"
DISABLE_FILE="${DISABLE_FILE:-logs/live_guard.disabled}"

mkdir -p logs
touch "$DISABLE_FILE"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop codex-trader-live.service >/dev/null 2>&1 || true
fi

if [[ ! -f "$PID_FILE" ]]; then
  echo "live_guard not running (no pid file)"
  exit 0
fi

pid="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${pid}" ]]; then
  rm -f "$PID_FILE"
  echo "live_guard pid file was empty"
  exit 0
fi

if kill -0 "$pid" 2>/dev/null; then
  kill "$pid" || true
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" || true
  fi
  echo "live_guard stopped (pid=$pid)"
else
  echo "live_guard process not found (stale pid=$pid)"
fi

rm -f "$PID_FILE"
if [[ -f "$CHILD_PID_FILE" ]]; then
  child_pid="$(cat "$CHILD_PID_FILE" 2>/dev/null || true)"
  if [[ -n "${child_pid}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" >/dev/null 2>&1 || true
    sleep 1
    kill -9 "$child_pid" >/dev/null 2>&1 || true
  fi
  rm -f "$CHILD_PID_FILE"
fi

# Best effort: ensure launched stack is down, too.
curl -sS -X POST http://127.0.0.1:8000/shutdown >/dev/null 2>&1 || true
