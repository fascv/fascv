from __future__ import annotations

from typing import Any


STRATEGY_NAMES: tuple[str, ...] = (
    "staircase",
    "pullback_continuation",
    "breakout_retest",
    "continuation",
    "breakout",
    "relative_strength",
    "rebound",
)


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def macro_base_gate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if bool(row.get("macro_up_context")):
        return True
    return bool(row.get("macro_soft_support")) and (
        bool(row.get("short_horizon_scalp_ok")) or bool(row.get("staircase_trend"))
    )


def gate_penalty(row: dict[str, Any]) -> float:
    reason = str(row.get("gate_reason", "") or "").strip().lower()
    if not reason:
        return 0.0
    if reason in {"still_dumping", "macro_downtrend", "macro_down_context"}:
        return 220.0
    if reason in {"spread", "depth", "coin_peak_reentry_pending", "overextended"}:
        return 120.0
    if reason in {"structure_rollover", "structure_stall"}:
        return 90.0
    if reason in {"post_dump_recovery_pending", "no_macro_support_1h"}:
        return 60.0
    if reason in {"no_trend_setup_range", "no_trend_setup_uptrend"}:
        return 45.0
    return 30.0


def impulse_rank(row: dict[str, Any]) -> float:
    fast_score = as_float(row.get("fast_impulse_score"), 0.0)
    if fast_score > 0.0:
        return fast_score
    ret15 = as_float(row.get("ret15_bps"), 0.0)
    rel15 = as_float(row.get("rel15_bps"), 0.0)
    slope_short = as_float(row.get("structure_slope_short_bps"), 0.0)
    spread = as_float(row.get("spread_bps"), 0.0)
    return ret15 + max(0.0, rel15) + (0.6 * max(0.0, slope_short)) - (0.2 * spread)


def staircase_rank(row: dict[str, Any]) -> float:
    fast_score = as_float(row.get("fast_staircase_score"), 0.0)
    if fast_score > 0.0:
        return fast_score
    base_score = as_float(row.get("staircase_score"), 0.0)
    ret60 = as_float(row.get("ret60_bps"), 0.0)
    ret120 = as_float(row.get("ret120_bps"), 0.0)
    spread = as_float(row.get("spread_bps"), 0.0)
    return base_score + (0.6 * max(0.0, ret60)) + (0.25 * max(0.0, ret120)) - (0.25 * spread)


def drawdown_from_peak_bps(row: dict[str, Any]) -> float:
    return max(
        0.0,
        as_float(
            row.get("geo_drawdown_from_peak_bps", row.get("structure_drawdown_bps", row.get("drawdown_from_peak_bps", 0.0))),
            0.0,
        ),
    )


def has_true_staircase_structure(row: dict[str, Any]) -> bool:
    if bool(row.get("fast_staircase")) or bool(row.get("staircase_trend")):
        return True
    pullback_count = as_float(row.get("staircase_pullback_count"), 0.0)
    positive_share = as_float(row.get("staircase_positive_share"), 0.0)
    max_pullback_run = as_float(row.get("staircase_max_pullback_run_bps"), 9999.0)
    staircase_score_value = as_float(row.get("staircase_score"), 0.0)
    if pullback_count >= 2.0 and positive_share >= 0.56 and max_pullback_run <= 110.0:
        return True
    return (
        staircase_score_value >= 78.0
        and positive_share >= 0.58
        and max_pullback_run <= 95.0
    )


def looks_like_late_burst_entry(row: dict[str, Any]) -> bool:
    return (
        as_float(row.get("fast_impulse_score"), 0.0) >= 18.0
        and as_float(row.get("fast_ret_90s_bps"), 0.0) >= 14.0
        and as_float(row.get("rel60_bps"), 0.0) >= 55.0
        and as_float(row.get("staircase_pullback_count"), 0.0) < 2.0
        and as_float(row.get("staircase_positive_share"), 0.0) < 0.60
    )


def is_breakout_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 20.0:
        return False
    if bool(row.get("fast_impulse")):
        return True
    if as_float(row.get("fast_impulse_score"), 0.0) >= 12.0:
        return True
    if not bool(row.get("short_horizon_scalp_ok")):
        return False
    if as_float(row.get("pos_pct"), 100.0) > 95.0:
        return False
    return (
        as_float(row.get("ret15_bps"), 0.0) >= 18.0
        and as_float(row.get("structure_slope_short_bps"), 0.0) >= 0.8
    )


