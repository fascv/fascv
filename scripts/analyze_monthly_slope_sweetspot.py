#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.rotation_universe import POOL


BINANCE_VISION_BASE = "https://data.binance.vision/data/spot/monthly/klines"
BAR_HOURS = 5.0 / 60.0


@dataclass(frozen=True)
class SlopeBin:
    low: float
    high: Optional[float]

    @property
    def label(self) -> str:
        if self.high is None:
            return f">{self.low:.0f}"
        return f"{self.low:.0f}..{self.high:.0f}"

    @property
    def mid(self) -> float:
        if self.high is None:
            return self.low + 50.0
        return (self.low + self.high) / 2.0

    def contains(self, value: float) -> bool:
        if self.high is None:
            return value > self.low
        return self.low < value <= self.high


SLOPE_BINS: tuple[SlopeBin, ...] = (
    SlopeBin(0.0, 10.0),
    SlopeBin(10.0, 20.0),
    SlopeBin(20.0, 30.0),
    SlopeBin(30.0, 40.0),
    SlopeBin(40.0, 50.0),
    SlopeBin(50.0, 70.0),
    SlopeBin(70.0, 100.0),
    SlopeBin(100.0, 150.0),
    SlopeBin(150.0, None),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _month_back_from(year: int, month: int, back: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) - back
    return idx // 12, (idx % 12) + 1


def _latest_full_month() -> tuple[int, int]:
    now = _utc_now()
    return _month_back_from(now.year, now.month, 1)


def _zip_url(symbol: str, interval: str, year: int, month: int) -> str:
    return (
        f"{BINANCE_VISION_BASE}/{symbol}/{interval}/"
        f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
    )


def _zip_cache_path(cache_dir: Path, symbol: str, interval: str, year: int, month: int) -> Path:
    return (
        cache_dir
        / "monthly"
        / "klines"
        / symbol
        / interval
        / f"{symbol}-{interval}-{year:04d}-{month:02d}.zip"
    )


def _url_exists(url: str, timeout_sec: float) -> bool:
    req = Request(url, method="HEAD")
    try:
        with urlopen(req, timeout=timeout_sec) as resp:
            return int(getattr(resp, "status", 0) or 0) == 200
    except HTTPError as exc:
        if exc.code == 404:
            return False
        return False
    except URLError:
        return False
    except Exception:
        return False


def _download_file(url: str, out_path: Path, timeout_sec: float) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout_sec) as resp:
        payload = resp.read()
    out_path.write_bytes(payload)


def _resolve_month_zip(
    symbol: str,
    interval: str,
    month_hint: Optional[str],
    cache_dir: Path,
    lookback_months: int,
    timeout_sec: float,
    refresh: bool,
) -> tuple[Path, str]:
    if month_hint:
        year_s, month_s = month_hint.split("-", 1)
        year = int(year_s)
        month = int(month_s)
        url = _zip_url(symbol, interval, year, month)
        cache_path = _zip_cache_path(cache_dir, symbol, interval, year, month)
        if refresh or (not cache_path.exists()):
            _download_file(url, cache_path, timeout_sec=timeout_sec)
        return cache_path, f"{year:04d}-{month:02d}"

    start_year, start_month = _latest_full_month()
    for back in range(max(1, lookback_months)):
        year, month = _month_back_from(start_year, start_month, back)
        url = _zip_url(symbol, interval, year, month)
        cache_path = _zip_cache_path(cache_dir, symbol, interval, year, month)
        if cache_path.exists() and not refresh:
            return cache_path, f"{year:04d}-{month:02d}"
        if not _url_exists(url, timeout_sec=timeout_sec):
            continue
        _download_file(url, cache_path, timeout_sec=timeout_sec)
        return cache_path, f"{year:04d}-{month:02d}"
    raise FileNotFoundError(f"no monthly zip found for {symbol} within lookback={lookback_months}")


