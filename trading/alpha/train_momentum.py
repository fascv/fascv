from __future__ import annotations

import argparse
import copy
import json
import os
from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple

from trading.alpha.training import make_walk_forward_splits
from trading.app import build_pipeline
from trading.config import load_config
from trading.data.backtest import BacktestCSVDataSource
from trading.data.base import MarketDataSource
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


class _ListDataSource(MarketDataSource):
    def __init__(self, events: List[MarketEvent]):
        self._events = list(events)

    def __iter__(self) -> Iterable[MarketEvent]:
        yield from self._events


def _parse_grid(raw: str, default: List[float]) -> List[float]:
    text = str(raw or "").strip()
    if not text:
        return list(default)
    out: List[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out or list(default)


def _run_metrics(cfg: Dict[str, Any], events: List[MarketEvent]) -> Dict[str, Any]:
    cfg = copy.deepcopy(cfg)
    # Training must not recursively apply an existing override file.
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
    result = pipeline.run_backtest()
    return dict(result.metrics)


@dataclass
class Candidate:
    lookback: int
    threshold_bps: float
    scale: float


def _score(metrics: Dict[str, Any]) -> Tuple[float, float, float]:
    # Higher is better. Tie-break: lower drawdown, then more trades.
    sharpe = float(metrics.get("sharpe") or 0.0)
    dd = float(metrics.get("max_drawdown_pct") or 0.0)
    trades = float(metrics.get("trades") or 0.0)
    return (sharpe, -dd, trades)


def _choose_best_on_window(
    base_cfg: Dict[str, Any],
    events: List[MarketEvent],
    candidates: List[Candidate],
) -> Tuple[Candidate, Dict[str, Any]]:
    best = candidates[0]
    best_metrics: Dict[str, Any] = {}
    best_score = (-1e18, -1e18, -1e18)
    for c in candidates:
        cfg = copy.deepcopy(base_cfg)
        cfg.setdefault("alpha", {})
        cfg["alpha"]["lookback"] = int(c.lookback)
        cfg["alpha"]["threshold_bps"] = float(c.threshold_bps)
        cfg["alpha"]["scale"] = float(c.scale)
        m = _run_metrics(cfg, events)
        s = _score(m)
        if s > best_score:
            best_score = s
            best = c
            best_metrics = m
    return best, best_metrics


def train_walk_forward_momentum(
    cfg: Dict[str, Any],
    events: List[MarketEvent],
    *,
    train_size: int,
    val_size: int,
    test_size: int,
    purge: int,
    threshold_grid: List[float],
    scale_grid: List[float],
    lookback_grid: List[int],
    top_k_train: int,
) -> Dict[str, Any]:
    n = len(events)
    splits = make_walk_forward_splits(
        n=n,
        train_size=train_size,
        val_size=val_size,
        test_size=test_size,
        purge=purge,
    )
    if not splits:
        raise ValueError("No walk-forward splits possible with given sizes.")

    candidates: List[Candidate] = []
    for lb in lookback_grid:
        for th in threshold_grid:
            for sc in scale_grid:
                candidates.append(Candidate(lookback=int(lb), threshold_bps=float(th), scale=float(sc)))
    if not candidates:
        raise ValueError("Empty candidate grid.")

    split_reports: List[Dict[str, Any]] = []
    chosen: List[Candidate] = []

    for i, sp in enumerate(splits):
        tr0, tr1 = sp.train_idx
        va0, va1 = sp.val_idx
        te0, te1 = sp.test_idx
        train_events = events[tr0:tr1]
        val_events = events[va0:va1]
        test_events = events[te0:te1]

        # 1) Train window selection (grid search by Sharpe).
        train_scores: List[Tuple[Tuple[float, float, float], Candidate, Dict[str, Any]]] = []
        for c in candidates:
            cfg_c = copy.deepcopy(cfg)
            cfg_c.setdefault("alpha", {})
            cfg_c["alpha"]["lookback"] = int(c.lookback)
            cfg_c["alpha"]["threshold_bps"] = float(c.threshold_bps)
            cfg_c["alpha"]["scale"] = float(c.scale)
            m = _run_metrics(cfg_c, train_events)
            train_scores.append((_score(m), c, m))
        train_scores.sort(key=lambda t: t[0], reverse=True)
        top = train_scores[: max(1, int(top_k_train))]

        # 2) Validate on val window among top-k.
        top_candidates = [c for _, c, _ in top]
        best_val, best_val_metrics = _choose_best_on_window(cfg, val_events, top_candidates)

        # 3) Test window report with the chosen params.
        cfg_best = copy.deepcopy(cfg)
        cfg_best.setdefault("alpha", {})
        cfg_best["alpha"]["lookback"] = int(best_val.lookback)
        cfg_best["alpha"]["threshold_bps"] = float(best_val.threshold_bps)
        cfg_best["alpha"]["scale"] = float(best_val.scale)
        test_metrics = _run_metrics(cfg_best, test_events)

        chosen.append(best_val)
        split_reports.append(
            {
                "split": i,
                "idx": {"train": [tr0, tr1], "val": [va0, va1], "test": [te0, te1]},
                "best": {
                    "lookback": best_val.lookback,
                    "threshold_bps": best_val.threshold_bps,
                    "scale": best_val.scale,
                },
                "val_metrics": best_val_metrics,
                "test_metrics": test_metrics,
                "top_train": [
                    {
                        "lookback": c.lookback,
                        "threshold_bps": c.threshold_bps,
                        "scale": c.scale,
                        "train_metrics": m,
                    }
                    for _, c, m in top
                ],
            }
        )

    # Aggregate choice across splits (robust to outliers).
    best = {
        "lookback": int(round(median([c.lookback for c in chosen]))),
        "threshold_bps": float(median([c.threshold_bps for c in chosen])),
        "scale": float(median([c.scale for c in chosen])),
    }

    return {
        "n_events": n,
        "window": {
            "train_size": train_size,
            "val_size": val_size,
            "test_size": test_size,
            "purge": purge,
            "splits": len(splits),
        },
        "grid": {
            "threshold_bps": threshold_grid,
            "scale": scale_grid,
            "lookback": lookback_grid,
            "top_k_train": top_k_train,
        },
        "best_aggregate": best,
        "splits": split_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward training for MomentumAlpha (grid search).")
    parser.add_argument("--config", default="configs/paper.yaml")
    parser.add_argument("--out-yaml", default="configs/alpha_trained.yaml")
    parser.add_argument("--out-json", default="reports/alpha_train_report.json")
    parser.add_argument("--max-events", type=int, default=0, help="Cap events to keep training bounded (0 disables)")

    parser.add_argument("--train-size", type=int, default=0)
    parser.add_argument("--val-size", type=int, default=0)
    parser.add_argument("--test-size", type=int, default=0)
    parser.add_argument("--purge", type=int, default=0)

    parser.add_argument("--threshold-grid", default="0.5,1,1.5,2,3,4,5,6,8,10")
    parser.add_argument("--scale-grid", default="0.5,1,2,3,5")
    parser.add_argument("--lookback-grid", default="1,2,3,4,5")
    parser.add_argument("--top-k-train", type=int, default=6)
    args = parser.parse_args()

    cfg = load_config(str(args.config)).raw
    default_micro = _cfg(cfg, "data.default_micro", {})
    data_source = str(_cfg(cfg, "data.source", "csv") or "csv").strip().lower()
    if data_source in {"sqlite", "db"}:
        db_path = str(_cfg(cfg, "data.db_path", "data/market.db"))
        symbol = str(_cfg(cfg, "data.symbol", _cfg(cfg, "md.pair", "XBT/EUR")))
        timeframe = str(_cfg(cfg, "data.timeframe", "5m"))
        start = _cfg(cfg, "data.start", None)
        end = _cfg(cfg, "data.end", None)
        max_events = int(args.max_events or 0)
        events = list(
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
    else:
        data_path = _cfg(cfg, "data.path", "")
        if not data_path:
            raise SystemExit("config.data.path is required for training (or set data.source=sqlite + data.db_path)")
        events = list(BacktestCSVDataSource(path=data_path, default_micro=default_micro))
        max_events = int(args.max_events or 0)
        if max_events > 0 and len(events) > max_events:
            events = events[-max_events:]
    n = len(events)
    if n < 200:
        raise SystemExit(f"need at least ~200 events for walk-forward training, got {n}")

    # If window sizes are not set, derive something reasonable from n.
    train_size = int(args.train_size) if int(args.train_size) > 0 else max(100, int(n * 0.5))
    val_size = int(args.val_size) if int(args.val_size) > 0 else max(50, int(n * 0.2))
    test_size = int(args.test_size) if int(args.test_size) > 0 else max(50, int(n * 0.2))
    purge = int(args.purge) if int(args.purge) > 0 else max(1, int(n * 0.01))

    threshold_grid = _parse_grid(str(args.threshold_grid), [2.0])
    scale_grid = _parse_grid(str(args.scale_grid), [1.0])
    lookback_grid = [int(x) for x in _parse_grid(str(args.lookback_grid), [3.0])]

    report = train_walk_forward_momentum(
        cfg=cfg,
        events=events,
        train_size=train_size,
        val_size=val_size,
        test_size=test_size,
        purge=purge,
        threshold_grid=threshold_grid,
        scale_grid=scale_grid,
        lookback_grid=lookback_grid,
        top_k_train=int(args.top_k_train),
    )

    out_yaml = str(args.out_yaml)
    out_json = str(args.out_json)
    os.makedirs(os.path.dirname(out_yaml) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)

    # Write YAML override to be consumed by core/app via alpha.override_path.
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise SystemExit(f"PyYAML required to write yaml: {exc}")

    best = report["best_aggregate"]
    overlay = {"alpha": {"lookback": best["lookback"], "threshold_bps": best["threshold_bps"], "scale": best["scale"]}}
    with open(out_yaml, "w", encoding="utf-8") as f:
        yaml.safe_dump(overlay, f, sort_keys=False)

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(json.dumps({"ok": True, "out_yaml": out_yaml, "out_json": out_json, "best": best}, indent=2))


if __name__ == "__main__":
    main()
