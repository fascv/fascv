from __future__ import annotations

import copy
from typing import Any, Mapping


ROTATION_RUNTIME_CONFIG_VERSION = 2

ROTATION_STRATEGY_TO_ALPHA_TYPE: dict[str, str] = {
    "staircase": "continuation",
    "pullback_continuation": "continuation",
    "breakout_retest": "breakout",
    "continuation": "continuation",
    "breakout": "breakout",
    "relative_strength": "trend",
    "rebound": "swing",
}

ROTATION_STRATEGY_CONFIG_OVERLAYS: dict[str, dict[str, Any]] = {
    "staircase": {
        "alpha": {
            "continuation": {
                "rebound_trigger_bps": 0.0,
                "max_structure_range_pos": 1.0,
                "staircase_min_drawdown_from_peak_bps": 0.0,
                "staircase_max_context_range_pos": 0.99,
            }
        }
    },
    "pullback_continuation": {
        "alpha": {
            "continuation": {
                "rebound_trigger_bps": 1.5,
                "pullback_max_bps": 78.0,
                "max_structure_range_pos": 0.99,
            }
        }
    },
}


def normalize_rotation_strategy_name(raw: object) -> str:
    return str(raw or "").strip().lower()


def normalize_symbol_strategy_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        symbol = str(key).strip().upper()
        strategy = normalize_rotation_strategy_name(value)
        if symbol and strategy:
            out[symbol] = strategy
    return out


def _normalize_alpha_type_name(raw: object) -> str:
    name = normalize_rotation_strategy_name(raw)
    if name in {"momentum", "trend"}:
        return "trend"
    if name in {"mean_reversion", "reversion"}:
        return "mean_reversion"
    if name in {"continuation", "followthrough"}:
        return "continuation"
    if name == "breakout":
        return "breakout"
    if name == "swing":
        return "swing"
    if name == "auto":
        return "auto"
    return ""


def rotation_strategy_to_alpha_type(strategy_name: object, fallback: object = "continuation") -> str:
    strategy = normalize_rotation_strategy_name(strategy_name)
    mapped = ROTATION_STRATEGY_TO_ALPHA_TYPE.get(strategy)
    if mapped:
        return mapped
    direct = _normalize_alpha_type_name(strategy)
    if direct:
        return direct
    fallback_name = _normalize_alpha_type_name(fallback)
    if fallback_name:
        return fallback_name
    return "continuation"


def build_selected_alpha_map(strategy_map: Mapping[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for symbol, strategy in strategy_map.items():
        symbol_name = str(symbol).strip().upper()
        if not symbol_name:
            continue
        out[symbol_name] = rotation_strategy_to_alpha_type(strategy)
    return out


def _deep_update(dst: dict[str, Any], src: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, Mapping):
            child = dst.get(key)
            if not isinstance(child, dict):
                child = {}
            dst[key] = _deep_update(dict(child), value)
            continue
        dst[key] = value
    return dst


def _coerce_float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _sanitize_alpha_swing_bands(runtime_cfg: dict[str, Any]) -> None:
    alpha = runtime_cfg.get("alpha")
    if not isinstance(alpha, dict):
        return
    swing = alpha.get("swing")
    if not isinstance(swing, dict):
        return
    buy = _coerce_float(swing.get("buy_band"), 0.42)
    sell = _coerce_float(swing.get("sell_band"), 0.67)
    buy = max(0.01, min(0.98, buy))
    sell = max(0.02, min(0.99, sell))
    min_gap = 0.01
    if sell <= buy + min_gap:
        if buy + min_gap < 0.99:
            sell = buy + min_gap
        else:
            buy = max(0.01, sell - min_gap)
    swing["buy_band"] = buy
    swing["sell_band"] = sell


def build_rotation_runtime_config(base_cfg: Mapping[str, Any], strategy_name: object) -> tuple[dict[str, Any], str]:
    runtime_cfg = copy.deepcopy(dict(base_cfg))
    normalized_strategy_name = normalize_rotation_strategy_name(strategy_name)
    alpha = runtime_cfg.setdefault("alpha", {})
    current_alpha_type = alpha.get("type", "continuation")
    alpha_type = rotation_strategy_to_alpha_type(normalized_strategy_name, fallback=current_alpha_type)
    alpha["type"] = alpha_type
    strategy_overlay = ROTATION_STRATEGY_CONFIG_OVERLAYS.get(normalized_strategy_name)
    if strategy_overlay:
        runtime_cfg = _deep_update(runtime_cfg, strategy_overlay)
    _sanitize_alpha_swing_bands(runtime_cfg)
    runtime = runtime_cfg.setdefault("runtime", {})
    runtime["rotation_strategy_name"] = normalized_strategy_name
    runtime["rotation_alpha_type"] = alpha_type
    runtime["rotation_runtime_config_version"] = ROTATION_RUNTIME_CONFIG_VERSION
    return runtime_cfg, alpha_type
