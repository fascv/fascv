#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import concurrent.futures
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing import get_context
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional

# Ensure repo root is importable when script is executed as a file.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from trading.alpha.training import make_walk_forward_splits
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


def _parse_ts_utc(raw: str) -> datetime:
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) >= 5 and (text[-5] in {"+", "-"}) and text[-3] != ":":
        # e.g. +0000 -> +00:00
        text = text[:-2] + ":" + text[-2:]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_events_from_sqlite_ts_utc(
    db_path: str,
    *,
    symbol: str,
    timeframe: str,
    max_events: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> List[MarketEvent]:
    # This loader is compatible with the schema used in /codex/geld/trading.db:
    # candles(symbol,timeframe,ts_utc,open,high,low,close,volume,...)
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        if int(max_events) > 0:
            rows = cur.execute(
                """
                SELECT ts_utc, open, high, low, close, volume
                FROM candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY ts_utc DESC
                LIMIT ?
                """,
                (str(symbol), str(timeframe), int(max_events)),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = cur.execute(
                """
                SELECT ts_utc, open, high, low, close, volume
                FROM candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY ts_utc ASC
                """,
                (str(symbol), str(timeframe)),
            ).fetchall()
    finally:
        con.close()

    events: List[MarketEvent] = []
    for ts_utc, o, h, l, c, v in rows:
        try:
            ts = _parse_ts_utc(str(ts_utc))
            if start is not None and ts < start:
                continue
            if end is not None and ts >= end:
                continue
            events.append(
                MarketEvent(
                    ts=ts,
                    open=float(o),
                    high=float(h),
                    low=float(l),
                    close=float(c),
                    volume=float(v),
                    micro={},
                )
            )
        except Exception:
            continue
    return events


def _load_events(
    cfg: Dict[str, Any],
    *,
    max_events: int,
    external_db_path: str | None = None,
    external_symbol: str | None = None,
    external_timeframe: str | None = None,
    external_start: str | None = None,
    external_end: str | None = None,
) -> List[MarketEvent]:
    default_micro = _cfg(cfg, "data.default_micro", {})
    if external_db_path:
        symbol = str(
            (external_symbol or "").strip()
            or _cfg(cfg, "data.symbol", _cfg(cfg, "md.pair", "BTC/EUR"))
        )
        timeframe = str(
            (external_timeframe or "").strip()
            or _cfg(cfg, "data.timeframe", "5m")
        )
        start_raw = external_start if external_start else _cfg(cfg, "data.start", None)
        end_raw = external_end if external_end else _cfg(cfg, "data.end", None)
        start_dt = _parse_ts_utc(str(start_raw)) if start_raw else None
        end_dt = _parse_ts_utc(str(end_raw)) if end_raw else None
        events = _load_events_from_sqlite_ts_utc(
            external_db_path,
            symbol=symbol,
            timeframe=timeframe,
            max_events=max_events,
            start=start_dt,
            end=end_dt,
        )
        if default_micro:
            for e in events:
                if not e.micro:
                    e.micro = dict(default_micro)
        return events

    source = str(_cfg(cfg, "data.source", "csv") or "csv").strip().lower()
    if source in {"sqlite", "db"}:
        db_path = str(_cfg(cfg, "data.db_path", "data/market.db"))
        symbol = str(_cfg(cfg, "data.symbol", _cfg(cfg, "md.pair", "BTC/EUR")))
        timeframe = str(_cfg(cfg, "data.timeframe", "5m"))
        start = _cfg(cfg, "data.start", None)
        end = _cfg(cfg, "data.end", None)
        return list(
            SQLiteOHLCVDataSource(
                db_path=db_path,
                symbol=symbol,
                timeframe=timeframe,
                default_micro=default_micro,
                start=str(start) if start else None,
                end=str(end) if end else None,
                limit=max_events if max_events > 0 else None,
                latest=True if max_events > 0 else False,
            )
        )

    data_path = str(_cfg(cfg, "data.path", "") or "")
    events = list(BacktestCSVDataSource(path=data_path, default_micro=default_micro))
    if max_events > 0 and len(events) > max_events:
        events = events[-max_events:]
    return events


def _run_metrics(cfg: Dict[str, Any], events: List[MarketEvent]) -> Dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    if isinstance(cfg.get("alpha"), dict):
        # Avoid recursive overlays while optimizing.
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
    result = pipeline.run_backtest()
    metrics = dict(result.metrics)
    eq = result.equity_curve or []
    if eq:
        start_eq = float(eq[0].get("equity", 0.0) or 0.0)
        end_eq = float(eq[-1].get("equity", 0.0) or 0.0)
    else:
        start_eq = 0.0
        end_eq = 0.0
    ret_pct = ((end_eq / start_eq - 1.0) * 100.0) if start_eq > 0 else 0.0
    metrics["start_equity_eur"] = start_eq
    metrics["end_equity_eur"] = end_eq
    metrics["return_pct"] = ret_pct
    return metrics


class _ListDataSource:
    def __init__(self, events: List[MarketEvent]):
        self._events = list(events)

    def __iter__(self) -> Iterable[MarketEvent]:
        yield from self._events


@dataclass(frozen=True)
class Candidate:
    alpha_lookback: int
    alpha_threshold_bps: float
    alpha_scale: float
    gate_safety_margin_bps: float
    gate_max_spread_bps: float
    gate_max_atr_bps: float
    risk_cooldown_bars: int
    risk_vol_target_bps: float
    risk_use_vol_scaling: bool
    risk_use_gate_size_factor: bool
    risk_entry_edge_bps: float
    risk_exit_edge_bps: float
    risk_min_hold_bars: int
    risk_require_break_even_for_exit: bool
    risk_min_exit_profit_bps: float

    def apply(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(cfg)
        out.setdefault("alpha", {})
        out.setdefault("gate", {})
        out.setdefault("risk", {})
        out["alpha"]["lookback"] = int(self.alpha_lookback)
        out["alpha"]["threshold_bps"] = float(self.alpha_threshold_bps)
        out["alpha"]["scale"] = float(self.alpha_scale)
        out["gate"]["safety_margin_bps"] = float(self.gate_safety_margin_bps)
        out["gate"]["max_spread_bps"] = float(self.gate_max_spread_bps)
        out["gate"]["max_atr_bps"] = float(self.gate_max_atr_bps)
        out["risk"]["cooldown_bars"] = int(self.risk_cooldown_bars)
        out["risk"]["vol_target_bps"] = float(self.risk_vol_target_bps)
        out["risk"]["use_vol_scaling"] = bool(self.risk_use_vol_scaling)
        out["risk"]["use_gate_size_factor"] = bool(self.risk_use_gate_size_factor)
        out["risk"]["entry_edge_bps"] = float(self.risk_entry_edge_bps)
        out["risk"]["exit_edge_bps"] = float(self.risk_exit_edge_bps)
        out["risk"]["min_hold_bars"] = int(self.risk_min_hold_bars)
        out["risk"]["require_break_even_for_exit"] = bool(self.risk_require_break_even_for_exit)
        out["risk"]["min_exit_profit_bps"] = float(self.risk_min_exit_profit_bps)
        return out

    def as_overlay(self) -> Dict[str, Any]:
        return {
            "alpha": {
                "lookback": int(self.alpha_lookback),
                "threshold_bps": float(self.alpha_threshold_bps),
                "scale": float(self.alpha_scale),
            },
            "gate": {
                "safety_margin_bps": float(self.gate_safety_margin_bps),
                "max_spread_bps": float(self.gate_max_spread_bps),
                "max_atr_bps": float(self.gate_max_atr_bps),
            },
            "risk": {
                "cooldown_bars": int(self.risk_cooldown_bars),
                "vol_target_bps": float(self.risk_vol_target_bps),
                "use_vol_scaling": bool(self.risk_use_vol_scaling),
                "use_gate_size_factor": bool(self.risk_use_gate_size_factor),
                "entry_edge_bps": float(self.risk_entry_edge_bps),
                "exit_edge_bps": float(self.risk_exit_edge_bps),
                "min_hold_bars": int(self.risk_min_hold_bars),
                "require_break_even_for_exit": bool(self.risk_require_break_even_for_exit),
                "min_exit_profit_bps": float(self.risk_min_exit_profit_bps),
            },
        }


_PAR_CFG: Dict[str, Any] | None = None
_PAR_EVENTS: List[MarketEvent] | None = None
_PAR_SPLITS: Any = None
_PAR_MIN_OOS_TRADES: int = 0
_PAR_ENFORCE_POSITIVE_OOS: bool = False


def _parallel_init(
    cfg: Dict[str, Any],
    events: List[MarketEvent],
    split_candidates: Any,
    min_oos_trades: int,
    enforce_positive_oos: bool,
) -> None:
    global _PAR_CFG, _PAR_EVENTS, _PAR_SPLITS, _PAR_MIN_OOS_TRADES, _PAR_ENFORCE_POSITIVE_OOS
    _PAR_CFG = cfg
    _PAR_EVENTS = events
    _PAR_SPLITS = split_candidates
    _PAR_MIN_OOS_TRADES = int(min_oos_trades)
    _PAR_ENFORCE_POSITIVE_OOS = bool(enforce_positive_oos)


def _evaluate_candidate_task(task: tuple[int, Candidate]) -> Dict[str, Any]:
    idx, cand = task
    if _PAR_CFG is None or _PAR_EVENTS is None or _PAR_SPLITS is None:
        raise RuntimeError("parallel worker not initialized")

    cfg_c = cand.apply(_PAR_CFG)
    split_scores: List[float] = []
    split_metrics: List[Dict[str, Any]] = []
    for sp in _PAR_SPLITS:
        te0, te1 = sp.test_idx
        test_events = _PAR_EVENTS[te0:te1]
        m = _run_metrics(cfg_c, test_events)
        split_metrics.append(
            {
                "test_idx": [te0, te1],
                "return_pct": float(m.get("return_pct", 0.0)),
                "sharpe": float(m.get("sharpe", 0.0)),
                "max_drawdown_pct": float(m.get("max_drawdown_pct", 0.0)),
                "profit_factor": float(m.get("profit_factor", 0.0)),
                "trades": int(m.get("trades", 0) or 0),
            }
        )
        split_scores.append(
            _score(
                m,
                min_trades=_PAR_MIN_OOS_TRADES,
                enforce_positive=_PAR_ENFORCE_POSITIVE_OOS,
            )
        )

    robust_score = 0.6 * median(split_scores) + 0.4 * mean(split_scores)
    oos_return_mean = mean([s["return_pct"] for s in split_metrics]) if split_metrics else 0.0
    oos_dd_mean = mean([s["max_drawdown_pct"] for s in split_metrics]) if split_metrics else 0.0
    oos_trades_mean = mean([s["trades"] for s in split_metrics]) if split_metrics else 0.0

    return {
        "candidate_index": idx,
        "params": cand.as_overlay(),
        "robust_score": robust_score,
        "oos_return_mean_pct": oos_return_mean,
        "oos_drawdown_mean_pct": oos_dd_mean,
        "oos_trades_mean": oos_trades_mean,
        "splits": split_metrics,
    }


def _score(metrics: Dict[str, Any], *, min_trades: int = 0, enforce_positive: bool = False) -> float:
    # Cost-aware robust objective: prioritize positive return, controlled drawdown,
    # and stable risk-adjusted performance. Activity is only a floor check.
    ret = float(metrics.get("return_pct") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
    trades = float(metrics.get("trades") or 0.0)
    profit_factor = float(metrics.get("profit_factor") or 0.0)

    score = (
        5.0 * ret
        + 14.0 * sharpe
        + 35.0 * max(min(profit_factor - 1.0, 2.0), -1.0)
        - 2.5 * drawdown
    )

    if ret <= 0:
        score -= 100.0 + 6.0 * abs(ret)
    if sharpe <= 0:
        score -= 25.0 + 4.0 * abs(sharpe)
    if profit_factor < 1.0:
        score -= 40.0 * (1.0 - profit_factor)
    if drawdown > 8.0:
        score -= 3.0 * (drawdown - 8.0)

    if trades < 5:
        score -= 20.0
    if int(min_trades) > 0 and trades < float(min_trades):
        score -= 1000.0 + 50.0 * float(min_trades - trades)
    if enforce_positive and ret <= 0:
        score -= 3000.0
    return score


def _sample_candidates(trials: int, seed: int) -> List[Candidate]:
    rng = random.Random(seed)
    space = {
        "alpha_lookback": [2, 3, 4, 5, 6, 8, 10, 12, 16, 24],
        "alpha_threshold_bps": [0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0],
        "alpha_scale": [0.5, 1.0, 2.0, 3.0, 5.0, 8.0],
        "gate_safety_margin_bps": [0.5, 1.0, 1.5, 2.0, 3.0],
        "gate_max_spread_bps": [8.0, 12.0, 16.0, 20.0],
        "gate_max_atr_bps": [80.0, 120.0, 160.0, 220.0, 300.0],
        "risk_cooldown_bars": [1, 2, 3, 4, 6, 8, 12],
        "risk_vol_target_bps": [70.0, 90.0, 110.0, 130.0],
        "risk_use_vol_scaling": [False, True],
        "risk_use_gate_size_factor": [False, True],
        "risk_entry_edge_bps": [0.0, 4.0, 8.0, 12.0, 20.0, 30.0, 40.0, 60.0],
        "risk_exit_edge_bps": [-2.0, 0.0, 2.0, 4.0, 8.0, 12.0, 20.0],
        "risk_min_hold_bars": [0, 1, 2, 3, 4, 6],
        "risk_require_break_even_for_exit": [False, True],
        "risk_min_exit_profit_bps": [0.0, 5.0, 10.0, 20.0, 30.0],
    }

    out: List[Candidate] = []
    seen: set[tuple] = set()
    while len(out) < max(1, int(trials)):
        cand = Candidate(
            alpha_lookback=rng.choice(space["alpha_lookback"]),
            alpha_threshold_bps=rng.choice(space["alpha_threshold_bps"]),
            alpha_scale=rng.choice(space["alpha_scale"]),
            gate_safety_margin_bps=rng.choice(space["gate_safety_margin_bps"]),
            gate_max_spread_bps=rng.choice(space["gate_max_spread_bps"]),
            gate_max_atr_bps=rng.choice(space["gate_max_atr_bps"]),
            risk_cooldown_bars=rng.choice(space["risk_cooldown_bars"]),
            risk_vol_target_bps=rng.choice(space["risk_vol_target_bps"]),
            risk_use_vol_scaling=rng.choice(space["risk_use_vol_scaling"]),
            risk_use_gate_size_factor=rng.choice(space["risk_use_gate_size_factor"]),
            risk_entry_edge_bps=rng.choice(space["risk_entry_edge_bps"]),
            risk_exit_edge_bps=rng.choice(space["risk_exit_edge_bps"]),
            risk_min_hold_bars=rng.choice(space["risk_min_hold_bars"]),
            risk_require_break_even_for_exit=rng.choice(space["risk_require_break_even_for_exit"]),
            risk_min_exit_profit_bps=rng.choice(space["risk_min_exit_profit_bps"]),
        )
        key = (
            cand.alpha_lookback,
            cand.alpha_threshold_bps,
            cand.alpha_scale,
            cand.gate_safety_margin_bps,
            cand.gate_max_spread_bps,
            cand.gate_max_atr_bps,
            cand.risk_cooldown_bars,
            cand.risk_vol_target_bps,
            cand.risk_use_vol_scaling,
            cand.risk_use_gate_size_factor,
            cand.risk_entry_edge_bps,
            cand.risk_exit_edge_bps,
            cand.risk_min_hold_bars,
            cand.risk_require_break_even_for_exit,
            cand.risk_min_exit_profit_bps,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def optimize(
    cfg: Dict[str, Any],
    events: List[MarketEvent],
    *,
    trials: int,
    seed: int,
    jobs: int,
    train_size: int,
    val_size: int,
    test_size: int,
    purge: int,
    min_oos_trades: int,
    enforce_positive_oos: bool,
) -> Dict[str, Any]:
    if len(events) < (train_size + val_size + test_size + purge):
        raise ValueError("Not enough events for requested walk-forward sizes.")

    split_candidates = make_walk_forward_splits(
        n=len(events),
        train_size=train_size,
        val_size=val_size,
        test_size=test_size,
        purge=purge,
    )
    if not split_candidates:
        raise ValueError("No walk-forward splits possible with current parameters.")

    candidates = _sample_candidates(trials=trials, seed=seed)
    reports: List[Dict[str, Any]] = []
    best: Dict[str, Any] | None = None

    jobs = max(1, int(jobs))
    tasks = list(enumerate(candidates, start=1))
    if jobs == 1:
        for task in tasks:
            # Keep the single-process path fully local without global worker state.
            idx, cand = task
            cfg_c = cand.apply(cfg)
            split_scores: List[float] = []
            split_metrics: List[Dict[str, Any]] = []
            for sp in split_candidates:
                te0, te1 = sp.test_idx
                test_events = events[te0:te1]
                m = _run_metrics(cfg_c, test_events)
                split_metrics.append(
                    {
                        "test_idx": [te0, te1],
                        "return_pct": float(m.get("return_pct", 0.0)),
                        "sharpe": float(m.get("sharpe", 0.0)),
                        "max_drawdown_pct": float(m.get("max_drawdown_pct", 0.0)),
                        "profit_factor": float(m.get("profit_factor", 0.0)),
                        "trades": int(m.get("trades", 0) or 0),
                    }
                )
                split_scores.append(
                    _score(
                        m,
                        min_trades=int(min_oos_trades),
                        enforce_positive=bool(enforce_positive_oos),
                    )
                )

            robust_score = 0.6 * median(split_scores) + 0.4 * mean(split_scores)
            oos_return_mean = mean([s["return_pct"] for s in split_metrics]) if split_metrics else 0.0
            oos_dd_mean = mean([s["max_drawdown_pct"] for s in split_metrics]) if split_metrics else 0.0
            oos_trades_mean = mean([s["trades"] for s in split_metrics]) if split_metrics else 0.0
            report = {
                "candidate_index": idx,
                "params": cand.as_overlay(),
                "robust_score": robust_score,
                "oos_return_mean_pct": oos_return_mean,
                "oos_drawdown_mean_pct": oos_dd_mean,
                "oos_trades_mean": oos_trades_mean,
                "splits": split_metrics,
            }
            reports.append(report)
            if best is None or float(report["robust_score"]) > float(best["robust_score"]):
                best = report
    else:
        mp_ctx = get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=jobs,
            mp_context=mp_ctx,
            initializer=_parallel_init,
            initargs=(cfg, events, split_candidates, int(min_oos_trades), bool(enforce_positive_oos)),
        ) as ex:
            futures = [ex.submit(_evaluate_candidate_task, task) for task in tasks]
            for fut in concurrent.futures.as_completed(futures):
                report = fut.result()
                reports.append(report)
                if best is None or float(report["robust_score"]) > float(best["robust_score"]):
                    best = report

    if best is None:
        raise RuntimeError("No optimization report produced.")

    reports.sort(key=lambda r: float(r.get("robust_score", -1e18)), reverse=True)
    return {"best": best, "top": reports[:10], "splits": len(split_candidates), "trials": len(candidates)}


def _derive_sizes(n: int) -> tuple[int, int, int, int]:
    # Keep this stable and conservative for robustness.
    train = max(1200, int(n * 0.5))
    val = max(600, int(n * 0.2))
    test = max(600, int(n * 0.2))
    purge = max(1, int(n * 0.01))
    return train, val, test, purge


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Optimize core simulation settings (alpha/gate/risk) with walk-forward OOS scoring "
            "and drawdown-aware objective."
        )
    )
    p.add_argument("--config", default="configs/sim.yaml")
    p.add_argument("--out-yaml", default="configs/sim_optimized.yaml")
    p.add_argument("--out-json", default="reports/sim_opt_report.json")
    p.add_argument("--db-path", default="", help="Optional external sqlite db with candles(ts_utc...) schema.")
    p.add_argument("--symbol", default="", help="Override symbol when using --db-path (e.g. XBT/EUR).")
    p.add_argument("--timeframe", default="", help="Override timeframe when using --db-path (e.g. 5m).")
    p.add_argument("--start", default="", help="Optional inclusive start timestamp override for external db.")
    p.add_argument("--end", default="", help="Optional exclusive end timestamp override for external db.")
    p.add_argument("--max-events", type=int, default=30000)
    p.add_argument("--trials", type=int, default=80)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Parallel worker processes for candidate evaluation (default: half logical CPUs).",
    )
    p.add_argument("--train-size", type=int, default=0)
    p.add_argument("--val-size", type=int, default=0)
    p.add_argument("--test-size", type=int, default=0)
    p.add_argument("--purge", type=int, default=0)
    p.add_argument("--min-oos-trades", type=int, default=0)
    p.add_argument("--enforce-positive-oos", action="store_true")
    args = p.parse_args()

    cfg = load_config(str(args.config)).raw
    events = _load_events(
        cfg,
        max_events=max(0, int(args.max_events)),
        external_db_path=str(args.db_path).strip() or None,
        external_symbol=str(args.symbol).strip() or None,
        external_timeframe=str(args.timeframe).strip() or None,
        external_start=str(args.start).strip() or None,
        external_end=str(args.end).strip() or None,
    )
    n = len(events)
    if n < 3000:
        raise SystemExit(f"need at least ~3000 events for robust full-parameter optimization, got {n}")

    holdout = max(1200, int(n * 0.2))
    search_events = events[:-holdout]
    holdout_events = events[-holdout:]
    n_search = len(search_events)

    if int(args.train_size) > 0 and int(args.val_size) > 0 and int(args.test_size) > 0:
        train_size = int(args.train_size)
        val_size = int(args.val_size)
        test_size = int(args.test_size)
        purge = int(args.purge) if int(args.purge) > 0 else max(1, int(n_search * 0.01))
    else:
        train_size, val_size, test_size, purge = _derive_sizes(n_search)

    summary = optimize(
        cfg=cfg,
        events=search_events,
        trials=int(args.trials),
        seed=int(args.seed),
        jobs=int(args.jobs),
        train_size=train_size,
        val_size=val_size,
        test_size=test_size,
        purge=purge,
        min_oos_trades=int(args.min_oos_trades),
        enforce_positive_oos=bool(args.enforce_positive_oos),
    )
    best_overlay = dict(summary["best"]["params"])

    # Holdout check on untouched tail section.
    holdout_cfg = copy.deepcopy(cfg)
    if isinstance(holdout_cfg.get("alpha"), dict):
        holdout_cfg["alpha"].pop("override_path", None)
    for section, values in best_overlay.items():
        holdout_cfg.setdefault(section, {})
        if isinstance(values, dict):
            holdout_cfg[section].update(values)
    holdout_metrics = _run_metrics(holdout_cfg, holdout_events)

    report = {
        "ok": True,
        "config": str(args.config),
        "events_total": n,
        "events_search": n_search,
        "events_holdout": holdout,
        "window": {
            "train_size": train_size,
            "val_size": val_size,
            "test_size": test_size,
            "purge": purge,
            "splits": int(summary["splits"]),
        },
        "trials": int(summary["trials"]),
        "best": summary["best"],
        "top": summary["top"],
        "holdout": {
            "return_pct": float(holdout_metrics.get("return_pct", 0.0)),
            "sharpe": float(holdout_metrics.get("sharpe", 0.0)),
            "max_drawdown_pct": float(holdout_metrics.get("max_drawdown_pct", 0.0)),
            "profit_factor": float(holdout_metrics.get("profit_factor", 0.0)),
            "trades": int(holdout_metrics.get("trades", 0) or 0),
            "start_equity_eur": float(holdout_metrics.get("start_equity_eur", 0.0)),
            "end_equity_eur": float(holdout_metrics.get("end_equity_eur", 0.0)),
        },
    }

    out_yaml = str(args.out_yaml)
    out_json = str(args.out_json)
    os.makedirs(os.path.dirname(out_yaml) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)

    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise SystemExit(f"PyYAML is required: {exc}")

    with open(out_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(best_overlay, f, sort_keys=False)

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(
        json.dumps(
            {
                "ok": True,
                "out_yaml": out_yaml,
                "out_json": out_json,
                "holdout_return_pct": report["holdout"]["return_pct"],
                "holdout_max_drawdown_pct": report["holdout"]["max_drawdown_pct"],
                "trials": report["trials"],
                "splits": report["window"]["splits"],
                "jobs": int(args.jobs),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
