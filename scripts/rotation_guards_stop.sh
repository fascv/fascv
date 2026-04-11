#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs

stop_pid() {
  local pid="$1"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  kill -INT "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  kill -TERM "$pid" 2>/dev/null || true
  sleep 1
  kill -KILL "$pid" 2>/dev/null || true
}

stop_lane() {
  local name="$1"
  local control_port="$2"
  local pid_file="logs/${name}_rotation_guard.pid"
  local child_pid_file="logs/${name}_rotation_guard.child.pid"
  local disable_file="logs/${name}_rotation_guard.disabled"

  touch "$disable_file"

  local pid=""
  local child_pid=""
  [[ -f "$pid_file" ]] && pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -f "$child_pid_file" ]] && child_pid="$(cat "$child_pid_file" 2>/dev/null || true)"

  # First try to flatten while control is alive.
  curl -sS -X POST "http://127.0.0.1:${control_port}/flatten" >/dev/null 2>&1 || true
  sleep 2
  curl -sS -X POST "http://127.0.0.1:${control_port}/pause" >/dev/null 2>&1 || true
  curl -sS -X POST "http://127.0.0.1:${control_port}/shutdown" >/dev/null 2>&1 || true

  stop_pid "$pid"
  stop_pid "$child_pid"

  rm -f "$pid_file" "$child_pid_file"
  echo "${name}: stopped"
}

stop_lane "op" 8004
stop_lane "near" 8008
stop_lane "ena" 8010
stop_lane "render" 8012
stop_lane "dot" 8014
stop_lane "hbar" 8016
stop_lane "esp" 8018
stop_lane "kite" 8020
