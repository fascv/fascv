from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True)
class MarketStructure:
    phase: str
    confidence: float
    slope_short_bps: float
    slope_medium_bps: float
    slope_long_bps: float
    curvature_bps: float
    range_pos: float
    rebound_bps: float
    drawdown_bps: float
    extension_bps: float
    level_6h: float = 0.5
    level_24h: float = 0.5
    pivot_reversal_bps: float = 0.0
    bars_since_valley: int = 999
    bars_since_peak: int = 999
    rebound_from_valley_bps: float = 0.0
    drawdown_from_peak_bps: float = 0.0
    up_structure: bool = False
    down_structure: bool = False
    active_leg: str = "flat"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _smooth(closes: Sequence[float], window: int = 3) -> list[float]:
    if window <= 1 or len(closes) <= 2:
        return [float(v) for v in closes]
    # Two-pass EMA reduces noise while preserving turning points.
    w = max(2, int(window))
    alpha = 2.0 / (float(w) + 1.0)
    forward: list[float] = []
    prev = float(closes[0])
    for value in closes:
        current = (alpha * float(value)) + ((1.0 - alpha) * prev)
        forward.append(current)
        prev = current
    backward: list[float] = []
    prev = float(forward[-1])
    for value in reversed(forward):
        current = (alpha * float(value)) + ((1.0 - alpha) * prev)
        backward.append(current)
        prev = current
    return list(reversed(backward))


