from __future__ import annotations

from collections import Counter
from typing import Any

from trading.meta.rotation_shadow import ShadowCandidate, TradeSummary


SUPPORTED_AUTOTUNE_PROFILES: tuple[str, ...] = (
    "scalp_breakout",
    "scalp_uptrend",
    "scalp_guarded",
    "scalp_guarded_open",
    "scalp_lockdown",
)
MIN_STRATEGY_EVIDENCE_TRADES = 4
MIN_SYMBOL_EVIDENCE_TRADES = 4
MIN_PROFILE_SWITCH_TRADES = 6
MIN_PROFILE_SWITCH_MARGIN = 12.0


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _market_snapshot(payload: dict[str, Any]) -> dict[str, float]:
    rows = payload.get("all_rows") or payload.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    row_count = 0
    eligible_count = 0
    breakout_count = 0
    macro_up_count = 0
    tight_spread_count = 0
    top_opportunity_count = 0
    spreads: list[float] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row_count += 1
        spread_bps = _safe_float(raw_row.get("fast_spread_bps") or raw_row.get("spread_bps"))
        if spread_bps > 0.0:
            spreads.append(spread_bps)
        if 0.0 < spread_bps <= 12.0:
            tight_spread_count += 1
        eligible = bool(raw_row.get("eligible")) or bool(raw_row.get("keep_open"))
        breakout = (
            bool(raw_row.get("fast_impulse"))
            or bool(raw_row.get("fast_staircase"))
            or bool(raw_row.get("strong_continuation_context"))
            or bool(raw_row.get("staircase_trend"))
        )
        macro_up = (
            bool(raw_row.get("macro_up_context"))
            or bool(raw_row.get("mid_trend_up_1h"))
            or bool(raw_row.get("mid_trend_up_6h_balanced"))
            or bool(raw_row.get("broad_uptrend_context"))
        )
        if eligible:
            eligible_count += 1
        if breakout:
            breakout_count += 1
        if macro_up:
            macro_up_count += 1
        if eligible or breakout:
            top_opportunity_count += 1
    avg_spread_bps = sum(spreads) / float(len(spreads)) if spreads else 0.0
    breakout_market = (
        top_opportunity_count >= 3
        and breakout_count >= 2
        and macro_up_count >= 2
        and tight_spread_count >= 3
        and (avg_spread_bps <= 12.5 or avg_spread_bps == 0.0)
    )
    guarded_market = top_opportunity_count >= 1 and (avg_spread_bps <= 14.5 or avg_spread_bps == 0.0)
    thin_market = top_opportunity_count == 0 or (tight_spread_count <= 1 and avg_spread_bps >= 14.0)
    return {
        "row_count": float(row_count),
        "eligible_count": float(eligible_count),
        "breakout_count": float(breakout_count),
        "macro_up_count": float(macro_up_count),
        "tight_spread_count": float(tight_spread_count),
        "top_opportunity_count": float(top_opportunity_count),
        "avg_spread_bps": avg_spread_bps,
        "breakout_market": 1.0 if breakout_market else 0.0,
        "guarded_market": 1.0 if guarded_market else 0.0,
        "thin_market": 1.0 if thin_market else 0.0,
    }


def _failed_start_rate(exit_reasons: dict[str, Any]) -> float:
    total = 0.0
    for value in exit_reasons.values():
        total += _safe_float(value)
    if total <= 0.0:
        return 0.0
    return _safe_float(exit_reasons.get("failed_start_exit")) / total


def _exit_path_stats(payload: object) -> dict[str, float]:
    if not isinstance(payload, dict):
        payload = {}
    early_exit_rate = _safe_float(payload.get("early_exit_rate"))
    protective_exit_rate = _safe_float(payload.get("protective_exit_rate"))
    failed_start_recovery_rate = _safe_float(payload.get("failed_start_recovery_rate"))
    trailing_missed_run_rate = _safe_float(payload.get("trailing_missed_run_rate"))
    shakeout_then_run_rate = _safe_float(payload.get("shakeout_then_run_rate"))
    micro_pop_loss_run_rate = _safe_float(payload.get("micro_pop_loss_run_rate"))
    hold_opportunity_rate = _safe_float(payload.get("hold_opportunity_rate"))
    avg_post_exit_close_15m_bps = _safe_float(payload.get("avg_post_exit_close_15m_bps"))
    avg_post_exit_close_30m_bps = _safe_float(payload.get("avg_post_exit_close_30m_bps"))
    avg_post_exit_close_60m_bps = _safe_float(payload.get("avg_post_exit_close_60m_bps"))
    avg_post_exit_close_120m_bps = _safe_float(payload.get("avg_post_exit_close_120m_bps"))
    early_exit_bias = max(
        0.0,
        early_exit_rate
        + (0.80 * failed_start_recovery_rate)
        + (0.55 * trailing_missed_run_rate)
        + (0.70 * shakeout_then_run_rate)
        + (0.95 * micro_pop_loss_run_rate)
        + (0.35 * max(0.0, avg_post_exit_close_15m_bps) / 18.0)
        + (0.40 * max(0.0, avg_post_exit_close_60m_bps) / 24.0)
        + (0.25 * max(0.0, avg_post_exit_close_120m_bps) / 32.0)
        - (0.70 * protective_exit_rate),
    )
    protective_exit_bias = max(
        0.0,
        protective_exit_rate
        + (0.35 * max(0.0, -avg_post_exit_close_15m_bps) / 18.0)
        + (0.25 * max(0.0, -avg_post_exit_close_30m_bps) / 24.0)
        + (0.20 * max(0.0, -avg_post_exit_close_60m_bps) / 28.0)
        - (0.25 * shakeout_then_run_rate)
        - (0.35 * micro_pop_loss_run_rate)
        - (0.45 * early_exit_rate),
    )
    if hold_opportunity_rate <= 0.0:
        hold_opportunity_rate = max(
            early_exit_rate,
            failed_start_recovery_rate,
            trailing_missed_run_rate,
            shakeout_then_run_rate,
            micro_pop_loss_run_rate,
        )
    return {
        "exit_path_sample_count": _safe_float(payload.get("sample_count")),
        "early_exit_rate": early_exit_rate,
        "protective_exit_rate": protective_exit_rate,
        "failed_start_recovery_rate": failed_start_recovery_rate,
        "trailing_missed_run_rate": trailing_missed_run_rate,
        "shakeout_then_run_rate": shakeout_then_run_rate,
        "micro_pop_loss_run_rate": micro_pop_loss_run_rate,
        "hold_opportunity_rate": hold_opportunity_rate,
        "avg_post_exit_close_15m_bps": avg_post_exit_close_15m_bps,
        "avg_post_exit_close_30m_bps": avg_post_exit_close_30m_bps,
        "avg_post_exit_close_60m_bps": avg_post_exit_close_60m_bps,
        "avg_post_exit_close_120m_bps": avg_post_exit_close_120m_bps,
        "early_exit_bias": early_exit_bias,
        "protective_exit_bias": protective_exit_bias,
    }


