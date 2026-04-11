#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import random
import sys
from multiprocessing import get_context
from statistics import mean, median, pstdev
from typing import Any, Dict, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts.optimize_sim_all import _load_events, _run_metrics  # reuse proven data + backtest wiring
from trading.alpha.training import make_walk_forward_splits
from trading.config import load_config
from trading.config_overlay import deep_merge


_PAR_CFG: Dict[str, Any] | None = None
_PAR_EVENTS: List[Any] | None = None
_PAR_SPLITS: Any = None
_PAR_MIN_OOS_TRADES: int = 0
_PAR_ENFORCE_POSITIVE_OOS: bool = False


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


COMMON_FAMILIES_8 = [
    "trend_momentum_fast",
    "trend_momentum_slow",
    "range_mean_reversion",
    "deep_mean_reversion",
    "vol_breakout_intraday",
    "vol_breakout_swing",
    "oscillation_swing",
    "regime_auto",
]

_FAMILY_ALIASES: Dict[str, List[str]] = {
    "momentum": ["trend_momentum_fast", "trend_momentum_slow"],
    "mean_reversion": ["range_mean_reversion", "deep_mean_reversion"],
    "breakout": ["vol_breakout_intraday", "vol_breakout_swing"],
    "swing": ["oscillation_swing"],
    "auto": ["regime_auto"],
    "common8": list(COMMON_FAMILIES_8),
}


