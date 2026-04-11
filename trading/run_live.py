from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict

from trading.app import build_pipeline
from trading.config import load_config
from trading.utils.env import load_env
from trading.data.backtest import BacktestCSVDataSource
from trading.data.live import KrakenWebSocketDataSource
from trading.execution.backtest import BacktestExecutionConfig, BacktestSimulator
from trading.execution.paper import PaperExecutionAdapter
from trading.execution.live import KrakenRestExecutionAdapter


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def run_live(config_path: str, mode: str) -> Dict[str, Any]:
    load_env()
    cfg = load_config(config_path).raw

    journal_path = _cfg(cfg, "journal.path", "logs/journal_live.jsonl")
    os.makedirs(os.path.dirname(journal_path), exist_ok=True)

    if mode == "paper":
        mock_path = _cfg(cfg, "live.mock_data_path", _cfg(cfg, "data.path", ""))
        default_micro = _cfg(cfg, "data.default_micro", {})
        data_source = BacktestCSVDataSource(path=mock_path, default_micro=default_micro)
        execution = PaperExecutionAdapter(
            BacktestSimulator(
                BacktestExecutionConfig(
                    latency_bars=int(_cfg(cfg, "execution.latency_bars", 0)),
                    partial_fill_ratio=float(_cfg(cfg, "execution.partial_fill_ratio", 1.0)),
                    slippage_bps=float(_cfg(cfg, "execution.slippage_bps", 1.0)),
                ),
                maker_fee_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
                taker_fee_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
            )
        )
    else:
        pair = _cfg(cfg, "live.kraken_pair", "BTC/EUR")
        ws_url = _cfg(cfg, "live.websocket_url", "wss://ws.kraken.com/v2")
        data_source = KrakenWebSocketDataSource(pair=pair, url=ws_url)
        execution = KrakenRestExecutionAdapter(
            api_key=_cfg(cfg, "live.api_key", ""),
            api_secret=_cfg(cfg, "live.api_secret", ""),
            max_rate_per_sec=float(_cfg(cfg, "live.rate_limit_per_sec", 1.0)),
        )

    pipeline = build_pipeline(cfg, data_source=data_source, execution=execution, journal_path=journal_path)
    result = pipeline.run_backtest()
    if pipeline.journal:
        pipeline.journal.close()
    return result.metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()
    metrics = run_live(args.config, args.mode)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
