from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class Report:
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    equity_curve: List[Dict[str, Any]]


def _sharpe(returns: List[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = var ** 0.5
    if std == 0:
        return 0.0
    return mean / std * (len(returns) ** 0.5)


def build_report(
    equity_curve: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    expected_cost_bps: List[float],
    realized_cost_bps: List[float],
    turnover_eur: float,
) -> Report:
    returns = [row["ret"] for row in equity_curve if row.get("ret") is not None]
    sharpe = _sharpe(returns)
    max_dd = max((row["drawdown_pct"] for row in equity_curve), default=0.0)
    hit_rate = 0.0
    if trades:
        wins = sum(1 for t in trades if t["pnl_eur"] > 0)
        hit_rate = wins / len(trades)
    profit_factor = 0.0
    if trades:
        gains = sum(t["pnl_eur"] for t in trades if t["pnl_eur"] > 0)
        losses = -sum(t["pnl_eur"] for t in trades if t["pnl_eur"] < 0)
        profit_factor = gains / losses if losses > 0 else 0.0
    avg_expected = sum(expected_cost_bps) / len(expected_cost_bps) if expected_cost_bps else 0.0
    avg_realized = sum(realized_cost_bps) / len(realized_cost_bps) if realized_cost_bps else 0.0

    metrics = {
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "turnover_eur": turnover_eur,
        "avg_expected_cost_bps": avg_expected,
        "avg_realized_cost_bps": avg_realized,
        "hit_rate": hit_rate,
        "profit_factor": profit_factor,
        "trades": len(trades),
        "bars": len(equity_curve),
    }
    return Report(metrics=metrics, trades=trades, equity_curve=equity_curve)
