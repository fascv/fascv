#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PID_FILE="${PID_FILE:-logs/live_guard.pid}"
CHILD_PID_FILE="${CHILD_PID_FILE:-logs/live_guard.child.pid}"
DISABLE_FILE="${DISABLE_FILE:-logs/live_guard.disabled}"

mkdir -p logs
touch "$DISABLE_FILE"

child_pids() {
  local parent="$1"
  ps -o pid= --ppid "$parent" 2>/dev/null | awk '{print $1}'
}

collect_descendants_postorder() {
  local parent="$1"
  local child=""
  while read -r child; do
    [[ -z "$child" ]] && continue
    collect_descendants_postorder "$child"
    printf '%s\n' "$child"
  done < <(child_pids "$parent")
}

kill_pid_tree() {
  local root_pid="${1:-0}"
  [[ "$root_pid" =~ ^[0-9]+$ ]] || return 0
  (( root_pid > 0 )) || return 0

  local targets=()
  local pid=""
  while read -r pid; do
    [[ -n "$pid" ]] && targets+=("$pid")
  done < <(collect_descendants_postorder "$root_pid")
  targets+=("$root_pid")

  local sig=""
  local alive=0
  for sig in TERM KILL; do
    for pid in "${targets[@]}"; do
      kill "-$sig" "$pid" >/dev/null 2>&1 || true
    done
    for _ in {1..15}; do
      alive=0
      for pid in "${targets[@]}"; do
        if kill -0 "$pid" >/dev/null 2>&1; then
          alive=1
          break
        fi
      done
      (( alive == 0 )) && return 0
      sleep 0.2
    done
  done
}

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
  kill_pid_tree "$pid"
  echo "live_guard stopped (pid=$pid)"
else
  echo "live_guard process not found (stale pid=$pid)"
fi

rm -f "$PID_FILE"
if [[ -f "$CHILD_PID_FILE" ]]; then
  child_pid="$(cat "$CHILD_PID_FILE" 2>/dev/null || true)"
  if [[ -n "${child_pid}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill_pid_tree "$child_pid"
  fi
  rm -f "$CHILD_PID_FILE"
fi

# Best effort: ensure launched stack is down, too.
curl -sS -X POST http://127.0.0.1:8000/shutdown >/dev/null 2>&1 || true
