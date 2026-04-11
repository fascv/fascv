#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
from dataclasses import dataclass
from statistics import mean
from typing import Any, Dict, Iterable, List

# Ensure repo root import.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from trading.app import build_pipeline
from trading.config import load_config
from trading.data.backtest import BacktestCSVDataSource
from trading.data.sqlite_ohlcv import SQLiteOHLCVDataSource
from trading.execution.backtest import BacktestExecutionConfig, BacktestSimulator
from trading.types import MarketEvent


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class _ListDataSource:
    def __init__(self, events: List[MarketEvent]):
        self._events = list(events)

    def __iter__(self) -> Iterable[MarketEvent]:
        yield from self._events


def _load_events(cfg: Dict[str, Any], max_events: int) -> List[MarketEvent]:
    source = str(_cfg(cfg, "data.source", "csv") or "csv").strip().lower()
    default_micro = _cfg(cfg, "data.default_micro", {})
    if source in {"sqlite", "db"}:
        events = list(
            SQLiteOHLCVDataSource(
                db_path=str(_cfg(cfg, "data.db_path", "data/market.db")),
                symbol=str(_cfg(cfg, "data.symbol", _cfg(cfg, "md.pair", "BTC/EUR"))),
                timeframe=str(_cfg(cfg, "data.timeframe", "5m")),
                default_micro=default_micro,
                start=str(_cfg(cfg, "data.start", "")) or None,
                end=str(_cfg(cfg, "data.end", "")) or None,
                limit=max_events if max_events > 0 else None,
                latest=True if max_events > 0 else False,
            )
        )
    else:
        path = str(_cfg(cfg, "data.path", "") or "").strip()
        if not path:
            fallback = "data/sim_live_5s_from_journal.csv"
            if os.path.exists(fallback):
                path = fallback
        if not path:
            raise SystemExit("No data.path configured and fallback data/sim_live_5s_from_journal.csv not found.")
        events = list(BacktestCSVDataSource(path=path, default_micro=default_micro))
        if max_events > 0 and len(events) > max_events:
            events = events[-max_events:]
    return events


def _run_metrics(cfg: Dict[str, Any], events: List[MarketEvent]) -> Dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    if isinstance(cfg.get("alpha"), dict):
        cfg["alpha"].pop("override_path", None)
    execution = BacktestSimulator(
        BacktestExecutionConfig(
            latency_bars=int(_cfg(cfg, "execution.latency_bars", 0)),
            partial_fill_ratio=float(_cfg(cfg, "execution.partial_fill_ratio", 1.0)),
            slippage_bps=float(_cfg(cfg, "execution.slippage_bps", 1.0)),
        ),
        maker_fee_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
        taker_fee_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
    )
    pipeline = build_pipeline(cfg, data_source=_ListDataSource(events), execution=execution, journal_path=None)
    return dict(pipeline.run_backtest().metrics)


@dataclass(frozen=True)
class Candidate:
    trend_lookback: int
    trend_threshold: float
    trend_scale: float
    reversion_lookback: int
    reversion_threshold: float
    reversion_scale: float
    breakout_lookback: int
    breakout_trigger: float
    breakout_scale: float
    regime_lookback: int
    trend_momentum: float
    range_momentum: float
    high_vol_atr: float
    low_vol_atr: float
    breakout_return: float

    def overlay(self) -> Dict[str, Any]:
        return {
            "alpha": {
                "type": "auto",
                "auto": {
                    "trend": {
                        "lookback": int(self.trend_lookback),
                        "threshold_bps": float(self.trend_threshold),
                        "scale": float(self.trend_scale),
                    },
                    "mean_reversion": {
                        "lookback": int(self.reversion_lookback),
                        "threshold_bps": float(self.reversion_threshold),
                        "scale": float(self.reversion_scale),
                        "max_edge_bps": 0.0,
                    },
                    "breakout": {
                        "lookback": int(self.breakout_lookback),
                        "trigger_bps": float(self.breakout_trigger),
                        "scale": float(self.breakout_scale),
                        "max_edge_bps": 0.0,
                    },
                    "regime": {
                        "lookback": int(self.regime_lookback),
                        "trend_momentum_bps": float(self.trend_momentum),
                        "range_momentum_bps": float(self.range_momentum),
                        "high_vol_atr_bps": float(self.high_vol_atr),
                        "low_vol_atr_bps": float(self.low_vol_atr),
                        "breakout_return_bps": float(self.breakout_return),
                        "default_regime": "trend",
                        "trend_strategy": "trend",
                        "range_strategy": "mean_reversion",
                        "breakout_strategy": "breakout",
                    },
                },
            }
        }


