#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.meta.rotation_shadow import (  # noqa: E402
    NON_TUNABLE_BLOCK_REASONS,
    CounterfactualSample,
    TradeSample,
    _early_exit_signal,
    _failed_start_recovery_signal,
    _late_entry_signal,
    _micro_pop_loss_run_signal,
    _parse_ts,
    _post_exit_metric,
    _shakeout_then_run_signal,
    _symbol_from_journal,
    _top_zone_entry_signal,
    _trade_exit_price,
    _trade_gross_exit_bps,
    _trade_hold_minutes,
    _trade_net_exit_bps,
    _trailing_missed_run_signal,
    build_trade_summary,
    extract_counterfactual_samples,
    extract_trade_samples,
    load_env_file,
    load_rotation_state,
    summarize_trade_sample_verification,
)


DEFAULT_ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
DEFAULT_RUNTIME_ENV_FILE = REPO_ROOT / "configs" / "rotation_meta_runtime.env"
DEFAULT_AUTOTUNE_REPORT_FILE = REPO_ROOT / "logs" / "rotation_profile_autotune_report.json"
DEFAULT_REPORT_FILE = REPO_ROOT / "logs" / "rotation_replay_lab_report.json"
JOURNAL_GLOB = "journal_live_binance_*_usdc_rotation.jsonl"
DEFAULT_PROFILE = "scalp_guarded"
DEFAULT_UNIT_NOTIONAL = 8.0
EXIT_WEAK_REASONS = {
    "failed_start_exit",
    "time_break_even_floor",
    "chop_break_even_reclaim",
}
ENV_PROFILE_KEY_MAP: dict[str, str] = {
    "ROTATION_ENTRY_EDGE_BPS": "entry_edge_bps",
    "ROTATION_ENTRY_COST_BUFFER_BPS": "entry_cost_buffer_bps",
    "ROTATION_ENTRY_COST_COVERAGE_RATIO": "entry_cost_coverage_ratio",
    "ROTATION_ENTRY_COST_ROUNDTRIP_MULTIPLIER": "entry_cost_roundtrip_multiplier",
    "ROTATION_ENTRY_MIN_ATR_TO_COST_RATIO": "entry_min_atr_to_cost_ratio",
    "ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS": "late_entry_block_context_range_pos",
    "ROTATION_LATE_ENTRY_BLOCK_STRUCTURE_RANGE_POS": "late_entry_block_structure_range_pos",
    "ROTATION_LATE_ENTRY_BLOCK_MAX_CONTEXT_DRAWDOWN_BPS": "late_entry_block_max_context_drawdown_bps",
    "ROTATION_LATE_ENTRY_BLOCK_MIN_TREND_RETURN_BPS": "late_entry_block_min_trend_return_bps",
    "ROTATION_LATE_ENTRY_BLOCK_MIN_RETURN_BPS": "late_entry_block_min_return_bps",
    "ROTATION_CONT_TREND_MIN_BPS": "cont_trend_min_bps",
    "ROTATION_CONT_REBOUND_TRIGGER_BPS": "cont_rebound_trigger_bps",
    "ROTATION_CONT_MIN_VOLUME_Z": "cont_min_volume_z",
    "ROTATION_CONT_MAX_RANGE_POS": "cont_max_range_pos",
    "ROTATION_CONT_MAX_STRUCTURE_RANGE_POS": "cont_max_structure_range_pos",
    "ROTATION_CONT_RANGE_CONTINUATION_MAX_RANGE_POS": "cont_range_continuation_max_range_pos",
    "ROTATION_CONT_STAIRCASE_MIN_TREND_BPS": "cont_staircase_min_trend_bps",
    "ROTATION_CONT_STAIRCASE_MIN_RET_BPS": "cont_staircase_min_ret_bps",
    "ROTATION_CONT_STAIRCASE_MIN_VOLUME_Z": "cont_staircase_min_volume_z",
    "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS": "cont_staircase_min_slope_medium_bps",
    "ROTATION_CONT_STAIRCASE_MIN_SLOPE_LONG_BPS": "cont_staircase_min_slope_long_bps",
    "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS": "cont_staircase_min_drawdown_from_peak_bps",
    "ROTATION_CONT_STAIRCASE_MAX_DRAWDOWN_FROM_PEAK_BPS": "cont_staircase_max_drawdown_from_peak_bps",
    "ROTATION_CONT_STAIRCASE_MAX_CONTEXT_RANGE_POS": "cont_staircase_max_context_range_pos",
    "ROTATION_CONT_STAIRCASE_MAX_SPREAD_BPS": "cont_staircase_max_spread_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MAX_CONTEXT_RANGE_POS": "cont_early_liftoff_max_context_range_pos",
    "ROTATION_CONT_EARLY_LIFTOFF_MAX_STRUCTURE_RANGE_POS": "cont_early_liftoff_max_structure_range_pos",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_CONTEXT_REBOUND_BPS": "cont_early_liftoff_min_context_rebound_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MAX_SPREAD_BPS": "cont_early_liftoff_max_spread_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_VOLUME_Z": "cont_early_liftoff_min_volume_z",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_RET_BPS": "cont_early_liftoff_min_ret_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_SLOPE_SHORT_BPS": "cont_early_liftoff_min_slope_short_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_SLOPE_MEDIUM_BPS": "cont_early_liftoff_min_slope_medium_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_DRAWDOWN_FROM_PEAK_BPS": "cont_early_liftoff_min_drawdown_from_peak_bps",
    "ROTATION_CONT_EARLY_LIFTOFF_MIN_TREND_BPS": "cont_early_liftoff_min_trend_bps",
    "ROTATION_BREAKOUT_TRIGGER_BPS": "breakout_trigger_bps",
    "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_CONTEXT_RANGE_POS": "breakout_thin_rebound_block_context_range_pos",
    "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_CONTEXT_REBOUND_BPS": "breakout_thin_rebound_block_context_rebound_bps",
    "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_MIN_SPREAD_BPS": "breakout_thin_rebound_block_min_spread_bps",
    "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_CONTEXT_RANGE_POS": "breakout_mid_rebound_block_context_range_pos",
    "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_CONTEXT_REBOUND_BPS": "breakout_mid_rebound_block_context_rebound_bps",
    "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_MIN_VOLUME_Z": "breakout_mid_rebound_block_min_volume_z",
    "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_CONTEXT_RANGE_POS": "breakout_late_rebound_block_context_range_pos",
    "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_CONTEXT_REBOUND_BPS": "breakout_late_rebound_block_context_rebound_bps",
    "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_MIN_VOLUME_Z": "breakout_late_rebound_block_min_volume_z",
    "ROTATION_GATE_COST_COVERAGE_RATIO": "gate_cost_coverage_ratio",
    "ROTATION_GATE_COST_ROUNDTRIP_MULTIPLIER": "gate_cost_roundtrip_multiplier",
    "ROTATION_SWING_MICRO_REBOUND_MAX_SPREAD_BPS": "swing_micro_rebound_max_spread_bps",
    "ROTATION_SWING_MICRO_REBOUND_MAX_CONTEXT_RANGE_POS": "swing_micro_rebound_max_context_range_pos",
    "ROTATION_SWING_MICRO_REBOUND_MIN_CONTEXT_REBOUND_BPS": "swing_micro_rebound_min_context_rebound_bps",
    "ROTATION_SWING_MICRO_REBOUND_MIN_RET_BPS": "swing_micro_rebound_min_ret_bps",
    "ROTATION_SWING_REVERSAL_THRESHOLD_BPS": "swing_reversal_threshold_bps",
    "ROTATION_SWING_MIN_RANGE_BPS": "swing_min_range_bps",
    "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WEAK_EXIT": "reentry_cooldown_bars_after_weak_exit",
    "ROTATION_REENTRY_MIN_MOVE_BPS": "reentry_min_move_bps",
    "ROTATION_FAILED_START_MIN_BARS": "failed_start_min_bars",
    "ROTATION_FAILED_START_MAX_BARS": "failed_start_max_bars",
    "ROTATION_FAILED_START_MIN_REBOUND_BPS": "failed_start_min_rebound_bps",
    "ROTATION_FAILED_START_LOSS_BPS": "failed_start_loss_bps",
    "ROTATION_HARD_STOP_LOSS_BPS": "hard_stop_loss_bps",
    "ROTATION_MIN_EXIT_PROFIT_BPS": "min_exit_profit_bps",
    "ROTATION_GREEN_CANDLE_TAKE_MIN_BARS": "green_candle_take_min_bars",
    "ROTATION_GREEN_CANDLE_TAKE_MAX_BARS": "green_candle_take_max_bars",
    "ROTATION_GREEN_CANDLE_TAKE_REQUIRED_GREEN_BARS": "green_candle_take_required_green_bars",
    "ROTATION_GREEN_CANDLE_TAKE_MIN_PROFIT_BPS": "green_candle_take_min_profit_bps",
    "ROTATION_TRAILING_ACTIVATION_BPS": "trailing_activation_bps",
    "ROTATION_TRAILING_STOP_BPS": "trailing_stop_bps",
    "ROTATION_CAMPAIGN_HOLD_ENABLED": "campaign_hold_enabled",
    "ROTATION_CAMPAIGN_HOLD_MIN_BARS": "campaign_hold_min_bars",
    "ROTATION_CAMPAIGN_HOLD_MIN_PROFIT_BPS": "campaign_hold_min_profit_bps",
    "ROTATION_CAMPAIGN_HOLD_MIN_TREND_BPS": "campaign_hold_min_trend_bps",
    "ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS": "campaign_hold_max_drawdown_from_peak_bps",
    "ROTATION_TIME_BREAK_EVEN_FLOOR_BARS": "time_break_even_floor_bars",
    "ROTATION_MD_INTERVAL_SECONDS": "md_interval_seconds",
}
PROFILE_KEY_ENV_MAP = {value: key for key, value in ENV_PROFILE_KEY_MAP.items()}
RUNTIME_ACTION_STRATEGIES = (
    "BREAKOUT",
    "STAIRCASE",
    "PULLBACK_CONTINUATION",
    "BREAKOUT_RETEST",
    "CONTINUATION",
    "RELATIVE_STRENGTH",
    "REBOUND",
)