def breakout_rank(row: dict[str, Any]) -> float:
    score = impulse_rank(row)
    score += 35.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += 20.0 if bool(row.get("strong_continuation_context")) else 0.0
    score += 15.0 if bool(row.get("macro_up_context")) else 0.0
    score -= max(0.0, as_float(row.get("pos_pct"), 0.0) - 95.0) * 8.0
    score -= gate_penalty(row) * 0.50
    return score


def is_breakout_retest_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if looks_like_late_burst_entry(row):
        return False
    return (
        bool(row.get("up_structure"))
        and as_float(row.get("rel60_bps"), 0.0) >= 20.0
        and as_float(row.get("fast_impulse_score"), 0.0) >= 6.0
        and 4.0 <= drawdown_from_peak_bps(row) <= 120.0
        and as_float(row.get("pos_pct"), 100.0) <= 91.0
    )


def breakout_retest_rank(row: dict[str, Any]) -> float:
    score = impulse_rank(row) * 0.85
    score += 28.0 if bool(row.get("up_structure")) else 0.0
    score += 18.0 if bool(row.get("macro_up_context")) else 0.0
    score += 16.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += max(0.0, min(40.0, drawdown_from_peak_bps(row) - 4.0)) * 0.65
    score += max(0.0, as_float(row.get("rel60_bps"), 0.0)) * 0.22
    score += max(0.0, as_float(row.get("ret15_bps"), 0.0)) * 0.25
    score -= max(0.0, as_float(row.get("pos_pct"), 0.0) - 88.0) * 5.5
    score -= gate_penalty(row) * 0.42
    return score


def is_staircase_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if looks_like_late_burst_entry(row) and not bool(row.get("staircase_trend")):
        return False
    if bool(row.get("fast_staircase")) or bool(row.get("staircase_trend")):
        return True
    if as_float(row.get("fast_staircase_score"), 0.0) >= 10.0 and has_true_staircase_structure(row):
        return True
    if (
        has_true_staircase_structure(row)
        and bool(row.get("up_structure"))
        and as_float(row.get("pos_24h_pct"), 100.0) <= 92.0
    ):
        return True
    if (
        has_true_staircase_structure(row)
        and not bool(row.get("still_dumping"))
        and as_float(row.get("current_slope_bps_h"), 0.0) >= 1.0
        and as_float(row.get("ret60_bps"), 0.0) >= 18.0
        and as_float(row.get("pos_24h_pct"), 100.0) <= 92.0
        and (
            bool(row.get("macro_up_context"))
            or bool(row.get("up_structure"))
            or as_float(row.get("staircase_positive_share"), 0.0) >= 0.66
        )
    ):
        return True
    return (
        has_true_staircase_structure(row)
        and (bool(row.get("short_horizon_scalp_ok")) or bool(row.get("up_structure")))
        and as_float(row.get("ret60_bps"), 0.0) >= 24.0
        and as_float(row.get("current_slope_bps_h"), 0.0) >= 1.5
        and as_float(row.get("pos_24h_pct"), 100.0) <= 90.0
    )


def staircase_strategy_rank(row: dict[str, Any]) -> float:
    score = staircase_rank(row)
    score += 30.0 if bool(row.get("staircase_trend")) else 0.0
    score += 20.0 if bool(row.get("trend_ready")) else 0.0
    score += 10.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += 20.0 if bool(row.get("up_structure")) else 0.0
    score += 18.0 if has_true_staircase_structure(row) else 0.0
    score += max(0.0, as_float(row.get("staircase_positive_share"), 0.0) - 0.50) * 90.0
    score += max(0.0, as_float(row.get("current_slope_bps_h"), 0.0)) * 0.6
    score += 15.0 if bool(row.get("macro_up_context")) else 0.0
    score += 12.0 if 28.0 <= as_float(row.get("pos_pct"), 100.0) <= 84.0 else 0.0
    score -= max(0.0, as_float(row.get("staircase_max_pullback_run_bps"), 0.0) - 85.0) * 0.35
    score -= max(0.0, as_float(row.get("pos_24h_pct"), 0.0) - 90.0) * 5.0
    score -= 120.0 if looks_like_late_burst_entry(row) else 0.0
    score -= gate_penalty(row) * 0.32
    return score