def _parse_families(raw: str) -> List[str]:
    allowed = set(COMMON_FAMILIES_8) | set(_FAMILY_ALIASES.keys())
    out: List[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        x = item.strip().lower()
        if not x:
            continue
        if x not in allowed:
            raise ValueError(f"unsupported family: {x}")
        expanded = _FAMILY_ALIASES.get(x, [x])
        for fam in expanded:
            if fam in seen:
                continue
            seen.add(fam)
            out.append(fam)
    if not out:
        out = list(COMMON_FAMILIES_8)
    return out


def _canonical_overlay_key(overlay: Dict[str, Any]) -> str:
    return json.dumps(overlay, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _base_overlay_for_shared(rng: random.Random) -> Dict[str, Any]:
    max_exposure = rng.choice([10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 60.0, 75.0])
    order_type = rng.choice(["market", "limit", "limit"])
    post_only = bool(rng.choice([True, False])) if order_type == "limit" else False
    order = {
        "type": order_type,
        "post_only": bool(post_only),
        "limit_offset_bps": float(rng.choice([0.25, 0.5, 1.0, 2.0, 3.0, 5.0])),
        "cycle_trade_eur": float(max_exposure),
    }
    gate = {
        "safety_margin_bps": float(rng.choice([0.25, 0.5, 1.0, 1.5, 2.0, 3.0])),
        "max_spread_bps": float(rng.choice([8.0, 12.0, 16.0, 20.0, 24.0, 30.0])),
        "max_atr_bps": float(rng.choice([80.0, 120.0, 160.0, 220.0, 300.0, 400.0, 550.0])),
    }
    risk = {
        "max_exposure_eur": float(max_exposure),
        "vol_target_bps": float(rng.choice([70.0, 90.0, 110.0, 130.0, 160.0, 180.0, 220.0])),
        "cooldown_bars": int(rng.choice([1, 2, 3, 4, 6, 8, 12])),
        "allow_short": bool(rng.choice([False, False, True])),
        "use_vol_scaling": bool(rng.choice([False, True])),
        "use_gate_size_factor": bool(rng.choice([False, True])),
        "entry_edge_bps": float(rng.choice([0.0, 2.0, 4.0, 8.0, 12.0, 20.0, 30.0, 40.0, 60.0, 80.0, 100.0])),
        "exit_edge_bps": float(rng.choice([-8.0, -4.0, -2.0, 0.0, 2.0, 4.0, 8.0, 12.0, 20.0, 30.0])),
        "min_hold_bars": int(rng.choice([0, 1, 2, 3, 4, 6, 8, 12, 16])),
        "require_break_even_for_exit": bool(rng.choice([False, True])),
        "min_exit_profit_bps": float(rng.choice([0.0, 5.0, 10.0, 20.0, 30.0, 40.0, 60.0])),
    }
    return {"gate": gate, "risk": risk, "order": order}


def _sample_alpha_momentum(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "momentum",
        "lookback": int(rng.choice([2, 3, 4, 5, 6, 8, 10, 12, 16])),
        "threshold_bps": float(rng.choice([0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 20.0, 30.0, 40.0, 60.0, 80.0])),
        "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0])),
    }


def _sample_alpha_mean_reversion(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "mean_reversion",
        "mean_reversion": {
            "lookback": int(rng.choice([3, 4, 5, 6, 8, 10, 12, 16])),
            "threshold_bps": float(rng.choice([0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0])),
            "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0])),
            "max_edge_bps": float(rng.choice([0.0, 50.0, 80.0, 120.0, 180.0, 250.0, 320.0])),
        },
    }


def _sample_alpha_breakout(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "breakout",
        "breakout": {
            "lookback": int(rng.choice([6, 8, 10, 12, 16, 20, 24, 30])),
            "trigger_bps": float(rng.choice([2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 30.0, 40.0])),
            "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0])),
            "max_edge_bps": float(rng.choice([0.0, 60.0, 100.0, 150.0, 220.0, 300.0])),
        },
    }


def _sample_alpha_swing(rng: random.Random) -> Dict[str, Any]:
    buy = float(rng.choice([0.25, 0.30, 0.35, 0.40, 0.45]))
    sell = float(rng.choice([0.60, 0.65, 0.70, 0.75]))
    if buy >= sell:
        buy, sell = 0.35, 0.70
    return {
        "type": "swing",
        "swing": {
            "lookback": int(rng.choice([10, 14, 18, 22, 26, 30])),
            "buy_band": buy,
            "sell_band": sell,
            "momentum_lookback": int(rng.choice([3, 5, 7, 10, 12])),
            "reversal_threshold_bps": float(rng.choice([0.2, 0.4, 0.6, 1.0, 1.5, 2.0, 3.0, 5.0])),
            "edge_scale": float(rng.choice([0.5, 0.8, 1.0, 1.2, 1.6, 2.0, 3.0])),
            "min_range_bps": float(rng.choice([20.0, 35.0, 50.0, 70.0, 90.0, 120.0, 160.0])),
            "max_edge_bps": float(rng.choice([60.0, 90.0, 120.0, 160.0, 220.0, 300.0])),
        },
    }


def _sample_alpha_auto(rng: random.Random) -> Dict[str, Any]:
    low_vol = float(rng.choice([30.0, 40.0, 50.0, 60.0, 70.0]))
    high_vol = float(rng.choice([100.0, 120.0, 150.0, 180.0, 220.0, 280.0, 350.0]))
    if low_vol >= high_vol:
        low_vol, high_vol = 50.0, 150.0

    range_mom = float(rng.choice([1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0]))
    trend_mom = float(rng.choice([4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 30.0]))
    if range_mom >= trend_mom:
        range_mom, trend_mom = 2.0, 8.0

    return {
        "type": "auto",
        "auto": {
            "trend": {
                "lookback": int(rng.choice([2, 3, 4, 5, 6, 8, 10])),
                "threshold_bps": float(rng.choice([0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 40.0])),
                "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0])),
            },
            "mean_reversion": {
                "lookback": int(rng.choice([4, 6, 8, 10, 12])),
                "threshold_bps": float(rng.choice([0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0])),
                "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0])),
                "max_edge_bps": float(rng.choice([0.0, 40.0, 80.0, 120.0, 180.0, 250.0])),
            },
            "breakout": {
                "lookback": int(rng.choice([8, 12, 16, 20, 24])),
                "trigger_bps": float(rng.choice([3.0, 5.0, 8.0, 10.0, 14.0, 20.0, 30.0])),
                "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0, 8.0])),
                "max_edge_bps": float(rng.choice([0.0, 60.0, 100.0, 150.0, 220.0])),
            },
            "swing": {
                "lookback": int(rng.choice([14, 18, 22, 26])),
                "buy_band": float(rng.choice([0.30, 0.35, 0.40])),
                "sell_band": float(rng.choice([0.65, 0.70, 0.75])),
                "momentum_lookback": int(rng.choice([5, 7, 10])),
                "reversal_threshold_bps": float(rng.choice([0.3, 0.5, 0.8, 1.2, 2.0, 3.0])),
                "edge_scale": float(rng.choice([0.8, 1.0, 1.2, 1.6, 2.0, 3.0])),
                "min_range_bps": float(rng.choice([25.0, 40.0, 60.0, 90.0, 130.0])),
                "max_edge_bps": float(rng.choice([70.0, 110.0, 160.0, 220.0, 300.0])),
            },
            "regime": {
                "lookback": int(rng.choice([4, 6, 8, 10, 12])),
                "trend_momentum_bps": trend_mom,
                "range_momentum_bps": range_mom,
                "high_vol_atr_bps": high_vol,
                "low_vol_atr_bps": low_vol,
                "breakout_return_bps": float(rng.choice([5.0, 8.0, 10.0, 12.0, 16.0, 24.0, 36.0])),
                "default_regime": str(rng.choice(["trend", "range"])),
                "trend_strategy": str(rng.choice(["trend", "breakout"])),
                "range_strategy": str(rng.choice(["mean_reversion", "swing"])),
                "breakout_strategy": str(rng.choice(["breakout", "trend"])),
            },
        },
    }


def _sample_alpha_trend_momentum_fast(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "momentum",
        "lookback": int(rng.choice([2, 3, 4, 5, 6, 8, 10])),
        "threshold_bps": float(rng.choice([0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0])),
        "scale": float(rng.choice([2.0, 3.0, 5.0, 8.0, 10.0, 12.0])),
    }


def _sample_alpha_trend_momentum_slow(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "momentum",
        "lookback": int(rng.choice([8, 10, 12, 16, 20, 24, 30])),
        "threshold_bps": float(rng.choice([4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 40.0, 60.0, 80.0])),
        "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0])),
    }


def _sample_alpha_range_mean_reversion(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "mean_reversion",
        "mean_reversion": {
            "lookback": int(rng.choice([4, 6, 8, 10, 12])),
            "threshold_bps": float(rng.choice([0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0])),
            "scale": float(rng.choice([2.0, 3.0, 5.0, 8.0, 10.0])),
            "max_edge_bps": float(rng.choice([80.0, 120.0, 180.0, 250.0, 320.0])),
        },
    }


def _sample_alpha_deep_mean_reversion(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "mean_reversion",
        "mean_reversion": {
            "lookback": int(rng.choice([8, 10, 12, 16, 20, 24])),
            "threshold_bps": float(rng.choice([6.0, 8.0, 12.0, 16.0, 24.0, 30.0])),
            "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0])),
            "max_edge_bps": float(rng.choice([120.0, 180.0, 250.0, 320.0, 400.0])),
        },
    }


def _sample_alpha_vol_breakout_intraday(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "breakout",
        "breakout": {
            "lookback": int(rng.choice([6, 8, 10, 12, 16, 20, 24])),
            "trigger_bps": float(rng.choice([4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 30.0])),
            "scale": float(rng.choice([2.0, 3.0, 5.0, 8.0, 10.0])),
            "max_edge_bps": float(rng.choice([120.0, 180.0, 250.0, 320.0, 400.0])),
        },
    }


def _sample_alpha_vol_breakout_swing(rng: random.Random) -> Dict[str, Any]:
    return {
        "type": "breakout",
        "breakout": {
            "lookback": int(rng.choice([16, 20, 24, 30, 36, 48])),
            "trigger_bps": float(rng.choice([10.0, 12.0, 16.0, 20.0, 30.0, 40.0, 60.0])),
            "scale": float(rng.choice([0.5, 1.0, 2.0, 3.0, 5.0])),
            "max_edge_bps": float(rng.choice([160.0, 220.0, 300.0, 400.0, 500.0])),
        },
    }


def _sample_alpha_oscillation_swing(rng: random.Random) -> Dict[str, Any]:
    buy = float(rng.choice([0.25, 0.30, 0.35, 0.40]))
    sell = float(rng.choice([0.65, 0.70, 0.75]))
    if buy >= sell:
        buy, sell = 0.35, 0.70
    return {
        "type": "swing",
        "swing": {
            "lookback": int(rng.choice([14, 18, 22, 26, 30])),
            "buy_band": buy,
            "sell_band": sell,
            "momentum_lookback": int(rng.choice([5, 7, 10, 12])),
            "reversal_threshold_bps": float(rng.choice([0.3, 0.5, 0.8, 1.2, 2.0, 3.0])),
            "edge_scale": float(rng.choice([0.8, 1.0, 1.2, 1.6, 2.0])),
            "min_range_bps": float(rng.choice([35.0, 50.0, 70.0, 90.0, 120.0, 160.0])),
            "max_edge_bps": float(rng.choice([90.0, 120.0, 160.0, 220.0, 300.0])),
        },
    }


def _sample_alpha_for_family(family: str, rng: random.Random) -> Dict[str, Any]:
    if family in {"momentum", "trend_momentum_fast"}:
        if family == "trend_momentum_fast":
            return _sample_alpha_trend_momentum_fast(rng)
        return _sample_alpha_momentum(rng)
    if family in {"mean_reversion", "range_mean_reversion"}:
        if family == "range_mean_reversion":
            return _sample_alpha_range_mean_reversion(rng)
        return _sample_alpha_mean_reversion(rng)
    if family in {"deep_mean_reversion"}:
        return _sample_alpha_deep_mean_reversion(rng)
    if family in {"breakout", "vol_breakout_intraday"}:
        if family == "vol_breakout_intraday":
            return _sample_alpha_vol_breakout_intraday(rng)
        return _sample_alpha_breakout(rng)
    if family in {"vol_breakout_swing"}:
        return _sample_alpha_vol_breakout_swing(rng)
    if family in {"swing", "oscillation_swing"}:
        if family == "oscillation_swing":
            return _sample_alpha_oscillation_swing(rng)
        return _sample_alpha_swing(rng)
    if family in {"auto", "regime_auto"}:
        return _sample_alpha_auto(rng)
    if family in {"trend_momentum_slow"}:
        return _sample_alpha_trend_momentum_slow(rng)
    raise ValueError(f"unsupported family: {family}")


def _apply_family_bias(overlay: Dict[str, Any], family: str, rng: random.Random) -> None:
    gate = overlay.setdefault("gate", {})
    risk = overlay.setdefault("risk", {})
    order = overlay.setdefault("order", {})

    if family == "trend_momentum_fast":
        gate["max_spread_bps"] = float(rng.choice([12.0, 16.0, 20.0, 24.0]))
        risk["cooldown_bars"] = int(rng.choice([1, 2, 3, 4]))
        risk["min_hold_bars"] = int(rng.choice([0, 1, 2, 3, 4]))
        risk["entry_edge_bps"] = float(rng.choice([0.0, 2.0, 4.0, 8.0, 12.0]))
    elif family == "trend_momentum_slow":
        gate["max_spread_bps"] = float(rng.choice([8.0, 12.0, 16.0]))
        risk["cooldown_bars"] = int(rng.choice([4, 6, 8, 12]))
        risk["min_hold_bars"] = int(rng.choice([6, 8, 12, 16]))
        risk["entry_edge_bps"] = float(rng.choice([8.0, 12.0, 20.0, 30.0, 40.0]))
        risk["min_exit_profit_bps"] = float(rng.choice([10.0, 20.0, 30.0, 40.0, 60.0]))
        order["type"] = "limit"
    elif family == "range_mean_reversion":
        gate["max_spread_bps"] = float(rng.choice([8.0, 12.0, 16.0]))
        risk["cooldown_bars"] = int(rng.choice([1, 2, 3, 4]))
        risk["entry_edge_bps"] = float(rng.choice([0.0, 2.0, 4.0, 8.0]))
        risk["allow_short"] = bool(rng.choice([True, True, False]))
        order["type"] = "limit"
    elif family == "deep_mean_reversion":
        gate["max_spread_bps"] = float(rng.choice([12.0, 16.0, 20.0]))
        risk["cooldown_bars"] = int(rng.choice([2, 3, 4, 6]))
        risk["entry_edge_bps"] = float(rng.choice([8.0, 12.0, 20.0, 30.0]))
        risk["allow_short"] = bool(rng.choice([True, False]))
        order["type"] = "limit"
    elif family == "vol_breakout_intraday":
        gate["max_spread_bps"] = float(rng.choice([16.0, 20.0, 24.0, 30.0]))
        gate["max_atr_bps"] = float(rng.choice([160.0, 220.0, 300.0, 400.0, 550.0]))
        risk["cooldown_bars"] = int(rng.choice([1, 2, 3, 4]))
        risk["min_hold_bars"] = int(rng.choice([0, 1, 2, 3]))
        order["type"] = "market"
        order["post_only"] = False
    elif family == "vol_breakout_swing":
        gate["max_spread_bps"] = float(rng.choice([12.0, 16.0, 20.0, 24.0]))
        gate["max_atr_bps"] = float(rng.choice([220.0, 300.0, 400.0, 550.0]))
        risk["cooldown_bars"] = int(rng.choice([3, 4, 6, 8]))
        risk["min_hold_bars"] = int(rng.choice([4, 6, 8, 12, 16]))
        risk["entry_edge_bps"] = float(rng.choice([8.0, 12.0, 20.0, 30.0]))
    elif family == "oscillation_swing":
        gate["max_spread_bps"] = float(rng.choice([8.0, 12.0, 16.0, 20.0]))
        risk["cooldown_bars"] = int(rng.choice([2, 3, 4, 6]))
        risk["min_hold_bars"] = int(rng.choice([2, 3, 4, 6, 8, 12]))
        risk["entry_edge_bps"] = float(rng.choice([2.0, 4.0, 8.0, 12.0]))
        order["type"] = str(rng.choice(["limit", "market"]))
    elif family == "regime_auto":
        gate["max_spread_bps"] = float(rng.choice([12.0, 16.0, 20.0, 24.0]))
        gate["max_atr_bps"] = float(rng.choice([160.0, 220.0, 300.0, 400.0]))
        risk["cooldown_bars"] = int(rng.choice([2, 3, 4, 6, 8]))

    if str(order.get("type", "market")) == "limit":
        order["post_only"] = bool(rng.choice([True, False]))


def _sample_candidates(*, trials: int, seed: int, families: List[str]) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    weights = {f: 1 for f in families}
    if "regime_auto" in weights:
        weights["regime_auto"] = 2
    family_bag: List[str] = []
    for f, w in weights.items():
        family_bag.extend([f] * max(1, int(w)))

    while len(out) < max(1, int(trials)):
        family = rng.choice(family_bag)
        overlay = _base_overlay_for_shared(rng)
        overlay["alpha"] = _sample_alpha_for_family(family, rng)
        _apply_family_bias(overlay, family, rng)

        key = _canonical_overlay_key(overlay)
        if key in seen:
            continue
        seen.add(key)
        out.append({"family": family, "overlay": overlay})
    return out


def _score(metrics: Dict[str, Any], *, min_trades: int = 0, enforce_positive: bool = False) -> float:
    ret = float(metrics.get("return_pct") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
    trades = float(metrics.get("trades") or 0.0)
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    avg_cost = float(metrics.get("avg_realized_cost_bps") or 0.0)
    turnover = float(metrics.get("turnover_eur") or 0.0)

    score = (
        6.0 * ret
        + 16.0 * sharpe
        + 35.0 * _clip(profit_factor - 1.0, -1.0, 2.0)
        - 2.8 * drawdown
        - 0.8 * avg_cost
        - 0.0002 * turnover
    )

    if ret <= 0:
        score -= 120.0 + 6.0 * abs(ret)
    if sharpe <= 0:
        score -= 30.0 + 5.0 * abs(sharpe)
    if profit_factor < 1.0:
        score -= 45.0 * (1.0 - profit_factor)
    if drawdown > 8.0:
        score -= 4.0 * (drawdown - 8.0)
    if trades < 5:
        score -= 30.0
    if trades > 350:
        score -= 1.0 * (trades - 350.0)
    if int(min_trades) > 0 and trades < float(min_trades):
        score -= 1000.0 + 50.0 * float(min_trades - trades)
    if enforce_positive and ret <= 0:
        score -= 3000.0
    return score


def _selection_score(metrics: Dict[str, Any], *, min_trades: int = 0, enforce_positive: bool = False) -> float:
    # Secondary model-selection objective on a separate holdout slice.
    ret = float(metrics.get("return_pct") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    drawdown = float(metrics.get("max_drawdown_pct") or 0.0)
    trades = float(metrics.get("trades") or 0.0)
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    avg_cost = float(metrics.get("avg_realized_cost_bps") or 0.0)
    turnover = float(metrics.get("turnover_eur") or 0.0)

    score = (
        8.0 * ret
        + 18.0 * sharpe
        + 30.0 * _clip(profit_factor - 1.0, -1.0, 2.0)
        - 3.0 * drawdown
        - 0.6 * avg_cost
        - 0.00015 * turnover
    )
    if ret <= 0:
        score -= 220.0 + 8.0 * abs(ret)
    if sharpe <= 0:
        score -= 60.0 + 5.0 * abs(sharpe)
    if drawdown > 8.0:
        score -= 5.0 * (drawdown - 8.0)
    req_trades = max(3, int(min_trades))
    if trades < float(req_trades):
        score -= 250.0 + 30.0 * float(req_trades - trades)
    if trades <= 0:
        score -= 700.0
    if trades >= float(req_trades):
        score += 4.0 * _clip((trades / float(req_trades)) - 1.0, 0.0, 2.0)
    if enforce_positive and ret <= 0:
        score -= 3000.0
    return score


def _evaluate_candidate(
    cfg: Dict[str, Any],
    events: List[Any],
    split_candidates: Any,
    candidate: Dict[str, Any],
    *,
    min_oos_trades: int,
    enforce_positive_oos: bool,
) -> Dict[str, Any]:
    cfg_c = copy.deepcopy(cfg)
    if isinstance(cfg_c.get("alpha"), dict):
        cfg_c["alpha"].pop("override_path", None)
    cfg_c = deep_merge(cfg_c, candidate["overlay"])

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
                "avg_realized_cost_bps": float(m.get("avg_realized_cost_bps", 0.0)),
                "turnover_eur": float(m.get("turnover_eur", 0.0)),
            }
        )
        split_scores.append(
            _score(
                m,
                min_trades=int(min_oos_trades),
                enforce_positive=bool(enforce_positive_oos),
            )
        )

    rets = [float(s["return_pct"]) for s in split_metrics] if split_metrics else [0.0]
    ret_std = pstdev(rets) if len(rets) > 1 else 0.0
    pos_ratio = float(sum(1 for r in rets if r > 0.0)) / float(len(rets))
    robust_score = 0.55 * median(split_scores) + 0.35 * mean(split_scores) - 0.10 * ret_std + 25.0 * (pos_ratio - 0.5)

    return {
        "family": str(candidate["family"]),
        "params": candidate["overlay"],
        "robust_score": robust_score,
        "oos_return_mean_pct": mean([s["return_pct"] for s in split_metrics]) if split_metrics else 0.0,
        "oos_drawdown_mean_pct": mean([s["max_drawdown_pct"] for s in split_metrics]) if split_metrics else 0.0,
        "oos_trades_mean": mean([s["trades"] for s in split_metrics]) if split_metrics else 0.0,
        "oos_avg_realized_cost_bps_mean": mean([s["avg_realized_cost_bps"] for s in split_metrics]) if split_metrics else 0.0,
        "oos_turnover_eur_mean": mean([s["turnover_eur"] for s in split_metrics]) if split_metrics else 0.0,
        "oos_return_std_pct": ret_std,
        "oos_positive_ratio": pos_ratio,
        "splits": split_metrics,
    }


def _parallel_init(
    cfg: Dict[str, Any],
    events: List[Any],
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


def _evaluate_candidate_task(task: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
    idx, cand = task
    if _PAR_CFG is None or _PAR_EVENTS is None or _PAR_SPLITS is None:
        raise RuntimeError("parallel worker not initialized")
    out = _evaluate_candidate(
        cfg=_PAR_CFG,
        events=_PAR_EVENTS,
        split_candidates=_PAR_SPLITS,
        candidate=cand,
        min_oos_trades=_PAR_MIN_OOS_TRADES,
        enforce_positive_oos=_PAR_ENFORCE_POSITIVE_OOS,
    )
    out["candidate_index"] = int(idx)
    return out


def _derive_sizes(n: int) -> tuple[int, int, int, int]:
    train = max(1200, int(n * 0.5))
    val = max(600, int(n * 0.2))
    test = max(600, int(n * 0.2))
    purge = max(1, int(n * 0.01))
    return train, val, test, purge


def optimize(
    cfg: Dict[str, Any],
    events: List[Any],
    *,
    trials: int,
    seed: int,
    jobs: int,
    families: List[str],
    train_size: int,
    val_size: int,
    test_size: int,
    purge: int,
    min_oos_trades: int,
    enforce_positive_oos: bool,
    top_k: int,
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

    candidates = _sample_candidates(trials=trials, seed=seed, families=families)
    reports: List[Dict[str, Any]] = []
    best: Dict[str, Any] | None = None

    jobs = max(1, int(jobs))
    tasks = list(enumerate(candidates, start=1))
    if jobs == 1:
        for idx, cand in tasks:
            report = _evaluate_candidate(
                cfg=cfg,
                events=events,
                split_candidates=split_candidates,
                candidate=cand,
                min_oos_trades=int(min_oos_trades),
                enforce_positive_oos=bool(enforce_positive_oos),
            )
            report["candidate_index"] = int(idx)
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
    keep = max(1, int(top_k))
    return {"best": best, "top": reports[:keep], "splits": len(split_candidates), "trials": len(candidates)}


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Professional simulation optimizer: multi-strategy search across momentum/reversion/"
            "breakout/swing/auto-regime with walk-forward robust scoring."
        )
    )
    p.add_argument("--config", default="configs/sim_auto.yaml")
    p.add_argument("--out-yaml", default="configs/sim_optimized_professional.yaml")
    p.add_argument("--out-json", default="reports/sim_opt_report_professional.json")
    p.add_argument("--db-path", default="", help="Optional external sqlite db with candles(ts_utc...) schema.")
    p.add_argument("--symbol", default="", help="Override symbol when using --db-path (e.g. XBT/EUR).")
    p.add_argument("--timeframe", default="", help="Override timeframe when using --db-path (e.g. 5m).")
    p.add_argument("--start", default="", help="Optional inclusive start timestamp override for external db.")
    p.add_argument("--end", default="", help="Optional exclusive end timestamp override for external db.")
    p.add_argument("--max-events", type=int, default=30000)
    p.add_argument("--families", default="momentum,mean_reversion,breakout,swing,auto")
    p.add_argument("--trials", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--selection-candidates", type=int, default=30)
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
        raise SystemExit(f"need at least ~3000 events for robust professional optimization, got {n}")

    families = _parse_families(str(args.families))

    holdout_total = max(1200, int(n * 0.2))
    selection_holdout = max(600, holdout_total // 2)
    final_holdout = max(600, holdout_total - selection_holdout)
    if selection_holdout + final_holdout > holdout_total:
        final_holdout = max(1, holdout_total - selection_holdout)

    search_events = events[:-holdout_total]
    tail_events = events[-holdout_total:]
    selection_events = tail_events[:selection_holdout]
    final_holdout_events = tail_events[selection_holdout:]
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
        families=families,
        train_size=train_size,
        val_size=val_size,
        test_size=test_size,
        purge=purge,
        min_oos_trades=int(args.min_oos_trades),
        enforce_positive_oos=bool(args.enforce_positive_oos),
        top_k=max(10, int(args.selection_candidates)),
    )

    # Professional selection: re-rank top robust candidates on an untouched
    # selection holdout slice, then evaluate chosen champion on final lockbox.
    selection_pool = list(summary["top"])[: max(1, int(args.selection_candidates))]
    selection_ranked: List[Dict[str, Any]] = []
    for cand in selection_pool:
        overlay = dict(cand.get("params", {}))
        cfg_c = deep_merge(copy.deepcopy(cfg), overlay)
        if isinstance(cfg_c.get("alpha"), dict):
            cfg_c["alpha"].pop("override_path", None)
        m = _run_metrics(cfg_c, selection_events)
        sel_score = _selection_score(
            m,
            min_trades=max(3, int(args.min_oos_trades) * 2),
            enforce_positive=bool(args.enforce_positive_oos),
        )
        selection_ranked.append(
            {
                "candidate_index": int(cand.get("candidate_index", 0) or 0),
                "family": str(cand.get("family", "")),
                "selection_score": float(sel_score),
                "selection_return_pct": float(m.get("return_pct", 0.0)),
                "selection_sharpe": float(m.get("sharpe", 0.0)),
                "selection_max_drawdown_pct": float(m.get("max_drawdown_pct", 0.0)),
                "selection_profit_factor": float(m.get("profit_factor", 0.0)),
                "selection_trades": int(m.get("trades", 0) or 0),
                "selection_avg_realized_cost_bps": float(m.get("avg_realized_cost_bps", 0.0)),
                "selection_turnover_eur": float(m.get("turnover_eur", 0.0)),
            }
        )

    selection_ranked.sort(key=lambda x: float(x.get("selection_score", -1e18)), reverse=True)
    min_selection_trades = max(3, int(args.min_oos_trades))
    selection_eligible = [x for x in selection_ranked if int(x.get("selection_trades", 0) or 0) >= min_selection_trades]
    champion_fallback = "eligible_by_min_trades"
    champion_rank = selection_eligible[0] if selection_eligible else None
    if champion_rank is None and selection_ranked:
        active_ranked = [x for x in selection_ranked if int(x.get("selection_trades", 0) or 0) > 0]
        if active_ranked:
            active_ranked.sort(
                key=lambda x: (
                    int(x.get("selection_trades", 0) or 0),
                    float(x.get("selection_score", -1e18)),
                ),
                reverse=True,
            )
            champion_rank = active_ranked[0]
            champion_fallback = "max_active_trades_then_score"
        else:
            champion_rank = selection_ranked[0]
            champion_fallback = "all_zero_trades_fallback"
    champion_candidate = summary["best"]
    if champion_rank is not None:
        idx = int(champion_rank["candidate_index"])
        found = next((c for c in selection_pool if int(c.get("candidate_index", 0) or 0) == idx), None)
        if found is not None:
            champion_candidate = found

    best_overlay = dict(champion_candidate["params"])
    holdout_cfg = deep_merge(copy.deepcopy(cfg), best_overlay)
    if isinstance(holdout_cfg.get("alpha"), dict):
        holdout_cfg["alpha"].pop("override_path", None)
    holdout_metrics = _run_metrics(holdout_cfg, final_holdout_events)

    chosen_best = dict(champion_candidate)
    if champion_rank is not None:
        chosen_best["selection"] = champion_rank

    report = {
        "ok": True,
        "optimizer": "professional_multi_strategy",
        "config": str(args.config),
        "families": families,
        "events_total": n,
        "events_search": n_search,
        "events_holdout_selection": len(selection_events),
        "events_holdout_final": len(final_holdout_events),
        "window": {
            "train_size": train_size,
            "val_size": val_size,
            "test_size": test_size,
            "purge": purge,
            "splits": int(summary["splits"]),
        },
        "trials": int(summary["trials"]),
        "best": chosen_best,
        "top": summary["top"],
        "selection_holdout": {
            "selection_candidates": int(len(selection_pool)),
            "min_selection_trades": int(min_selection_trades),
            "eligible_candidates": int(len(selection_eligible)),
            "champion_fallback": str(champion_fallback),
            "champion": champion_rank,
            "top": selection_ranked[:10],
        },
        "holdout": {
            "return_pct": float(holdout_metrics.get("return_pct", 0.0)),
            "sharpe": float(holdout_metrics.get("sharpe", 0.0)),
            "max_drawdown_pct": float(holdout_metrics.get("max_drawdown_pct", 0.0)),
            "profit_factor": float(holdout_metrics.get("profit_factor", 0.0)),
            "trades": int(holdout_metrics.get("trades", 0) or 0),
            "avg_realized_cost_bps": float(holdout_metrics.get("avg_realized_cost_bps", 0.0)),
            "turnover_eur": float(holdout_metrics.get("turnover_eur", 0.0)),
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
                "optimizer": "professional_multi_strategy",
                "out_yaml": out_yaml,
                "out_json": out_json,
                "best_family": report["best"]["family"],
                "holdout_return_pct": report["holdout"]["return_pct"],
                "holdout_max_drawdown_pct": report["holdout"]["max_drawdown_pct"],
                "holdout_trades": report["holdout"]["trades"],
                "trials": report["trials"],
                "splits": report["window"]["splits"],
                "jobs": int(args.jobs),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
