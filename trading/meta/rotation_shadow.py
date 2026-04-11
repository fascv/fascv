from __future__ import annotations

import json
import math
import os
import random
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading.binance import trade_mirror
from trading.meta.strategy_views import (
    STRATEGY_NAMES,
    annotate_rows_with_strategy_views,
    infer_trade_strategy,
    strategy_score,
)


DEFAULT_MODEL_FEATURES: tuple[str, ...] = (
    "return_bps",
    "trend_return_bps",
    "atr_bps",
    "volume_z",
    "context_return_bps",
    "context_drawdown_bps",
    "context_rebound_bps",
    "context_range_pos",
    "spread_bps",
    "depth",
    "imbalance",
    "edge_bps_effective",
    "expected_cost_bps",
    "structure_confidence",
    "structure_slope_short_bps",
    "structure_slope_medium_bps",
    "structure_drawdown_from_peak_bps",
    "structure_extension_bps",
    "up_structure",
    "down_structure",
    "phase_bottom",
    "phase_range",
    "phase_peak",
    "phase_rollover",
    "phase_downtrend",
)

JOURNAL_GLOB = "journal_live_binance_*_usdc_rotation.jsonl"
POST_EXIT_HORIZONS_MINUTES: tuple[int, ...] = (5, 15, 30, 60, 120)
DEFAULT_MARKET_INTERVAL_SECONDS = 60.0
COUNTERFACTUAL_DECISION_SPACING_SECONDS = 15.0 * 60.0
SESSION_TAGS: tuple[str, ...] = ("asia", "europe", "us", "late")
REGIME_TAGS: tuple[str, ...] = ("bottom", "lift_off", "range", "uptrend", "peak", "rollover", "downtrend", "mixed", "unknown")
NON_TUNABLE_BLOCK_REASONS: tuple[str, ...] = (
    "daily_loss_limit",
    "cooldown",
    "cooldown_loss",
    "rebalance_deadband",
    "gate_block",
    "unknown",
)
STOPLIKE_EXIT_REASONS: tuple[str, ...] = (
    "emergency_exit",
    "stop_loss",
    "hard_stop",
    "hard_stop_loss",
    "cooldown_loss",
)
ENTRY_QUALITY_PROMOTION_THRESHOLD_BPS = 12.0
RECENT_SAMPLE_MIN_WEIGHT = 1.0
RECENT_SAMPLE_MAX_WEIGHT = 1.6
AMBIGUOUS_LOSS_WEIGHT = 0.35
PROTECTIVE_LOSS_WEIGHT = 1.15
MIRROR_VERIFY_OFF = "off"
MIRROR_VERIFY_ANNOTATE = "annotate"
MIRROR_VERIFY_REQUIRE = "require"
MIRROR_MATCH_TOLERANCE_SEC = 180.0
MIRROR_MATCH_QTY_REL_TOL = 0.03
MIRROR_MATCH_PNL_ABS_TOL = 0.20
MIRROR_MATCH_PNL_NOTIONAL_RATIO = 0.005


def _session_tag(ts: datetime | None) -> tuple[str, int, int]:
    if ts is None:
        return "unknown", -1, -1
    hour = int(ts.hour)
    weekday = int(ts.weekday())
    if hour < 7:
        return "asia", hour, weekday
    if hour < 13:
        return "europe", hour, weekday
    if hour < 21:
        return "us", hour, weekday
    return "late", hour, weekday


def _regime_tag_from_vector(vector: dict[str, float] | None) -> str:
    payload = vector if isinstance(vector, dict) else {}
    if _safe_float(payload.get("phase_bottom")) > 0.5:
        return "bottom"
    if _safe_float(payload.get("phase_lift_off")) > 0.5:
        return "lift_off"
    if _safe_float(payload.get("phase_range")) > 0.5:
        return "range"
    if _safe_float(payload.get("phase_peak")) > 0.5:
        return "peak"
    if _safe_float(payload.get("phase_rollover")) > 0.5:
        return "rollover"
    if _safe_float(payload.get("phase_downtrend")) > 0.5:
        return "downtrend"
    up = _safe_float(payload.get("up_structure")) > 0.5
    down = _safe_float(payload.get("down_structure")) > 0.5
    if up and not down:
        return "uptrend"
    if down and not up:
        return "downtrend"
    if up and down:
        return "mixed"
    return "unknown"


@dataclass
class TradeSample:
    symbol: str
    entry_ts: str
    exit_ts: str
    buy_qty: float
    sell_qty: float
    buy_notional: float
    sell_notional: float
    fees: float
    dust_notional: float
    net_pnl: float
    profitable: bool
    exit_reason: str | None
    strategy_at_entry: str
    strategy_scores: dict[str, float]
    features: dict[str, float]
    inferred_strategy_at_entry: str = ""
    strategy_at_entry_source: str = ""
    alpha_type_at_entry: str = ""
    alpha_model_class_at_entry: str = ""
    post_exit_metrics: dict[str, float] = field(default_factory=dict)
    regime_tag: str = ""
    session_tag: str = ""
    entry_hour_utc: int = -1
    entry_weekday: int = -1
    entry_quality_score_bps: float = 0.0
    entry_quality_reason: str = ""
    learning_weight: float = 1.0
    mirror_verified: bool = False
    mirror_match_mode: str = ""
    mirror_net_pnl: float = 0.0
    mirror_pnl_delta: float = 0.0
    entry_fill_count: int = 1
    entry_fill_span_minutes: float = 0.0
    technical_flags: list[str] = field(default_factory=list)


@dataclass
class CounterfactualSample:
    symbol: str
    decision_ts: str
    anchor_price: float
    block_reason: str
    gate_reason: str
    risk_reason: str
    strategy_primary: str
    features: dict[str, float]
    decision_context: dict[str, Any]
    post_decision_metrics: dict[str, float] = field(default_factory=dict)
    regime_tag: str = ""
    session_tag: str = ""
    decision_hour_utc: int = -1
    decision_weekday: int = -1


@dataclass
class TradeSummary:
    trade_count: int
    net_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    last_exit_ts: str | None
    exit_reasons: dict[str, int]
    strategy_breakdown: dict[str, dict[str, Any]]
    symbol_breakdown: dict[str, dict[str, Any]]
    exit_path_summary: dict[str, Any] = field(default_factory=dict)
    entry_path_summary: dict[str, Any] = field(default_factory=dict)
    no_trade_summary: dict[str, Any] = field(default_factory=dict)
    regime_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class LogisticTradeModel:
    feature_names: list[str]
    means: dict[str, float]
    scales: dict[str, float]
    weights: dict[str, float]
    bias: float
    calibration_bias: float
    sample_count: int
    positive_rate: float
    train_metrics: dict[str, float]
    test_metrics: dict[str, float]
    training_diagnostics: dict[str, float]
    trained_at: str
    model_version: str = "rotation-logreg-v2"

    def predict_score(self, features: dict[str, float]) -> float:
        score = float(self.bias)
        for name in self.feature_names:
            value = float(features.get(name, 0.0) or 0.0)
            mean = float(self.means.get(name, 0.0))
            scale = float(self.scales.get(name, 1.0) or 1.0)
            weight = float(self.weights.get(name, 0.0))
            score += ((value - mean) / scale) * weight
        return score + float(self.calibration_bias)

    def predict_proba(self, features: dict[str, float]) -> float:
        score = self.predict_score(features)
        if score >= 0.0:
            z = math.exp(-score)
            return 1.0 / (1.0 + z)
        z = math.exp(score)
        return z / (1.0 + z)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LogisticTradeModel":
        return cls(
            feature_names=[str(item) for item in payload.get("feature_names") or []],
            means={str(k): float(v) for k, v in (payload.get("means") or {}).items()},
            scales={str(k): max(1e-9, float(v)) for k, v in (payload.get("scales") or {}).items()},
            weights={str(k): float(v) for k, v in (payload.get("weights") or {}).items()},
            bias=float(payload.get("bias") or 0.0),
            calibration_bias=float(payload.get("calibration_bias") or 0.0),
            sample_count=int(payload.get("sample_count") or 0),
            positive_rate=float(payload.get("positive_rate") or 0.0),
            train_metrics=dict(payload.get("train_metrics") or {}),
            test_metrics=dict(payload.get("test_metrics") or {}),
            training_diagnostics=dict(payload.get("training_diagnostics") or {}),
            trained_at=str(payload.get("trained_at") or ""),
            model_version=str(payload.get("model_version") or "rotation-logreg-v2"),
        )


@dataclass
class ShadowCandidate:
    symbol: str
    ts: str
    age_sec: float
    selected: bool
    watch: bool
    current_profile: str
    strategy_primary: str
    gate_reason: str
    open_notional: float
    has_position: bool
    p_profit: float
    feature_vector: dict[str, float]
    decision_context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sample_entry_phase(sample: TradeSample) -> str:
    features = sample.features if isinstance(sample.features, dict) else {}
    phase_scores = {
        "bottom": _safe_float(features.get("phase_bottom")),
        "range": _safe_float(features.get("phase_range")),
        "peak": _safe_float(features.get("phase_peak")),
        "rollover": _safe_float(features.get("phase_rollover")),
        "downtrend": _safe_float(features.get("phase_downtrend")),
    }
    best_phase = max(phase_scores.items(), key=lambda item: item[1], default=("unknown", 0.0))
    if best_phase[1] > 0.0:
        return best_phase[0]
    if _safe_float(features.get("up_structure")) > 0.0:
        return "uptrend"
    if _safe_float(features.get("down_structure")) > 0.0:
        return "downtrend"
    return "unknown"


def _recent_trade_diagnosis(sample: TradeSample) -> tuple[str, list[str]]:
    flags: list[str] = []
    if _late_entry_signal(sample):
        flags.append("late_entry_top_chase")
    if _poor_entry_signal(sample):
        flags.append("poor_entry")
    if _micro_pop_loss_run_signal(sample):
        flags.append("micro_pop_loss_run")
    if _shakeout_then_run_signal(sample):
        flags.append("shakeout_then_run")
    if _failed_start_recovery_signal(sample):
        flags.append("failed_start_recovery")
    if _trailing_missed_run_signal(sample):
        flags.append("trailing_missed_run")
    if _early_exit_signal(sample):
        flags.append("early_exit")
    if _protective_exit_signal(sample):
        flags.append("protective_exit")

    if any(
        flag in flags
        for flag in {
            "micro_pop_loss_run",
            "shakeout_then_run",
            "failed_start_recovery",
            "trailing_missed_run",
        }
    ):
        if "late_entry_top_chase" in flags:
            return "entry_and_exit_weak", flags
        return "likely_exit_too_early", flags
    if "late_entry_top_chase" in flags:
        return "likely_late_entry_top_chase", flags
    if "early_exit" in flags and "protective_exit" not in flags:
        return "possible_exit_too_early", flags
    if "protective_exit" in flags:
        return "likely_protective_exit", flags
    if float(sample.net_pnl or 0.0) < 0.0:
        return "likely_bad_trade_or_market", flags
    return "likely_reasonable_exit", flags