def _no_trade_stats(payload: object) -> dict[str, float]:
    if not isinstance(payload, dict):
        payload = {}
    sample_count = _safe_float(payload.get("sample_count"))
    tunable_sample_count = _safe_float(payload.get("tunable_sample_count"))
    missed_entry_rate = _safe_float(payload.get("tunable_missed_entry_rate"), _safe_float(payload.get("missed_entry_rate")))
    correct_block_rate = _safe_float(
        payload.get("tunable_correct_block_rate"),
        _safe_float(payload.get("correct_block_rate")),
    )
    avg_post_decision_close_30m_bps = _safe_float(payload.get("avg_post_decision_close_30m_bps"))
    avg_post_decision_close_60m_bps = _safe_float(payload.get("avg_post_decision_close_60m_bps"))
    missed_entry_bias = max(
        0.0,
        missed_entry_rate
        + (0.30 * max(0.0, avg_post_decision_close_30m_bps) / 18.0)
        + (0.45 * max(0.0, avg_post_decision_close_60m_bps) / 24.0)
        - (0.70 * correct_block_rate),
    )
    correct_block_bias = max(
        0.0,
        correct_block_rate
        + (0.30 * max(0.0, -avg_post_decision_close_30m_bps) / 18.0)
        + (0.35 * max(0.0, -avg_post_decision_close_60m_bps) / 24.0)
        - (0.60 * missed_entry_rate),
    )
    return {
        "no_trade_sample_count": sample_count,
        "tunable_no_trade_sample_count": tunable_sample_count,
        "missed_entry_rate": missed_entry_rate,
        "correct_block_rate": correct_block_rate,
        "avg_post_decision_close_30m_bps": avg_post_decision_close_30m_bps,
        "avg_post_decision_close_60m_bps": avg_post_decision_close_60m_bps,
        "missed_entry_bias": missed_entry_bias,
        "correct_block_bias": correct_block_bias,
    }


def _entry_path_stats(payload: object) -> dict[str, float]:
    if not isinstance(payload, dict):
        payload = {}
    poor_entry_rate = _safe_float(payload.get("poor_entry_rate"))
    top_zone_entry_rate = _safe_float(payload.get("top_zone_entry_rate"))
    late_entry_rate = _safe_float(payload.get("late_entry_rate"))
    avg_entry_quality_score_bps = _safe_float(payload.get("avg_entry_quality_score_bps"))
    late_entry_bias = max(
        0.0,
        late_entry_rate
        + (0.70 * top_zone_entry_rate)
        + (0.45 * poor_entry_rate)
        + (0.35 * max(0.0, -avg_entry_quality_score_bps) / 20.0),
    )
    poor_entry_bias = max(
        0.0,
        poor_entry_rate + (0.35 * max(0.0, -avg_entry_quality_score_bps) / 24.0),
    )
    return {
        "entry_path_sample_count": _safe_float(payload.get("sample_count")),
        "poor_entry_rate": poor_entry_rate,
        "top_zone_entry_rate": top_zone_entry_rate,
        "late_entry_rate": late_entry_rate,
        "avg_entry_quality_score_bps": avg_entry_quality_score_bps,
        "late_entry_bias": late_entry_bias,
        "poor_entry_bias": poor_entry_bias,
    }


def _trade_window_stats(trade_summary: TradeSummary) -> dict[str, float]:
    trade_count = max(0, int(getattr(trade_summary, "trade_count", 0) or 0))
    net_pnl = _safe_float(getattr(trade_summary, "net_pnl", 0.0))
    win_rate = _safe_float(getattr(trade_summary, "win_rate", 0.0))
    loss_rate = 1.0 - win_rate if trade_count > 0 else 0.0
    failed_start_rate = _failed_start_rate(getattr(trade_summary, "exit_reasons", {}) or {})
    stats = {
        "trade_count": float(trade_count),
        "net_pnl": net_pnl,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "failed_start_rate": failed_start_rate,
        "very_bad_cycle": 1.0
        if trade_count >= 6 and (net_pnl <= -0.25 or loss_rate >= 0.70 or failed_start_rate >= 0.60)
        else 0.0,
        "weak_cycle": 1.0
        if trade_count >= 4 and (net_pnl < -0.05 or loss_rate > 0.55 or failed_start_rate >= 0.45)
        else 0.0,
        "good_cycle": 1.0
        if trade_count >= 4 and net_pnl >= 0.10 and loss_rate <= 0.45 and failed_start_rate <= 0.35
        else 0.0,
    }
    stats.update(_exit_path_stats(getattr(trade_summary, "exit_path_summary", {}) or {}))
    stats.update(_entry_path_stats(getattr(trade_summary, "entry_path_summary", {}) or {}))
    stats.update(_no_trade_stats(getattr(trade_summary, "no_trade_summary", {}) or {}))
    return stats


def _selected_row_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("all_rows") or payload.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    row_map = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        if symbol:
            row_map[symbol] = row
    return row_map


