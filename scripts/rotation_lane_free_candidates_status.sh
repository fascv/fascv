#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

UNIT_NAME="${UNIT_NAME:-codex-rotation-lane-free-candidates}"
PID_FILE="${PID_FILE:-$ROOT_DIR/logs/rotation_lane_free_candidates.pid}"
LATEST_JSON="${LATEST_JSON:-$ROOT_DIR/logs/rotation_lane_free_candidates_latest.json}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/logs/rotation_lane_free_candidates.log}"

if systemctl --user is-active --quiet "${UNIT_NAME}.service"; then
  echo "running unit=${UNIT_NAME}.service"
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]]; then
      echo "pid=$PID"
    fi
  fi
else
  echo "not running unit=${UNIT_NAME}.service"
  if [[ -f "$PID_FILE" ]]; then
    echo "pid_file_present=true (possibly stale)"
  fi
fi

if [[ -f "$LATEST_JSON" ]]; then
  echo "latest snapshot:"
  python3 - <<'PY'
import json
from pathlib import Path
p = Path("logs/rotation_lane_free_candidates_latest.json")
obj = json.loads(p.read_text(encoding="utf-8"))
top = obj.get("lane_free_candidate_top") or {}
print(json.dumps({
    "ts": obj.get("ts"),
    "source_generated_at": obj.get("source_generated_at"),
    "lane_free_candidate_count": obj.get("lane_free_candidate_count"),
    "lane_free_top_symbol": top.get("symbol", ""),
}, ensure_ascii=True, indent=2))
PY
fi

if [[ -f "$LOG_FILE" ]]; then
  echo "log tail:"
  tail -n 5 "$LOG_FILE" || true
fi
