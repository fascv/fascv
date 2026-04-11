#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs

wait_http() {
  local url="$1"
  local deadline_s="${2:-20}"
  local start_ts
  start_ts="$(date +%s)"
  while true; do
    if curl -fsS -m 0.5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    if (( "$(date +%s)" - start_ts >= deadline_s )); then
      return 1
    fi
    sleep 1
  done
}

start_lane() {
  local name="$1"
  local config="$2"
  local control_port="$3"
  local exec_port="$4"
  local journal_port="$5"
  local core_port="$6"
  local md_port="$7"

  local pid_file="logs/${name}_rotation_guard.pid"
  local child_pid_file="logs/${name}_rotation_guard.child.pid"
  local disable_file="logs/${name}_rotation_guard.disabled"
  local guard_log="logs/${name}_rotation_guard.log"

  rm -f "$disable_file"

  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "${name}: already running (guard pid=$pid)"
      return 0
    fi
    rm -f "$pid_file"
  fi

  if command -v setsid >/dev/null 2>&1; then
    setsid env \
      MODE=live \
      CONFIG="$config" \
      START_IMPACT_CONSOLE=0 \
      START_JOURNAL_GUI=0 \
      START_CORE_GUI=0 \
      START_MD_GUI=0 \
      START_EXEC=0 \
      CONTROL_PORT="$control_port" \
      EXEC_PORT="$exec_port" \
      JOURNAL_GUI_PORT="$journal_port" \
      CORE_GUI_PORT="$core_port" \
      MD_GUI_PORT="$md_port" \
      GUARD_LOG="$guard_log" \
      PID_FILE="$pid_file" \
      CHILD_PID_FILE="$child_pid_file" \
      DISABLE_FILE="$disable_file" \
      ./scripts/live_guard.sh >> "$guard_log" 2>&1 < /dev/null &
  else
    nohup env \
      MODE=live \
      CONFIG="$config" \
      START_IMPACT_CONSOLE=0 \
      START_JOURNAL_GUI=0 \
      START_CORE_GUI=0 \
      START_MD_GUI=0 \
      START_EXEC=0 \
      CONTROL_PORT="$control_port" \
      EXEC_PORT="$exec_port" \
      JOURNAL_GUI_PORT="$journal_port" \
      CORE_GUI_PORT="$core_port" \
      MD_GUI_PORT="$md_port" \
      GUARD_LOG="$guard_log" \
      PID_FILE="$pid_file" \
      CHILD_PID_FILE="$child_pid_file" \
      DISABLE_FILE="$disable_file" \
      ./scripts/live_guard.sh >> "$guard_log" 2>&1 < /dev/null &
  fi

  sleep 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    echo "${name}: start requested; guard pid not yet written (check $guard_log)" >&2
    return 1
  fi

  if wait_http "http://127.0.0.1:${control_port}/health" 25; then
    echo "${name}: started (guard pid=$pid, control=http://127.0.0.1:${control_port}/)"
  else
    echo "${name}: guard running (pid=$pid), but control did not become ready yet (check $guard_log)" >&2
    return 1
  fi
}

start_lane "resolv" "configs/live_binance_resolv_usdc_rotation.yaml" 8312 18610 18620 18630 18640
start_lane "sign" "configs/live_binance_sign_usdc_rotation.yaml" 8306 18310 18320 18330 18340
start_lane "wlfi" "configs/live_binance_wlfi_usdc_rotation.yaml" 8358 20910 20920 20930 20940
start_lane "kite" "configs/live_binance_kite_usdc_rotation.yaml" 8020 9010 9020 9030 9040