def is_pullback_continuation_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")) or bool(row.get("still_dumping")):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 28.0:
        return False
    if looks_like_late_burst_entry(row):
        return False
    return (
        bool(row.get("up_structure"))
        and as_float(row.get("score_trend"), 0.0) >= 125.0
        and as_float(row.get("current_slope_bps_h"), 0.0) >= 1.2
        and as_float(row.get("ret60_bps"), 0.0) >= 14.0
        and as_float(row.get("pos_pct"), 100.0) <= 86.0
        and 6.0 <= drawdown_from_peak_bps(row) <= 110.0
    )


def pullback_continuation_rank(row: dict[str, Any]) -> float:
    score = max(0.0, as_float(row.get("score_trend"), 0.0))
    score += 42.0 if bool(row.get("strong_continuation_context")) else 0.0
    score += 30.0 if bool(row.get("up_structure")) else 0.0
    score += 24.0 if bool(row.get("macro_up_context")) else 0.0
    score += max(0.0, as_float(row.get("current_slope_bps_h"), 0.0)) * 0.45
    score += max(0.0, as_float(row.get("ret60_bps"), 0.0)) * 0.35
    score += max(0.0, min(50.0, drawdown_from_peak_bps(row) - 4.0)) * 0.55
    score += 12.0 if 35.0 <= as_float(row.get("pos_pct"), 100.0) <= 82.0 else 0.0
    score -= max(0.0, as_float(row.get("spread_bps"), 0.0) - 8.0) * 4.8
    score -= gate_penalty(row) * 0.55
    return score


def is_continuation_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")) or bool(row.get("still_dumping")):
        return False
    if not macro_base_gate(row):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 28.0:
        return False
    return (
        bool(row.get("keep_open"))
        or bool(row.get("eligible"))
        or bool(row.get("trend_ready"))
        or bool(row.get("strong_continuation_context"))
        or is_pullback_continuation_candidate(row)
        or (
            has_true_staircase_structure(row)
            and bool(row.get("up_structure"))
            and as_float(row.get("pos_pct"), 100.0) <= 88.0
        )
        or as_float(row.get("score_trend"), 0.0) >= 145.0
    )


def continuation_rank(row: dict[str, Any]) -> float:
    score = max(0.0, as_float(row.get("score_trend"), 0.0))
    score += 180.0 if bool(row.get("keep_open")) else 0.0
    score += 120.0 if bool(row.get("eligible")) else 0.0
    score += 45.0 if bool(row.get("trend_ready")) else 0.0
    score += 25.0 if bool(row.get("strong_continuation_context")) else 0.0
    score += 20.0 if bool(row.get("macro_up_context")) else 0.0
    score += 10.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += 22.0 if is_pullback_continuation_candidate(row) else 0.0
    score += 16.0 if has_true_staircase_structure(row) else 0.0
    score += max(0.0, as_float(row.get("ret15_bps"), 0.0)) * 0.8
    score += max(0.0, as_float(row.get("current_slope_bps_h"), 0.0)) * 0.4
    score += 10.0 if 35.0 <= as_float(row.get("pos_pct"), 100.0) <= 82.0 else 0.0
    score -= max(0.0, as_float(row.get("spread_bps"), 0.0) - 8.0) * 6.0
    score -= max(0.0, as_float(row.get("pos_pct"), 0.0) - 88.0) * 5.0
    score -= 35.0 if looks_like_late_burst_entry(row) else 0.0
    score -= gate_penalty(row) * 0.60
    return score


def is_relative_strength_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 18.0:
        return False
    return (
        bool(row.get("up_structure"))
        and as_float(row.get("net24_pct"), 0.0) >= 0.75
        and as_float(row.get("rel60_bps"), 0.0) >= 25.0
        and as_float(row.get("score_trend"), 0.0) >= 80.0
    )


