#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="${MODE:-live}"
CONFIG="${CONFIG:-configs/live_binance_eth_usdc.yaml}"
MAX_RESTARTS="${MAX_RESTARTS:-0}"          # 0 = infinite
BASE_SLEEP_SEC="${BASE_SLEEP_SEC:-2}"
MAX_SLEEP_SEC="${MAX_SLEEP_SEC:-60}"
MIN_UPTIME_SEC="${MIN_UPTIME_SEC:-90}"     # if run was stable this long, reset backoff

GUARD_LOG="${GUARD_LOG:-logs/live_guard.log}"
PID_FILE="${PID_FILE:-logs/live_guard.pid}"
CHILD_PID_FILE="${CHILD_PID_FILE:-logs/live_guard.child.pid}"
DISABLE_FILE="${DISABLE_FILE:-logs/live_guard.disabled}"

mkdir -p logs
echo "$$" > "$PID_FILE"

stop=0
child_pid=""

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

stop_child() {
  if [[ -n "${child_pid}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill_pid_tree "$child_pid"
    wait "$child_pid" >/dev/null 2>&1 || true
  fi
  rm -f "$CHILD_PID_FILE"
  child_pid=""
}

on_term() {
  stop=1
  stop_child
}
trap on_term INT TERM

restart_count=0
sleep_sec="$BASE_SLEEP_SEC"

ts() { date -Is; }

echo "$(ts) [guard] start mode=${MODE} config=${CONFIG}" >> "$GUARD_LOG"

wait_disabled() {
  echo "$(ts) [guard] disabled via ${DISABLE_FILE}; waiting" >> "$GUARD_LOG"
  while (( stop == 0 )) && [[ -f "$DISABLE_FILE" ]]; do
    sleep 5
  done
}

if [[ -f "$DISABLE_FILE" ]]; then
  wait_disabled
fi

while (( stop == 0 )); do
  if [[ -f "$DISABLE_FILE" ]]; then
    wait_disabled
    continue
  fi
  start_epoch="$(date +%s)"
  echo "$(ts) [guard] launch trading.sh" >> "$GUARD_LOG"

  set +e
  MODE="$MODE" CONFIG="$CONFIG" ./trading.sh >> "$GUARD_LOG" 2>&1 &
  child_pid=$!
  echo "$child_pid" > "$CHILD_PID_FILE"
  wait "$child_pid"
  rc=$?
  set -e
  rm -f "$CHILD_PID_FILE"
  child_pid=""

  end_epoch="$(date +%s)"
  runtime=$(( end_epoch - start_epoch ))

  echo "$(ts) [guard] trading.sh exited rc=${rc} runtime_sec=${runtime}" >> "$GUARD_LOG"

  if (( stop != 0 )); then
    break
  fi

  if (( MAX_RESTARTS > 0 )); then
    restart_count=$(( restart_count + 1 ))
    if (( restart_count > MAX_RESTARTS )); then
      echo "$(ts) [guard] max restarts reached (${MAX_RESTARTS}), stop" >> "$GUARD_LOG"
      break
    fi
  fi

  if (( runtime >= MIN_UPTIME_SEC )); then
    sleep_sec="$BASE_SLEEP_SEC"
  else
    sleep_sec=$(( sleep_sec * 2 ))
    if (( sleep_sec > MAX_SLEEP_SEC )); then
      sleep_sec="$MAX_SLEEP_SEC"
    fi
  fi

  echo "$(ts) [guard] restart in ${sleep_sec}s" >> "$GUARD_LOG"
  sleep "$sleep_sec"
done

echo "$(ts) [guard] stop" >> "$GUARD_LOG"
rm -f "$PID_FILE"
rm -f "$CHILD_PID_FILE"
