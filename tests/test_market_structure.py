from __future__ import annotations

from trading.market_structure import classify_market_structure


def _series(start: float, step: float, n: int) -> list[float]:
    out: list[float] = []
    value = start
    for _ in range(n):
        out.append(value)
        value += step
    return out


def _upsample_linear(values: list[float], factor: int) -> list[float]:
    if factor <= 1 or len(values) <= 1:
        return list(values)
    out: list[float] = []
    for a, b in zip(values[:-1], values[1:]):
        out.append(a)
        step = (b - a) / float(factor)
        for idx in range(1, factor):
            out.append(a + (step * idx))
    out.append(values[-1])
    return out


def test_detects_uptrend_from_rising_series() -> None:
    closes = _series(100.0, 0.45, 80)
    state = classify_market_structure(closes)
    assert state.phase == "uptrend"
    assert state.confidence > 0.5


def test_detects_liftoff_from_valley_shape() -> None:
    down = _series(100.0, -0.55, 30)
    base = [down[-1] - 0.1, down[-1] - 0.05, down[-1], down[-1] + 0.05, down[-1] + 0.1]
    up = _series(base[-1], 0.60, 45)
    closes = down + base + up
    state = classify_market_structure(closes)
    assert state.phase in {"lift_off", "uptrend"}


def test_detects_stall_or_peak_after_flat_top() -> None:
    up = _series(10.0, 0.12, 55)
    top = [up[-1] + 0.03, up[-1] + 0.01, up[-1] + 0.02, up[-1] + 0.00, up[-1] + 0.01, up[-1] - 0.01]
    closes = up + top + [top[-1], top[-1] - 0.01, top[-1] - 0.02]
    state = classify_market_structure(closes)
    assert state.phase in {"stall", "peak", "rollover"}


def test_detects_rollover_after_strong_run_and_drop() -> None:
    up = _series(1.0, 0.02, 70)
    drop = [up[-1] - 0.01, up[-1] - 0.03, up[-1] - 0.05, up[-1] - 0.08, up[-1] - 0.10, up[-1] - 0.12]
    closes = up + drop
    state = classify_market_structure(closes)
    assert state.phase in {"rollover", "downtrend", "peak"}


def test_half_mountain_geometry_tracks_valley_to_trend() -> None:
    down = _series(100.0, -0.80, 28)
    valley = [down[-1] - 0.10, down[-1] - 0.05, down[-1], down[-1] + 0.10]
    rise = _series(valley[-1], 0.90, 32)
    closes = down + valley + rise
    state = classify_market_structure(closes)
    assert state.phase in {"lift_off", "uptrend"}
    assert state.active_leg == "rise"
    assert state.bars_since_valley <= 40


def test_detects_downtrend_and_falling_leg_from_monotonic_drop() -> None:
    closes = _series(210.0, -0.75, 96)
    state = classify_market_structure(closes)
    assert state.phase == "downtrend"
    assert state.active_leg == "fall"
    assert state.confidence > 0.5


def test_detects_bottom_geometry_after_capitulation_and_base() -> None:
    drop = _series(120.0, -0.80, 46)
    base = [drop[-1] - 0.12, drop[-1] - 0.16, drop[-1] - 0.14, drop[-1] - 0.18, drop[-1] - 0.11, drop[-1] - 0.09]
    retest = [base[-1] - 0.04, base[-1] + 0.02, base[-1] - 0.01, base[-1] + 0.03, base[-1] + 0.05]
    closes = drop + base + retest
    state = classify_market_structure(closes)
    assert state.phase in {"bottom", "lift_off"}
    assert state.bars_since_valley <= 12
    assert state.level_24h <= 0.35


def test_detects_rollover_after_mountain_top_break() -> None:
    up = _series(35.0, 0.42, 74)
    top_break = [up[-1] + 0.10, up[-1] + 0.04, up[-1] - 0.01, up[-1] - 0.10, up[-1] - 0.22, up[-1] - 0.31, up[-1] - 0.37]
    closes = up + top_break
    state = classify_market_structure(closes)
    assert state.phase in {"peak", "rollover", "downtrend"}
    assert state.active_leg == "fall"
    assert state.drawdown_from_peak_bps >= 10.0


def test_interval_normalization_preserves_uptrend_structure() -> None:
    coarse = _series(100.0, 0.08, 360)
    fine = _upsample_linear(coarse, 12)

    coarse_state = classify_market_structure(
        coarse,
        bar_seconds=60,
        slope_normalize_seconds=60,
        short_window=4,
        medium_window=12,
        long_window=36,
        smooth_window=3,
        level_6h_window=72,
        level_24h_window=288,
    )
    fine_state = classify_market_structure(
        fine,
        bar_seconds=5,
        slope_normalize_seconds=60,
        short_window=48,
        medium_window=144,
        long_window=432,
        smooth_window=36,
        level_6h_window=864,
        level_24h_window=3456,
    )

    assert coarse_state.phase == "uptrend"
    assert fine_state.phase == "uptrend"
    assert abs(coarse_state.slope_short_bps - fine_state.slope_short_bps) < 2.5
    assert abs(coarse_state.slope_medium_bps - fine_state.slope_medium_bps) < 2.5
