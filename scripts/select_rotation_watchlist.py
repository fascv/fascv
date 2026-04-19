#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.market_structure import classify_market_structure
from trading.rotation_universe import POOL as ROTATION_POOL
from trading.utils.env import load_env

BINANCE_BASE = "https://api.binance.com"
CANDIDATES = tuple(ROTATION_POOL)
KLINE_INTERVAL = "5m"
NEAR_KLINE_INTERVAL = "1h"
MACRO_KLINE_INTERVAL = "1d"
# 5m candles with paginated fetch: keep enough history so the coin-specific
# near corridor can use a meaningful 7-13 day valley-to-valley context.
BAR_MINUTES = 5.0
BAR_HOURS = BAR_MINUTES / 60.0
NEAR_RANGE_MIN_DAYS = 7.0
NEAR_RANGE_MAX_DAYS = 13.0
MID_RANGE_DAYS = 60.0
LONG_RANGE_DAYS = 180.0
NEAR_RANGE_MIN_BARS = int((NEAR_RANGE_MIN_DAYS * 24.0 * 60.0) / BAR_MINUTES)
NEAR_RANGE_MAX_BARS = int((NEAR_RANGE_MAX_DAYS * 24.0 * 60.0) / BAR_MINUTES)
MID_RANGE_BARS = int((MID_RANGE_DAYS * 24.0 * 60.0) / BAR_MINUTES)
LONG_RANGE_BARS = int((LONG_RANGE_DAYS * 24.0 * 60.0) / BAR_MINUTES)
KLINE_REQUEST_LIMIT = 1000
# Keep 5m scan window compact for selector runtime; near 7-13d is computed
# from dedicated 1h data.
KLINE_LIMIT = 864
NEAR_KLINE_LIMIT = max(336, int(round(NEAR_RANGE_MAX_DAYS * 24.0 + 24.0)))
MACRO_KLINE_LIMIT = max(220, int(round(LONG_RANGE_DAYS + 30.0)))
DEFAULT_SELECTOR_MODE = "trend"
DEFAULT_SWEETSPOT_PATH = REPO_ROOT / "logs" / "rotation_slope_sweetspots_1m.json"
ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
UNIVERSE_POLICY_CACHE_FILE = REPO_ROOT / "logs" / "rotation_universe_policy_cache.json"
AUTO_BLACKLIST_CACHE_FILE = REPO_ROOT / "logs" / "rotation_auto_blacklist_cache.json"
AUTO_BLACKLIST_CACHE_SCHEMA_VERSION = 3
TOKEN_PREFILTER_CACHE_FILE = REPO_ROOT / "logs" / "rotation_token_prefilter_cache.json"
TOKEN_PREFILTER_CACHE_SCHEMA_VERSION = 1
DEFAULT_QUOTE_ASSET = "USDC"
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
KNOWN_QUOTES = ("USDC", "USDT", "FDUSD", "BUSD", "TUSD", "USDP", "DAI", "EUR", "USD", "BTC", "ETH")
EXCLUSION_ENV_VARS = ("ROTATION_EXCLUDED_BASE_SYMBOLS", "ROTATION_BLACKLIST_SYMBOLS")
AUTO_BLACKLIST_DEFAULT_LEVEL = "mild"
PERSISTENT_DOWNTREND_MIN_DAYS_DEFAULT = 21
AUTO_BLACKLIST_LISTING_KLINE_LIMIT_DEFAULT = 3000
AUTO_BLACKLIST_LISTING_DECAY_MIN_HISTORY_DAYS_DEFAULT = 120
AUTO_BLACKLIST_LISTING_DECAY_MIN_DROP_FROM_START_PCT_DEFAULT = 40.0
AUTO_BLACKLIST_LISTING_DECAY_MIN_DRAWDOWN_FROM_HIGH_PCT_DEFAULT = 60.0
AUTO_BLACKLIST_LISTING_DECAY_MIN_SHARE_BELOW_START_PCT_DEFAULT = 85.0
TOKEN_PREFILTER_BAD_TRADE_RISKS = {"honeypot"}
TOKEN_PREFILTER_HIGH_RISK_FLAGS = {
    "all_snipers_honeypot",
    "high_fail_rate",
    "high_siphon_rate",
}
TOKEN_PREFILTER_MEDIUM_RISK_FLAGS = {
    "medium_fail_rate",
    "medium_siphon_rate",
}
TOKEN_PREFILTER_CHAIN_IDS = {
    "BSC": 56,
    "BEP20": 56,
    "ETH": 1,
    "ERC20": 1,
    "BASE": 8453,
    "ARB": 42161,
    "ARBITRUM": 42161,
    "ARBITRUMONE": 42161,
    "OP": 10,
    "OPTIMISM": 10,
    "MATIC": 137,
    "POLYGON": 137,
    "AVAX": 43114,
    "AVALANCHE": 43114,
    "FTM": 250,
    "FANTOM": 250,
}
DEFAULT_EXCLUDED_BASE_SYMBOLS: frozenset[str] = frozenset(
    {
        "EUR",
        "EURI",
        "USD",
        "USDC",
        "USDT",
        "FDUSD",
        "BUSD",
        "TUSD",
        "USDP",
        "DAI",
        "USDS",
        "PYUSD",
        "EURC",
    }
)

PERSISTENT_DOWNTREND_LEVEL_THRESHOLDS: dict[str, dict[str, float]] = {
    # Mild: catches broad long-running down drifts, including weaker laggards.
    "mild": {
        "ret180_pct_max": -55.0,
        "ret90_pct_max": -20.0,
        "rel180_pct_max": -20.0,
        "rel90_pct_max": -8.0,
        "long_high_shift_pct_max": -20.0,
        "long_low_shift_pct_max": -15.0,
        "long_log_slope_max": -0.0018,
    },
    # Medium: balanced setting.
    "medium": {
        "ret180_pct_max": -60.0,
        "ret90_pct_max": -25.0,
        "rel180_pct_max": -30.0,
        "rel90_pct_max": -12.0,
        "long_high_shift_pct_max": -25.0,
        "long_low_shift_pct_max": -20.0,
        "long_log_slope_max": -0.0022,
    },
    # Strict: only symbols that keep sliding down over months.
    "strict": {
        "ret180_pct_max": -65.0,
        "ret90_pct_max": -28.0,
        "rel180_pct_max": -38.0,
        "rel90_pct_max": -16.0,
        "long_high_shift_pct_max": -30.0,
        "long_low_shift_pct_max": -24.0,
        "long_log_slope_max": -0.0026,
    },
    # Aggressive: stricter universe cleanup, marks persistent laggards earlier.
    "aggressive": {
        "ret180_pct_max": -50.0,
        "ret90_pct_max": -18.0,
        "rel180_pct_max": -15.0,
        "rel90_pct_max": -6.0,
        "long_high_shift_pct_max": -16.0,
        "long_low_shift_pct_max": -12.0,
        "long_log_slope_max": -0.0015,
    },
    # Extreme: strongest downtrend filter for pre-universe blacklist usage.
    "extreme": {
        "ret180_pct_max": -42.0,
        "ret90_pct_max": -14.0,
        "rel180_pct_max": -10.0,
        "rel90_pct_max": -4.0,
        "long_high_shift_pct_max": -12.0,
        "long_low_shift_pct_max": -9.0,
        "long_log_slope_max": -0.0012,
    },
}

LISTING_STRUCTURAL_DOWNTREND_LEVEL_THRESHOLDS: dict[str, dict[str, float]] = {
    # Mild: only clear, deep structural losers.
    "mild": {
        "ret_since_listing_pct_max": -50.0,
        "drawdown_from_listing_high_pct_max": -65.0,
        "young_history_days_max": 120.0,
        "young_drawdown_from_high_pct_max": -60.0,
        "young_high_shift_pct_max": -35.0,
    },
    # Medium: balanced.
    "medium": {
        "ret_since_listing_pct_max": -45.0,
        "drawdown_from_listing_high_pct_max": -60.0,
        "young_history_days_max": 120.0,
        "young_drawdown_from_high_pct_max": -57.0,
        "young_high_shift_pct_max": -32.0,
    },
    # Strict: stronger than medium.
    "strict": {
        "ret_since_listing_pct_max": -40.0,
        "drawdown_from_listing_high_pct_max": -56.0,
        "young_history_days_max": 120.0,
        "young_drawdown_from_high_pct_max": -54.0,
        "young_high_shift_pct_max": -30.0,
    },
    # Aggressive: default structural-risk cleanup.
    "aggressive": {
        "ret_since_listing_pct_max": -32.0,
        "drawdown_from_listing_high_pct_max": -50.0,
        "young_history_days_max": 120.0,
        "young_drawdown_from_high_pct_max": -50.0,
        "young_high_shift_pct_max": -26.0,
    },
    # Extreme: strongest structural-risk cleanup.
    "extreme": {
        "ret_since_listing_pct_max": -25.0,
        "drawdown_from_listing_high_pct_max": -45.0,
        "young_history_days_max": 140.0,
        "young_drawdown_from_high_pct_max": -45.0,
        "young_high_shift_pct_max": -22.0,
    },
}


def _env_flag(name: str, default: str = "0") -> bool:
    raw = os.environ.get(name, default)
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_hours(name: str, default_hours: float) -> float:
    raw = os.environ.get(name, str(default_hours))
    try:
        return max(0.0, float(raw))
    except Exception:
        return float(default_hours)


def _env_int(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(float(raw))
    except Exception:
        value = int(default)
    value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return int(value)


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except Exception:
        value = float(default)
    value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return float(value)


def _env_csv_set(name: str, default: str = "") -> set[str]:
    raw = os.environ.get(name, default)
    out: set[str] = set()
    for item in str(raw or "").split(","):
        text = str(item or "").strip().lower()
        if text:
            out.add(text)
    return out


def _persistent_downdrift_min_days() -> int:
    raw = os.environ.get(
        "ROTATION_PERSISTENT_DOWNTREND_MIN_DAYS",
        str(PERSISTENT_DOWNTREND_MIN_DAYS_DEFAULT),
    )
    try:
        days = int(float(raw))
    except Exception:
        days = int(PERSISTENT_DOWNTREND_MIN_DAYS_DEFAULT)
    return max(7, days)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate Binance spot symbols for the rotation/scalp selector."
    )
    parser.add_argument(
        "--setup-mode",
        choices=("trend", "hybrid", "bottom_strict"),
        default="",
        help="Optional override for ROTATION_SETUP_MODE.",
    )
    parser.add_argument(
        "--universe-source",
        choices=("pool", "exchange"),
        default=os.environ.get("ROTATION_SELECTOR_UNIVERSE_SOURCE", "pool"),
        help="Use the production pool or discover all tradable spot symbols from exchangeInfo.",
    )
    parser.add_argument(
        "--quote-asset",
        default=os.environ.get("ROTATION_SELECTOR_QUOTE_ASSET", DEFAULT_QUOTE_ASSET),
        help="Quote asset used for market discovery and market ids (default: USDC).",
    )
    parser.add_argument(
        "--symbols",
        default=os.environ.get("ROTATION_SELECTOR_SYMBOLS", ""),
        help="Optional comma-separated base symbols to scan instead of pool/exchange discovery.",
    )
    parser.add_argument(
        "--ignore-balances",
        action="store_true",
        default=_env_flag("ROTATION_SELECTOR_IGNORE_BALANCES", "0"),
        help="Disable account balance lookup so keep_open does not affect scoring.",
    )
    return parser.parse_args()


