#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

status_lane() {
  local symbol="$1"
  local slug="$2"
  local control_port="$3"
  local lane_type="$4"
  local pid_file="logs/${slug}_rotation_guard.pid"
  local child_pid_file="logs/${slug}_rotation_guard.child.pid"
  local disable_file="logs/${slug}_rotation_guard.disabled"

  local guard_pid="-"
  local child_pid="-"
  local guard_state="down"
  local child_state="down"
  local health="000"
  local disabled="no"

  [[ -f "$disable_file" ]] && disabled="yes"

  if [[ -f "$pid_file" ]]; then
    guard_pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$guard_pid" ]] && kill -0 "$guard_pid" 2>/dev/null; then
      guard_state="up"
    fi
  fi

  if [[ -f "$child_pid_file" ]]; then
    child_pid="$(cat "$child_pid_file" 2>/dev/null || true)"
    if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
      child_state="up"
    fi
  fi

  health="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${control_port}/health" || true)"

  printf '%-10s %-8s guard=%-4s pid=%-8s child=%-4s child_pid=%-8s health=%-3s disabled=%s\n' \
    "$symbol" "$lane_type" "$guard_state" "$guard_pid" "$child_state" "$child_pid" "$health" "$disabled"
}

ACTIVE_FILE="$ROOT_DIR/configs/rotation_active_lanes.json"

if [[ ! -f "$ACTIVE_FILE" ]]; then
  echo "rotation_active_lanes.json fehlt: $ACTIVE_FILE"
  exit 1
fi

mapfile -t lane_rows < <(
python3 - "$ROOT_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
active_file = root / "configs" / "rotation_active_lanes.json"
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from trading.rotation_universe import POOL, build_lanes

lanes = build_lanes(POOL)
payload = json.loads(active_file.read_text(encoding="utf-8"))

selected = [str(item).upper() for item in (payload.get("selected") or [])]
selected = [symbol for symbol in selected if symbol in lanes]
watch = [str(item).upper() for item in (payload.get("watch_symbols") or selected)]
watch = [symbol for symbol in watch if symbol in lanes]
if not watch:
    watch = list(selected)

ordered: list[str] = []
for symbol in selected + watch:
    if symbol not in ordered:
        ordered.append(symbol)

print(f"__META__\tselected={len(selected)}\twatch={len(watch)}\tshown={len(ordered)}")
for symbol in ordered:
    lane = lanes[symbol]
    slug = str(lane["slug"])
    control_port = int(lane["ports"][0])
    lane_type = "selected" if symbol in selected else "watch"
    print(f"{symbol}\t{slug}\t{control_port}\t{lane_type}")
PY
)

if [[ ${#lane_rows[@]} -eq 0 ]]; then
  echo "Keine Lanes gefunden."
  exit 1
fi

meta_line="${lane_rows[0]}"
if [[ "$meta_line" == __META__* ]]; then
  printf '%s\n' "${meta_line#__META__	}"
fi

for row in "${lane_rows[@]:1}"; do
  IFS=$'\t' read -r symbol slug control_port lane_type <<< "$row"
  status_lane "$symbol" "$slug" "$control_port" "$lane_type"
done
