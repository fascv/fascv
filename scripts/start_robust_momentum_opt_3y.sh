#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="${DB_PATH:-data/market_robust.db}"
JOBS="${JOBS:-12}"
TRIALS="${TRIALS:-2400}"
START_TS="${START_TS:-2023-01-01T00:00:00Z}"
END_TS="${END_TS:-}"

mkdir -p logs reports data configs

FETCH_CMD=(
  python3 scripts/fetch_kraken_trades_ohlcv.py
  --db "$DB_PATH"
  --symbol XBT/EUR
  --timeframe 5m
  --start "$START_TS"
  --sleep-sec 0.35
  --timeout-sec 20
  --retry-max 20
  --retry-backoff-sec 1.5
  --retry-backoff-max-sec 60
  --progress-every 500
)
if [[ -n "$END_TS" ]]; then
  FETCH_CMD+=(--end "$END_TS")
fi

"${FETCH_CMD[@]}"

python3 scripts/prepare_kraken_data_and_optimize.py \
  --config configs/sim.yaml \
  --db-path "$DB_PATH" \
  --symbol XBT/EUR \
  --timeframe 5m \
  --start 2023-01-01 \
  --min-years 2.5 \
  --max-events 0 \
  --target-splits 8 \
  --train-ratio 3 \
  --val-ratio 1 \
  --min-train 20000 \
  --min-val 7000 \
  --min-test 7000 \
  --purge-pct 0.003 \
  --trials "$TRIALS" \
  --jobs "$JOBS" \
  --min-oos-trades 40 \
  --out-yaml configs/sim_optimized_momentum_robust.yaml \
  --out-json reports/sim_opt_report_momentum_robust.json \
  --profile-json reports/data_profile_momentum_robust.json