def build_recent_trade_examples(
    samples: list[TradeSample],
    *,
    lookback_hours: float = 6.0,
    limit: int = 6,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(lookback_hours)))
    recent_items = [
        sample
        for sample in samples
        if (_parse_ts(sample.exit_ts) or cutoff) >= cutoff
    ]
    recent_items.sort(key=lambda item: _parse_ts(item.exit_ts) or cutoff, reverse=True)

    examples: list[dict[str, Any]] = []
    for sample in recent_items[: max(1, int(limit))]:
        diagnosis_hint, flags = _recent_trade_diagnosis(sample)
        buy_qty = max(0.0, float(sample.buy_qty or 0.0))
        sell_qty = max(0.0, float(sample.sell_qty or 0.0))
        entry_price = (float(sample.buy_notional or 0.0) / buy_qty) if buy_qty > 0.0 else 0.0
        exit_price = (float(sample.sell_notional or 0.0) / sell_qty) if sell_qty > 0.0 else 0.0
        metrics = sample.post_exit_metrics if isinstance(sample.post_exit_metrics, dict) else {}
        best_post_exit_mfe = max(
            _safe_float(metrics.get("post_exit_mfe_15m_bps")),
            _safe_float(metrics.get("post_exit_mfe_30m_bps")),
            _safe_float(metrics.get("post_exit_mfe_60m_bps")),
            _safe_float(metrics.get("post_exit_mfe_120m_bps")),
        )
        best_post_exit_close = max(
            _safe_float(metrics.get("post_exit_close_15m_bps")),
            _safe_float(metrics.get("post_exit_close_30m_bps")),
            _safe_float(metrics.get("post_exit_close_60m_bps")),
            _safe_float(metrics.get("post_exit_close_120m_bps")),
        )
        features = sample.features if isinstance(sample.features, dict) else {}
        examples.append(
            {
                "symbol": str(sample.symbol or "").upper(),
                "entry_ts": sample.entry_ts,
                "exit_ts": sample.exit_ts,
                "strategy_at_entry": str(sample.strategy_at_entry or "").strip().lower() or "unknown",
                "strategy_at_entry_source": str(sample.strategy_at_entry_source or "").strip().lower() or "unknown",
                "inferred_strategy_at_entry": str(sample.inferred_strategy_at_entry or "").strip().lower() or "unknown",
                "alpha_type_at_entry": str(sample.alpha_type_at_entry or "").strip().lower() or "unknown",
                "alpha_model_class_at_entry": str(sample.alpha_model_class_at_entry or "").strip() or "unknown",
                "entry_phase": _sample_entry_phase(sample),
                "regime_tag": str(sample.regime_tag or "").strip().lower() or _sample_entry_phase(sample),
                "session_tag": str(sample.session_tag or "").strip().lower() or "unknown",
                "exit_reason": str(sample.exit_reason or "").strip().lower() or "unknown",
                "net_pnl": round(float(sample.net_pnl or 0.0), 8),
                "net_exit_bps": round(_trade_net_exit_bps(sample), 3),
                "gross_exit_bps": round(_trade_gross_exit_bps(sample), 3),
                "hold_minutes": round(_trade_hold_minutes(sample), 2),
                "entry_price": round(entry_price, 8),
                "exit_price": round(exit_price, 8),
                "entry_snapshot": {
                    "return_bps": round(_safe_float(features.get("return_bps")), 3),
                    "trend_return_bps": round(_safe_float(features.get("trend_return_bps")), 3),
                    "spread_bps": round(_safe_float(features.get("spread_bps")), 3),
                    "expected_cost_bps": round(_safe_float(features.get("expected_cost_bps")), 3),
                    "context_range_pos": round(_safe_float(features.get("context_range_pos")), 4),
                    "context_drawdown_from_peak_bps": round(
                        max(0.0, -_safe_float(features.get("context_drawdown_bps"))),
                        3,
                    ),
                    "structure_range_pos": round(_safe_float(features.get("structure_range_pos")), 4),
                    "structure_drawdown_from_peak_bps": round(
                        _safe_float(features.get("structure_drawdown_from_peak_bps")),
                        3,
                    ),
                    "structure_extension_bps": round(
                        _safe_float(features.get("structure_extension_bps")),
                        3,
                    ),
                    "up_structure": bool(_safe_float(features.get("up_structure")) > 0.0),
                    "down_structure": bool(_safe_float(features.get("down_structure")) > 0.0),
                    "entry_quality_score_bps": round(float(sample.entry_quality_score_bps or 0.0), 3),
                    "entry_quality_reason": str(sample.entry_quality_reason or "").strip().lower(),
                },
                "post_exit": {
                    "best_mfe_bps": round(best_post_exit_mfe, 3),
                    "best_close_bps": round(best_post_exit_close, 3),
                    "mfe_15m_bps": round(_safe_float(metrics.get("post_exit_mfe_15m_bps")), 3),
                    "close_15m_bps": round(_safe_float(metrics.get("post_exit_close_15m_bps")), 3),
                    "mfe_30m_bps": round(_safe_float(metrics.get("post_exit_mfe_30m_bps")), 3),
                    "close_30m_bps": round(_safe_float(metrics.get("post_exit_close_30m_bps")), 3),
                    "mfe_60m_bps": round(_safe_float(metrics.get("post_exit_mfe_60m_bps")), 3),
                    "close_60m_bps": round(_safe_float(metrics.get("post_exit_close_60m_bps")), 3),
                    "mfe_120m_bps": round(_safe_float(metrics.get("post_exit_mfe_120m_bps")), 3),
                    "close_120m_bps": round(_safe_float(metrics.get("post_exit_close_120m_bps")), 3),
                    "mae_15m_bps": round(_safe_float(metrics.get("post_exit_mae_15m_bps")), 3),
                    "mae_30m_bps": round(_safe_float(metrics.get("post_exit_mae_30m_bps")), 3),
                },
                "flags": flags,
                "diagnosis_hint": diagnosis_hint,
            }
        )
    return examples


def _counterfactual_post_metric(sample: CounterfactualSample, key: str, default: float = 0.0) -> float:
    metrics = sample.post_decision_metrics if isinstance(sample.post_decision_metrics, dict) else {}
    return _safe_float(metrics.get(key), default)


def _missed_entry_signal(sample: CounterfactualSample) -> bool:
    bars15 = int(round(_counterfactual_post_metric(sample, "post_decision_bars_15m", 0.0)))
    bars30 = int(round(_counterfactual_post_metric(sample, "post_decision_bars_30m", 0.0)))
    bars60 = int(round(_counterfactual_post_metric(sample, "post_decision_bars_60m", 0.0)))
    mfe15 = _counterfactual_post_metric(sample, "post_decision_mfe_15m_bps", 0.0)
    mae15 = _counterfactual_post_metric(sample, "post_decision_mae_15m_bps", 0.0)
    close15 = _counterfactual_post_metric(sample, "post_decision_close_15m_bps", 0.0)
    mfe30 = _counterfactual_post_metric(sample, "post_decision_mfe_30m_bps", 0.0)
    close30 = _counterfactual_post_metric(sample, "post_decision_close_30m_bps", 0.0)
    mfe60 = _counterfactual_post_metric(sample, "post_decision_mfe_60m_bps", 0.0)
    close60 = _counterfactual_post_metric(sample, "post_decision_close_60m_bps", 0.0)
    if bars15 > 0 and mfe15 >= 16.0 and close15 >= 6.0 and mae15 > -18.0:
        return True
    if bars30 > 0 and mfe30 >= 24.0 and close30 >= 10.0:
        return True
    return bars60 > 0 and mfe60 >= 34.0 and close60 >= 14.0


def _correct_block_signal(sample: CounterfactualSample) -> bool:
    bars15 = int(round(_counterfactual_post_metric(sample, "post_decision_bars_15m", 0.0)))
    bars30 = int(round(_counterfactual_post_metric(sample, "post_decision_bars_30m", 0.0)))
    mfe15 = _counterfactual_post_metric(sample, "post_decision_mfe_15m_bps", 0.0)
    mae15 = _counterfactual_post_metric(sample, "post_decision_mae_15m_bps", 0.0)
    close15 = _counterfactual_post_metric(sample, "post_decision_close_15m_bps", 0.0)
    mfe30 = _counterfactual_post_metric(sample, "post_decision_mfe_30m_bps", 0.0)
    mae30 = _counterfactual_post_metric(sample, "post_decision_mae_30m_bps", 0.0)
    close30 = _counterfactual_post_metric(sample, "post_decision_close_30m_bps", 0.0)
    if bars15 > 0 and close15 <= -5.0 and mae15 <= -12.0 and mfe15 < 14.0:
        return True
    return bars30 > 0 and close30 <= -8.0 and mae30 <= -18.0 and mfe30 < 18.0


def _recent_no_trade_diagnosis(sample: CounterfactualSample) -> tuple[str, list[str]]:
    flags: list[str] = []
    if _missed_entry_signal(sample):
        flags.append("missed_entry")
    if _correct_block_signal(sample):
        flags.append("correct_block")
    if "daily_loss_limit" in str(sample.block_reason or "").strip().lower():
        flags.append("risk_block")
    if "edge_below_costs" in str(sample.block_reason or "").strip().lower():
        flags.append("cost_gate")
    if "spread" in str(sample.block_reason or "").strip().lower():
        flags.append("spread_gate")
    if "structure" in str(sample.block_reason or "").strip().lower():
        flags.append("structure_gate")
    if "missed_entry" in flags:
        return "likely_gate_too_strict", flags
    if "correct_block" in flags:
        return "likely_correct_block", flags
    return "unclear_block_quality", flags


def build_recent_no_trade_examples(
    samples: list[CounterfactualSample],
    *,
    lookback_hours: float = 6.0,
    limit: int = 4,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(lookback_hours)))
    recent_items = [
        sample
        for sample in samples
        if (_parse_ts(sample.decision_ts) or cutoff) >= cutoff
    ]
    recent_items.sort(key=lambda item: _parse_ts(item.decision_ts) or cutoff, reverse=True)

    examples: list[dict[str, Any]] = []
    for sample in recent_items[: max(1, int(limit))]:
        diagnosis_hint, flags = _recent_no_trade_diagnosis(sample)
        metrics = sample.post_decision_metrics if isinstance(sample.post_decision_metrics, dict) else {}
        examples.append(
            {
                "symbol": str(sample.symbol or "").upper(),
                "decision_ts": sample.decision_ts,
                "strategy_primary": str(sample.strategy_primary or "").strip().lower() or "unknown",
                "block_reason": str(sample.block_reason or "").strip().lower() or "unknown",
                "gate_reason": str(sample.gate_reason or "").strip().lower() or "unknown",
                "risk_reason": str(sample.risk_reason or "").strip().lower() or "unknown",
                "regime_tag": str(sample.regime_tag or "").strip().lower() or "unknown",
                "session_tag": str(sample.session_tag or "").strip().lower() or "unknown",
                "anchor_price": round(float(sample.anchor_price or 0.0), 8),
                "decision_snapshot": {
                    "return_bps": round(_safe_float(sample.features.get("return_bps")), 3),
                    "trend_return_bps": round(_safe_float(sample.features.get("trend_return_bps")), 3),
                    "spread_bps": round(_safe_float(sample.features.get("spread_bps")), 3),
                    "expected_cost_bps": round(_safe_float(sample.features.get("expected_cost_bps")), 3),
                    "edge_bps_effective": round(_safe_float(sample.features.get("edge_bps_effective")), 3),
                    "context_range_pos": round(_safe_float(sample.features.get("context_range_pos")), 4),
                },
                "post_decision": {
                    "mfe_15m_bps": round(_safe_float(metrics.get("post_decision_mfe_15m_bps")), 3),
                    "close_15m_bps": round(_safe_float(metrics.get("post_decision_close_15m_bps")), 3),
                    "mfe_30m_bps": round(_safe_float(metrics.get("post_decision_mfe_30m_bps")), 3),
                    "close_30m_bps": round(_safe_float(metrics.get("post_decision_close_30m_bps")), 3),
                    "mfe_60m_bps": round(_safe_float(metrics.get("post_decision_mfe_60m_bps")), 3),
                    "close_60m_bps": round(_safe_float(metrics.get("post_decision_close_60m_bps")), 3),
                    "mfe_120m_bps": round(_safe_float(metrics.get("post_decision_mfe_120m_bps")), 3),
                    "close_120m_bps": round(_safe_float(metrics.get("post_decision_close_120m_bps")), 3),
                    "mae_15m_bps": round(_safe_float(metrics.get("post_decision_mae_15m_bps")), 3),
                    "mae_30m_bps": round(_safe_float(metrics.get("post_decision_mae_30m_bps")), 3),
                },
                "flags": flags,
                "diagnosis_hint": diagnosis_hint,
            }
        )
    return examples


