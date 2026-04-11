#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

INTERVAL_MINUTES="${SHADOW_USDC_SCAN_INTERVAL_MINUTES:-15}"
LOOKBACK_HOURS="${SHADOW_USDC_SCAN_LOOKBACK_HOURS:-24}"
TOP_KEEP="${SHADOW_USDC_SCAN_TOP_KEEP:-100}"
MIN_SCANS="${SHADOW_USDC_SCAN_MIN_SCANS:-4}"
LOG_FILE="logs/shadow_usdc_scalp_scan.log"

mkdir -p logs/shadow_usdc_scalp_snapshots

running=1
trap 'running=0' INT TERM

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] start continuous shadow usdc scalp scan interval=${INTERVAL_MINUTES}m" >> "$LOG_FILE"

while (( running )); do
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] snapshot cycle begin" >> "$LOG_FILE"

  python3 scripts/scan_shadow_usdc_scalp_universe.py \
    --iterations 1 \
    --interval-minutes "$INTERVAL_MINUTES" \
    --continue-on-error >> "$LOG_FILE" 2>&1 || true

  python3 scripts/analyze_shadow_usdc_scalp_universe.py \
    --lookback-hours "$LOOKBACK_HOURS" \
    --top-keep "$TOP_KEEP" \
    --min-scans "$MIN_SCANS" >> "$LOG_FILE" 2>&1 || true

  if (( ! running )); then
    break
  fi

  sleep_seconds="$(python3 - <<PY
interval_minutes = float("${INTERVAL_MINUTES}")
print(max(0, int(interval_minutes * 60)))
PY
)"

  for (( elapsed=0; elapsed<sleep_seconds; elapsed+=1 )); do
    if (( ! running )); then
      break 2
    fi
    sleep 1
  done
done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] stop continuous shadow usdc scalp scan" >> "$LOG_FILE"
