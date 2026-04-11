#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs

UNIT_NAME="${UNIT_NAME:-codex-rotation-lane-free-candidates}"
PID_FILE="${PID_FILE:-$ROOT_DIR/logs/rotation_lane_free_candidates.pid}"
OUT_JSONL="${OUT_JSONL:-$ROOT_DIR/logs/rotation_lane_free_candidates.jsonl}"
LATEST_JSON="${LATEST_JSON:-$ROOT_DIR/logs/rotation_lane_free_candidates_latest.json}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/logs/rotation_lane_free_candidates.log}"
INTERVAL_SEC="${INTERVAL_SEC:-30}"
TOP="${TOP:-8}"

if systemctl --user is-active --quiet "${UNIT_NAME}.service"; then
  echo "lane-free logger already running (unit=${UNIT_NAME}.service)"
  exit 0
fi

rm -f "$PID_FILE"

if ! systemd-run --user \
  --unit "$UNIT_NAME" \
  --collect \
  --property=Restart=always \
  --property=RestartSec=5 \
  /bin/bash -lc "cd '$ROOT_DIR' && exec python3 scripts/rotation_lane_free_candidates.py --interval-sec '$INTERVAL_SEC' --top '$TOP' --pid-file '$PID_FILE' --out-jsonl '$OUT_JSONL' --latest-json '$LATEST_JSON' >> '$LOG_FILE' 2>&1"; then
  # Unit may already exist transiently; try a plain start as fallback.
  systemctl --user start "${UNIT_NAME}.service"
fi

sleep 1
if systemctl --user is-active --quiet "${UNIT_NAME}.service"; then
  echo "lane-free logger started (unit=${UNIT_NAME}.service, log=$LOG_FILE)"
else
  echo "lane-free logger start requested but unit is not active; check journalctl --user -u ${UNIT_NAME}.service"
fi