def relative_strength_rank(row: dict[str, Any]) -> float:
    score = max(0.0, as_float(row.get("net24_pct"), 0.0)) * 42.0
    score += max(0.0, as_float(row.get("rel60_bps"), 0.0)) * 1.1
    score += max(0.0, as_float(row.get("rel15_bps"), 0.0)) * 0.75
    score += max(0.0, as_float(row.get("score_trend"), 0.0)) * 0.24
    score += 20.0 if bool(row.get("macro_up_context")) else 0.0
    score += 18.0 if bool(row.get("up_structure")) else 0.0
    score -= max(0.0, as_float(row.get("spread_bps"), 0.0) - 6.0) * 5.5
    score -= max(0.0, as_float(row.get("pos_pct"), 0.0) - 90.0) * 4.5
    score -= gate_penalty(row) * 0.35
    return score


def is_rebound_candidate(row: dict[str, Any]) -> bool:
    if bool(row.get("still_dumping")):
        return False
    if as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if bool(row.get("macro_down_context")) and not (
        bool(row.get("post_dump_recovery_ready"))
        and bool(row.get("geo_early_liftoff_trend"))
        and not bool(row.get("rebound_in_downtrend"))
    ):
        return False
    return (
        bool(row.get("bottom_candidate"))
        or bool(row.get("recent_rebound_ready"))
        or bool(row.get("post_dump_recovery_ready"))
        or bool(row.get("base_ready"))
        or (bool(row.get("fresh_bottom")) and bool(row.get("higher_low_ready")))
    )


def rebound_rank(row: dict[str, Any]) -> float:
    score = max(0.0, as_float(row.get("score_bottom"), 0.0))
    score += 120.0 if bool(row.get("bottom_candidate")) else 0.0
    score += 80.0 if bool(row.get("recent_rebound_ready")) else 0.0
    score += 70.0 if bool(row.get("post_dump_recovery_ready")) else 0.0
    score += 40.0 if bool(row.get("base_ready")) else 0.0
    score += 20.0 if bool(row.get("higher_low_ready")) else 0.0
    score += 40.0 if bool(row.get("fresh_bottom")) else 0.0
    score += 50.0 if bool(row.get("geo_early_liftoff_trend")) else 0.0
    score += 20.0 if bool(row.get("macro_up_context")) else 0.0
    score -= 60.0 if bool(row.get("macro_down_context")) else 0.0
    score -= 35.0 if bool(row.get("rebound_in_downtrend")) else 0.0
    score += max(0.0, 60.0 - as_float(row.get("pos_pct"), 60.0)) * 1.5
    score -= max(0.0, as_float(row.get("spread_bps"), 0.0) - 8.0) * 4.0
    score -= gate_penalty(row) * 0.40
    return score


def annotate_row_with_strategy_views(row: dict[str, Any]) -> dict[str, Any]:
    strategy_scores: dict[str, float] = {}
    if is_staircase_candidate(row):
        strategy_scores["staircase"] = round(staircase_strategy_rank(row), 6)
    if is_pullback_continuation_candidate(row):
        strategy_scores["pullback_continuation"] = round(pullback_continuation_rank(row), 6)
    if is_breakout_retest_candidate(row):
        strategy_scores["breakout_retest"] = round(breakout_retest_rank(row), 6)
    if is_continuation_candidate(row):
        strategy_scores["continuation"] = round(continuation_rank(row), 6)
    if is_breakout_candidate(row):
        strategy_scores["breakout"] = round(breakout_rank(row), 6)
    if is_relative_strength_candidate(row):
        strategy_scores["relative_strength"] = round(relative_strength_rank(row), 6)
    if is_rebound_candidate(row):
        strategy_scores["rebound"] = round(rebound_rank(row), 6)

    ordered = sorted(strategy_scores.items(), key=lambda item: item[1], reverse=True)
    row["strategy_scores"] = strategy_scores
    row["strategy_tags"] = [name for name, _ in ordered]
    row["strategy_primary"] = ordered[0][0] if ordered else ""
    row["strategy_primary_score"] = round(ordered[0][1], 6) if ordered else 0.0
    meta_score = ordered[0][1] if ordered else 0.0
    if len(ordered) > 1:
        meta_score += 0.18 * sum(score for _, score in ordered[1:])
    row["strategy_meta_score"] = round(meta_score, 6)
    return row