@dataclass
class PolicyCandidate:
    name: str
    profile: str
    risk_mode: str
    values: dict[str, float]
    source: str
    notes: list[str] = field(default_factory=list)


@dataclass
class CandidateResult:
    name: str
    score: float
    reliability_score: float
    est_net_pnl: float
    net_pnl_delta: float
    executed_trade_count: int
    synthetic_trade_count: int
    blocked_trade_count: int
    blocked_negative_trade_count: int
    blocked_positive_trade_count: int
    improved_exit_count: int
    added_trade_count: int
    added_winner_count: int
    remaining_late_entry_losses: int
    remaining_early_exit_bugs: int
    remaining_weak_exit_reentries: int
    reason_counts: dict[str, int]
    parameter_overrides: dict[str, float]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["score"] = round(float(self.score), 6)
        payload["reliability_score"] = round(float(self.reliability_score), 6)
        payload["est_net_pnl"] = round(float(self.est_net_pnl), 6)
        payload["net_pnl_delta"] = round(float(self.net_pnl_delta), 6)
        payload["parameter_overrides"] = {
            key: round(float(value), 6)
            for key, value in sorted(self.parameter_overrides.items())
        }
        return payload


@dataclass
class ReplayWindow:
    lookback_hours: float
    trades: list[TradeSample]
    no_trade_samples: list[CounterfactualSample]
    trade_summary: Any
    unit_notional: float
    baseline_result: CandidateResult


@dataclass
class AggregateCandidateResult:
    name: str
    primary_result: CandidateResult
    aggregate_score_delta: float
    aggregate_net_pnl_delta: float
    aggregate_reliability_delta: float
    improving_window_count: int
    worsening_window_count: int
    neutral_window_count: int
    accepted: bool
    acceptance_reasons: list[str]
    window_results: list[dict[str, Any]]
    parameter_overrides: dict[str, float]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "aggregate_score_delta": _round_float(self.aggregate_score_delta),
            "aggregate_net_pnl_delta": _round_float(self.aggregate_net_pnl_delta),
            "aggregate_reliability_delta": _round_float(self.aggregate_reliability_delta),
            "improving_window_count": int(self.improving_window_count),
            "worsening_window_count": int(self.worsening_window_count),
            "neutral_window_count": int(self.neutral_window_count),
            "accepted": bool(self.accepted),
            "acceptance_reasons": list(self.acceptance_reasons),
            "window_results": list(self.window_results),
            "parameter_overrides": {
                key: _round_float(value)
                for key, value in sorted(self.parameter_overrides.items())
            },
            "notes": list(self.notes),
        }


@dataclass
class SymbolState:
    open_until: datetime | None = None
    last_exit_price: float = 0.0
    last_weak_exit_ts: datetime | None = None


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _round_float(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _cutoff_dt(lookback_hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(lookback_hours)))


def _reference_now_from_rows(rows: list[object], *, ts_getter) -> datetime:
    latest: datetime | None = None
    for row in rows:
        ts = _parse_ts(ts_getter(row))
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest or datetime.now(timezone.utc)


def _parse_evaluation_windows(
    *,
    primary_lookback_hours: float,
    evaluation_windows: list[float] | None,
) -> list[float]:
    raw = list(evaluation_windows or [])
    if not raw:
        raw = [6.0, primary_lookback_hours, 48.0, 72.0]
    windows: list[float] = []
    seen: set[float] = set()
    for value in [primary_lookback_hours, *raw]:
        hours = round(max(0.1, _safe_float(value, primary_lookback_hours)), 4)
        if hours in seen:
            continue
        seen.add(hours)
        windows.append(hours)
    return windows


def _recent_trade_samples(samples: list[TradeSample], lookback_hours: float) -> list[TradeSample]:
    reference_now = _reference_now_from_rows(samples, ts_getter=lambda sample: getattr(sample, "exit_ts", None))
    cutoff = reference_now - timedelta(hours=max(0.1, float(lookback_hours)))
    recent = [sample for sample in samples if (_parse_ts(sample.exit_ts) or cutoff) >= cutoff]
    recent.sort(key=lambda item: ((_parse_ts(item.entry_ts) or cutoff), (_parse_ts(item.exit_ts) or cutoff)))
    return recent


def _recent_counterfactual_samples(
    samples: list[CounterfactualSample],
    lookback_hours: float,
) -> list[CounterfactualSample]:
    reference_now = _reference_now_from_rows(
        samples,
        ts_getter=lambda sample: getattr(sample, "decision_ts", None),
    )
    cutoff = reference_now - timedelta(hours=max(0.1, float(lookback_hours)))
    recent = [sample for sample in samples if (_parse_ts(sample.decision_ts) or cutoff) >= cutoff]
    recent.sort(key=lambda item: (_parse_ts(item.decision_ts) or cutoff))
    return recent


def _normalize_policy_values(values: dict[str, Any]) -> dict[str, float]:
    out = {str(key): _safe_float(value) for key, value in (values or {}).items()}
    for key in (
        "late_entry_block_context_range_pos",
        "late_entry_block_structure_range_pos",
    ):
        if key in out:
            out[key] = max(0.0, min(1.0, out[key]))
    out["entry_edge_bps"] = max(0.0, out.get("entry_edge_bps", 0.0))
    out["entry_cost_buffer_bps"] = max(0.0, out.get("entry_cost_buffer_bps", 0.0))
    out["entry_cost_coverage_ratio"] = max(0.0, min(1.2, out.get("entry_cost_coverage_ratio", 1.0)))
    out["entry_cost_roundtrip_multiplier"] = max(1.0, out.get("entry_cost_roundtrip_multiplier", 2.0))
    out["entry_min_atr_to_cost_ratio"] = max(0.0, out.get("entry_min_atr_to_cost_ratio", 1.0))
    out["gate_cost_coverage_ratio"] = max(0.0, min(1.2, out.get("gate_cost_coverage_ratio", 1.0)))
    out["gate_cost_roundtrip_multiplier"] = max(1.0, out.get("gate_cost_roundtrip_multiplier", 2.0))
    out["reentry_cooldown_bars_after_weak_exit"] = max(
        0.0, out.get("reentry_cooldown_bars_after_weak_exit", 0.0)
    )
    out["reentry_min_move_bps"] = max(0.0, out.get("reentry_min_move_bps", 0.0))
    out["failed_start_min_bars"] = max(0.0, out.get("failed_start_min_bars", 0.0))
    out["failed_start_max_bars"] = max(
        out["failed_start_min_bars"],
        out.get("failed_start_max_bars", out["failed_start_min_bars"]),
    )
    out["failed_start_loss_bps"] = max(0.0, out.get("failed_start_loss_bps", 0.0))
    out["failed_start_min_rebound_bps"] = max(0.0, out.get("failed_start_min_rebound_bps", 0.0))
    out["hard_stop_loss_bps"] = max(0.0, out.get("hard_stop_loss_bps", 0.0))
    out["trailing_activation_bps"] = max(0.0, out.get("trailing_activation_bps", 0.0))
    out["trailing_stop_bps"] = max(0.0, out.get("trailing_stop_bps", 0.0))
    out["time_break_even_floor_bars"] = max(0.0, out.get("time_break_even_floor_bars", 0.0))
    out["min_exit_profit_bps"] = max(0.0, out.get("min_exit_profit_bps", 0.0))
    out["campaign_hold_enabled"] = 1.0 if out.get("campaign_hold_enabled", 0.0) >= 0.5 else 0.0
    out["campaign_hold_min_bars"] = max(0.0, out.get("campaign_hold_min_bars", 0.0))
    out["campaign_hold_min_profit_bps"] = max(0.0, out.get("campaign_hold_min_profit_bps", 0.0))
    out["campaign_hold_min_trend_bps"] = max(0.0, out.get("campaign_hold_min_trend_bps", 0.0))
    out["campaign_hold_max_drawdown_from_peak_bps"] = max(
        0.0, out.get("campaign_hold_max_drawdown_from_peak_bps", 0.0)
    )
    out["md_interval_seconds"] = max(15.0, out.get("md_interval_seconds", 60.0))
    return out


def _profile_signature(values: dict[str, float]) -> tuple[tuple[str, float], ...]:
    return tuple(sorted((key, round(float(value), 5)) for key, value in values.items()))


def _policy_overrides_against_baseline(
    baseline: dict[str, float],
    candidate: dict[str, float],
) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for key, value in candidate.items():
        base_value = baseline.get(key)
        if base_value is None:
            overrides[key] = value
            continue
        if abs(float(value) - float(base_value)) > 1e-9:
            overrides[key] = value
    return overrides