def _normalize_symbol_token(raw: str) -> str:
    token = str(raw or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")
    if not token:
        return ""
    for quote in KNOWN_QUOTES:
        if token.endswith(quote) and len(token) > len(quote):
            return token[: -len(quote)]
    return token


def _excluded_base_symbols() -> set[str]:
    out = set(DEFAULT_EXCLUDED_BASE_SYMBOLS)
    for env_name in EXCLUSION_ENV_VARS:
        raw = os.environ.get(env_name, "")
        for item in str(raw or "").split(","):
            token = _normalize_symbol_token(str(item or ""))
            if token:
                out.add(token)
    return out


def _is_excluded_base_symbol(symbol: str, quote_asset: str = "") -> bool:
    token = str(symbol or "").strip().upper()
    if not token:
        return True
    quote_token = str(quote_asset or "").strip().upper()
    if quote_token and token == quote_token:
        return True
    return token in _excluded_base_symbols()


def _dedupe_symbols(symbols: list[str], *, quote_asset: str = DEFAULT_QUOTE_ASSET) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = _normalize_symbol_token(raw)
        if not symbol or symbol in seen:
            continue
        if _is_excluded_base_symbol(symbol, quote_asset):
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _selector_mode() -> str:
    mode = os.environ.get("ROTATION_SETUP_MODE", DEFAULT_SELECTOR_MODE)
    mode_norm = str(mode or "").strip().lower()
    if mode_norm in {"hybrid", "mixed"}:
        return "hybrid"
    if mode_norm in {"trend"}:
        return "trend"
    return "bottom_strict"


def _load_slope_profiles(path: Path) -> dict[str, dict[str, float | int | None]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        return {}
    out: dict[str, dict[str, float | int | None]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).upper().strip()
        rec = item.get("recommendation")
        if not symbol or not isinstance(rec, dict):
            continue
        try:
            low = float(rec.get("low_bps_h"))
            high_raw = rec.get("high_bps_h")
            high = None if high_raw is None else float(high_raw)
            mid = float(rec.get("mid_bps_h"))
            n = int(rec.get("n", 0))
            slope_window_hours = float(item.get("slope_window_hours", 4.0))
        except Exception:
            continue
        out[symbol] = {
            "low_bps_h": low,
            "high_bps_h": high,
            "mid_bps_h": mid,
            "n": n,
            "slope_window_hours": slope_window_hours,
        }
    return out


def _slope_bps_per_hour(closes: list[float], window_bars: int) -> float:
    n = min(len(closes), max(2, int(window_bars)))
    if n < 2:
        return 0.0
    segment = [float(v) for v in closes[-n:]]
    if len(segment) < 2 or segment[-1] <= 0.0:
        return 0.0
    x_mean = (n - 1) / 2.0
    denom = sum((idx - x_mean) ** 2 for idx in range(n))
    if denom <= 0.0:
        return 0.0
    y_mean = sum(segment) / float(n)
    cov_num = sum((idx - x_mean) * (segment[idx] - y_mean) for idx in range(n))
    b_per_bar = cov_num / denom
    return (b_per_bar / segment[-1]) * 10000.0 / BAR_HOURS


def _profile_tolerance_bps_h(low_bps_h: float, high_bps_h: float | None) -> float:
    if high_bps_h is None:
        return max(10.0, low_bps_h * 0.12)
    width = max(5.0, high_bps_h - low_bps_h)
    return max(8.0, width * 0.35)


def _slope_profile_match_state(
    current_bps_h: float,
    low_bps_h: float,
    high_bps_h: float | None,
) -> tuple[bool, float, float]:
    tol = _profile_tolerance_bps_h(low_bps_h, high_bps_h)
    if high_bps_h is None:
        min_allowed = low_bps_h - tol
        matched = current_bps_h >= min_allowed
        distance = 0.0 if matched else (min_allowed - current_bps_h)
        center = low_bps_h + 40.0
        spread = max(25.0, tol * 2.0)
        alignment = max(0.0, 1.0 - (abs(current_bps_h - center) / spread))
        return matched, max(0.0, distance), alignment

    lo = low_bps_h - tol
    hi = high_bps_h + tol
    matched = lo <= current_bps_h <= hi
    if matched:
        distance = 0.0
    elif current_bps_h < lo:
        distance = lo - current_bps_h
    else:
        distance = current_bps_h - hi
    center = (low_bps_h + high_bps_h) / 2.0
    half_span = max(8.0, ((high_bps_h - low_bps_h) / 2.0) + tol)
    alignment = max(0.0, 1.0 - (abs(current_bps_h - center) / half_span))
    return matched, max(0.0, distance), alignment


def _coin_peak_block_profile(
    low_bps_h: float,
    high_bps_h: float | None,
    samples: int,
) -> dict[str, float | str] | None:
    # Require enough history; otherwise skip coin-specific peak blocking.
    if samples < 120:
        return None
    high_eff = 220.0 if high_bps_h is None else float(high_bps_h)
    # Flat/slow profiles are not the target for peak-chase blocking.
    if low_bps_h < 50.0 and high_eff < 70.0:
        return None
    if high_bps_h is None or low_bps_h >= 150.0:
        return {
            "profile_class": "extreme",
            "max_pos_24h_pct": 58.0,
            "max_bars_since_peak": 18.0,
            "min_rebound_from_valley_bps": 90.0,
            "max_drawdown_from_peak_bps": 120.0,
        }
    if low_bps_h >= 100.0:
        return {
            "profile_class": "high",
            "max_pos_24h_pct": 62.0,
            "max_bars_since_peak": 14.0,
            "min_rebound_from_valley_bps": 110.0,
            "max_drawdown_from_peak_bps": 100.0,
        }
    if low_bps_h >= 70.0:
        return {
            "profile_class": "mid",
            "max_pos_24h_pct": 66.0,
            "max_bars_since_peak": 12.0,
            "min_rebound_from_valley_bps": 130.0,
            "max_drawdown_from_peak_bps": 85.0,
        }
    return {
        "profile_class": "broad",
        "max_pos_24h_pct": 70.0,
        "max_bars_since_peak": 10.0,
        "min_rebound_from_valley_bps": 160.0,
        "max_drawdown_from_peak_bps": 70.0,
    }


def _return_bps(closes: list[float], bars: int) -> float:
    if len(closes) <= bars:
        return 0.0
    base = closes[-(bars + 1)]
    last = closes[-1]
    if base <= 0.0:
        return 0.0
    return ((last / base) - 1.0) * 10000.0


def _log_slope_per_step(closes: list[float]) -> float:
    series = [float(v or 0.0) for v in closes if float(v or 0.0) > 0.0]
    n = len(series)
    if n < 3:
        return 0.0
    x_mean = (n - 1) / 2.0
    den = sum((idx - x_mean) ** 2 for idx in range(n))
    if den <= 0.0:
        return 0.0
    y_vals = [math.log(v) for v in series]
    y_mean = sum(y_vals) / float(n)
    cov_num = sum((idx - x_mean) * (y_vals[idx] - y_mean) for idx in range(n))
    return cov_num / den


def _range_shift_pct(closes: list[float], first_bars: int, second_bars: int) -> tuple[float, float]:
    series = [float(v or 0.0) for v in closes if float(v or 0.0) > 0.0]
    first_n = max(2, int(first_bars))
    second_n = max(2, int(second_bars))
    required = first_n + second_n
    if len(series) < required:
        return 0.0, 0.0
    segment = series[-required:]
    first = segment[:first_n]
    second = segment[-second_n:]
    low_first = min(first)
    high_first = max(first)
    low_second = min(second)
    high_second = max(second)
    if low_first <= 0.0 or high_first <= 0.0:
        return 0.0, 0.0
    high_shift_pct = ((high_second / high_first) - 1.0) * 100.0
    low_shift_pct = ((low_second / low_first) - 1.0) * 100.0
    return high_shift_pct, low_shift_pct


def _scale_compound_threshold_pct(base_pct: float, base_days: float, target_days: float) -> float:
    base_days_eff = max(1.0, float(base_days))
    target_days_eff = max(1.0, float(target_days))
    base_pct_eff = float(base_pct)
    ratio = 1.0 + (base_pct_eff / 100.0)
    if ratio <= 0.0:
        scaled_linear = base_pct_eff * (target_days_eff / base_days_eff)
        return max(-99.999, scaled_linear)
    scaled_ratio = ratio ** (target_days_eff / base_days_eff)
    scaled_pct = (scaled_ratio - 1.0) * 100.0
    return max(-99.999, scaled_pct)


def _scale_linear_threshold_pct(base_pct: float, base_days: float, target_days: float) -> float:
    base_days_eff = max(1.0, float(base_days))
    target_days_eff = max(1.0, float(target_days))
    return float(base_pct) * (target_days_eff / base_days_eff)


def _listing_structure_metrics(symbol_listing_closes: list[float]) -> dict[str, float | bool | str]:
    series = [float(v or 0.0) for v in symbol_listing_closes if float(v or 0.0) > 0.0]
    history_days = max(0, len(series) - 1)
    min_required_days = _persistent_downdrift_min_days()
    if history_days < min_required_days:
        return {
            "enabled": True,
            "data_ok": False,
            "reason": "insufficient_listing_history",
            "history_days": float(history_days),
            "min_required_days": float(min_required_days),
        }
    first = float(series[0])
    last = float(series[-1])
    if first <= 0.0 or last <= 0.0:
        return {
            "enabled": True,
            "data_ok": False,
            "reason": "invalid_listing_prices",
            "history_days": float(history_days),
            "min_required_days": float(min_required_days),
        }
    listing_high = max(series)
    ret_since_listing_pct = ((last / first) - 1.0) * 100.0
    drawdown_from_listing_high_pct = ((last / listing_high) - 1.0) * 100.0 if listing_high > 0.0 else 0.0
    share_days_below_listing_start_pct = (
        (sum(1 for value in series if float(value) < first) / float(len(series))) * 100.0
        if series
        else 0.0
    )
    half = max(2, len(series) // 2)
    listing_high_shift_pct, listing_low_shift_pct = _range_shift_pct(
        series,
        first_bars=half,
        second_bars=half,
    )
    return {
        "enabled": True,
        "data_ok": True,
        "reason": "",
        "history_days": float(history_days),
        "min_required_days": float(min_required_days),
        "ret_since_listing_pct": float(ret_since_listing_pct),
        "drawdown_from_listing_high_pct": float(drawdown_from_listing_high_pct),
        "share_days_below_listing_start_pct": float(share_days_below_listing_start_pct),
        "listing_high_shift_pct": float(listing_high_shift_pct),
        "listing_low_shift_pct": float(listing_low_shift_pct),
    }


def _listing_structural_downdrift_block(
    listing_metrics: dict[str, float | bool | str],
    *,
    level: str,
) -> tuple[bool, str]:
    thresholds = LISTING_STRUCTURAL_DOWNTREND_LEVEL_THRESHOLDS.get(str(level or "").strip().lower())
    if thresholds is None:
        return False, ""
    if not bool(listing_metrics.get("data_ok", False)):
        return False, ""

    history_days = float(listing_metrics.get("history_days", 0.0) or 0.0)
    ret_since_listing_pct = float(listing_metrics.get("ret_since_listing_pct", 0.0) or 0.0)
    drawdown_from_listing_high_pct = float(
        listing_metrics.get("drawdown_from_listing_high_pct", 0.0) or 0.0
    )
    listing_high_shift_pct = float(listing_metrics.get("listing_high_shift_pct", 0.0) or 0.0)

    long_down = (
        ret_since_listing_pct <= float(thresholds["ret_since_listing_pct_max"])
        and drawdown_from_listing_high_pct
        <= float(thresholds["drawdown_from_listing_high_pct_max"])
    )
    young_crash = (
        history_days <= float(thresholds["young_history_days_max"])
        and drawdown_from_listing_high_pct
        <= float(thresholds["young_drawdown_from_high_pct_max"])
        and listing_high_shift_pct <= float(thresholds["young_high_shift_pct_max"])
    )
    if long_down:
        return True, f"listing_structural_downdrift_{level}"
    if young_crash:
        return True, f"listing_young_crash_profile_{level}"
    return False, ""


def _listing_decay_profile_block(
    listing_metrics: dict[str, float | bool | str],
    *,
    min_history_days: float,
    min_drop_from_start_pct: float,
    min_drawdown_from_high_pct: float,
    min_share_below_start_pct: float,
) -> tuple[bool, str]:
    if not bool(listing_metrics.get("data_ok", False)):
        return False, ""
    history_days = float(listing_metrics.get("history_days", 0.0) or 0.0)
    if history_days < float(min_history_days):
        return False, ""
    ret_since_listing_pct = float(listing_metrics.get("ret_since_listing_pct", 0.0) or 0.0)
    drawdown_from_listing_high_pct = float(
        listing_metrics.get("drawdown_from_listing_high_pct", 0.0) or 0.0
    )
    share_days_below_listing_start_pct = float(
        listing_metrics.get("share_days_below_listing_start_pct", 0.0) or 0.0
    )
    drop_from_start_pct = max(0.0, -ret_since_listing_pct)
    drawdown_abs_pct = max(0.0, -drawdown_from_listing_high_pct)
    blocked = (
        drop_from_start_pct >= float(min_drop_from_start_pct)
        and drawdown_abs_pct >= float(min_drawdown_from_high_pct)
        and share_days_below_listing_start_pct >= float(min_share_below_start_pct)
    )
    if blocked:
        return True, "listing_structural_decay_profile"
    return False, ""


def _persistent_downdrift_metrics(
    symbol_macro_closes: list[float],
    btc_macro_closes: list[float],
    *,
    level: str,
) -> dict[str, float | bool | str]:
    level_norm = str(level or "").strip().lower()
    thresholds = PERSISTENT_DOWNTREND_LEVEL_THRESHOLDS.get(level_norm)
    if thresholds is None:
        return {
            "enabled": False,
            "data_ok": False,
            "blocked": False,
            "level": "off",
            "reason": "disabled",
        }

    symbol_series = [float(v or 0.0) for v in symbol_macro_closes if float(v or 0.0) > 0.0]
    btc_series = [float(v or 0.0) for v in btc_macro_closes if float(v or 0.0) > 0.0]
    symbol_days = max(0, len(symbol_series) - 1)
    btc_days = max(0, len(btc_series) - 1)
    min_required_days = _persistent_downdrift_min_days()
    if symbol_days < min_required_days or btc_days < min_required_days:
        return {
            "enabled": True,
            "data_ok": False,
            "blocked": False,
            "level": level_norm,
            "reason": "insufficient_macro_history",
            "history_days": float(symbol_days),
            "btc_history_days": float(btc_days),
            "min_required_days": float(min_required_days),
        }

    eval_days = int(min(float(LONG_RANGE_DAYS), float(symbol_days), float(btc_days)))
    if eval_days < 2:
        return {
            "enabled": True,
            "data_ok": False,
            "blocked": False,
            "level": level_norm,
            "reason": "insufficient_macro_history",
            "history_days": float(symbol_days),
            "btc_history_days": float(btc_days),
            "min_required_days": float(min_required_days),
        }

    eval_window = eval_days + 1
    symbol_long = symbol_series[-eval_window:]
    btc_long = btc_series[-eval_window:]

    ret180_days = max(1, eval_days)
    ret90_days = max(1, min(90, eval_days))

    ret180_pct = _return_bps(symbol_long, ret180_days) / 100.0
    ret90_pct = _return_bps(symbol_long, ret90_days) / 100.0
    btc_ret180_pct = _return_bps(btc_long, ret180_days) / 100.0
    btc_ret90_pct = _return_bps(btc_long, ret90_days) / 100.0
    rel180_pct = ret180_pct - btc_ret180_pct
    rel90_pct = ret90_pct - btc_ret90_pct

    long_half = max(2, int(math.floor(float(ret180_days) / 2.0)))
    mid_days = max(2, min(int(MID_RANGE_DAYS), ret180_days))
    mid_half = max(2, int(math.floor(float(mid_days) / 2.0)))
    long_high_shift_pct, long_low_shift_pct = _range_shift_pct(
        symbol_long,
        first_bars=long_half,
        second_bars=long_half,
    )
    mid_high_shift_pct, mid_low_shift_pct = _range_shift_pct(
        symbol_long,
        first_bars=mid_half,
        second_bars=mid_half,
    )
    long_log_slope = _log_slope_per_step(symbol_long)

    ret180_pct_max = _scale_compound_threshold_pct(
        float(thresholds["ret180_pct_max"]),
        LONG_RANGE_DAYS,
        float(ret180_days),
    )
    ret90_pct_max = _scale_compound_threshold_pct(
        float(thresholds["ret90_pct_max"]),
        90.0,
        float(ret90_days),
    )
    rel180_pct_max = _scale_linear_threshold_pct(
        float(thresholds["rel180_pct_max"]),
        LONG_RANGE_DAYS,
        float(ret180_days),
    )
    rel90_pct_max = _scale_linear_threshold_pct(
        float(thresholds["rel90_pct_max"]),
        90.0,
        float(ret90_days),
    )
    long_high_shift_pct_max = _scale_compound_threshold_pct(
        float(thresholds["long_high_shift_pct_max"]),
        LONG_RANGE_DAYS,
        float(ret180_days),
    )
    long_low_shift_pct_max = _scale_compound_threshold_pct(
        float(thresholds["long_low_shift_pct_max"]),
        LONG_RANGE_DAYS,
        float(ret180_days),
    )
    long_log_slope_max = float(thresholds["long_log_slope_max"])

    blocked = (
        ret180_pct <= ret180_pct_max
        and ret90_pct <= ret90_pct_max
        and rel180_pct <= rel180_pct_max
        and rel90_pct <= rel90_pct_max
        and long_high_shift_pct <= long_high_shift_pct_max
        and long_low_shift_pct <= long_low_shift_pct_max
        and long_log_slope < long_log_slope_max
    )

    return {
        "enabled": True,
        "data_ok": True,
        "blocked": bool(blocked),
        "level": level_norm,
        "reason": ("blocked" if blocked else ""),
        "history_days": float(symbol_days),
        "btc_history_days": float(btc_days),
        "min_required_days": float(min_required_days),
        "eval_days": float(ret180_days),
        "ret90_days": float(ret90_days),
        "eval_mode": (
            "since_listing"
            if ret180_days < int(round(LONG_RANGE_DAYS))
            else "full_window"
        ),
        "ret180_pct": ret180_pct,
        "ret90_pct": ret90_pct,
        "btc_ret180_pct": btc_ret180_pct,
        "btc_ret90_pct": btc_ret90_pct,
        "rel180_pct": rel180_pct,
        "rel90_pct": rel90_pct,
        "mid_high_shift_pct": mid_high_shift_pct,
        "mid_low_shift_pct": mid_low_shift_pct,
        "long_high_shift_pct": long_high_shift_pct,
        "long_low_shift_pct": long_low_shift_pct,
        "long_log_slope": long_log_slope,
        "ret180_pct_max": ret180_pct_max,
        "ret90_pct_max": ret90_pct_max,
        "rel180_pct_max": rel180_pct_max,
        "rel90_pct_max": rel90_pct_max,
        "long_high_shift_pct_max": long_high_shift_pct_max,
        "long_low_shift_pct_max": long_low_shift_pct_max,
        "long_log_slope_max": long_log_slope_max,
    }


def _non_falling_longtrend_block(
    metrics: dict[str, float | bool | str],
    *,
    enabled: bool,
    ret180_min_pct: float,
    long_high_shift_min_pct: float,
    long_low_shift_min_pct: float,
    long_log_slope_min: float,
    rel180_min_pct: float | None = None,
) -> tuple[bool, str]:
    if not enabled:
        return False, ""
    if not bool(metrics.get("data_ok", False)):
        return True, "insufficient_macro_history"

    checks: list[tuple[str, float, float]] = [
        ("ret180", float(metrics.get("ret180_pct", 0.0) or 0.0), float(ret180_min_pct)),
        (
            "long_high_shift",
            float(metrics.get("long_high_shift_pct", 0.0) or 0.0),
            float(long_high_shift_min_pct),
        ),
        (
            "long_low_shift",
            float(metrics.get("long_low_shift_pct", 0.0) or 0.0),
            float(long_low_shift_min_pct),
        ),
        ("long_log_slope", float(metrics.get("long_log_slope", 0.0) or 0.0), float(long_log_slope_min)),
    ]
    if rel180_min_pct is not None:
        checks.append(
            ("rel180", float(metrics.get("rel180_pct", 0.0) or 0.0), float(rel180_min_pct))
        )

    for field, value, minimum in checks:
        if value < minimum:
            return True, f"{field}_below_min"
    return False, ""


def _returns(closes: list[float], bars: int) -> list[float]:
    tail = closes[-(bars + 1) :]
    out: list[float] = []
    for a, b in zip(tail, tail[1:]):
        if a <= 0.0:
            out.append(0.0)
        else:
            out.append((b / a) - 1.0)
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 1.0
    xs = xs[-n:]
    ys = ys[-n:]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0.0 or var_y <= 0.0:
        return 1.0
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, cov / math.sqrt(var_x * var_y)))


def _abs_corr(xs: list[float], ys: list[float]) -> float:
    return abs(_pearson(xs, ys))


def _select_diversified_symbols(
    *,
    priority_symbols: list[str],
    row_map: dict[str, dict[str, object]],
    returns_map: dict[str, list[float]],
    target_size: int,
    penalty: float,
    target_corr: float,
) -> list[str]:
    target = max(1, int(target_size))
    ordered = [sym for sym in priority_symbols if sym in row_map]
    selected: list[str] = []
    seen: set[str] = set()

    for sym in ordered:
        row = row_map.get(sym) or {}
        if bool(row.get("keep_open")) and sym not in seen:
            selected.append(sym)
            seen.add(sym)
        if len(selected) >= target:
            return selected[:target]

    while len(selected) < target:
        best_symbol = ""
        best_value = -1e18
        for sym in ordered:
            if sym in seen:
                continue
            row = row_map.get(sym) or {}
            base_score = float(row.get("score", 0.0) or 0.0)
            corr_penalty = 0.0
            if selected:
                cur_returns = returns_map.get(sym) or []
                corr_vals: list[float] = []
                if cur_returns:
                    for picked in selected:
                        other_returns = returns_map.get(picked) or []
                        if other_returns:
                            corr_vals.append(_abs_corr(cur_returns, other_returns))
                if corr_vals:
                    avg_corr = sum(corr_vals) / float(len(corr_vals))
                    corr_penalty = max(0.0, avg_corr - target_corr) * max(0.0, penalty)
            adjusted = base_score - corr_penalty
            if adjusted > best_value:
                best_value = adjusted
                best_symbol = sym
        if not best_symbol:
            break
        selected.append(best_symbol)
        seen.add(best_symbol)

    return selected[:target]


def _public_json(path: str) -> object:
    url = f"{BINANCE_BASE}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        msg = body.strip()
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                msg = str(payload.get("msg") or payload.get("message") or msg)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {path}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error {path}: {exc}") from exc


def _fetch_exchange_symbols(quote_asset: str) -> list[str]:
    payload = _public_json("/api/v3/exchangeInfo")
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        return []
    quote_asset_norm = str(quote_asset or DEFAULT_QUOTE_ASSET).strip().upper() or DEFAULT_QUOTE_ASSET
    out: list[str] = []
    seen: set[str] = set()
    for row in symbols:
        if not isinstance(row, dict):
            continue
        base_asset = str(row.get("baseAsset", "")).strip().upper()
        row_quote = str(row.get("quoteAsset", "")).strip().upper()
        status = str(row.get("status", "")).strip().upper()
        permissions = row.get("permissions")
        if not base_asset or row_quote != quote_asset_norm or status != "TRADING":
            continue
        if not base_asset.isascii():
            continue
        if _is_excluded_base_symbol(base_asset, quote_asset_norm):
            continue
        if any(base_asset.endswith(sfx) for sfx in LEVERAGED_SUFFIXES):
            continue
        if isinstance(permissions, list) and permissions and "SPOT" not in permissions:
            continue
        if base_asset in seen:
            continue
        seen.add(base_asset)
        out.append(base_asset)
    return sorted(out)


def _resolve_candidates(args: argparse.Namespace) -> tuple[list[str], str]:
    quote_asset = str(args.quote_asset or DEFAULT_QUOTE_ASSET).strip().upper() or DEFAULT_QUOTE_ASSET
    explicit = _dedupe_symbols(str(args.symbols or "").split(","), quote_asset=quote_asset)
    if explicit:
        return explicit, "explicit_symbols"
    if str(args.universe_source).strip().lower() == "exchange":
        return _fetch_exchange_symbols(quote_asset), "exchange"
    return _dedupe_symbols([str(symbol) for symbol in CANDIDATES], quote_asset=quote_asset), "pool"


def _load_active_selected_symbols() -> set[str]:
    if not ACTIVE_FILE.is_file():
        return set()
    try:
        payload = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()
    out: set[str] = set()
    for item in payload.get("selected", []):
        symbol = str(item).strip().upper()
        if symbol:
            out.add(symbol)
    return out


def _load_universe_policy_cache(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_universe_policy_cache(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _auto_blacklist_level(default_level: str = AUTO_BLACKLIST_DEFAULT_LEVEL) -> str:
    raw = str(
        os.environ.get("ROTATION_AUTO_BLACKLIST_DOWNTREND_LEVEL", default_level)
    ).strip().lower()
    if raw in {"", "0", "off", "false", "no", "none", "disabled"}:
        return "off"
    if raw in PERSISTENT_DOWNTREND_LEVEL_THRESHOLDS:
        return raw
    if default_level in PERSISTENT_DOWNTREND_LEVEL_THRESHOLDS:
        return default_level
    return AUTO_BLACKLIST_DEFAULT_LEVEL


def _load_auto_blacklist_cache(path: Path) -> dict[str, object]:
    return _load_universe_policy_cache(path)


def _save_auto_blacklist_cache(path: Path, payload: dict[str, object]) -> None:
    _save_universe_policy_cache(path, payload)


def _build_auto_blacklist(
    *,
    quote_asset: str,
    scan_symbols: list[str],
    btc_macro_closes: list[float],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    enabled = _env_flag("ROTATION_AUTO_BLACKLIST_ENABLED", "1")
    level = _auto_blacklist_level()
    use_listing_structural_block = _env_flag(
        "ROTATION_AUTO_BLACKLIST_USE_LISTING_STRUCTURAL_BLOCK",
        "1",
    )
    listing_decay_profile_enabled = _env_flag(
        "ROTATION_AUTO_BLACKLIST_LISTING_DECAY_PROFILE_ENABLED",
        "0",
    )
    listing_decay_min_history_days = _env_int(
        "ROTATION_AUTO_BLACKLIST_LISTING_DECAY_MIN_HISTORY_DAYS",
        AUTO_BLACKLIST_LISTING_DECAY_MIN_HISTORY_DAYS_DEFAULT,
        minimum=30,
        maximum=2000,
    )
    listing_decay_min_drop_from_start_pct = _env_float(
        "ROTATION_AUTO_BLACKLIST_LISTING_DECAY_MIN_DROP_FROM_START_PCT",
        AUTO_BLACKLIST_LISTING_DECAY_MIN_DROP_FROM_START_PCT_DEFAULT,
        minimum=0.0,
        maximum=99.0,
    )
    listing_decay_min_drawdown_from_high_pct = _env_float(
        "ROTATION_AUTO_BLACKLIST_LISTING_DECAY_MIN_DRAWDOWN_FROM_HIGH_PCT",
        AUTO_BLACKLIST_LISTING_DECAY_MIN_DRAWDOWN_FROM_HIGH_PCT_DEFAULT,
        minimum=0.0,
        maximum=99.0,
    )
    listing_decay_min_share_below_start_pct = _env_float(
        "ROTATION_AUTO_BLACKLIST_LISTING_DECAY_MIN_SHARE_BELOW_START_PCT",
        AUTO_BLACKLIST_LISTING_DECAY_MIN_SHARE_BELOW_START_PCT_DEFAULT,
        minimum=0.0,
        maximum=100.0,
    )
    listing_kline_limit = _env_int(
        "ROTATION_AUTO_BLACKLIST_LISTING_KLINE_LIMIT",
        AUTO_BLACKLIST_LISTING_KLINE_LIMIT_DEFAULT,
        minimum=220,
        maximum=8000,
    )
    max_age_sec = int(
        _env_hours("ROTATION_AUTO_BLACKLIST_RECHECK_HOURS", 24.0) * 3600.0
    )
    force_refresh = _env_flag("ROTATION_AUTO_BLACKLIST_FORCE_REFRESH", "0")
    now_ts = int(time.time())
    quote_asset_norm = str(quote_asset or DEFAULT_QUOTE_ASSET).strip().upper()
    scan_symbols_norm = _dedupe_symbols(scan_symbols, quote_asset=quote_asset_norm)
    auto_blacklist_active = bool(enabled and (level != "off" or listing_decay_profile_enabled))

    cache = _load_auto_blacklist_cache(AUTO_BLACKLIST_CACHE_FILE)
    cache_generated_ts = int(cache.get("generated_at_ts") or 0)
    cache_level = str(cache.get("downdrift_level", "")).strip().lower()
    cache_quote = str(cache.get("quote_asset", "")).strip().upper()
    cache_schema_version = int(cache.get("schema_version") or 0)
    cache_entries_raw = cache.get("entries")
    if not isinstance(cache_entries_raw, dict):
        cache_entries_raw = {}
    cache_scanned_raw = cache.get("scanned_symbols")
    if not isinstance(cache_scanned_raw, list):
        cache_scanned_raw = []
    cache_scanned = {
        _normalize_symbol_token(str(item or ""))
        for item in cache_scanned_raw
        if _normalize_symbol_token(str(item or ""))
    }
    scan_set = set(scan_symbols_norm)
    cache_entries: dict[str, dict[str, object]] = {}
    for raw_symbol, raw_entry in cache_entries_raw.items():
        symbol = _normalize_symbol_token(str(raw_symbol or ""))
        if not symbol:
            continue
        if not isinstance(raw_entry, dict):
            raw_entry = {}
        reason = str(raw_entry.get("reason", "")).strip() or "auto_blacklist"
        entry = dict(raw_entry)
        entry["reason"] = reason
        entry["symbol"] = symbol
        cache_entries[symbol] = entry

    refresh_reason = ""
    need_refresh = False
    if auto_blacklist_active:
        if force_refresh:
            need_refresh = True
            refresh_reason = "force_refresh"
        elif cache_quote != quote_asset_norm:
            need_refresh = True
            refresh_reason = "quote_asset_changed"
        elif cache_level != level:
            need_refresh = True
            refresh_reason = "level_changed"
        elif bool(cache.get("listing_decay_profile_enabled", False)) != bool(
            listing_decay_profile_enabled
        ):
            need_refresh = True
            refresh_reason = "listing_decay_profile_mode_changed"
        elif listing_decay_profile_enabled and (
            int(cache.get("listing_decay_profile_min_history_days") or 0)
            != int(listing_decay_min_history_days)
            or float(cache.get("listing_decay_profile_min_drop_from_start_pct") or 0.0)
            != float(listing_decay_min_drop_from_start_pct)
            or float(cache.get("listing_decay_profile_min_drawdown_from_high_pct") or 0.0)
            != float(listing_decay_min_drawdown_from_high_pct)
            or float(cache.get("listing_decay_profile_min_share_below_start_pct") or 0.0)
            != float(listing_decay_min_share_below_start_pct)
        ):
            need_refresh = True
            refresh_reason = "listing_decay_profile_thresholds_changed"
        elif cache_schema_version != AUTO_BLACKLIST_CACHE_SCHEMA_VERSION:
            need_refresh = True
            refresh_reason = "cache_schema_changed"
        elif now_ts - cache_generated_ts > max_age_sec:
            need_refresh = True
            refresh_reason = "stale_cache"
        elif cache_scanned and cache_scanned != scan_set:
            need_refresh = True
            refresh_reason = "scan_symbol_set_changed"
        elif not cache_entries_raw and not cache_scanned:
            need_refresh = True
            refresh_reason = "missing_cache"

    refreshed = False
    refresh_ok = True
    refresh_errors: list[dict[str, str]] = []
    refresh_failed_symbols: set[str] = set()
    entries_out: dict[str, dict[str, object]] = {}

    if auto_blacklist_active and need_refresh:
        refreshed = True
        for symbol in scan_symbols_norm:
            market = f"{symbol}{quote_asset_norm}"
            try:
                macro_klines = _get_klines(
                    market,
                    interval=MACRO_KLINE_INTERVAL,
                    target_bars=MACRO_KLINE_LIMIT,
                )
                symbol_macro_closes = [float(k[4]) for k in macro_klines]
                metrics = _persistent_downdrift_metrics(
                    symbol_macro_closes,
                    btc_macro_closes,
                    level=level,
                )
                macro_blocked = (
                    level != "off"
                    and bool(metrics.get("data_ok", False))
                    and bool(metrics.get("blocked", False))
                )
                listing_metrics: dict[str, float | bool | str] = {
                    "enabled": bool(use_listing_structural_block or listing_decay_profile_enabled),
                    "data_ok": False,
                    "reason": "disabled",
                }
                listing_blocked = False
                listing_reason = ""
                listing_decay_profile_blocked = False
                listing_decay_profile_reason = ""
                if use_listing_structural_block or listing_decay_profile_enabled:
                    try:
                        listing_klines = _get_klines(
                            market,
                            interval=MACRO_KLINE_INTERVAL,
                            target_bars=listing_kline_limit,
                        )
                        symbol_listing_closes = [float(k[4]) for k in listing_klines]
                    except Exception:
                        symbol_listing_closes = list(symbol_macro_closes)
                    listing_metrics = _listing_structure_metrics(symbol_listing_closes)

                if use_listing_structural_block and level != "off":
                    listing_blocked, listing_reason = _listing_structural_downdrift_block(
                        listing_metrics,
                        level=level,
                    )
                if listing_decay_profile_enabled:
                    (
                        listing_decay_profile_blocked,
                        listing_decay_profile_reason,
                    ) = _listing_decay_profile_block(
                        listing_metrics,
                        min_history_days=float(listing_decay_min_history_days),
                        min_drop_from_start_pct=float(listing_decay_min_drop_from_start_pct),
                        min_drawdown_from_high_pct=float(listing_decay_min_drawdown_from_high_pct),
                        min_share_below_start_pct=float(listing_decay_min_share_below_start_pct),
                    )

                if macro_blocked or listing_blocked or listing_decay_profile_blocked:
                    reason = (
                        f"persistent_downdrift_{level}"
                        if macro_blocked
                        else (
                            listing_decay_profile_reason
                            if listing_decay_profile_blocked
                            else (listing_reason or f"listing_structural_downdrift_{level}")
                        )
                    )
                    entries_out[symbol] = {
                        "symbol": symbol,
                        "reason": reason,
                        "macro_blocked": bool(macro_blocked),
                        "listing_blocked": bool(listing_blocked),
                        "listing_block_reason": str(listing_reason or ""),
                        "listing_decay_profile_blocked": bool(listing_decay_profile_blocked),
                        "listing_decay_profile_reason": str(listing_decay_profile_reason or ""),
                        "ret180_pct": float(metrics.get("ret180_pct", 0.0) or 0.0),
                        "ret90_pct": float(metrics.get("ret90_pct", 0.0) or 0.0),
                        "rel180_pct": float(metrics.get("rel180_pct", 0.0) or 0.0),
                        "rel90_pct": float(metrics.get("rel90_pct", 0.0) or 0.0),
                        "long_high_shift_pct": float(
                            metrics.get("long_high_shift_pct", 0.0) or 0.0
                        ),
                        "long_low_shift_pct": float(
                            metrics.get("long_low_shift_pct", 0.0) or 0.0
                        ),
                        "long_log_slope": float(
                            metrics.get("long_log_slope", 0.0) or 0.0
                        ),
                        "listing_history_days": float(
                            listing_metrics.get("history_days", 0.0) or 0.0
                        ),
                        "listing_ret_since_listing_pct": float(
                            listing_metrics.get("ret_since_listing_pct", 0.0) or 0.0
                        ),
                        "listing_drawdown_from_high_pct": float(
                            listing_metrics.get("drawdown_from_listing_high_pct", 0.0) or 0.0
                        ),
                        "listing_share_days_below_start_pct": float(
                            listing_metrics.get("share_days_below_listing_start_pct", 0.0) or 0.0
                        ),
                        "listing_high_shift_pct": float(
                            listing_metrics.get("listing_high_shift_pct", 0.0) or 0.0
                        ),
                        "listing_low_shift_pct": float(
                            listing_metrics.get("listing_low_shift_pct", 0.0) or 0.0
                        ),
                    }
            except Exception as exc:
                refresh_ok = False
                refresh_failed_symbols.add(symbol)
                refresh_errors.append({"symbol": symbol, "error": str(exc).strip()})

        # Partial refresh failures must not silently clear prior blacklist decisions
        # for failed symbols; keep their last known cached entry.
        if refresh_failed_symbols and cache_entries:
            for symbol in sorted(refresh_failed_symbols):
                cached_entry = cache_entries.get(symbol)
                if isinstance(cached_entry, dict):
                    entries_out[symbol] = dict(cached_entry)

        if refresh_errors and cache_entries and not entries_out:
            for symbol, entry in cache_entries.items():
                entries_out[symbol] = dict(entry)

        payload: dict[str, object] = {
            "schema_version": AUTO_BLACKLIST_CACHE_SCHEMA_VERSION,
            "generated_at_ts": now_ts,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
            "quote_asset": quote_asset_norm,
            "downdrift_level": level,
            "enabled": True,
            "use_listing_structural_block": bool(use_listing_structural_block),
            "listing_decay_profile_enabled": bool(listing_decay_profile_enabled),
            "listing_decay_profile_min_history_days": int(listing_decay_min_history_days),
            "listing_decay_profile_min_drop_from_start_pct": float(
                listing_decay_min_drop_from_start_pct
            ),
            "listing_decay_profile_min_drawdown_from_high_pct": float(
                listing_decay_min_drawdown_from_high_pct
            ),
            "listing_decay_profile_min_share_below_start_pct": float(
                listing_decay_min_share_below_start_pct
            ),
            "listing_kline_limit": int(listing_kline_limit),
            "scan_symbol_count": len(scan_symbols_norm),
            "scanned_symbols": scan_symbols_norm,
            "entry_count": len(entries_out),
            "entries": entries_out,
            "refresh_ok": refresh_ok,
            "refresh_errors": refresh_errors[:96],
            "refresh_failed_symbols": sorted(refresh_failed_symbols),
        }
        _save_auto_blacklist_cache(AUTO_BLACKLIST_CACHE_FILE, payload)
    else:
        if auto_blacklist_active:
            for symbol, entry in cache_entries.items():
                entries_out[symbol] = dict(entry)

    info: dict[str, object] = {
        "enabled": bool(auto_blacklist_active),
        "downdrift_level": level,
        "use_listing_structural_block": bool(use_listing_structural_block),
        "listing_decay_profile_enabled": bool(listing_decay_profile_enabled),
        "listing_decay_profile_min_history_days": int(listing_decay_min_history_days),
        "listing_decay_profile_min_drop_from_start_pct": float(
            listing_decay_min_drop_from_start_pct
        ),
        "listing_decay_profile_min_drawdown_from_high_pct": float(
            listing_decay_min_drawdown_from_high_pct
        ),
        "listing_decay_profile_min_share_below_start_pct": float(
            listing_decay_min_share_below_start_pct
        ),
        "listing_kline_limit": int(listing_kline_limit),
        "refresh_forced": bool(force_refresh),
        "refresh_needed": bool(need_refresh),
        "refresh_reason": refresh_reason,
        "refreshed": refreshed,
        "refresh_ok": refresh_ok,
        "refresh_errors_total": len(refresh_errors),
        "refresh_errors_sample": refresh_errors[:24],
        "refresh_failed_symbols": sorted(refresh_failed_symbols)[:48],
        "cache_file": str(AUTO_BLACKLIST_CACHE_FILE),
        "cache_schema_version": cache_schema_version,
        "cache_schema_expected": AUTO_BLACKLIST_CACHE_SCHEMA_VERSION,
        "cache_generated_at_ts": cache_generated_ts,
        "cache_max_age_sec": max_age_sec,
        "scan_symbol_count": len(scan_symbols_norm),
        "entry_count": len(entries_out),
        "symbols": sorted(entries_out.keys()),
    }

    if not auto_blacklist_active:
        return {}, info
    return entries_out, info


def _network_chain_id(network: str) -> int | None:
    normalized = str(network or "").strip().upper().replace(" ", "")
    if not normalized:
        return None
    if normalized in TOKEN_PREFILTER_CHAIN_IDS:
        return TOKEN_PREFILTER_CHAIN_IDS[normalized]
    if "BSC" in normalized or "BEP20" in normalized:
        return 56
    if "ETH" in normalized or "ERC20" in normalized:
        return 1
    if "BASE" in normalized:
        return 8453
    if "ARBITRUM" in normalized:
        return 42161
    if "OPTIMISM" in normalized:
        return 10
    if "POLYGON" in normalized or "MATIC" in normalized:
        return 137
    if "AVAX" in normalized or "AVALANCHE" in normalized:
        return 43114
    if "FANTOM" in normalized:
        return 250
    return None


def _public_url_json(url: str, params: dict[str, object] | None = None) -> object:
    qs = urllib.parse.urlencode({k: str(v) for k, v in (params or {}).items()})
    full_url = f"{url}?{qs}" if qs else url
    req = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {url}: {body[:240].strip()}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error {url}: {exc}") from exc


def _fetch_24h_ticker_map() -> dict[str, dict[str, float]]:
    payload = _public_json("/api/v3/ticker/24hr")
    out: dict[str, dict[str, float]] = {}
    if not isinstance(payload, list):
        return out
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        try:
            out[symbol] = {
                "quoteVolume": float(item.get("quoteVolume") or 0.0),
                "count": float(item.get("count") or 0.0),
            }
        except Exception:
            continue
    return out


def _fetch_binance_capital_asset_map() -> dict[str, dict[str, object]]:
    payload = _signed_get("/sapi/v1/capital/config/getall")
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, object]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        coin = str(item.get("coin", "")).strip().upper()
        if coin:
            out[coin] = item
    return out


def _honeypot_scan(contract: str, chain_id: int) -> dict[str, object]:
    payload = _public_url_json(
        "https://api.honeypot.is/v2/IsHoneypot",
        {"address": contract, "chainID": chain_id},
    )
    if not isinstance(payload, dict):
        return {"status": "invalid_payload"}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    honeypot = (
        payload.get("honeypotResult")
        if isinstance(payload.get("honeypotResult"), dict)
        else {}
    )
    flags_raw = payload.get("flags")
    flags = [str(item) for item in flags_raw] if isinstance(flags_raw, list) else []
    return {
        "status": "ok",
        "trade_risk": str(summary.get("risk") or "").strip().lower(),
        "risk_level": summary.get("riskLevel"),
        "is_honeypot": honeypot.get("isHoneypot"),
        "flags": flags,
    }


def _goplus_scan(contract: str, chain_id: int) -> dict[str, object]:
    payload = _public_url_json(
        f"https://api.gopluslabs.io/api/v1/token_security/{int(chain_id)}",
        {"contract_addresses": contract},
    )
    if not isinstance(payload, dict):
        return {"status": "invalid_payload"}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    item = result.get(str(contract).lower()) if isinstance(result, dict) else None
    if not isinstance(item, dict):
        return {"status": "missing_result"}
    return {
        "status": "ok",
        "is_open_source": item.get("is_open_source"),
        "is_proxy": item.get("is_proxy"),
        "is_mintable": item.get("is_mintable"),
        "buy_tax": item.get("buy_tax"),
        "sell_tax": item.get("sell_tax"),
        "is_blacklisted": item.get("is_blacklisted"),
        "is_whitelisted": item.get("is_whitelisted"),
        "transfer_pausable": item.get("transfer_pausable"),
        "slippage_modifiable": item.get("slippage_modifiable"),
        "cannot_sell_all": item.get("cannot_sell_all"),
    }


def _contract_scan_is_high_risk(scan: dict[str, object]) -> bool:
    hp = scan.get("honeypot") if isinstance(scan.get("honeypot"), dict) else {}
    gp = scan.get("goplus") if isinstance(scan.get("goplus"), dict) else {}
    risk = str(hp.get("trade_risk") or "").strip().lower()
    flags = {str(item).strip().lower() for item in (hp.get("flags") or [])}
    try:
        risk_level = int(float(hp.get("risk_level") or 0))
    except Exception:
        risk_level = 0
    if risk in TOKEN_PREFILTER_BAD_TRADE_RISKS:
        return True
    if bool(hp.get("is_honeypot")):
        return True
    if risk_level >= 80:
        return True
    if flags & TOKEN_PREFILTER_HIGH_RISK_FLAGS:
        return True
    if str(gp.get("is_mintable") or "").strip() == "1":
        return True
    if str(gp.get("transfer_pausable") or "").strip() == "1":
        return True
    if str(gp.get("slippage_modifiable") or "").strip() == "1":
        return True
    return False


def _contract_scan_is_unknown(scan: dict[str, object]) -> bool:
    hp = scan.get("honeypot") if isinstance(scan.get("honeypot"), dict) else {}
    risk = str(hp.get("trade_risk") or "").strip().lower()
    status = str(hp.get("status") or "").strip().lower()
    return status != "ok" or risk in {"", "unknown"}


def _contract_scan_warning_flags(scan: dict[str, object]) -> list[str]:
    hp = scan.get("honeypot") if isinstance(scan.get("honeypot"), dict) else {}
    gp = scan.get("goplus") if isinstance(scan.get("goplus"), dict) else {}
    flags: list[str] = []
    for item in hp.get("flags") or []:
        text = str(item).strip().lower()
        if text in TOKEN_PREFILTER_MEDIUM_RISK_FLAGS:
            flags.append(text)
    if str(gp.get("is_proxy") or "").strip() == "1":
        flags.append("proxy_contract")
    return sorted(set(flags))


def _decide_contract_prefilter(
    scans: list[dict[str, object]],
    *,
    block_nondefault_when_default_unknown: bool,
) -> dict[str, object]:
    default_scan = next((scan for scan in scans if bool(scan.get("is_default"))), None)
    if default_scan is None:
        default_scan = scans[0] if scans else None
    high_risk_scans = [scan for scan in scans if _contract_scan_is_high_risk(scan)]
    warnings = sorted(
        {
            flag
            for scan in scans
            for flag in _contract_scan_warning_flags(scan)
        }
    )
    if default_scan is not None and _contract_scan_is_high_risk(default_scan):
        return {
            "blocked": True,
            "reason": "token_contract_risk_default",
            "warnings": warnings,
        }
    if (
        block_nondefault_when_default_unknown
        and default_scan is not None
        and _contract_scan_is_unknown(default_scan)
        and high_risk_scans
    ):
        return {
            "blocked": True,
            "reason": "token_contract_risk_nondefault_default_unknown",
            "warnings": warnings,
        }
    return {
        "blocked": False,
        "reason": "",
        "warnings": warnings,
    }


def _scan_token_contracts(symbol: str, asset_info: dict[str, object]) -> dict[str, object]:
    networks = asset_info.get("networkList")
    if not isinstance(networks, list):
        networks = []
    candidates: list[dict[str, object]] = []
    for item in networks:
        if not isinstance(item, dict):
            continue
        network = str(item.get("network") or "").strip().upper()
        contract = str(item.get("contractAddress") or "").strip()
        chain_id = _network_chain_id(network)
        if not network or not contract or chain_id is None:
            continue
        candidates.append(
            {
                "network": network,
                "chain_id": int(chain_id),
                "contract": contract,
                "is_default": bool(item.get("isDefault")),
            }
        )

    default_candidate = next((item for item in candidates if bool(item.get("is_default"))), None)
    if default_candidate is None and candidates:
        default_candidate = candidates[0]

    def _run_scan(item: dict[str, object]) -> dict[str, object]:
        contract = str(item.get("contract") or "")
        chain_id = int(item.get("chain_id") or 0)
        scan: dict[str, object] = {
            "network": str(item.get("network") or ""),
            "chain_id": int(chain_id),
            "contract": contract,
            "is_default": bool(item.get("is_default")),
            "honeypot": {"status": "not_run"},
            "goplus": {"status": "not_run"},
        }
        try:
            scan["honeypot"] = _honeypot_scan(contract, int(chain_id))
        except Exception as exc:
            scan["honeypot"] = {"status": "error", "error": str(exc).strip()}
        try:
            scan["goplus"] = _goplus_scan(contract, int(chain_id))
        except Exception as exc:
            scan["goplus"] = {"status": "error", "error": str(exc).strip()}
        return scan

    scans: list[dict[str, object]] = []
    if default_candidate is not None:
        default_scan = _run_scan(default_candidate)
        scans.append(default_scan)
        if (
            _contract_scan_is_unknown(default_scan)
            and _env_flag("ROTATION_TOKEN_PREFILTER_BLOCK_NONDEFAULT_WHEN_DEFAULT_UNKNOWN", "1")
        ):
            for item in candidates:
                if item is default_candidate:
                    continue
                scan = _run_scan(item)
                scans.append(scan)
                if _contract_scan_is_high_risk(scan):
                    break
    decision = _decide_contract_prefilter(
        scans,
        block_nondefault_when_default_unknown=_env_flag(
            "ROTATION_TOKEN_PREFILTER_BLOCK_NONDEFAULT_WHEN_DEFAULT_UNKNOWN",
            "1",
        ),
    )
    return {
        "symbol": symbol,
        "blocked": bool(decision.get("blocked", False)),
        "reason": str(decision.get("reason") or ""),
        "warnings": decision.get("warnings") if isinstance(decision.get("warnings"), list) else [],
        "scans": scans,
    }


def _build_contract_risk_cache(
    *,
    scan_symbols: list[str],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    enabled = _env_flag("ROTATION_TOKEN_PREFILTER_CONTRACT_ENABLED", "0")
    if not enabled:
        return {}, {"enabled": False}
    max_age_sec = int(_env_hours("ROTATION_TOKEN_PREFILTER_RECHECK_HOURS", 24.0) * 3600.0)
    force_refresh = _env_flag("ROTATION_TOKEN_PREFILTER_FORCE_REFRESH", "0")
    now_ts = int(time.time())
    scan_symbols_norm = [_normalize_symbol_token(symbol) for symbol in scan_symbols]
    scan_symbols_norm = [symbol for symbol in scan_symbols_norm if symbol]
    scan_set = set(scan_symbols_norm)

    cache = _load_universe_policy_cache(TOKEN_PREFILTER_CACHE_FILE)
    cache_schema = int(cache.get("schema_version") or 0)
    cache_generated_ts = int(cache.get("generated_at_ts") or 0)
    cache_scanned_raw = cache.get("scanned_symbols")
    if not isinstance(cache_scanned_raw, list):
        cache_scanned_raw = []
    cache_scanned = {
        _normalize_symbol_token(str(item or ""))
        for item in cache_scanned_raw
        if _normalize_symbol_token(str(item or ""))
    }
    entries_raw = cache.get("entries")
    if not isinstance(entries_raw, dict):
        entries_raw = {}

    need_refresh = bool(force_refresh)
    refresh_reason = "force_refresh" if force_refresh else ""
    if not need_refresh:
        if cache_schema != TOKEN_PREFILTER_CACHE_SCHEMA_VERSION:
            need_refresh = True
            refresh_reason = "cache_schema_changed"
        elif now_ts - cache_generated_ts > max_age_sec:
            need_refresh = True
            refresh_reason = "stale_cache"
        elif cache_scanned != scan_set:
            need_refresh = True
            refresh_reason = "scan_symbol_set_changed"
        elif not entries_raw:
            need_refresh = True
            refresh_reason = "missing_cache"

    if not need_refresh:
        entries: dict[str, dict[str, object]] = {}
        for symbol, entry in entries_raw.items():
            if isinstance(entry, dict):
                entries[_normalize_symbol_token(str(symbol))] = dict(entry)
        return entries, {
            "enabled": True,
            "refreshed": False,
            "refresh_needed": False,
            "refresh_reason": "",
            "cache_file": str(TOKEN_PREFILTER_CACHE_FILE),
            "entry_count": len(entries),
        }

    entries_out: dict[str, dict[str, object]] = {}
    refresh_ok = True
    refresh_errors: list[dict[str, str]] = []
    try:
        asset_map = _fetch_binance_capital_asset_map()
    except Exception as exc:
        asset_map = {}
        refresh_ok = False
        refresh_errors.append({"symbol": "*", "error": str(exc).strip()})

    for symbol in scan_symbols_norm:
        asset_info = asset_map.get(symbol)
        if not isinstance(asset_info, dict):
            continue
        try:
            entries_out[symbol] = _scan_token_contracts(symbol, asset_info)
        except Exception as exc:
            refresh_ok = False
            refresh_errors.append({"symbol": symbol, "error": str(exc).strip()})

    payload: dict[str, object] = {
        "schema_version": TOKEN_PREFILTER_CACHE_SCHEMA_VERSION,
        "generated_at_ts": now_ts,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "scanned_symbols": scan_symbols_norm,
        "entry_count": len(entries_out),
        "entries": entries_out,
        "refresh_ok": refresh_ok,
        "refresh_errors": refresh_errors[:96],
    }
    _save_universe_policy_cache(TOKEN_PREFILTER_CACHE_FILE, payload)
    return entries_out, {
        "enabled": True,
        "refreshed": True,
        "refresh_needed": True,
        "refresh_reason": refresh_reason,
        "refresh_ok": refresh_ok,
        "refresh_errors_total": len(refresh_errors),
        "refresh_errors_sample": refresh_errors[:24],
        "cache_file": str(TOKEN_PREFILTER_CACHE_FILE),
        "entry_count": len(entries_out),
    }


def _build_token_prefilter(
    *,
    quote_asset: str,
    scan_symbols: list[str],
    book_ticker_map: dict[str, dict[str, float]],
    universe_policy_map: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    enabled = _env_flag("ROTATION_TOKEN_PREFILTER_ENABLED", "1")
    quote_asset_norm = str(quote_asset or DEFAULT_QUOTE_ASSET).strip().upper() or DEFAULT_QUOTE_ASSET
    scan_symbols_norm = _dedupe_symbols(scan_symbols, quote_asset=quote_asset_norm)
    min_24h_quote_volume = _env_float(
        "ROTATION_TOKEN_PREFILTER_MIN_24H_QUOTE_VOLUME",
        0.0,
        minimum=0.0,
    )
    max_spread_bps = _env_float(
        "ROTATION_TOKEN_PREFILTER_MAX_SPREAD_BPS",
        35.0,
        minimum=0.0,
    )
    min_top_depth_notional = _env_float(
        "ROTATION_TOKEN_PREFILTER_MIN_TOP_DEPTH_NOTIONAL",
        80.0,
        minimum=0.0,
    )
    blocked_tags = _env_csv_set("ROTATION_TOKEN_PREFILTER_BLOCK_TAGS", "Seed")
    contract_hard_block = _env_flag("ROTATION_TOKEN_PREFILTER_CONTRACT_HARD_BLOCK", "0")
    if not enabled:
        return {}, {
            "enabled": False,
            "min_24h_quote_volume": min_24h_quote_volume,
            "max_spread_bps": max_spread_bps,
            "min_top_depth_notional": min_top_depth_notional,
            "blocked_tags": sorted(blocked_tags),
            "contract_hard_block": contract_hard_block,
        }

    ticker_map: dict[str, dict[str, float]] = {}
    ticker_error = ""
    try:
        ticker_map = _fetch_24h_ticker_map()
    except Exception as exc:
        ticker_error = str(exc).strip()

    product_map: dict[str, dict[str, object]] = {}
    product_error = ""
    if blocked_tags:
        try:
            product_map = _fetch_binance_product_map()
        except Exception as exc:
            product_error = str(exc).strip()

    contract_map, contract_info = _build_contract_risk_cache(scan_symbols=scan_symbols_norm)
    entries: dict[str, dict[str, object]] = {}
    for symbol in scan_symbols_norm:
        market = f"{symbol}{quote_asset_norm}"
        reasons: list[str] = []
        ticker = ticker_map.get(market, {})
        quote_volume_24h = float(ticker.get("quoteVolume", 0.0) or 0.0)
        book = book_ticker_map.get(market, {})
        bid = float(book.get("bidPrice", 0.0) or 0.0)
        ask = float(book.get("askPrice", 0.0) or 0.0)
        bid_qty = float(book.get("bidQty", 0.0) or 0.0)
        ask_qty = float(book.get("askQty", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else 0.0
        spread_bps = ((ask - bid) / mid) * 10000.0 if mid > 0.0 else 0.0
        top_depth_notional = (bid * bid_qty) + (ask * ask_qty)
        if ticker_map and quote_volume_24h < min_24h_quote_volume:
            reasons.append("token_prefilter_low_24h_volume")
        if mid > 0.0 and spread_bps > max_spread_bps:
            reasons.append("token_prefilter_wide_spread")
        if top_depth_notional > 0.0 and top_depth_notional < min_top_depth_notional:
            reasons.append("token_prefilter_thin_top_depth")

        product_info = product_map.get(market, {})
        tags_raw = product_info.get("tags") if isinstance(product_info, dict) else []
        product_tags = [str(item).strip() for item in tags_raw] if isinstance(tags_raw, list) else []
        if not product_tags and isinstance(universe_policy_map, dict):
            policy_info = universe_policy_map.get(market, {})
            policy_tags = policy_info.get("monitoring_tags") if isinstance(policy_info, dict) else []
            product_tags = (
                [str(item).strip() for item in policy_tags]
                if isinstance(policy_tags, list)
                else []
            )
        matched_block_tags = sorted(
            {
                str(tag).strip().lower()
                for tag in product_tags
                if str(tag).strip().lower() in blocked_tags
            }
        )
        if matched_block_tags:
            reasons.append(f"token_prefilter_tag_{matched_block_tags[0]}")

        contract_entry = contract_map.get(symbol, {})
        if (
            contract_hard_block
            and isinstance(contract_entry, dict)
            and bool(contract_entry.get("blocked", False))
        ):
            reasons.append(str(contract_entry.get("reason") or "token_contract_risk"))

        entries[symbol] = {
            "symbol": symbol,
            "market": market,
            "blocked": bool(reasons),
            "reason": reasons[0] if reasons else "",
            "reasons": reasons,
            "quote_volume_24h": quote_volume_24h,
            "spread_bps": spread_bps,
            "top_depth_notional": top_depth_notional,
            "product_tags": product_tags,
            "matched_block_tags": matched_block_tags,
            "contract": contract_entry if isinstance(contract_entry, dict) else {},
        }

    blocked_symbols = sorted(symbol for symbol, entry in entries.items() if bool(entry.get("blocked")))
    return entries, {
        "enabled": True,
        "min_24h_quote_volume": min_24h_quote_volume,
        "max_spread_bps": max_spread_bps,
        "min_top_depth_notional": min_top_depth_notional,
        "blocked_tags": sorted(blocked_tags),
        "contract_hard_block": contract_hard_block,
        "ticker_ok": not bool(ticker_error),
        "ticker_error": ticker_error,
        "product_ok": not bool(product_error),
        "product_error": product_error,
        "contract": contract_info,
        "scan_symbol_count": len(scan_symbols_norm),
        "entry_count": len(entries),
        "blocked_count": len(blocked_symbols),
        "blocked_symbols": blocked_symbols[:96],
    }


def _fetch_binance_product_map() -> dict[str, dict[str, object]]:
    url = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
    payload: object = {}
    for _ in range(2):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.load(resp)
            break
        except Exception:
            payload = {}
            time.sleep(0.5)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, object]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        market = str(item.get("s", "")).strip().upper()
        if not market:
            continue
        tags_raw = item.get("tags")
        tags: list[str] = []
        if isinstance(tags_raw, list):
            for tag in tags_raw:
                text = str(tag).strip()
                if text:
                    tags.append(text)
        out[market] = {
            "status": str(item.get("st", "")).strip().upper(),
            "tags": tags,
            "is_monitoring": any(str(tag).strip().lower() == "monitoring" for tag in tags),
        }
    return out


def _fetch_exchange_symbol_meta() -> dict[str, dict[str, object]]:
    payload = _public_json("/api/v3/exchangeInfo")
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        return {}
    out: dict[str, dict[str, object]] = {}
    for row in symbols:
        if not isinstance(row, dict):
            continue
        market = str(row.get("symbol", "")).strip().upper()
        if not market:
            continue
        permissions_raw = row.get("permissions")
        permissions: list[str] = []
        if isinstance(permissions_raw, list):
            for item in permissions_raw:
                val = str(item).strip().upper()
                if val:
                    permissions.append(val)
        out[market] = {
            "status": str(row.get("status", "")).strip().upper(),
            "baseAsset": str(row.get("baseAsset", "")).strip().upper(),
            "quoteAsset": str(row.get("quoteAsset", "")).strip().upper(),
            "isSpotTradingAllowed": bool(row.get("isSpotTradingAllowed", False)),
            "permissions": permissions,
        }
    return out


def _build_universe_policy(
    *,
    markets: list[str],
    quote_asset: str,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    now_ts = int(time.time())
    cache = _load_universe_policy_cache(UNIVERSE_POLICY_CACHE_FILE)
    monitoring_cache = cache.get("monitoring")
    if not isinstance(monitoring_cache, dict):
        monitoring_cache = {}
    problem_cache = cache.get("problems")
    if not isinstance(problem_cache, dict):
        problem_cache = {}

    force_refresh = _env_flag("ROTATION_UNIVERSE_POLICY_FORCE_REFRESH", "0")
    monitoring_max_age_sec = int(_env_hours("ROTATION_MONITORING_RECHECK_HOURS", 24.0 * 7.0) * 3600.0)
    problem_max_age_sec = int(_env_hours("ROTATION_PROBLEM_RECHECK_HOURS", 24.0 * 3.0) * 3600.0)

    monitoring_refresh_ts = int(cache.get("monitoring_refreshed_at_ts") or 0)
    problem_refresh_ts = int(cache.get("problem_refreshed_at_ts") or 0)
    need_monitoring_refresh = force_refresh or (now_ts - monitoring_refresh_ts > monitoring_max_age_sec)
    need_problem_refresh = force_refresh or (now_ts - problem_refresh_ts > problem_max_age_sec)
    if not need_monitoring_refresh:
        for market in markets:
            if market not in monitoring_cache:
                need_monitoring_refresh = True
                break
    if not need_problem_refresh:
        for market in markets:
            if market not in problem_cache:
                need_problem_refresh = True
                break

    monitoring_ok = True
    if need_monitoring_refresh:
        product_map = _fetch_binance_product_map()
        if product_map:
            monitoring_cache = {}
            for market in markets:
                info = product_map.get(market, {})
                tags = info.get("tags")
                if not isinstance(tags, list):
                    tags = []
                monitoring_cache[market] = {
                    "status": str(info.get("status", "")).strip().upper(),
                    "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
                    "is_monitoring": bool(info.get("is_monitoring", False)),
                }
            monitoring_refresh_ts = now_ts
        else:
            monitoring_ok = False

    problem_ok = True
    if need_problem_refresh:
        try:
            exchange_meta = _fetch_exchange_symbol_meta()
        except Exception:
            exchange_meta = {}
        if exchange_meta:
            problem_cache = {}
            quote_asset_norm = str(quote_asset or DEFAULT_QUOTE_ASSET).strip().upper()
            for market in markets:
                meta = exchange_meta.get(market)
                reasons: list[str] = []
                if not isinstance(meta, dict):
                    reasons.append("missing_exchange_symbol")
                    problem_cache[market] = {"is_problem": True, "reasons": reasons}
                    continue
                status = str(meta.get("status", "")).strip().upper()
                is_spot_allowed = bool(meta.get("isSpotTradingAllowed", False))
                row_quote = str(meta.get("quoteAsset", "")).strip().upper()
                permissions = meta.get("permissions")
                if not isinstance(permissions, list):
                    permissions = []
                permissions = [str(item).strip().upper() for item in permissions if str(item).strip()]
                if status != "TRADING":
                    reasons.append(f"status_{status.lower() or 'unknown'}")
                if row_quote and row_quote != quote_asset_norm:
                    reasons.append("quote_mismatch")
                if not is_spot_allowed:
                    reasons.append("spot_disabled")
                if permissions and "SPOT" not in permissions:
                    reasons.append("spot_permission_missing")
                problem_cache[market] = {
                    "is_problem": bool(reasons),
                    "reasons": reasons,
                    "status": status,
                }
            problem_refresh_ts = now_ts
        else:
            problem_ok = False

    policy_map: dict[str, dict[str, object]] = {}
    for market in markets:
        monitoring_info_raw = monitoring_cache.get(market) if isinstance(monitoring_cache, dict) else None
        monitoring_data_unknown = not isinstance(monitoring_info_raw, dict)
        monitoring_info = monitoring_info_raw if isinstance(monitoring_info_raw, dict) else {}
        if not isinstance(monitoring_info, dict):
            monitoring_info = {}
        problem_info_raw = problem_cache.get(market) if isinstance(problem_cache, dict) else None
        problem_data_unknown = not isinstance(problem_info_raw, dict)
        problem_info = problem_info_raw if isinstance(problem_info_raw, dict) else {}
        if not isinstance(problem_info, dict):
            problem_info = {}
        tags = monitoring_info.get("tags")
        if not isinstance(tags, list):
            tags = []
        reasons = problem_info.get("reasons")
        if not isinstance(reasons, list):
            reasons = []
        policy_data_unknown = monitoring_data_unknown or problem_data_unknown
        policy_map[market] = {
            "is_monitoring": bool(monitoring_info.get("is_monitoring", False)),
            "monitoring_tags": [str(tag).strip() for tag in tags if str(tag).strip()],
            "is_problem": bool(problem_info.get("is_problem", False)),
            "problem_reasons": [str(item).strip() for item in reasons if str(item).strip()],
            "monitoring_data_unknown": bool(monitoring_data_unknown),
            "problem_data_unknown": bool(problem_data_unknown),
            "policy_data_unknown": bool(policy_data_unknown),
        }

    cache_out: dict[str, object] = {
        "generated_at_ts": now_ts,
        "monitoring_refreshed_at_ts": monitoring_refresh_ts,
        "problem_refreshed_at_ts": problem_refresh_ts,
        "monitoring": monitoring_cache,
        "problems": problem_cache,
    }
    _save_universe_policy_cache(UNIVERSE_POLICY_CACHE_FILE, cache_out)
    info = {
        "monitoring_refresh_ok": monitoring_ok,
        "problem_refresh_ok": problem_ok,
        "monitoring_refreshed_at_ts": monitoring_refresh_ts,
        "problem_refreshed_at_ts": problem_refresh_ts,
        "monitoring_max_age_sec": monitoring_max_age_sec,
        "problem_max_age_sec": problem_max_age_sec,
        "force_refresh": force_refresh,
        "monitoring_data_unknown_total": sum(
            1 for market in markets if bool(policy_map.get(market, {}).get("monitoring_data_unknown", False))
        ),
        "problem_data_unknown_total": sum(
            1 for market in markets if bool(policy_map.get(market, {}).get("problem_data_unknown", False))
        ),
        "policy_data_unknown_total": sum(
            1 for market in markets if bool(policy_map.get(market, {}).get("policy_data_unknown", False))
        ),
    }
    return policy_map, info


def _get_klines(
    symbol: str,
    *,
    interval: str = KLINE_INTERVAL,
    target_bars: int | None = None,
) -> list[list[object]]:
    if target_bars is None:
        if interval == KLINE_INTERVAL:
            target = max(1, int(KLINE_LIMIT))
        else:
            target = max(1, int(MACRO_KLINE_LIMIT))
    else:
        target = max(1, int(target_bars))
    req_limit = max(1, min(int(KLINE_REQUEST_LIMIT), 1000))
    all_rows: list[list[object]] = []
    end_time_ms: int | None = None
    last_first_open_ms: int | None = None

    # Pull newest chunk first, then walk backwards with endTime until enough
    # bars are available (required for 7-13 day near-corridor estimation).
    while len(all_rows) < target:
        remaining = max(1, target - len(all_rows))
        limit = min(req_limit, remaining)
        params: dict[str, object] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        qs = urllib.parse.urlencode(params)
        payload = _public_json(f"/api/v3/klines?{qs}")
        if not isinstance(payload, list) or not payload:
            break
        chunk = [item for item in payload if isinstance(item, list) and len(item) >= 6]
        if not chunk:
            break

        first_open_ms = int(float(chunk[0][0]))
        if last_first_open_ms is not None and first_open_ms >= last_first_open_ms:
            break
        last_first_open_ms = first_open_ms

        all_rows = chunk + all_rows
        end_time_ms = first_open_ms - 1
        if len(chunk) < limit:
            break

    if len(all_rows) > target:
        all_rows = all_rows[-target:]
    return all_rows


def _parse_book_ticker(raw: object) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        bid = float(raw["bidPrice"])
        ask = float(raw["askPrice"])
        bid_qty = float(raw["bidQty"])
        ask_qty = float(raw["askQty"])
    except Exception:
        return None
    return {
        "bidPrice": bid,
        "askPrice": ask,
        "bidQty": bid_qty,
        "askQty": ask_qty,
    }


def _get_book_ticker_map() -> dict[str, dict[str, float]]:
    payload = _public_json("/api/v3/ticker/bookTicker")
    out: dict[str, dict[str, float]] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).upper().strip()
            if not symbol:
                continue
            parsed = _parse_book_ticker(item)
            if parsed is None:
                continue
            out[symbol] = parsed
        return out
    if isinstance(payload, dict):
        symbol = str(payload.get("symbol", "")).upper().strip()
        parsed = _parse_book_ticker(payload)
        if symbol and parsed is not None:
            out[symbol] = parsed
    return out


def _load_env() -> None:
    load_env(str(REPO_ROOT / ".env"))


def _signed_get(path: str, params: dict[str, object] | None = None) -> object:
    key = os.environ.get("BINANCE_API_KEY", "")
    secret = os.environ.get("BINANCE_API_SECRET", "").encode()
    query = dict(params or {})
    query["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(query)
    sig = hmac.new(secret, qs.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"{BINANCE_BASE}{path}?{qs}&signature={sig}",
        headers={"X-MBX-APIKEY": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        msg = body.strip()
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                msg = str(payload.get("msg") or payload.get("message") or msg)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {path}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error {path}: {exc}") from exc


def _is_rate_limit_error(error_text: str) -> bool:
    text = str(error_text or "").strip().lower()
    if not text:
        return False
    return (
        "too much request weight" in text
        or "way too much request weight" in text
        or "ip banned until" in text
        or "-1003" in text
        or "http 429" in text
    )


def _load_previous_rows() -> list[dict[str, object]]:
    if not ACTIVE_FILE.exists():
        return []
    try:
        payload = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("all_rows")
    if not isinstance(rows, list):
        rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, object]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        out.append(item)
    return out


def _turn_count(series: list[float]) -> int:
    turns = 0
    prev_dir = 0
    for a, b in zip(series, series[1:]):
        direction = 1 if b > a else (-1 if b < a else 0)
        if direction and prev_dir and direction != prev_dir:
            turns += 1
        if direction:
            prev_dir = direction
    return turns


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v or 0.0) for v in values)
    if len(ordered) == 1:
        return float(ordered[0])
    q_clamped = max(0.0, min(1.0, float(q)))
    pos = q_clamped * float(len(ordered) - 1)
    lo = int(pos)
    hi = min(len(ordered) - 1, lo + 1)
    if hi <= lo:
        return float(ordered[lo])
    frac = pos - float(lo)
    return float(ordered[lo]) + ((float(ordered[hi]) - float(ordered[lo])) * frac)


def _adaptive_cycle_timing_stats(
    closes: list[float],
    *,
    enabled: bool,
    lookback_bars: int,
    extrema_window: int,
    min_half_cycle_bars: int,
    max_half_cycle_bars: int,
    min_turning_points: int,
    min_swing_bps: float,
    min_confidence: float,
) -> dict[str, float | str | bool]:
    default: dict[str, float | str | bool] = {
        "enabled": bool(enabled),
        "fit_ok": False,
        "confidence": 0.0,
        "half_cycle_bars": 0.0,
        "full_cycle_bars": 0.0,
        "half_cycle_iqr_bars": 0.0,
        "turning_points": 0.0,
        "half_intervals": 0.0,
        "swing_median_bps": 0.0,
        "bars_since_last_pivot": 0.0,
        "window_low_bars": 0.0,
        "window_high_bars": 0.0,
        "window_active": False,
        "phase_progress": 0.0,
        "next_window_start_bars": 0.0,
        "next_window_end_bars": 0.0,
        "last_pivot_type": "",
    }
    if not enabled:
        return default
    if len(closes) < 32:
        return default

    n = min(len(closes), max(96, int(lookback_bars)))
    segment = [float(v or 0.0) for v in closes[-n:] if float(v or 0.0) > 0.0]
    if len(segment) < 32:
        return default
    window = max(2, int(extrema_window))
    if len(segment) < ((window * 2) + 6):
        return default

    min_pivot_gap = max(2, int(max(2, int(min_half_cycle_bars)) * 0.30))
    min_transition_swing_bps = max(1.0, float(min_swing_bps) * 0.60)

    # Lightweight smoothing to reduce micro-noise without delaying pivots too much.
    smooth: list[float] = []
    for idx in range(len(segment)):
        start = max(0, idx - 2)
        end = min(len(segment), idx + 3)
        span = segment[start:end]
        smooth.append(sum(span) / float(max(1, len(span))))

    pivot_candidates: list[tuple[int, str, float]] = []
    for idx in range(window, len(smooth) - window):
        val = float(smooth[idx] or 0.0)
        left = smooth[idx - window : idx]
        right = smooth[idx + 1 : idx + 1 + window]
        if not left or not right:
            continue
        is_trough = (
            all(val <= point for point in left)
            and all(val <= point for point in right)
            and (val < min(left) or val < min(right))
        )
        is_peak = (
            all(val >= point for point in left)
            and all(val >= point for point in right)
            and (val > max(left) or val > max(right))
        )
        if is_trough == is_peak:
            continue
        pivot_candidates.append((idx, "trough" if is_trough else "peak", float(segment[idx])))

    if not pivot_candidates:
        return default

    # Collapse consecutive same-type pivots: keep the more extreme one.
    pivots: list[list[float | int | str]] = []
    for idx, pivot_type, price in pivot_candidates:
        if not pivots:
            pivots.append([idx, pivot_type, price])
            continue
        last_idx = int(pivots[-1][0])
        last_type = str(pivots[-1][1])
        last_price = float(pivots[-1][2])
        if pivot_type == last_type:
            replace = (
                (pivot_type == "trough" and price < last_price)
                or (pivot_type == "peak" and price > last_price)
            )
            if replace:
                pivots[-1] = [idx, pivot_type, price]
            continue
        if idx > last_idx:
            pivots.append([idx, pivot_type, price])

    # Filter weak/too-close alternating pivots; otherwise noisy symbols create
    # fake 3-10 bar half-cycles that never represent the actionable cycle.
    filtered_pivots: list[list[float | int | str]] = []
    for idx_raw, pivot_type_raw, price_raw in pivots:
        idx = int(idx_raw)
        pivot_type = str(pivot_type_raw)
        price = float(price_raw)
        if not filtered_pivots:
            filtered_pivots.append([idx, pivot_type, price])
            continue
        last_idx = int(filtered_pivots[-1][0])
        last_type = str(filtered_pivots[-1][1])
        last_price = float(filtered_pivots[-1][2])
        if pivot_type == last_type:
            replace = (
                (pivot_type == "trough" and price < last_price)
                or (pivot_type == "peak" and price > last_price)
            )
            if replace:
                filtered_pivots[-1] = [idx, pivot_type, price]
            continue
        dist = float(max(0, idx - last_idx))
        move_bps = 0.0
        if last_price > 0.0 and price > 0.0:
            move_bps = abs(((price / last_price) - 1.0) * 10000.0)
        if dist < float(min_pivot_gap) or move_bps < min_transition_swing_bps:
            continue
        filtered_pivots.append([idx, pivot_type, price])

    pivots = filtered_pivots
    if len(pivots) < max(4, int(min_turning_points)):
        return default

    half_intervals: list[float] = []
    swing_bps_values: list[float] = []
    for idx in range(len(pivots) - 1):
        a_idx = int(pivots[idx][0])
        b_idx = int(pivots[idx + 1][0])
        dist = float(max(0, b_idx - a_idx))
        if dist >= float(min_pivot_gap):
            half_intervals.append(dist)
        a_price = float(pivots[idx][2])
        b_price = float(pivots[idx + 1][2])
        if a_price > 0.0 and b_price > 0.0:
            swing_bps_values.append(abs(((b_price / a_price) - 1.0) * 10000.0))

    if len(half_intervals) < 3:
        return default

    full_intervals: list[float] = []
    for idx in range(len(pivots) - 2):
        if str(pivots[idx][1]) != str(pivots[idx + 2][1]):
            continue
        full_dist = float(max(0, int(pivots[idx + 2][0]) - int(pivots[idx][0])))
        if full_dist > 0.0:
            full_intervals.append(full_dist)

    half_cycle_bars = _quantile(half_intervals, 0.5)
    half_q25 = _quantile(half_intervals, 0.25)
    half_q75 = _quantile(half_intervals, 0.75)
    half_cycle_iqr_bars = max(0.0, half_q75 - half_q25)
    full_cycle_bars = _quantile(full_intervals, 0.5) if full_intervals else (half_cycle_bars * 2.0)
    swing_median_bps = _quantile(swing_bps_values, 0.5) if swing_bps_values else 0.0

    bars_since_last_pivot = float((len(segment) - 1) - int(pivots[-1][0]))
    window_low_bars = max(float(max(1, int(min_half_cycle_bars))), half_cycle_bars * 0.55)
    window_high_bars = min(float(max(2, int(max_half_cycle_bars))), half_cycle_bars * 1.80)
    if window_high_bars <= window_low_bars:
        window_high_bars = window_low_bars + 1.0
    phase_progress = (
        (bars_since_last_pivot / half_cycle_bars) if half_cycle_bars > 0.0 else 0.0
    )
    window_active = window_low_bars <= bars_since_last_pivot <= window_high_bars

    sample_score = max(0.0, min(1.0, float(len(half_intervals)) / 8.0))
    variability_ratio = (
        (half_cycle_iqr_bars / half_cycle_bars) if half_cycle_bars > 0.0 else 1.0
    )
    stability_score = max(0.0, min(1.0, 1.0 - min(1.0, variability_ratio)))
    swing_score = max(
        0.0,
        min(1.0, swing_median_bps / max(1.0, float(min_swing_bps) * 2.0)),
    )
    confidence = (0.42 * sample_score) + (0.38 * stability_score) + (0.20 * swing_score)

    half_range_ok = (
        half_cycle_bars >= float(max(1, int(min_half_cycle_bars)))
        and half_cycle_bars <= float(max(2, int(max_half_cycle_bars)))
    )
    swing_ok = swing_median_bps >= float(min_swing_bps)
    fit_ok = (
        half_range_ok
        and swing_ok
        and confidence >= float(min_confidence)
    )

    return {
        "enabled": True,
        "fit_ok": bool(fit_ok),
        "confidence": float(confidence),
        "half_cycle_bars": float(half_cycle_bars),
        "full_cycle_bars": float(full_cycle_bars),
        "half_cycle_iqr_bars": float(half_cycle_iqr_bars),
        "turning_points": float(len(pivots)),
        "half_intervals": float(len(half_intervals)),
        "swing_median_bps": float(swing_median_bps),
        "bars_since_last_pivot": float(bars_since_last_pivot),
        "window_low_bars": float(window_low_bars),
        "window_high_bars": float(window_high_bars),
        "window_active": bool(window_active),
        "phase_progress": float(phase_progress),
        "next_window_start_bars": float(max(0.0, window_low_bars - bars_since_last_pivot)),
        "next_window_end_bars": float(max(0.0, window_high_bars - bars_since_last_pivot)),
        "last_pivot_type": str(pivots[-1][1]),
    }


def _down_streak(closes: list[float], max_bars: int = 4) -> int:
    if len(closes) < 2:
        return 0
    streak = 0
    pairs = zip(reversed(closes[:-1]), reversed(closes[1:]))
    for prev_close, last_close in pairs:
        if streak >= max_bars:
            break
        if last_close < prev_close:
            streak += 1
            continue
        break
    return streak


def _last_still_dumping_event(closes: list[float], lookback_bars: int = 36) -> tuple[int, int]:
    n = len(closes)
    if n < 8:
        return 999, -1
    span = max(8, int(lookback_bars))
    start = max(6, n - span)
    last_idx = -1
    for idx in range(start, n):
        view = closes[: idx + 1]
        if _down_streak(view, max_bars=4) >= 3 and _return_bps(view, 6) <= -40.0:
            last_idx = idx
    if last_idx < 0:
        return 999, -1
    return (n - 1) - last_idx, last_idx


def _up_streak(closes: list[float], max_bars: int = 4) -> int:
    if len(closes) < 2:
        return 0
    streak = 0
    pairs = zip(reversed(closes[:-1]), reversed(closes[1:]))
    for prev_close, last_close in pairs:
        if streak >= max_bars:
            break
        if last_close > prev_close:
            streak += 1
            continue
        break
    return streak


def _bars_since_last_low(closes: list[float]) -> int:
    if not closes:
        return 999
    low = min(closes)
    last_idx = 0
    for idx, value in enumerate(closes):
        if value <= low:
            last_idx = idx
    return (len(closes) - 1) - last_idx


def _latest_swing_low(closes: list[float], lookback: int = 24, left: int = 2, right: int = 2) -> tuple[int, float]:
    if len(closes) < (left + right + 1):
        return -1, 0.0
    start = max(left, len(closes) - lookback)
    end = len(closes) - right
    best_idx = -1
    for idx in range(start, end):
        current = closes[idx]
        if current <= 0.0:
            continue
        left_window = closes[idx - left : idx]
        right_window = closes[idx + 1 : idx + 1 + right]
        if len(left_window) < left or len(right_window) < right:
            continue
        if all(current <= value for value in left_window) and all(current < value for value in right_window):
            best_idx = idx
    if best_idx >= 0:
        return best_idx, float(closes[best_idx])
    tail = closes[-lookback:]
    if not tail:
        return -1, 0.0
    local_idx = min(range(len(tail)), key=lambda idx: tail[idx])
    best_idx = len(closes) - len(tail) + local_idx
    return best_idx, float(closes[best_idx])


def _higher_low_after_swing(closes: list[float], swing_idx: int) -> bool:
    if swing_idx < 0 or swing_idx >= len(closes) - 2:
        return False
    swing_low = float(closes[swing_idx] or 0.0)
    if swing_low <= 0.0:
        return False
    post = [float(value or 0.0) for value in closes[swing_idx + 1 :]]
    if len(post) < 2:
        return False
    if max(post) <= swing_low:
        return False
    if min(post) <= swing_low:
        return False
    return post[-1] >= post[-2]


def _window_pos_pct(closes: list[float], bars: int) -> float:
    if not closes:
        return 50.0
    n = min(len(closes), max(2, int(bars)))
    segment = closes[-n:]
    low = min(segment)
    high = max(segment)
    if high <= low:
        return 50.0
    last = segment[-1]
    return max(0.0, min(100.0, ((last - low) / (high - low)) * 100.0))


def _window_min_return_bps(closes: list[float], bars: int, span_bars: int) -> float:
    if not closes:
        return 0.0
    span = max(1, int(span_bars))
    n = min(len(closes), max(span + 1, int(bars)))
    segment = closes[-n:]
    out = 0.0
    for idx in range(span, len(segment)):
        base = float(segment[idx - span] or 0.0)
        last = float(segment[idx] or 0.0)
        if base <= 0.0:
            continue
        ret_bps = ((last / base) - 1.0) * 10000.0
        if ret_bps < out:
            out = ret_bps
    return out


def _window_pos_pct_excluding_crash_events(
    closes: list[float],
    bars: int,
    *,
    crash_span_bars: int,
    crash_drop_bps: float,
) -> tuple[float, float]:
    if not closes:
        return 50.0, 0.0
    n = min(len(closes), max(2, int(bars)))
    segment = [float(v or 0.0) for v in closes[-n:]]
    span = max(1, int(crash_span_bars))
    threshold = float(crash_drop_bps)
    crash_mask = [False] * len(segment)
    crash_events = 0
    last_marked_idx = -999999
    for idx in range(span, len(segment)):
        base = segment[idx - span]
        last = segment[idx]
        if base <= 0.0:
            continue
        ret_bps = ((last / base) - 1.0) * 10000.0
        if ret_bps <= threshold:
            start = max(0, idx - span)
            end = min(len(segment), idx + 1)
            if start > last_marked_idx:
                crash_events += 1
            for j in range(start, end):
                crash_mask[j] = True
            last_marked_idx = max(last_marked_idx, end - 1)
    filtered = [value for idx, value in enumerate(segment) if value > 0.0 and not crash_mask[idx]]
    if len(filtered) < max(20, span + 2):
        filtered = [value for value in segment if value > 0.0]
    if len(filtered) < 2:
        return _window_pos_pct(segment, len(segment)), float(crash_events)
    return _window_pos_pct(filtered, len(filtered)), float(crash_events)


def _window_width_pct(closes: list[float], bars: int) -> float:
    if not closes:
        return 0.0
    n = min(len(closes), max(2, int(bars)))
    segment = closes[-n:]
    low = min(segment)
    high = max(segment)
    if low <= 0.0 or high <= low:
        return 0.0
    return ((high / low) - 1.0) * 100.0


def _local_trough_indices(
    closes: list[float],
    left: int = 4,
    right: int = 4,
    min_gap: int = 6,
    min_excursion_bps: float = 18.0,
) -> list[int]:
    n = len(closes)
    if n < (left + right + 1):
        return []
    out: list[int] = []
    for idx in range(left, n - right):
        current = float(closes[idx] or 0.0)
        if current <= 0.0:
            continue
        left_window = [float(v or 0.0) for v in closes[idx - left : idx]]
        right_window = [float(v or 0.0) for v in closes[idx + 1 : idx + 1 + right]]
        if len(left_window) < left or len(right_window) < right:
            continue
        if not (all(current <= value for value in left_window) and all(current < value for value in right_window)):
            continue
        left_peak = max(left_window) if left_window else current
        right_peak = max(right_window) if right_window else current
        excursion_left_bps = ((left_peak / current) - 1.0) * 10000.0 if current > 0.0 else 0.0
        excursion_right_bps = ((right_peak / current) - 1.0) * 10000.0 if current > 0.0 else 0.0
        excursion_bps = min(excursion_left_bps, excursion_right_bps)
        if excursion_bps < max(0.0, float(min_excursion_bps)):
            continue
        if out and (idx - out[-1]) < max(1, int(min_gap)):
            prev_idx = out[-1]
            if current <= float(closes[prev_idx] or 0.0):
                out[-1] = idx
            continue
        out.append(idx)
    return out


def _local_peak_indices(
    closes: list[float],
    left: int = 4,
    right: int = 4,
    min_gap: int = 6,
    min_excursion_bps: float = 18.0,
) -> list[int]:
    n = len(closes)
    if n < (left + right + 1):
        return []
    out: list[int] = []
    for idx in range(left, n - right):
        current = float(closes[idx] or 0.0)
        if current <= 0.0:
            continue
        left_window = [float(v or 0.0) for v in closes[idx - left : idx]]
        right_window = [float(v or 0.0) for v in closes[idx + 1 : idx + 1 + right]]
        if len(left_window) < left or len(right_window) < right:
            continue
        if not (all(current >= value for value in left_window) and all(current > value for value in right_window)):
            continue
        left_valley = min(left_window) if left_window else current
        right_valley = min(right_window) if right_window else current
        excursion_left_bps = (1.0 - (left_valley / current)) * 10000.0 if current > 0.0 else 0.0
        excursion_right_bps = (1.0 - (right_valley / current)) * 10000.0 if current > 0.0 else 0.0
        excursion_bps = min(excursion_left_bps, excursion_right_bps)
        if excursion_bps < max(0.0, float(min_excursion_bps)):
            continue
        if out and (idx - out[-1]) < max(1, int(min_gap)):
            prev_idx = out[-1]
            if current >= float(closes[prev_idx] or 0.0):
                out[-1] = idx
            continue
        out.append(idx)
    return out


def _micro_valley_context_position_pct(
    closes: list[float],
    *,
    near_closes: list[float] | None = None,
    near_bar_minutes: float = BAR_MINUTES,
    fallback_pos_pct: float = 50.0,
) -> dict[str, float | str | bool]:
    def _clamp_pct(value: float) -> float:
        return max(0.0, min(100.0, float(value)))

    try:
        fallback = float(fallback_pos_pct)
    except Exception:
        fallback = 50.0
    if not math.isfinite(fallback):
        fallback = 50.0
    fallback = _clamp_pct(fallback)

    near_minutes = max(1.0, float(near_bar_minutes or BAR_MINUTES))
    near_series = [float(v or 0.0) for v in (near_closes or closes) if float(v or 0.0) > 0.0]
    if not near_series:
        near_series = [float(v or 0.0) for v in closes if float(v or 0.0) > 0.0]
    if len(near_series) < 3:
        return {
            "pos_pct": fallback,
            "mode": "fallback",
            "fallback_used": True,
            "trough_count": 0.0,
            "peak_count": 0.0,
            "anchor_low": 0.0,
            "anchor_high": 0.0,
            "bars_since_anchor": 0.0,
        }

    if near_minutes >= 30.0:
        pivot_left = 6
        pivot_right = 6
        pivot_min_gap = max(4, int(round((24.0 * 60.0) / near_minutes)))
        pivot_min_excursion_bps = 60.0
    else:
        pivot_left = 4
        pivot_right = 4
        pivot_min_gap = max(6, int(round(30.0 / near_minutes)))
        pivot_min_excursion_bps = 18.0

    trough_indices = _local_trough_indices(
        near_series,
        left=pivot_left,
        right=pivot_right,
        min_gap=pivot_min_gap,
        min_excursion_bps=pivot_min_excursion_bps,
    )
    peak_indices = _local_peak_indices(
        near_series,
        left=pivot_left,
        right=pivot_right,
        min_gap=pivot_min_gap,
        min_excursion_bps=pivot_min_excursion_bps,
    )

    def _segment_position(segment: list[float]) -> tuple[float, float, float] | None:
        if len(segment) < 2:
            return None
        low = min(segment)
        high = max(segment)
        last = segment[-1]
        if low <= 0.0 or high <= low:
            return None
        pos = _clamp_pct(((last - low) / (high - low)) * 100.0)
        return pos, float(low), float(high)

    if trough_indices:
        trough_idx = int(trough_indices[-1])
        if 0 <= trough_idx < len(near_series) - 1:
            segment = near_series[trough_idx:]
            segment_pos = _segment_position(segment)
            if segment_pos is not None:
                pos, low, high = segment_pos
                return {
                    "pos_pct": pos,
                    "mode": "valley_to_peak",
                    "fallback_used": False,
                    "trough_count": float(len(trough_indices)),
                    "peak_count": float(len(peak_indices)),
                    "anchor_low": low,
                    "anchor_high": high,
                    "bars_since_anchor": float((len(near_series) - 1) - trough_idx),
                }

    if len(peak_indices) >= 2:
        anchor_idx = int(peak_indices[-2])
        if 0 <= anchor_idx < len(near_series) - 1:
            segment = near_series[anchor_idx:]
            segment_pos = _segment_position(segment)
            if segment_pos is not None:
                pos, low, high = segment_pos
                return {
                    "pos_pct": pos,
                    "mode": "peak_to_peak",
                    "fallback_used": False,
                    "trough_count": float(len(trough_indices)),
                    "peak_count": float(len(peak_indices)),
                    "anchor_low": low,
                    "anchor_high": high,
                    "bars_since_anchor": float((len(near_series) - 1) - anchor_idx),
                }

    return {
        "pos_pct": fallback,
        "mode": "fallback",
        "fallback_used": True,
        "trough_count": float(len(trough_indices)),
        "peak_count": float(len(peak_indices)),
        "anchor_low": float(min(near_series)),
        "anchor_high": float(max(near_series)),
        "bars_since_anchor": float(len(near_series) - 1),
    }


def _adaptive_corridor_pos_pct(
    closes: list[float],
    *,
    near_closes: list[float] | None = None,
    near_bar_minutes: float = BAR_MINUTES,
    macro_closes: list[float] | None = None,
) -> dict[str, float]:
    if not closes and not near_closes:
        return {
            "pos_pct": 50.0,
            "base_pos_pct": 50.0,
            "anchor_pos_pct": 50.0,
            "near_pos_pct": 50.0,
            "mid_pos_pct": 50.0,
            "long_pos_pct": 50.0,
            "near_width_pct": 0.0,
            "trend_bias": 0.0,
            "trend_shift_pct": 0.0,
            "cycle_bars": 24.0,
            "cycle_gap_median_bars": 24.0,
            "trough_count": 0.0,
            "near_bars": 24.0,
            "mid_bars": float(MID_RANGE_BARS),
            "long_bars": float(LONG_RANGE_BARS),
        }
    near_minutes = max(1.0, float(near_bar_minutes or BAR_MINUTES))
    near_series = [float(v or 0.0) for v in (near_closes or closes) if float(v or 0.0) > 0.0]
    if not near_series:
        near_series = [float(v or 0.0) for v in closes if float(v or 0.0) > 0.0]
    if not near_series:
        return {
            "pos_pct": 50.0,
            "base_pos_pct": 50.0,
            "anchor_pos_pct": 50.0,
            "near_pos_pct": 50.0,
            "mid_pos_pct": 50.0,
            "long_pos_pct": 50.0,
            "near_width_pct": 0.0,
            "trend_bias": 0.0,
            "trend_shift_pct": 0.0,
            "cycle_bars": 24.0,
            "cycle_gap_median_bars": 24.0,
            "trough_count": 0.0,
            "near_bars": 24.0,
            "mid_bars": float(MID_RANGE_BARS),
            "long_bars": float(LONG_RANGE_BARS),
        }

    near_min_samples = max(2, int(round((NEAR_RANGE_MIN_DAYS * 24.0 * 60.0) / near_minutes)))
    near_max_samples = max(
        near_min_samples,
        int(round((NEAR_RANGE_MAX_DAYS * 24.0 * 60.0) / near_minutes)),
    )
    n_near = len(near_series)
    base_pos_pct = _window_pos_pct(near_series, n_near)

    if near_minutes >= 30.0:
        trough_left = 6
        trough_right = 6
        trough_min_gap = max(4, int(round((24.0 * 60.0) / near_minutes)))
        trough_min_excursion_bps = 60.0
    else:
        trough_left = 4
        trough_right = 4
        trough_min_gap = max(6, int(round(30.0 / near_minutes)))
        trough_min_excursion_bps = 18.0

    trough_indices = _local_trough_indices(
        near_series,
        left=trough_left,
        right=trough_right,
        min_gap=trough_min_gap,
        min_excursion_bps=trough_min_excursion_bps,
    )
    gaps: list[int] = []
    if len(trough_indices) >= 2:
        for a, b in zip(trough_indices[:-1], trough_indices[1:]):
            gap = int(b - a)
            if gap > 0:
                gaps.append(gap)
    if gaps:
        ordered = sorted(gaps[-8:])
        gap_median = float(ordered[len(ordered) // 2])
    else:
        gap_median = float(max(near_min_samples, min(near_max_samples, max(8, n_near // 6))))

    near_samples = int(max(near_min_samples, min(near_max_samples, round(gap_median))))
    near_samples = min(n_near, max(2, near_samples))
    cycle_samples = int(max(8, min(near_max_samples, round(gap_median))))

    near_scale_to_5m = near_minutes / BAR_MINUTES
    near_bars = int(round(float(near_samples) * near_scale_to_5m))
    cycle_bars = int(round(float(cycle_samples) * near_scale_to_5m))
    effective_gap_median = float(gap_median) * near_scale_to_5m

    near_pos_pct = _window_pos_pct(near_series, near_samples)
    near_width_pct = _window_width_pct(near_series, near_samples)

    macro_series = [float(v or 0.0) for v in (macro_closes or []) if float(v or 0.0) > 0.0]
    macro_sample_minutes = 24.0 * 60.0
    if len(macro_series) < 2:
        macro_series = list(near_series)
        macro_sample_minutes = near_minutes

    mid_target_samples = max(2, int(round((MID_RANGE_DAYS * 24.0 * 60.0) / macro_sample_minutes)))
    long_target_samples = max(
        mid_target_samples,
        int(round((LONG_RANGE_DAYS * 24.0 * 60.0) / macro_sample_minutes)),
    )
    mid_samples = max(2, min(len(macro_series), mid_target_samples))
    long_samples = max(mid_samples, min(len(macro_series), long_target_samples))

    mid_pos_pct = _window_pos_pct(macro_series, mid_samples)
    long_pos_pct = _window_pos_pct(macro_series, long_samples)

    macro_scale_to_5m = macro_sample_minutes / BAR_MINUTES
    mid_bars = int(round(float(mid_samples) * macro_scale_to_5m))
    long_bars = int(round(float(long_samples) * macro_scale_to_5m))
    mid_bars = max(near_bars, mid_bars)
    long_bars = max(mid_bars, long_bars)

    mid_ret_pct = _return_bps(macro_series, max(1, mid_samples - 1)) / 100.0
    long_ret_pct = _return_bps(macro_series, max(1, long_samples - 1)) / 100.0
    trend_bias = ((mid_ret_pct / 4.0) * 0.60) + ((long_ret_pct / 8.0) * 0.40)
    trend_bias = max(-1.0, min(1.0, trend_bias))
    trend_shift_pct = -trend_bias * 8.0

    correction_mid = mid_pos_pct - near_pos_pct
    correction_long = long_pos_pct - near_pos_pct
    correction_raw = (correction_mid * 0.60) + (correction_long * 0.40)
    correction_with_trend = correction_raw + trend_shift_pct

    macro_divergence = (abs(correction_mid) * 0.60) + (abs(correction_long) * 0.40)
    max_shift_from_near = max(
        6.0,
        min(
            18.0,
            5.0 + (near_width_pct * 1.1) + (macro_divergence * 0.35),
        ),
    )
    bounded_shift = max(-max_shift_from_near, min(max_shift_from_near, correction_with_trend))
    pos_pct = max(0.0, min(100.0, near_pos_pct + bounded_shift))
    anchor_pos_pct = near_pos_pct + correction_raw

    return {
        "pos_pct": pos_pct,
        "base_pos_pct": base_pos_pct,
        "anchor_pos_pct": anchor_pos_pct,
        "near_pos_pct": near_pos_pct,
        "mid_pos_pct": mid_pos_pct,
        "long_pos_pct": long_pos_pct,
        "near_width_pct": near_width_pct,
        "trend_bias": trend_bias,
        "trend_shift_pct": trend_shift_pct,
        "max_shift_from_base_pct": max_shift_from_near,
        "cycle_bars": float(max(16, min(NEAR_RANGE_MAX_BARS, cycle_bars))),
        "cycle_gap_median_bars": float(effective_gap_median),
        "trough_count": float(len(trough_indices)),
        "near_bars": float(near_bars),
        "mid_bars": float(mid_bars),
        "long_bars": float(long_bars),
    }


def _net_range_step_after_fees_pct(width_pct: float, step_fraction: float, roundtrip_fee_bps: float) -> float:
    gross_step_pct = max(0.0, float(width_pct or 0.0)) * max(0.0, float(step_fraction or 0.0))
    fee_pct = max(0.0, float(roundtrip_fee_bps or 0.0)) / 100.0
    return gross_step_pct - fee_pct


def _cycle_fitness_stats(
    closes: list[float],
    *,
    lookback_bars: int,
    entry_pct: float,
    exit_pct: float,
    max_bars_to_exit: int,
) -> dict[str, float]:
    if len(closes) < 12:
        return {
            "attempts": 0.0,
            "completed_cycles": 0.0,
            "failed_cycles": 0.0,
            "success_rate": 0.0,
            "median_bars_to_exit": 0.0,
            "last_cycle_bars_ago": 9999.0,
        }
    n = min(len(closes), max(12, int(lookback_bars)))
    segment = [float(v or 0.0) for v in closes[-n:]]
    low = min(segment)
    high = max(segment)
    if high <= low or low <= 0.0:
        return {
            "attempts": 0.0,
            "completed_cycles": 0.0,
            "failed_cycles": 0.0,
            "success_rate": 0.0,
            "median_bars_to_exit": 0.0,
            "last_cycle_bars_ago": 9999.0,
        }
    pos_series = [((px - low) / (high - low)) * 100.0 for px in segment]
    max_hold = max(1, int(max_bars_to_exit))

    attempts = 0
    completed = 0
    failed = 0
    durations: list[int] = []
    last_cycle_idx = -1

    armed = False
    start_idx = -1
    prev_pos = pos_series[0]
    for idx, pos in enumerate(pos_series):
        if not armed:
            crossed_down = prev_pos > entry_pct and pos <= entry_pct
            first_bar_touch = idx == 0 and pos <= entry_pct
            if crossed_down or first_bar_touch:
                armed = True
                start_idx = idx
                attempts += 1
        else:
            if pos >= exit_pct:
                bars = max(1, idx - start_idx)
                completed += 1
                durations.append(bars)
                last_cycle_idx = idx
                armed = False
                start_idx = -1
            elif (idx - start_idx) > max_hold:
                failed += 1
                armed = False
                start_idx = -1
        prev_pos = pos

    if attempts <= 0:
        success_rate = 0.0
    else:
        success_rate = float(completed) / float(attempts)
    if durations:
        ordered = sorted(durations)
        median = ordered[len(ordered) // 2]
    else:
        median = 0
    last_cycle_bars_ago = float((len(pos_series) - 1) - last_cycle_idx) if last_cycle_idx >= 0 else 9999.0
    return {
        "attempts": float(attempts),
        "completed_cycles": float(completed),
        "failed_cycles": float(failed),
        "success_rate": success_rate,
        "median_bars_to_exit": float(median),
        "last_cycle_bars_ago": last_cycle_bars_ago,
    }


def _staircase_stats(closes: list[float], lookback_bars: int = 18) -> dict[str, float]:
    if len(closes) < 4:
        return {
            "positive_share": 0.0,
            "pullback_count": 0.0,
            "turns": 0.0,
            "max_negative_bar_bps": 0.0,
            "max_pullback_run_bps": 0.0,
            "net_bps": 0.0,
            "mean_up_bps": 0.0,
            "mean_down_bps": 0.0,
        }
    n = min(len(closes), max(4, int(lookback_bars) + 1))
    segment = [float(value or 0.0) for value in closes[-n:]]
    returns_bps: list[float] = []
    positive_moves = 0
    negative_moves = 0
    total_up_bps = 0.0
    total_down_bps = 0.0
    max_negative_bar_bps = 0.0
    max_pullback_run_bps = 0.0
    current_pullback_run_bps = 0.0
    pullback_count = 0
    in_pullback = False
    for prev_close, last_close in zip(segment, segment[1:]):
        if prev_close <= 0.0 or last_close <= 0.0:
            move_bps = 0.0
        else:
            move_bps = ((last_close / prev_close) - 1.0) * 10000.0
        returns_bps.append(move_bps)
        if move_bps > 0.0:
            positive_moves += 1
            total_up_bps += move_bps
            current_pullback_run_bps = 0.0
            in_pullback = False
        elif move_bps < 0.0:
            negative_moves += 1
            down_bps = -move_bps
            total_down_bps += down_bps
            max_negative_bar_bps = max(max_negative_bar_bps, down_bps)
            current_pullback_run_bps += down_bps
            max_pullback_run_bps = max(max_pullback_run_bps, current_pullback_run_bps)
            if not in_pullback:
                pullback_count += 1
                in_pullback = True
        else:
            current_pullback_run_bps = 0.0
            in_pullback = False
    directional_moves = positive_moves + negative_moves
    positive_share = (
        (positive_moves / float(directional_moves)) if directional_moves > 0 else 0.0
    )
    net_bps = (
        ((segment[-1] / segment[0]) - 1.0) * 10000.0
        if segment[0] > 0.0 and segment[-1] > 0.0
        else 0.0
    )
    mean_up_bps = total_up_bps / float(positive_moves) if positive_moves > 0 else 0.0
    mean_down_bps = total_down_bps / float(negative_moves) if negative_moves > 0 else 0.0
    return {
        "positive_share": positive_share,
        "pullback_count": float(pullback_count),
        "turns": float(_turn_count(segment)),
        "max_negative_bar_bps": max_negative_bar_bps,
        "max_pullback_run_bps": max_pullback_run_bps,
        "net_bps": net_bps,
        "mean_up_bps": mean_up_bps,
        "mean_down_bps": mean_down_bps,
    }


def _strict_valley_context(
    *,
    pos_6h_pct: float,
    pos_24h_pct: float,
    long_up_hot: bool,
    down_structure: bool,
) -> bool:
    return (
        pos_6h_pct <= 44.0
        and pos_24h_pct <= 57.0
        and not long_up_hot
        and not down_structure
    )


def _relaxed_valley_context(
    *,
    pos_6h_pct: float,
    pos_24h_pct: float,
    long_up_hot: bool,
    down_structure: bool,
    macro_down_context: bool,
    falling_now: bool,
    spread_bps: float,
    structure_phase: str,
    recent_rebound_ready: bool,
    post_dump_recovery_ready: bool,
    previous_selloff: bool,
    rebound_from_30m_low_bps: float,
    rebound_from_60m_low_bps: float,
    bars_since_30m_low: int,
    bars_since_swing_low: int,
) -> bool:
    if long_up_hot or down_structure or falling_now:
        return False
    if spread_bps > 18.5:
        return False
    if structure_phase not in {"bottom", "lift_off", "range"}:
        return False
    if pos_24h_pct > 64.0 or pos_6h_pct > 66.0:
        return False
    if macro_down_context and not post_dump_recovery_ready:
        return False
    fresh_low = min(int(bars_since_30m_low), int(bars_since_swing_low)) <= 8
    if not fresh_low:
        return False
    if recent_rebound_ready or post_dump_recovery_ready:
        return True
    if not previous_selloff:
        return False
    return rebound_from_30m_low_bps >= 12.0 and rebound_from_60m_low_bps >= 32.0


def _fresh_late_rebound_override(
    *,
    active_leg: str,
    pos_24h_pct: float,
    spread_bps: float,
    bars_since_30m_low: int,
    ret10_bps: float,
    ret15_bps: float,
) -> bool:
    return (
        active_leg == "rise"
        and pos_24h_pct <= 24.0
        and spread_bps <= 14.0
        and int(bars_since_30m_low) <= 6
        and ret10_bps >= 4.0
        and ret15_bps >= 12.0
    )


def _fresh_liftoff_bottom_candidate(
    *,
    structure_phase: str,
    active_leg: str,
    pos_24h_pct: float,
    spread_bps: float,
    bars_since_30m_low: int,
    ret10_bps: float,
    ret15_bps: float,
    ret60_bps: float,
    previous_selloff: bool,
    in_valley_context: bool,
    macro_down_context: bool,
) -> bool:
    return (
        structure_phase == "lift_off"
        and active_leg == "rise"
        and pos_24h_pct <= 22.0
        and spread_bps <= 13.0
        and int(bars_since_30m_low) <= 6
        and ret10_bps >= 4.0
        and ret15_bps >= 16.0
        and ret60_bps >= 20.0
        and previous_selloff
        and in_valley_context
        and not macro_down_context
    )


def main() -> None:
    _load_env()
    args = _parse_args()
    if str(args.setup_mode or "").strip():
        os.environ["ROTATION_SETUP_MODE"] = str(args.setup_mode).strip()
    selector_mode = _selector_mode()
    strict_bottom_only = selector_mode == "bottom_strict"
    trend_only = selector_mode == "trend"
    quote_asset = str(args.quote_asset or DEFAULT_QUOTE_ASSET).strip().upper() or DEFAULT_QUOTE_ASSET
    try:
        selected_target_size = int(
            os.environ.get("ROTATION_WATCHLIST_SELECT_TOP", os.environ.get("ACTIVE_TOP", "4"))
        )
    except Exception:
        selected_target_size = 4
    selected_target_size = max(1, selected_target_size)
    diversity_enabled = _env_flag("ROTATION_DIVERSITY_ENABLED", "1")
    try:
        diversity_penalty = float(os.environ.get("ROTATION_DIVERSITY_PENALTY", "220.0"))
    except Exception:
        diversity_penalty = 220.0
    try:
        diversity_target_corr = float(os.environ.get("ROTATION_DIVERSITY_TARGET_CORR", "0.55"))
    except Exception:
        diversity_target_corr = 0.55
    candidates, candidate_source = _resolve_candidates(args)
    selected_grace = _load_active_selected_symbols()
    markets = [f"{symbol}{quote_asset}" for symbol in candidates if str(symbol).strip()]
    universe_policy_map, universe_policy_info = _build_universe_policy(
        markets=markets,
        quote_asset=quote_asset,
    )
    allow_snapshot_fallback = candidate_source == "pool" and quote_asset == DEFAULT_QUOTE_ASSET
    try:
        max_spread_bps = float(os.environ.get("ROTATION_MAX_SPREAD_BPS", "24.0"))
    except Exception:
        max_spread_bps = 24.0
    try:
        cycle_fit_lookback_bars = int(os.environ.get("ROTATION_CYCLE_FIT_LOOKBACK_BARS", "864"))
    except Exception:
        cycle_fit_lookback_bars = 864
    try:
        cycle_fit_entry_pct = float(os.environ.get("ROTATION_CYCLE_FIT_ENTRY_PCT", "25.0"))
    except Exception:
        cycle_fit_entry_pct = 25.0
    try:
        cycle_fit_exit_pct = float(os.environ.get("ROTATION_CYCLE_FIT_EXIT_PCT", "50.0"))
    except Exception:
        cycle_fit_exit_pct = 50.0
    try:
        cycle_fit_max_bars_to_exit = int(os.environ.get("ROTATION_CYCLE_FIT_MAX_BARS_TO_EXIT", "576"))
    except Exception:
        cycle_fit_max_bars_to_exit = 576
    try:
        cycle_fit_min_completed = float(os.environ.get("ROTATION_CYCLE_FIT_MIN_COMPLETED", "1.0"))
    except Exception:
        cycle_fit_min_completed = 1.0
    try:
        cycle_fit_min_success_rate = float(os.environ.get("ROTATION_CYCLE_FIT_MIN_SUCCESS_RATE", "0.25"))
    except Exception:
        cycle_fit_min_success_rate = 0.25
    try:
        cycle_fit_roundtrip_fee_bps = float(os.environ.get("ROTATION_CYCLE_FIT_ROUNDTRIP_FEE_BPS", "20.0"))
    except Exception:
        cycle_fit_roundtrip_fee_bps = 20.0
    try:
        cycle_fit_min_net_10pct_range_step_pct = float(
            os.environ.get("ROTATION_CYCLE_FIT_MIN_NET_10PCT_RANGE_STEP_PCT", "0.0")
        )
    except Exception:
        cycle_fit_min_net_10pct_range_step_pct = 0.0
    adaptive_cycle_enabled = _env_flag("ROTATION_SELECTOR_ADAPTIVE_CYCLE_ENABLED", "1")
    try:
        adaptive_cycle_lookback_bars = int(
            os.environ.get(
                "ROTATION_SELECTOR_ADAPTIVE_CYCLE_LOOKBACK_BARS",
                str(max(2016, cycle_fit_lookback_bars)),
            )
        )
        adaptive_cycle_lookback_bars = max(96, adaptive_cycle_lookback_bars)
    except Exception:
        adaptive_cycle_lookback_bars = max(2016, cycle_fit_lookback_bars)
    try:
        adaptive_cycle_extrema_window = int(
            os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_EXTREMA_WINDOW", "4")
        )
        adaptive_cycle_extrema_window = max(2, adaptive_cycle_extrema_window)
    except Exception:
        adaptive_cycle_extrema_window = 4
    try:
        adaptive_cycle_min_half_bars = int(
            os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_HALF_BARS", "72")
        )
        adaptive_cycle_min_half_bars = max(2, adaptive_cycle_min_half_bars)
    except Exception:
        adaptive_cycle_min_half_bars = 72
    try:
        adaptive_cycle_max_half_bars = int(
            os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MAX_HALF_BARS", "1728")
        )
        adaptive_cycle_max_half_bars = max(adaptive_cycle_min_half_bars + 1, adaptive_cycle_max_half_bars)
    except Exception:
        adaptive_cycle_max_half_bars = max(adaptive_cycle_min_half_bars + 1, 1728)
    try:
        adaptive_cycle_min_turning_points = int(
            os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_TURNING_POINTS", "5")
        )
        adaptive_cycle_min_turning_points = max(4, adaptive_cycle_min_turning_points)
    except Exception:
        adaptive_cycle_min_turning_points = 5
    try:
        adaptive_cycle_min_swing_bps = float(
            os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_SWING_BPS", "60.0")
        )
        adaptive_cycle_min_swing_bps = max(1.0, adaptive_cycle_min_swing_bps)
    except Exception:
        adaptive_cycle_min_swing_bps = 60.0
    try:
        adaptive_cycle_min_confidence = float(
            os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_CONFIDENCE", "0.32")
        )
        adaptive_cycle_min_confidence = max(0.0, min(1.0, adaptive_cycle_min_confidence))
    except Exception:
        adaptive_cycle_min_confidence = 0.32
    try:
        simple_swing_range_bars = int(
            os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_RANGE_BARS", "2016")
        )
        simple_swing_range_bars = max(96, simple_swing_range_bars)
    except Exception:
        simple_swing_range_bars = 2016
    try:
        simple_swing_range_crash_span_bars = int(
            os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_RANGE_CRASH_SPAN_BARS", "12")
        )
        simple_swing_range_crash_span_bars = max(2, simple_swing_range_crash_span_bars)
    except Exception:
        simple_swing_range_crash_span_bars = 12
    try:
        simple_swing_range_crash_drop_bps = float(
            os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_RANGE_CRASH_DROP_BPS", "-600.0")
        )
    except Exception:
        simple_swing_range_crash_drop_bps = -600.0
    # Downtrend filtering is handled exclusively by universe/auto-blacklist.
    persistent_downdrift_level = "off"
    require_non_falling_longtrend = False
    try:
        non_falling_ret180_min_pct = float(
            os.environ.get("ROTATION_NON_FALLING_RET180_MIN_PCT", "0.0")
        )
    except Exception:
        non_falling_ret180_min_pct = 0.0
    try:
        non_falling_long_high_shift_min_pct = float(
            os.environ.get("ROTATION_NON_FALLING_LONG_HIGH_SHIFT_MIN_PCT", "0.0")
        )
    except Exception:
        non_falling_long_high_shift_min_pct = 0.0
    try:
        non_falling_long_low_shift_min_pct = float(
            os.environ.get("ROTATION_NON_FALLING_LONG_LOW_SHIFT_MIN_PCT", "0.0")
        )
    except Exception:
        non_falling_long_low_shift_min_pct = 0.0
    try:
        non_falling_long_log_slope_min = float(
            os.environ.get("ROTATION_NON_FALLING_LONG_LOG_SLOPE_MIN", "0.0")
        )
    except Exception:
        non_falling_long_log_slope_min = 0.0
    non_falling_rel180_raw = str(
        os.environ.get("ROTATION_NON_FALLING_REL180_MIN_PCT", "")
    ).strip()
    if non_falling_rel180_raw == "":
        non_falling_rel180_min_pct: float | None = None
    else:
        try:
            non_falling_rel180_min_pct = float(non_falling_rel180_raw)
        except Exception:
            non_falling_rel180_min_pct = None
    macro_soft_mode = (
        str(os.environ.get("ROTATION_MACRO_SOFT_MODE", "1")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    try:
        scalp_ret15_min_bps = float(os.environ.get("ROTATION_SCALP_RET15_MIN_BPS", "6.0"))
    except Exception:
        scalp_ret15_min_bps = 6.0
    try:
        scalp_slope_short_min_bps = float(
            os.environ.get("ROTATION_SCALP_SLOPE_SHORT_MIN_BPS", "2.0")
        )
    except Exception:
        scalp_slope_short_min_bps = 2.0
    try:
        scalp_up_ratio_min = float(os.environ.get("ROTATION_SCALP_UP_RATIO_MIN", "0.25"))
    except Exception:
        scalp_up_ratio_min = 0.25
    try:
        staircase_lookback_bars = int(
            os.environ.get("ROTATION_STAIRCASE_LOOKBACK_BARS", "18")
        )
    except Exception:
        staircase_lookback_bars = 18
    try:
        staircase_positive_share_min = float(
            os.environ.get("ROTATION_STAIRCASE_POSITIVE_SHARE_MIN", "0.55")
        )
    except Exception:
        staircase_positive_share_min = 0.55
    try:
        staircase_ret60_min_bps = float(
            os.environ.get("ROTATION_STAIRCASE_RET60_MIN_BPS", "20.0")
        )
    except Exception:
        staircase_ret60_min_bps = 20.0
    try:
        staircase_ret120_min_bps = float(
            os.environ.get("ROTATION_STAIRCASE_RET120_MIN_BPS", "36.0")
        )
    except Exception:
        staircase_ret120_min_bps = 36.0
    try:
        staircase_min_pullback_count = int(
            os.environ.get("ROTATION_STAIRCASE_MIN_PULLBACK_COUNT", "1")
        )
    except Exception:
        staircase_min_pullback_count = 1
    try:
        staircase_min_turns = float(os.environ.get("ROTATION_STAIRCASE_MIN_TURNS", "2.0"))
    except Exception:
        staircase_min_turns = 2.0
    try:
        staircase_max_pullback_run_bps = float(
            os.environ.get("ROTATION_STAIRCASE_MAX_PULLBACK_RUN_BPS", "90.0")
        )
    except Exception:
        staircase_max_pullback_run_bps = 90.0
    try:
        staircase_min_pos_24h_pct = float(
            os.environ.get("ROTATION_STAIRCASE_MIN_POS_24H_PCT", "28.0")
        )
    except Exception:
        staircase_min_pos_24h_pct = 28.0
    try:
        staircase_max_pos_24h_pct = float(
            os.environ.get("ROTATION_STAIRCASE_MAX_POS_24H_PCT", "88.0")
        )
    except Exception:
        staircase_max_pos_24h_pct = 88.0
    try:
        staircase_min_drawdown_from_peak_bps = float(
            os.environ.get("ROTATION_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS", "8.0")
        )
    except Exception:
        staircase_min_drawdown_from_peak_bps = 8.0
    try:
        staircase_max_drawdown_from_peak_bps = float(
            os.environ.get("ROTATION_STAIRCASE_MAX_DRAWDOWN_FROM_PEAK_BPS", "80.0")
        )
    except Exception:
        staircase_max_drawdown_from_peak_bps = 80.0
    try:
        staircase_max_spread_bps = float(
            os.environ.get("ROTATION_STAIRCASE_MAX_SPREAD_BPS", "18.0")
        )
    except Exception:
        staircase_max_spread_bps = 18.0
    try:
        rebound_down_ret60_max_bps = float(
            os.environ.get(
                "ROTATION_REBOUND_DOWN_RET60_MAX_BPS",
                os.environ.get("ROTATION_REBOUND_DOWN_RET720_MAX_BPS", "0.0"),
            )
        )
    except Exception:
        rebound_down_ret60_max_bps = 0.0
    try:
        rebound_down_ret1440_max_bps = float(
            os.environ.get("ROTATION_REBOUND_DOWN_RET1440_MAX_BPS", "-40.0")
        )
    except Exception:
        rebound_down_ret1440_max_bps = -40.0
    try:
        rebound_down_ret2880_max_bps = float(
            os.environ.get("ROTATION_REBOUND_DOWN_RET2880_MAX_BPS", "120.0")
        )
    except Exception:
        rebound_down_ret2880_max_bps = 120.0
    rebound_down_macro_only = (
        str(os.environ.get("ROTATION_REBOUND_DOWN_MACRO_ONLY", "1")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    try:
        post_dump_lock_bars = int(os.environ.get("ROTATION_POST_DUMP_LOCK_BARS", "5"))
    except Exception:
        post_dump_lock_bars = 5
    try:
        post_dump_min_rebound_bps = float(
            os.environ.get("ROTATION_POST_DUMP_MIN_REBOUND_BPS", "10.0")
        )
    except Exception:
        post_dump_min_rebound_bps = 10.0
    try:
        post_dump_min_ret15_bps = float(os.environ.get("ROTATION_POST_DUMP_MIN_RET15_BPS", "2.0"))
    except Exception:
        post_dump_min_ret15_bps = 2.0
    try:
        post_dump_max_down_streak = int(os.environ.get("ROTATION_POST_DUMP_MAX_DOWN_STREAK", "2"))
    except Exception:
        post_dump_max_down_streak = 2
    try:
        post_dump_strong_ret15_bps = float(
            os.environ.get("ROTATION_POST_DUMP_STRONG_RET15_BPS", "20.0")
        )
    except Exception:
        post_dump_strong_ret15_bps = 20.0
    try:
        min_depth_notional = float(os.environ.get("ROTATION_MIN_DEPTH_NOTIONAL", "80.0"))
    except Exception:
        min_depth_notional = 80.0
    try:
        depth_relaxed_min_notional = float(
            os.environ.get("ROTATION_DEPTH_RELAXED_MIN_NOTIONAL", "55.0")
        )
    except Exception:
        depth_relaxed_min_notional = 55.0
    try:
        depth_relax_max_spread_bps = float(
            os.environ.get("ROTATION_DEPTH_RELAX_MAX_SPREAD_BPS", "12.0")
        )
    except Exception:
        depth_relax_max_spread_bps = 12.0
    try:
        depth_relax_min_hour_qv = float(os.environ.get("ROTATION_DEPTH_RELAX_MIN_HOUR_QV", "2500.0"))
    except Exception:
        depth_relax_min_hour_qv = 2500.0
    try:
        depth_relax_min_5m_qv = float(os.environ.get("ROTATION_DEPTH_RELAX_MIN_5M_QV", "20.0"))
    except Exception:
        depth_relax_min_5m_qv = 20.0
    try:
        min_quote_volume_5m = float(os.environ.get("ROTATION_MIN_5M_QUOTE_VOLUME", "8.0"))
    except Exception:
        min_quote_volume_5m = 8.0
    try:
        min_quote_volume_60m = float(os.environ.get("ROTATION_MIN_60M_QUOTE_VOLUME", "1500.0"))
    except Exception:
        min_quote_volume_60m = 1500.0
    volume_gate_require_both = _env_flag("ROTATION_VOLUME_GATE_REQUIRE_BOTH", "0")
    token_prefilter_min_listing_age_days = _env_float(
        "ROTATION_TOKEN_PREFILTER_MIN_LISTING_AGE_DAYS",
        30.0,
        minimum=0.0,
        maximum=365.0,
    )
    try:
        slope_profile_alignment_entry_min = float(
            os.environ.get("ROTATION_SLOPE_PROFILE_ALIGNMENT_MIN", "0.06")
        )
    except Exception:
        slope_profile_alignment_entry_min = 0.06
    try:
        slope_profile_distance_entry_max_bps_h = float(
            os.environ.get("ROTATION_SLOPE_PROFILE_DISTANCE_MAX_BPS_H", "72.0")
        )
    except Exception:
        slope_profile_distance_entry_max_bps_h = 72.0
    sweetspot_path = Path(
        os.environ.get("ROTATION_SWEETSPOT_PATH", str(DEFAULT_SWEETSPOT_PATH))
    ).resolve()
    slope_profiles = _load_slope_profiles(sweetspot_path)
    balances = {}
    include_balances = not bool(args.ignore_balances)
    if include_balances:
        try:
            acct = _signed_get("/api/v3/account")
            balances = {
                entry["asset"]: float(entry["free"]) + float(entry["locked"])
                for entry in acct.get("balances", [])
            }
        except Exception:
            balances = {}

    try:
        btc_market = f"BTC{quote_asset}" if quote_asset != "BTC" else "BTCUSDC"
        btc_klines = _get_klines(btc_market)
        btc_closes = [float(k[4]) for k in btc_klines]
        btc_ret15 = _return_bps(btc_closes, 3)
        btc_ret60 = _return_bps(btc_closes, 12)
        btc_returns = _returns(btc_closes, 24)
    except Exception:
        btc_closes = []
        btc_ret15 = 0.0
        btc_ret60 = 0.0
        btc_returns = []
    try:
        btc_macro_klines = _get_klines(
            f"BTC{quote_asset}" if quote_asset != "BTC" else "BTCUSDC",
            interval=MACRO_KLINE_INTERVAL,
            target_bars=MACRO_KLINE_LIMIT,
        )
        btc_macro_closes = [float(k[4]) for k in btc_macro_klines]
    except Exception:
        btc_macro_closes = []
    if quote_asset != "USDC" and len(btc_macro_closes) < max(31, int(LONG_RANGE_DAYS) + 1):
        try:
            btc_macro_klines = _get_klines(
                "BTCUSDC",
                interval=MACRO_KLINE_INTERVAL,
                target_bars=MACRO_KLINE_LIMIT,
            )
            btc_macro_closes = [float(k[4]) for k in btc_macro_klines]
        except Exception:
            btc_macro_closes = []

    auto_blacklist_map, auto_blacklist_info = _build_auto_blacklist(
        quote_asset=quote_asset,
        scan_symbols=[str(symbol) for symbol in CANDIDATES],
        btc_macro_closes=btc_macro_closes,
    )

    rows: list[dict[str, float | str | bool]] = []
    symbol_returns_short: dict[str, list[float]] = {}
    fetch_errors: list[dict[str, str]] = []
    rate_limit_detected = False
    persistent_downdrift_blocked_count = 0
    non_falling_longtrend_blocked_count = 0
    book_ticker_map: dict[str, dict[str, float]] = {}
    book_ticker_source = "bulk"
    book_ticker_bulk_error = ""
    book_ticker_fallback_hits = 0
    try:
        book_ticker_map = _get_book_ticker_map()
        if not book_ticker_map:
            raise RuntimeError("empty bookTicker payload")
    except Exception as exc:
        book_ticker_source = "per_symbol"
        book_ticker_bulk_error = str(exc).strip() or repr(exc)
    token_prefilter_map, token_prefilter_info = _build_token_prefilter(
        quote_asset=quote_asset,
        scan_symbols=[str(symbol) for symbol in candidates],
        book_ticker_map=book_ticker_map,
        universe_policy_map=universe_policy_map,
    )
    for symbol in candidates:
        market = f"{symbol}{quote_asset}"
        balance_qty = max(0.0, float(balances.get(symbol, 0.0) or 0.0))
        precheck_open_notional = 0.0
        precheck_has_open = False
        if balance_qty > 0.0:
            try:
                book = book_ticker_map.get(market)
                if book is None:
                    book_raw = _public_json(f"/api/v3/ticker/bookTicker?symbol={market}")
                    book = _parse_book_ticker(book_raw)
                    if book is not None:
                        book_ticker_fallback_hits += 1
                bid = float((book or {}).get("bidPrice", 0.0) or 0.0)
                if bid > 0.0:
                    precheck_open_notional = balance_qty * bid
                    precheck_has_open = precheck_open_notional >= 2.0
            except Exception:
                precheck_open_notional = 0.0
                precheck_has_open = False
        auto_blacklist_entry = auto_blacklist_map.get(symbol, {})
        if not isinstance(auto_blacklist_entry, dict):
            auto_blacklist_entry = {}
        auto_blacklisted = bool(auto_blacklist_entry)
        auto_blacklist_reason = str(auto_blacklist_entry.get("reason", "") or "").strip()
        if auto_blacklisted and not auto_blacklist_reason:
            auto_blacklist_reason = "auto_blacklist"
        token_prefilter_entry = token_prefilter_map.get(symbol, {})
        if not isinstance(token_prefilter_entry, dict):
            token_prefilter_entry = {}
        token_prefilter_blocked = bool(token_prefilter_entry.get("blocked", False))
        token_prefilter_reason = str(token_prefilter_entry.get("reason", "") or "").strip()
        token_prefilter_reasons_raw = token_prefilter_entry.get("reasons")
        token_prefilter_reasons = (
            [str(item) for item in token_prefilter_reasons_raw]
            if isinstance(token_prefilter_reasons_raw, list)
            else []
        )
        if token_prefilter_blocked and not token_prefilter_reason:
            token_prefilter_reason = "token_prefilter"
        policy = universe_policy_map.get(market, {})
        monitoring_tags = policy.get("monitoring_tags")
        if not isinstance(monitoring_tags, list):
            monitoring_tags = []
        is_monitoring = bool(policy.get("is_monitoring", False))
        problem_reasons = policy.get("problem_reasons")
        if not isinstance(problem_reasons, list):
            problem_reasons = []
        is_problem = bool(policy.get("is_problem", False))
        monitoring_data_unknown = bool(policy.get("monitoring_data_unknown", False))
        problem_data_unknown = bool(policy.get("problem_data_unknown", False))
        policy_data_unknown = bool(
            policy.get(
                "policy_data_unknown",
                monitoring_data_unknown or problem_data_unknown,
            )
        )
        hard_excluded = False
        hard_reason = ""
        if symbol not in selected_grace:
            if auto_blacklisted:
                hard_excluded = True
                hard_reason = auto_blacklist_reason
            elif token_prefilter_blocked:
                hard_excluded = True
                hard_reason = token_prefilter_reason
            elif is_monitoring:
                hard_excluded = True
                hard_reason = "monitoring_tag"
            elif is_problem:
                hard_excluded = True
                hard_reason = (
                    f"problem_case:{problem_reasons[0]}"
                    if problem_reasons
                    else "problem_case"
                )
            elif policy_data_unknown:
                hard_excluded = True
                hard_reason = "universe_policy_data_unknown"
        # Hard universe blocks prevent new entries, but they must not hide an
        # already open spot position from the rotation/watch machinery.
        if hard_excluded and not precheck_has_open:
            rows.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "score": -50000.0,
                    "setup_type": "blocked",
                    "open_notional": precheck_open_notional,
                    "keep_open": False,
                    "eligible": False,
                    "gate_reason": hard_reason,
                    "hard_excluded": True,
                    "hard_exclusion_reason": hard_reason,
                    "auto_blacklisted": auto_blacklisted,
                    "auto_blacklist_reason": auto_blacklist_reason,
                    "token_prefilter_blocked": token_prefilter_blocked,
                    "token_prefilter_reason": token_prefilter_reason,
                    "token_prefilter_reasons": token_prefilter_reasons,
                    "token_prefilter": token_prefilter_entry,
                    "token_prefilter_listing_history_days": 0.0,
                    "is_monitoring": is_monitoring,
                    "monitoring_tags": monitoring_tags,
                    "is_problem_case": is_problem,
                    "problem_case_reasons": problem_reasons,
                    "monitoring_data_unknown": monitoring_data_unknown,
                    "problem_data_unknown": problem_data_unknown,
                    "policy_data_unknown": policy_data_unknown,
                    "excluded_with_selected_grace": False,
                }
            )
            continue
        try:
            klines = _get_klines(market)
            closes = [float(k[4]) for k in klines]
            quote_volumes = [float(k[7]) for k in klines]
            if len(closes) < 40:
                continue
            last = closes[-1]
            if last <= 0.0:
                continue

            width_pct = _window_width_pct(closes, len(closes))
            near_klines = _get_klines(
                market,
                interval=NEAR_KLINE_INTERVAL,
                target_bars=NEAR_KLINE_LIMIT,
            )
            near_closes = [float(k[4]) for k in near_klines]
            macro_klines = _get_klines(
                market,
                interval=MACRO_KLINE_INTERVAL,
                target_bars=MACRO_KLINE_LIMIT,
            )
            macro_closes = [float(k[4]) for k in macro_klines]
            token_prefilter_listing_history_days = max(0.0, float(len(macro_closes) - 1))
            persistent_downdrift = _persistent_downdrift_metrics(
                macro_closes,
                btc_macro_closes,
                level=persistent_downdrift_level,
            )

            corridor = _adaptive_corridor_pos_pct(
                closes,
                near_closes=near_closes,
                near_bar_minutes=60.0,
                macro_closes=macro_closes,
            )
            pos_pct = float(corridor.get("pos_pct", 50.0))
            cycle_stats = _cycle_fitness_stats(
                closes,
                lookback_bars=cycle_fit_lookback_bars,
                entry_pct=cycle_fit_entry_pct,
                exit_pct=cycle_fit_exit_pct,
                max_bars_to_exit=cycle_fit_max_bars_to_exit,
            )
            cycle_completed = float(cycle_stats["completed_cycles"])
            cycle_attempts = float(cycle_stats["attempts"])
            cycle_success_rate = float(cycle_stats["success_rate"])
            cycle_median_bars_to_exit = float(cycle_stats["median_bars_to_exit"])
            cycle_last_bars_ago = float(cycle_stats["last_cycle_bars_ago"])
            gross_10pct_range_step_pct = max(0.0, float(width_pct) * 0.10)
            net_10pct_range_step_after_fees_pct = _net_range_step_after_fees_pct(
                width_pct=float(width_pct),
                step_fraction=0.10,
                roundtrip_fee_bps=cycle_fit_roundtrip_fee_bps,
            )
            cycle_fit_profit_step_ok = (
                net_10pct_range_step_after_fees_pct >= cycle_fit_min_net_10pct_range_step_pct
            )
            cycle_fit_ok_static = (
                cycle_completed >= max(0.0, cycle_fit_min_completed)
                and cycle_success_rate >= max(0.0, min(1.0, cycle_fit_min_success_rate))
                and cycle_fit_profit_step_ok
            )
            adaptive_cycle_stats = _adaptive_cycle_timing_stats(
                closes,
                enabled=adaptive_cycle_enabled,
                lookback_bars=adaptive_cycle_lookback_bars,
                extrema_window=adaptive_cycle_extrema_window,
                min_half_cycle_bars=adaptive_cycle_min_half_bars,
                max_half_cycle_bars=adaptive_cycle_max_half_bars,
                min_turning_points=adaptive_cycle_min_turning_points,
                min_swing_bps=adaptive_cycle_min_swing_bps,
                min_confidence=adaptive_cycle_min_confidence,
            )
            cycle_fit_ok = bool(cycle_fit_ok_static) or bool(adaptive_cycle_stats.get("fit_ok", False))
            if symbol in selected_grace:
                cycle_fit_ok = True
            tail = closes[-288:]
            turns_24h = _turn_count(tail)
            net24_pct = ((tail[-1] / tail[0]) - 1.0) * 100.0 if tail[0] > 0.0 else 0.0

            book = book_ticker_map.get(market)
            if book is None:
                book_raw = _public_json(f"/api/v3/ticker/bookTicker?symbol={market}")
                book = _parse_book_ticker(book_raw)
                if book is None:
                    raise RuntimeError(f"invalid bookTicker payload for {market}")
                book_ticker_fallback_hits += 1
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            bid_qty = float(book["bidQty"])
            ask_qty = float(book["askQty"])
            mid = (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else 0.0
            spread_bps = ((ask - bid) / mid) * 10000.0 if mid > 0.0 else 9999.0
            top_depth_notional = (bid * bid_qty) + (ask * ask_qty)

            open_notional = float(balances.get(symbol, 0.0) or 0.0) * bid
            has_open = open_notional >= 2.0

            last_qv = quote_volumes[-1]
            hour_qv = sum(quote_volumes[-12:])
            ret10_bps = _return_bps(closes, 2)
            ret15_bps = _return_bps(closes, 3)
            ret30_bps = _return_bps(closes, 6)
            ret60_bps = _return_bps(closes, 12)
            ret120_bps = _return_bps(closes, 24)
            ret360_bps = _return_bps(closes, 72)
            ret720_bps = _return_bps(closes, 144)
            ret1440_bps = _return_bps(closes, 288)
            ret2880_bps = _return_bps(closes, 576)
            rel15_bps = ret15_bps - btc_ret15
            rel60_bps = ret60_bps - btc_ret60
            short_returns = _returns(closes, 24)
            symbol_returns_short[symbol] = short_returns
            corr_btc = _pearson(short_returns, btc_returns) if btc_returns else 1.0
            up_moves = sum(1 for a, b in zip(closes[-7:-1], closes[-6:]) if b > a)
            up_ratio = up_moves / 6.0
            mean20 = sum(closes[-20:]) / 20.0
            overextension_bps = ((last / mean20) - 1.0) * 10000.0 if mean20 > 0.0 else 0.0
            structure = classify_market_structure(closes, bar_seconds=300)
            structure_phase = str(structure.phase or "unknown")
            structure_confidence = float(structure.confidence)
            structure_is_bottom = structure_phase in {"bottom", "lift_off"}
            structure_is_trend = structure_phase in {"lift_off", "uptrend"}
            structure_is_bad = structure_phase in {"peak", "rollover", "downtrend"}
            pos_6h_pct = float(structure.level_6h) * 100.0
            pos_24h_pct = float(structure.level_24h) * 100.0
            micro_valley_context = _micro_valley_context_position_pct(
                closes,
                near_closes=near_closes,
                near_bar_minutes=60.0,
                fallback_pos_pct=float(corridor.get("near_pos_pct", pos_24h_pct)),
            )
            valley_context_pos_pct = float(micro_valley_context.get("pos_pct", pos_24h_pct) or pos_24h_pct)
            # 48h fallback basis (legacy) for simple-swing entry positioning.
            pos_48h_pct = _window_pos_pct(closes, 576)
            # Primary basis: 7d range with flash-crash bars excluded.
            pos_7d_pct = _window_pos_pct(closes, simple_swing_range_bars)
            pos_7d_nocrash_pct, crash_7d_event_count = _window_pos_pct_excluding_crash_events(
                closes,
                simple_swing_range_bars,
                crash_span_bars=simple_swing_range_crash_span_bars,
                crash_drop_bps=simple_swing_range_crash_drop_bps,
            )
            crash_7d_min_ret30_bps = _window_min_return_bps(closes, simple_swing_range_bars, 6)
            crash_7d_min_ret60_bps = _window_min_return_bps(
                closes,
                simple_swing_range_bars,
                simple_swing_range_crash_span_bars,
            )
            crash_7d_detected = crash_7d_event_count > 0.0
            pivot_reversal_bps = max(1.0, float(structure.pivot_reversal_bps))
            geo_rebound_from_valley_bps = float(structure.rebound_from_valley_bps)
            geo_drawdown_from_peak_bps = float(structure.drawdown_from_peak_bps)
            bars_since_valley = int(structure.bars_since_valley)
            bars_since_peak = int(structure.bars_since_peak)
            active_leg = str(structure.active_leg or "flat")
            up_structure = bool(structure.up_structure)
            down_structure = bool(structure.down_structure)
            profile = slope_profiles.get(symbol)
            has_slope_profile = profile is not None
            slope_profile_low_bps_h = (
                float(profile["low_bps_h"]) if profile is not None else 0.0
            )
            slope_profile_high_raw = profile.get("high_bps_h") if profile is not None else None
            slope_profile_high_bps_h = (
                None if slope_profile_high_raw is None else float(slope_profile_high_raw)
            )
            slope_profile_mid_bps_h = (
                float(profile["mid_bps_h"]) if profile is not None else 0.0
            )
            slope_profile_samples = int(profile["n"]) if profile is not None else 0
            slope_profile_window_hours = (
                float(profile["slope_window_hours"]) if profile is not None else 4.0
            )
            slope_window_bars = max(
                2, int(round(max(0.1, slope_profile_window_hours) / BAR_HOURS))
            )
            current_slope_bps_h = _slope_bps_per_hour(closes, slope_window_bars)
            slope_profile_match = True
            slope_profile_distance_bps_h = 0.0
            slope_profile_alignment = 0.5
            if has_slope_profile:
                (
                    slope_profile_match,
                    slope_profile_distance_bps_h,
                    slope_profile_alignment,
                ) = _slope_profile_match_state(
                    current_slope_bps_h,
                    slope_profile_low_bps_h,
                    slope_profile_high_bps_h,
                )
            slope_profile_entry_ok = (not has_slope_profile) or (
                slope_profile_match
                or slope_profile_alignment >= slope_profile_alignment_entry_min
                or slope_profile_distance_bps_h <= slope_profile_distance_entry_max_bps_h
            )
            peak_profile = (
                _coin_peak_block_profile(
                    slope_profile_low_bps_h,
                    slope_profile_high_bps_h,
                    slope_profile_samples,
                )
                if has_slope_profile
                else None
            )
            peak_profile_class = str(peak_profile.get("profile_class", "")) if peak_profile else ""
            peak_block_max_pos_24h_pct = (
                float(peak_profile["max_pos_24h_pct"]) if peak_profile else 100.0
            )
            peak_block_max_bars_since_peak = (
                float(peak_profile["max_bars_since_peak"]) if peak_profile else 0.0
            )
            peak_block_min_rebound_from_valley_bps = (
                float(peak_profile["min_rebound_from_valley_bps"]) if peak_profile else 0.0
            )
            peak_block_max_drawdown_from_peak_bps = (
                float(peak_profile["max_drawdown_from_peak_bps"]) if peak_profile else 0.0
            )
            coin_peak_blocked = (
                peak_profile is not None
                and active_leg in {"rise", "flat"}
                and pos_24h_pct >= peak_block_max_pos_24h_pct
                and bars_since_peak <= peak_block_max_bars_since_peak
                and geo_rebound_from_valley_bps >= peak_block_min_rebound_from_valley_bps
                and geo_drawdown_from_peak_bps <= peak_block_max_drawdown_from_peak_bps
            )
            geo_rebound_min_bps = max(4.0, pivot_reversal_bps * 0.22)
            geo_rebound_max_bps = max(42.0, pivot_reversal_bps * 2.20)
            broad_uptrend_context = (
                structure_phase == "uptrend"
                and active_leg == "rise"
                and ret720_bps >= 80.0
                and ret1440_bps >= 60.0
                and ret2880_bps >= 180.0
                and float(structure.slope_medium_bps) >= 4.0
                and float(structure.slope_long_bps) >= 1.0
            )
            basic_overextension_limit_bps = 900.0 if (trend_only and broad_uptrend_context) else 320.0
            long_term_uptrend_raw = (
                ret2880_bps >= 120.0
                and ret1440_bps >= 40.0
                and ret720_bps >= 10.0
                and float(structure.slope_long_bps) >= 0.2
            )
            long_up_hot = (
                ret360_bps >= 90.0
                or float(structure.slope_long_bps) >= 8.0
                or (ret120_bps >= 60.0 and pos_24h_pct >= 60.0)
                or (up_structure and pos_24h_pct >= 68.0)
            )
            down_streak = _down_streak(closes, max_bars=4)
            up_streak = _up_streak(closes, max_bars=4)
            falling_now = down_streak >= 2 and ret10_bps <= -15.0
            previous_selloff = (
                ret30_bps <= -15.0
                or ret60_bps <= -20.0
                or ret120_bps <= -40.0
                or net24_pct <= -1.0
            )
            closes_30m = closes[-7:]
            closes_60m = closes[-13:]
            recent_low_30m = min(closes_30m)
            recent_low_60m = min(closes_60m)
            bars_since_30m_low = _bars_since_last_low(closes_30m)
            bars_since_60m_low = _bars_since_last_low(closes_60m)
            swing_low_idx, swing_low_price = _latest_swing_low(closes, lookback=24)
            bars_since_swing_low = (len(closes) - 1) - swing_low_idx if swing_low_idx >= 0 else 999
            pre_swing_high = 0.0
            if swing_low_idx > 0:
                start_idx = max(0, swing_low_idx - 24)
                pre_window = closes[start_idx:swing_low_idx]
                if pre_window:
                    pre_swing_high = max(pre_window)
            pre_swing_drop_bps = (
                ((swing_low_price / pre_swing_high) - 1.0) * 10000.0
                if pre_swing_high > 0.0 and swing_low_price > 0.0
                else 0.0
            )
            rebound_from_30m_low_bps = (
                ((last / recent_low_30m) - 1.0) * 10000.0 if recent_low_30m > 0.0 else 0.0
            )
            rebound_from_60m_low_bps = (
                ((last / recent_low_60m) - 1.0) * 10000.0 if recent_low_60m > 0.0 else 0.0
            )
            swing_rebound_bps = (
                ((last / swing_low_price) - 1.0) * 10000.0 if swing_low_price > 0.0 else 0.0
            )
            higher_low_ready = _higher_low_after_swing(closes, swing_low_idx)
            base_ready = (
                previous_selloff
                and 1 <= bars_since_swing_low <= 4
                and pre_swing_drop_bps <= -25.0
                and 6.0 <= swing_rebound_bps <= 80.0
                and higher_low_ready
            )
            fresh_bottom = bars_since_swing_low <= 2 or bars_since_30m_low <= 1
            recent_rebound_ready = (
                (
                    up_streak >= 1
                    and ret10_bps >= 2.0
                    and rebound_from_30m_low_bps >= 4.0
                    and fresh_bottom
                )
                or base_ready
            )
            still_dumping = down_streak >= 3 and ret30_bps <= -40.0
            bars_since_last_dump, last_dump_idx = _last_still_dumping_event(
                closes, lookback_bars=36
            )
            recent_dump_context = still_dumping or (bars_since_last_dump <= post_dump_lock_bars)
            post_dump_segment = closes[last_dump_idx:] if last_dump_idx >= 0 else []
            post_dump_low = min(post_dump_segment) if post_dump_segment else last
            rebound_from_post_dump_low_bps = (
                ((last / post_dump_low) - 1.0) * 10000.0 if post_dump_low > 0.0 else 0.0
            )
            bars_since_post_dump_low = (
                _bars_since_last_low(post_dump_segment) if post_dump_segment else 999
            )
            post_dump_recovery_ready = (
                recent_dump_context
                and rebound_from_post_dump_low_bps >= post_dump_min_rebound_bps
                and ret15_bps >= post_dump_min_ret15_bps
                and (up_streak >= 1 or ret15_bps >= post_dump_strong_ret15_bps)
                and down_streak <= post_dump_max_down_streak
                and bars_since_post_dump_low >= 1
            )
            post_dump_blocked = recent_dump_context and not post_dump_recovery_ready
            mid_trend_up_1h = (
                ret60_bps >= 20.0
                and ret120_bps >= 0.0
                and float(structure.slope_medium_bps) >= 1.0
            )
            mid_trend_up_1h_strict = (
                ret60_bps >= 35.0
                and float(structure.slope_medium_bps) >= 2.0
                and float(structure.slope_long_bps) >= 0.3
                and up_ratio >= 0.33
                and not down_structure
            )
            # Keep legacy field for telemetry, but 1h-only setup does not use 6h gating.
            mid_trend_up_6h_balanced = False
            hour_up_soft = (
                ret60_bps >= 15.0
                and float(structure.slope_medium_bps) >= 0.8
                and active_leg != "fall"
            )
            hour_down_hard = (
                ret60_bps <= -15.0
                and (
                    down_structure
                    or active_leg == "fall"
                    or float(structure.slope_medium_bps) <= 0.0
                )
            )
            hour_down_soft = (
                ret60_bps <= 5.0
                and down_structure
                and float(structure.slope_medium_bps) <= 0.2
            )
            macro_up_context = (
                mid_trend_up_1h_strict
                or (
                    hour_up_soft
                    and up_structure
                    and float(structure.slope_long_bps) >= 0.0
                )
            )
            macro_down_context = (
                hour_down_hard
                or hour_down_soft
                or (
                    down_structure
                    and float(structure.slope_long_bps) <= 0.0
                    and ret60_bps <= 20.0
                )
            )
            long_term_uptrend_context = hour_up_soft and not macro_down_context
            trend_horizon_ok = (
                mid_trend_up_1h_strict
                or (mid_trend_up_1h and not macro_down_context)
            )
            if rebound_down_macro_only:
                rebound_in_downtrend_raw = (
                    structure_phase in {"bottom", "lift_off"} and macro_down_context
                )
            else:
                rebound_in_downtrend_raw = (
                    structure_phase in {"bottom", "lift_off"}
                    and (
                        macro_down_context
                        or (down_structure and ret60_bps <= rebound_down_ret60_max_bps)
                    )
                )
            clear_downtrend_pressure = (
                active_leg == "fall"
                or ret60_bps <= -25.0
                or (
                    down_structure
                    and float(structure.slope_medium_bps) <= 0.0
                    and ret60_bps <= 0.0
                )
            )
            rebound_strength_override = (
                active_leg == "rise"
                and ret15_bps >= max(6.0, scalp_ret15_min_bps)
                and ret60_bps >= -5.0
                and float(structure.slope_short_bps) >= max(2.0, scalp_slope_short_min_bps)
                and up_ratio >= max(0.25, scalp_up_ratio_min)
                and not post_dump_blocked
            )
            rebound_in_downtrend = (
                rebound_in_downtrend_raw
                and clear_downtrend_pressure
                and not rebound_strength_override
            )
            countertrend_rebound = (
                ret15_bps > 0.0
                and ret60_bps > 0.0
                and (
                    macro_down_context
                    or (float(structure.slope_long_bps) <= -0.3 and ret60_bps <= 10.0)
                    or (down_structure and ret60_bps <= 10.0)
                )
            )
            short_horizon_range_override = (
                structure_phase == "range"
                and active_leg == "rise"
                and not macro_down_context
                and ret60_bps >= 12.0
                and float(structure.slope_medium_bps) >= 2.0
                and float(structure.slope_long_bps) >= 0.6
                and up_ratio >= 0.33
                and not post_dump_blocked
                and not countertrend_rebound
            )
            trend_horizon_effective_ok = trend_horizon_ok or short_horizon_range_override
            short_horizon_scalp_ok = (
                ret15_bps >= scalp_ret15_min_bps
                and ret60_bps >= -5.0
                and float(structure.slope_short_bps) >= scalp_slope_short_min_bps
                and active_leg != "fall"
                and up_ratio >= scalp_up_ratio_min
                and not post_dump_blocked
                and not countertrend_rebound
            )
            strict_valley_context = _strict_valley_context(
                pos_6h_pct=pos_6h_pct,
                pos_24h_pct=valley_context_pos_pct,
                long_up_hot=long_up_hot,
                down_structure=down_structure,
            )
            relaxed_valley_context = _relaxed_valley_context(
                pos_6h_pct=pos_6h_pct,
                pos_24h_pct=valley_context_pos_pct,
                long_up_hot=long_up_hot,
                down_structure=down_structure,
                macro_down_context=macro_down_context,
                falling_now=falling_now,
                spread_bps=spread_bps,
                structure_phase=structure_phase,
                recent_rebound_ready=recent_rebound_ready,
                post_dump_recovery_ready=post_dump_recovery_ready,
                previous_selloff=previous_selloff,
                rebound_from_30m_low_bps=rebound_from_30m_low_bps,
                rebound_from_60m_low_bps=rebound_from_60m_low_bps,
                bars_since_30m_low=bars_since_30m_low,
                bars_since_swing_low=bars_since_swing_low,
            )
            in_valley_context = strict_valley_context or relaxed_valley_context
            fresh_liftoff_bottom_candidate = _fresh_liftoff_bottom_candidate(
                structure_phase=structure_phase,
                active_leg=active_leg,
                pos_24h_pct=pos_24h_pct,
                spread_bps=spread_bps,
                bars_since_30m_low=bars_since_30m_low,
                ret10_bps=ret10_bps,
                ret15_bps=ret15_bps,
                ret60_bps=ret60_bps,
                previous_selloff=previous_selloff,
                in_valley_context=in_valley_context,
                macro_down_context=macro_down_context,
            )
            fresh_late_rebound_override = _fresh_late_rebound_override(
                active_leg=active_leg,
                pos_24h_pct=pos_24h_pct,
                spread_bps=spread_bps,
                bars_since_30m_low=bars_since_30m_low,
                ret10_bps=ret10_bps,
                ret15_bps=ret15_bps,
            )
            too_late_rebound = (
                pos_24h_pct >= 68.0
                or geo_rebound_from_valley_bps >= max(92.0, pivot_reversal_bps * 3.20)
                or (bars_since_valley >= 14 and not fresh_late_rebound_override)
            )
            bottom_zone = pos_pct <= 45.0

            # Hybrid universe filter: allow both bottom-rebound and trend-follow setups.
            basic_eligible = True
            gate_reason = ""
            depth_min_notional_effective = max(10.0, float(min_depth_notional))
            depth_relaxed_applied = False
            non_falling_longtrend_blocked = False
            non_falling_longtrend_reason = ""
            if not has_open:
                (
                    non_falling_longtrend_blocked,
                    non_falling_longtrend_reason,
                ) = _non_falling_longtrend_block(
                    persistent_downdrift,
                    enabled=require_non_falling_longtrend,
                    ret180_min_pct=non_falling_ret180_min_pct,
                    long_high_shift_min_pct=non_falling_long_high_shift_min_pct,
                    long_low_shift_min_pct=non_falling_long_low_shift_min_pct,
                    long_log_slope_min=non_falling_long_log_slope_min,
                    rel180_min_pct=non_falling_rel180_min_pct,
                )
            depth_relax_shorttrend_ok = (
                ret60_bps >= -5.0
                and float(structure.slope_medium_bps) >= 1.0
                and active_leg != "fall"
            )
            if (
                spread_bps <= max(0.0, depth_relax_max_spread_bps)
                and (hour_qv >= max(0.0, depth_relax_min_hour_qv) or last_qv >= max(0.0, depth_relax_min_5m_qv))
                and depth_relax_shorttrend_ok
            ):
                depth_min_notional_effective = max(
                    10.0,
                    min(depth_min_notional_effective, float(depth_relaxed_min_notional)),
                )
                depth_relaxed_applied = depth_min_notional_effective < max(
                    10.0, float(min_depth_notional)
                )
            if spread_bps > max_spread_bps:
                basic_eligible = False
                gate_reason = "spread"
            elif top_depth_notional < depth_min_notional_effective:
                basic_eligible = False
                gate_reason = "depth"
            elif (
                volume_gate_require_both
                and (last_qv < min_quote_volume_5m or hour_qv < min_quote_volume_60m)
            ):
                basic_eligible = False
                gate_reason = "volume"
            elif (
                (not volume_gate_require_both)
                and last_qv < min_quote_volume_5m
                and hour_qv < min_quote_volume_60m
            ):
                basic_eligible = False
                gate_reason = "volume"
            elif still_dumping:
                basic_eligible = False
                gate_reason = "still_dumping"
            elif post_dump_blocked:
                basic_eligible = False
                gate_reason = "post_dump_recovery_pending"
            elif coin_peak_blocked:
                basic_eligible = False
                gate_reason = "coin_peak_reentry_pending"
            elif overextension_bps > basic_overextension_limit_bps:
                basic_eligible = False
                gate_reason = "overextended"
            elif structure_phase == "unknown":
                basic_eligible = False
                gate_reason = "structure_warmup"
            elif structure_is_bad:
                basic_eligible = False
                gate_reason = f"structure_{structure_phase}"
            elif structure_phase == "stall" and not long_term_uptrend_context:
                basic_eligible = False
                gate_reason = "structure_stall"
            elif not cycle_fit_ok:
                basic_eligible = False
                if not cycle_fit_profit_step_ok:
                    gate_reason = "cycle_fit_unprofitable_step"
                else:
                    gate_reason = "cycle_fit_miss"
            elif (
                persistent_downdrift_level != "off"
                and bool(persistent_downdrift.get("data_ok", False))
                and bool(persistent_downdrift.get("blocked", False))
                and not has_open
                and symbol not in selected_grace
            ):
                basic_eligible = False
                gate_reason = f"persistent_downdrift_{persistent_downdrift_level}"
                persistent_downdrift_blocked_count += 1
            elif (
                token_prefilter_min_listing_age_days > 0.0
                and not has_open
                and symbol not in selected_grace
                and token_prefilter_listing_history_days > 0.0
                and token_prefilter_listing_history_days < token_prefilter_min_listing_age_days
            ):
                basic_eligible = False
                gate_reason = "token_prefilter_young_listing"
            elif non_falling_longtrend_blocked:
                basic_eligible = False
                if non_falling_longtrend_reason:
                    gate_reason = f"non_falling_longtrend_{non_falling_longtrend_reason}"
                else:
                    gate_reason = "non_falling_longtrend_required"
                non_falling_longtrend_blocked_count += 1

            geo_fresh_valley = bars_since_valley <= 10
            geo_bottom_ready = (
                structure_is_bottom
                and in_valley_context
                and geo_fresh_valley
                and active_leg in {"rise", "flat"}
                and geo_rebound_from_valley_bps >= -2.0
                and geo_rebound_from_valley_bps <= geo_rebound_max_bps
            )
            trend_pos_ceiling = 100.0 if structure_phase == "uptrend" else 97.0
            geo_trend_ready = (
                active_leg == "rise"
                and (
                    up_structure
                    or structure_phase in {"lift_off", "uptrend"}
                    or (
                        structure_phase == "bottom"
                        and float(structure.slope_medium_bps) >= 0.0
                        and ret60_bps > -20.0
                    )
                    or (
                        structure_phase == "range"
                        and float(structure.slope_medium_bps) >= 2.0
                        and float(structure.slope_long_bps) >= 0.5
                    )
                )
                and 8.0 <= pos_24h_pct <= trend_pos_ceiling
                and geo_rebound_from_valley_bps >= geo_rebound_min_bps
            )
            geo_early_liftoff_trend = (
                structure_phase == "lift_off"
                and active_leg == "rise"
                and bars_since_valley <= 8
                and geo_rebound_from_valley_bps >= max(8.0, pivot_reversal_bps * 0.30)
                and ret15_bps >= 8.0
                and up_streak >= 1
            )
            staircase_stats = _staircase_stats(closes, lookback_bars=staircase_lookback_bars)
            staircase_positive_share = float(staircase_stats["positive_share"])
            staircase_pullback_count = float(staircase_stats["pullback_count"])
            staircase_turns = float(staircase_stats["turns"])
            staircase_max_negative_bar_bps = float(staircase_stats["max_negative_bar_bps"])
            staircase_max_pullback_run = float(staircase_stats["max_pullback_run_bps"])
            staircase_net_bps = float(staircase_stats["net_bps"])
            staircase_mean_up_bps = float(staircase_stats["mean_up_bps"])
            staircase_mean_down_bps = float(staircase_stats["mean_down_bps"])
            staircase_trend = (
                structure_phase in {"lift_off", "uptrend", "range"}
                and active_leg == "rise"
                and up_structure
                and not down_structure
                and not macro_down_context
                and not rebound_in_downtrend
                and not countertrend_rebound
                and not post_dump_blocked
                and spread_bps <= min(max_spread_bps, staircase_max_spread_bps)
                and ret60_bps >= staircase_ret60_min_bps
                and ret120_bps >= staircase_ret120_min_bps
                and ret15_bps >= max(-18.0, scalp_ret15_min_bps - 14.0)
                and float(structure.slope_short_bps) >= max(0.5, scalp_slope_short_min_bps * 0.45)
                and float(structure.slope_medium_bps) >= 1.5
                and float(structure.slope_long_bps) >= 0.35
                and up_ratio >= max(0.33, staircase_positive_share_min - 0.08)
                and staircase_positive_share >= staircase_positive_share_min
                and staircase_pullback_count >= float(staircase_min_pullback_count)
                and staircase_turns >= staircase_min_turns
                and staircase_max_pullback_run <= staircase_max_pullback_run_bps
                and staircase_net_bps >= max(20.0, staircase_ret60_min_bps * 0.9)
                and pos_24h_pct >= staircase_min_pos_24h_pct
                and pos_24h_pct <= staircase_max_pos_24h_pct
                and geo_drawdown_from_peak_bps >= staircase_min_drawdown_from_peak_bps
                and geo_drawdown_from_peak_bps <= staircase_max_drawdown_from_peak_bps
                and (macro_up_context or trend_horizon_effective_ok or short_horizon_scalp_ok)
            )
            macro_soft_support = trend_horizon_effective_ok
            if macro_soft_mode and not macro_down_context:
                macro_soft_support = (
                    trend_horizon_effective_ok or short_horizon_scalp_ok or staircase_trend
                )
            if macro_down_context:
                macro_trend_class = "down"
            elif macro_up_context:
                macro_trend_class = "up"
            else:
                macro_trend_class = "neutral"
            macro_entry_ready = (
                macro_trend_class == "up"
                or (
                    macro_trend_class == "neutral"
                    and macro_soft_support
                    and short_horizon_scalp_ok
                )
            )
            trend_context_confirmed = (
                (
                    structure_phase == "uptrend"
                    and active_leg == "rise"
                    and float(structure.slope_medium_bps) >= 0.5
                )
                or (
                    structure_phase == "lift_off"
                    and active_leg == "rise"
                    and ret60_bps >= 0.0
                    and float(structure.slope_long_bps) >= 0.3
                    and (up_structure or ret60_bps >= 25.0)
                )
                or (
                    structure_phase == "range"
                    and active_leg == "rise"
                    and ret60_bps >= 20.0
                    and float(structure.slope_medium_bps) >= 3.0
                    and float(structure.slope_long_bps) >= 1.0
                    and up_ratio >= 0.16
                )
                or (
                    structure_phase in {"stall", "range"}
                    and active_leg in {"rise", "flat"}
                    and long_term_uptrend_context
                    and ret60_bps >= -10.0
                    and up_ratio >= 0.34
                )
                or (
                    structure_phase == "bottom"
                    and active_leg == "rise"
                    and ret15_bps >= -5.0
                    and ret60_bps >= -20.0
                    and float(structure.slope_medium_bps) >= 0.0
                    and up_ratio >= 0.24
                )
            )
            trend_context_range_minimal = (
                structure_phase == "range"
                and active_leg == "rise"
                and not macro_down_context
                and not countertrend_rebound
                and not rebound_in_downtrend
                and not post_dump_blocked
                and ret60_bps >= 12.0
                and float(structure.slope_medium_bps) >= 2.0
                and float(structure.slope_long_bps) >= 0.6
                and up_ratio >= 0.33
            )
            trend_context_effective_confirmed = (
                trend_context_confirmed or trend_context_range_minimal
            )

            bottom_candidate_base = (
                (geo_bottom_ready or fresh_liftoff_bottom_candidate)
                and not too_late_rebound
                and ret10_bps >= -12.0
            )
            strict_bottom_candidate = (
                geo_bottom_ready
                and structure_phase == "lift_off"
                and bars_since_valley <= 4
                and not too_late_rebound
                and geo_rebound_from_valley_bps >= geo_rebound_min_bps
                and geo_rebound_from_valley_bps <= max(40.0, pivot_reversal_bps * 1.80)
                and ret10_bps >= -2.0
            )
            bottom_candidate = strict_bottom_candidate if strict_bottom_only else bottom_candidate_base

            trend_ready = (
                (
                    (geo_trend_ready or geo_early_liftoff_trend)
                    and macro_entry_ready
                    and ret60_bps >= -15.0
                    and ret15_bps >= -25.0
                    and up_ratio >= 0.20
                    and not macro_down_context
                    and trend_context_effective_confirmed
                    and slope_profile_entry_ok
                    and not post_dump_blocked
                    and not rebound_in_downtrend
                    and not countertrend_rebound
                )
                or staircase_trend
            )
            strong_continuation_context = (
                structure_phase in {"uptrend", "range"}
                and active_leg == "rise"
                and macro_up_context
                and float(structure.slope_medium_bps) >= 4.0
                and float(structure.slope_long_bps) >= 1.0
                and up_ratio >= 0.50
            )
            trend_overextended = (
                (
                    not strong_continuation_context
                    and pos_24h_pct >= 92.0
                )
                or overextension_bps >= (1100.0 if strong_continuation_context else 260.0)
                or structure_phase in {"peak"}
                or (
                    not strong_continuation_context
                    and pos_24h_pct >= 72.0
                    and geo_drawdown_from_peak_bps <= max(6.0, pivot_reversal_bps * 0.20)
                )
            )
            range_noise_downstructure_ok = (
                structure_phase == "range"
                and active_leg == "rise"
                and ret60_bps >= 25.0
                and float(structure.slope_medium_bps) >= 3.0
                and float(structure.slope_long_bps) >= 1.0
                and up_ratio >= 0.16
            )
            trend_candidate = (
                (
                    trend_ready
                    and not trend_overextended
                    and (down_streak <= 1 or (geo_early_liftoff_trend and down_streak <= 2))
                    and not still_dumping
                    and (
                        float(structure.drawdown_bps) <= 45.0
                        or (geo_early_liftoff_trend and float(structure.drawdown_bps) <= 85.0)
                    )
                    and (
                        not down_structure
                        or strong_continuation_context
                        or range_noise_downstructure_ok
                    )
                    and not macro_down_context
                    and not countertrend_rebound
                )
                or staircase_trend
            )
            trend_candidate_relaxed = (
                not trend_candidate
                and not macro_down_context
                and not countertrend_rebound
                and structure_phase in {"uptrend", "range"}
                and active_leg == "rise"
                and macro_soft_support
                and trend_context_effective_confirmed
                and slope_profile_entry_ok
                and not post_dump_blocked
                and ret60_bps >= 20.0
                and ret15_bps >= 0.0
                and float(structure.slope_medium_bps) >= 3.5
                and float(structure.slope_long_bps) >= 1.0
                and down_streak <= 1
                and pos_24h_pct <= 88.0
                and not still_dumping
            )
            trend_candidate_range_relaxed = (
                not trend_candidate
                and not trend_candidate_relaxed
                and not macro_down_context
                and not countertrend_rebound
                and not rebound_in_downtrend
                and structure_phase == "range"
                and active_leg == "rise"
                and macro_soft_support
                and slope_profile_entry_ok
                and not post_dump_blocked
                and ret60_bps >= 10.0
                and float(structure.slope_medium_bps) >= 3.0
                and float(structure.slope_long_bps) >= 0.8
                and up_ratio >= 0.33
                and down_streak <= 1
                and pos_24h_pct <= 90.0
                and geo_drawdown_from_peak_bps >= max(8.0, pivot_reversal_bps * 0.25)
                and not still_dumping
            )
            if trend_only and (trend_candidate_relaxed or trend_candidate_range_relaxed):
                trend_candidate = True
                trend_ready = True
            if strict_bottom_only:
                trend_candidate = False
            if trend_only:
                bottom_candidate = False

            if strict_bottom_only:
                eligible = basic_eligible and bottom_candidate
            elif trend_only:
                eligible = basic_eligible and trend_candidate
            else:
                eligible = basic_eligible and (bottom_candidate or trend_candidate)
            if not eligible and basic_eligible:
                if strict_bottom_only:
                    if not structure_is_bottom:
                        gate_reason = f"structure_{structure_phase}"
                    elif not in_valley_context:
                        gate_reason = "no_valley_context"
                    elif too_late_rebound:
                        gate_reason = "late_rebound"
                    elif falling_now:
                        gate_reason = "falling_now"
                    else:
                        gate_reason = "no_bottom_setup"
                elif trend_only:
                    if rebound_in_downtrend:
                        gate_reason = "rebound_in_downtrend"
                    elif countertrend_rebound:
                        gate_reason = "countertrend_rebound"
                    elif has_slope_profile and not slope_profile_entry_ok:
                        gate_reason = "slope_profile_mismatch"
                    elif macro_down_context:
                        gate_reason = "macro_downtrend"
                    elif not macro_soft_support:
                        gate_reason = "no_macro_support_1h"
                    else:
                        gate_reason = f"no_trend_setup_{structure_phase}"
                else:
                    if structure_is_bad and not trend_candidate:
                        gate_reason = f"structure_{structure_phase}"
                    elif not in_valley_context and not trend_candidate:
                        gate_reason = "no_valley_context"
                    elif too_late_rebound and not trend_candidate:
                        gate_reason = "late_rebound"
                    elif falling_now and not trend_candidate:
                        gate_reason = "falling_now"
                    else:
                        gate_reason = f"no_setup_{structure_phase}"

            if has_open:
                eligible = True
                gate_reason = "keep_open"

            score_bottom = 0.0
            score_bottom += max(0.0, 54.0 - pos_24h_pct) * 2.4
            score_bottom += max(0.0, 44.0 - pos_6h_pct) * 1.2
            score_bottom += max(0.0, 10.0 - float(bars_since_valley)) * 8.0
            if geo_rebound_min_bps <= geo_rebound_from_valley_bps <= geo_rebound_max_bps:
                target = min(32.0, geo_rebound_min_bps * 2.2)
                score_bottom += 70.0 - abs(geo_rebound_from_valley_bps - target) * 0.45
            if geo_bottom_ready:
                score_bottom += 95.0
            score_bottom -= max(0.0, spread_bps - 8.0) * 2.4
            score_bottom -= max(0.0, overextension_bps - 70.0) * 0.30
            score_bottom -= max(0, down_streak - 1) * 20.0
            if structure_phase == "bottom":
                score_bottom += 42.0
            elif structure_phase == "lift_off":
                score_bottom += 66.0
            elif structure_phase in {"stall", "peak", "rollover", "downtrend"}:
                score_bottom -= 170.0
            score_bottom += structure_confidence * 30.0
            score_bottom += min(180.0, cycle_completed * 60.0)
            score_bottom += cycle_success_rate * 110.0
            if cycle_median_bars_to_exit > 0.0:
                score_bottom -= max(0.0, cycle_median_bars_to_exit - float(cycle_fit_max_bars_to_exit)) * 0.25
            score_bottom -= max(0.0, float(structure.drawdown_bps) - 25.0) * 0.35
            score_bottom -= max(0.0, pos_6h_pct - 38.0) * 1.2
            score_bottom -= max(0.0, pos_24h_pct - 52.0) * 0.9
            score_bottom -= max(0.0, geo_drawdown_from_peak_bps - max(18.0, pivot_reversal_bps * 0.8)) * 0.20
            if active_leg == "fall":
                score_bottom -= 120.0
            if down_structure:
                score_bottom -= 140.0
            if long_up_hot:
                score_bottom -= 120.0
            if too_late_rebound:
                score_bottom -= 220.0
            if not in_valley_context:
                score_bottom -= 260.0
            if not bottom_candidate:
                score_bottom -= 140.0

            score_trend = 0.0
            score_trend += min(200.0, max(0.0, ret60_bps) * 0.75)
            score_trend += min(130.0, max(0.0, ret120_bps) * 0.35)
            score_trend += min(160.0, max(0.0, ret720_bps) * 0.45)
            score_trend += min(140.0, max(0.0, ret1440_bps) * 0.30)
            score_trend += min(90.0, max(0.0, ret2880_bps) * 0.20)
            score_trend += min(90.0, max(0.0, rel60_bps) * 0.35)
            score_trend += max(0.0, rel15_bps) * 0.18
            score_trend += max(0.0, ret15_bps) * 0.40
            score_trend += up_ratio * 28.0
            score_trend += min(12.0, max(0.0, 2.0 - float(down_streak)) * 6.0)
            score_trend += max(0.0, pos_24h_pct - 18.0) * 1.10
            score_trend -= max(0.0, spread_bps - 8.0) * 2.8
            score_trend -= max(0.0, overextension_bps - 90.0) * 0.45
            score_trend -= max(0.0, pos_24h_pct - 88.0) * 2.2
            score_trend -= max(0.0, 0.70 - up_ratio) * 25.0
            score_trend -= max(0.0, -ret720_bps) * 0.80
            score_trend -= max(0.0, -ret1440_bps) * 0.95
            score_trend -= max(0.0, -ret2880_bps) * 0.55
            if structure_phase == "uptrend":
                score_trend += 86.0
            elif structure_phase == "lift_off":
                score_trend += 58.0
            elif structure_phase in {"stall", "peak", "rollover", "downtrend"}:
                score_trend -= 170.0
            if up_structure:
                score_trend += 45.0
            if down_structure and not geo_early_liftoff_trend:
                score_trend -= 140.0
            if active_leg == "rise":
                score_trend += 35.0
            elif active_leg == "fall":
                score_trend -= 120.0
            if geo_trend_ready:
                score_trend += 80.0
            if geo_early_liftoff_trend:
                score_trend += 70.0
            score_trend += structure_confidence * 32.0
            score_trend -= max(0.0, float(structure.drawdown_bps) - 20.0) * 0.45
            score_trend -= max(0.0, geo_drawdown_from_peak_bps - max(24.0, pivot_reversal_bps * 0.9)) * 0.35
            if has_slope_profile:
                score_trend += slope_profile_alignment * 110.0
                score_trend -= min(280.0, slope_profile_distance_bps_h * 2.4)
                if slope_profile_entry_ok:
                    score_trend += 22.0
                else:
                    score_trend -= 90.0
            score_trend += min(140.0, cycle_completed * 45.0)
            score_trend += cycle_success_rate * 80.0
            if corr_btc > 0.98:
                score_trend -= (corr_btc - 0.98) * 800.0
            if macro_down_context:
                score_trend -= 320.0
            if rebound_in_downtrend:
                score_trend -= 260.0
            if not macro_up_context:
                score_trend -= 220.0
            if not trend_candidate:
                score_trend -= 70.0
            staircase_score = 0.0
            staircase_score += staircase_positive_share * 90.0
            staircase_score += min(120.0, max(0.0, staircase_net_bps) * 0.30)
            staircase_score += min(45.0, max(0.0, ret60_bps - staircase_ret60_min_bps) * 0.50)
            staircase_score += min(30.0, max(0.0, staircase_pullback_count - 1.0) * 15.0)
            staircase_score -= max(0.0, staircase_max_pullback_run - 40.0) * 0.90
            staircase_score -= max(0.0, staircase_max_negative_bar_bps - 25.0) * 0.60
            staircase_score -= max(0.0, spread_bps - 8.0) * 1.80
            if staircase_trend:
                score_trend += 85.0 + max(0.0, staircase_score)

            if strict_bottom_only:
                score = score_bottom
                setup_type = "bottom"
            elif trend_only:
                score = score_trend
                setup_type = "trend"
            else:
                score = max(score_bottom, score_trend)
                setup_type = "bottom" if score_bottom > score_trend else "trend"
                if abs(score_bottom - score_trend) <= 12.0:
                    setup_type = "hybrid"

            if not eligible:
                score -= 10000.0
            if has_open:
                score += 1000.0

            rows.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "score": score,
                    "score_bottom": score_bottom,
                    "score_trend": score_trend,
                    "setup_type": setup_type,
                    "pos_pct": pos_pct,
                    "width72_pct": width_pct,
                    "pos_pct_raw72": float(corridor.get("base_pos_pct", pos_pct)),
                    "corridor_anchor_pos_pct": float(corridor.get("anchor_pos_pct", pos_pct)),
                    "corridor_near_pos_pct": float(corridor.get("near_pos_pct", pos_pct)),
                    "corridor_mid_pos_pct": float(corridor.get("mid_pos_pct", pos_pct)),
                    "corridor_long_pos_pct": float(corridor.get("long_pos_pct", pos_pct)),
                    "corridor_near_width_pct": float(corridor.get("near_width_pct", width_pct)),
                    "corridor_trend_bias": float(corridor.get("trend_bias", 0.0)),
                    "corridor_trend_shift_pct": float(corridor.get("trend_shift_pct", 0.0)),
                    "corridor_max_shift_from_base_pct": float(
                        corridor.get("max_shift_from_base_pct", 0.0)
                    ),
                    "corridor_cycle_bars": float(corridor.get("cycle_bars", 0.0)),
                    "corridor_cycle_gap_median_bars": float(
                        corridor.get("cycle_gap_median_bars", 0.0)
                    ),
                    "corridor_trough_count": float(corridor.get("trough_count", 0.0)),
                    "corridor_near_bars": float(corridor.get("near_bars", 0.0)),
                    "corridor_mid_bars": float(corridor.get("mid_bars", 0.0)),
                    "corridor_long_bars": float(corridor.get("long_bars", 0.0)),
                    "persistent_downdrift_level": str(
                        persistent_downdrift.get("level", persistent_downdrift_level)
                    ),
                    "persistent_downdrift_enabled": bool(
                        persistent_downdrift.get("enabled", False)
                    ),
                    "persistent_downdrift_data_ok": bool(
                        persistent_downdrift.get("data_ok", False)
                    ),
                    "persistent_downdrift_blocked": bool(
                        persistent_downdrift.get("blocked", False)
                    ),
                    "persistent_downdrift_reason": str(
                        persistent_downdrift.get("reason", "") or ""
                    ),
                    "persistent_downdrift_eval_mode": str(
                        persistent_downdrift.get("eval_mode", "") or ""
                    ),
                    "persistent_downdrift_eval_days": float(
                        persistent_downdrift.get("eval_days", 0.0) or 0.0
                    ),
                    "persistent_downdrift_history_days": float(
                        persistent_downdrift.get("history_days", 0.0) or 0.0
                    ),
                    "persistent_downdrift_min_required_days": float(
                        persistent_downdrift.get("min_required_days", 0.0) or 0.0
                    ),
                    "persistent_downdrift_ret180_pct": float(
                        persistent_downdrift.get("ret180_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_ret90_pct": float(
                        persistent_downdrift.get("ret90_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_rel180_pct": float(
                        persistent_downdrift.get("rel180_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_rel90_pct": float(
                        persistent_downdrift.get("rel90_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_mid_high_shift_pct": float(
                        persistent_downdrift.get("mid_high_shift_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_mid_low_shift_pct": float(
                        persistent_downdrift.get("mid_low_shift_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_long_high_shift_pct": float(
                        persistent_downdrift.get("long_high_shift_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_long_low_shift_pct": float(
                        persistent_downdrift.get("long_low_shift_pct", 0.0) or 0.0
                    ),
                    "persistent_downdrift_long_log_slope": float(
                        persistent_downdrift.get("long_log_slope", 0.0) or 0.0
                    ),
                    "non_falling_longtrend_required": bool(require_non_falling_longtrend),
                    "non_falling_longtrend_blocked": bool(non_falling_longtrend_blocked),
                    "non_falling_longtrend_reason": str(non_falling_longtrend_reason or ""),
                    "non_falling_longtrend_ret180_min_pct": float(non_falling_ret180_min_pct),
                    "non_falling_longtrend_long_high_shift_min_pct": float(
                        non_falling_long_high_shift_min_pct
                    ),
                    "non_falling_longtrend_long_low_shift_min_pct": float(
                        non_falling_long_low_shift_min_pct
                    ),
                    "non_falling_longtrend_long_log_slope_min": float(
                        non_falling_long_log_slope_min
                    ),
                    "non_falling_longtrend_rel180_min_pct": (
                        None
                        if non_falling_rel180_min_pct is None
                        else float(non_falling_rel180_min_pct)
                    ),
                    "turns24": float(turns_24h),
                    "net24_pct": net24_pct,
                    "open_notional": open_notional,
                    "keep_open": has_open,
                    "eligible": eligible,
                    "gate_reason": gate_reason,
                    "hard_excluded": False,
                    "hard_exclusion_reason": "",
                    "auto_blacklisted": auto_blacklisted,
                    "auto_blacklist_reason": auto_blacklist_reason,
                    "token_prefilter_blocked": token_prefilter_blocked,
                    "token_prefilter_reason": token_prefilter_reason,
                    "token_prefilter_reasons": token_prefilter_reasons,
                    "token_prefilter": token_prefilter_entry,
                    "token_prefilter_listing_history_days": token_prefilter_listing_history_days,
                    "is_monitoring": is_monitoring,
                    "monitoring_tags": monitoring_tags,
                    "is_problem_case": is_problem,
                    "problem_case_reasons": problem_reasons,
                    "monitoring_data_unknown": monitoring_data_unknown,
                    "problem_data_unknown": problem_data_unknown,
                    "policy_data_unknown": policy_data_unknown,
                    "excluded_with_selected_grace": bool(
                        (symbol in selected_grace)
                        and (
                            auto_blacklisted
                            or token_prefilter_blocked
                            or is_monitoring
                            or is_problem
                            or policy_data_unknown
                        )
                    ),
                    "spread_bps": spread_bps,
                    "top_depth_notional": top_depth_notional,
                    "depth_min_notional_effective": depth_min_notional_effective,
                    "depth_relaxed_applied": depth_relaxed_applied,
                    "quote_volume_5m": last_qv,
                    "quote_volume_60m": hour_qv,
                    "ret15_bps": ret15_bps,
                    "ret30_bps": ret30_bps,
                    "ret60_bps": ret60_bps,
                    "ret120_bps": ret120_bps,
                    "ret360_bps": ret360_bps,
                    "ret720_bps": ret720_bps,
                    "ret1440_bps": ret1440_bps,
                    "ret2880_bps": ret2880_bps,
                    "ret10_bps": ret10_bps,
                    "cycle_fit_ok": cycle_fit_ok,
                    "cycle_fit_static_ok": bool(cycle_fit_ok_static),
                    "cycle_fit_adaptive_ok": bool(adaptive_cycle_stats.get("fit_ok", False)),
                    "adaptive_cycle_fit_ok": bool(adaptive_cycle_stats.get("fit_ok", False)),
                    "cycle_fit_attempts": cycle_attempts,
                    "cycle_fit_completed_cycles": cycle_completed,
                    "cycle_fit_success_rate": cycle_success_rate,
                    "cycle_fit_median_bars_to_exit": cycle_median_bars_to_exit,
                    "cycle_fit_last_cycle_bars_ago": cycle_last_bars_ago,
                    "cycle_fit_entry_pct": cycle_fit_entry_pct,
                    "cycle_fit_exit_pct": cycle_fit_exit_pct,
                    "cycle_fit_lookback_bars": float(cycle_fit_lookback_bars),
                    "cycle_fit_max_bars_to_exit": float(cycle_fit_max_bars_to_exit),
                    "cycle_fit_roundtrip_fee_bps": cycle_fit_roundtrip_fee_bps,
                    "cycle_fit_gross_10pct_range_step_pct": gross_10pct_range_step_pct,
                    "cycle_fit_net_10pct_range_step_after_fees_pct": net_10pct_range_step_after_fees_pct,
                    "cycle_fit_profit_step_ok": cycle_fit_profit_step_ok,
                    "adaptive_cycle_enabled": bool(adaptive_cycle_stats.get("enabled", False)),
                    "adaptive_cycle_confidence": float(adaptive_cycle_stats.get("confidence", 0.0) or 0.0),
                    "adaptive_cycle_half_bars": float(adaptive_cycle_stats.get("half_cycle_bars", 0.0) or 0.0),
                    "adaptive_cycle_full_bars": float(adaptive_cycle_stats.get("full_cycle_bars", 0.0) or 0.0),
                    "adaptive_cycle_half_iqr_bars": float(adaptive_cycle_stats.get("half_cycle_iqr_bars", 0.0) or 0.0),
                    "adaptive_cycle_turning_points": float(adaptive_cycle_stats.get("turning_points", 0.0) or 0.0),
                    "adaptive_cycle_half_intervals": float(adaptive_cycle_stats.get("half_intervals", 0.0) or 0.0),
                    "adaptive_cycle_swing_median_bps": float(adaptive_cycle_stats.get("swing_median_bps", 0.0) or 0.0),
                    "adaptive_cycle_bars_since_last_pivot": float(
                        adaptive_cycle_stats.get("bars_since_last_pivot", 0.0) or 0.0
                    ),
                    "adaptive_cycle_window_low_bars": float(adaptive_cycle_stats.get("window_low_bars", 0.0) or 0.0),
                    "adaptive_cycle_window_high_bars": float(adaptive_cycle_stats.get("window_high_bars", 0.0) or 0.0),
                    "adaptive_cycle_window_active": bool(adaptive_cycle_stats.get("window_active", False)),
                    "adaptive_cycle_phase_progress": float(adaptive_cycle_stats.get("phase_progress", 0.0) or 0.0),
                    "adaptive_cycle_next_window_start_bars": float(
                        adaptive_cycle_stats.get("next_window_start_bars", 0.0) or 0.0
                    ),
                    "adaptive_cycle_next_window_end_bars": float(
                        adaptive_cycle_stats.get("next_window_end_bars", 0.0) or 0.0
                    ),
                    "adaptive_cycle_last_pivot_type": str(
                        adaptive_cycle_stats.get("last_pivot_type", "") or ""
                    ),
                    "rel15_bps": rel15_bps,
                    "rel60_bps": rel60_bps,
                    "corr_btc": corr_btc,
                    "up_ratio": up_ratio,
                    "down_streak": float(down_streak),
                    "up_streak": float(up_streak),
                    "still_dumping": still_dumping,
                    "post_dump_blocked": post_dump_blocked,
                    "post_dump_recovery_ready": post_dump_recovery_ready,
                    "bars_since_last_dump": float(bars_since_last_dump),
                    "bars_since_post_dump_low": float(bars_since_post_dump_low),
                    "rebound_from_post_dump_low_bps": rebound_from_post_dump_low_bps,
                    "falling_now": falling_now,
                    "previous_selloff": previous_selloff,
                    "bottom_candidate": bottom_candidate,
                    "bottom_candidate_base": bottom_candidate_base,
                    "strict_bottom_candidate": strict_bottom_candidate,
                    "trend_candidate": trend_candidate,
                    "trend_ready": trend_ready,
                    "trend_overextended": trend_overextended,
                    "broad_uptrend_context": broad_uptrend_context,
                    "strong_continuation_context": strong_continuation_context,
                    "trend_context_confirmed": trend_context_confirmed,
                    "trend_context_range_minimal": trend_context_range_minimal,
                    "trend_context_effective_confirmed": trend_context_effective_confirmed,
                    "trend_candidate_relaxed": trend_candidate_relaxed,
                    "trend_candidate_range_relaxed": trend_candidate_range_relaxed,
                    "macro_up_context": macro_up_context,
                    "macro_down_context": macro_down_context,
                    "rebound_in_downtrend": rebound_in_downtrend,
                    "rebound_in_downtrend_raw": rebound_in_downtrend_raw,
                    "clear_downtrend_pressure": clear_downtrend_pressure,
                    "rebound_strength_override": rebound_strength_override,
                    "countertrend_rebound": countertrend_rebound,
                    "mid_trend_up_1h": mid_trend_up_1h,
                    "mid_trend_up_1h_strict": mid_trend_up_1h_strict,
                    "mid_trend_up_12h": mid_trend_up_1h,
                    "mid_trend_up_12h_strict": mid_trend_up_1h_strict,
                    "mid_trend_up_6h_balanced": mid_trend_up_6h_balanced,
                    "trend_horizon_ok": trend_horizon_ok,
                    "short_horizon_range_override": short_horizon_range_override,
                    "trend_horizon_effective_ok": trend_horizon_effective_ok,
                    "short_horizon_scalp_ok": short_horizon_scalp_ok,
                    "staircase_trend": staircase_trend,
                    "staircase_score": staircase_score,
                    "staircase_positive_share": staircase_positive_share,
                    "staircase_pullback_count": staircase_pullback_count,
                    "staircase_turns": staircase_turns,
                    "staircase_max_negative_bar_bps": staircase_max_negative_bar_bps,
                    "staircase_max_pullback_run_bps": staircase_max_pullback_run,
                    "staircase_net_bps": staircase_net_bps,
                    "staircase_mean_up_bps": staircase_mean_up_bps,
                    "staircase_mean_down_bps": staircase_mean_down_bps,
                    "macro_soft_support": macro_soft_support,
                    "macro_trend_class": macro_trend_class,
                    "long_term_uptrend_context": long_term_uptrend_context,
                    "range_noise_downstructure_ok": range_noise_downstructure_ok,
                    "has_slope_profile": has_slope_profile,
                    "current_slope_bps_h": current_slope_bps_h,
                    "slope_profile_match": slope_profile_match,
                    "slope_profile_entry_ok": slope_profile_entry_ok,
                    "slope_profile_distance_bps_h": slope_profile_distance_bps_h,
                    "slope_profile_alignment": slope_profile_alignment,
                    "slope_profile_entry_alignment_min": (
                        slope_profile_alignment_entry_min if has_slope_profile else None
                    ),
                    "slope_profile_entry_distance_max_bps_h": (
                        slope_profile_distance_entry_max_bps_h if has_slope_profile else None
                    ),
                    "slope_profile_window_hours": slope_profile_window_hours,
                    "slope_profile_low_bps_h": slope_profile_low_bps_h if has_slope_profile else None,
                    "slope_profile_high_bps_h": slope_profile_high_bps_h,
                    "slope_profile_mid_bps_h": slope_profile_mid_bps_h if has_slope_profile else None,
                    "slope_profile_samples": slope_profile_samples if has_slope_profile else 0,
                    "peak_profile_class": peak_profile_class if peak_profile else None,
                    "coin_peak_blocked": coin_peak_blocked,
                    "peak_block_max_pos_24h_pct": peak_block_max_pos_24h_pct if peak_profile else None,
                    "peak_block_max_bars_since_peak": peak_block_max_bars_since_peak if peak_profile else None,
                    "peak_block_min_rebound_from_valley_bps": (
                        peak_block_min_rebound_from_valley_bps if peak_profile else None
                    ),
                    "peak_block_max_drawdown_from_peak_bps": (
                        peak_block_max_drawdown_from_peak_bps if peak_profile else None
                    ),
                    "bottom_zone": bottom_zone,
                    "in_valley_context_strict": strict_valley_context,
                    "in_valley_context_relaxed": relaxed_valley_context,
                    "in_valley_context": in_valley_context,
                    "long_up_hot": long_up_hot,
                    "active_leg": active_leg,
                    "up_structure": up_structure,
                    "down_structure": down_structure,
                    "geo_early_liftoff_trend": geo_early_liftoff_trend,
                    "pivot_reversal_bps": pivot_reversal_bps,
                    "geo_rebound_from_valley_bps": geo_rebound_from_valley_bps,
                    "geo_drawdown_from_peak_bps": geo_drawdown_from_peak_bps,
                    "bars_since_valley": float(bars_since_valley),
                    "bars_since_peak": float(bars_since_peak),
                    "fresh_bottom": fresh_bottom,
                    "recent_rebound_ready": recent_rebound_ready,
                    "base_ready": base_ready,
                    "higher_low_ready": higher_low_ready,
                    "bars_since_30m_low": float(bars_since_30m_low),
                    "bars_since_60m_low": float(bars_since_60m_low),
                    "bars_since_swing_low": float(bars_since_swing_low),
                    "pre_swing_drop_bps": pre_swing_drop_bps,
                    "swing_rebound_bps": swing_rebound_bps,
                    "rebound_from_30m_low_bps": rebound_from_30m_low_bps,
                    "rebound_from_60m_low_bps": rebound_from_60m_low_bps,
                    "pos_6h_pct": pos_6h_pct,
                    "pos_24h_pct": pos_24h_pct,
                    "micro_valley_pos_pct": valley_context_pos_pct,
                    "micro_valley_pos_mode": str(micro_valley_context.get("mode", "fallback") or "fallback"),
                    "micro_valley_pos_fallback_used": bool(
                        micro_valley_context.get("fallback_used", False)
                    ),
                    "micro_valley_anchor_low": float(micro_valley_context.get("anchor_low", 0.0) or 0.0),
                    "micro_valley_anchor_high": float(micro_valley_context.get("anchor_high", 0.0) or 0.0),
                    "micro_valley_bars_since_anchor": float(
                        micro_valley_context.get("bars_since_anchor", 0.0) or 0.0
                    ),
                    "micro_valley_trough_count": float(
                        micro_valley_context.get("trough_count", 0.0) or 0.0
                    ),
                    "micro_valley_peak_count": float(
                        micro_valley_context.get("peak_count", 0.0) or 0.0
                    ),
                    "pos_7d_pct": pos_7d_pct,
                    "pos_7d_nocrash_pct": pos_7d_nocrash_pct,
                    "crash_7d_detected": crash_7d_detected,
                    "crash_7d_event_count": crash_7d_event_count,
                    "crash_7d_min_ret30_bps": crash_7d_min_ret30_bps,
                    "crash_7d_min_ret60_bps": crash_7d_min_ret60_bps,
                    "pos_48h_pct": pos_48h_pct,
                    "overextension_bps": overextension_bps,
                    "structure_phase": structure_phase,
                    "structure_confidence": structure_confidence,
                    "structure_slope_short_bps": float(structure.slope_short_bps),
                    "structure_slope_medium_bps": float(structure.slope_medium_bps),
                    "structure_slope_long_bps": float(structure.slope_long_bps),
                    "structure_curvature_bps": float(structure.curvature_bps),
                    "structure_drawdown_bps": float(structure.drawdown_bps),
                    "structure_level_6h_pct": float(structure.level_6h) * 100.0,
                    "structure_level_24h_pct": float(structure.level_24h) * 100.0,
                }
            )
        except Exception as exc:
            err = str(exc).strip() or repr(exc)
            fetch_errors.append({"symbol": symbol, "error": err})
            if _is_rate_limit_error(err):
                rate_limit_detected = True
                break
            continue

    fallback_rows_used = False
    row_source = "live"
    fallback_reason = ""
    if allow_snapshot_fallback and (rate_limit_detected or not rows) and not rows:
        previous_rows = _load_previous_rows()
        if previous_rows:
            rows = previous_rows
            fallback_rows_used = True
            row_source = "previous_snapshot"
            fallback_reason = "rate_limit" if rate_limit_detected else "empty_live_rows"
    elif allow_snapshot_fallback and rate_limit_detected and rows:
        previous_rows = _load_previous_rows()
        if previous_rows:
            row_map: dict[str, dict[str, object]] = {}
            for item in previous_rows:
                symbol = str(item.get("symbol", "")).strip().upper()
                if symbol:
                    row_map[symbol] = item
            for item in rows:
                symbol = str(item.get("symbol", "")).strip().upper()
                if symbol:
                    row_map[symbol] = item
            rows = [item for item in row_map.values() if isinstance(item, dict)]
            fallback_rows_used = True
            row_source = "live_plus_snapshot"
            fallback_reason = "rate_limit_partial"

    rows.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    eligible_rows = [row for row in rows if bool(row.get("eligible"))]
    bottom_rank = sorted(
        [row for row in eligible_rows if bool(row.get("bottom_candidate"))],
        key=lambda item: float(item["score"]),
        reverse=True,
    )
    trend_rank = sorted(
        [row for row in eligible_rows if bool(row.get("trend_candidate"))],
        key=lambda item: float(item["score"]),
        reverse=True,
    )

    selected_symbols_priority: list[str] = []
    if strict_bottom_only:
        for row in bottom_rank:
            symbol = str(row["symbol"])
            if symbol not in selected_symbols_priority:
                selected_symbols_priority.append(symbol)
            if len(selected_symbols_priority) >= selected_target_size:
                break
    elif trend_only:
        for row in trend_rank:
            symbol = str(row["symbol"])
            if symbol not in selected_symbols_priority:
                selected_symbols_priority.append(symbol)
            if len(selected_symbols_priority) >= selected_target_size:
                break
    else:
        for row in bottom_rank[:2]:
            symbol = str(row["symbol"])
            if symbol not in selected_symbols_priority:
                selected_symbols_priority.append(symbol)
        for row in trend_rank[:2]:
            symbol = str(row["symbol"])
            if symbol not in selected_symbols_priority:
                selected_symbols_priority.append(symbol)
    for row in eligible_rows:
        symbol = str(row["symbol"])
        if symbol not in selected_symbols_priority:
            selected_symbols_priority.append(symbol)
        if len(selected_symbols_priority) >= max(selected_target_size * 3, 24):
            break

    row_map = {str(row.get("symbol", "")).strip().upper(): row for row in rows}
    if diversity_enabled:
        selected_symbols = _select_diversified_symbols(
            priority_symbols=selected_symbols_priority,
            row_map=row_map,
            returns_map=symbol_returns_short,
            target_size=selected_target_size,
            penalty=diversity_penalty,
            target_corr=diversity_target_corr,
        )
    else:
        selected_symbols = selected_symbols_priority[:selected_target_size]

    selected = [row for row in rows if str(row["symbol"]) in selected_symbols][:selected_target_size]
    selected_corr_pairs: list[float] = []
    for i in range(len(selected_symbols)):
        a = selected_symbols[i]
        ra = symbol_returns_short.get(a) or []
        if not ra:
            continue
        for j in range(i + 1, len(selected_symbols)):
            b = selected_symbols[j]
            rb = symbol_returns_short.get(b) or []
            if not rb:
                continue
            selected_corr_pairs.append(_abs_corr(ra, rb))
    selected_avg_abs_corr = (
        sum(selected_corr_pairs) / float(len(selected_corr_pairs))
        if selected_corr_pairs
        else 0.0
    )
    result = {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "row_source": row_source,
        "fallback_rows_used": fallback_rows_used,
        "fallback_reason": fallback_reason,
        "rate_limit_detected": rate_limit_detected,
        "book_ticker_source": book_ticker_source,
        "book_ticker_bulk_size": len(book_ticker_map),
        "book_ticker_bulk_error": book_ticker_bulk_error,
        "book_ticker_fallback_hits": book_ticker_fallback_hits,
        "errors_total": len(fetch_errors),
        "errors_sample": fetch_errors[:16],
        "selector_mode": selector_mode,
        "quote_asset": quote_asset,
        "candidate_source": candidate_source,
        "candidate_count": len(candidates),
        "selected_target_size": selected_target_size,
        "diversity_enabled": diversity_enabled,
        "diversity_penalty": diversity_penalty,
        "diversity_target_corr": diversity_target_corr,
        "selected_avg_abs_corr": selected_avg_abs_corr,
        "active_selected_grace_count": len(selected_grace),
        "universe_policy": universe_policy_info,
        "universe_policy_cache_file": str(UNIVERSE_POLICY_CACHE_FILE),
        "auto_blacklist": auto_blacklist_info,
        "auto_blacklist_cache_file": str(AUTO_BLACKLIST_CACHE_FILE),
        "token_prefilter": token_prefilter_info,
        "token_prefilter_cache_file": str(TOKEN_PREFILTER_CACHE_FILE),
        "include_balances": include_balances,
        "allow_snapshot_fallback": allow_snapshot_fallback,
        "max_spread_bps": max_spread_bps,
        "cycle_fit_lookback_bars": cycle_fit_lookback_bars,
        "cycle_fit_entry_pct": cycle_fit_entry_pct,
        "cycle_fit_exit_pct": cycle_fit_exit_pct,
        "cycle_fit_max_bars_to_exit": cycle_fit_max_bars_to_exit,
        "cycle_fit_min_completed": cycle_fit_min_completed,
        "cycle_fit_min_success_rate": cycle_fit_min_success_rate,
        "cycle_fit_roundtrip_fee_bps": cycle_fit_roundtrip_fee_bps,
        "cycle_fit_min_net_10pct_range_step_pct": cycle_fit_min_net_10pct_range_step_pct,
        "adaptive_cycle_enabled": adaptive_cycle_enabled,
        "adaptive_cycle_lookback_bars": adaptive_cycle_lookback_bars,
        "adaptive_cycle_extrema_window": adaptive_cycle_extrema_window,
        "adaptive_cycle_min_half_bars": adaptive_cycle_min_half_bars,
        "adaptive_cycle_max_half_bars": adaptive_cycle_max_half_bars,
        "adaptive_cycle_min_turning_points": adaptive_cycle_min_turning_points,
        "adaptive_cycle_min_swing_bps": adaptive_cycle_min_swing_bps,
        "adaptive_cycle_min_confidence": adaptive_cycle_min_confidence,
        "simple_swing_range_bars": simple_swing_range_bars,
        "simple_swing_range_crash_span_bars": simple_swing_range_crash_span_bars,
        "simple_swing_range_crash_drop_bps": simple_swing_range_crash_drop_bps,
        "persistent_downdrift_level": persistent_downdrift_level,
        "persistent_downdrift_min_days": float(_persistent_downdrift_min_days()),
        "persistent_downdrift_blocked_count": persistent_downdrift_blocked_count,
        "persistent_downdrift_thresholds": (
            dict(PERSISTENT_DOWNTREND_LEVEL_THRESHOLDS.get(persistent_downdrift_level, {}))
            if persistent_downdrift_level != "off"
            else {}
        ),
        "listing_structural_downdrift_thresholds": (
            dict(LISTING_STRUCTURAL_DOWNTREND_LEVEL_THRESHOLDS.get(persistent_downdrift_level, {}))
            if persistent_downdrift_level != "off"
            else {}
        ),
        "non_falling_longtrend_required": bool(require_non_falling_longtrend),
        "non_falling_longtrend_blocked_count": non_falling_longtrend_blocked_count,
        "non_falling_longtrend_thresholds": {
            "ret180_min_pct": float(non_falling_ret180_min_pct),
            "long_high_shift_min_pct": float(non_falling_long_high_shift_min_pct),
            "long_low_shift_min_pct": float(non_falling_long_low_shift_min_pct),
            "long_log_slope_min": float(non_falling_long_log_slope_min),
            "rel180_min_pct": (
                None
                if non_falling_rel180_min_pct is None
                else float(non_falling_rel180_min_pct)
            ),
        },
        "rebound_down_macro_only": rebound_down_macro_only,
        "rebound_down_ret60_max_bps": rebound_down_ret60_max_bps,
        "rebound_down_ret720_max_bps": rebound_down_ret60_max_bps,
        "rebound_down_ret1440_max_bps": rebound_down_ret1440_max_bps,
        "rebound_down_ret2880_max_bps": rebound_down_ret2880_max_bps,
        "post_dump_lock_bars": post_dump_lock_bars,
        "post_dump_min_rebound_bps": post_dump_min_rebound_bps,
        "post_dump_min_ret15_bps": post_dump_min_ret15_bps,
        "post_dump_max_down_streak": post_dump_max_down_streak,
        "post_dump_strong_ret15_bps": post_dump_strong_ret15_bps,
        "min_depth_notional": min_depth_notional,
        "depth_relaxed_min_notional": depth_relaxed_min_notional,
        "depth_relax_max_spread_bps": depth_relax_max_spread_bps,
        "depth_relax_min_hour_qv": depth_relax_min_hour_qv,
        "depth_relax_min_5m_qv": depth_relax_min_5m_qv,
        "min_quote_volume_5m": min_quote_volume_5m,
        "min_quote_volume_60m": min_quote_volume_60m,
        "volume_gate_require_both": volume_gate_require_both,
        "token_prefilter_min_listing_age_days": token_prefilter_min_listing_age_days,
        "slope_profile_path": str(sweetspot_path),
        "slope_profiles_loaded": len(slope_profiles),
        "selected": selected,
        "rows": rows,
    }
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
