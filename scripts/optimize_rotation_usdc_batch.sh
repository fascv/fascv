#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p reports/binance_rotation_usdc configs/binance_rotation_usdc

echo "[$(date -Is)] batch start"

for sym in NEARUSDC DOTUSDC SUIUSDC; do
  echo "[$(date -Is)] fetch $sym"
  python3 scripts/fetch_binance_klines.py \
    --db data/market.binance.db \
    --symbol "$sym" \
    --timeframe 15m \
    --start 2024-03-01 \
    --end 2026-03-01
done

for pair in MORPHO/USDC NEAR/USDC DOT/USDC SUI/USDC; do
  slug="$(echo "$pair" | tr '[:upper:]/' '[:lower:]_')"
  echo "[$(date -Is)] optimize $pair"
  python3 scripts/optimize_sim_professional.py \
    --config configs/sim_binance_professional.yaml \
    --db-path data/market.binance.db \
    --symbol "$pair" \
    --timeframe 15m \
    --max-events 70000 \
    --families common8 \
    --trials 80 \
    --selection-candidates 30 \
    --jobs 4 \
    --min-oos-trades 8 \
    --enforce-positive-oos \
    --out-yaml "configs/binance_rotation_usdc/sim_optimized_${slug}_15m.yaml" \
    --out-json "reports/binance_rotation_usdc/sim_opt_report_${slug}_15m.json"
done

python3 - <<'PY'
import glob
import json

items = []
for path in sorted(glob.glob("reports/binance_rotation_usdc/sim_opt_report_*_15m.json")):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        continue
    best = doc.get("best") or {}
    items.append(
        {
            "report": path,
            "family": best.get("family"),
            "final_holdout_return_pct": best.get("final_holdout_return_pct"),
            "final_holdout_max_drawdown_pct": best.get("final_holdout_max_drawdown_pct"),
            "oos_trades_mean": best.get("oos_trades_mean"),
        }
    )

with open("reports/binance_rotation_usdc/summary.json", "w", encoding="utf-8") as f:
    json.dump({"items": items}, f, ensure_ascii=True, indent=2)

print(json.dumps({"summary": "reports/binance_rotation_usdc/summary.json", "items": len(items)}, ensure_ascii=True))
PY

echo "[$(date -Is)] batch done"
