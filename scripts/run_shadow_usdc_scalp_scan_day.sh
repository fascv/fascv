#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs/shadow_usdc_scalp_snapshots

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] start shadow usdc scalp scan" >> logs/shadow_usdc_scalp_scan.log
python3 scripts/scan_shadow_usdc_scalp_universe.py --iterations 96 --interval-minutes 15 --continue-on-error >> logs/shadow_usdc_scalp_scan.log 2>&1
python3 scripts/analyze_shadow_usdc_scalp_universe.py --lookback-hours 24 --top-keep 100 --min-scans 4 >> logs/shadow_usdc_scalp_scan.log 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] finished shadow usdc scalp scan" >> logs/shadow_usdc_scalp_scan.log