def _median(values: Sequence[float]) -> float:
    xs = sorted(float(v) for v in values)
    if not xs:
        return 0.0
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def _mad(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    med = _median(values)
    return _median([abs(float(value) - med) for value in values])


def _slope_bps(
    closes: Sequence[float],
    window: int,
    *,
    bar_seconds: int = 300,
    normalize_seconds: int | None = None,
) -> float:
    n = min(len(closes), max(2, int(window)))
    if n < 2:
        return 0.0
    # Use log-prices for scale-stable slope estimation.
    segment = [float(v) for v in closes[-n:] if float(v) > 0.0]
    if len(segment) < 2:
        return 0.0
    logs = [math.log(v) for v in segment]
    mean_x = (n - 1) / 2.0
    denom = sum((idx - mean_x) ** 2 for idx in range(n))
    if denom <= 0.0:
        return 0.0
    mean_y = sum(logs) / float(n)
    num = sum((idx - mean_x) * (logs[idx] - mean_y) for idx in range(n))
    slope_per_bar = num / denom
    slope_bps = slope_per_bar * 10000.0
    if normalize_seconds is not None:
        ref_seconds = max(1, int(normalize_seconds))
        bar_seconds = max(1, int(bar_seconds))
        slope_bps *= float(ref_seconds) / float(bar_seconds)
    return slope_bps


def _range_position(closes: Sequence[float], window: int) -> tuple[float, float, float]:
    n = min(len(closes), max(2, int(window)))
    segment = [float(v) for v in closes[-n:]]
    lo = min(segment)
    hi = max(segment)
    last = segment[-1]
    if hi <= lo:
        return 0.5, lo, hi
    return _clamp((last - lo) / (hi - lo), 0.0, 1.0), lo, hi


def _collapse_same_type(points: Sequence[tuple[int, float, str]]) -> list[tuple[int, float, str]]:
    out: list[tuple[int, float, str]] = []
    for idx, price, kind in points:
        if not out:
            out.append((idx, price, kind))
            continue
        last_idx, last_price, last_kind = out[-1]
        if kind != last_kind:
            out.append((idx, price, kind))
            continue
        if kind == "valley" and price <= last_price:
            out[-1] = (idx, price, kind)
        elif kind == "peak" and price >= last_price:
            out[-1] = (idx, price, kind)
        else:
            out[-1] = (last_idx, last_price, last_kind)
    return sorted(out, key=lambda item: item[0])


def _zigzag_pivots(
    closes: Sequence[float],
    reversal_bps: float,
    *,
    min_bars: int = 2,
) -> list[tuple[int, float, str]]:
    n = len(closes)
    if n < 3:
        return []
    prices = [float(v) for v in closes]
    rev = max(1.0, float(reversal_bps))
    min_span = max(1, int(min_bars))

    pivots: list[tuple[int, float, str]] = []
    direction = 0  # 1=up leg (seeking peak), -1=down leg (seeking valley)
    low_idx = 0
    high_idx = 0
    low_price = prices[0]
    high_price = prices[0]

    for idx in range(1, n):
        price = prices[idx]
        if price <= 0.0:
            continue

        if direction == 0:
            if price <= low_price:
                low_idx, low_price = idx, price
            if price >= high_price:
                high_idx, high_price = idx, price
            up_from_low_bps = ((price / low_price) - 1.0) * 10000.0 if low_price > 0.0 else 0.0
            down_from_high_bps = ((high_price / price) - 1.0) * 10000.0 if price > 0.0 else 0.0
            if up_from_low_bps >= rev and (idx - low_idx) >= min_span:
                pivots.append((low_idx, low_price, "valley"))
                direction = 1
                high_idx, high_price = idx, price
            elif down_from_high_bps >= rev and (idx - high_idx) >= min_span:
                pivots.append((high_idx, high_price, "peak"))
                direction = -1
                low_idx, low_price = idx, price
            continue

        if direction == 1:
            if price >= high_price:
                high_idx, high_price = idx, price
            drawdown_bps = ((high_price / price) - 1.0) * 10000.0 if price > 0.0 else 0.0
            if drawdown_bps >= rev and (idx - high_idx) >= min_span:
                pivots.append((high_idx, high_price, "peak"))
                direction = -1
                low_idx, low_price = idx, price
            continue

        if price <= low_price:
            low_idx, low_price = idx, price
        rebound_bps = ((price / low_price) - 1.0) * 10000.0 if low_price > 0.0 else 0.0
        if rebound_bps >= rev and (idx - low_idx) >= min_span:
            pivots.append((low_idx, low_price, "valley"))
            direction = 1
            high_idx, high_price = idx, price

    # Keep only confirmed pivots (terminal extrema stay implicit in active leg).
    if direction == 0 and high_idx != low_idx:
        if high_idx > low_idx:
            pivots.append((low_idx, low_price, "valley"))
            pivots.append((high_idx, high_price, "peak"))
        else:
            pivots.append((high_idx, high_price, "peak"))
            pivots.append((low_idx, low_price, "valley"))

    pivots = _collapse_same_type(pivots)
    if len(pivots) <= 2:
        return pivots

    filtered: list[tuple[int, float, str]] = [pivots[0]]
    swing_gate = rev * 0.60
    for idx, price, kind in pivots[1:]:
        prev_idx, prev_price, prev_kind = filtered[-1]
        if kind == prev_kind:
            if kind == "valley" and price <= prev_price:
                filtered[-1] = (idx, price, kind)
            elif kind == "peak" and price >= prev_price:
                filtered[-1] = (idx, price, kind)
            continue
        if prev_price <= 0.0:
            continue
        swing_bps = abs(((price / prev_price) - 1.0) * 10000.0)
        if swing_bps >= swing_gate:
            filtered.append((idx, price, kind))
    return filtered


def _last_pivot(
    pivots: Sequence[tuple[int, float, str]],
    kind: str,
) -> tuple[int, float, str] | None:
    for pivot in reversed(pivots):
        if pivot[2] == kind:
            return pivot
    return None


def _fallback_extreme_idx(closes: Sequence[float], window: int, mode: str) -> int:
    n = len(closes)
    if n <= 0:
        return 0
    w = min(n, max(2, int(window)))
    start = n - w
    segment = [float(v) for v in closes[start:]]
    if mode == "low":
        local_idx = min(range(len(segment)), key=lambda idx: segment[idx])
    else:
        local_idx = max(range(len(segment)), key=lambda idx: segment[idx])
    return start + local_idx


def classify_market_structure(
    closes: Sequence[float],
    *,
    short_window: int = 4,
    medium_window: int = 12,
    long_window: int = 36,
    smooth_window: int = 3,
    bar_seconds: int = 300,
    slope_normalize_seconds: int | None = None,
    level_6h_window: int = 72,
    level_24h_window: int = 288,
) -> MarketStructure:
    values = [float(v) for v in closes if float(v) > 0.0]
    min_bars = max(12, int(long_window))
    if len(values) < min_bars:
        return MarketStructure(
            phase="unknown",
            confidence=0.0,
            slope_short_bps=0.0,
            slope_medium_bps=0.0,
            slope_long_bps=0.0,
            curvature_bps=0.0,
            range_pos=0.5,
            rebound_bps=0.0,
            drawdown_bps=0.0,
            extension_bps=0.0,
            level_6h=0.5,
            level_24h=0.5,
            pivot_reversal_bps=0.0,
            bars_since_valley=999,
            bars_since_peak=999,
            rebound_from_valley_bps=0.0,
            drawdown_from_peak_bps=0.0,
            up_structure=False,
            down_structure=False,
            active_leg="flat",
        )

    smooth = _smooth(values, window=smooth_window)
    slope_short = _slope_bps(
        smooth,
        short_window,
        bar_seconds=bar_seconds,
        normalize_seconds=slope_normalize_seconds,
    )
    slope_medium = _slope_bps(
        smooth,
        medium_window,
        bar_seconds=bar_seconds,
        normalize_seconds=slope_normalize_seconds,
    )
    slope_long = _slope_bps(
        smooth,
        long_window,
        bar_seconds=bar_seconds,
        normalize_seconds=slope_normalize_seconds,
    )
    curvature = (slope_short - slope_medium) * 0.70 + (slope_medium - slope_long) * 0.30
    range_pos, local_low, local_high = _range_position(smooth, long_window)
    level_6h, _, _ = _range_position(smooth, level_6h_window)
    level_24h, _, _ = _range_position(smooth, level_24h_window)
    last = float(smooth[-1])
    rebound_bps = ((last / local_low) - 1.0) * 10000.0 if local_low > 0.0 else 0.0
    drawdown_bps = ((local_high - last) / local_high) * 10000.0 if local_high > 0.0 else 0.0
    mean_long = sum(smooth[-long_window:]) / float(long_window)
    extension_bps = ((last / mean_long) - 1.0) * 10000.0 if mean_long > 0.0 else 0.0
    abs_rets_bps: list[float] = []
    for a, b in zip(smooth[-145:-1], smooth[-144:]):
        if a <= 0.0:
            continue
        abs_rets_bps.append(abs(math.log(b / a)) * 10000.0)
    noise_bps = _median(abs_rets_bps)
    noise_mad_bps = _mad(abs_rets_bps) * 1.4826
    vol_bps = max(noise_bps, noise_mad_bps)
    pivot_reversal_bps = _clamp(max(20.0, noise_bps * 4.0, vol_bps * 3.2), 20.0, 220.0)
    pivots = _zigzag_pivots(smooth, pivot_reversal_bps, min_bars=max(2, int(short_window // 2)))

    last_valley = _last_pivot(pivots, "valley")
    last_peak = _last_pivot(pivots, "peak")
    if last_valley is None:
        idx = _fallback_extreme_idx(smooth, long_window, "low")
        last_valley = (idx, float(smooth[idx]), "valley")
    if last_peak is None:
        idx = _fallback_extreme_idx(smooth, long_window, "high")
        last_peak = (idx, float(smooth[idx]), "peak")

    valley_idx, valley_price, _ = last_valley
    peak_idx, peak_price, _ = last_peak
    bars_since_valley = max(0, len(smooth) - 1 - int(valley_idx))
    bars_since_peak = max(0, len(smooth) - 1 - int(peak_idx))
    rebound_from_valley_bps = ((last / valley_price) - 1.0) * 10000.0 if valley_price > 0.0 else 0.0
    drawdown_from_peak_bps = ((peak_price - last) / peak_price) * 10000.0 if peak_price > 0.0 else 0.0

    peaks = [pivot for pivot in pivots if pivot[2] == "peak"]
    valleys = [pivot for pivot in pivots if pivot[2] == "valley"]
    trend_step_bps = max(3.0, pivot_reversal_bps * 0.14)
    up_structure = False
    down_structure = False
    if len(peaks) >= 2 and len(valleys) >= 2:
        last_peak_ret = ((peaks[-1][1] / peaks[-2][1]) - 1.0) * 10000.0 if peaks[-2][1] > 0.0 else 0.0
        last_valley_ret = (
            ((valleys[-1][1] / valleys[-2][1]) - 1.0) * 10000.0 if valleys[-2][1] > 0.0 else 0.0
        )
        up_structure = last_peak_ret >= trend_step_bps and last_valley_ret >= trend_step_bps * 0.8
        down_structure = last_peak_ret <= -trend_step_bps and last_valley_ret <= -trend_step_bps * 0.8

    active_leg = "flat"
    if pivots:
        _, pivot_price, pivot_kind = pivots[-1]
        eps = 0.0002
        if pivot_kind == "valley":
            if last >= pivot_price * (1.0 + eps):
                active_leg = "rise"
        else:
            if last <= pivot_price * (1.0 - eps):
                active_leg = "fall"
    if active_leg == "rise" and slope_short <= -2.0:
        active_leg = "fall"
    elif active_leg == "fall" and slope_short >= 2.0:
        active_leg = "rise"
    elif active_leg == "flat":
        if slope_short >= 2.0:
            active_leg = "rise"
        elif slope_short <= -2.0:
            active_leg = "fall"

    slope_flat_bps = max(3.0, vol_bps * 0.08)
    # Keep gentle but persistent trends classified as trends after interval normalization.
    slope_trend_bps = max(5.0, vol_bps * 0.18)
    curve_turn_bps = max(3.0, vol_bps * 0.08)
    turning_up = slope_short >= slope_flat_bps and curvature >= curve_turn_bps
    turning_down = slope_short <= -slope_flat_bps and curvature <= -curve_turn_bps
    near_bottom = level_24h <= 0.35
    near_top = level_24h >= 0.72
    strong_up = slope_medium >= slope_trend_bps and slope_long >= -2.0
    strong_down = slope_medium <= -slope_trend_bps and slope_long <= 2.0

    phase = "range"
    rebound_gate_bps = max(8.0, pivot_reversal_bps * 0.25)
    pullback_gate_bps = max(8.0, pivot_reversal_bps * 0.32)
    fresh_valley = bars_since_valley <= 10
    fresh_peak = bars_since_peak <= 10

    if down_structure and strong_down:
        phase = "downtrend"
    elif (
        up_structure
        and strong_up
        and active_leg != "fall"
        and (
            not near_top
            or (
                drawdown_from_peak_bps <= max(4.0, pullback_gate_bps * 0.35)
                and slope_short > 0.0
            )
        )
    ):
        phase = "uptrend"
    elif fresh_peak and drawdown_from_peak_bps >= pullback_gate_bps and turning_down:
        phase = "rollover" if near_top else "downtrend"
    elif near_top and active_leg == "fall" and drawdown_from_peak_bps >= max(6.0, pullback_gate_bps * 0.5):
        phase = "rollover"
    elif near_top and turning_down and active_leg == "fall":
        phase = "peak"
    elif near_top and active_leg in {"flat", "fall"} and abs(slope_short) <= (slope_flat_bps * 1.2):
        if drawdown_from_peak_bps >= max(6.0, pullback_gate_bps * 0.5):
            phase = "peak"
        else:
            phase = "stall"
    elif near_top and abs(slope_short) <= slope_flat_bps and abs(slope_medium) <= slope_flat_bps:
        phase = "stall"
    elif fresh_valley and near_bottom and rebound_from_valley_bps <= rebound_gate_bps and abs(slope_short) <= 6.0:
        phase = "bottom"
    elif fresh_valley and rebound_from_valley_bps >= rebound_gate_bps and turning_up and active_leg != "fall":
        phase = "lift_off"
    elif strong_up and active_leg != "fall" and (not near_top or slope_short > (slope_flat_bps * 0.5)):
        phase = "uptrend"
    elif strong_down and active_leg != "rise":
        phase = "downtrend"
    elif near_bottom and turning_up:
        phase = "lift_off"
    elif near_bottom:
        phase = "bottom"

    churn = _clamp(float(len(pivots)) / max(6.0, float(len(smooth)) * 0.22), 0.0, 1.0)

    if phase == "uptrend":
        confidence = _clamp(
            (max(0.0, slope_medium) / 16.0)
            + (max(0.0, slope_long) / 16.0)
            + (0.20 if up_structure else 0.0)
            + (0.10 if active_leg == "rise" else 0.0),
            0.0,
            1.0,
        ) * (1.0 - (0.15 * churn))
    elif phase == "lift_off":
        confidence = _clamp(
            (max(0.0, rebound_from_valley_bps) / max(12.0, pivot_reversal_bps * 1.2)) * 0.50
            + (max(0.0, slope_short) / 14.0) * 0.30
            + (max(0.0, curvature) / 12.0) * 0.20,
            0.0,
            1.0,
        )
    elif phase == "bottom":
        confidence = _clamp(
            (max(0.0, 0.42 - level_24h) / 0.42) * 0.50
            + max(0.0, 1.0 - (abs(slope_short) / 10.0)) * 0.30
            + (0.20 if fresh_valley else 0.0),
            0.0,
            1.0,
        )
    elif phase in {"stall", "peak"}:
        confidence = _clamp(
            ((level_24h - 0.68) / 0.32) * 0.45
            + (max(0.0, drawdown_from_peak_bps) / max(20.0, pivot_reversal_bps * 1.4)) * 0.35
            + (max(0.0, -slope_short) / 12.0) * 0.20,
            0.0,
            1.0,
        )
    elif phase in {"rollover", "downtrend"}:
        confidence = _clamp(
            (max(0.0, -slope_medium) / 16.0)
            + (max(0.0, drawdown_from_peak_bps) / max(20.0, pivot_reversal_bps * 1.2)) * 0.35
            + (0.20 if down_structure else 0.0),
            0.0,
            1.0,
        ) * (1.0 - (0.15 * churn))
    else:
        confidence = _clamp((abs(slope_medium) + abs(curvature)) / 32.0, 0.0, 1.0)

    return MarketStructure(
        phase=phase,
        confidence=confidence,
        slope_short_bps=slope_short,
        slope_medium_bps=slope_medium,
        slope_long_bps=slope_long,
        curvature_bps=curvature,
        range_pos=range_pos,
        rebound_bps=rebound_bps,
        drawdown_bps=drawdown_bps,
        extension_bps=extension_bps,
        level_6h=level_6h,
        level_24h=level_24h,
        pivot_reversal_bps=pivot_reversal_bps,
        bars_since_valley=bars_since_valley,
        bars_since_peak=bars_since_peak,
        rebound_from_valley_bps=rebound_from_valley_bps,
        drawdown_from_peak_bps=drawdown_from_peak_bps,
        up_structure=up_structure,
        down_structure=down_structure,
        active_leg=active_leg,
    )
