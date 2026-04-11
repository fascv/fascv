# Modular BTC Spot Trading Framework (Kraken)

This is a minimal, modular Python framework for short-term BTC spot strategies (5m–30m). Backtest and live share the same pipeline; only the data source and execution adapter are swapped.

## Module workspaces (multi-Codex)

The repository is split into isolated module workspaces so you can run one Codex instance per module folder:

- `modules/md`
- `modules/core`
- `modules/exec`
- `modules/journal`
- `modules/control`
- `modules/impact_console`

Shared contracts live in `shared/src/trading_shared`.
The orchestration app workspace is in `apps/launch`.

Workspace launcher helper:

```bash
scripts/run_launch_workspace.sh --mode paper --config configs/paper.yaml
```

## Quick start

```bash
python -m trading.run_backtest --config configs/example.yaml
```

Outputs:
- `logs/journal.jsonl`
- `reports/report.json`

Paper mode (mock live feed):

```bash
python -m trading.run_live --config configs/example.yaml --mode paper
```

## Multi-process live engine

Start the multi-process engine (market data, core, exec, journal, optional control):

```bash
python -m trading.launch --mode paper --config configs/paper.yaml
python -m trading.launch --mode live --config configs/live.yaml
```

Control plane (optional) runs via FastAPI when `control.enabled: true`.

## Web GUI (recommended start)

This command loads `.env` automatically, forces control-plane enabled, and starts the engine + browser dashboard API:

```bash
python3 -m trading.run_gui --mode paper --config configs/paper.yaml
python3 -m trading.run_gui --mode live --config configs/live.yaml
```

GUI:
- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/docs`

## Minimal Training (Fast Smoke Test)

This produces a tiny `alpha` override with a single grid point (bounded to max 2000 events):

```bash
python3 scripts/train_momentum_min.py --config configs/paper.yaml
```

Then enable it in your config:

```yaml
alpha:
  override_path: configs/alpha_trained.yaml
```

## Notes
- Cost model + tradeability gate are applied before any order decision.
- Kill switches: daily loss limit, max drawdown, cooldown.
- Journal stores features, costs, gate, alpha, risk, orders, fills, fees, slippage, and state per bar.
- Kraken pair mapping uses XBT for BTC (e.g., `BTC/EUR` -> `XBT/EUR`).
- Live exec supports Kraken dead-man switch via CancelAllOrdersAfter (configurable tick/timeout).
- Optional `impact` process consumes `impact_console` output and publishes `NewsEvent` into core; core then enriches features with:
  - `news_sentiment`
  - `news_impact`
  - `news_source_count`
  - `news_age_sec`

## Live
Live uses Kraken WS v2 (public book + trades) and REST for orders. API keys must be provided via environment variables (`KRAKEN_API_KEY`, `KRAKEN_API_SECRET`) or a `.env` file with `chmod 600`.

## Historical OHLCV (SQLite)

For training/backtests, the project can use a simple SQLite store for historical OHLCV candles (similar to the `geld` project).

- Default DB path: `data/market.db`
- Table: `candles` (primary key: `(symbol, timeframe, ts_utc)`)

Import Kraken OHLCVT ZIP (best for deep history):

```bash
python3 -m trading.data.import_kraken_ohlcvt_zip --zip /path/to/Kraken_OHLCVT.zip --db data/market.db --timeframe-min 5 --quote EUR --start 2024-01-01 --end 2024-12-31
```

Import existing CSV (backtest format):

```bash
python3 -m trading.data.import_csv_ohlcv --db data/market.db --csv-path data/sample_ohlcv.csv --symbol XBT/EUR --timeframe 5m
```

Fetch recent candles from Kraken public OHLC endpoint (recent-window only):

```bash
python3 -m trading.data.fetch_kraken_ohlcv --db data/market.db --symbol XBT/EUR --timeframe 5m --start 2026-02-01T00:00:00Z
```

## Robust Data Prep + Optimization (Walk-Forward)

For a "data-first" optimization flow (bulk history import, coverage checks, REST top-up, auto-derived walk-forward windows, and optimizer launch), use:

```bash
python3 scripts/prepare_kraken_data_and_optimize.py \
  --config configs/sim.yaml \
  --db-path data/market.db \
  --symbol XBT/EUR \
  --timeframe 5m \
  --zip /path/to/Kraken_OHLCVT.zip \
  --start 2024-01-01 \
  --min-years 2 \
  --target-splits 5 \
  --trials 160
```

Key outputs:
- `reports/data_profile.json` (coverage, missing bars, max gaps, optimization plan)
- `configs/sim_optimized.yaml` (best overlay)
- `reports/sim_opt_report.json` (full optimization report incl. holdout)

Dry-run (show plan + command without running optimization):

```bash
python3 scripts/prepare_kraken_data_and_optimize.py --dry-run
```

## Professional Workflow (Multi-Strategy + Regime + Governance)

For a production-style run (multi-strategy search across momentum/mean-reversion/breakout/swing/auto, walk-forward robustness, and hard holdout/OOS gates):

```bash
python3 scripts/governed_professional_optimizer.py \
  --config configs/sim_auto.yaml \
  --db-path data/market_robust.db \
  --symbol XBT/EUR \
  --timeframe 5m \
  --start 2023-01-01 \
  --families momentum,mean_reversion,breakout,swing,auto \
  --trials-list 1200,2400 \
  --seed-list 42,1337 \
  --jobs 12
```

Convenience launcher with data top-up + governed optimization:

```bash
scripts/start_professional_opt_3y.sh
```

Other pair example (more volatile profile), e.g. `LINK/EUR`:

```bash
scripts/start_professional_opt_link_3y.sh
# or generic:
SYMBOL=LINK/EUR scripts/start_professional_opt_3y.sh
```

Key outputs:
- `reports/professional_governance_summary.json`
- `reports/professional_governance_runs.jsonl`
- `configs/sim_optimized_professional_governed.yaml` (only written if gates pass)

Runtime config for this mode:
- `configs/sim_professional.yaml`

## Chaos harness

```bash
python3 scripts/chaos_harness.py --config configs/chaos.yaml
```

## Exec runbook

- `docs/exec_runbook.md`

## Impact source toggle

Enable in config:

```yaml
impact:
  enabled: true
  source: api
  api_url: "http://127.0.0.1:8011/arrow?window=1h&mode=auto"
  file_path: modules/impact_console/diagnostics/arrow_1h.json
  interval_seconds: 15
  fallback_to_file: true
```

## Ops templates

- `GO_LIVE_CHECKLIST.md`
- `ops/btc-trader.service`
- `ops/btc-trader.logrotate`
