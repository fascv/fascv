# Modular BTC Spot Trading Framework (Kraken)

This is a minimal, modular Python framework for short-term BTC spot strategies (5m–30m). Backtest and live share the same pipeline; only the data source and execution adapter are swapped.

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

## Notes
- Cost model + tradeability gate are applied before any order decision.
- Kill switches: daily loss limit, max drawdown, cooldown.
- Journal stores features, costs, gate, alpha, risk, orders, fills, fees, slippage, and state per bar.
- Kraken pair mapping uses XBT for BTC (e.g., `BTC/EUR` -> `XBT/EUR`).
- Live exec supports Kraken dead-man switch via CancelAllOrdersAfter (configurable tick/timeout).

## Live
Live uses Kraken WS v2 (public book + trades) and REST for orders. API keys must be provided via environment variables (`KRAKEN_API_KEY`, `KRAKEN_API_SECRET`) or a `.env` file with `chmod 600`.

## Chaos harness

```bash
python3 scripts/chaos_harness.py --config configs/chaos.yaml
```

## Ops templates

- `GO_LIVE_CHECKLIST.md`
- `ops/btc-trader.service`
- `ops/btc-trader.logrotate`
