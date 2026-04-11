#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.optimize_sim_all import _load_events, _run_metrics
from trading.config import load_config
from trading.config_overlay import deep_merge


def _parse_days_list(raw: str) -> List[int]:
    out: List[int] = []
    for x in str(raw or "").split(","):
        x = x.strip()
        if not x:
            continue
        out.append(max(1, int(x)))
    if not out:
        out = [60, 120, 240]
    return out


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:
        raise SystemExit(f"PyYAML required: {exc}")
    with open(path, "r", encoding="utf-8") as f:
        obj = yaml.safe_load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"overlay is not a mapping: {path}")
    return obj


def _score(m: Dict[str, Any]) -> float:
    ret = float(m.get("return_pct", 0.0) or 0.0)
    sharpe = float(m.get("sharpe", 0.0) or 0.0)
    dd = float(m.get("max_drawdown_pct", 0.0) or 0.0)
    trades = float(m.get("trades", 0.0) or 0.0)
    score = ret + 0.5 * sharpe - 0.2 * dd
    if trades < 6:
        score -= 0.5 * float(6.0 - trades)
    return score


def _subset_window(events: List[Any], start: datetime, end: datetime) -> List[Any]:
    out: List[Any] = []
    for e in events:
        ts = getattr(e, "ts", None)
        if ts is None:
            continue
        if ts >= start and ts < end:
            out.append(e)
    return out


def _common_overlay_from_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("gate", "risk", "order", "cost", "execution"):
        val = cfg.get(key)
        if isinstance(val, dict):
            out[key] = copy.deepcopy(val)
    return out