def _profile_prior(profile: str, market: dict[str, float], trade_stats: dict[str, float]) -> float:
    breakout_market = bool(market.get("breakout_market"))
    guarded_market = bool(market.get("guarded_market"))
    thin_market = bool(market.get("thin_market"))
    very_bad_cycle = bool(trade_stats.get("very_bad_cycle"))
    weak_cycle = bool(trade_stats.get("weak_cycle"))
    good_cycle = bool(trade_stats.get("good_cycle"))
    early_exit_bias = _safe_float(trade_stats.get("early_exit_bias"))
    protective_exit_bias = _safe_float(trade_stats.get("protective_exit_bias"))
    late_entry_bias = _safe_float(trade_stats.get("late_entry_bias"))
    missed_entry_bias = _safe_float(trade_stats.get("missed_entry_bias"))
    correct_block_bias = _safe_float(trade_stats.get("correct_block_bias"))

    score = 0.0
    if profile == "scalp_breakout":
        if breakout_market:
            score += 34.0
        if thin_market:
            score -= 26.0
        if weak_cycle:
            score -= 14.0
        if good_cycle and breakout_market:
            score += 10.0
    elif profile == "scalp_uptrend":
        if breakout_market:
            score += 28.0
        elif guarded_market:
            score += 14.0
        if thin_market:
            score -= 10.0
        if very_bad_cycle and breakout_market:
            score += 24.0
        elif weak_cycle:
            score += 6.0
    elif profile in {"scalp_guarded", "scalp_guarded_open"}:
        if guarded_market:
            score += 18.0
        if breakout_market:
            score -= 8.0
        if weak_cycle:
            score += 10.0
        if very_bad_cycle and not breakout_market:
            score += 12.0
    elif profile == "scalp_lockdown":
        if very_bad_cycle and not breakout_market:
            score += 32.0
        if breakout_market:
            score -= 38.0
        if thin_market:
            score += 18.0
        if good_cycle:
            score -= 12.0
    if early_exit_bias >= 0.20:
        if profile in {"scalp_breakout", "scalp_uptrend"}:
            score += (20.0 if breakout_market else 10.0) * min(1.25, early_exit_bias)
        elif profile in {"scalp_guarded", "scalp_guarded_open"} and not thin_market:
            score += 5.0 * min(1.0, early_exit_bias)
        elif profile == "scalp_lockdown":
            score -= 24.0 * min(1.25, early_exit_bias)
    if protective_exit_bias >= 0.20:
        if profile == "scalp_lockdown":
            score += 16.0 * min(1.2, protective_exit_bias)
        elif profile in {"scalp_breakout", "scalp_uptrend"}:
            score -= 8.0 * min(1.0, protective_exit_bias)
    if late_entry_bias >= 0.18:
        if profile == "scalp_breakout":
            score -= (24.0 if breakout_market else 18.0) * min(1.3, late_entry_bias)
        elif profile == "scalp_uptrend":
            score += (12.0 if breakout_market else 8.0) * min(1.1, late_entry_bias)
        elif profile in {"scalp_guarded", "scalp_guarded_open"}:
            score += 14.0 * min(1.15, late_entry_bias)
        elif profile == "scalp_lockdown" and (weak_cycle or thin_market):
            score += 8.0 * min(1.0, late_entry_bias)
    if missed_entry_bias >= 0.18 and not thin_market:
        if profile == "scalp_breakout":
            score += 24.0 * min(1.2, missed_entry_bias)
        elif profile == "scalp_uptrend":
            score += 18.0 * min(1.1, missed_entry_bias)
        elif profile in {"scalp_guarded", "scalp_guarded_open"}:
            score -= 8.0 * min(1.0, missed_entry_bias)
        elif profile == "scalp_lockdown":
            score -= 22.0 * min(1.25, missed_entry_bias)
    if correct_block_bias >= 0.18:
        if profile == "scalp_lockdown":
            score += 16.0 * min(1.1, correct_block_bias)
        elif profile in {"scalp_guarded", "scalp_guarded_open"}:
            score += 8.0 * min(1.0, correct_block_bias)
        elif profile in {"scalp_breakout", "scalp_uptrend"}:
            score -= 7.0 * min(1.0, correct_block_bias)
    return score


def _strategy_score(
    strategy: str,
    trade_summary: TradeSummary,
) -> tuple[float, list[str]]:
    breakdown = getattr(trade_summary, "strategy_breakdown", {}) or {}
    stats = breakdown.get(strategy) if isinstance(breakdown, dict) else {}
    if not isinstance(stats, dict):
        return 0.0, []
    trade_count = int(stats.get("trade_count") or 0)
    if trade_count <= 0:
        return 0.0, []
    net_pnl = _safe_float(stats.get("net_pnl"))
    win_rate = _safe_float(stats.get("win_rate"))
    failed_start_rate = _failed_start_rate(stats.get("exit_reasons") or {})
    exit_path_stats = _exit_path_stats(stats.get("exit_path_summary") or {})
    entry_path_stats = _entry_path_stats(stats.get("entry_path_summary") or {})
    early_exit_bias = _safe_float(exit_path_stats.get("early_exit_bias"))
    late_entry_bias = _safe_float(entry_path_stats.get("late_entry_bias"))
    scale = min(1.0, trade_count / 6.0)
    score = 0.0
    score += scale * max(-26.0, min(26.0, net_pnl * 220.0))
    score += scale * max(-10.0, min(10.0, (win_rate - 0.5) * 50.0))
    score -= scale * max(0.0, min(14.0, failed_start_rate * 24.0))
    score += scale * max(0.0, min(12.0, early_exit_bias * 10.0))
    score -= scale * max(0.0, min(14.0, late_entry_bias * 10.0))
    reasons: list[str] = []
    if trade_count >= MIN_STRATEGY_EVIDENCE_TRADES and net_pnl > 0.03 and win_rate >= 0.5:
        reasons.append(f"strategy:{strategy}:positive")
    if trade_count >= MIN_STRATEGY_EVIDENCE_TRADES and (
        net_pnl < -0.05 or failed_start_rate >= 0.5 or late_entry_bias >= 0.45
    ):
        if late_entry_bias >= max(0.45, early_exit_bias + 0.10):
            reasons.append(f"strategy:{strategy}:late_entry_top_chasing")
        elif early_exit_bias >= 0.30:
            reasons.append(f"strategy:{strategy}:exit_too_early")
        else:
            reasons.append(f"strategy:{strategy}:weak")
    return score, reasons


def _symbol_score(
    symbol: str,
    trade_summary: TradeSummary,
) -> tuple[float, list[str]]:
    breakdown = getattr(trade_summary, "symbol_breakdown", {}) or {}
    stats = breakdown.get(symbol) if isinstance(breakdown, dict) else {}
    if not isinstance(stats, dict):
        return 0.0, []
    trade_count = int(stats.get("trade_count") or 0)
    if trade_count <= 0:
        return 0.0, []
    net_pnl = _safe_float(stats.get("net_pnl"))
    win_rate = _safe_float(stats.get("win_rate"))
    failed_start_rate = _failed_start_rate(stats.get("exit_reasons") or {})
    exit_path_stats = _exit_path_stats(stats.get("exit_path_summary") or {})
    entry_path_stats = _entry_path_stats(stats.get("entry_path_summary") or {})
    early_exit_bias = _safe_float(exit_path_stats.get("early_exit_bias"))
    late_entry_bias = _safe_float(entry_path_stats.get("late_entry_bias"))
    scale = min(1.0, trade_count / 5.0)
    score = 0.0
    score += scale * max(-22.0, min(12.0, net_pnl * 240.0))
    score += scale * max(-8.0, min(8.0, (win_rate - 0.5) * 40.0))
    score -= scale * max(0.0, min(12.0, failed_start_rate * 22.0))
    score += scale * max(0.0, min(10.0, early_exit_bias * 9.0))
    score -= scale * max(0.0, min(12.0, late_entry_bias * 9.0))
    reasons: list[str] = []
    if trade_count >= MIN_SYMBOL_EVIDENCE_TRADES and (net_pnl <= -0.08 or failed_start_rate >= 0.5):
        if late_entry_bias >= max(0.40, early_exit_bias + 0.08):
            reasons.append(f"symbol:{symbol}:late_entry_top_chasing")
        elif early_exit_bias >= 0.35:
            reasons.append(f"symbol:{symbol}:recoverable")
        else:
            reasons.append(f"symbol:{symbol}:avoid")
    if trade_count >= MIN_SYMBOL_EVIDENCE_TRADES and net_pnl > 0.03 and win_rate >= 0.5:
        reasons.append(f"symbol:{symbol}:keep")
    return score, reasons