def _apply_overlay(cfg: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(cfg)
    out.setdefault("alpha", {})
    alpha_ov = overlay.get("alpha", {})
    if isinstance(alpha_ov, dict):
        for k, v in alpha_ov.items():
            if isinstance(v, dict) and isinstance(out["alpha"].get(k), dict):
                out["alpha"][k].update(v)
            else:
                out["alpha"][k] = v
    return out


def _score(metrics: Dict[str, Any]) -> float:
    ret = float(metrics.get("return_pct") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    dd = float(metrics.get("max_drawdown_pct") or 0.0)
    trades = float(metrics.get("trades") or 0.0)
    pf = float(metrics.get("profit_factor") or 0.0)
    s = 2.0 * ret + 12.0 * sharpe + 5.0 * max(0.0, pf - 1.0) + 0.03 * min(trades, 100.0) - 1.0 * dd
    if trades < 3:
        s -= 8.0
    if ret <= 0:
        s -= 5.0
    return s


def _sample_candidates(trials: int, seed: int) -> List[Candidate]:
    rng = random.Random(seed)
    space = {
        "trend_lookback": [2, 3, 4, 6, 8],
        "trend_threshold": [0.1, 0.25, 0.5, 1.0],
        "trend_scale": [4.0, 8.0, 12.0, 16.0],
        "reversion_lookback": [4, 6, 8, 12],
        "reversion_threshold": [0.5, 1.0, 1.5, 2.0],
        "reversion_scale": [1.0, 2.0, 3.0, 5.0],
        "breakout_lookback": [8, 12, 16, 24],
        "breakout_trigger": [4.0, 6.0, 8.0, 10.0],
        "breakout_scale": [2.0, 4.0, 6.0, 8.0],
        "regime_lookback": [4, 6, 8, 12],
        "trend_momentum": [4.0, 6.0, 8.0, 10.0],
        "range_momentum": [1.0, 2.0, 3.0, 4.0],
        "high_vol_atr": [80.0, 100.0, 120.0, 160.0],
        "low_vol_atr": [30.0, 40.0, 50.0, 60.0],
        "breakout_return": [5.0, 6.0, 8.0, 10.0],
    }
    out: List[Candidate] = []
    seen: set[tuple] = set()
    while len(out) < max(1, trials):
        c = Candidate(
            trend_lookback=rng.choice(space["trend_lookback"]),
            trend_threshold=rng.choice(space["trend_threshold"]),
            trend_scale=rng.choice(space["trend_scale"]),
            reversion_lookback=rng.choice(space["reversion_lookback"]),
            reversion_threshold=rng.choice(space["reversion_threshold"]),
            reversion_scale=rng.choice(space["reversion_scale"]),
            breakout_lookback=rng.choice(space["breakout_lookback"]),
            breakout_trigger=rng.choice(space["breakout_trigger"]),
            breakout_scale=rng.choice(space["breakout_scale"]),
            regime_lookback=rng.choice(space["regime_lookback"]),
            trend_momentum=rng.choice(space["trend_momentum"]),
            range_momentum=rng.choice(space["range_momentum"]),
            high_vol_atr=rng.choice(space["high_vol_atr"]),
            low_vol_atr=rng.choice(space["low_vol_atr"]),
            breakout_return=rng.choice(space["breakout_return"]),
        )
        key = tuple(c.__dict__.values())
        if key in seen:
            continue
        if c.low_vol_atr >= c.high_vol_atr:
            continue
        seen.add(key)
        out.append(c)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Minimal auto-alpha training (trend/reversion/breakout + regime switch).")
    p.add_argument("--config", default="configs/sim_auto.yaml")
    p.add_argument("--out-yaml", default="configs/alpha_auto_trained_minimal.yaml")
    p.add_argument("--out-json", default="reports/alpha_auto_train_minimal.json")
    p.add_argument("--max-events", type=int, default=6000)
    p.add_argument("--trials", type=int, default=40)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    cfg = load_config(str(args.config)).raw
    events = _load_events(cfg, int(args.max_events))
    if len(events) < 1200:
        raise SystemExit(f"Need >=1200 events for minimal training, got {len(events)}")

    holdout = max(400, int(len(events) * 0.2))
    train_events = events[:-holdout]
    holdout_events = events[-holdout:]

    candidates = _sample_candidates(trials=int(args.trials), seed=int(args.seed))
    reports: List[Dict[str, Any]] = []
    best = None
    best_score = -1e18
    for idx, cand in enumerate(candidates, start=1):
        ov = cand.overlay()
        c_cfg = _apply_overlay(cfg, ov)
        m = _run_metrics(c_cfg, train_events)
        s = _score(m)
        report = {
            "candidate_index": idx,
            "score": s,
            "trades": int(m.get("trades", 0) or 0),
            "return_pct": float(m.get("return_pct", 0.0) or 0.0),
            "max_drawdown_pct": float(m.get("max_drawdown_pct", 0.0) or 0.0),
            "params": ov,
        }
        reports.append(report)
        if s > best_score:
            best_score = s
            best = report
    if best is None:
        raise SystemExit("No candidate evaluated.")

    best_cfg = _apply_overlay(cfg, dict(best["params"]))
    holdout_metrics = _run_metrics(best_cfg, holdout_events)

    out_yaml = str(args.out_yaml)
    out_json = str(args.out_json)
    os.makedirs(os.path.dirname(out_yaml) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)

    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise SystemExit(f"PyYAML is required: {exc}")

    with open(out_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(best["params"], f, sort_keys=False)

    result = {
        "ok": True,
        "config": str(args.config),
        "events_total": len(events),
        "events_train": len(train_events),
        "events_holdout": len(holdout_events),
        "trials": len(candidates),
        "best": best,
        "holdout": {
            "return_pct": float(holdout_metrics.get("return_pct", 0.0) or 0.0),
            "sharpe": float(holdout_metrics.get("sharpe", 0.0) or 0.0),
            "max_drawdown_pct": float(holdout_metrics.get("max_drawdown_pct", 0.0) or 0.0),
            "profit_factor": float(holdout_metrics.get("profit_factor", 0.0) or 0.0),
            "trades": int(holdout_metrics.get("trades", 0) or 0),
        },
        "top": sorted(reports, key=lambda r: float(r["score"]), reverse=True)[:10],
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(
        json.dumps(
            {
                "ok": True,
                "out_yaml": out_yaml,
                "out_json": out_json,
                "holdout_return_pct": result["holdout"]["return_pct"],
                "holdout_trades": result["holdout"]["trades"],
                "trials": result["trials"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