def annotate_rows_with_strategy_views(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {name: [] for name in STRATEGY_NAMES}
    for row in rows:
        annotate_row_with_strategy_views(row)
    for strategy in STRATEGY_NAMES:
        ranked_rows = [row for row in rows if strategy_score(row, strategy) > 0.0]
        ranked_rows.sort(
            key=lambda row: (
                strategy_score(row, strategy),
                candidate_meta_score(row),
                as_float(row.get("score"), 0.0),
            ),
            reverse=True,
        )
        rankings[strategy] = ranked_rows
    return rankings


def strategy_score(row: dict[str, Any], strategy: str) -> float:
    raw = row.get("strategy_scores")
    if isinstance(raw, dict):
        try:
            return float(raw.get(strategy, 0.0) or 0.0)
        except Exception:
            return 0.0
    return 0.0


def candidate_meta_score(row: dict[str, Any]) -> float:
    try:
        return float(row.get("strategy_meta_score") or 0.0)
    except Exception:
        return 0.0


def infer_trade_strategy_scores(features: dict[str, Any]) -> dict[str, float]:
    ret = as_float(features.get("return_bps"), 0.0)
    trend = as_float(features.get("trend_return_bps"), 0.0)
    volume_z = as_float(features.get("volume_z"), 0.0)
    range_pos = as_float(features.get("context_range_pos"), 0.5)
    range_pos_pct = range_pos * 100.0 if range_pos <= 1.0 else range_pos
    rebound = as_float(features.get("context_rebound_bps"), 0.0)
    drawdown = abs(min(0.0, as_float(features.get("context_drawdown_bps"), 0.0)))
    spread = as_float(features.get("spread_bps"), 0.0)
    slope_short = as_float(features.get("structure_slope_short_bps"), 0.0)
    slope_medium = as_float(features.get("structure_slope_medium_bps"), 0.0)
    extension = as_float(features.get("structure_extension_bps"), 0.0)
    up_structure = as_float(features.get("up_structure"), 0.0) >= 0.5
    down_structure = as_float(features.get("down_structure"), 0.0) >= 0.5
    phase_bottom = as_float(features.get("phase_bottom"), 0.0) >= 0.5
    phase_range = as_float(features.get("phase_range"), 0.0) >= 0.5
    phase_peak = as_float(features.get("phase_peak"), 0.0) >= 0.5
    phase_rollover = as_float(features.get("phase_rollover"), 0.0) >= 0.5
    phase_downtrend = as_float(features.get("phase_downtrend"), 0.0) >= 0.5
    phase_lift_off = as_float(features.get("phase_lift_off"), 0.0) >= 0.5

    breakout = (
        max(0.0, ret) * 2.6
        + max(0.0, trend) * 1.1
        + max(0.0, slope_short) * 8.0
        + max(0.0, volume_z) * 6.0
        + (32.0 if phase_lift_off else 0.0)
        + (16.0 if up_structure else 0.0)
        + (10.0 if phase_range and range_pos_pct < 86.0 else 0.0)
        - max(0.0, spread - 8.0) * 3.5
        - max(0.0, range_pos_pct - 92.0) * 4.5
        - max(0.0, extension - 70.0) * 1.3
        - (18.0 if phase_peak or phase_rollover or phase_downtrend else 0.0)
    )
    staircase = (
        max(0.0, trend) * 1.55
        + max(0.0, slope_medium) * 20.0
        + max(0.0, slope_short) * 7.0
        + max(0.0, volume_z) * 3.5
        + (22.0 if up_structure else 0.0)
        + (15.0 if 12.0 <= range_pos_pct <= 84.0 else 0.0)
        + (10.0 if -12.0 <= ret <= 45.0 else 0.0)
        - max(0.0, spread - 8.0) * 2.4
        - max(0.0, extension - 55.0) * 1.2
        - (18.0 if phase_downtrend else 0.0)
    )
    pullback_continuation = (
        max(0.0, trend) * 1.65
        + max(0.0, slope_medium) * 18.0
        + max(0.0, slope_short) * 4.0
        + max(0.0, ret) * 0.45
        + (28.0 if up_structure else 0.0)
        + (18.0 if 22.0 <= range_pos_pct <= 82.0 else 0.0)
        + (12.0 if 8.0 <= drawdown <= 90.0 else 0.0)
        - max(0.0, spread - 8.0) * 3.2
        - max(0.0, extension - 65.0) * 1.2
        - (18.0 if phase_peak or phase_rollover or phase_downtrend else 0.0)
    )
    breakout_retest = (
        max(0.0, ret) * 1.9
        + max(0.0, trend) * 0.95
        + max(0.0, slope_short) * 7.5
        + max(0.0, volume_z) * 5.0
        + (22.0 if up_structure else 0.0)
        + (18.0 if 6.0 <= drawdown <= 90.0 else 0.0)
        + (12.0 if phase_lift_off or phase_range else 0.0)
        - max(0.0, spread - 8.0) * 3.2
        - max(0.0, range_pos_pct - 90.0) * 4.0
        - max(0.0, extension - 75.0) * 1.2
        - (16.0 if phase_peak or phase_rollover or phase_downtrend else 0.0)
    )
    continuation = (
        max(0.0, trend) * 1.9
        + max(0.0, slope_medium) * 22.0
        + max(0.0, slope_short) * 5.0
        + max(0.0, ret) * 0.8
        + (24.0 if up_structure else 0.0)
        + (14.0 if phase_range or phase_lift_off else 0.0)
        + (10.0 if range_pos_pct < 88.0 else 0.0)
        - max(0.0, spread - 8.0) * 3.0
        - max(0.0, range_pos_pct - 88.0) * 5.0
        - max(0.0, extension - 65.0) * 1.5
        - (20.0 if phase_peak or phase_rollover or phase_downtrend else 0.0)
    )
    relative_strength = (
        max(0.0, trend) * 1.25
        + max(0.0, ret) * 0.9
        + max(0.0, slope_medium) * 16.0
        + max(0.0, volume_z) * 2.5
        + (18.0 if up_structure else 0.0)
        + (12.0 if range_pos_pct < 90.0 else 0.0)
        - max(0.0, spread - 8.0) * 4.2
        - max(0.0, extension - 80.0) * 1.0
        - (15.0 if phase_downtrend or phase_rollover else 0.0)
    )
    rebound_score = (
        max(0.0, rebound) * 1.15
        + drawdown * 0.12
        + max(0.0, ret) * 0.7
        + (28.0 if phase_bottom else 0.0)
        + (12.0 if range_pos_pct <= 55.0 else 0.0)
        + (10.0 if not down_structure else 0.0)
        + (8.0 if slope_short >= -4.0 else 0.0)
        - max(0.0, spread - 8.0) * 2.5
        - (22.0 if down_structure and trend < 0.0 else 0.0)
        - (15.0 if phase_peak or phase_rollover else 0.0)
    )
    return {
        "staircase": round(max(0.0, staircase), 6),
        "pullback_continuation": round(max(0.0, pullback_continuation), 6),
        "breakout_retest": round(max(0.0, breakout_retest), 6),
        "continuation": round(max(0.0, continuation), 6),
        "breakout": round(max(0.0, breakout), 6),
        "relative_strength": round(max(0.0, relative_strength), 6),
        "rebound": round(max(0.0, rebound_score), 6),
    }


def infer_trade_strategy(features: dict[str, Any]) -> tuple[str, dict[str, float]]:
    scores = infer_trade_strategy_scores(features)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ordered or ordered[0][1] <= 0.0:
        return "", scores
    return ordered[0][0], scores


def serialize_strategy_rankings(
    rankings: dict[str, list[dict[str, Any]]],
    *,
    limit: int = 12,
) -> dict[str, list[dict[str, Any]]]:
    payload: dict[str, list[dict[str, Any]]] = {}
    for strategy in STRATEGY_NAMES:
        entries: list[dict[str, Any]] = []
        for row in rankings.get(strategy, [])[: max(1, int(limit))]:
            entries.append(
                {
                    "symbol": str(row.get("symbol", "")).upper(),
                    "score": round(strategy_score(row, strategy), 6),
                    "primary": str(row.get("strategy_primary", "") or ""),
                    "meta_score": round(candidate_meta_score(row), 6),
                    "gate_reason": str(row.get("gate_reason", "") or ""),
                    "eligible": bool(row.get("eligible")),
                    "keep_open": bool(row.get("keep_open")),
                    "setup_type": str(row.get("setup_type", "") or ""),
                    "spread_bps": round(as_float(row.get("spread_bps"), 0.0), 6),
                    "market": str(row.get("market", "") or ""),
                }
            )
        payload[strategy] = entries
    return payload