def evaluate_profile_payload(
    *,
    profile: str,
    payload: dict[str, Any],
    trade_summary: TradeSummary,
    candidates: list[ShadowCandidate],
) -> dict[str, Any]:
    candidate_map = {item.symbol.upper(): item for item in candidates}
    selected = [str(item).upper() for item in payload.get("selected") or [] if str(item).strip()]
    selected_strategy_map = payload.get("selected_strategy_map") if isinstance(payload.get("selected_strategy_map"), dict) else {}
    row_map = _selected_row_map(payload)
    trade_stats = _trade_window_stats(trade_summary)
    market = _market_snapshot(payload)

    selected_candidates = [candidate_map[symbol] for symbol in selected if symbol in candidate_map]
    avg_selected_p = (
        sum(item.p_profit for item in selected_candidates) / float(len(selected_candidates))
        if selected_candidates
        else 0.0
    )
    max_selected_p = max((item.p_profit for item in selected_candidates), default=0.0)
    positive_selected = sum(1 for item in selected_candidates if item.p_profit >= 0.56)
    avg_selected_spread = (
        sum(_safe_float(row_map.get(symbol, {}).get("spread_bps")) for symbol in selected) / float(len(selected))
        if selected
        else 0.0
    )
    eligible_selected = sum(
        1
        for symbol in selected
        if bool(row_map.get(symbol, {}).get("eligible")) or bool(row_map.get(symbol, {}).get("keep_open"))
    )

    score = 0.0
    reasons: list[str] = []
    score += _profile_prior(profile, market, trade_stats)
    score += 180.0 * (avg_selected_p - 0.5)
    score += 75.0 * max(0.0, max_selected_p - 0.5)
    score += 12.0 * float(positive_selected)
    score += 4.0 * float(eligible_selected)
    score -= 1.35 * avg_selected_spread
    if bool(payload.get("selection_relaxed")):
        score -= 18.0
        reasons.append("selection_relaxed")
    active_candidate_count = int(payload.get("active_candidate_count") or 0)
    if active_candidate_count < len(selected):
        score -= 6.0 * float(len(selected) - active_candidate_count)
    if selected_candidates:
        reasons.append(f"selected_avg_p={avg_selected_p:.3f}")
    if market.get("breakout_market"):
        reasons.append("breakout_market")
    if trade_stats.get("very_bad_cycle"):
        reasons.append("very_bad_cycle")

    selected_strategy_counts = Counter(
        str(selected_strategy_map.get(symbol) or "").strip().lower() or "unknown"
        for symbol in selected
    )
    for strategy, count in selected_strategy_counts.items():
        strategy_value, strategy_reasons = _strategy_score(strategy, trade_summary)
        score += strategy_value * min(1.0, count / max(1.0, len(selected)))
        reasons.extend(strategy_reasons)
    for symbol in selected:
        symbol_value, symbol_reasons = _symbol_score(symbol, trade_summary)
        score += symbol_value
        reasons.extend(symbol_reasons)

    return {
        "profile": profile,
        "score": round(score, 6),
        "selected": selected,
        "selected_count": len(selected),
        "selected_strategy_map": {
            str(symbol).upper(): str(strategy).strip().lower()
            for symbol, strategy in selected_strategy_map.items()
            if str(symbol).strip()
        },
        "avg_selected_p_profit": round(avg_selected_p, 6),
        "max_selected_p_profit": round(max_selected_p, 6),
        "positive_selected_count": positive_selected,
        "eligible_selected_count": eligible_selected,
        "avg_selected_spread_bps": round(avg_selected_spread, 6),
        "market_snapshot": {
            key: (round(value, 6) if isinstance(value, float) else value)
            for key, value in market.items()
        },
        "reasons": sorted(set(reasons)),
        "profile_values": dict(payload.get("profile_values") or {}),
    }