def _runtime_env_profile_overrides(path: Path) -> dict[str, float]:
    env = load_env_file(path) if path.exists() else {}
    mode = str(env.get("ROTATION_META_MODE") or "").strip().lower()
    if mode == "disabled":
        return {}
    if mode == "fallback":
        runtime_has_action = any(
            str(env.get(f"ROTATION_STRATEGY_ACTION_{strategy}") or "").strip().lower()
            in {"primary", "secondary", "watch"}
            for strategy in RUNTIME_ACTION_STRATEGIES
        )
        if (
            not runtime_has_action
            and not str(env.get("ROTATION_STRATEGY_SLOT_PLAN") or "").strip()
            and not str(env.get("ROTATION_META_CANDIDATE_OVERRIDES") or "").strip()
        ):
            return {}
    overrides: dict[str, float] = {}
    for env_name, profile_key in ENV_PROFILE_KEY_MAP.items():
        raw = env.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        overrides[profile_key] = _safe_float(raw)
    return overrides


def _load_live_policy(
    *,
    active_file: Path,
    runtime_env_file: Path,
) -> PolicyCandidate:
    active_state = load_rotation_state(active_file)
    profile = str(active_state.get("profile") or DEFAULT_PROFILE).strip().lower() or DEFAULT_PROFILE
    risk_mode = str(active_state.get("risk_mode") or "").strip().lower()
    values = dict(active_state.get("profile_values") or {})
    runtime_overrides = _runtime_env_profile_overrides(runtime_env_file)
    values.update(runtime_overrides)
    return PolicyCandidate(
        name="baseline",
        profile=profile,
        risk_mode=risk_mode,
        values=_normalize_policy_values(values),
        source="active_state+runtime_env" if runtime_overrides else "active_state",
        notes=[],
    )


def _build_candidate(
    *,
    baseline: PolicyCandidate,
    name: str,
    overrides: dict[str, Any],
    source: str,
    notes: list[str] | None = None,
) -> PolicyCandidate:
    merged = dict(baseline.values)
    merged.update({str(key): _safe_float(value) for key, value in (overrides or {}).items()})
    return PolicyCandidate(
        name=name,
        profile=baseline.profile,
        risk_mode=baseline.risk_mode,
        values=_normalize_policy_values(merged),
        source=source,
        notes=list(notes or []),
    )


def _bias_snapshot(trade_summary: Any, autotune_report: dict[str, Any]) -> dict[str, float]:
    exit_path = getattr(trade_summary, "exit_path_summary", {}) or {}
    entry_path = getattr(trade_summary, "entry_path_summary", {}) or {}
    no_trade_path = getattr(trade_summary, "no_trade_summary", {}) or {}
    snapshot = {
        "early_exit_bias": _safe_float(autotune_report.get("early_exit_bias"), _safe_float(exit_path.get("early_exit_rate"))),
        "hold_opportunity_rate": _safe_float(
            autotune_report.get("hold_opportunity_rate"),
            _safe_float(exit_path.get("hold_opportunity_rate")),
        ),
        "late_entry_bias": _safe_float(
            autotune_report.get("late_entry_bias"),
            _safe_float(entry_path.get("late_entry_rate")),
        ),
        "late_entry_rate": _safe_float(autotune_report.get("late_entry_rate"), _safe_float(entry_path.get("late_entry_rate"))),
        "missed_entry_bias": _safe_float(
            autotune_report.get("missed_entry_bias"),
            _safe_float(no_trade_path.get("tunable_missed_entry_rate")),
        ),
        "correct_block_bias": _safe_float(
            autotune_report.get("correct_block_bias"),
            _safe_float(no_trade_path.get("tunable_correct_block_rate")),
        ),
    }
    return snapshot


def _aggregate_bias_snapshot(windows: list[ReplayWindow]) -> dict[str, float]:
    totals = {
        "early_exit_bias": 0.0,
        "hold_opportunity_rate": 0.0,
        "late_entry_bias": 0.0,
        "late_entry_rate": 0.0,
        "missed_entry_bias": 0.0,
        "correct_block_bias": 0.0,
    }
    total_weight = 0.0
    for window in windows:
        trade_count = max(1, len(window.trades))
        no_trade_count = max(1, len(window.no_trade_samples))
        weight = math.sqrt(float(trade_count)) + (0.35 * math.sqrt(float(no_trade_count)))
        snapshot = _bias_snapshot(window.trade_summary, {})
        for key in totals:
            totals[key] += weight * snapshot.get(key, 0.0)
        total_weight += weight
    if total_weight <= 0.0:
        return dict(totals)
    return {key: value / total_weight for key, value in totals.items()}


def _seed_candidates(
    *,
    baseline: PolicyCandidate,
    autotune_report: dict[str, Any],
    bias_snapshot: dict[str, float],
) -> list[PolicyCandidate]:
    snapshot = dict(bias_snapshot or {})
    seeds: list[PolicyCandidate] = [baseline]
    autotune_overrides = {}
    raw_autotune_overrides = autotune_report.get("parameter_overrides")
    if isinstance(raw_autotune_overrides, dict):
        for env_name, value in raw_autotune_overrides.items():
            profile_key = ENV_PROFILE_KEY_MAP.get(str(env_name).strip())
            if profile_key:
                autotune_overrides[profile_key] = _safe_float(value)
    if autotune_overrides:
        seeds.append(
            _build_candidate(
                baseline=baseline,
                name="autotune",
                overrides=autotune_overrides,
                source="autotune_report",
                notes=[str(autotune_report.get("reason") or "").strip() or "autotune_report"],
            )
        )

    late_entry_bias = snapshot["late_entry_bias"]
    early_exit_bias = snapshot["early_exit_bias"]
    missed_entry_bias = snapshot["missed_entry_bias"]
    correct_block_bias = snapshot["correct_block_bias"]

    if late_entry_bias > 0.12:
        seeds.append(
            _build_candidate(
                baseline=baseline,
                name="late_entry_guard",
                overrides={
                    "entry_edge_bps": baseline.values.get("entry_edge_bps", 0.0) + (0.6 + (0.8 * late_entry_bias)),
                    "entry_cost_coverage_ratio": baseline.values.get("entry_cost_coverage_ratio", 1.0)
                    + (0.08 * late_entry_bias),
                    "late_entry_block_context_range_pos": baseline.values.get(
                        "late_entry_block_context_range_pos", 1.0
                    )
                    - (0.04 + (0.07 * late_entry_bias)),
                    "late_entry_block_structure_range_pos": baseline.values.get(
                        "late_entry_block_structure_range_pos", 1.0
                    )
                    - (0.03 + (0.05 * late_entry_bias)),
                    "late_entry_block_max_context_drawdown_bps": max(
                        12.0,
                        baseline.values.get("late_entry_block_max_context_drawdown_bps", 24.0)
                        - (4.0 + (8.0 * late_entry_bias)),
                    ),
                },
                source="seed",
                notes=["stricter_top_zone_block"],
            )
        )
    if early_exit_bias > 0.12:
        seeds.append(
            _build_candidate(
                baseline=baseline,
                name="hold_winners",
                overrides={
                    "failed_start_min_bars": baseline.values.get("failed_start_min_bars", 0.0)
                    + (1.0 + math.ceil(early_exit_bias * 2.0)),
                    "failed_start_loss_bps": baseline.values.get("failed_start_loss_bps", 0.0)
                    + (8.0 + (12.0 * early_exit_bias)),
                    "trailing_activation_bps": baseline.values.get("trailing_activation_bps", 0.0)
                    + (4.0 + (8.0 * early_exit_bias)),
                    "trailing_stop_bps": baseline.values.get("trailing_stop_bps", 0.0)
                    + (2.0 + (5.0 * early_exit_bias)),
                    "time_break_even_floor_bars": baseline.values.get("time_break_even_floor_bars", 0.0)
                    + (2.0 + math.ceil(early_exit_bias * 4.0)),
                    "min_exit_profit_bps": baseline.values.get("min_exit_profit_bps", 0.0)
                    + (2.0 + (3.0 * early_exit_bias)),
                    "campaign_hold_enabled": 1.0,
                    "campaign_hold_min_bars": max(
                        baseline.values.get("campaign_hold_min_bars", 0.0),
                        2.0 + math.ceil(early_exit_bias * 4.0),
                    ),
                },
                source="seed",
                notes=["weaker_early_exit_pressure"],
            )
        )
    if missed_entry_bias > max(0.18, correct_block_bias + 0.05):
        seeds.append(
            _build_candidate(
                baseline=baseline,
                name="reclaim_missed_entries",
                overrides={
                    "entry_edge_bps": max(
                        0.4,
                        baseline.values.get("entry_edge_bps", 0.0) - (0.35 + (0.5 * missed_entry_bias)),
                    ),
                    "entry_cost_coverage_ratio": max(
                        0.38,
                        baseline.values.get("entry_cost_coverage_ratio", 1.0) - (0.06 * missed_entry_bias),
                    ),
                    "gate_cost_coverage_ratio": max(
                        0.34,
                        baseline.values.get("gate_cost_coverage_ratio", 1.0) - (0.08 * missed_entry_bias),
                    ),
                    "reentry_min_move_bps": max(
                        12.0,
                        baseline.values.get("reentry_min_move_bps", 0.0) - (8.0 + (10.0 * missed_entry_bias)),
                    ),
                },
                source="seed",
                notes=["looser_gate_for_missed_entries"],
            )
        )
    if late_entry_bias > 0.12 or early_exit_bias > 0.12:
        seeds.append(
            _build_candidate(
                baseline=baseline,
                name="balanced_repair",
                overrides={
                    "entry_edge_bps": baseline.values.get("entry_edge_bps", 0.0) + (0.35 * late_entry_bias),
                    "late_entry_block_context_range_pos": baseline.values.get(
                        "late_entry_block_context_range_pos", 1.0
                    )
                    - (0.02 + (0.05 * late_entry_bias)),
                    "failed_start_min_bars": baseline.values.get("failed_start_min_bars", 0.0)
                    + (1.0 if early_exit_bias > 0.15 else 0.0),
                    "failed_start_loss_bps": baseline.values.get("failed_start_loss_bps", 0.0)
                    + (6.0 * early_exit_bias),
                    "trailing_activation_bps": baseline.values.get("trailing_activation_bps", 0.0)
                    + (3.0 * early_exit_bias),
                    "time_break_even_floor_bars": baseline.values.get("time_break_even_floor_bars", 0.0)
                    + (2.0 * early_exit_bias),
                    "reentry_cooldown_bars_after_weak_exit": baseline.values.get(
                        "reentry_cooldown_bars_after_weak_exit", 0.0
                    )
                    + (1.0 if early_exit_bias > 0.18 else 0.0),
                },
                source="seed",
                notes=["entry_plus_exit_balance"],
            )
        )
    return seeds