def _load_ohlcv_from_zip(path: Path) -> tuple[list[float], list[float], list[float], list[int]]:
    closes: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    opens_ms: list[int] = []
    with zipfile.ZipFile(path, "r") as zf:
        names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"zip has no csv: {path}")
        with zf.open(names[0], "r") as fh:
            reader = csv.reader((line.decode("utf-8", "ignore") for line in fh))
            for row in reader:
                if len(row) < 5:
                    continue
                try:
                    ts_raw = int(float(row[0]))
                    # Binance Vision may provide ns/us/ms based timestamps in CSV.
                    if ts_raw >= 10**17:  # nanoseconds
                        open_ms = ts_raw // 1_000_000
                    elif ts_raw >= 10**15:  # microseconds
                        open_ms = ts_raw // 1_000
                    elif ts_raw >= 10**12:  # milliseconds
                        open_ms = ts_raw
                    else:  # seconds
                        open_ms = ts_raw * 1_000
                    high = float(row[2])
                    low = float(row[3])
                    close = float(row[4])
                except Exception:
                    continue
                opens_ms.append(open_ms)
                highs.append(high)
                lows.append(low)
                closes.append(close)
    return closes, lows, highs, opens_ms


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    w = pos - lo
    return float(sorted_vals[lo] * (1.0 - w) + sorted_vals[hi] * w)


def _slope_bps_per_hour(closes: list[float], w: int, t: int, pref_y: list[float], pref_ky: list[float]) -> Optional[float]:
    i = t - w + 1
    x_mean = (w - 1) / 2.0
    denom = sum((j - x_mean) * (j - x_mean) for j in range(w))
    sum_y = pref_y[t + 1] - pref_y[i]
    mean_y = sum_y / w
    sum_ky = pref_ky[t + 1] - pref_ky[i]
    sum_xy_local = sum_ky - (i * sum_y)
    cov_num = sum_xy_local - (w * x_mean * mean_y)
    b_per_bar = cov_num / denom
    price = closes[t]
    if price <= 0.0:
        return None
    return (b_per_bar / price) * 10000.0 / BAR_HOURS