def _build_parameter_overrides(
    *,
    profile_values: dict[str, Any],
    trade_summary: TradeSummary,
    evaluation: dict[str, Any],
) -> dict[str, float | int]:
    if not profile_values:
        return {}
    trade_stats = _trade_window_stats(trade_summary)
    trade_count = int(trade_stats["trade_count"])
    if trade_count <= 0:
        return {}

    net_pnl = float(trade_stats["net_pnl"])
    loss_rate = float(trade_stats["loss_rate"])
    failed_start_rate = float(trade_stats["failed_start_rate"])
    early_exit_bias = float(trade_stats.get("early_exit_bias", 0.0))
    protective_exit_bias = float(trade_stats.get("protective_exit_bias", 0.0))
    failed_start_recovery_rate = float(trade_stats.get("failed_start_recovery_rate", 0.0))
    trailing_missed_run_rate = float(trade_stats.get("trailing_missed_run_rate", 0.0))
    shakeout_then_run_rate = float(trade_stats.get("shakeout_then_run_rate", 0.0))
    micro_pop_loss_run_rate = float(trade_stats.get("micro_pop_loss_run_rate", 0.0))
    hold_opportunity_rate = float(trade_stats.get("hold_opportunity_rate", 0.0))
    late_entry_rate = float(trade_stats.get("late_entry_rate", 0.0))
    top_zone_entry_rate = float(trade_stats.get("top_zone_entry_rate", 0.0))
    poor_entry_rate = float(trade_stats.get("poor_entry_rate", 0.0))
    late_entry_bias = float(trade_stats.get("late_entry_bias", 0.0))
    missed_entry_rate = float(trade_stats.get("missed_entry_rate", 0.0))
    correct_block_rate = float(trade_stats.get("correct_block_rate", 0.0))
    missed_entry_bias = float(trade_stats.get("missed_entry_bias", 0.0))
    correct_block_bias = float(trade_stats.get("correct_block_bias", 0.0))
    avg_post_exit_close_60m_bps = float(trade_stats.get("avg_post_exit_close_60m_bps", 0.0))
    avg_post_exit_close_120m_bps = float(trade_stats.get("avg_post_exit_close_120m_bps", 0.0))
    avg_post_decision_close_60m_bps = float(trade_stats.get("avg_post_decision_close_60m_bps", 0.0))
    avg_selected_p = _safe_float(evaluation.get("avg_selected_p_profit"))

    loss_pressure = max(0.0, min(1.5, (-net_pnl) / 0.25))
    loss_pressure += max(0.0, loss_rate - 0.5) * 0.8
    positive_pressure = max(0.0, min(1.0, net_pnl / 0.25))
    positive_pressure += max(0.0, avg_selected_p - 0.58) * 1.8

    entry_edge = _safe_float(profile_values.get("entry_edge_bps"))
    entry_coverage = _safe_float(profile_values.get("entry_cost_coverage_ratio"), 1.0)
    gate_coverage = _safe_float(profile_values.get("gate_cost_coverage_ratio"), 1.0)
    min_atr_ratio = _safe_float(profile_values.get("entry_min_atr_to_cost_ratio"), 1.0)
    late_entry_context_range_pos = _safe_float(
        profile_values.get("late_entry_block_context_range_pos"),
        0.90,
    )
    late_entry_structure_range_pos = _safe_float(
        profile_values.get("late_entry_block_structure_range_pos"),
        0.985,
    )
    late_entry_max_context_drawdown_bps = _safe_float(
        profile_values.get("late_entry_block_max_context_drawdown_bps"),
        20.0,
    )
    late_entry_min_trend_return_bps = _safe_float(
        profile_values.get("late_entry_block_min_trend_return_bps"),
        90.0,
    )
    late_entry_min_return_bps = _safe_float(
        profile_values.get("late_entry_block_min_return_bps"),
        10.0,
    )
    weak_exit_reentry_cooldown = max(
        0,
        int(round(_safe_float(profile_values.get("reentry_cooldown_bars_after_weak_exit"), 0.0))),
    )
    reentry_move = _safe_float(profile_values.get("reentry_min_move_bps"), 0.0)
    failed_start_min_bars = max(0, int(round(_safe_float(profile_values.get("failed_start_min_bars"), 1.0))))
    failed_start_max_bars = max(1, int(round(_safe_float(profile_values.get("failed_start_max_bars"), 2.0))))
    failed_start_loss_bps = _safe_float(profile_values.get("failed_start_loss_bps"), 24.0)
    failed_start_min_rebound_bps = _safe_float(profile_values.get("failed_start_min_rebound_bps"), 24.0)
    trailing_activation_bps = _safe_float(profile_values.get("trailing_activation_bps"), 18.0)
    trailing_stop_bps = _safe_float(profile_values.get("trailing_stop_bps"), 9.0)
    min_exit_profit_bps = _safe_float(profile_values.get("min_exit_profit_bps"), 10.0)
    green_candle_take_min_bars = max(1, int(round(_safe_float(profile_values.get("green_candle_take_min_bars"), 2.0))))
    green_candle_take_max_bars = max(0, int(round(_safe_float(profile_values.get("green_candle_take_max_bars"), 0.0))))
    green_candle_take_required_green_bars = max(
        1,
        int(round(_safe_float(profile_values.get("green_candle_take_required_green_bars"), 2.0))),
    )
    green_candle_take_min_profit_bps = _safe_float(profile_values.get("green_candle_take_min_profit_bps"), 2.0)
    campaign_hold_enabled = 1 if int(round(_safe_float(profile_values.get("campaign_hold_enabled"), 1.0))) else 0
    campaign_hold_min_bars = max(1, int(round(_safe_float(profile_values.get("campaign_hold_min_bars"), 3.0))))
    campaign_hold_min_profit_bps = _safe_float(profile_values.get("campaign_hold_min_profit_bps"), 10.0)
    campaign_hold_min_trend_bps = _safe_float(profile_values.get("campaign_hold_min_trend_bps"), 120.0)
    campaign_hold_max_drawdown_bps = _safe_float(
        profile_values.get("campaign_hold_max_drawdown_from_peak_bps"),
        32.0,
    )
    time_break_even_floor_bars = max(0, int(round(_safe_float(profile_values.get("time_break_even_floor_bars"), 0.0))))
    shakeout_hold_bias = max(
        shakeout_then_run_rate,
        max(0.0, avg_post_exit_close_60m_bps) / 30.0,
        max(0.0, avg_post_exit_close_120m_bps) / 42.0,
    )
    micro_pop_hold_bias = max(
        micro_pop_loss_run_rate,
        max(0.0, avg_post_exit_close_60m_bps) / 26.0,
    )
    missed_entry_hold_bias = max(
        missed_entry_bias,
        missed_entry_rate,
        max(0.0, avg_post_decision_close_60m_bps) / 26.0,
    )
    exit_problem_dominant = (
        early_exit_bias >= max(0.18, protective_exit_bias + 0.08)
        or failed_start_recovery_rate >= 0.35
        or hold_opportunity_rate >= 0.35
        or micro_pop_loss_run_rate >= 0.15
        or shakeout_then_run_rate >= 0.20
    )

    if (loss_pressure > positive_pressure or failed_start_rate >= 0.25) and not exit_problem_dominant:
        entry_edge += 0.55 * loss_pressure + 0.50 * failed_start_rate
        entry_coverage = min(1.0, entry_coverage + (0.10 * loss_pressure) + (0.10 * failed_start_rate))
        gate_coverage = min(1.0, gate_coverage + (0.08 * loss_pressure) + (0.10 * failed_start_rate))
        min_atr_ratio = min(1.45, min_atr_ratio + (0.08 * loss_pressure) + (0.05 * failed_start_rate))
        reentry_move += (12.0 * loss_pressure) + (20.0 * failed_start_rate)
        failed_start_max_bars = max(1, failed_start_max_bars - (1 if failed_start_rate >= 0.40 else 0))
        failed_start_loss_bps = max(12.0, failed_start_loss_bps - (7.0 * loss_pressure) - (10.0 * failed_start_rate))
    elif positive_pressure > 0.20 and trade_count >= 6:
        entry_edge = max(0.6, entry_edge - (0.25 * positive_pressure))
        entry_coverage = max(0.45, entry_coverage - (0.04 * positive_pressure))
        gate_coverage = max(0.40, gate_coverage - (0.04 * positive_pressure))
        min_atr_ratio = max(0.70, min_atr_ratio - (0.04 * positive_pressure))
        reentry_move = max(20.0, reentry_move - (6.0 * positive_pressure))

    if missed_entry_bias >= 0.18 and missed_entry_bias > correct_block_bias:
        entry_edge = max(0.5, entry_edge - (0.45 * missed_entry_hold_bias))
        entry_coverage = max(0.38, entry_coverage - (0.06 * missed_entry_hold_bias))
        gate_coverage = max(0.34, gate_coverage - (0.07 * missed_entry_hold_bias))
        min_atr_ratio = max(0.62, min_atr_ratio - (0.05 * missed_entry_hold_bias))
        reentry_move = max(14.0, reentry_move - (10.0 * missed_entry_hold_bias))
    elif correct_block_bias >= 0.18 and correct_block_bias > missed_entry_bias:
        entry_edge += 0.25 * correct_block_bias
        entry_coverage = min(1.0, entry_coverage + (0.04 * correct_block_bias))
        gate_coverage = min(1.0, gate_coverage + (0.05 * correct_block_bias))
        min_atr_ratio = min(1.55, min_atr_ratio + (0.04 * correct_block_bias))

    if early_exit_bias >= 0.18 and early_exit_bias > protective_exit_bias:
        entry_edge = max(0.6, entry_edge - (0.35 * early_exit_bias))
        entry_coverage = max(0.45, entry_coverage - (0.05 * early_exit_bias))
        gate_coverage = max(0.40, gate_coverage - (0.05 * early_exit_bias))
        reentry_move = max(18.0, reentry_move - (8.0 * early_exit_bias) - (4.0 * shakeout_hold_bias))
        weak_exit_reentry_cooldown = min(
            12,
            weak_exit_reentry_cooldown
            + (1 if early_exit_bias >= 0.25 else 0)
            + (1 if failed_start_recovery_rate >= 0.30 else 0)
            + (1 if shakeout_then_run_rate >= 0.20 or micro_pop_loss_run_rate >= 0.15 else 0),
        )
        failed_start_min_bars = min(
            6,
            failed_start_min_bars
            + (1 if early_exit_bias >= 0.25 else 0)
            + (1 if failed_start_recovery_rate >= 0.35 else 0)
            + (1 if hold_opportunity_rate >= 0.45 else 0)
            + (1 if shakeout_then_run_rate >= 0.20 or micro_pop_loss_run_rate >= 0.15 else 0),
        )
        failed_start_max_bars = min(
            6,
            failed_start_max_bars
            + (1 if failed_start_recovery_rate >= 0.35 else 0)
            + (1 if early_exit_bias >= 0.55 else 0),
        )
        failed_start_loss_bps = min(
            60.0,
            failed_start_loss_bps
            + (10.0 * early_exit_bias)
            + (6.0 * failed_start_recovery_rate)
            + (6.0 * shakeout_hold_bias),
        )
        failed_start_min_rebound_bps = max(
            6.0,
            failed_start_min_rebound_bps - (8.0 * early_exit_bias) - (4.0 * shakeout_hold_bias),
        )
        trailing_activation_bps = min(
            54.0,
            trailing_activation_bps + (5.0 * early_exit_bias) + (4.0 * shakeout_hold_bias),
        )
        trailing_stop_bps = min(
            24.0,
            trailing_stop_bps
            + (3.5 * early_exit_bias)
            + (1.5 * trailing_missed_run_rate)
            + (2.0 * shakeout_hold_bias),
        )
        min_exit_profit_bps = min(
            28.0,
            min_exit_profit_bps + (2.5 * early_exit_bias) + (1.25 * shakeout_hold_bias),
        )
        campaign_hold_enabled = 1
        campaign_hold_min_bars = min(
            12,
            campaign_hold_min_bars
            + (1 if early_exit_bias >= 0.35 else 0)
            + (1 if shakeout_then_run_rate >= 0.25 else 0),
        )
        time_break_even_floor_bars = min(
            24,
            time_break_even_floor_bars
            + (1 if early_exit_bias >= 0.30 else 0)
            + (1 if failed_start_recovery_rate >= 0.35 else 0)
            + (1 if hold_opportunity_rate >= 0.50 else 0),
        )
        campaign_hold_min_profit_bps = max(
            4.0,
            campaign_hold_min_profit_bps - (2.5 * early_exit_bias) - (1.5 * shakeout_hold_bias),
        )
        campaign_hold_min_trend_bps = max(
            55.0,
            campaign_hold_min_trend_bps - (18.0 * early_exit_bias) - (12.0 * shakeout_hold_bias),
        )
        campaign_hold_max_drawdown_bps = min(
            72.0,
            campaign_hold_max_drawdown_bps + (8.0 * early_exit_bias) + (10.0 * shakeout_hold_bias),
        )

    if micro_pop_loss_run_rate >= 0.15:
        min_exit_profit_bps = min(34.0, min_exit_profit_bps + (3.0 * micro_pop_hold_bias))
        trailing_activation_bps = min(64.0, trailing_activation_bps + (7.0 * micro_pop_hold_bias))
        trailing_stop_bps = min(26.0, trailing_stop_bps + (2.0 * micro_pop_hold_bias))
        weak_exit_reentry_cooldown = min(
            12,
            weak_exit_reentry_cooldown
            + (1 if micro_pop_loss_run_rate >= 0.15 else 0)
            + (1 if micro_pop_loss_run_rate >= 0.35 else 0),
        )
        time_break_even_floor_bars = min(
            24,
            time_break_even_floor_bars
            + (1 if micro_pop_loss_run_rate >= 0.15 else 0)
            + (1 if micro_pop_loss_run_rate >= 0.30 else 0)
            + (1 if micro_pop_loss_run_rate >= 0.45 else 0),
        )
        green_candle_take_min_bars = min(
            6,
            green_candle_take_min_bars
            + (1 if micro_pop_loss_run_rate >= 0.20 else 0)
            + (1 if micro_pop_loss_run_rate >= 0.45 else 0),
        )
        if green_candle_take_max_bars > 0:
            green_candle_take_max_bars = min(
                8,
                max(green_candle_take_max_bars, green_candle_take_min_bars + 1)
                + (1 if micro_pop_loss_run_rate >= 0.35 else 0),
            )
        green_candle_take_required_green_bars = min(
            4,
            green_candle_take_required_green_bars + (1 if micro_pop_loss_run_rate >= 0.30 else 0),
        )
        green_candle_take_min_profit_bps = min(
            18.0,
            green_candle_take_min_profit_bps + (4.0 * micro_pop_hold_bias) + (1.5 * early_exit_bias),
        )

    if late_entry_bias >= 0.18:
        late_entry_context_range_pos = max(
            0.72,
            late_entry_context_range_pos
            - (0.05 * late_entry_bias)
            - (0.03 * top_zone_entry_rate),
        )
        late_entry_structure_range_pos = max(
            0.82,
            late_entry_structure_range_pos
            - (0.04 * late_entry_bias)
            - (0.02 * top_zone_entry_rate),
        )
        late_entry_max_context_drawdown_bps = min(
            80.0,
            late_entry_max_context_drawdown_bps
            + (12.0 * late_entry_bias)
            + (8.0 * top_zone_entry_rate)
            + (5.0 * poor_entry_rate),
        )
        late_entry_min_trend_return_bps = max(
            30.0,
            late_entry_min_trend_return_bps
            - (24.0 * late_entry_bias)
            - (12.0 * top_zone_entry_rate),
        )
        late_entry_min_return_bps = max(
            2.0,
            late_entry_min_return_bps
            - (6.0 * late_entry_bias)
            - (3.0 * top_zone_entry_rate),
        )
        if late_entry_bias > (missed_entry_bias + 0.06):
            entry_edge += (0.40 * late_entry_bias) + (0.20 * top_zone_entry_rate)
            entry_coverage = min(
                1.0,
                entry_coverage + (0.05 * late_entry_bias) + (0.02 * poor_entry_rate),
            )
            gate_coverage = min(
                1.0,
                gate_coverage + (0.04 * late_entry_bias) + (0.02 * top_zone_entry_rate),
            )
            min_atr_ratio = min(
                1.55,
                min_atr_ratio + (0.04 * late_entry_bias) + (0.02 * poor_entry_rate),
            )

    return {
        "ROTATION_ENTRY_EDGE_BPS": round(entry_edge, 4),
        "ROTATION_ENTRY_COST_COVERAGE_RATIO": round(entry_coverage, 4),
        "ROTATION_GATE_COST_COVERAGE_RATIO": round(gate_coverage, 4),
        "ROTATION_ENTRY_MIN_ATR_TO_COST_RATIO": round(min_atr_ratio, 4),
        "ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS": round(late_entry_context_range_pos, 4),
        "ROTATION_LATE_ENTRY_BLOCK_STRUCTURE_RANGE_POS": round(
            late_entry_structure_range_pos, 4
        ),
        "ROTATION_LATE_ENTRY_BLOCK_MAX_CONTEXT_DRAWDOWN_BPS": round(
            late_entry_max_context_drawdown_bps, 4
        ),
        "ROTATION_LATE_ENTRY_BLOCK_MIN_TREND_RETURN_BPS": round(
            late_entry_min_trend_return_bps, 4
        ),
        "ROTATION_LATE_ENTRY_BLOCK_MIN_RETURN_BPS": round(late_entry_min_return_bps, 4),
        "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WEAK_EXIT": int(weak_exit_reentry_cooldown),
        "ROTATION_REENTRY_MIN_MOVE_BPS": round(reentry_move, 4),
        "ROTATION_FAILED_START_MIN_BARS": int(failed_start_min_bars),
        "ROTATION_FAILED_START_MAX_BARS": int(failed_start_max_bars),
        "ROTATION_FAILED_START_LOSS_BPS": round(failed_start_loss_bps, 4),
        "ROTATION_FAILED_START_MIN_REBOUND_BPS": round(failed_start_min_rebound_bps, 4),
        "ROTATION_TRAILING_ACTIVATION_BPS": round(trailing_activation_bps, 4),
        "ROTATION_TRAILING_STOP_BPS": round(trailing_stop_bps, 4),
        "ROTATION_MIN_EXIT_PROFIT_BPS": round(min_exit_profit_bps, 4),
        "ROTATION_GREEN_CANDLE_TAKE_MIN_BARS": int(green_candle_take_min_bars),
        "ROTATION_GREEN_CANDLE_TAKE_MAX_BARS": int(green_candle_take_max_bars),
        "ROTATION_GREEN_CANDLE_TAKE_REQUIRED_GREEN_BARS": int(green_candle_take_required_green_bars),
        "ROTATION_GREEN_CANDLE_TAKE_MIN_PROFIT_BPS": round(green_candle_take_min_profit_bps, 4),
        "ROTATION_TIME_BREAK_EVEN_FLOOR_BARS": int(time_break_even_floor_bars),
        "ROTATION_CAMPAIGN_HOLD_ENABLED": int(campaign_hold_enabled),
        "ROTATION_CAMPAIGN_HOLD_MIN_BARS": int(campaign_hold_min_bars),
        "ROTATION_CAMPAIGN_HOLD_MIN_PROFIT_BPS": round(campaign_hold_min_profit_bps, 4),
        "ROTATION_CAMPAIGN_HOLD_MIN_TREND_BPS": round(campaign_hold_min_trend_bps, 4),
        "ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS": round(campaign_hold_max_drawdown_bps, 4),
    }