def _build_cfg(base_cfg: Dict[str, Any], common_overlay: Dict[str, Any], champion_overlay: Dict[str, Any]) -> Dict[str, Any]:
    alpha = champion_overlay.get("alpha")
    if not isinstance(alpha, dict):
        raise ValueError("champion overlay must contain alpha mapping")
    cfg = deep_merge(copy.deepcopy(base_cfg), common_overlay)
    cfg = deep_merge(cfg, {"alpha": alpha})
    if isinstance(cfg.get("alpha"), dict):
        cfg["alpha"].pop("override_path", None)
    return cfg


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Forward-style head-to-head comparison of two champion overlays under identical "
            "risk/gate/order/cost/execution settings."
        )
    )
    p.add_argument("--config", default="configs/sim_auto.yaml")
    p.add_argument("--common-config", default="configs/sim_auto.yaml")
    p.add_argument("--champion-a", default="configs/sim_optimized_professional_governed.yaml")
    p.add_argument("--champion-b", default="configs/sim_optimized_professional_governed_breakout_swing.yaml")
    p.add_argument("--name-a", default="swing_champion")
    p.add_argument("--name-b", default="breakout_swing_champion")
    p.add_argument("--db-path", default="data/market_robust.db")
    p.add_argument("--symbol", default="XBT/EUR")
    p.add_argument("--timeframe", default="30m")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="")
    p.add_argument("--windows-days", default="60,120,240")
    p.add_argument("--min-events-per-window", type=int, default=500)
    p.add_argument("--out-json", default="reports/champion_forward_compare_30m.json")
    args = p.parse_args()

    root = Path(REPO_ROOT)
    out_json = (root / str(args.out_json)).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)

    base_cfg = load_config(str(args.config)).raw
    common_cfg = load_config(str(args.common_config)).raw
    common_overlay = _common_overlay_from_cfg(common_cfg)

    overlay_a = _load_yaml((root / str(args.champion_a)).resolve())
    overlay_b = _load_yaml((root / str(args.champion_b)).resolve())

    cfg_a = _build_cfg(base_cfg, common_overlay, overlay_a)
    cfg_b = _build_cfg(base_cfg, common_overlay, overlay_b)

    events = _load_events(
        base_cfg,
        max_events=0,
        external_db_path=str((root / str(args.db_path)).resolve()),
        external_symbol=str(args.symbol),
        external_timeframe=str(args.timeframe),
        external_start=str(args.start).strip() or None,
        external_end=str(args.end).strip() or None,
    )
    if len(events) < int(args.min_events_per_window):
        raise SystemExit(f"not enough events: {len(events)}")

    events = sorted(events, key=lambda e: getattr(e, "ts"))
    ts_min = getattr(events[0], "ts")
    ts_max = getattr(events[-1], "ts")
    end_ts = ts_max + timedelta(seconds=1)

    windows = _parse_days_list(args.windows_days)
    results: List[Dict[str, Any]] = []
    wins = {str(args.name_a): 0, str(args.name_b): 0}

    for d in windows:
        start_ts = end_ts - timedelta(days=int(d))
        ev = _subset_window(events, start=start_ts, end=end_ts)
        if len(ev) < int(args.min_events_per_window):
            results.append(
                {
                    "window_days": int(d),
                    "status": "skipped",
                    "reason": "too_few_events",
                    "events": int(len(ev)),
                }
            )
            continue

        m_a = _run_metrics(cfg_a, ev)
        m_b = _run_metrics(cfg_b, ev)
        score_a = _score(m_a)
        score_b = _score(m_b)
        winner = str(args.name_a) if score_a >= score_b else str(args.name_b)
        wins[winner] += 1

        results.append(
            {
                "window_days": int(d),
                "status": "ok",
                "events": int(len(ev)),
                "start_ts": start_ts.isoformat(),
                "end_ts": (end_ts - timedelta(seconds=1)).isoformat(),
                str(args.name_a): {
                    "return_pct": float(m_a.get("return_pct", 0.0)),
                    "sharpe": float(m_a.get("sharpe", 0.0)),
                    "max_drawdown_pct": float(m_a.get("max_drawdown_pct", 0.0)),
                    "trades": int(m_a.get("trades", 0) or 0),
                    "avg_realized_cost_bps": float(m_a.get("avg_realized_cost_bps", 0.0)),
                    "score": float(score_a),
                },
                str(args.name_b): {
                    "return_pct": float(m_b.get("return_pct", 0.0)),
                    "sharpe": float(m_b.get("sharpe", 0.0)),
                    "max_drawdown_pct": float(m_b.get("max_drawdown_pct", 0.0)),
                    "trades": int(m_b.get("trades", 0) or 0),
                    "avg_realized_cost_bps": float(m_b.get("avg_realized_cost_bps", 0.0)),
                    "score": float(score_b),
                },
                "winner": winner,
                "delta": {
                    "return_pct": float(m_a.get("return_pct", 0.0) - m_b.get("return_pct", 0.0)),
                    "sharpe": float(m_a.get("sharpe", 0.0) - m_b.get("sharpe", 0.0)),
                    "max_drawdown_pct": float(m_a.get("max_drawdown_pct", 0.0) - m_b.get("max_drawdown_pct", 0.0)),
                    "score": float(score_a - score_b),
                },
            }
        )

    if wins[str(args.name_a)] > wins[str(args.name_b)]:
        recommended = str(args.name_a)
    elif wins[str(args.name_b)] > wins[str(args.name_a)]:
        recommended = str(args.name_b)
    else:
        recommended = "tie"

    report = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "config": str((root / str(args.config)).resolve()),
        "common_config": str((root / str(args.common_config)).resolve()),
        "db_path": str((root / str(args.db_path)).resolve()),
        "symbol": str(args.symbol),
        "timeframe": str(args.timeframe),
        "events_total": int(len(events)),
        "events_range": {"start": ts_min.isoformat(), "end": ts_max.isoformat()},
        "champions": {
            str(args.name_a): str((root / str(args.champion_a)).resolve()),
            str(args.name_b): str((root / str(args.champion_b)).resolve()),
        },
        "common_overlay_keys": sorted(common_overlay.keys()),
        "windows": results,
        "wins": wins,
        "recommended": recommended,
        "notes": [
            "Both champions are evaluated with identical gate/risk/order/cost/execution blocks.",
            "Only alpha section differs between champions in this comparison.",
        ],
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(
        json.dumps(
            {
                "ok": True,
                "out_json": str(out_json),
                "recommended": recommended,
                "wins": wins,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