def _parse_ts(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_bool(value: object) -> float:
    return 1.0 if bool(value) else 0.0


def _symbol_from_journal(path: Path) -> str:
    stem = path.stem
    prefix = "journal_live_binance_"
    if not stem.startswith(prefix) or not stem.endswith("_usdc_rotation"):
        return ""
    return stem[len(prefix) : -len("_usdc_rotation")].upper()


def _normalize_mirror_verification_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {MIRROR_VERIFY_OFF, MIRROR_VERIFY_ANNOTATE, MIRROR_VERIFY_REQUIRE}:
        return text
    return MIRROR_VERIFY_OFF


def _mirror_symbol_key(value: object) -> str:
    return trade_mirror.normalize_usdc_symbol(value).removesuffix("USDC")


def _relative_gap(left: float, right: float) -> float:
    scale = max(abs(float(left)), abs(float(right)), 1e-9)
    return abs(float(left) - float(right)) / scale


def _collect_sample_windows(samples: list[TradeSample]) -> dict[str, tuple[int, int]]:
    windows: dict[str, tuple[int, int]] = {}
    buffer_ms = int(30.0 * 60.0 * 1000.0)
    for sample in samples:
        symbol_key = _mirror_symbol_key(sample.symbol)
        if not symbol_key:
            continue
        entry_dt = _parse_ts(sample.entry_ts)
        exit_dt = _parse_ts(sample.exit_ts)
        if entry_dt is None or exit_dt is None:
            continue
        start_ms = max(0, int(entry_dt.timestamp() * 1000.0) - buffer_ms)
        end_ms = int(exit_dt.timestamp() * 1000.0) + buffer_ms
        existing = windows.get(symbol_key)
        if existing is None:
            windows[symbol_key] = (start_ms, end_ms)
        else:
            windows[symbol_key] = (min(existing[0], start_ms), max(existing[1], end_ms))
    return windows


def _load_mirror_bundle_index(
    samples: list[TradeSample],
    *,
    mirror_dir: Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    windows = _collect_sample_windows(samples)
    if not windows:
        return {}
    index: dict[str, list[dict[str, Any]]] = {}
    for symbol_key, (start_ms, end_ms) in windows.items():
        rows = trade_mirror.load_mirror_rows(symbol_key, mirror_dir)
        if not rows:
            continue
        filtered_rows = [
            row
            for row in rows
            if start_ms <= int(row.get("timeMs") or 0) <= end_ms
        ]
        if not filtered_rows:
            continue
        aggregated = trade_mirror.aggregate_order_fills(filtered_rows)
        report = trade_mirror.build_report(
            from_iso=trade_mirror.iso_from_ms(start_ms),
            to_iso=trade_mirror.iso_from_ms(end_ms),
            normalized_trades=aggregated,
        )
        bundles = [item for item in report.get("bundles") or [] if isinstance(item, dict)]
        if bundles:
            index[symbol_key] = bundles
    return index


def _best_mirror_bundle_match(
    sample: TradeSample,
    bundles: list[dict[str, Any]],
    used_indices: set[int],
) -> tuple[int, dict[str, Any], str] | None:
    entry_ts = _parse_ts(sample.entry_ts)
    exit_ts = _parse_ts(sample.exit_ts)
    if entry_ts is None or exit_ts is None:
        return None
    sample_qty = max(_safe_float(sample.buy_qty), _safe_float(sample.sell_qty))
    if sample_qty <= 0.0:
        return None
    best_candidate: tuple[tuple[float, float, float, float], int, dict[str, Any], str] | None = None
    pnl_tolerance = max(
        MIRROR_MATCH_PNL_ABS_TOL,
        abs(_safe_float(sample.buy_notional)) * MIRROR_MATCH_PNL_NOTIONAL_RATIO,
    )

    for idx, bundle in enumerate(bundles):
        if idx in used_indices:
            continue
        try:
            bundle_entry = trade_mirror.parse_iso_utc(str(bundle.get("buyTime") or ""))
            bundle_exit = trade_mirror.parse_iso_utc(str(bundle.get("sellTime") or ""))
        except Exception:
            continue
        entry_delta = abs((bundle_entry - entry_ts).total_seconds())
        exit_delta = abs((bundle_exit - exit_ts).total_seconds())
        if entry_delta > MIRROR_MATCH_TOLERANCE_SEC or exit_delta > MIRROR_MATCH_TOLERANCE_SEC:
            continue
        bundle_qty = _safe_float(bundle.get("quantity"))
        qty_gap = _relative_gap(bundle_qty, sample_qty)
        if qty_gap > MIRROR_MATCH_QTY_REL_TOL:
            continue
        bundle_pnl = _safe_float(bundle.get("proceedsUsdc"))
        pnl_gap = abs(bundle_pnl - _safe_float(sample.net_pnl))
        match_mode = "time_qty_pnl" if pnl_gap <= pnl_tolerance else "time_qty_only"
        score = (entry_delta + exit_delta, qty_gap, pnl_gap, abs(bundle_qty - sample_qty))
        if best_candidate is None or score < best_candidate[0]:
            best_candidate = (score, idx, bundle, match_mode)

    if best_candidate is None:
        return None
    return best_candidate[1], best_candidate[2], best_candidate[3]


def annotate_trade_samples_with_mirror(
    samples: list[TradeSample],
    *,
    mirror_dir: Path | None = None,
) -> list[TradeSample]:
    bundle_index = _load_mirror_bundle_index(samples, mirror_dir=mirror_dir)
    if not bundle_index:
        return samples
    grouped_samples: dict[str, list[TradeSample]] = {}
    for sample in samples:
        grouped_samples.setdefault(_mirror_symbol_key(sample.symbol), []).append(sample)
    for symbol_key, symbol_samples in grouped_samples.items():
        bundles = bundle_index.get(symbol_key) or []
        if not bundles:
            continue
        used_indices: set[int] = set()
        ordered_samples = sorted(symbol_samples, key=lambda item: (item.exit_ts, item.entry_ts))
        for sample in ordered_samples:
            matched = _best_mirror_bundle_match(sample, bundles, used_indices)
            if matched is None:
                continue
            bundle_index_value, bundle, match_mode = matched
            used_indices.add(bundle_index_value)
            sample.mirror_match_mode = match_mode
            sample.mirror_net_pnl = _safe_float(bundle.get("proceedsUsdc"))
            sample.mirror_pnl_delta = sample.net_pnl - sample.mirror_net_pnl
            sample.mirror_verified = match_mode == "time_qty_pnl"
    return samples


def summarize_trade_sample_verification(samples: list[TradeSample]) -> dict[str, Any]:
    total_count = len(samples)
    verified_count = sum(1 for sample in samples if bool(sample.mirror_verified))
    partial_match_count = sum(1 for sample in samples if str(sample.mirror_match_mode) == "time_qty_only")
    unmatched_count = max(0, total_count - verified_count - partial_match_count)
    return {
        "mode": "journal_plus_binance_mirror",
        "total_trade_count": total_count,
        "verified_trade_count": verified_count,
        "partial_match_trade_count": partial_match_count,
        "unmatched_trade_count": unmatched_count,
    }


def tail_json_lines(path: Path, max_lines: int = 400) -> list[dict[str, Any]]:
    buf: deque[str] = deque(maxlen=max(1, int(max_lines)))
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                buf.append(line)
    out: list[dict[str, Any]] = []
    for line in buf:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


_STRATEGY_NAME_ALIASES: dict[str, str] = {
    "trend": "relative_strength",
    "momentum": "relative_strength",
    "mean_reversion": "rebound",
    "reversion": "rebound",
    "swing": "rebound",
    "continuation": "continuation",
    "breakout": "breakout",
}

_MODEL_CLASS_STRATEGY_MAP: dict[str, str] = {
    "breakoutalpha": "breakout",
    "continuationalpha": "continuation",
    "momentumalpha": "relative_strength",
    "meanreversionalpha": "rebound",
    "swingalpha": "rebound",
}


def _normalize_strategy_label(raw: object) -> str:
    name = str(raw or "").strip().lower()
    if not name:
        return ""
    if name in STRATEGY_NAMES:
        return name
    return _STRATEGY_NAME_ALIASES.get(name, "")


def _extract_strategy_context_from_decision(event: dict[str, Any]) -> dict[str, str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
    if not isinstance(payload, dict):
        return {}
    alpha = payload.get("alpha") if isinstance(payload.get("alpha"), dict) else {}
    meta = alpha.get("meta") if isinstance(alpha.get("meta"), dict) else {}
    alpha_type = str(payload.get("alpha_type") or alpha.get("type") or "").strip().lower()
    model_class = str(alpha.get("model_class") or "").strip()

    for source, raw in (
        ("alpha_active_strategy", payload.get("alpha_active_strategy")),
        ("alpha.active_strategy", alpha.get("active_strategy")),
        ("alpha.meta.active_strategy", meta.get("active_strategy")),
        ("alpha_type", alpha_type),
    ):
        strategy = _normalize_strategy_label(raw)
        if strategy:
            return {
                "strategy_at_entry": strategy,
                "strategy_source": source,
                "alpha_type_at_entry": alpha_type,
                "alpha_model_class_at_entry": model_class,
            }

    model_class_strategy = _MODEL_CLASS_STRATEGY_MAP.get(model_class.lower(), "")
    if model_class_strategy:
        return {
            "strategy_at_entry": model_class_strategy,
            "strategy_source": "alpha.model_class",
            "alpha_type_at_entry": alpha_type,
            "alpha_model_class_at_entry": model_class,
        }
    return {
        "alpha_type_at_entry": alpha_type,
        "alpha_model_class_at_entry": model_class,
    }


def _extract_vector_from_decision(event: dict[str, Any]) -> dict[str, float]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
    if not isinstance(payload, dict):
        return {}
    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    alpha = payload.get("alpha") if isinstance(payload.get("alpha"), dict) else {}
    cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    meta = alpha.get("meta") if isinstance(alpha.get("meta"), dict) else {}
    structure = meta.get("structure") if isinstance(meta.get("structure"), dict) else {}
    phase = str(structure.get("phase", "") or "").strip().lower()
    swing_state = str(meta.get("swing_state", "") or "").strip().lower()
    continuation_state = str(meta.get("continuation_state", "") or "").strip().lower()
    breakout_state = str(meta.get("breakout_state", "") or "").strip().lower()

    vector = {
        "return_bps": _safe_float(features.get("return_bps")),
        "trend_return_bps": _safe_float(features.get("trend_return_bps")),
        "atr_bps": _safe_float(features.get("atr_bps")),
        "volume_z": _safe_float(features.get("volume_z")),
        "context_return_bps": _safe_float(features.get("context_return_bps")),
        "context_drawdown_bps": _safe_float(features.get("context_drawdown_bps")),
        "context_rebound_bps": _safe_float(features.get("context_rebound_bps")),
        "context_range_pos": _safe_float(features.get("context_range_pos")),
        "spread_bps": _safe_float(features.get("spread_bps")),
        "depth": _safe_float(features.get("depth")),
        "imbalance": _safe_float(features.get("imbalance")),
        "edge_bps_effective": _safe_float(alpha.get("edge_bps_effective", alpha.get("edge_bps"))),
        "expected_cost_bps": _safe_float(cost.get("expected_cost_bps")),
        "structure_confidence": _safe_float(structure.get("confidence")),
        "structure_slope_short_bps": _safe_float(structure.get("slope_short_bps")),
        "structure_slope_medium_bps": _safe_float(structure.get("slope_medium_bps")),
        "structure_slope_long_bps": _safe_float(structure.get("slope_long_bps")),
        "structure_drawdown_from_peak_bps": _safe_float(structure.get("drawdown_from_peak_bps")),
        "structure_extension_bps": _safe_float(structure.get("extension_bps")),
        "structure_range_pos": _safe_float(structure.get("range_pos")),
        "structure_rebound_bps": _safe_float(structure.get("rebound_bps")),
        "bars_since_peak": _safe_float(structure.get("bars_since_peak")),
        "swing_momentum_bps": _safe_float(meta.get("swing_momentum_bps")),
        "swing_range_bps": _safe_float(meta.get("swing_range_bps")),
        "swing_oscillator": _safe_float(meta.get("swing_oscillator")),
        "breakout_up_bps": _safe_float(meta.get("breakout_up_bps")),
        "breakout_down_bps": _safe_float(meta.get("breakout_down_bps")),
        "up_structure": _safe_bool(structure.get("up_structure")),
        "down_structure": _safe_bool(structure.get("down_structure")),
        "phase_bottom": 1.0 if phase == "bottom" else 0.0,
        "phase_lift_off": 1.0 if phase == "lift_off" else 0.0,
        "phase_range": 1.0 if phase == "range" else 0.0,
        "phase_peak": 1.0 if phase == "peak" else 0.0,
        "phase_rollover": 1.0 if phase == "rollover" else 0.0,
        "phase_downtrend": 1.0 if phase == "downtrend" else 0.0,
        "swing_state_valley_rebound": 1.0 if swing_state == "valley_rebound" else 0.0,
        "swing_state_micro_valley_rebound": 1.0 if swing_state == "micro_valley_rebound" else 0.0,
        "continuation_state_await_liftoff": 1.0 if continuation_state == "await_liftoff" else 0.0,
        "continuation_state_early_liftoff": 1.0 if continuation_state == "early_liftoff_override" else 0.0,
        "continuation_state_staircase": 1.0 if continuation_state == "staircase_override" else 0.0,
        "breakout_state_up": 1.0 if breakout_state == "up_breakout" else 0.0,
    }
    return vector


def _trade_sample_to_json_line(sample: TradeSample) -> str:
    return json.dumps(asdict(sample), ensure_ascii=True)


def _extract_market_event(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
    if not isinstance(payload, dict):
        return None
    ts = _parse_ts(payload.get("ts") or event.get("ts"))
    if ts is None:
        return None
    micro = payload.get("micro") if isinstance(payload.get("micro"), dict) else {}
    open_px = _safe_float(payload.get("open"))
    high_px = _safe_float(payload.get("high"))
    low_px = _safe_float(payload.get("low"))
    close_px = _safe_float(payload.get("close"))
    if max(open_px, high_px, low_px, close_px) <= 0.0:
        return None
    return {
        "ts": ts,
        "open": open_px,
        "high": high_px,
        "low": low_px,
        "close": close_px,
        "volume": _safe_float(payload.get("volume")),
        "spread_bps": _safe_float(micro.get("spread_bps")),
        "depth": _safe_float(micro.get("depth")),
        "imbalance": _safe_float(micro.get("imbalance")),
    }


def _extract_position_sync_state(event: dict[str, Any], symbol: str) -> tuple[float, float] | None:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
    if not isinstance(payload, dict):
        return None

    if event_type in {"exec_core_account_sync", "core_account_synced"}:
        base_asset = str(payload.get("base_asset") or "").strip().upper()
        pair = str(payload.get("pair") or "").strip().upper()
        if base_asset and base_asset != symbol:
            return None
        if not base_asset and pair and not pair.startswith(f"{symbol}/"):
            return None
        qty = _safe_float(payload.get("position_btc"), float("nan"))
        if math.isnan(qty):
            return None
        reference_price = _safe_float(payload.get("avg_entry_price", payload.get("reference_price")), 0.0)
        return max(0.0, qty), max(0.0, reference_price)

    if event_type == "exec_balance_snapshot":
        balances = payload.get("balances") if isinstance(payload.get("balances"), dict) else {}
        if symbol not in balances:
            return None
        raw_balance = balances.get(symbol)
        if isinstance(raw_balance, dict):
            qty = _safe_float(raw_balance.get("total", raw_balance.get("free")), float("nan"))
        else:
            qty = _safe_float(raw_balance, float("nan"))
        if math.isnan(qty):
            return None
        return max(0.0, qty), 0.0

    return None


def _reconcile_open_trade_with_sync(
    open_trade: dict[str, Any],
    *,
    synced_qty: float,
    synced_reference_price: float,
    dust_close_usdc: float,
) -> dict[str, Any] | None:
    remaining_qty = max(0.0, _safe_float(open_trade.get("remaining_qty")))
    if remaining_qty <= 1e-9:
        return None
    remaining_buy_notional = max(0.0, _safe_float(open_trade.get("remaining_buy_notional")))
    remaining_buy_fees = max(0.0, _safe_float(open_trade.get("remaining_buy_fees")))
    avg_entry_price = (remaining_buy_notional / remaining_qty) if remaining_qty > 0.0 else 0.0
    mark_price = synced_reference_price if synced_reference_price > 0.0 else avg_entry_price
    synced_notional = max(0.0, synced_qty * mark_price) if mark_price > 0.0 else 0.0
    qty_tolerance = max(1e-9, remaining_qty * 0.005)

    if synced_qty <= qty_tolerance or (synced_notional > 0.0 and synced_notional <= dust_close_usdc):
        return None

    if synced_qty + qty_tolerance < remaining_qty:
        ratio = max(0.0, min(1.0, synced_qty / remaining_qty))
        open_trade["remaining_qty"] = synced_qty
        open_trade["remaining_buy_notional"] = remaining_buy_notional * ratio
        open_trade["remaining_buy_fees"] = remaining_buy_fees * ratio
        return open_trade

    if synced_qty > remaining_qty + qty_tolerance:
        return None

    return open_trade


def _market_interval_seconds(events: list[dict[str, Any]]) -> float:
    deltas: list[float] = []
    previous_ts: datetime | None = None
    for event in events:
        ts = event.get("ts")
        if not isinstance(ts, datetime):
            continue
        if previous_ts is not None:
            delta = (ts - previous_ts).total_seconds()
            if 15.0 <= delta <= 3600.0:
                deltas.append(delta)
        previous_ts = ts
    if not deltas:
        return DEFAULT_MARKET_INTERVAL_SECONDS
    deltas.sort()
    return deltas[len(deltas) // 2]


def _compute_post_path_metrics(
    *,
    anchor_ts: datetime | None,
    anchor_price: float,
    market_events: list[dict[str, Any]],
    prefix: str,
) -> dict[str, float]:
    if anchor_ts is None or anchor_price <= 0.0 or not market_events:
        return {}
    interval_seconds = _market_interval_seconds(market_events)
    interval = timedelta(seconds=max(1.0, interval_seconds))
    metrics: dict[str, float] = {}
    for horizon_minutes in POST_EXIT_HORIZONS_MINUTES:
        horizon_end = anchor_ts + timedelta(minutes=horizon_minutes)
        bars: list[dict[str, Any]] = []
        for event in market_events:
            bar_ts = event.get("ts")
            if not isinstance(bar_ts, datetime):
                continue
            if bar_ts > horizon_end:
                break
            if bar_ts + interval <= anchor_ts:
                continue
            bars.append(event)
        metrics[f"{prefix}_bars_{horizon_minutes}m"] = float(len(bars))
        if not bars:
            continue
        highs = [float(event.get("high") or 0.0) for event in bars if float(event.get("high") or 0.0) > 0.0]
        lows = [float(event.get("low") or 0.0) for event in bars if float(event.get("low") or 0.0) > 0.0]
        last_close = float(bars[-1].get("close") or 0.0)
        if highs:
            metrics[f"{prefix}_mfe_{horizon_minutes}m_bps"] = ((max(highs) / anchor_price) - 1.0) * 10000.0
        if lows:
            metrics[f"{prefix}_mae_{horizon_minutes}m_bps"] = ((min(lows) / anchor_price) - 1.0) * 10000.0
        if last_close > 0.0:
            metrics[f"{prefix}_close_{horizon_minutes}m_bps"] = ((last_close / anchor_price) - 1.0) * 10000.0
    return metrics


def _compute_post_exit_metrics(
    *,
    exit_ts: datetime | None,
    exit_price: float,
    market_events: list[dict[str, Any]],
) -> dict[str, float]:
    return _compute_post_path_metrics(
        anchor_ts=exit_ts,
        anchor_price=exit_price,
        market_events=market_events,
        prefix="post_exit",
    )


def _post_exit_metric(sample: TradeSample, key: str, default: float = 0.0) -> float:
    metrics = sample.post_exit_metrics if isinstance(sample.post_exit_metrics, dict) else {}
    return _safe_float(metrics.get(key), default)


def _trade_gross_exit_bps(sample: TradeSample) -> float:
    buy_notional = max(0.0, float(sample.buy_notional or 0.0))
    sell_notional = max(0.0, float(sample.sell_notional or 0.0))
    if buy_notional <= 0.0 or sell_notional <= 0.0:
        return 0.0
    return ((sell_notional / buy_notional) - 1.0) * 10000.0


def _trade_net_exit_bps(sample: TradeSample) -> float:
    buy_notional = max(0.0, float(sample.buy_notional or 0.0))
    if buy_notional <= 0.0:
        return 0.0
    return (float(sample.net_pnl or 0.0) / buy_notional) * 10000.0


def _trade_hold_minutes(sample: TradeSample) -> float:
    entry_ts = _parse_ts(sample.entry_ts)
    exit_ts = _parse_ts(sample.exit_ts)
    if entry_ts is None or exit_ts is None:
        return 0.0
    return max(0.0, (exit_ts - entry_ts).total_seconds() / 60.0)


def _trade_entry_price(sample: TradeSample) -> float:
    buy_qty = max(0.0, float(sample.buy_qty or 0.0))
    buy_notional = max(0.0, float(sample.buy_notional or 0.0))
    if buy_qty <= 0.0 or buy_notional <= 0.0:
        return 0.0
    return buy_notional / buy_qty


def _trade_exit_price(sample: TradeSample) -> float:
    sell_qty = max(0.0, float(sample.sell_qty or 0.0))
    sell_notional = max(0.0, float(sample.sell_notional or 0.0))
    if sell_qty <= 0.0 or sell_notional <= 0.0:
        return 0.0
    return sell_notional / sell_qty


def _post_entry_metric(sample: TradeSample, key: str, default: float = 0.0) -> float:
    entry_price = _trade_entry_price(sample)
    exit_price = _trade_exit_price(sample)
    if entry_price <= 0.0 or exit_price <= 0.0:
        return float(default)
    post_exit_bps = _post_exit_metric(sample, key, float("nan"))
    if math.isnan(post_exit_bps):
        return float(default)
    future_price = exit_price * (1.0 + (post_exit_bps / 10000.0))
    if future_price <= 0.0:
        return float(default)
    return ((future_price / entry_price) - 1.0) * 10000.0


def _entry_quality_score(sample: TradeSample) -> tuple[float, str]:
    candidates: list[tuple[str, float]] = [("realized_net_exit", _trade_net_exit_bps(sample))]
    for horizon_minutes in (15, 30, 60):
        bars = int(round(_post_exit_metric(sample, f"post_exit_bars_{horizon_minutes}m", 0.0)))
        if bars <= 0:
            continue
        close_bps = _post_entry_metric(
            sample,
            f"post_exit_close_{horizon_minutes}m_bps",
            _trade_net_exit_bps(sample),
        )
        candidates.append((f"post_entry_close_{horizon_minutes}m", close_bps))
    best_reason, best_score = max(candidates, key=lambda item: item[1], default=("realized_net_exit", 0.0))
    return best_score, best_reason


def _annotate_learning_fields(sample: TradeSample) -> tuple[float, str, bool]:
    score_bps, score_reason = _entry_quality_score(sample)
    exit_reason = str(sample.exit_reason or "").strip().lower()
    protective_exit = _protective_exit_signal(sample)
    ambiguous_loss = (
        (not bool(sample.profitable))
        and exit_reason not in STOPLIKE_EXIT_REASONS
        and not protective_exit
        and score_bps >= ENTRY_QUALITY_PROMOTION_THRESHOLD_BPS
    )
    if bool(sample.profitable):
        label_reason = "realized_profit"
    elif ambiguous_loss:
        label_reason = score_reason
    elif protective_exit:
        label_reason = "protective_exit"
    elif exit_reason in STOPLIKE_EXIT_REASONS:
        label_reason = "stoplike_exit"
    else:
        label_reason = "realized_loss"
    sample.entry_quality_score_bps = score_bps
    sample.entry_quality_reason = label_reason
    return score_bps, label_reason, ambiguous_loss


def _poor_entry_signal(sample: TradeSample) -> bool:
    score_bps = float(sample.entry_quality_score_bps or 0.0)
    if score_bps <= -8.0:
        return True
    if float(sample.net_pnl or 0.0) < 0.0 and _trade_net_exit_bps(sample) <= -12.0:
        return True
    return False


def _top_zone_entry_signal(sample: TradeSample) -> bool:
    features = sample.features if isinstance(sample.features, dict) else {}
    context_range_pos = _safe_float(features.get("context_range_pos"))
    structure_range_pos = _safe_float(features.get("structure_range_pos"))
    context_drawdown_from_peak_bps = max(0.0, -_safe_float(features.get("context_drawdown_bps")))
    trend_return_bps = _safe_float(features.get("trend_return_bps"))
    return (
        context_range_pos >= 0.84
        or structure_range_pos >= 0.92
        or (
            context_range_pos >= 0.78
            and context_drawdown_from_peak_bps <= 32.0
            and trend_return_bps >= 140.0
        )
    )


def _late_entry_signal(sample: TradeSample) -> bool:
    features = sample.features if isinstance(sample.features, dict) else {}
    trend_return_bps = _safe_float(features.get("trend_return_bps"))
    return_bps = _safe_float(features.get("return_bps"))
    structure_extension_bps = _safe_float(features.get("structure_extension_bps"))
    context_drawdown_from_peak_bps = max(0.0, -_safe_float(features.get("context_drawdown_bps")))
    if not _top_zone_entry_signal(sample):
        return False
    if trend_return_bps < 110.0:
        return False
    if return_bps < 10.0 and structure_extension_bps < 32.0:
        return False
    if context_drawdown_from_peak_bps > 55.0:
        return False
    return _poor_entry_signal(sample)


def _recency_weight(index: int, total: int) -> float:
    if total <= 1:
        return RECENT_SAMPLE_MIN_WEIGHT
    progress = max(0.0, min(1.0, float(index) / float(total - 1)))
    return RECENT_SAMPLE_MIN_WEIGHT + (
        (RECENT_SAMPLE_MAX_WEIGHT - RECENT_SAMPLE_MIN_WEIGHT) * progress
    )


def _prepare_training_samples(train_samples: list[TradeSample]) -> dict[str, float]:
    diagnostics: dict[str, float] = {
        "train_sample_count": float(len(train_samples)),
        "ambiguous_loss_count": 0.0,
        "ambiguous_loss_rate": 0.0,
        "protective_loss_count": 0.0,
        "avg_train_weight": 0.0,
        "min_train_weight": 0.0,
        "max_train_weight": 0.0,
        "first_train_weight": 0.0,
        "last_train_weight": 0.0,
        "avg_ambiguous_loss_weight": 0.0,
    }
    if not train_samples:
        return diagnostics

    weights: list[float] = []
    ambiguous_weights: list[float] = []
    ambiguous_count = 0
    protective_count = 0
    for idx, sample in enumerate(train_samples):
        _score_bps, reason, ambiguous_loss = _annotate_learning_fields(sample)
        weight = _recency_weight(idx, len(train_samples))
        if ambiguous_loss:
            weight *= AMBIGUOUS_LOSS_WEIGHT
            ambiguous_count += 1
            ambiguous_weights.append(weight)
        elif (not bool(sample.profitable)) and reason == "protective_exit":
            weight *= PROTECTIVE_LOSS_WEIGHT
            protective_count += 1
        sample.learning_weight = weight
        weights.append(weight)

    diagnostics["ambiguous_loss_count"] = float(ambiguous_count)
    diagnostics["ambiguous_loss_rate"] = ambiguous_count / float(len(train_samples))
    diagnostics["protective_loss_count"] = float(protective_count)
    diagnostics["avg_train_weight"] = sum(weights) / float(len(weights))
    diagnostics["min_train_weight"] = min(weights)
    diagnostics["max_train_weight"] = max(weights)
    diagnostics["first_train_weight"] = weights[0]
    diagnostics["last_train_weight"] = weights[-1]
    diagnostics["avg_ambiguous_loss_weight"] = (
        sum(ambiguous_weights) / float(len(ambiguous_weights))
        if ambiguous_weights
        else 0.0
    )
    return diagnostics


def _early_exit_signal(sample: TradeSample) -> bool:
    bars15 = int(round(_post_exit_metric(sample, "post_exit_bars_15m", 0.0)))
    bars30 = int(round(_post_exit_metric(sample, "post_exit_bars_30m", 0.0)))
    mfe15 = _post_exit_metric(sample, "post_exit_mfe_15m_bps", 0.0)
    mae15 = _post_exit_metric(sample, "post_exit_mae_15m_bps", 0.0)
    close15 = _post_exit_metric(sample, "post_exit_close_15m_bps", 0.0)
    mfe30 = _post_exit_metric(sample, "post_exit_mfe_30m_bps", 0.0)
    close30 = _post_exit_metric(sample, "post_exit_close_30m_bps", 0.0)
    if bars15 > 0 and mfe15 >= 12.0 and close15 >= 6.0 and mae15 > -14.0:
        return True
    return bars30 > 0 and mfe30 >= 18.0 and close30 >= 8.0 and mae15 > -18.0


def _protective_exit_signal(sample: TradeSample) -> bool:
    bars15 = int(round(_post_exit_metric(sample, "post_exit_bars_15m", 0.0)))
    bars30 = int(round(_post_exit_metric(sample, "post_exit_bars_30m", 0.0)))
    mfe15 = _post_exit_metric(sample, "post_exit_mfe_15m_bps", 0.0)
    mae15 = _post_exit_metric(sample, "post_exit_mae_15m_bps", 0.0)
    close15 = _post_exit_metric(sample, "post_exit_close_15m_bps", 0.0)
    mfe30 = _post_exit_metric(sample, "post_exit_mfe_30m_bps", 0.0)
    mae30 = _post_exit_metric(sample, "post_exit_mae_30m_bps", 0.0)
    close30 = _post_exit_metric(sample, "post_exit_close_30m_bps", 0.0)
    if bars15 > 0 and mae15 <= -10.0 and close15 <= -4.0 and mfe15 < 12.0:
        return True
    return bars30 > 0 and mae30 <= -16.0 and close30 <= -8.0 and mfe30 < 18.0


def _failed_start_recovery_signal(sample: TradeSample) -> bool:
    reason = str(sample.exit_reason or "").strip().lower()
    if reason != "failed_start_exit":
        return False
    bars15 = int(round(_post_exit_metric(sample, "post_exit_bars_15m", 0.0)))
    bars30 = int(round(_post_exit_metric(sample, "post_exit_bars_30m", 0.0)))
    mfe15 = _post_exit_metric(sample, "post_exit_mfe_15m_bps", 0.0)
    close15 = _post_exit_metric(sample, "post_exit_close_15m_bps", 0.0)
    close30 = _post_exit_metric(sample, "post_exit_close_30m_bps", 0.0)
    if bars15 > 0 and (mfe15 >= 12.0 or close15 >= 4.0):
        return True
    return bars30 > 0 and close30 >= 8.0


def _trailing_missed_run_signal(sample: TradeSample) -> bool:
    reason = str(sample.exit_reason or "").strip().lower()
    if reason not in {"trailing_stop", "trim", "take_profit"}:
        return False
    return _early_exit_signal(sample)


def _shakeout_then_run_signal(sample: TradeSample) -> bool:
    reason = str(sample.exit_reason or "").strip().lower()
    if reason in {"emergency_exit", "stop_loss", "hard_stop"}:
        return False
    bars15 = int(round(_post_exit_metric(sample, "post_exit_bars_15m", 0.0)))
    bars30 = int(round(_post_exit_metric(sample, "post_exit_bars_30m", 0.0)))
    bars60 = int(round(_post_exit_metric(sample, "post_exit_bars_60m", 0.0)))
    bars120 = int(round(_post_exit_metric(sample, "post_exit_bars_120m", 0.0)))
    mae15 = _post_exit_metric(sample, "post_exit_mae_15m_bps", 0.0)
    mae30 = _post_exit_metric(sample, "post_exit_mae_30m_bps", 0.0)
    close15 = _post_exit_metric(sample, "post_exit_close_15m_bps", 0.0)
    close30 = _post_exit_metric(sample, "post_exit_close_30m_bps", 0.0)
    close60 = _post_exit_metric(sample, "post_exit_close_60m_bps", 0.0)
    close120 = _post_exit_metric(sample, "post_exit_close_120m_bps", 0.0)
    mfe30 = _post_exit_metric(sample, "post_exit_mfe_30m_bps", 0.0)
    mfe60 = _post_exit_metric(sample, "post_exit_mfe_60m_bps", 0.0)
    mfe120 = _post_exit_metric(sample, "post_exit_mfe_120m_bps", 0.0)
    short_shakeout = (
        (bars15 > 0 and mae15 <= -18.0 and close15 <= 4.0)
        or (bars30 > 0 and mae30 <= -22.0 and close30 <= 8.0)
    )
    later_run = (
        (bars30 > 0 and mfe30 >= 22.0 and close30 >= 8.0)
        or (bars60 > 0 and mfe60 >= 30.0 and close60 >= 12.0)
        or (bars120 > 0 and mfe120 >= 38.0 and close120 >= 16.0)
    )
    return short_shakeout and later_run


def _micro_pop_loss_run_signal(sample: TradeSample) -> bool:
    reason = str(sample.exit_reason or "").strip().lower()
    if reason in {"emergency_exit", "stop_loss", "hard_stop", "hard_stop_loss"}:
        return False
    if reason not in {
        "green_candle_take_exit",
        "time_break_even_floor",
        "trailing_stop",
        "trim",
        "take_profit",
        "chop_break_even_reclaim",
    }:
        return False
    net_exit_bps = _trade_net_exit_bps(sample)
    gross_exit_bps = _trade_gross_exit_bps(sample)
    hold_minutes = _trade_hold_minutes(sample)
    if net_exit_bps > 0.0:
        return False
    if gross_exit_bps < -4.0:
        return False
    if hold_minutes > 20.0:
        return False
    bars15 = int(round(_post_exit_metric(sample, "post_exit_bars_15m", 0.0)))
    bars30 = int(round(_post_exit_metric(sample, "post_exit_bars_30m", 0.0)))
    bars60 = int(round(_post_exit_metric(sample, "post_exit_bars_60m", 0.0)))
    mfe15 = _post_exit_metric(sample, "post_exit_mfe_15m_bps", 0.0)
    close15 = _post_exit_metric(sample, "post_exit_close_15m_bps", 0.0)
    mfe30 = _post_exit_metric(sample, "post_exit_mfe_30m_bps", 0.0)
    close30 = _post_exit_metric(sample, "post_exit_close_30m_bps", 0.0)
    mfe60 = _post_exit_metric(sample, "post_exit_mfe_60m_bps", 0.0)
    close60 = _post_exit_metric(sample, "post_exit_close_60m_bps", 0.0)
    quick_follow_through = (
        (bars15 > 0 and mfe15 >= max(12.0, abs(net_exit_bps) + 8.0) and close15 >= 3.0)
        or (bars30 > 0 and mfe30 >= max(18.0, abs(net_exit_bps) + 12.0) and close30 >= 6.0)
        or (bars60 > 0 and mfe60 >= max(26.0, abs(net_exit_bps) + 16.0) and close60 >= 10.0)
    )
    return quick_follow_through


def _summarize_exit_path(samples: list[TradeSample]) -> dict[str, Any]:
    post_items = [
        sample for sample in samples if isinstance(sample.post_exit_metrics, dict) and sample.post_exit_metrics
    ]
    summary: dict[str, Any] = {
        "sample_count": len(post_items),
        "early_exit_count": 0,
        "early_exit_rate": 0.0,
        "protective_exit_count": 0,
        "protective_exit_rate": 0.0,
        "failed_start_recovery_count": 0,
        "failed_start_recovery_rate": 0.0,
        "trailing_missed_run_count": 0,
        "trailing_missed_run_rate": 0.0,
        "shakeout_then_run_count": 0,
        "shakeout_then_run_rate": 0.0,
        "micro_pop_loss_run_count": 0,
        "micro_pop_loss_run_rate": 0.0,
        "hold_opportunity_rate": 0.0,
        "by_reason": {},
    }
    for horizon_minutes in POST_EXIT_HORIZONS_MINUTES:
        eligible = [
            sample
            for sample in post_items
            if int(round(_post_exit_metric(sample, f"post_exit_bars_{horizon_minutes}m", 0.0))) > 0
        ]
        summary[f"avg_post_exit_mfe_{horizon_minutes}m_bps"] = (
            sum(_post_exit_metric(sample, f"post_exit_mfe_{horizon_minutes}m_bps", 0.0) for sample in eligible)
            / float(len(eligible))
            if eligible
            else 0.0
        )
        summary[f"avg_post_exit_mae_{horizon_minutes}m_bps"] = (
            sum(_post_exit_metric(sample, f"post_exit_mae_{horizon_minutes}m_bps", 0.0) for sample in eligible)
            / float(len(eligible))
            if eligible
            else 0.0
        )
        summary[f"avg_post_exit_close_{horizon_minutes}m_bps"] = (
            sum(_post_exit_metric(sample, f"post_exit_close_{horizon_minutes}m_bps", 0.0) for sample in eligible)
            / float(len(eligible))
            if eligible
            else 0.0
        )

    classified = [
        sample
        for sample in post_items
        if int(round(_post_exit_metric(sample, "post_exit_bars_15m", 0.0))) > 0
        or int(round(_post_exit_metric(sample, "post_exit_bars_30m", 0.0))) > 0
        or int(round(_post_exit_metric(sample, "post_exit_bars_60m", 0.0))) > 0
        or int(round(_post_exit_metric(sample, "post_exit_bars_120m", 0.0))) > 0
    ]
    if not classified:
        return summary

    early_count = 0
    protective_count = 0
    failed_start_recovery_count = 0
    trailing_missed_count = 0
    shakeout_then_run_count = 0
    micro_pop_loss_run_count = 0
    reason_summary: dict[str, dict[str, float | int]] = {}
    for sample in classified:
        reason = str(sample.exit_reason or "unknown").strip().lower() or "unknown"
        early = _early_exit_signal(sample)
        protective = _protective_exit_signal(sample)
        failed_start_recovery = _failed_start_recovery_signal(sample)
        trailing_missed = _trailing_missed_run_signal(sample)
        shakeout_then_run = _shakeout_then_run_signal(sample)
        micro_pop_loss_run = _micro_pop_loss_run_signal(sample)
        early_count += 1 if early else 0
        protective_count += 1 if protective else 0
        failed_start_recovery_count += 1 if failed_start_recovery else 0
        trailing_missed_count += 1 if trailing_missed else 0
        shakeout_then_run_count += 1 if shakeout_then_run else 0
        micro_pop_loss_run_count += 1 if micro_pop_loss_run else 0
        bucket = reason_summary.setdefault(
            reason,
            {
                "count": 0,
                "early_exit_count": 0,
                "protective_exit_count": 0,
                "failed_start_recovery_count": 0,
                "trailing_missed_run_count": 0,
                "shakeout_then_run_count": 0,
                "micro_pop_loss_run_count": 0,
            },
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["early_exit_count"] = int(bucket["early_exit_count"]) + (1 if early else 0)
        bucket["protective_exit_count"] = int(bucket["protective_exit_count"]) + (1 if protective else 0)
        bucket["failed_start_recovery_count"] = int(bucket["failed_start_recovery_count"]) + (
            1 if failed_start_recovery else 0
        )
        bucket["trailing_missed_run_count"] = int(bucket["trailing_missed_run_count"]) + (1 if trailing_missed else 0)
        bucket["shakeout_then_run_count"] = int(bucket["shakeout_then_run_count"]) + (1 if shakeout_then_run else 0)
        bucket["micro_pop_loss_run_count"] = int(bucket["micro_pop_loss_run_count"]) + (
            1 if micro_pop_loss_run else 0
        )

    classified_count = float(len(classified))
    summary["early_exit_count"] = early_count
    summary["early_exit_rate"] = early_count / classified_count
    summary["protective_exit_count"] = protective_count
    summary["protective_exit_rate"] = protective_count / classified_count
    failed_start_items = sum(
        1 for sample in classified if str(sample.exit_reason or "").strip().lower() == "failed_start_exit"
    )
    trailing_items = sum(
        1
        for sample in classified
        if str(sample.exit_reason or "").strip().lower() in {"trailing_stop", "trim", "take_profit"}
    )
    summary["failed_start_recovery_count"] = failed_start_recovery_count
    summary["failed_start_recovery_rate"] = (
        failed_start_recovery_count / float(failed_start_items) if failed_start_items else 0.0
    )
    summary["trailing_missed_run_count"] = trailing_missed_count
    summary["trailing_missed_run_rate"] = trailing_missed_count / float(trailing_items) if trailing_items else 0.0
    summary["shakeout_then_run_count"] = shakeout_then_run_count
    summary["shakeout_then_run_rate"] = shakeout_then_run_count / classified_count
    summary["micro_pop_loss_run_count"] = micro_pop_loss_run_count
    summary["micro_pop_loss_run_rate"] = micro_pop_loss_run_count / classified_count
    hold_opportunity_count = sum(
        1
        for sample in classified
        if (
            _early_exit_signal(sample)
            or _failed_start_recovery_signal(sample)
            or _trailing_missed_run_signal(sample)
            or _shakeout_then_run_signal(sample)
            or _micro_pop_loss_run_signal(sample)
        )
    )
    summary["hold_opportunity_rate"] = hold_opportunity_count / classified_count

    by_reason: dict[str, Any] = {}
    for reason, payload in sorted(reason_summary.items()):
        count = max(1, int(payload.get("count") or 0))
        by_reason[reason] = {
            "count": count,
            "early_exit_rate": float(payload.get("early_exit_count") or 0) / float(count),
            "protective_exit_rate": float(payload.get("protective_exit_count") or 0) / float(count),
            "failed_start_recovery_rate": float(payload.get("failed_start_recovery_count") or 0) / float(count),
            "trailing_missed_run_rate": float(payload.get("trailing_missed_run_count") or 0) / float(count),
            "shakeout_then_run_rate": float(payload.get("shakeout_then_run_count") or 0) / float(count),
            "micro_pop_loss_run_rate": float(payload.get("micro_pop_loss_run_count") or 0) / float(count),
        }
    summary["by_reason"] = by_reason
    return summary


def _summarize_entry_path(samples: list[TradeSample]) -> dict[str, Any]:
    qualified = [
        sample
        for sample in samples
        if isinstance(sample.features, dict) and sample.features
    ]
    summary: dict[str, Any] = {
        "sample_count": len(qualified),
        "poor_entry_count": 0,
        "poor_entry_rate": 0.0,
        "top_zone_entry_count": 0,
        "top_zone_entry_rate": 0.0,
        "late_entry_count": 0,
        "late_entry_rate": 0.0,
        "avg_entry_quality_score_bps": 0.0,
    }
    if not qualified:
        return summary
    poor_entry_count = 0
    top_zone_entry_count = 0
    late_entry_count = 0
    entry_quality_scores: list[float] = []
    for sample in qualified:
        entry_quality_scores.append(float(sample.entry_quality_score_bps or 0.0))
        if _poor_entry_signal(sample):
            poor_entry_count += 1
        if _top_zone_entry_signal(sample):
            top_zone_entry_count += 1
        if _late_entry_signal(sample):
            late_entry_count += 1
    sample_count = float(len(qualified))
    summary["poor_entry_count"] = poor_entry_count
    summary["poor_entry_rate"] = poor_entry_count / sample_count
    summary["top_zone_entry_count"] = top_zone_entry_count
    summary["top_zone_entry_rate"] = top_zone_entry_count / sample_count
    summary["late_entry_count"] = late_entry_count
    summary["late_entry_rate"] = late_entry_count / sample_count
    summary["avg_entry_quality_score_bps"] = (
        sum(entry_quality_scores) / float(len(entry_quality_scores))
        if entry_quality_scores
        else 0.0
    )
    return summary


def _summarize_no_trade_path(samples: list[CounterfactualSample]) -> dict[str, Any]:
    post_items = [
        sample for sample in samples if isinstance(sample.post_decision_metrics, dict) and sample.post_decision_metrics
    ]
    summary: dict[str, Any] = {
        "sample_count": len(post_items),
        "missed_entry_count": 0,
        "missed_entry_rate": 0.0,
        "correct_block_count": 0,
        "correct_block_rate": 0.0,
        "tunable_sample_count": 0,
        "tunable_missed_entry_count": 0,
        "tunable_missed_entry_rate": 0.0,
        "tunable_correct_block_count": 0,
        "tunable_correct_block_rate": 0.0,
        "by_reason": {},
    }
    for horizon_minutes in POST_EXIT_HORIZONS_MINUTES:
        eligible = [
            sample
            for sample in post_items
            if int(round(_counterfactual_post_metric(sample, f"post_decision_bars_{horizon_minutes}m", 0.0))) > 0
        ]
        summary[f"avg_post_decision_mfe_{horizon_minutes}m_bps"] = (
            sum(_counterfactual_post_metric(sample, f"post_decision_mfe_{horizon_minutes}m_bps", 0.0) for sample in eligible)
            / float(len(eligible))
            if eligible
            else 0.0
        )
        summary[f"avg_post_decision_mae_{horizon_minutes}m_bps"] = (
            sum(_counterfactual_post_metric(sample, f"post_decision_mae_{horizon_minutes}m_bps", 0.0) for sample in eligible)
            / float(len(eligible))
            if eligible
            else 0.0
        )
        summary[f"avg_post_decision_close_{horizon_minutes}m_bps"] = (
            sum(_counterfactual_post_metric(sample, f"post_decision_close_{horizon_minutes}m_bps", 0.0) for sample in eligible)
            / float(len(eligible))
            if eligible
            else 0.0
        )

    classified = [
        sample
        for sample in post_items
        if int(round(_counterfactual_post_metric(sample, "post_decision_bars_15m", 0.0))) > 0
        or int(round(_counterfactual_post_metric(sample, "post_decision_bars_30m", 0.0))) > 0
        or int(round(_counterfactual_post_metric(sample, "post_decision_bars_60m", 0.0))) > 0
        or int(round(_counterfactual_post_metric(sample, "post_decision_bars_120m", 0.0))) > 0
    ]
    if not classified:
        return summary

    missed_entry_count = 0
    correct_block_count = 0
    tunable_items = 0
    tunable_missed_entry_count = 0
    tunable_correct_block_count = 0
    reason_summary: dict[str, dict[str, int]] = {}
    for sample in classified:
        block_reason = str(sample.block_reason or "unknown").strip().lower() or "unknown"
        missed_entry = _missed_entry_signal(sample)
        correct_block = _correct_block_signal(sample)
        missed_entry_count += 1 if missed_entry else 0
        correct_block_count += 1 if correct_block else 0
        if block_reason not in NON_TUNABLE_BLOCK_REASONS:
            tunable_items += 1
            tunable_missed_entry_count += 1 if missed_entry else 0
            tunable_correct_block_count += 1 if correct_block else 0
        bucket = reason_summary.setdefault(
            block_reason,
            {
                "count": 0,
                "missed_entry_count": 0,
                "correct_block_count": 0,
            },
        )
        bucket["count"] = int(bucket["count"]) + 1
        bucket["missed_entry_count"] = int(bucket["missed_entry_count"]) + (1 if missed_entry else 0)
        bucket["correct_block_count"] = int(bucket["correct_block_count"]) + (1 if correct_block else 0)

    classified_count = float(len(classified))
    summary["missed_entry_count"] = missed_entry_count
    summary["missed_entry_rate"] = missed_entry_count / classified_count
    summary["correct_block_count"] = correct_block_count
    summary["correct_block_rate"] = correct_block_count / classified_count
    summary["tunable_sample_count"] = tunable_items
    summary["tunable_missed_entry_count"] = tunable_missed_entry_count
    summary["tunable_missed_entry_rate"] = (
        tunable_missed_entry_count / float(tunable_items) if tunable_items else 0.0
    )
    summary["tunable_correct_block_count"] = tunable_correct_block_count
    summary["tunable_correct_block_rate"] = (
        tunable_correct_block_count / float(tunable_items) if tunable_items else 0.0
    )
    summary["by_reason"] = {
        reason: {
            "count": int(payload.get("count") or 0),
            "missed_entry_rate": float(payload.get("missed_entry_count") or 0) / max(1.0, float(payload.get("count") or 0)),
            "correct_block_rate": float(payload.get("correct_block_count") or 0) / max(1.0, float(payload.get("count") or 0)),
        }
        for reason, payload in sorted(reason_summary.items())
    }
    return summary


def _summarize_trade_items(items: list[TradeSample]) -> dict[str, Any]:
    wins = [sample.net_pnl for sample in items if sample.net_pnl > 0.0]
    losses = [sample.net_pnl for sample in items if sample.net_pnl <= 0.0]
    exit_reasons = Counter(sample.exit_reason or "unknown" for sample in items)
    hold_seconds: list[float] = []
    for sample in items:
        entry_ts = _parse_ts(sample.entry_ts)
        exit_ts = _parse_ts(sample.exit_ts)
        if entry_ts is None or exit_ts is None:
            continue
        hold_seconds.append(max(0.0, (exit_ts - entry_ts).total_seconds()))
    return {
        "trade_count": len(items),
        "net_pnl": sum(sample.net_pnl for sample in items),
        "win_rate": (len(wins) / float(len(items))) if items else 0.0,
        "avg_win": (sum(wins) / float(len(wins))) if wins else 0.0,
        "avg_loss": (sum(losses) / float(len(losses))) if losses else 0.0,
        "avg_hold_sec": (sum(hold_seconds) / float(len(hold_seconds))) if hold_seconds else 0.0,
        "last_exit_ts": items[-1].exit_ts if items else None,
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "exit_path_summary": _summarize_exit_path(items),
        "entry_path_summary": _summarize_entry_path(items),
    }


def _group_segment_breakdown(
    trades: list[TradeSample],
    no_trade_samples: list[CounterfactualSample],
    *,
    tag_getter: Any,
    allowed_tags: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    trade_grouped: dict[str, list[TradeSample]] = {}
    no_trade_grouped: dict[str, list[CounterfactualSample]] = {}
    for sample in trades:
        tag = str(tag_getter(sample) or "").strip().lower() or "unknown"
        trade_grouped.setdefault(tag, []).append(sample)
    for sample in no_trade_samples:
        tag = str(tag_getter(sample) or "").strip().lower() or "unknown"
        no_trade_grouped.setdefault(tag, []).append(sample)
    keys = set(trade_grouped) | set(no_trade_grouped)
    if allowed_tags is not None:
        keys |= {str(item).strip().lower() for item in allowed_tags}
    breakdown: dict[str, dict[str, Any]] = {}
    for tag in sorted(keys):
        trade_items = trade_grouped.get(tag, [])
        no_trade_items = no_trade_grouped.get(tag, [])
        payload = _summarize_trade_items(trade_items)
        payload["no_trade_count"] = len(no_trade_items)
        payload["no_trade_summary"] = _summarize_no_trade_path(no_trade_items)
        breakdown[tag] = payload
    return breakdown


def extract_trade_samples(
    log_dir: Path,
    *,
    min_notional: float = 3.0,
    dust_close_usdc: float = 1.0,
    max_decision_age_sec: float = 180.0,
    mirror_verification: str = MIRROR_VERIFY_OFF,
    mirror_dir: Path | None = None,
) -> list[TradeSample]:
    samples: list[TradeSample] = []
    verification_mode = _normalize_mirror_verification_mode(mirror_verification)
    for path in sorted(log_dir.glob(JOURNAL_GLOB)):
        symbol = _symbol_from_journal(path)
        if not symbol:
            continue

        last_decision_ts: datetime | None = None
        last_decision_vector: dict[str, float] | None = None
        last_decision_strategy_context: dict[str, str] | None = None
        pending_exit_reason: str | None = None
        open_trade: dict[str, Any] | None = None
        market_events: list[dict[str, Any]] = []
        sample_offset = len(samples)
        active_alpha_type = ""

        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                try:
                    event = json.loads(raw_line)
                except Exception:
                    continue
                event_type = str(event.get("event_type") or "")
                event_ts = _parse_ts(event.get("ts"))
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
                if not isinstance(payload, dict):
                    payload = {}

                if open_trade is not None:
                    technical_flags = open_trade.setdefault("technical_flags", set())
                    if event_type == "exec_sell_qty_clamped_to_balance":
                        technical_flags.add("exec_sell_qty_clamped_to_balance")
                    elif event_type == "exec_sell_skipped_below_min_notional":
                        technical_flags.add("exec_sell_skipped_below_min_notional")
                    elif event_type == "add_order_insufficient_balance":
                        technical_flags.add("add_order_insufficient_balance")
                    elif event_type == "exec_report":
                        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                        reason = str(payload.get("reason") or "").strip().lower()
                        source = str(meta.get("source") or payload.get("source") or "").strip().lower()
                        if reason == "account_sync_delta" or source == "account_sync_delta":
                            technical_flags.add("account_sync_delta")
                    elif event_type == "fill":
                        source = str(payload.get("source") or "").strip().lower()
                        if source == "account_sync_delta":
                            technical_flags.add("account_sync_delta")

                if event_type == "core_reload_applied":
                    alpha_type = str(payload.get("alpha_type") or "").strip().lower()
                    if alpha_type:
                        if open_trade is not None:
                            open_alpha_type = str(
                                open_trade.get("open_alpha_type")
                                or open_trade.get("alpha_type_at_entry")
                                or ""
                            ).strip().lower()
                            if open_alpha_type and alpha_type != open_alpha_type:
                                technical_flags = open_trade.setdefault("technical_flags", set())
                                technical_flags.add("mid_trade_reload")
                            open_trade["open_alpha_type"] = alpha_type
                        active_alpha_type = alpha_type
                    continue

                if event_type == "core_decision":
                    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
                    if bool(risk.get("allow")) and _safe_float(risk.get("target_btc")) <= 0.0:
                        pending_exit_reason = str(risk.get("reason") or "") or None
                    vector = _extract_vector_from_decision(event)
                    strategy_context = _extract_strategy_context_from_decision(event)
                    alpha_type = str(payload.get("alpha_type") or "").strip().lower()
                    if alpha_type:
                        active_alpha_type = alpha_type
                    if event_ts is not None and (vector or strategy_context):
                        last_decision_ts = event_ts
                        last_decision_vector = vector
                        last_decision_strategy_context = strategy_context
                    continue

                if event_type == "market":
                    market_event = _extract_market_event(event)
                    if market_event is not None:
                        market_events.append(market_event)
                    continue

                sync_state = _extract_position_sync_state(event, symbol)
                if sync_state is not None:
                    if open_trade is not None:
                        synced_qty, synced_reference_price = sync_state
                        reconciled = _reconcile_open_trade_with_sync(
                            open_trade,
                            synced_qty=synced_qty,
                            synced_reference_price=synced_reference_price,
                            dust_close_usdc=dust_close_usdc,
                        )
                        if reconciled is None:
                            open_trade = None
                            pending_exit_reason = None
                        else:
                            open_trade = reconciled
                    continue

                if event_type != "fill":
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
                if not isinstance(payload, dict):
                    continue
                side = str(payload.get("side") or "").lower()
                qty = _safe_float(payload.get("qty_btc"))
                price = _safe_float(payload.get("price"))
                fee = _safe_float(payload.get("fee_eur"))
                if qty <= 0.0 or price <= 0.0:
                    continue

                if side == "buy":
                    if open_trade is None:
                        age_ok = False
                        if event_ts is not None and last_decision_ts is not None:
                            age_ok = (event_ts - last_decision_ts).total_seconds() <= max_decision_age_sec
                        entry_vector = dict(last_decision_vector or {}) if age_ok and last_decision_vector else {}
                        strategy_context = (
                            dict(last_decision_strategy_context or {})
                            if age_ok and last_decision_strategy_context
                            else {}
                        )
                        inferred_strategy_at_entry, strategy_scores = (
                            infer_trade_strategy(entry_vector) if entry_vector else ("", {})
                        )
                        strategy_at_entry = str(strategy_context.get("strategy_at_entry") or "").strip().lower()
                        strategy_source = str(strategy_context.get("strategy_source") or "").strip().lower()
                        if not strategy_at_entry:
                            strategy_at_entry = inferred_strategy_at_entry
                            strategy_source = "inferred" if inferred_strategy_at_entry else ""
                        regime_tag = _regime_tag_from_vector(entry_vector)
                        session_tag, entry_hour_utc, entry_weekday = _session_tag(event_ts)
                        open_trade = {
                            "entry_ts": str(event.get("ts") or ""),
                            "buy_qty": 0.0,
                            "entry_vector": entry_vector,
                            "strategy_at_entry": strategy_at_entry,
                            "strategy_at_entry_source": strategy_source,
                            "inferred_strategy_at_entry": inferred_strategy_at_entry,
                            "strategy_scores": strategy_scores,
                            "alpha_type_at_entry": str(strategy_context.get("alpha_type_at_entry") or ""),
                            "alpha_model_class_at_entry": str(strategy_context.get("alpha_model_class_at_entry") or ""),
                            "regime_tag": regime_tag,
                            "session_tag": session_tag,
                            "entry_hour_utc": entry_hour_utc,
                            "entry_weekday": entry_weekday,
                            "remaining_qty": 0.0,
                            "remaining_buy_notional": 0.0,
                            "remaining_buy_fees": 0.0,
                            "buy_fill_count": 0,
                            "first_fill_ts": None,
                            "last_fill_ts": None,
                            "technical_flags": set(),
                            "open_alpha_type": active_alpha_type,
                        }
                    open_trade["buy_qty"] += qty
                    open_trade["remaining_qty"] += qty
                    open_trade["remaining_buy_notional"] += qty * price
                    open_trade["remaining_buy_fees"] += fee
                    open_trade["buy_fill_count"] = int(open_trade.get("buy_fill_count") or 0) + 1
                    if event_ts is not None:
                        if open_trade.get("first_fill_ts") is None:
                            open_trade["first_fill_ts"] = event_ts
                        open_trade["last_fill_ts"] = event_ts
                    continue

                if side != "sell" or open_trade is None:
                    continue

                remaining_qty = float(open_trade.get("remaining_qty") or 0.0)
                matched_qty = min(qty, remaining_qty)
                if matched_qty <= 0.0:
                    continue
                remaining_buy_notional = float(open_trade.get("remaining_buy_notional") or 0.0)
                remaining_buy_fees = float(open_trade.get("remaining_buy_fees") or 0.0)
                avg_entry_price = (remaining_buy_notional / remaining_qty) if remaining_qty > 0.0 else 0.0
                alloc_buy_notional = matched_qty * avg_entry_price
                alloc_buy_fee = (
                    remaining_buy_fees * (matched_qty / remaining_qty)
                    if remaining_qty > 0.0
                    else 0.0
                )
                sell_notional = matched_qty * price
                realized_fees = alloc_buy_fee + fee
                realized_pnl = sell_notional - alloc_buy_notional - realized_fees
                open_trade["remaining_qty"] = max(0.0, remaining_qty - matched_qty)
                open_trade["remaining_buy_notional"] = max(0.0, remaining_buy_notional - alloc_buy_notional)
                open_trade["remaining_buy_fees"] = max(0.0, remaining_buy_fees - alloc_buy_fee)

                residual_qty = float(open_trade.get("remaining_qty") or 0.0)
                residual_mark = residual_qty * price
                if alloc_buy_notional >= min_notional:
                    vector = dict(open_trade.get("entry_vector") or {})
                    if vector:
                        exit_price = (sell_notional / matched_qty) if matched_qty > 0.0 else 0.0
                        entry_fill_count = max(1, int(open_trade.get("buy_fill_count") or 1))
                        first_fill_ts = open_trade.get("first_fill_ts")
                        last_fill_ts = open_trade.get("last_fill_ts")
                        entry_fill_span_minutes = 0.0
                        if isinstance(first_fill_ts, datetime) and isinstance(last_fill_ts, datetime):
                            entry_fill_span_minutes = max(
                                0.0,
                                (last_fill_ts - first_fill_ts).total_seconds() / 60.0,
                            )
                        technical_flags = sorted(
                            str(flag)
                            for flag in (open_trade.get("technical_flags") or set())
                            if str(flag).strip()
                        )
                        sample = TradeSample(
                                symbol=symbol,
                                entry_ts=str(open_trade.get("entry_ts") or ""),
                                exit_ts=str(event.get("ts") or ""),
                                buy_qty=matched_qty,
                                sell_qty=matched_qty,
                                buy_notional=alloc_buy_notional,
                                sell_notional=sell_notional,
                                fees=realized_fees,
                                dust_notional=0.0,
                                net_pnl=realized_pnl,
                                profitable=realized_pnl > 0.0,
                                exit_reason=pending_exit_reason,
                                strategy_at_entry=str(open_trade.get("strategy_at_entry") or ""),
                                strategy_scores=dict(open_trade.get("strategy_scores") or {}),
                                features=vector,
                                inferred_strategy_at_entry=str(open_trade.get("inferred_strategy_at_entry") or ""),
                                strategy_at_entry_source=str(open_trade.get("strategy_at_entry_source") or ""),
                                alpha_type_at_entry=str(open_trade.get("alpha_type_at_entry") or ""),
                                alpha_model_class_at_entry=str(open_trade.get("alpha_model_class_at_entry") or ""),
                                post_exit_metrics=_compute_post_exit_metrics(
                                    exit_ts=event_ts,
                                    exit_price=exit_price,
                                    market_events=market_events,
                                ),
                                regime_tag=str(open_trade.get("regime_tag") or ""),
                                session_tag=str(open_trade.get("session_tag") or ""),
                                entry_hour_utc=int(open_trade.get("entry_hour_utc") or -1),
                                entry_weekday=int(open_trade.get("entry_weekday") or -1),
                                entry_fill_count=entry_fill_count,
                                entry_fill_span_minutes=entry_fill_span_minutes,
                                technical_flags=technical_flags,
                            )
                        _annotate_learning_fields(sample)
                        samples.append(sample)

                if residual_qty > 1e-9 and residual_mark > dust_close_usdc:
                    continue

                if alloc_buy_notional < min_notional:
                    open_trade = None
                    pending_exit_reason = None
                    continue

                vector = dict(open_trade.get("entry_vector") or {})
                open_trade = None
                pending_exit_reason = None
        if market_events and len(samples) > sample_offset:
            refreshed_metrics = [
                _compute_post_exit_metrics(
                    exit_ts=_parse_ts(sample.exit_ts),
                    exit_price=(sample.sell_notional / sample.sell_qty) if sample.sell_qty > 0.0 else 0.0,
                    market_events=market_events,
                )
                for sample in samples[sample_offset:]
            ]
            for sample, metrics in zip(samples[sample_offset:], refreshed_metrics):
                sample.post_exit_metrics = metrics
                _annotate_learning_fields(sample)
    if verification_mode != MIRROR_VERIFY_OFF:
        annotate_trade_samples_with_mirror(samples, mirror_dir=mirror_dir)
        if verification_mode == MIRROR_VERIFY_REQUIRE:
            samples = [sample for sample in samples if bool(sample.mirror_verified)]
    samples.sort(key=lambda item: item.exit_ts)
    return samples


def extract_counterfactual_samples(
    log_dir: Path,
    *,
    min_anchor_price: float = 0.0000001,
    max_decision_age_sec: float = 180.0,
    min_decision_spacing_sec: float = COUNTERFACTUAL_DECISION_SPACING_SECONDS,
    dust_position_qty: float = 1e-9,
    lookback_hours: float | None = None,
) -> list[CounterfactualSample]:
    samples: list[CounterfactualSample] = []
    decision_cutoff = None
    if lookback_hours is not None:
        decision_cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(lookback_hours)))
    for path in sorted(log_dir.glob(JOURNAL_GLOB)):
        symbol = _symbol_from_journal(path)
        if not symbol:
            continue

        market_events: list[dict[str, Any]] = []
        buy_fill_times: list[datetime] = []
        blocked_decisions: list[dict[str, Any]] = []
        position_qty_estimate = 0.0
        last_recorded_decision_ts: datetime | None = None
        if lookback_hours is not None:
            max_lines = max(1200, int(round((max(0.1, float(lookback_hours)) + 2.0) * 240.0)))
            event_stream = tail_json_lines(path, max_lines=max_lines)
        else:
            event_stream = []
            with path.open(encoding="utf-8") as fh:
                for raw_line in fh:
                    try:
                        event = json.loads(raw_line)
                    except Exception:
                        continue
                    if isinstance(event, dict):
                        event_stream.append(event)

        for event in event_stream:
            event_type = str(event.get("event_type") or "")
            event_ts = _parse_ts(event.get("ts"))
            if decision_cutoff is not None and event_ts is not None and event_ts < decision_cutoff:
                continue
            if event_type == "market":
                market_event = _extract_market_event(event)
                if market_event is not None:
                    market_events.append(market_event)
                continue

            sync_state = _extract_position_sync_state(event, symbol)
            if sync_state is not None:
                synced_qty, _synced_reference_price = sync_state
                position_qty_estimate = max(0.0, synced_qty)
                continue

            if event_type == "fill":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
                if not isinstance(payload, dict):
                    continue
                side = str(payload.get("side") or "").strip().lower()
                qty = max(0.0, _safe_float(payload.get("qty_btc")))
                if qty <= 0.0:
                    continue
                if side == "buy":
                    position_qty_estimate += qty
                    if event_ts is not None:
                        buy_fill_times.append(event_ts)
                elif side == "sell":
                    position_qty_estimate = max(0.0, position_qty_estimate - qty)
                continue

            if event_type != "core_decision":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
            if not isinstance(payload, dict) or event_ts is None:
                continue
            vector = _extract_vector_from_decision(event)
            if not vector:
                continue
            features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
            gate = payload.get("gate") if isinstance(payload.get("gate"), dict) else {}
            risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
            intents = payload.get("intents") if isinstance(payload.get("intents"), list) else []
            anchor_price = _safe_float(features.get("price"))
            if anchor_price <= min_anchor_price:
                continue
            gate_allow = bool(gate.get("allow"))
            risk_allow = bool(risk.get("allow"))
            target_qty = _safe_float(risk.get("target_btc"))
            blocked = (not gate_allow) or (not risk_allow) or target_qty <= 0.0
            if not blocked or intents:
                continue
            if position_qty_estimate > dust_position_qty:
                continue
            if (
                last_recorded_decision_ts is not None
                and (event_ts - last_recorded_decision_ts).total_seconds() < max(60.0, float(min_decision_spacing_sec))
            ):
                continue
            strategy_primary, _strategy_scores = infer_trade_strategy(vector)
            regime_tag = _regime_tag_from_vector(vector)
            session_tag, hour_utc, weekday = _session_tag(event_ts)
            gate_reason = str(gate.get("reason") or "").strip().lower()
            risk_reason = str(risk.get("reason") or "").strip().lower()
            block_reason = gate_reason if gate_reason and risk_reason in {"", "gate_block"} else (risk_reason or gate_reason or "unknown")
            blocked_decisions.append(
                {
                    "symbol": symbol,
                    "decision_ts": event_ts,
                    "anchor_price": anchor_price,
                    "block_reason": block_reason,
                    "gate_reason": gate_reason or "unknown",
                    "risk_reason": risk_reason or "unknown",
                    "strategy_primary": str(strategy_primary or "").strip().lower() or "unknown",
                    "features": dict(vector),
                    "decision_context": _compact_decision_context(event),
                    "regime_tag": regime_tag,
                    "session_tag": session_tag,
                    "decision_hour_utc": hour_utc,
                    "decision_weekday": weekday,
                }
            )
            last_recorded_decision_ts = event_ts

        for item in blocked_decisions:
            decision_ts = item["decision_ts"]
            if any(
                0.0 <= (buy_ts - decision_ts).total_seconds() <= max_decision_age_sec
                for buy_ts in buy_fill_times
            ):
                continue
            metrics = _compute_post_path_metrics(
                anchor_ts=decision_ts,
                anchor_price=float(item["anchor_price"]),
                market_events=market_events,
                prefix="post_decision",
            )
            if not metrics:
                continue
            samples.append(
                CounterfactualSample(
                    symbol=symbol,
                    decision_ts=decision_ts.isoformat(),
                    anchor_price=float(item["anchor_price"]),
                    block_reason=str(item["block_reason"]),
                    gate_reason=str(item["gate_reason"]),
                    risk_reason=str(item["risk_reason"]),
                    strategy_primary=str(item["strategy_primary"]),
                    features=dict(item["features"]),
                    decision_context=dict(item["decision_context"]),
                    post_decision_metrics=metrics,
                    regime_tag=str(item["regime_tag"]),
                    session_tag=str(item["session_tag"]),
                    decision_hour_utc=int(item["decision_hour_utc"]),
                    decision_weekday=int(item["decision_weekday"]),
                )
            )

    samples.sort(key=lambda item: item.decision_ts)
    return samples


def _split_samples(samples: list[TradeSample], test_ratio: float) -> tuple[list[TradeSample], list[TradeSample]]:
    cutoff = max(1, min(len(samples) - 1, int(round(len(samples) * (1.0 - test_ratio)))))
    return samples[:cutoff], samples[cutoff:]


def _feature_stats(
    samples: list[TradeSample],
    feature_names: list[str],
    *,
    sample_weights: list[float] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    means: dict[str, float] = {}
    scales: dict[str, float] = {}
    weights = list(sample_weights or [])
    for name in feature_names:
        values = [float(sample.features.get(name, 0.0) or 0.0) for sample in samples]
        if values and len(weights) == len(values) and sum(weights) > 0.0:
            total_weight = sum(weights)
            mean = sum(value * weight for value, weight in zip(values, weights)) / total_weight
            var = sum(weight * ((value - mean) ** 2) for value, weight in zip(values, weights)) / total_weight
        else:
            mean = sum(values) / float(len(values)) if values else 0.0
            var = (
                sum((value - mean) ** 2 for value in values) / float(len(values))
                if values
                else 0.0
            )
        scale = math.sqrt(var) if var > 1e-12 else 1.0
        means[name] = mean
        scales[name] = scale
    return means, scales


def _scaled_vector(sample: TradeSample, feature_names: list[str], means: dict[str, float], scales: dict[str, float]) -> list[float]:
    out: list[float] = []
    for name in feature_names:
        value = float(sample.features.get(name, 0.0) or 0.0)
        out.append((value - means[name]) / scales[name])
    return out


def _classification_metrics(model: LogisticTradeModel, samples: list[TradeSample]) -> dict[str, float]:
    if not samples:
        return {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "brier": 0.0,
            "avg_p_positive": 0.0,
        }
    tp = fp = tn = fn = 0
    brier = 0.0
    positives = 0.0
    for sample in samples:
        p = model.predict_proba(sample.features)
        pred = p >= 0.5
        actual = bool(sample.profitable)
        positives += p
        brier += (p - (1.0 if actual else 0.0)) ** 2
        if pred and actual:
            tp += 1
        elif pred and not actual:
            fp += 1
        elif not pred and actual:
            fn += 1
        else:
            tn += 1
    total = len(samples)
    precision = tp / float(tp + fp) if (tp + fp) else 0.0
    recall = tp / float(tp + fn) if (tp + fn) else 0.0
    return {
        "accuracy": (tp + tn) / float(total),
        "precision": precision,
        "recall": recall,
        "brier": brier / float(total),
        "avg_p_positive": positives / float(total),
    }


def _sigmoid(score: float) -> float:
    if score >= 0.0:
        z = math.exp(-score)
        return 1.0 / (1.0 + z)
    z = math.exp(score)
    return z / (1.0 + z)


def _fit_calibration_bias(raw_scores: list[float], target_rate: float) -> float:
    if not raw_scores:
        return 0.0
    target = min(1.0 - 1e-6, max(1e-6, float(target_rate)))

    def _mean_probability(offset: float) -> float:
        return sum(_sigmoid(score + offset) for score in raw_scores) / float(len(raw_scores))

    low = -12.0
    high = 12.0
    for _ in range(64):
        mid = (low + high) / 2.0
        avg = _mean_probability(mid)
        if avg > target:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def train_trade_model(
    samples: list[TradeSample],
    *,
    feature_names: tuple[str, ...] = DEFAULT_MODEL_FEATURES,
    epochs: int = 600,
    learning_rate: float = 0.05,
    l2: float = 0.002,
    test_ratio: float = 0.25,
    seed: int = 42,
) -> LogisticTradeModel:
    if len(samples) < 12:
        raise ValueError("need at least 12 completed trades for a meaningful model")

    ordered = sorted(samples, key=lambda item: item.exit_ts)
    train_samples, test_samples = _split_samples(ordered, test_ratio)
    training_diagnostics = _prepare_training_samples(train_samples)
    for sample in test_samples:
        _annotate_learning_fields(sample)
        sample.learning_weight = 1.0
    names = list(feature_names)
    means, scales = _feature_stats(
        train_samples,
        names,
        sample_weights=[float(sample.learning_weight or 1.0) for sample in train_samples],
    )
    weights = {name: 0.0 for name in names}
    bias = 0.0

    rng = random.Random(seed)
    train_buffer = list(train_samples)
    for _epoch in range(max(1, int(epochs))):
        rng.shuffle(train_buffer)
        grad_w = {name: 0.0 for name in names}
        grad_b = 0.0
        for sample in train_buffer:
            x = _scaled_vector(sample, names, means, scales)
            linear = bias
            for idx, name in enumerate(names):
                linear += x[idx] * weights[name]
            if linear >= 0.0:
                z = math.exp(-linear)
                pred = 1.0 / (1.0 + z)
            else:
                z = math.exp(linear)
                pred = z / (1.0 + z)
            actual = 1.0 if sample.profitable else 0.0
            sample_weight = max(1e-6, float(sample.learning_weight or 1.0))
            diff = (pred - actual) * sample_weight
            grad_b += diff
            for idx, name in enumerate(names):
                grad_w[name] += diff * x[idx]
        n = sum(max(1e-6, float(sample.learning_weight or 1.0)) for sample in train_buffer)
        if n <= 0.0:
            break
        for name in names:
            grad = (grad_w[name] / n) + (l2 * weights[name])
            weights[name] -= learning_rate * grad
        bias -= learning_rate * (grad_b / n)

    model = LogisticTradeModel(
        feature_names=names,
        means=means,
        scales=scales,
        weights=weights,
        bias=bias,
        calibration_bias=0.0,
        sample_count=len(samples),
        positive_rate=sum(1.0 for sample in samples if sample.profitable) / float(len(samples)),
        train_metrics={},
        test_metrics={},
        training_diagnostics=training_diagnostics,
        trained_at=datetime.now(timezone.utc).isoformat(),
    )
    train_weight_total = sum(max(1e-6, float(sample.learning_weight or 1.0)) for sample in train_samples)
    train_positive_rate = (
        sum(
            (1.0 if sample.profitable else 0.0) * max(1e-6, float(sample.learning_weight or 1.0))
            for sample in train_samples
        )
        / train_weight_total
        if train_weight_total > 0.0
        else model.positive_rate
    )
    raw_train_scores = [model.predict_score(sample.features) for sample in train_samples]
    model.calibration_bias = _fit_calibration_bias(raw_train_scores, train_positive_rate)
    model.train_metrics = _classification_metrics(model, train_samples)
    model.test_metrics = _classification_metrics(model, test_samples)
    return model


def save_model(path: Path, model: LogisticTradeModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model.to_dict(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def load_model(path: Path) -> LogisticTradeModel:
    return LogisticTradeModel.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_rotation_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_decision_context(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data")
    if not isinstance(payload, dict):
        return {}
    gate = payload.get("gate") if isinstance(payload.get("gate"), dict) else {}
    alpha = payload.get("alpha") if isinstance(payload.get("alpha"), dict) else {}
    meta = alpha.get("meta") if isinstance(alpha.get("meta"), dict) else {}
    structure = meta.get("structure") if isinstance(meta.get("structure"), dict) else {}
    return {
        "gate_allow": bool(gate.get("allow")),
        "gate_reason": str(gate.get("reason") or ""),
        "edge_bps_effective": _safe_float(alpha.get("edge_bps_effective", alpha.get("edge_bps"))),
        "expected_cost_bps": _safe_float((payload.get("cost") or {}).get("expected_cost_bps")),
        "phase": str(structure.get("phase") or ""),
        "up_structure": bool(structure.get("up_structure")),
        "down_structure": bool(structure.get("down_structure")),
    }


def _latest_decision_from_journal(path: Path, max_lines: int = 400) -> tuple[datetime | None, dict[str, float], dict[str, Any]]:
    events = tail_json_lines(path, max_lines=max_lines)
    for event in reversed(events):
        if str(event.get("event_type") or "") != "core_decision":
            continue
        ts = _parse_ts(event.get("ts"))
        vector = _extract_vector_from_decision(event)
        if not vector:
            continue
        return ts, vector, _compact_decision_context(event)
    return None, {}, {}


def build_shadow_candidates(
    log_dir: Path,
    active_state: dict[str, Any],
    model: LogisticTradeModel,
    *,
    max_decision_age_sec: float = 300.0,
) -> list[ShadowCandidate]:
    selected = {str(item).upper() for item in active_state.get("selected") or []}
    watch = {str(item).upper() for item in active_state.get("watch_symbols") or []}
    rows = active_state.get("all_rows") or active_state.get("rows") or []
    row_map = {
        str(row.get("symbol", "")).upper(): row
        for row in rows
        if isinstance(row, dict) and str(row.get("symbol", "")).upper()
    }
    profile = str(active_state.get("profile") or "")
    now = datetime.now(timezone.utc)
    candidates: list[ShadowCandidate] = []
    for path in sorted(log_dir.glob(JOURNAL_GLOB)):
        symbol = _symbol_from_journal(path)
        if not symbol or (watch and symbol not in watch and symbol not in selected):
            continue
        ts, vector, context = _latest_decision_from_journal(path)
        if ts is None or not vector:
            continue
        age_sec = max(0.0, (now - ts).total_seconds())
        if age_sec > max_decision_age_sec:
            continue
        row = row_map.get(symbol, {})
        open_notional = _safe_float(row.get("open_notional"))
        candidate = ShadowCandidate(
            symbol=symbol,
            ts=ts.isoformat(),
            age_sec=age_sec,
            selected=symbol in selected,
            watch=symbol in watch or symbol in selected,
            current_profile=profile,
            strategy_primary=str(row.get("strategy_primary") or ""),
            gate_reason=str(row.get("gate_reason") or context.get("gate_reason") or ""),
            open_notional=open_notional,
            has_position=open_notional > 1.0,
            p_profit=model.predict_proba(vector),
            feature_vector=vector,
            decision_context=context,
        )
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            item.p_profit,
            1.0 if item.selected else 0.0,
            -item.age_sec,
        ),
        reverse=True,
    )
    return candidates


def build_watch_pool_strategy_summary(
    active_state: dict[str, Any],
    candidates: list[ShadowCandidate],
    *,
    top_per_strategy: int = 6,
    ml_positive_threshold: float = 0.56,
) -> dict[str, Any]:
    selected = {str(item).upper() for item in active_state.get("selected") or []}
    watch = {str(item).upper() for item in active_state.get("watch_symbols") or []} | selected
    raw_rows = active_state.get("all_rows") or active_state.get("rows") or []
    rows = [
        dict(row)
        for row in raw_rows
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() in watch
    ]
    annotate_rows_with_strategy_views(rows)
    candidate_map = {item.symbol.upper(): item for item in candidates}
    summary: dict[str, Any] = {}
    for strategy in STRATEGY_NAMES:
        strat_rows = [
            row
            for row in rows
            if strategy_score(row, strategy) > 0.0
        ]
        strat_rows.sort(
            key=lambda row: (
                float(
                    candidate_map.get(str(row.get("symbol", "")).upper()).p_profit
                    if candidate_map.get(str(row.get("symbol", "")).upper()) is not None
                    else 0.0
                ),
                bool(row.get("eligible")) or bool(row.get("keep_open")),
                strategy_score(row, strategy),
            ),
            reverse=True,
        )
        top_rows = strat_rows[: max(1, int(top_per_strategy))]
        gate_counts = Counter(str(row.get("gate_reason") or "") for row in strat_rows if str(row.get("gate_reason") or ""))
        buy_ready_count = sum(1 for row in strat_rows if bool(row.get("eligible")) or bool(row.get("keep_open")))
        ml_positive_count = 0
        top_candidates: list[dict[str, Any]] = []
        top_p_values: list[float] = []
        top_score_values: list[float] = []
        for row in top_rows:
            symbol = str(row.get("symbol", "")).upper()
            candidate = candidate_map.get(symbol)
            p_profit = float(candidate.p_profit) if candidate is not None else 0.0
            if p_profit >= ml_positive_threshold:
                ml_positive_count += 1
            top_p_values.append(p_profit)
            top_score_values.append(strategy_score(row, strategy))
            top_candidates.append(
                {
                    "symbol": symbol,
                    "selected": symbol in selected,
                    "eligible": bool(row.get("eligible")),
                    "keep_open": bool(row.get("keep_open")),
                    "gate_reason": str(row.get("gate_reason") or ""),
                    "strategy_score": round(strategy_score(row, strategy), 6),
                    "p_profit": round(p_profit, 6),
                    "open_notional": _safe_float(row.get("open_notional")),
                    "spread_bps": _safe_float(row.get("spread_bps")),
                }
            )
        summary[strategy] = {
            "candidate_count": len(strat_rows),
            "buy_ready_count": buy_ready_count,
            "ml_positive_count": ml_positive_count,
            "avg_top_p_profit": (
                sum(top_p_values) / float(len(top_p_values))
                if top_p_values
                else 0.0
            ),
            "avg_top_strategy_score": (
                sum(top_score_values) / float(len(top_score_values))
                if top_score_values
                else 0.0
            ),
            "dominant_gate_reasons": dict(gate_counts.most_common(3)),
            "top_candidates": top_candidates,
        }
    return summary


def build_trade_summary(
    samples: list[TradeSample],
    *,
    lookback_hours: float = 6.0,
    no_trade_samples: list[CounterfactualSample] | None = None,
) -> TradeSummary:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(lookback_hours)))
    recent = [sample for sample in samples if (_parse_ts(sample.exit_ts) or cutoff) >= cutoff]
    recent_no_trade = [
        sample
        for sample in (no_trade_samples or [])
        if (_parse_ts(sample.decision_ts) or cutoff) >= cutoff
    ]
    wins = [sample.net_pnl for sample in recent if sample.net_pnl > 0.0]
    losses = [sample.net_pnl for sample in recent if sample.net_pnl <= 0.0]
    exit_reasons = Counter(sample.exit_reason or "unknown" for sample in recent)
    last_exit_ts = recent[-1].exit_ts if recent else None
    exit_path_summary = _summarize_exit_path(recent)
    entry_path_summary = _summarize_entry_path(recent)
    no_trade_summary = _summarize_no_trade_path(recent_no_trade)
    strategy_breakdown: dict[str, dict[str, Any]] = {}
    symbol_breakdown: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[TradeSample]] = {}
    symbol_grouped: dict[str, list[TradeSample]] = {}
    for sample in recent:
        key = str(sample.strategy_at_entry or "").strip().lower() or "unknown"
        grouped.setdefault(key, []).append(sample)
        symbol = str(sample.symbol or "").strip().upper()
        if symbol:
            symbol_grouped.setdefault(symbol, []).append(sample)
    for strategy in list(STRATEGY_NAMES) + ["unknown"]:
        items = grouped.get(strategy, [])
        symbols = Counter(sample.symbol for sample in items)
        payload = _summarize_trade_items(items)
        payload["top_symbols"] = [symbol for symbol, _count in symbols.most_common(3)]
        strategy_breakdown[strategy] = payload
    for symbol, items in sorted(symbol_grouped.items()):
        strategies = Counter((sample.strategy_at_entry or "unknown") for sample in items)
        payload = _summarize_trade_items(items)
        payload["strategies"] = dict(sorted(strategies.items()))
        symbol_breakdown[symbol] = payload
    regime_breakdown = _group_segment_breakdown(
        recent,
        recent_no_trade,
        tag_getter=lambda sample: getattr(sample, "regime_tag", ""),
        allowed_tags=REGIME_TAGS,
    )
    session_breakdown = _group_segment_breakdown(
        recent,
        recent_no_trade,
        tag_getter=lambda sample: getattr(sample, "session_tag", ""),
        allowed_tags=SESSION_TAGS,
    )
    return TradeSummary(
        trade_count=len(recent),
        net_pnl=sum(sample.net_pnl for sample in recent),
        win_rate=(len(wins) / float(len(recent))) if recent else 0.0,
        avg_win=(sum(wins) / float(len(wins))) if wins else 0.0,
        avg_loss=(sum(losses) / float(len(losses))) if losses else 0.0,
        last_exit_ts=last_exit_ts,
        exit_reasons=dict(sorted(exit_reasons.items())),
        strategy_breakdown=strategy_breakdown,
        symbol_breakdown=symbol_breakdown,
        exit_path_summary=exit_path_summary,
        entry_path_summary=entry_path_summary,
        no_trade_summary=no_trade_summary,
        regime_breakdown=regime_breakdown,
        session_breakdown=session_breakdown,
    )


def export_samples_jsonl(path: Path, samples: list[TradeSample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(_trade_sample_to_json_line(sample) + "\n")


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def merge_env_file(path: Path) -> dict[str, str]:
    env = load_env_file(path)
    for key, value in env.items():
        os.environ.setdefault(key, value)
    return env
