#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ZIP_PATH="${ZIP_PATH:-data/Kraken_OHLCVT.zip}"
JOBS="${JOBS:-12}"
TRIALS="${TRIALS:-2400}"
START_DATE="${START_DATE:-2022-01-01}"

mkdir -p logs reports data

if [[ ! -f "$ZIP_PATH" ]]; then
  python3 scripts/download_kraken_ohlcvt.py --out "$ZIP_PATH"
fi

python3 scripts/prepare_kraken_data_and_optimize.py \
  --config configs/sim.yaml \
  --db-path data/market.db \
  --symbol XBT/EUR \
  --timeframe 5m \
  --zip "$ZIP_PATH" \
  --start "$START_DATE" \
  --min-years 3 \
  --max-events 350000 \
  --target-splits 8 \
  --train-ratio 3 \
  --val-ratio 1 \
  --min-train 40000 \
  --min-val 12000 \
  --min-test 12000 \
  --purge-pct 0.003 \
  --trials "$TRIALS" \
  --jobs "$JOBS" \
  --min-oos-trades 40 \
  --out-yaml configs/sim_optimized_momentum_robust.yaml \
  --out-json reports/sim_opt_report_momentum_robust.json \
  --profile-json reports/data_profile_momentum_robust.json
