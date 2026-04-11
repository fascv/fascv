#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="${DB_PATH:-data/market_robust.db}"
JOBS="${JOBS:-12}"
TRIALS_LIST="${TRIALS_LIST:-1200,2400}"
SEEDS="${SEEDS:-42,1337}"
START_TS="${START_TS:-}"
END_TS="${END_TS:-}"
FAMILIES="${FAMILIES:-momentum,mean_reversion,breakout,swing,auto}"
TOPUP_LOOKBACK_SEC="${TOPUP_LOOKBACK_SEC:-86400}"
SYMBOL="${SYMBOL:-XBT/EUR}"
TIMEFRAME="${TIMEFRAME:-5m}"
START_DATE="${START_DATE:-2023-01-01}"
MIN_YEARS="${MIN_YEARS:-2.5}"
SYMBOL_TAG="${SYMBOL_TAG:-$(echo "$SYMBOL" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//')}"

if [[ -z "${OUT_YAML_FINAL+x}" ]]; then
  if [[ "$SYMBOL_TAG" == "xbt_eur" || "$SYMBOL_TAG" == "btc_eur" ]]; then
    OUT_YAML_FINAL="configs/sim_optimized_professional_governed.yaml"
  else
    OUT_YAML_FINAL="configs/sim_optimized_professional_governed_${SYMBOL_TAG}.yaml"
  fi
fi
if [[ -z "${OUT_JSON_FINAL+x}" ]]; then
  if [[ "$SYMBOL_TAG" == "xbt_eur" || "$SYMBOL_TAG" == "btc_eur" ]]; then
    OUT_JSON_FINAL="reports/sim_opt_report_professional_governed.json"
  else
    OUT_JSON_FINAL="reports/sim_opt_report_professional_governed_${SYMBOL_TAG}.json"
  fi
fi
if [[ -z "${GOVERNANCE_JSON+x}" ]]; then
  if [[ "$SYMBOL_TAG" == "xbt_eur" || "$SYMBOL_TAG" == "btc_eur" ]]; then
    GOVERNANCE_JSON="reports/professional_governance_summary.json"
  else
    GOVERNANCE_JSON="reports/professional_governance_summary_${SYMBOL_TAG}.json"
  fi
fi
if [[ -z "${GOVERNANCE_LOG+x}" ]]; then
  if [[ "$SYMBOL_TAG" == "xbt_eur" || "$SYMBOL_TAG" == "btc_eur" ]]; then
    GOVERNANCE_LOG="reports/professional_governance_runs.jsonl"
  else
    GOVERNANCE_LOG="reports/professional_governance_runs_${SYMBOL_TAG}.jsonl"
  fi
fi

mkdir -p logs reports data configs

if [[ -z "$START_TS" ]]; then
  START_TS="$(python3 - "$DB_PATH" "$TOPUP_LOOKBACK_SEC" "$SYMBOL" "$TIMEFRAME" <<'PY'
import datetime
import sqlite3
import sys

db_path = str(sys.argv[1] if len(sys.argv) > 1 else "data/market_robust.db")
lookback = int(float(sys.argv[2] if len(sys.argv) > 2 else "86400"))
symbol = str(sys.argv[3] if len(sys.argv) > 3 else "XBT/EUR")
timeframe = str(sys.argv[4] if len(sys.argv) > 4 else "5m")
fallback = "2023-01-01T00:00:00Z"

try:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    row = cur.execute(
        "select max(ts_unix) from candles where symbol=? and timeframe=?",
        (symbol, timeframe),
    ).fetchone()
    con.close()
    mx = int(row[0]) if row and row[0] is not None else 0
except Exception:
    mx = 0

if mx <= 0:
    print(fallback)
else:
    start_unix = max(0, mx - max(0, lookback))
    dt = datetime.datetime.fromtimestamp(start_unix, tz=datetime.timezone.utc)
    print(dt.isoformat().replace("+00:00", "Z"))
PY
)"
fi

echo "Using symbol/timeframe: $SYMBOL / $TIMEFRAME"
echo "Using incremental fetch start: $START_TS"

FETCH_CMD=(
  python3 scripts/fetch_kraken_trades_ohlcv.py
  --db "$DB_PATH"
  --symbol "$SYMBOL"
  --timeframe "$TIMEFRAME"
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

python3 scripts/governed_professional_optimizer.py \
  --config configs/sim_auto.yaml \
  --db-path "$DB_PATH" \
  --symbol "$SYMBOL" \
  --timeframe "$TIMEFRAME" \
  --start "$START_DATE" \
  --families "$FAMILIES" \
  --trials-list "$TRIALS_LIST" \
  --seed-list "$SEEDS" \
  --jobs "$JOBS" \
  --min-years "$MIN_YEARS" \
  --max-events 0 \
  --target-splits 8 \
  --train-ratio 3 \
  --val-ratio 1 \
  --min-train 20000 \
  --min-val 7000 \
  --min-test 7000 \
  --purge-pct 0.003 \
  --min-oos-trades 30 \
  --holdout-min-return 0.0 \
  --holdout-min-sharpe 0.0 \
  --holdout-max-drawdown 8.0 \
  --holdout-max-realized-cost-bps 40.0 \
  --oos-min-return 0.0 \
  --oos-min-trades 30.0 \
  --out-yaml-final "$OUT_YAML_FINAL" \
  --out-json-final "$OUT_JSON_FINAL" \
  --governance-json "$GOVERNANCE_JSON" \
  --governance-log "$GOVERNANCE_LOG"