def _bars_between(
    earlier: datetime | None,
    later: datetime | None,
    interval_seconds: float,
) -> float:
    if earlier is None or later is None:
        return float("inf")
    delta_seconds = max(0.0, (later - earlier).total_seconds())
    return delta_seconds / max(1.0, float(interval_seconds))


def _required_entry_edge_bps(values: dict[str, float], expected_cost_bps: float) -> float:
    return max(
        values.get("entry_edge_bps", 0.0),
        max(0.0, expected_cost_bps)
        * max(1.0, values.get("entry_cost_roundtrip_multiplier", 2.0))
        * max(0.0, values.get("entry_cost_coverage_ratio", 1.0))
        + max(0.0, values.get("entry_cost_buffer_bps", 0.0)),
    )


def _required_gate_edge_bps(values: dict[str, float], expected_cost_bps: float) -> float:
    return (
        max(0.0, expected_cost_bps)
        * max(1.0, values.get("gate_cost_roundtrip_multiplier", 2.0))
        * max(0.0, values.get("gate_cost_coverage_ratio", 1.0))
    )


def _late_entry_block(features: dict[str, float], values: dict[str, float]) -> bool:
    context_range_pos = max(0.0, min(1.0, _safe_float(features.get("context_range_pos"))))
    structure_range_pos = max(0.0, min(1.0, _safe_float(features.get("structure_range_pos"))))
    trend_return_bps = max(0.0, _safe_float(features.get("trend_return_bps")))
    return_bps = max(0.0, _safe_float(features.get("return_bps")))
    pullback_room_drawdown_bps = max(0.0, -_safe_float(features.get("context_drawdown_bps")))
    late_entry_top_zone = (
        context_range_pos > values.get("late_entry_block_context_range_pos", 1.0)
        or structure_range_pos > values.get("late_entry_block_structure_range_pos", 1.0)
    )
    late_entry_extension = True
    min_trend = values.get("late_entry_block_min_trend_return_bps", 0.0)
    min_ret = values.get("late_entry_block_min_return_bps", 0.0)
    if min_trend > 0.0:
        late_entry_extension = late_entry_extension and trend_return_bps >= min_trend
    if min_ret > 0.0:
        late_entry_extension = late_entry_extension and return_bps >= min_ret
    late_entry_pullback_room_too_small = (
        values.get("late_entry_block_max_context_drawdown_bps", 0.0) > 0.0
        and pullback_room_drawdown_bps <= values.get("late_entry_block_max_context_drawdown_bps", 0.0)
    )
    return bool(late_entry_top_zone and late_entry_extension and late_entry_pullback_room_too_small)


def _entry_block_reason_for_features(
    *,
    features: dict[str, float],
    expected_cost_bps: float,
    entry_price: float,
    state: SymbolState,
    values: dict[str, float],
    ts: datetime | None,
    require_gate_check: bool = False,
) -> str:
    interval_seconds = values.get("md_interval_seconds", 60.0)
    weak_exit_bars = values.get("reentry_cooldown_bars_after_weak_exit", 0.0)
    if weak_exit_bars > 0.0 and state.last_weak_exit_ts is not None:
        if _bars_between(state.last_weak_exit_ts, ts, interval_seconds) < weak_exit_bars:
            return "reentry_cooldown"
    if (
        entry_price > 0.0
        and state.last_exit_price > 0.0
        and values.get("reentry_min_move_bps", 0.0) > 0.0
    ):
        move_bps = abs(entry_price - state.last_exit_price) / state.last_exit_price * 10000.0
        if move_bps < values.get("reentry_min_move_bps", 0.0):
            return "reentry_move_too_small"
    if _late_entry_block(features, values):
        return "late_entry_top_zone"
    atr_bps = max(0.0, _safe_float(features.get("atr_bps")))
    required_atr = max(0.0, expected_cost_bps) * max(0.0, values.get("entry_min_atr_to_cost_ratio", 0.0))
    if required_atr > 0.0 and atr_bps < required_atr:
        return "atr_below_entry_costs"
    edge_bps = _safe_float(features.get("edge_bps_effective"))
    if require_gate_check and edge_bps <= _required_gate_edge_bps(values, expected_cost_bps):
        return "edge_below_costs"
    if edge_bps <= _required_entry_edge_bps(values, expected_cost_bps):
        return "edge_below_entry"
    return ""


def _sample_fee_bps(sample: TradeSample) -> float:
    buy_notional = max(0.0, float(sample.buy_notional or 0.0))
    if buy_notional <= 0.0:
        return 0.0
    return max(0.0, float(sample.fees or 0.0) / buy_notional * 10000.0)


def _available_post_exit_horizons(sample: TradeSample) -> list[int]:
    horizons: list[int] = []
    for horizon in (15, 30, 60, 120):
        if int(round(_post_exit_metric(sample, f"post_exit_bars_{horizon}m", 0.0))) > 0:
            horizons.append(horizon)
    return horizons


def _future_entry_net_bps(sample: TradeSample, horizon_minutes: int) -> float | None:
    entry_price = max(0.0, _safe_float(sample.buy_notional) / max(1e-12, _safe_float(sample.buy_qty)))
    exit_price = _trade_exit_price(sample)
    if entry_price <= 0.0 or exit_price <= 0.0:
        return None
    post_exit_close_bps = _post_exit_metric(sample, f"post_exit_close_{horizon_minutes}m_bps", float("nan"))
    if math.isnan(post_exit_close_bps):
        return None
    future_exit_price = exit_price * (1.0 + (post_exit_close_bps / 10000.0))
    if future_exit_price <= 0.0:
        return None
    gross_bps = ((future_exit_price / entry_price) - 1.0) * 10000.0
    return gross_bps - _sample_fee_bps(sample)


def _trade_hold_strength_delta(candidate: dict[str, float], baseline: dict[str, float], sample: TradeSample) -> float:
    reason = str(sample.exit_reason or "").strip().lower()
    delta = 0.0
    if reason == "failed_start_exit":
        delta += 0.22 * max(0.0, candidate.get("failed_start_min_bars", 0.0) - baseline.get("failed_start_min_bars", 0.0))
        delta += 0.012 * max(0.0, candidate.get("failed_start_loss_bps", 0.0) - baseline.get("failed_start_loss_bps", 0.0))
    if reason == "hard_stop_loss":
        delta += 0.018 * max(
            0.0,
            candidate.get("hard_stop_loss_bps", 0.0) - baseline.get("hard_stop_loss_bps", 0.0),
        )
        delta += 0.09 * max(
            0.0,
            candidate.get("failed_start_min_bars", 0.0) - baseline.get("failed_start_min_bars", 0.0),
        )
        delta += 0.01 * max(
            0.0,
            candidate.get("failed_start_loss_bps", 0.0) - baseline.get("failed_start_loss_bps", 0.0),
        )
    if reason in {"trailing_stop", "trim", "take_profit"}:
        delta += 0.018 * max(0.0, candidate.get("trailing_activation_bps", 0.0) - baseline.get("trailing_activation_bps", 0.0))
        delta += 0.018 * max(0.0, candidate.get("trailing_stop_bps", 0.0) - baseline.get("trailing_stop_bps", 0.0))
    if reason in {"time_break_even_floor", "green_candle_take_exit", "chop_break_even_reclaim"}:
        delta += 0.14 * max(
            0.0,
            candidate.get("time_break_even_floor_bars", 0.0) - baseline.get("time_break_even_floor_bars", 0.0),
        )
        delta += 0.05 * max(0.0, candidate.get("min_exit_profit_bps", 0.0) - baseline.get("min_exit_profit_bps", 0.0))
    if candidate.get("campaign_hold_enabled", 0.0) >= 0.5 and baseline.get("campaign_hold_enabled", 0.0) < 0.5:
        delta += 0.25
    delta += 0.06 * max(
        0.0,
        candidate.get("campaign_hold_min_bars", 0.0) - baseline.get("campaign_hold_min_bars", 0.0),
    )
    return max(0.0, min(1.6, delta))