def _analyze_one_symbol(
    symbol: str,
    *,
    interval: str,
    slope_hours: float,
    month_hint: Optional[str],
    cache_dir: Path,
    lookback_months: int,
    timeout_sec: float,
    refresh: bool,
    min_samples_bin: int,
    target_mode: str,
    upper_tolerance: float,
    target_bps_h: float,
    target_span_bps_h: float,
    target_bonus: float,
    dd1_weight: float,
    dd4_weight: float,
    profit_upside75_weight: float,
    profit_upside90_weight: float,
    min_recommend_low_bps_h: float,
    effective_min_recommend_low_bps_h: float,
    min_run_median_minutes: float,
) -> dict:
    pair = f"{symbol}USDC"
    zip_path, month_used = _resolve_month_zip(
        pair,
        interval,
        month_hint=month_hint,
        cache_dir=cache_dir,
        lookback_months=lookback_months,
        timeout_sec=timeout_sec,
        refresh=refresh,
    )

    closes, lows, highs, opens_ms = _load_ohlcv_from_zip(zip_path)
    n = len(closes)
    if n < 200:
        raise RuntimeError(f"insufficient bars for {pair}: {n}")

    w = max(2, int(round(slope_hours / BAR_HOURS)))
    fwd_1h = max(1, int(round(1.0 / BAR_HOURS)))
    fwd_4h = max(1, int(round(4.0 / BAR_HOURS)))
    if n <= (w + fwd_4h + 2):
        raise RuntimeError(f"insufficient bars for slope windows {pair}: {n}")

    pref_y = [0.0] * (n + 1)
    pref_ky = [0.0] * (n + 1)
    for i, y in enumerate(closes):
        pref_y[i + 1] = pref_y[i] + y
        pref_ky[i + 1] = pref_ky[i] + (i * y)

    bins = {
        b.label: {
            "label": b.label,
            "low_bps_h": b.low,
            "high_bps_h": b.high,
            "mid_bps_h": b.mid,
            "n": 0,
            "sum_dd1_bps": 0.0,
            "sum_dd4_bps": 0.0,
            "sum_fr4_bps": 0.0,
            "cnt_dd1_le_50": 0,
            "cnt_dd4_le_100": 0,
            "cnt_fr4_pos": 0,
            "fr4_values": [],
        }
        for b in SLOPE_BINS
    }
    run_bars_by_label: dict[str, list[int]] = {b.label: [] for b in SLOPE_BINS}

    first_t = w - 1
    last_t = n - fwd_4h - 1
    current_run_label: Optional[str] = None
    current_run_len = 0
    for t in range(first_t, last_t + 1):
        slope = _slope_bps_per_hour(closes, w, t, pref_y, pref_ky)
        hit_label: Optional[str] = None
        if slope is None:
            hit = None
        else:
            hit = None
            for b in SLOPE_BINS:
                if b.contains(slope):
                    hit = bins[b.label]
                    hit_label = b.label
                    break

        if hit_label == current_run_label:
            if current_run_label is not None:
                current_run_len += 1
        else:
            if current_run_label is not None and current_run_len > 0:
                run_bars_by_label[current_run_label].append(current_run_len)
            current_run_label = hit_label
            current_run_len = 1 if hit_label is not None else 0

        if hit is None:
            continue
        px = closes[t]
        dd1 = ((min(lows[t + 1 : t + 1 + fwd_1h]) / px) - 1.0) * 10000.0
        dd4 = ((min(lows[t + 1 : t + 1 + fwd_4h]) / px) - 1.0) * 10000.0
        fr4 = ((closes[t + fwd_4h] / px) - 1.0) * 10000.0

        hit["n"] += 1
        hit["sum_dd1_bps"] += dd1
        hit["sum_dd4_bps"] += dd4
        hit["sum_fr4_bps"] += fr4
        hit["fr4_values"].append(fr4)
        if dd1 <= -50.0:
            hit["cnt_dd1_le_50"] += 1
        if dd4 <= -100.0:
            hit["cnt_dd4_le_100"] += 1
        if fr4 > 0.0:
            hit["cnt_fr4_pos"] += 1
    if current_run_label is not None and current_run_len > 0:
        run_bars_by_label[current_run_label].append(current_run_len)

    bin_rows: list[dict] = []
    for b in SLOPE_BINS:
        row = bins[b.label]
        n_bin = int(row["n"])
        if n_bin <= 0:
            continue
        mean_dd1 = float(row["sum_dd1_bps"]) / n_bin
        mean_dd4 = float(row["sum_dd4_bps"]) / n_bin
        mean_fr4 = float(row["sum_fr4_bps"]) / n_bin
        p_dd1 = 100.0 * float(row["cnt_dd1_le_50"]) / n_bin
        p_dd4 = 100.0 * float(row["cnt_dd4_le_100"]) / n_bin
        p_fr4_pos = 100.0 * float(row["cnt_fr4_pos"]) / n_bin
        fr4_sorted = sorted(float(v) for v in row["fr4_values"])
        run_bars = sorted(int(x) for x in run_bars_by_label.get(b.label, []) if int(x) > 0)
        run_count = len(run_bars)
        mean_run_hours = (float(sum(run_bars)) / run_count) * BAR_HOURS if run_count > 0 else 0.0
        median_run_hours = _quantile(run_bars, 0.50) * BAR_HOURS if run_count > 0 else 0.0
        p90_run_hours = _quantile(run_bars, 0.90) * BAR_HOURS if run_count > 0 else 0.0
        max_run_hours = (max(run_bars) * BAR_HOURS) if run_count > 0 else 0.0
        median_fr4 = _quantile(fr4_sorted, 0.50)
        q75_fr4 = _quantile(fr4_sorted, 0.75)
        q90_fr4 = _quantile(fr4_sorted, 0.90)
        upside_score = (profit_upside75_weight * max(0.0, q75_fr4)) + (
            profit_upside90_weight * max(0.0, q90_fr4)
        )
        profit_score = mean_fr4 + upside_score
        raw_score = profit_score - (dd1_weight * abs(mean_dd1)) - (dd4_weight * abs(mean_dd4))
        target_distance = abs(float(row["mid_bps_h"]) - target_bps_h)
        target_prox = max(0.0, 1.0 - (target_distance / max(1e-9, target_span_bps_h)))
        biased_score = raw_score + (target_bonus * target_prox)
        bin_rows.append(
            {
                "label": row["label"],
                "low_bps_h": row["low_bps_h"],
                "high_bps_h": row["high_bps_h"],
                "mid_bps_h": row["mid_bps_h"],
                "n": n_bin,
                "p_dd1_le_50_pct": p_dd1,
                "p_dd4_le_100_pct": p_dd4,
                "p_fr4_pos_pct": p_fr4_pos,
                "run_count": run_count,
                "mean_run_hours": mean_run_hours,
                "median_run_hours": median_run_hours,
                "p90_run_hours": p90_run_hours,
                "max_run_hours": max_run_hours,
                "mean_dd1_bps": mean_dd1,
                "mean_dd4_bps": mean_dd4,
                "mean_fr4_bps": mean_fr4,
                "median_fr4_bps": median_fr4,
                "q75_fr4_bps": q75_fr4,
                "q90_fr4_bps": q90_fr4,
                "profit_score": profit_score,
                "raw_score": raw_score,
                "target_proximity": target_prox,
                "biased_score": biased_score,
                "eligible_for_pick": n_bin >= min_samples_bin,
            }
        )

    if not bin_rows:
        raise RuntimeError(f"no slope bins for {pair}")

    eligible = [r for r in bin_rows if bool(r["eligible_for_pick"])]
    pool = eligible if eligible else bin_rows
    tradable_pool = [
        r for r in pool
        if float(r["low_bps_h"]) >= float(effective_min_recommend_low_bps_h)
    ]
    run_gate_h = max(0.0, float(min_run_median_minutes)) / 60.0
    if run_gate_h > 0.0:
        tradable_by_run = [
            r for r in tradable_pool
            if float(r.get("median_run_hours", 0.0)) >= run_gate_h
        ]
        if tradable_by_run:
            tradable_pool = tradable_by_run
    if tradable_pool:
        selection_pool = tradable_pool
        recommendation_fallback = False
    else:
        selection_pool = pool
        recommendation_fallback = True

    if str(target_mode).lower() == "auto":
        best_raw = max(float(r["raw_score"]) for r in selection_pool)
        near_best = [
            r for r in selection_pool
            if float(r["raw_score"]) >= (best_raw - max(0.0, float(upper_tolerance)))
        ]
        best = max(near_best, key=lambda r: (float(r["mid_bps_h"]), float(r["raw_score"]), float(r["n"])))
    else:
        best = max(selection_pool, key=lambda r: (float(r["biased_score"]), float(r["mid_bps_h"]), float(r["n"])))

    start_iso = datetime.fromtimestamp(opens_ms[0] / 1000.0, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(opens_ms[-1] / 1000.0, tz=timezone.utc).isoformat()
    return {
        "symbol": symbol,
        "pair": f"{symbol}/USDC",
        "month": month_used,
        "zip_path": str(zip_path),
        "bars": n,
        "from_utc": start_iso,
        "to_utc": end_iso,
        "slope_window_hours": slope_hours,
        "bin_min_samples": min_samples_bin,
        "recommendation": {
            "label": best["label"],
            "low_bps_h": best["low_bps_h"],
            "high_bps_h": best["high_bps_h"],
            "mid_bps_h": best["mid_bps_h"],
            "n": best["n"],
            "p_dd1_le_50_pct": best["p_dd1_le_50_pct"],
            "p_dd4_le_100_pct": best["p_dd4_le_100_pct"],
            "p_fr4_pos_pct": best["p_fr4_pos_pct"],
            "run_count": best["run_count"],
            "mean_run_hours": best["mean_run_hours"],
            "median_run_hours": best["median_run_hours"],
            "p90_run_hours": best["p90_run_hours"],
            "max_run_hours": best["max_run_hours"],
            "mean_fr4_bps": best["mean_fr4_bps"],
            "median_fr4_bps": best["median_fr4_bps"],
            "q75_fr4_bps": best["q75_fr4_bps"],
            "q90_fr4_bps": best["q90_fr4_bps"],
            "profit_score": best["profit_score"],
            "raw_score": best["raw_score"],
            "biased_score": best["biased_score"],
            "target_proximity": best["target_proximity"],
            "target_mode": str(target_mode).lower(),
            "upper_tolerance_raw_score": upper_tolerance,
            "upper_bias_target_bps_h": target_bps_h,
            "min_recommend_low_bps_h": float(min_recommend_low_bps_h),
            "effective_min_recommend_low_bps_h": float(effective_min_recommend_low_bps_h),
            "min_run_median_minutes": float(min_run_median_minutes),
            "recommendation_fallback": recommendation_fallback,
        },
        "bins": sorted(bin_rows, key=lambda r: float(r["mid_bps_h"])),
    }


def _parse_symbols(arg: str) -> list[str]:
    raw = [x.strip().upper() for x in str(arg or "").split(",")]
    return [x for x in raw if x]


def _break_even_floor_bps_h(break_even_bps: float, hold_minutes: float) -> float:
    hold_h = max(1e-9, float(hold_minutes) / 60.0)
    return max(0.0, float(break_even_bps)) / hold_h


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Download one monthly Binance Vision ZIP per coin and compute slope sweetspots "
            "with upper-range preference (coin-specific in auto mode)."
        )
    )
    ap.add_argument("--interval", default="5m", help="kline interval (default: 5m)")
    ap.add_argument(
        "--symbols",
        default="ALL",
        help="comma-separated symbols without quote (e.g. KITE,TIA) or ALL (default)",
    )
    ap.add_argument(
        "--month",
        default="",
        help="fixed month YYYY-MM; default uses most recent available monthly ZIP per symbol",
    )
    ap.add_argument("--workers", type=int, default=8, help="parallel workers")
    ap.add_argument("--lookback-months", type=int, default=18, help="auto-search depth")
    ap.add_argument("--timeout-sec", type=float, default=12.0, help="network timeout")
    ap.add_argument("--refresh", action="store_true", help="force re-download ZIPs")
    ap.add_argument("--cache-dir", default="data/binance_vision", help="ZIP cache dir")
    ap.add_argument("--output", default="logs/rotation_slope_sweetspots.json", help="output JSON")
    ap.add_argument("--slope-hours", type=float, default=4.0, help="slope window in hours")
    ap.add_argument("--min-samples-bin", type=int, default=120, help="minimum bin size for pick")
    ap.add_argument(
        "--target-mode",
        choices=("auto", "fixed"),
        default="auto",
        help="auto: coin-specific upper-biased near-best raw score; fixed: use target-bps-h bias",
    )
    ap.add_argument(
        "--upper-tolerance",
        type=float,
        default=15.0,
        help="in auto mode, allow bins within this raw-score distance from the best before preferring upper range",
    )
    ap.add_argument("--target-bps-h", type=float, default=30.0, help="upper-bias target slope")
    ap.add_argument("--target-span-bps-h", type=float, default=20.0, help="target bias span")
    ap.add_argument("--target-bonus", type=float, default=10.0, help="max bonus near target")
    ap.add_argument("--dd1-weight", type=float, default=0.6, help="penalty weight for 1h drawdown")
    ap.add_argument("--dd4-weight", type=float, default=0.2, help="penalty weight for 4h drawdown")
    ap.add_argument(
        "--profit-upside75-weight",
        type=float,
        default=0.35,
        help="weight for positive 75th percentile forward-return (profit potential)",
    )
    ap.add_argument(
        "--profit-upside90-weight",
        type=float,
        default=0.65,
        help="weight for positive 90th percentile forward-return (high-upside potential)",
    )
    ap.add_argument(
        "--min-recommend-low-bps-h",
        type=float,
        default=0.0,
        help="manual lower slope floor (final floor is max(this, break-even floor))",
    )
    ap.add_argument(
        "--min-run-median-minutes",
        type=float,
        default=30.0,
        help="minimum median contiguous run duration for recommended sweetspot",
    )
    ap.add_argument(
        "--break-even-bps",
        type=float,
        default=24.0,
        help="estimated roundtrip break-even cost in bps (fees+slippage)",
    )
    ap.add_argument(
        "--break-even-hold-minutes",
        type=float,
        default=30.0,
        help="target hold horizon in minutes for converting break-even into bps/h floor",
    )
    ap.add_argument(
        "--break-even-margin-bps",
        type=float,
        default=0.0,
        help="extra bps safety margin added on top of break-even before floor conversion",
    )
    args = ap.parse_args()

    if args.symbols.upper() == "ALL":
        symbols = list(POOL)
    else:
        symbols = _parse_symbols(args.symbols)
    if not symbols:
        raise SystemExit("no symbols selected")

    cache_dir = Path(args.cache_dir).resolve()
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    break_even_total_bps = max(0.0, float(args.break_even_bps) + float(args.break_even_margin_bps))
    break_even_floor_bps_h = _break_even_floor_bps_h(
        break_even_total_bps, float(args.break_even_hold_minutes)
    )
    effective_min_recommend_low_bps_h = max(
        float(args.min_recommend_low_bps_h),
        break_even_floor_bps_h,
    )

    started = _utc_now().isoformat()
    results: list[dict] = []
    errors: list[dict] = []

    with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
        fut_map = {
            ex.submit(
                _analyze_one_symbol,
                symbol,
                interval=str(args.interval),
                slope_hours=float(args.slope_hours),
                month_hint=(str(args.month).strip() or None),
                cache_dir=cache_dir,
                lookback_months=max(1, int(args.lookback_months)),
                timeout_sec=float(args.timeout_sec),
                refresh=bool(args.refresh),
                min_samples_bin=max(1, int(args.min_samples_bin)),
                target_mode=str(args.target_mode),
                upper_tolerance=float(args.upper_tolerance),
                target_bps_h=float(args.target_bps_h),
                target_span_bps_h=max(1e-9, float(args.target_span_bps_h)),
                target_bonus=float(args.target_bonus),
                dd1_weight=float(args.dd1_weight),
                dd4_weight=float(args.dd4_weight),
                profit_upside75_weight=float(args.profit_upside75_weight),
                profit_upside90_weight=float(args.profit_upside90_weight),
                min_recommend_low_bps_h=float(args.min_recommend_low_bps_h),
                effective_min_recommend_low_bps_h=float(effective_min_recommend_low_bps_h),
                min_run_median_minutes=float(args.min_run_median_minutes),
            ): symbol
            for symbol in symbols
        }

        done_count = 0
        total = len(fut_map)
        for fut in as_completed(fut_map):
            symbol = fut_map[fut]
            done_count += 1
            try:
                item = fut.result()
                results.append(item)
                rec = item["recommendation"]
                print(
                    f"[{done_count:>3}/{total}] {symbol}: {rec['label']} "
                    f"(mid={float(rec['mid_bps_h']):.1f} bps/h, n={int(rec['n'])})"
                )
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
                print(f"[{done_count:>3}/{total}] {symbol}: ERROR {exc}")

    results.sort(key=lambda r: str(r.get("symbol", "")))
    summary_bins: dict[str, int] = {}
    for r in results:
        label = str(r["recommendation"]["label"])
        summary_bins[label] = summary_bins.get(label, 0) + 1

    payload = {
        "ok": True,
        "generated_at": _utc_now().isoformat(),
        "started_at": started,
        "symbols_requested": len(symbols),
        "symbols_succeeded": len(results),
        "symbols_failed": len(errors),
        "params": {
            "interval": args.interval,
            "month": (str(args.month).strip() or None),
            "slope_hours": args.slope_hours,
            "min_samples_bin": args.min_samples_bin,
            "target_mode": args.target_mode,
            "upper_tolerance": args.upper_tolerance,
            "target_bps_h": args.target_bps_h,
            "target_span_bps_h": args.target_span_bps_h,
            "target_bonus": args.target_bonus,
            "dd1_weight": args.dd1_weight,
            "dd4_weight": args.dd4_weight,
            "profit_upside75_weight": args.profit_upside75_weight,
            "profit_upside90_weight": args.profit_upside90_weight,
            "min_recommend_low_bps_h": args.min_recommend_low_bps_h,
            "min_run_median_minutes": args.min_run_median_minutes,
            "break_even_bps": args.break_even_bps,
            "break_even_hold_minutes": args.break_even_hold_minutes,
            "break_even_margin_bps": args.break_even_margin_bps,
            "break_even_total_bps": break_even_total_bps,
            "break_even_floor_bps_h": break_even_floor_bps_h,
            "effective_min_recommend_low_bps_h": effective_min_recommend_low_bps_h,
        },
        "summary_recommendation_counts": summary_bins,
        "results": results,
        "errors": errors,
    }

    out_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print("")
    print(json.dumps({"ok": True, "output": str(out_path), "succeeded": len(results), "failed": len(errors)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