def recommend_rotation_autotune(
    *,
    current_profile: str,
    trade_summary: TradeSummary,
    candidates: list[ShadowCandidate],
    profile_payloads: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evaluations = [
        evaluate_profile_payload(
            profile=profile,
            payload=payload,
            trade_summary=trade_summary,
            candidates=candidates,
        )
        for profile, payload in profile_payloads.items()
        if profile in SUPPORTED_AUTOTUNE_PROFILES and isinstance(payload, dict)
    ]
    if not evaluations:
        return {
            "enabled": False,
            "current_profile": current_profile,
            "recommended_profile": current_profile,
            "confidence": 0.0,
            "score_margin": 0.0,
            "parameter_overrides": {},
            "avoid_symbols": [],
            "evaluations": [],
            "reason": "no_profile_payloads",
        }

    evaluations.sort(key=lambda item: (-_safe_float(item.get("score")), item.get("profile", "")))
    best = evaluations[0]
    runner_up = evaluations[1] if len(evaluations) > 1 else None
    margin = _safe_float(best.get("score")) - _safe_float((runner_up or {}).get("score"))
    trade_stats = _trade_window_stats(trade_summary)
    early_exit_bias = _safe_float(trade_stats.get("early_exit_bias"))
    protective_exit_bias = _safe_float(trade_stats.get("protective_exit_bias"))
    late_entry_bias = _safe_float(trade_stats.get("late_entry_bias"))
    missed_entry_bias = _safe_float(trade_stats.get("missed_entry_bias"))
    correct_block_bias = _safe_float(trade_stats.get("correct_block_bias"))
    missed_entry_bias = _safe_float(trade_stats.get("missed_entry_bias"))
    correct_block_bias = _safe_float(trade_stats.get("correct_block_bias"))

    confidence = 0.35
    confidence += min(0.25, max(0.0, margin) / 80.0)
    confidence += min(0.20, _safe_float(best.get("avg_selected_p_profit")) - 0.5)
    confidence += min(0.15, trade_stats["trade_count"] / 24.0)
    confidence += min(0.10, _safe_float(trade_stats.get("no_trade_sample_count")) / 36.0)
    confidence = max(0.15, min(0.95, confidence))

    best_profile = str(best.get("profile") or current_profile)
    strong_learning_signal = (
        early_exit_bias >= 0.45
        or late_entry_bias >= 0.35
        or missed_entry_bias >= 0.35
        or (correct_block_bias >= 0.40 and trade_stats["very_bad_cycle"])
    )
    if (
        best_profile != current_profile
        and int(trade_stats["trade_count"]) < MIN_PROFILE_SWITCH_TRADES
        and _safe_float(trade_stats.get("no_trade_sample_count")) < MIN_PROFILE_SWITCH_TRADES
        and not strong_learning_signal
    ):
        best_profile = current_profile
    if (
        best_profile != current_profile
        and margin < MIN_PROFILE_SWITCH_MARGIN
        and not strong_learning_signal
    ):
        best_profile = current_profile
    best_values = best.get("profile_values") if isinstance(best.get("profile_values"), dict) else {}
    parameter_overrides = _build_parameter_overrides(
        profile_values=best_values,
        trade_summary=trade_summary,
        evaluation=best,
    )

    avoid_symbols: list[str] = []
    exit_problem_symbols: list[str] = []
    symbol_breakdown = getattr(trade_summary, "symbol_breakdown", {}) or {}
    if isinstance(symbol_breakdown, dict):
        ranked_avoids: list[tuple[str, float, float]] = []
        for symbol, stats in symbol_breakdown.items():
            if not isinstance(stats, dict):
                continue
            trade_count = int(stats.get("trade_count") or 0)
            net_pnl = _safe_float(stats.get("net_pnl"))
            failed_start_rate = _failed_start_rate(stats.get("exit_reasons") or {})
            symbol_exit_path = _exit_path_stats(stats.get("exit_path_summary") or {})
            symbol_entry_path = _entry_path_stats(stats.get("entry_path_summary") or {})
            if (
                trade_count >= 1
                and _safe_float(symbol_exit_path.get("early_exit_bias")) >= 0.45
                and _safe_float(symbol_exit_path.get("failed_start_recovery_rate")) >= 0.5
            ):
                exit_problem_symbols.append(str(symbol).upper())
            if (
                trade_count >= MIN_SYMBOL_EVIDENCE_TRADES
                and (net_pnl <= -0.08 or failed_start_rate >= 0.5)
                and _safe_float(symbol_exit_path.get("early_exit_bias")) < 0.35
                and _safe_float(symbol_entry_path.get("late_entry_bias")) < 0.40
            ):
                ranked_avoids.append((str(symbol).upper(), net_pnl, failed_start_rate))
        ranked_avoids.sort(key=lambda item: (item[1], -item[2], item[0]))
        avoid_symbols = [symbol for symbol, _net, _rate in ranked_avoids[:6]]
        exit_problem_symbols = sorted(set(exit_problem_symbols))

    reason = f"best_profile={best_profile} margin={margin:.3f}"
    if trade_stats["very_bad_cycle"] and best.get("market_snapshot", {}).get("breakout_market"):
        reason = f"{reason}; uptrend_only_drawdown_reset"
    elif trade_stats["very_bad_cycle"]:
        reason = f"{reason}; continued_drawdown"
    if early_exit_bias >= 0.35 and early_exit_bias > protective_exit_bias:
        reason = f"{reason}; early_exit_recovery_detected"
    if _safe_float(trade_stats.get("shakeout_then_run_rate")) >= 0.20:
        reason = f"{reason}; shakeout_then_run_detected"
    if _safe_float(trade_stats.get("micro_pop_loss_run_rate")) >= 0.15:
        reason = f"{reason}; micro_pop_loss_run_detected"
    if late_entry_bias >= 0.30:
        reason = f"{reason}; late_entry_top_chase_detected"
    if missed_entry_bias >= 0.25 and missed_entry_bias > correct_block_bias:
        reason = f"{reason}; gates_too_strict_detected"

    risk_mode_override = ""
    best_market = best.get("market_snapshot") if isinstance(best.get("market_snapshot"), dict) else {}
    if (
        confidence >= 0.50
        and early_exit_bias >= 0.45
        and early_exit_bias > (protective_exit_bias + 0.10)
    ):
        if bool(best_market.get("breakout_market")) and _safe_float(best.get("avg_selected_p_profit")) >= 0.54:
            risk_mode_override = "normal"
        elif bool(best_market.get("guarded_market")):
            risk_mode_override = "cautious"
    if (
        not risk_mode_override
        and missed_entry_bias >= 0.35
        and missed_entry_bias > (correct_block_bias + 0.10)
        and _safe_float(best.get("avg_selected_p_profit")) >= 0.54
    ):
        risk_mode_override = "normal" if bool(best_market.get("breakout_market")) else "cautious"

    return {
        "enabled": True,
        "current_profile": current_profile,
        "recommended_profile": best_profile,
        "confidence": round(confidence, 4),
        "score_margin": round(margin, 6),
        "parameter_overrides": parameter_overrides,
        "avoid_symbols": avoid_symbols,
        "exit_problem_symbols": exit_problem_symbols,
        "evaluations": evaluations,
        "reason": reason,
        "risk_mode_override": risk_mode_override,
        "trade_count": int(trade_stats["trade_count"]),
        "no_trade_sample_count": int(round(_safe_float(trade_stats.get("no_trade_sample_count"), 0.0))),
        "early_exit_bias": round(early_exit_bias, 6),
        "protective_exit_bias": round(protective_exit_bias, 6),
        "failed_start_recovery_rate": round(_safe_float(trade_stats.get("failed_start_recovery_rate")), 6),
        "hold_opportunity_rate": round(_safe_float(trade_stats.get("hold_opportunity_rate")), 6),
        "late_entry_bias": round(late_entry_bias, 6),
        "late_entry_rate": round(_safe_float(trade_stats.get("late_entry_rate")), 6),
        "top_zone_entry_rate": round(_safe_float(trade_stats.get("top_zone_entry_rate")), 6),
        "poor_entry_rate": round(_safe_float(trade_stats.get("poor_entry_rate")), 6),
        "missed_entry_bias": round(missed_entry_bias, 6),
        "correct_block_bias": round(correct_block_bias, 6),
        "missed_entry_rate": round(_safe_float(trade_stats.get("missed_entry_rate")), 6),
        "correct_block_rate": round(_safe_float(trade_stats.get("correct_block_rate")), 6),
        "shakeout_then_run_rate": round(_safe_float(trade_stats.get("shakeout_then_run_rate")), 6),
        "micro_pop_loss_run_rate": round(_safe_float(trade_stats.get("micro_pop_loss_run_rate")), 6),
    }