def _target_exit_horizon(sample: TradeSample, hold_strength_delta: float) -> int | None:
    if hold_strength_delta <= 0.0:
        return None
    available = _available_post_exit_horizons(sample)
    if not available:
        return None
    target = 15
    if _trailing_missed_run_signal(sample) or _shakeout_then_run_signal(sample):
        target = 30
    if hold_strength_delta >= 0.55:
        target = max(target, 30)
    if hold_strength_delta >= 0.95:
        target = max(target, 60)
    for horizon in available:
        if horizon >= target:
            return horizon
    return available[-1]


def _simulate_exit_for_trade(
    sample: TradeSample,
    *,
    candidate_values: dict[str, float],
    baseline_values: dict[str, float],
) -> tuple[float, datetime | None, bool, str]:
    actual_exit_ts = _parse_ts(sample.exit_ts)
    actual_net_pnl = float(sample.net_pnl or 0.0)
    if actual_exit_ts is None:
        return actual_net_pnl, actual_exit_ts, False, "actual"
    if not (
        _early_exit_signal(sample)
        or _failed_start_recovery_signal(sample)
        or _trailing_missed_run_signal(sample)
        or _micro_pop_loss_run_signal(sample)
        or _shakeout_then_run_signal(sample)
    ):
        return actual_net_pnl, actual_exit_ts, False, "actual"
    hold_strength_delta = _trade_hold_strength_delta(candidate_values, baseline_values, sample)
    target_horizon = _target_exit_horizon(sample, hold_strength_delta)
    if target_horizon is None:
        return actual_net_pnl, actual_exit_ts, False, "actual"
    future_net_bps = _future_entry_net_bps(sample, target_horizon)
    if future_net_bps is None:
        return actual_net_pnl, actual_exit_ts, False, "actual"
    future_net_pnl = float(sample.buy_notional or 0.0) * future_net_bps / 10000.0
    min_improvement = max(0.35, abs(actual_net_pnl) * 0.08)
    if future_net_pnl <= actual_net_pnl + min_improvement:
        return actual_net_pnl, actual_exit_ts, False, "actual"
    return future_net_pnl, actual_exit_ts + timedelta(minutes=target_horizon), True, f"hold_{target_horizon}m"


def _synthetic_exit_horizon(
    sample: CounterfactualSample,
    *,
    values: dict[str, float],
    baseline_values: dict[str, float],
) -> int:
    hold_bias = 0.0
    if values.get("campaign_hold_enabled", 0.0) >= 0.5:
        hold_bias += 0.3
    hold_bias += 0.05 * max(0.0, values.get("campaign_hold_min_bars", 0.0) - baseline_values.get("campaign_hold_min_bars", 0.0))
    hold_bias += 0.02 * max(0.0, values.get("trailing_activation_bps", 0.0) - baseline_values.get("trailing_activation_bps", 0.0))
    strategy = str(sample.strategy_primary or "").strip().lower()
    if strategy in {"continuation", "relative_strength"}:
        hold_bias += 0.2
    if hold_bias >= 0.6:
        return 60
    if hold_bias >= 0.25:
        return 30
    return 15


def _counterfactual_close_bps(sample: CounterfactualSample, horizon_minutes: int) -> float | None:
    metrics = sample.post_decision_metrics if isinstance(sample.post_decision_metrics, dict) else {}
    bars = _safe_int(metrics.get(f"post_decision_bars_{horizon_minutes}m"), 0)
    if bars <= 0:
        return None
    return _safe_float(metrics.get(f"post_decision_close_{horizon_minutes}m_bps"), float("nan"))


def _estimate_synthetic_trade_pnl(
    sample: CounterfactualSample,
    *,
    unit_notional: float,
    candidate_values: dict[str, float],
    baseline_values: dict[str, float],
) -> tuple[float, datetime | None, int]:
    decision_ts = _parse_ts(sample.decision_ts)
    if decision_ts is None:
        return 0.0, None, 0
    target_horizon = _synthetic_exit_horizon(sample, values=candidate_values, baseline_values=baseline_values)
    close_bps = _counterfactual_close_bps(sample, target_horizon)
    if close_bps is None:
        for fallback in (15, 30, 60, 120):
            close_bps = _counterfactual_close_bps(sample, fallback)
            if close_bps is not None:
                target_horizon = fallback
                break
    if close_bps is None:
        return 0.0, None, 0
    expected_cost_bps = _safe_float(sample.decision_context.get("expected_cost_bps"))
    est_net_pnl = max(0.0, unit_notional) * ((close_bps - expected_cost_bps) / 10000.0)
    return est_net_pnl, decision_ts + timedelta(minutes=target_horizon), target_horizon


