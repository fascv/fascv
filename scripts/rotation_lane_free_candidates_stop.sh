#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

UNIT_NAME="${UNIT_NAME:-codex-rotation-lane-free-candidates}"
PID_FILE="${PID_FILE:-$ROOT_DIR/logs/rotation_lane_free_candidates.pid}"

if systemctl --user is-active --quiet "${UNIT_NAME}.service"; then
  systemctl --user stop "${UNIT_NAME}.service"
  echo "lane-free logger stopped (unit=${UNIT_NAME}.service)"
else
  echo "lane-free logger unit not active (${UNIT_NAME}.service)"
fi

rm -f "$PID_FILE"
