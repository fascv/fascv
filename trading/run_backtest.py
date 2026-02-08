from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict

from trading.app import build_pipeline
from trading.config import load_config
from trading.data.backtest import BacktestCSVDataSource
from trading.execution.backtest import BacktestExecutionConfig, BacktestSimulator


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def run_backtest(config_path: str) -> Dict[str, Any]:
    cfg = load_config(config_path).raw

    data_path = _cfg(cfg, "data.path", "")
    default_micro = _cfg(cfg, "data.default_micro", {})
    data_source = BacktestCSVDataSource(path=data_path, default_micro=default_micro)
    execution = BacktestSimulator(
        BacktestExecutionConfig(
            latency_bars=int(_cfg(cfg, "execution.latency_bars", 0)),
            partial_fill_ratio=float(_cfg(cfg, "execution.partial_fill_ratio", 1.0)),
            slippage_bps=float(_cfg(cfg, "execution.slippage_bps", 1.0)),
        ),
        maker_fee_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
        taker_fee_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
    )

    journal_path = _cfg(cfg, "journal.path", "logs/journal.jsonl")
    os.makedirs(os.path.dirname(journal_path), exist_ok=True)
    pipeline = build_pipeline(cfg, data_source=data_source, execution=execution, journal_path=journal_path)

    result = pipeline.run_backtest()
    if pipeline.journal:
        pipeline.journal.close()

    report_path = _cfg(cfg, "report.path", "reports/report.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "metrics": result.metrics,
                "trades": result.trades,
                "equity_curve": result.equity_curve,
            },
            f,
            indent=2,
        )

    return result.metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    metrics = run_backtest(args.config)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