def _default_unit_notional(trades: list[TradeSample]) -> float:
    notionals = sorted(float(sample.buy_notional or 0.0) for sample in trades if float(sample.buy_notional or 0.0) > 0.0)
    if not notionals:
        return DEFAULT_UNIT_NOTIONAL
    return notionals[len(notionals) // 2]


def _baseline_result(
    *,
    baseline: PolicyCandidate,
    trades: list[TradeSample],
) -> CandidateResult:
    net_pnl = sum(float(sample.net_pnl or 0.0) for sample in trades)
    late_entry_losses = sum(1 for sample in trades if _late_entry_signal(sample) and float(sample.net_pnl or 0.0) <= 0.0)
    early_exit_bugs = sum(
        1
        for sample in trades
        if (
            _early_exit_signal(sample)
            or _failed_start_recovery_signal(sample)
            or _trailing_missed_run_signal(sample)
            or _micro_pop_loss_run_signal(sample)
            or _shakeout_then_run_signal(sample)
        )
    )
    weak_exit_reentries = 0
    last_weak_exit_by_symbol: dict[str, datetime] = {}
    for sample in trades:
        symbol = str(sample.symbol or "").strip().upper()
        entry_ts = _parse_ts(sample.entry_ts)
        exit_ts = _parse_ts(sample.exit_ts)
        if symbol and entry_ts is not None and symbol in last_weak_exit_by_symbol:
            if _bars_between(last_weak_exit_by_symbol[symbol], entry_ts, baseline.values.get("md_interval_seconds", 60.0)) < baseline.values.get(
                "reentry_cooldown_bars_after_weak_exit",
                0.0,
            ):
                weak_exit_reentries += 1
        if str(sample.exit_reason or "").strip().lower() in EXIT_WEAK_REASONS and symbol and exit_ts is not None:
            last_weak_exit_by_symbol[symbol] = exit_ts
    score = (net_pnl * 20.0) - (1.4 * late_entry_losses) - (1.2 * early_exit_bugs) - (1.0 * weak_exit_reentries)
    reliability = 1.0 / (1.0 + math.exp(-(score / 8.0))) if score or trades else 0.5
    return CandidateResult(
        name=baseline.name,
        score=score,
        reliability_score=reliability,
        est_net_pnl=net_pnl,
        net_pnl_delta=0.0,
        executed_trade_count=len(trades),
        synthetic_trade_count=0,
        blocked_trade_count=0,
        blocked_negative_trade_count=0,
        blocked_positive_trade_count=0,
        improved_exit_count=0,
        added_trade_count=0,
        added_winner_count=0,
        remaining_late_entry_losses=late_entry_losses,
        remaining_early_exit_bugs=early_exit_bugs,
        remaining_weak_exit_reentries=weak_exit_reentries,
        reason_counts={},
        parameter_overrides={},
        notes=list(baseline.notes),
    )


def _build_replay_windows(
    *,
    baseline: PolicyCandidate,
    all_trades: list[TradeSample],
    all_no_trade_samples: list[CounterfactualSample],
    evaluation_windows: list[float],
) -> list[ReplayWindow]:
    windows: list[ReplayWindow] = []
    for lookback_hours in evaluation_windows:
        trades = _recent_trade_samples(all_trades, lookback_hours)
        no_trade_samples = _recent_counterfactual_samples(all_no_trade_samples, lookback_hours)
        trade_summary = build_trade_summary(
            all_trades,
            lookback_hours=lookback_hours,
            no_trade_samples=all_no_trade_samples,
        )
        windows.append(
            ReplayWindow(
                lookback_hours=float(lookback_hours),
                trades=trades,
                no_trade_samples=no_trade_samples,
                trade_summary=trade_summary,
                unit_notional=_default_unit_notional(trades),
                baseline_result=_baseline_result(baseline=baseline, trades=trades),
            )
        )
    return windows


def _simulate_candidate(
    *,
    candidate: PolicyCandidate,
    baseline: PolicyCandidate,
    trades: list[TradeSample],
    no_trade_samples: list[CounterfactualSample],
    unit_notional: float,
    baseline_result: CandidateResult,
) -> CandidateResult:
    if _profile_signature(candidate.values) == _profile_signature(baseline.values):
        return baseline_result

    states: dict[str, SymbolState] = {}
    est_net_pnl = 0.0
    blocked_trade_count = 0
    blocked_negative_trade_count = 0
    blocked_positive_trade_count = 0
    improved_exit_count = 0
    added_trade_count = 0
    added_winner_count = 0
    executed_trade_count = 0
    reason_counts: dict[str, int] = {}
    remaining_late_entry_losses = 0
    remaining_early_exit_bugs = 0
    remaining_weak_exit_reentries = 0

    merged_events: list[tuple[datetime, str, TradeSample | CounterfactualSample]] = []
    for sample in trades:
        ts = _parse_ts(sample.entry_ts)
        if ts is not None:
            merged_events.append((ts, "trade", sample))
    for sample in no_trade_samples:
        ts = _parse_ts(sample.decision_ts)
        if ts is not None:
            merged_events.append((ts, "counterfactual", sample))
    merged_events.sort(key=lambda item: (item[0], item[1]))

    for event_ts, kind, item in merged_events:
        symbol = str(getattr(item, "symbol", "") or "").strip().upper()
        if not symbol:
            continue
        state = states.setdefault(symbol, SymbolState())
        if state.open_until is not None and event_ts < state.open_until:
            if kind == "trade":
                blocked_trade_count += 1
                sample = item
                if float(sample.net_pnl or 0.0) <= 0.0:
                    blocked_negative_trade_count += 1
                else:
                    blocked_positive_trade_count += 1
                reason_counts["held_position"] = reason_counts.get("held_position", 0) + 1
            continue

        if kind == "trade":
            sample = item
            entry_price = max(0.0, float(sample.buy_notional or 0.0) / max(1e-12, float(sample.buy_qty or 0.0)))
            block_reason = _entry_block_reason_for_features(
                features=sample.features if isinstance(sample.features, dict) else {},
                expected_cost_bps=_safe_float((sample.features or {}).get("expected_cost_bps")),
                entry_price=entry_price,
                state=state,
                values=candidate.values,
                ts=event_ts,
            )
            if block_reason:
                blocked_trade_count += 1
                reason_counts[block_reason] = reason_counts.get(block_reason, 0) + 1
                if float(sample.net_pnl or 0.0) <= 0.0:
                    blocked_negative_trade_count += 1
                else:
                    blocked_positive_trade_count += 1
                continue

            est_trade_pnl, est_exit_ts, improved, exit_mode = _simulate_exit_for_trade(
                sample,
                candidate_values=candidate.values,
                baseline_values=baseline.values,
            )
            est_net_pnl += est_trade_pnl
            executed_trade_count += 1
            if improved:
                improved_exit_count += 1
                reason_counts[exit_mode] = reason_counts.get(exit_mode, 0) + 1
            else:
                if _late_entry_signal(sample) and est_trade_pnl <= 0.0:
                    remaining_late_entry_losses += 1
                if (
                    _early_exit_signal(sample)
                    or _failed_start_recovery_signal(sample)
                    or _trailing_missed_run_signal(sample)
                    or _micro_pop_loss_run_signal(sample)
                    or _shakeout_then_run_signal(sample)
                ):
                    remaining_early_exit_bugs += 1
            if est_exit_ts is not None:
                state.open_until = est_exit_ts
            state.last_exit_price = _trade_exit_price(sample)
            weak_exit = (
                (not improved)
                and str(sample.exit_reason or "").strip().lower() in EXIT_WEAK_REASONS
            )
            if weak_exit and est_exit_ts is not None:
                state.last_weak_exit_ts = est_exit_ts
            elif improved:
                state.last_weak_exit_ts = None
        else:
            sample = item
            if str(sample.block_reason or "").strip().lower() in NON_TUNABLE_BLOCK_REASONS:
                continue
            features = sample.features if isinstance(sample.features, dict) else {}
            expected_cost_bps = _safe_float(sample.decision_context.get("expected_cost_bps"))
            baseline_block = _entry_block_reason_for_features(
                features=features,
                expected_cost_bps=expected_cost_bps,
                entry_price=float(sample.anchor_price or 0.0),
                state=state,
                values=baseline.values,
                ts=event_ts,
                require_gate_check=True,
            )
            if not baseline_block:
                continue
            candidate_block = _entry_block_reason_for_features(
                features=features,
                expected_cost_bps=expected_cost_bps,
                entry_price=float(sample.anchor_price or 0.0),
                state=state,
                values=candidate.values,
                ts=event_ts,
                require_gate_check=True,
            )
            if candidate_block:
                continue
            synth_pnl, synth_exit_ts, horizon = _estimate_synthetic_trade_pnl(
                sample,
                unit_notional=unit_notional,
                candidate_values=candidate.values,
                baseline_values=baseline.values,
            )
            if synth_exit_ts is None:
                continue
            est_net_pnl += synth_pnl
            added_trade_count += 1
            if synth_pnl > 0.0:
                added_winner_count += 1
            reason_counts[f"synthetic_entry_{horizon}m"] = reason_counts.get(f"synthetic_entry_{horizon}m", 0) + 1
            state.open_until = synth_exit_ts
            state.last_exit_price = float(sample.anchor_price or 0.0)
            state.last_weak_exit_ts = None

    for key in ("reentry_cooldown", "reentry_move_too_small"):
        remaining_weak_exit_reentries += max(0, reason_counts.get(key, 0) - blocked_negative_trade_count)

    score = (
        (est_net_pnl * 20.0)
        + (1.35 * blocked_negative_trade_count)
        - (1.15 * blocked_positive_trade_count)
        + (0.9 * improved_exit_count)
        + (0.7 * added_winner_count)
        - (1.4 * remaining_late_entry_losses)
        - (1.2 * remaining_early_exit_bugs)
        - (1.0 * remaining_weak_exit_reentries)
    )
    reliability = 1.0 / (1.0 + math.exp(-(score / 8.0))) if score or (executed_trade_count + added_trade_count) else 0.5
    return CandidateResult(
        name=candidate.name,
        score=score,
        reliability_score=reliability,
        est_net_pnl=est_net_pnl,
        net_pnl_delta=est_net_pnl - baseline_result.est_net_pnl,
        executed_trade_count=executed_trade_count,
        synthetic_trade_count=0,
        blocked_trade_count=blocked_trade_count,
        blocked_negative_trade_count=blocked_negative_trade_count,
        blocked_positive_trade_count=blocked_positive_trade_count,
        improved_exit_count=improved_exit_count,
        added_trade_count=added_trade_count,
        added_winner_count=added_winner_count,
        remaining_late_entry_losses=remaining_late_entry_losses,
        remaining_early_exit_bugs=remaining_early_exit_bugs,
        remaining_weak_exit_reentries=remaining_weak_exit_reentries,
        reason_counts=dict(sorted(reason_counts.items())),
        parameter_overrides=_policy_overrides_against_baseline(baseline.values, candidate.values),
        notes=list(candidate.notes),
    )


def _window_outcome_state(
    *,
    candidate_result: CandidateResult,
    baseline_result: CandidateResult,
) -> str:
    score_delta = float(candidate_result.score) - float(baseline_result.score)
    pnl_delta = float(candidate_result.est_net_pnl) - float(baseline_result.est_net_pnl)
    if score_delta >= 0.5 and pnl_delta >= -0.02:
        return "improving"
    if pnl_delta >= 0.05:
        return "improving"
    if score_delta <= -0.75 or pnl_delta <= -0.05:
        return "worsening"
    return "neutral"


def _acceptance_reasons(
    *,
    improving_window_count: int,
    worsening_window_count: int,
    window_count: int,
    aggregate_score_delta: float,
    aggregate_net_pnl_delta: float,
    aggregate_reliability_delta: float,
    aggregate_late_entry_delta: float,
    aggregate_early_exit_delta: float,
    aggregate_weak_reentry_delta: float,
) -> list[str]:
    reasons: list[str] = []
    required_improving = max(1, math.ceil(float(window_count) * 0.6))
    allowed_worsening = max(0, int(math.floor(float(window_count) * 0.2)))
    if aggregate_score_delta <= 0.5:
        reasons.append("aggregate_score_delta_too_small")
    if aggregate_net_pnl_delta <= 0.03:
        reasons.append("aggregate_net_pnl_delta_too_small")
    if improving_window_count < required_improving:
        reasons.append("not_enough_improving_windows")
    if worsening_window_count > allowed_worsening:
        reasons.append("too_many_worsening_windows")
    if aggregate_reliability_delta < -0.02:
        reasons.append("reliability_regressed")
    if aggregate_late_entry_delta > 0.05:
        reasons.append("late_entry_losses_regressed")
    if aggregate_early_exit_delta > 0.05:
        reasons.append("early_exit_bugs_regressed")
    if aggregate_weak_reentry_delta > 0.05:
        reasons.append("weak_reentry_churn_regressed")
    return reasons


def _aggregate_candidate_results(
    *,
    candidate: PolicyCandidate,
    window_results: list[tuple[ReplayWindow, CandidateResult]],
    primary_lookback_hours: float,
    baseline: PolicyCandidate,
) -> AggregateCandidateResult:
    primary_result = next(
        (
            result
            for window, result in window_results
            if abs(float(window.lookback_hours) - float(primary_lookback_hours)) < 1e-9
        ),
        window_results[0][1],
    )
    score_deltas: list[float] = []
    pnl_deltas: list[float] = []
    reliability_deltas: list[float] = []
    late_entry_deltas: list[float] = []
    early_exit_deltas: list[float] = []
    weak_reentry_deltas: list[float] = []
    improving_window_count = 0
    worsening_window_count = 0
    neutral_window_count = 0
    window_payloads: list[dict[str, Any]] = []

    for window, result in window_results:
        baseline_result = window.baseline_result
        score_delta = float(result.score) - float(baseline_result.score)
        pnl_delta = float(result.est_net_pnl) - float(baseline_result.est_net_pnl)
        reliability_delta = float(result.reliability_score) - float(baseline_result.reliability_score)
        late_entry_delta = float(result.remaining_late_entry_losses) - float(baseline_result.remaining_late_entry_losses)
        early_exit_delta = float(result.remaining_early_exit_bugs) - float(baseline_result.remaining_early_exit_bugs)
        weak_reentry_delta = float(result.remaining_weak_exit_reentries) - float(
            baseline_result.remaining_weak_exit_reentries
        )
        outcome = _window_outcome_state(candidate_result=result, baseline_result=baseline_result)
        if outcome == "improving":
            improving_window_count += 1
        elif outcome == "worsening":
            worsening_window_count += 1
        else:
            neutral_window_count += 1
        score_deltas.append(score_delta)
        pnl_deltas.append(pnl_delta)
        reliability_deltas.append(reliability_delta)
        late_entry_deltas.append(late_entry_delta)
        early_exit_deltas.append(early_exit_delta)
        weak_reentry_deltas.append(weak_reentry_delta)
        window_payloads.append(
            {
                "lookback_hours": _round_float(window.lookback_hours, digits=4),
                "trade_count": len(window.trades),
                "no_trade_count": len(window.no_trade_samples),
                "outcome": outcome,
                "score_delta": _round_float(score_delta),
                "net_pnl_delta": _round_float(pnl_delta),
                "reliability_delta": _round_float(reliability_delta),
                "remaining_late_entry_losses_delta": _round_float(late_entry_delta),
                "remaining_early_exit_bugs_delta": _round_float(early_exit_delta),
                "remaining_weak_exit_reentries_delta": _round_float(weak_reentry_delta),
                "result": result.to_dict(),
            }
        )

    window_count = max(1, len(window_results))
    aggregate_score_delta = sum(score_deltas) / float(window_count)
    aggregate_net_pnl_delta = sum(pnl_deltas) / float(window_count)
    aggregate_reliability_delta = sum(reliability_deltas) / float(window_count)
    aggregate_late_entry_delta = sum(late_entry_deltas) / float(window_count)
    aggregate_early_exit_delta = sum(early_exit_deltas) / float(window_count)
    aggregate_weak_reentry_delta = sum(weak_reentry_deltas) / float(window_count)
    acceptance_reasons = _acceptance_reasons(
        improving_window_count=improving_window_count,
        worsening_window_count=worsening_window_count,
        window_count=window_count,
        aggregate_score_delta=aggregate_score_delta,
        aggregate_net_pnl_delta=aggregate_net_pnl_delta,
        aggregate_reliability_delta=aggregate_reliability_delta,
        aggregate_late_entry_delta=aggregate_late_entry_delta,
        aggregate_early_exit_delta=aggregate_early_exit_delta,
        aggregate_weak_reentry_delta=aggregate_weak_reentry_delta,
    )
    return AggregateCandidateResult(
        name=candidate.name,
        primary_result=primary_result,
        aggregate_score_delta=aggregate_score_delta,
        aggregate_net_pnl_delta=aggregate_net_pnl_delta,
        aggregate_reliability_delta=aggregate_reliability_delta,
        improving_window_count=improving_window_count,
        worsening_window_count=worsening_window_count,
        neutral_window_count=neutral_window_count,
        accepted=not acceptance_reasons,
        acceptance_reasons=acceptance_reasons or ["stable_multi_window_improvement"],
        window_results=window_payloads,
        parameter_overrides={
            PROFILE_KEY_ENV_MAP.get(key, key): value
            for key, value in _policy_overrides_against_baseline(baseline.values, candidate.values).items()
        },
        notes=list(candidate.notes),
    )


def _mutate_candidate(
    candidate: PolicyCandidate,
    *,
    baseline: PolicyCandidate,
    bias_snapshot: dict[str, float],
    rng: random.Random,
    round_index: int,
    mutation_index: int,
) -> PolicyCandidate:
    snapshot = dict(bias_snapshot or {})
    out = dict(candidate.values)
    notes: list[str] = []
    if snapshot["late_entry_bias"] >= 0.12 and rng.random() < 0.8:
        out["late_entry_block_context_range_pos"] -= rng.uniform(0.005, 0.03)
        out["late_entry_block_structure_range_pos"] -= rng.uniform(0.003, 0.02)
        out["late_entry_block_max_context_drawdown_bps"] -= rng.uniform(1.0, 5.0)
        out["entry_edge_bps"] += rng.uniform(0.1, 0.5)
        notes.append("late_entry_tighten")
    if snapshot["early_exit_bias"] >= 0.12 and rng.random() < 0.9:
        out["failed_start_min_bars"] += rng.choice([0.0, 1.0, 1.0, 2.0])
        out["failed_start_loss_bps"] += rng.uniform(2.0, 8.0)
        out["trailing_activation_bps"] += rng.uniform(1.0, 6.0)
        out["trailing_stop_bps"] += rng.uniform(0.5, 3.5)
        out["time_break_even_floor_bars"] += rng.choice([0.0, 1.0, 2.0, 3.0])
        out["campaign_hold_enabled"] = 1.0
        out["campaign_hold_min_bars"] += rng.choice([0.0, 1.0, 2.0])
        notes.append("exit_relax")
    if snapshot["missed_entry_bias"] > max(0.18, snapshot["correct_block_bias"] + 0.05) and rng.random() < 0.7:
        out["entry_edge_bps"] -= rng.uniform(0.05, 0.35)
        out["entry_cost_coverage_ratio"] -= rng.uniform(0.01, 0.06)
        out["gate_cost_coverage_ratio"] -= rng.uniform(0.02, 0.08)
        out["reentry_min_move_bps"] -= rng.uniform(2.0, 12.0)
        notes.append("entry_relax")
    if rng.random() < 0.4:
        out["reentry_cooldown_bars_after_weak_exit"] += rng.choice([-1.0, 0.0, 1.0, 2.0])
        notes.append("reentry_mix")
    mutated = _normalize_policy_values(out)
    for key, value in baseline.values.items():
        if key not in mutated:
            mutated[key] = value
    return PolicyCandidate(
        name=f"{candidate.name}__mut_r{round_index}_{mutation_index}",
        profile=candidate.profile,
        risk_mode=candidate.risk_mode,
        values=_normalize_policy_values(mutated),
        source="search",
        notes=notes,
    )


def _technical_bug_report(
    *,
    log_dir: Path,
    lookback_hours: float,
    trades: list[TradeSample],
) -> dict[str, Any]:
    cutoff = _cutoff_dt(lookback_hours)
    immediate_roundtrip_losses = sum(
        1
        for sample in trades
        if _trade_hold_minutes(sample) <= 6.0 and float(sample.net_pnl or 0.0) < 0.0
    )
    late_entry_losses = sum(
        1 for sample in trades if _late_entry_signal(sample) and float(sample.net_pnl or 0.0) <= 0.0
    )
    early_exit_candidates = sum(
        1
        for sample in trades
        if (
            _early_exit_signal(sample)
            or _failed_start_recovery_signal(sample)
            or _trailing_missed_run_signal(sample)
            or _micro_pop_loss_run_signal(sample)
            or _shakeout_then_run_signal(sample)
        )
    )
    mid_trade_reload_count = 0
    mid_trade_reload_examples: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob(JOURNAL_GLOB)):
        symbol = _symbol_from_journal(path)
        if not symbol:
            continue
        qty = 0.0
        active_alpha_type = ""
        last_open_alpha_type = ""
        reload_recorded_for_open_trade = False
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(raw_line)
            except Exception:
                continue
            event_ts = _parse_ts(event.get("ts"))
            if event_ts is None or event_ts < cutoff:
                continue
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
            if not isinstance(payload, dict):
                payload = {}
            if event_type == "core_reload_applied":
                alpha_type = str(payload.get("alpha_type") or "").strip().lower()
                if (
                    qty > 0.0
                    and not reload_recorded_for_open_trade
                    and alpha_type
                    and alpha_type != active_alpha_type
                ):
                    mid_trade_reload_count += 1
                    reload_recorded_for_open_trade = True
                    if len(mid_trade_reload_examples) < 12:
                        mid_trade_reload_examples.append(
                            {
                                "symbol": symbol,
                                "ts": event_ts.isoformat(),
                                "from_alpha": active_alpha_type or last_open_alpha_type or "unknown",
                                "to_alpha": alpha_type,
                            }
                        )
                if alpha_type:
                    active_alpha_type = alpha_type
                continue
            if event_type != "fill":
                continue
            side = str(payload.get("side") or "").strip().lower()
            fill_qty = max(0.0, _safe_float(payload.get("qty_btc")))
            if side == "buy":
                if qty <= 0.0:
                    last_open_alpha_type = active_alpha_type
                    reload_recorded_for_open_trade = False
                qty += fill_qty
            elif side == "sell":
                qty = max(0.0, qty - fill_qty)
                if qty <= 1e-9:
                    last_open_alpha_type = ""
                    reload_recorded_for_open_trade = False

    findings: list[str] = []
    if mid_trade_reload_count > 0:
        findings.append("mid_trade_strategy_reload")
    if immediate_roundtrip_losses > 0:
        findings.append("immediate_roundtrip_losses")
    if late_entry_losses > 0:
        findings.append("late_entry_losses")
    if early_exit_candidates > 0:
        findings.append("early_exit_opportunities")
    return {
        "mid_trade_reload_count": mid_trade_reload_count,
        "mid_trade_reload_examples": mid_trade_reload_examples,
        "immediate_roundtrip_loss_count": immediate_roundtrip_losses,
        "late_entry_loss_count": late_entry_losses,
        "early_exit_candidate_count": early_exit_candidates,
        "findings": findings,
    }


