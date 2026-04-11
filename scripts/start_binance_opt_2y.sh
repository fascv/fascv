#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="${DB_PATH:-data/market.binance.db}"
SYMBOL="${SYMBOL:-ETH/USDT}"
TIMEFRAME="${TIMEFRAME:-15m}"
START="${START:-$(date -u -d '2 years ago' +%F)}"
END="${END:-$(date -u +%F)}"
TRIALS="${TRIALS:-120}"
MAX_EVENTS="${MAX_EVENTS:-70000}"
CONFIG="${CONFIG:-configs/sim_binance_spot.yaml}"
OUT_YAML="${OUT_YAML:-configs/sim_optimized_binance.yaml}"

SYMBOL_SLUG="$(echo "$SYMBOL" | tr '/-' '_' | tr '[:upper:]' '[:lower:]')"
OUT_JSON="${OUT_JSON:-reports/sim_opt_binance_${SYMBOL_SLUG}_${TIMEFRAME}.json}"

echo "[1/2] Fetch Binance klines: $SYMBOL $TIMEFRAME $START -> $END"
python3 scripts/fetch_binance_klines.py \
  --db "$DB_PATH" \
  --symbol "$SYMBOL" \
  --timeframe "$TIMEFRAME" \
  --start "$START" \
  --end "$END"

echo "[2/2] Optimize on SQLite candles"
python3 scripts/optimize_sim_all.py \
  --config "$CONFIG" \
  --db-path "$DB_PATH" \
  --symbol "$SYMBOL" \
  --timeframe "$TIMEFRAME" \
  --max-events "$MAX_EVENTS" \
  --trials "$TRIALS" \
  --out-yaml "$OUT_YAML" \
  --out-json "$OUT_JSON"

echo "Done: $OUT_JSON"