def run_rotation_replay_lab(
    *,
    log_dir: Path,
    active_file: Path,
    runtime_env_file: Path,
    autotune_report_file: Path,
    lookback_hours: float,
    evaluation_windows: list[float] | None,
    rounds: int,
    beam_width: int,
    mutations_per_round: int,
    seed: int,
) -> dict[str, Any]:
    baseline = _load_live_policy(active_file=active_file, runtime_env_file=runtime_env_file)
    autotune_report = _load_json(autotune_report_file)
    parsed_windows = _parse_evaluation_windows(
        primary_lookback_hours=lookback_hours,
        evaluation_windows=evaluation_windows,
    )
    max_window = max(parsed_windows)
    all_trades = extract_trade_samples(log_dir, mirror_verification="annotate")
    trade_verification = summarize_trade_sample_verification(all_trades)
    all_trades = [sample for sample in all_trades if bool(sample.mirror_verified)]
    all_no_trade = extract_counterfactual_samples(log_dir, lookback_hours=max_window)
    windows = _build_replay_windows(
        baseline=baseline,
        all_trades=all_trades,
        all_no_trade_samples=all_no_trade,
        evaluation_windows=parsed_windows,
    )
    primary_window = windows[0]
    trades = primary_window.trades
    no_trade_samples = primary_window.no_trade_samples
    trade_summary = primary_window.trade_summary
    unit_notional = primary_window.unit_notional
    baseline_result = primary_window.baseline_result
    bias_snapshot = _aggregate_bias_snapshot(windows)
    candidates = _seed_candidates(
        baseline=baseline,
        autotune_report=autotune_report,
        bias_snapshot=bias_snapshot,
    )
    rng = random.Random(seed)
    evaluated: dict[tuple[tuple[str, float], ...], AggregateCandidateResult] = {}
    candidate_map: dict[tuple[tuple[str, float], ...], PolicyCandidate] = {}
    frontier = list(candidates)
    for round_index in range(max(1, int(rounds))):
        for candidate in frontier:
            signature = _profile_signature(candidate.values)
            if signature not in candidate_map:
                candidate_map[signature] = candidate
            if signature in evaluated:
                continue
            window_results = [
                (
                    window,
                    _simulate_candidate(
                        candidate=candidate,
                        baseline=baseline,
                        trades=window.trades,
                        no_trade_samples=window.no_trade_samples,
                        unit_notional=window.unit_notional,
                        baseline_result=window.baseline_result,
                    ),
                )
                for window in windows
            ]
            evaluated[signature] = _aggregate_candidate_results(
                candidate=candidate,
                window_results=window_results,
                primary_lookback_hours=lookback_hours,
                baseline=baseline,
            )
        ranked_signatures = sorted(
            evaluated,
            key=lambda item: (
                -evaluated[item].accepted,
                -evaluated[item].aggregate_score_delta,
                -evaluated[item].aggregate_net_pnl_delta,
                -evaluated[item].improving_window_count,
                evaluated[item].worsening_window_count,
                -evaluated[item].aggregate_reliability_delta,
                candidate_map[item].name,
            ),
        )
        elites = [candidate_map[item] for item in ranked_signatures[: max(1, int(beam_width))]]
        if round_index >= max(1, int(rounds)) - 1:
            break
        next_frontier: list[PolicyCandidate] = list(elites)
        for elite in elites:
            for mutation_index in range(max(1, int(mutations_per_round))):
                next_frontier.append(
                    _mutate_candidate(
                        elite,
                        baseline=baseline,
                        bias_snapshot=bias_snapshot,
                        rng=rng,
                        round_index=round_index + 1,
                        mutation_index=mutation_index,
                    )
                )
        frontier = next_frontier

    ranked = sorted(
        (
            (
                candidate_map[signature],
                evaluated[signature],
            )
            for signature in evaluated
        ),
        key=lambda item: (
            -item[1].accepted,
            -item[1].aggregate_score_delta,
            -item[1].aggregate_net_pnl_delta,
            -item[1].improving_window_count,
            item[1].worsening_window_count,
            -item[1].aggregate_reliability_delta,
            item[0].name,
        ),
    )
    best_candidate, best_aggregate_result = ranked[0]
    technical_bugs = _technical_bug_report(
        log_dir=log_dir,
        lookback_hours=max_window,
        trades=_recent_trade_samples(all_trades, max_window),
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "rotation_counterfactual_replay_lab",
        "lookback_hours": float(lookback_hours),
        "evaluation_windows": [_round_float(hours, digits=4) for hours in parsed_windows],
        "seed": int(seed),
        "search": {
            "rounds": int(rounds),
            "beam_width": int(beam_width),
            "mutations_per_round": int(mutations_per_round),
            "candidate_count": len(evaluated),
        },
        "baseline": baseline_result.to_dict(),
        "champion_decision": {
            "accepted": bool(best_aggregate_result.accepted),
            "candidate": best_candidate.name,
            "reasons": list(best_aggregate_result.acceptance_reasons),
            "aggregate_score_delta": _round_float(best_aggregate_result.aggregate_score_delta),
            "aggregate_net_pnl_delta": _round_float(best_aggregate_result.aggregate_net_pnl_delta),
            "aggregate_reliability_delta": _round_float(best_aggregate_result.aggregate_reliability_delta),
            "improving_window_count": int(best_aggregate_result.improving_window_count),
            "worsening_window_count": int(best_aggregate_result.worsening_window_count),
        },
        "best_candidate": {
            "name": best_candidate.name,
            "profile": best_candidate.profile,
            "risk_mode": best_candidate.risk_mode,
            "source": best_candidate.source,
            "notes": list(best_candidate.notes),
            "parameter_overrides": {
                key: _round_float(value)
                for key, value in sorted(best_aggregate_result.parameter_overrides.items())
            },
            "result": best_aggregate_result.primary_result.to_dict(),
            "aggregate_result": best_aggregate_result.to_dict(),
        },
        "top_candidates": [
            {
                "name": candidate.name,
                "source": candidate.source,
                "notes": list(candidate.notes),
                "result": result.primary_result.to_dict(),
                "aggregate_result": result.to_dict(),
            }
            for candidate, result in ranked[:10]
        ],
        "trade_window": {
            "trade_count": len(trades),
            "no_trade_count": len(no_trade_samples),
            "trade_summary": {
                "net_pnl": _round_float(_safe_float(getattr(trade_summary, "net_pnl", 0.0))),
                "win_rate": _round_float(_safe_float(getattr(trade_summary, "win_rate", 0.0))),
                "exit_reasons": dict(sorted((getattr(trade_summary, "exit_reasons", {}) or {}).items())),
                "exit_path_summary": getattr(trade_summary, "exit_path_summary", {}) or {},
                "entry_path_summary": getattr(trade_summary, "entry_path_summary", {}) or {},
                "no_trade_summary": getattr(trade_summary, "no_trade_summary", {}) or {},
            },
        },
        "evaluation_window_summary": [
            {
                "lookback_hours": _round_float(window.lookback_hours, digits=4),
                "trade_count": len(window.trades),
                "no_trade_count": len(window.no_trade_samples),
                "baseline": window.baseline_result.to_dict(),
            }
            for window in windows
        ],
        "trade_sample_source": trade_verification["mode"],
        "trade_verification": trade_verification,
        "unit_notional_eur": _round_float(unit_notional),
        "technical_bug_report": technical_bugs,
        "autotune_report_snapshot": {
            "enabled": bool(autotune_report.get("enabled")),
            "recommended_profile": autotune_report.get("recommended_profile"),
            "risk_mode_override": autotune_report.get("risk_mode_override"),
            "confidence": autotune_report.get("confidence"),
            "reason": autotune_report.get("reason"),
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay recent rotation journals, surface bug classes, and search for better parameter candidates."
    )
    parser.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    parser.add_argument("--active-file", default=str(DEFAULT_ACTIVE_FILE))
    parser.add_argument("--runtime-env-file", default=str(DEFAULT_RUNTIME_ENV_FILE))
    parser.add_argument("--autotune-report-file", default=str(DEFAULT_AUTOTUNE_REPORT_FILE))
    parser.add_argument("--report-file", default=str(DEFAULT_REPORT_FILE))
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument(
        "--evaluation-windows",
        default="6,24,48,72",
        help="Comma-separated lookback windows used for multi-window replay ranking; primary --lookback-hours is always included.",
    )
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--beam-width", type=int, default=4)
    parser.add_argument("--mutations-per-round", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    evaluation_windows: list[float] = []
    for raw in str(args.evaluation_windows or "").split(","):
        text = raw.strip()
        if not text:
            continue
        evaluation_windows.append(_safe_float(text, float(args.lookback_hours)))

    report = run_rotation_replay_lab(
        log_dir=Path(args.log_dir),
        active_file=Path(args.active_file),
        runtime_env_file=Path(args.runtime_env_file),
        autotune_report_file=Path(args.autotune_report_file),
        lookback_hours=float(args.lookback_hours),
        evaluation_windows=evaluation_windows,
        rounds=max(1, int(args.rounds)),
        beam_width=max(1, int(args.beam_width)),
        mutations_per_round=max(1, int(args.mutations_per_round)),
        seed=int(args.seed),
    )
    report_file = Path(args.report_file)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "report_file": str(report_file), "best_candidate": report["best_candidate"]["name"]}))


if __name__ == "__main__":
    main()
