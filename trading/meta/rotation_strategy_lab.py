from __future__ import annotations

from collections import Counter
import json
import math
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import rotation_replay_lab as replay_lab  # noqa: E402
from trading.meta.rotation_shadow import (  # noqa: E402
    NON_TUNABLE_BLOCK_REASONS,
    CounterfactualSample,
    TradeSample,
    extract_counterfactual_samples,
    extract_trade_samples,
    summarize_trade_sample_verification,
)


CORE_STRATEGIES: tuple[str, ...] = ("rebound", "staircase", "continuation", "breakout")
DEFAULT_CATALOG_FILE = REPO_ROOT / "configs" / "rotation_strategy_settings_catalog.yaml"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "logs" / "rotation_strategy_labs"
DEFAULT_ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
DEFAULT_RUNTIME_ENV_FILE = REPO_ROOT / "configs" / "rotation_meta_runtime.env"
DEFAULT_LOOKBACK_HOURS = 72.0
TECHNICAL_EXECUTION_FLAGS: tuple[str, ...] = (
    "exec_sell_qty_clamped_to_balance",
    "exec_sell_skipped_below_min_notional",
    "add_order_insufficient_balance",
)
MIXED_AGE_ENTRY_SPAN_MINUTES = 20.0
MAX_ACCEPTABLE_SCORE_REGRESSION = 1.5

DEFAULT_CANDIDATE_GRIDS: dict[str, tuple[float, ...]] = {
    "rebound": (0.0, 0.1, 0.2, 0.4, 0.6, 0.8),
    "staircase": (0.0, 1.0, 2.0, 4.0, 6.0),
    "continuation": (0.8, 1.0, 1.1, 1.15, 1.2, 1.35),
    "breakout": (3.0, 4.0, 5.0, 5.5, 6.0, 7.0, 8.0),
}


@dataclass
class StrategyLabSpec:
    name: str
    env_key: str
    runtime_path: str
    current_live_value: float
    next_candidate_value: float | None = None
    candidate_values: list[float] = field(default_factory=list)


@dataclass
class StrategyTrial:
    name: str
    setting_value: float
    overrides: dict[str, float] = field(default_factory=dict)


@dataclass
class StrategyCandidateResult:
    candidate_name: str
    strategy: str
    setting_value: float
    score: float
    tradeability_score: float
    est_net_pnl: float
    net_pnl_delta: float
    executed_trade_count: int
    added_trade_count: int
    added_winner_count: int
    blocked_trade_count: int
    blocked_positive_trade_count: int
    blocked_negative_trade_count: int
    remaining_edge_below_costs_count: int
    improved_exit_count: int
    remaining_early_exit_bugs: int
    remaining_weak_exit_reentries: int
    reason_counts: dict[str, int]
    parameter_overrides: dict[str, float]
    accepted: bool
    acceptance_reasons: list[str]

    @property
    def total_trade_count(self) -> int:
        return int(self.executed_trade_count + self.added_trade_count)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["total_trade_count"] = self.total_trade_count
        payload["score"] = replay_lab._round_float(self.score)
        payload["tradeability_score"] = replay_lab._round_float(self.tradeability_score)
        payload["est_net_pnl"] = replay_lab._round_float(self.est_net_pnl)
        payload["net_pnl_delta"] = replay_lab._round_float(self.net_pnl_delta)
        payload["reason_counts"] = {
            key: int(value)
            for key, value in sorted((self.reason_counts or {}).items())
        }
        payload["improved_exit_count"] = int(self.improved_exit_count)
        payload["remaining_early_exit_bugs"] = int(self.remaining_early_exit_bugs)
        payload["remaining_weak_exit_reentries"] = int(self.remaining_weak_exit_reentries)
        payload["parameter_overrides"] = {
            str(key): replay_lab._round_float(value)
            for key, value in sorted((self.parameter_overrides or {}).items())
        }
        payload["acceptance_reasons"] = list(self.acceptance_reasons)
        return payload


def load_strategy_catalog(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_strategy_specs(path: Path) -> list[StrategyLabSpec]:
    catalog = load_strategy_catalog(path)
    strategies = catalog.get("strategies") if isinstance(catalog.get("strategies"), dict) else {}
    specs: list[StrategyLabSpec] = []
    for name in CORE_STRATEGIES:
        row = strategies.get(name) if isinstance(strategies, dict) else {}
        primary = row.get("primary_setting") if isinstance(row.get("primary_setting"), dict) else {}
        diagnosis = row.get("diagnosis") if isinstance(row.get("diagnosis"), dict) else {}
        current_live_value = replay_lab._safe_float(primary.get("current_live_value"))
        next_candidate_value: float | None = None
        if "next_candidate_value" in primary:
            next_candidate_value = replay_lab._safe_float(primary.get("next_candidate_value"))
        elif (
            "next_candidate_value" in diagnosis
            and str(diagnosis.get("next_targeted_env") or "").strip() == str(primary.get("env") or "").strip()
        ):
            next_candidate_value = replay_lab._safe_float(diagnosis.get("next_candidate_value"))
        candidate_values = _candidate_grid(
            name=name,
            current_live_value=current_live_value,
            next_candidate_value=next_candidate_value,
        )
        specs.append(
            StrategyLabSpec(
                name=name,
                env_key=str(primary.get("env") or "").strip(),
                runtime_path=str(primary.get("runtime_path") or "").strip(),
                current_live_value=current_live_value,
                next_candidate_value=next_candidate_value,
                candidate_values=candidate_values,
            )
        )
    return specs


def _candidate_grid(
    *,
    name: str,
    current_live_value: float,
    next_candidate_value: float | None,
) -> list[float]:
    values = {replay_lab._round_float(item) for item in DEFAULT_CANDIDATE_GRIDS.get(name, ())}
    values.add(replay_lab._round_float(current_live_value))
    if next_candidate_value is not None:
        values.add(replay_lab._round_float(next_candidate_value))
    return sorted(values)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _feature(features: dict[str, float] | None, key: str, default: float = 0.0) -> float:
    payload = features if isinstance(features, dict) else {}
    return replay_lab._safe_float(payload.get(key), default)


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def strategy_edge_for_features(
    strategy: str,
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float] | None = None,
    decision_context: dict[str, Any] | None = None,
) -> tuple[float, str]:
    if strategy == "rebound":
        return _rebound_edge_for_features(
            features,
            setting_value,
            values=values,
            decision_context=decision_context,
        )
    if strategy == "staircase":
        return _staircase_edge_for_features(
            features,
            setting_value,
            values=values,
            decision_context=decision_context,
        )
    if strategy == "continuation":
        return _continuation_edge_for_features(
            features,
            setting_value,
            values=values,
            decision_context=decision_context,
        )
    if strategy == "breakout":
        return _breakout_edge_for_features(
            features,
            setting_value,
            values=values,
            decision_context=decision_context,
        )
    return 0.0, "unsupported_strategy"


def _rebound_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float] | None = None,
    decision_context: dict[str, Any] | None = None,
) -> tuple[float, str]:
    if values is None and not decision_context:
        return _legacy_rebound_edge_for_features(features, setting_value)
    return _configured_rebound_edge_for_features(
        features,
        setting_value,
        values=values or {},
        decision_context=decision_context or {},
    )


def _legacy_rebound_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
) -> tuple[float, str]:
    context_rebound_bps = max(0.0, _feature(features, "context_rebound_bps"))
    context_range_pos = _clamp(_feature(features, "context_range_pos", 1.0), 0.0, 1.0)
    structure_range_pos = _clamp(_feature(features, "structure_range_pos", context_range_pos), 0.0, 1.0)
    spread_bps = max(0.0, _feature(features, "spread_bps"))
    volume_z = _feature(features, "volume_z")
    trend_return_bps = _feature(features, "trend_return_bps")
    momentum_bps = _feature(features, "swing_momentum_bps", float("nan"))
    if math.isnan(momentum_bps) or abs(momentum_bps) < 1e-9:
        momentum_bps = max(_feature(features, "return_bps"), _feature(features, "structure_rebound_bps") * 0.10)
    if context_rebound_bps < 70.0:
        return 0.0, "rebound_context_too_small"
    if max(context_range_pos, structure_range_pos) > 0.62:
        return 0.0, "rebound_not_low_enough"
    if spread_bps > 14.0:
        return 0.0, "rebound_spread_too_high"
    if volume_z < -1.25:
        return 0.0, "rebound_volume_too_weak"
    if trend_return_bps < -300.0:
        return 0.0, "rebound_trend_too_weak"
    if momentum_bps < float(setting_value):
        return 0.0, "reversal_threshold"
    edge = context_rebound_bps * 0.05
    edge += max(0.0, momentum_bps) * 0.45
    edge += max(0.0, _feature(features, "structure_rebound_bps")) * 0.03
    edge -= max(0.0, spread_bps - 2.0) * 0.8
    edge -= max(0.0, (-trend_return_bps) - 180.0) * 0.02
    return max(12.0, edge), ""


def _configured_rebound_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float],
    decision_context: dict[str, Any],
) -> tuple[float, str]:
    del decision_context
    payload = features if isinstance(features, dict) else {}
    context_rebound_bps = max(0.0, _feature(payload, "context_rebound_bps"))
    context_range_pos = _clamp(_feature(payload, "context_range_pos", 1.0), 0.0, 1.0)
    structure_range_pos = _clamp(_feature(payload, "structure_range_pos", context_range_pos), 0.0, 1.0)
    spread_bps = max(0.0, _feature(payload, "spread_bps"))
    volume_z = _feature(payload, "volume_z")
    trend_return_bps = _feature(payload, "trend_return_bps")
    ret_bps = _feature(payload, "return_bps")
    momentum_bps = _feature(payload, "swing_momentum_bps", float("nan"))
    if math.isnan(momentum_bps) or abs(momentum_bps) < 1e-9:
        momentum_bps = max(_feature(payload, "return_bps"), _feature(payload, "structure_rebound_bps") * 0.10)

    min_context_rebound_bps = max(
        0.0,
        float(
            values.get(
                "swing_micro_rebound_min_context_rebound_bps",
                values.get("rebound_min_context_rebound_bps", 70.0),
            )
            or 70.0
        ),
    )
    max_context_range_pos = _clamp(
        float(
            values.get(
                "swing_micro_rebound_max_context_range_pos",
                values.get("rebound_max_context_range_pos", 0.62),
            )
            or 0.62
        ),
        0.0,
        1.0,
    )
    max_structure_range_pos = _clamp(
        float(values.get("rebound_max_structure_range_pos", max_context_range_pos) or max_context_range_pos),
        0.0,
        1.0,
    )
    max_spread_bps = max(
        0.0,
        float(
            values.get(
                "swing_micro_rebound_max_spread_bps",
                values.get("rebound_max_spread_bps", 14.0),
            )
            or 14.0
        ),
    )
    min_ret_bps = float(
        values.get(
            "swing_micro_rebound_min_ret_bps",
            values.get("rebound_min_ret_bps", -2.0),
        )
        or -2.0
    )
    confirm_rebound_bps = max(
        0.0,
        float(values.get("swing_micro_rebound_confirm_rebound_bps", 0.0) or 0.0),
    )
    confirm_min_ret_bps = float(
        values.get("swing_micro_rebound_confirm_min_ret_bps", 0.0) or 0.0
    )
    min_volume_z = float(values.get("rebound_min_volume_z", -1.25) or -1.25)
    min_trend_bps = float(values.get("rebound_min_trend_bps", -300.0) or -300.0)
    min_range_bps = max(0.0, float(values.get("swing_min_range_bps", 52.0) or 52.0))
    range_proxy_bps = max(
        0.0,
        _feature(payload, "swing_range_bps"),
        _feature(payload, "structure_extension_bps"),
        max(0.0, _feature(payload, "atr_bps")) * 3.0,
        abs(_feature(payload, "return_bps")) * 4.0,
        max(0.0, _feature(payload, "structure_drawdown_from_peak_bps")) * 1.2,
    )

    if range_proxy_bps < min_range_bps:
        return 0.0, "rebound_range_too_small"
    if context_rebound_bps < min_context_rebound_bps:
        return 0.0, "rebound_context_too_small"
    if context_range_pos > max_context_range_pos or structure_range_pos > max_structure_range_pos:
        return 0.0, "rebound_not_low_enough"
    if spread_bps > max_spread_bps:
        return 0.0, "rebound_spread_too_high"
    if ret_bps < min_ret_bps:
        return 0.0, "rebound_ret_too_low"
    if confirm_rebound_bps > 0.0 and context_rebound_bps >= confirm_rebound_bps and ret_bps < confirm_min_ret_bps:
        return 0.0, "rebound_wait_green"
    if volume_z < min_volume_z:
        return 0.0, "rebound_volume_too_weak"
    if trend_return_bps < min_trend_bps:
        return 0.0, "rebound_trend_too_weak"
    if momentum_bps < float(setting_value):
        return 0.0, "reversal_threshold"
    edge = context_rebound_bps * 0.05
    edge += max(0.0, momentum_bps) * 0.45
    edge += max(0.0, _feature(payload, "structure_rebound_bps")) * 0.03
    edge += max(0.0, range_proxy_bps - min_range_bps) * 0.04
    edge -= max(0.0, spread_bps - 2.0) * 0.8
    edge -= max(0.0, (-trend_return_bps) - 180.0) * 0.02
    edge -= max(0.0, max(context_range_pos, structure_range_pos) - 0.40) * 16.0
    return max(12.0, edge), ""


def _staircase_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float] | None = None,
    decision_context: dict[str, Any] | None = None,
) -> tuple[float, str]:
    if values is None and not decision_context:
        return _legacy_staircase_edge_for_features(features, setting_value)
    return _configured_staircase_edge_for_features(
        features,
        setting_value,
        values=values or {},
        decision_context=decision_context or {},
    )


def _legacy_staircase_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
) -> tuple[float, str]:
    spread_bps = max(0.0, _feature(features, "spread_bps"))
    volume_z = _feature(features, "volume_z")
    trend_return_bps = _feature(features, "trend_return_bps")
    ret_bps = _feature(features, "return_bps")
    slope_medium_bps = _feature(features, "structure_slope_medium_bps")
    slope_long_bps = _feature(features, "structure_slope_long_bps")
    drawdown_from_peak_bps = max(0.0, _feature(features, "structure_drawdown_from_peak_bps"))
    context_range_pos = _clamp(_feature(features, "context_range_pos", 1.0), 0.0, 1.0)
    if spread_bps <= 0.0 or spread_bps > 22.0:
        return 0.0, "staircase_spread"
    if volume_z < -1.0:
        return 0.0, "staircase_volume"
    if trend_return_bps < 0.0:
        return 0.0, "staircase_trend"
    if ret_bps < -12.0:
        return 0.0, "staircase_ret"
    if slope_medium_bps < 0.9:
        return 0.0, "staircase_slope_medium_floor"
    if slope_long_bps < 0.25:
        return 0.0, "staircase_slope_long_floor"
    if drawdown_from_peak_bps < float(setting_value):
        return 0.0, "staircase_drawdown"
    if context_range_pos > 0.94:
        return 0.0, "staircase_top_zone"
    edge = trend_return_bps * 0.16
    edge += max(0.0, slope_medium_bps) * 2.1
    edge += max(0.0, slope_long_bps) * 1.4
    edge += max(0.0, drawdown_from_peak_bps - float(setting_value)) * 0.20
    edge += max(0.0, ret_bps + 4.0) * 0.35
    edge -= max(0.0, spread_bps - 8.0) * 0.75
    edge -= max(0.0, context_range_pos - 0.78) * 90.0
    return max(18.0, edge), ""


def _configured_staircase_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float],
    decision_context: dict[str, Any],
) -> tuple[float, str]:
    payload = features if isinstance(features, dict) else {}
    phase = _phase_from_context(payload, decision_context)
    spread_bps = max(0.0, _feature(payload, "spread_bps"))
    volume_z = _feature(payload, "volume_z")
    trend_return_bps = _feature(payload, "trend_return_bps")
    ret_bps = _feature(payload, "return_bps")
    slope_short_bps = _feature(payload, "structure_slope_short_bps")
    slope_medium_bps = _feature(payload, "structure_slope_medium_bps")
    slope_long_bps = _feature(payload, "structure_slope_long_bps")
    drawdown_from_peak_bps = max(0.0, _feature(payload, "structure_drawdown_from_peak_bps"))
    context_drawdown_from_peak_bps = max(0.0, -_feature(payload, "context_drawdown_bps"))
    effective_drawdown_from_peak_bps = max(drawdown_from_peak_bps, context_drawdown_from_peak_bps)
    context_range_pos = _clamp(_feature(payload, "context_range_pos", 1.0), 0.0, 1.0)
    up_structure = _feature(payload, "up_structure") >= 0.5
    down_structure = _feature(payload, "down_structure") >= 0.5

    staircase_min_trend_bps = max(0.0, float(values.get("cont_staircase_min_trend_bps", 0.0) or 0.0))
    staircase_min_ret_bps = float(values.get("cont_staircase_min_ret_bps", -12.0) or -12.0)
    staircase_min_volume_z = float(values.get("cont_staircase_min_volume_z", -1.0) or -1.0)
    staircase_min_slope_medium_bps = float(
        values.get("cont_staircase_min_slope_medium_bps", setting_value) or setting_value
    )
    staircase_min_slope_long_bps = float(values.get("cont_staircase_min_slope_long_bps", 0.25) or 0.25)
    staircase_min_drawdown_from_peak_bps = max(0.0, float(setting_value))
    staircase_max_drawdown_from_peak_bps = max(
        staircase_min_drawdown_from_peak_bps,
        float(values.get("cont_staircase_max_drawdown_from_peak_bps", 120.0) or 120.0),
    )
    staircase_max_context_range_pos = _clamp(
        float(values.get("cont_staircase_max_context_range_pos", 0.94) or 0.94), 0.0, 1.0
    )
    staircase_max_spread_bps = max(0.0, float(values.get("cont_staircase_max_spread_bps", 22.0) or 22.0))
    staircase_require_up_structure = float(values.get("cont_staircase_require_up_structure", 0.0) or 0.0) >= 0.5
    stall_phase_ready = (
        phase == "stall"
        and not down_structure
        and (
            up_structure
            or slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.75)
        )
        and slope_short_bps >= -1.2
        and slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.70)
        and slope_long_bps >= max(-0.05, staircase_min_slope_long_bps - 0.14)
        and context_range_pos <= min(staircase_max_context_range_pos, 0.94)
        and effective_drawdown_from_peak_bps >= max(4.0, staircase_min_drawdown_from_peak_bps)
    )

    if phase and phase not in {"lift_off", "uptrend", "range", "rollover"} and not stall_phase_ready:
        return 0.0, "staircase_phase"
    if down_structure:
        return 0.0, "staircase_down_structure"
    if spread_bps <= 0.0 or spread_bps > staircase_max_spread_bps:
        return 0.0, "staircase_spread"
    if volume_z < staircase_min_volume_z:
        return 0.0, "staircase_volume"
    constructive_context = (
        not down_structure
        and (
            phase in {"uptrend", "range", "stall", "lift_off"}
            or up_structure
        )
    )
    effective_staircase_min_trend_bps = staircase_min_trend_bps
    if constructive_context and (
        slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.70)
        and slope_long_bps >= max(-0.05, staircase_min_slope_long_bps - 0.14)
    ):
        effective_staircase_min_trend_bps = max(0.0, staircase_min_trend_bps - 14.0)
    if stall_phase_ready:
        effective_staircase_min_trend_bps = max(0.0, effective_staircase_min_trend_bps - 10.0)
    if trend_return_bps < effective_staircase_min_trend_bps:
        return 0.0, "staircase_trend"
    if ret_bps < staircase_min_ret_bps:
        return 0.0, "staircase_ret"
    if slope_medium_bps < staircase_min_slope_medium_bps:
        return 0.0, "staircase_slope_medium_floor"
    effective_staircase_min_slope_long_bps = staircase_min_slope_long_bps
    if constructive_context and up_structure:
        effective_staircase_min_slope_long_bps = max(0.0, staircase_min_slope_long_bps - 0.06)
    if stall_phase_ready:
        effective_staircase_min_slope_long_bps = max(-0.05, effective_staircase_min_slope_long_bps - 0.08)
    if slope_long_bps < effective_staircase_min_slope_long_bps:
        return 0.0, "staircase_slope_long_floor"
    if effective_drawdown_from_peak_bps < staircase_min_drawdown_from_peak_bps:
        return 0.0, "staircase_drawdown"
    if effective_drawdown_from_peak_bps > staircase_max_drawdown_from_peak_bps:
        return 0.0, "staircase_drawdown_high"
    if context_range_pos > staircase_max_context_range_pos:
        return 0.0, "staircase_top_zone"
    if staircase_require_up_structure and not up_structure:
        return 0.0, "staircase_up_structure"
    if phase == "rollover" and slope_short_bps < -3.5:
        return 0.0, "staircase_rollover"
    edge = trend_return_bps * 0.16
    edge += max(0.0, slope_medium_bps) * 2.1
    edge += max(0.0, slope_long_bps) * 1.4
    edge += max(
        0.0,
        effective_drawdown_from_peak_bps - staircase_min_drawdown_from_peak_bps,
    ) * 0.20
    edge += max(0.0, ret_bps + 4.0) * 0.35
    edge -= max(0.0, spread_bps - 8.0) * 0.75
    edge -= max(0.0, context_range_pos - 0.78) * 90.0
    if phase == "rollover":
        edge -= 3.0
    elif stall_phase_ready:
        edge -= 2.5
    return max(18.0, edge), ""


def _continuation_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float] | None = None,
    decision_context: dict[str, Any] | None = None,
) -> tuple[float, str]:
    if values is None and not decision_context:
        return _legacy_continuation_edge_for_features(features, setting_value)
    return _configured_continuation_edge_for_features(
        features,
        setting_value,
        values=values or {},
        decision_context=decision_context or {},
    )


def _legacy_continuation_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
) -> tuple[float, str]:
    context_range_pos = _clamp(_feature(features, "context_range_pos", 1.0), 0.0, 1.0)
    structure_range_pos = _clamp(_feature(features, "structure_range_pos", context_range_pos), 0.0, 1.0)
    context_rebound_bps = max(0.0, _feature(features, "context_rebound_bps"))
    spread_bps = max(0.0, _feature(features, "spread_bps"))
    volume_z = _feature(features, "volume_z")
    ret_bps = _feature(features, "return_bps")
    trend_return_bps = _feature(features, "trend_return_bps")
    slope_short_bps = _feature(features, "structure_slope_short_bps")
    slope_medium_bps = _feature(features, "structure_slope_medium_bps")
    rebound_bps = _feature(features, "structure_rebound_bps")
    drawdown_from_peak_bps = max(
        0.0,
        max(_feature(features, "structure_drawdown_from_peak_bps"), -_feature(features, "context_drawdown_bps")),
    )
    if context_range_pos > 0.38:
        return 0.0, "continuation_context_too_high"
    if structure_range_pos > 0.55:
        return 0.0, "continuation_structure_too_high"
    if context_rebound_bps < 60.0:
        return 0.0, "continuation_rebound_too_small"
    if spread_bps <= 0.0 or spread_bps > 14.0:
        return 0.0, "continuation_spread"
    if volume_z < -1.1:
        return 0.0, "continuation_volume"
    if ret_bps < -1.0:
        return 0.0, "continuation_ret"
    if slope_short_bps < 2.4:
        return 0.0, "continuation_slope_short"
    if slope_medium_bps < float(setting_value):
        return 0.0, "continuation_slope_medium"
    if drawdown_from_peak_bps < 100.0:
        return 0.0, "continuation_drawdown"
    if trend_return_bps < -260.0:
        return 0.0, "continuation_trend"
    edge = context_rebound_bps * 0.09
    edge += max(0.0, slope_short_bps) * 1.7
    edge += max(0.0, slope_medium_bps) * 1.1
    edge += max(0.0, ret_bps + 2.0) * 0.5
    edge += max(0.0, rebound_bps) * 0.04
    edge -= max(0.0, spread_bps - 8.0) * 0.9
    edge -= max(0.0, (-trend_return_bps) - 180.0) * 0.03
    edge -= max(0.0, context_range_pos - 0.28) * 18.0
    return max(14.0, edge), ""


def _phase_from_context(
    features: dict[str, float] | None,
    decision_context: dict[str, Any] | None,
) -> str:
    raw = str((decision_context or {}).get("phase") or "").strip().lower()
    if raw and raw != "unknown":
        return raw
    payload = features if isinstance(features, dict) else {}
    for name in ("bottom", "lift_off", "range", "uptrend", "stall", "peak", "rollover", "downtrend"):
        if _feature(payload, f"phase_{name}") >= 0.5:
            return name
    up_structure = _feature(payload, "up_structure") >= 0.5
    down_structure = _feature(payload, "down_structure") >= 0.5
    if up_structure and not down_structure:
        return "uptrend"
    if down_structure and not up_structure:
        return "downtrend"
    return raw


def _configured_continuation_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float],
    decision_context: dict[str, Any],
) -> tuple[float, str]:
    payload = features if isinstance(features, dict) else {}
    phase = _phase_from_context(payload, decision_context)
    context_range_pos = _clamp(_feature(payload, "context_range_pos", 1.0), 0.0, 1.0)
    structure_range_pos = _clamp(_feature(payload, "structure_range_pos", context_range_pos), 0.0, 1.0)
    context_rebound_bps = max(0.0, _feature(payload, "context_rebound_bps"))
    spread_bps = max(0.0, _feature(payload, "spread_bps"))
    volume_z = _feature(payload, "volume_z")
    ret_bps = _feature(payload, "return_bps")
    trend_return_bps = _feature(payload, "trend_return_bps")
    slope_short_bps = _feature(payload, "structure_slope_short_bps")
    slope_medium_bps = _feature(payload, "structure_slope_medium_bps")
    slope_long_bps = _feature(payload, "structure_slope_long_bps")
    rebound_bps = _feature(payload, "structure_rebound_bps")
    up_structure = _feature(payload, "up_structure") >= 0.5
    down_structure = _feature(payload, "down_structure") >= 0.5
    context_drawdown_from_peak_bps = max(0.0, -_feature(payload, "context_drawdown_bps"))
    drawdown_from_peak_bps = max(0.0, _feature(payload, "structure_drawdown_from_peak_bps"))
    effective_drawdown_from_peak_bps = max(drawdown_from_peak_bps, context_drawdown_from_peak_bps)

    cont_trend_min_bps = float(values.get("cont_trend_min_bps", 5.0) or 5.0)
    cont_rebound_trigger_bps = max(0.0, float(values.get("cont_rebound_trigger_bps", 8.0) or 8.0))
    cont_min_volume_z = float(values.get("cont_min_volume_z", -999.0) or -999.0)
    cont_max_range_pos = _clamp(float(values.get("cont_max_range_pos", 0.82) or 0.82), 0.0, 1.0)
    cont_max_structure_range_pos = _clamp(
        float(values.get("cont_max_structure_range_pos", 1.0) or 1.0), 0.0, 1.0
    )
    range_continuation_max_range_pos = _clamp(
        float(values.get("cont_range_continuation_max_range_pos", 0.88) or 0.88),
        0.0,
        1.0,
    )
    staircase_min_trend_bps = max(0.0, float(values.get("cont_staircase_min_trend_bps", 0.0) or 0.0))
    staircase_min_ret_bps = float(values.get("cont_staircase_min_ret_bps", -12.0) or -12.0)
    staircase_min_volume_z = float(values.get("cont_staircase_min_volume_z", -999.0) or -999.0)
    staircase_min_slope_medium_bps = float(
        values.get("cont_staircase_min_slope_medium_bps", setting_value) or setting_value
    )
    staircase_min_slope_long_bps = float(values.get("cont_staircase_min_slope_long_bps", 0.0) or 0.0)
    staircase_min_drawdown_from_peak_bps = max(
        0.0, float(values.get("cont_staircase_min_drawdown_from_peak_bps", 0.0) or 0.0)
    )
    staircase_max_drawdown_from_peak_bps = max(
        staircase_min_drawdown_from_peak_bps,
        float(values.get("cont_staircase_max_drawdown_from_peak_bps", 120.0) or 120.0),
    )
    staircase_max_context_range_pos = _clamp(
        float(values.get("cont_staircase_max_context_range_pos", 1.0) or 1.0), 0.0, 1.0
    )
    staircase_max_spread_bps = max(0.0, float(values.get("cont_staircase_max_spread_bps", 22.0) or 22.0))
    staircase_require_up_structure = float(values.get("cont_staircase_require_up_structure", 0.0) or 0.0) >= 0.5
    early_liftoff_max_context_range_pos = float(
        values.get("cont_early_liftoff_max_context_range_pos", -1.0) or -1.0
    )
    if early_liftoff_max_context_range_pos <= 0.0:
        early_liftoff_max_context_range_pos = min(0.38, max(0.26, cont_max_range_pos * 0.46))
    early_liftoff_max_context_range_pos = _clamp(early_liftoff_max_context_range_pos, 0.0, 1.0)
    early_liftoff_max_structure_range_pos = _clamp(
        float(values.get("cont_early_liftoff_max_structure_range_pos", 0.55) or 0.55), 0.0, 1.0
    )
    early_liftoff_min_context_rebound_bps = float(
        values.get("cont_early_liftoff_min_context_rebound_bps", -1.0) or -1.0
    )
    if early_liftoff_min_context_rebound_bps <= 0.0:
        early_liftoff_min_context_rebound_bps = max(60.0, cont_rebound_trigger_bps * 6.0 + 20.0)
    early_liftoff_max_spread_bps = max(
        0.0,
        float(values.get("cont_early_liftoff_max_spread_bps", 14.0) or 14.0),
    )
    early_liftoff_min_volume_z = float(values.get("cont_early_liftoff_min_volume_z", -1.1) or -1.1)
    early_liftoff_min_ret_bps = float(values.get("cont_early_liftoff_min_ret_bps", -1.0) or -1.0)
    early_liftoff_min_slope_short_bps = float(
        values.get("cont_early_liftoff_min_slope_short_bps", 2.4) or 2.4
    )
    early_liftoff_min_slope_medium_bps = float(
        values.get("cont_early_liftoff_min_slope_medium_bps", 2.4) or 2.4
    )
    early_liftoff_min_drawdown_from_peak_bps = max(
        0.0,
        float(values.get("cont_early_liftoff_min_drawdown_from_peak_bps", 100.0) or 100.0),
    )
    early_liftoff_min_trend_bps = float(
        values.get("cont_early_liftoff_min_trend_bps", -260.0) or -260.0
    )

    def _staircase_edge() -> tuple[float, str]:
        stall_phase_ready = (
            phase == "stall"
            and not down_structure
            and (
                up_structure
                or slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.75)
            )
            and slope_short_bps >= -1.2
            and slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.70)
            and slope_long_bps >= max(-0.05, staircase_min_slope_long_bps - 0.14)
            and context_range_pos <= min(staircase_max_context_range_pos, 0.94)
            and effective_drawdown_from_peak_bps >= max(4.0, staircase_min_drawdown_from_peak_bps)
        )
        if phase and phase not in {"lift_off", "uptrend", "range", "rollover"} and not stall_phase_ready:
            return 0.0, "continuation_staircase_phase"
        if spread_bps <= 0.0 or spread_bps > staircase_max_spread_bps:
            return 0.0, "continuation_staircase_spread"
        if volume_z < staircase_min_volume_z:
            return 0.0, "continuation_staircase_volume"
        constructive_context = (
            not down_structure
            and (
                phase in {"uptrend", "range", "stall", "lift_off"}
                or up_structure
            )
        )
        effective_staircase_min_trend_bps = staircase_min_trend_bps
        if constructive_context and (
            slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.70)
            and slope_long_bps >= max(-0.05, staircase_min_slope_long_bps - 0.14)
        ):
            effective_staircase_min_trend_bps = max(0.0, staircase_min_trend_bps - 14.0)
        if stall_phase_ready:
            effective_staircase_min_trend_bps = max(0.0, effective_staircase_min_trend_bps - 10.0)
        if trend_return_bps < effective_staircase_min_trend_bps:
            return 0.0, "continuation_staircase_trend"
        if ret_bps < staircase_min_ret_bps:
            return 0.0, "continuation_staircase_ret"
        if slope_medium_bps < staircase_min_slope_medium_bps:
            return 0.0, "continuation_staircase_slope_medium"
        effective_staircase_min_slope_long_bps = staircase_min_slope_long_bps
        if constructive_context and up_structure:
            effective_staircase_min_slope_long_bps = max(0.0, staircase_min_slope_long_bps - 0.06)
        if stall_phase_ready:
            effective_staircase_min_slope_long_bps = max(
                -0.05, effective_staircase_min_slope_long_bps - 0.08
            )
        if slope_long_bps < effective_staircase_min_slope_long_bps:
            return 0.0, "continuation_staircase_slope_long"
        if effective_drawdown_from_peak_bps < staircase_min_drawdown_from_peak_bps:
            return 0.0, "continuation_staircase_drawdown_low"
        if effective_drawdown_from_peak_bps > staircase_max_drawdown_from_peak_bps:
            return 0.0, "continuation_staircase_drawdown_high"
        if context_range_pos > staircase_max_context_range_pos:
            return 0.0, "continuation_staircase_context_too_high"
        if staircase_require_up_structure and not up_structure:
            return 0.0, "continuation_staircase_up_structure"
        edge = trend_return_bps * 0.16
        edge += max(0.0, slope_medium_bps) * 2.1
        edge += max(0.0, slope_long_bps) * 1.4
        edge += max(
            0.0,
            effective_drawdown_from_peak_bps - staircase_min_drawdown_from_peak_bps,
        ) * 0.20
        edge += max(0.0, ret_bps + 4.0) * 0.35
        edge -= max(0.0, spread_bps - 8.0) * 0.75
        edge -= max(0.0, context_range_pos - 0.78) * 90.0
        if phase == "rollover":
            edge -= 3.0
        elif stall_phase_ready:
            edge -= 2.5
        return max(18.0, edge), ""

    def _range_continuation_edge() -> tuple[float, str]:
        if structure_range_pos > min(cont_max_structure_range_pos, range_continuation_max_range_pos):
            return 0.0, "continuation_range_too_high"
        if spread_bps <= 0.0 or spread_bps > 22.0:
            return 0.0, "continuation_spread"
        if volume_z < cont_min_volume_z:
            return 0.0, "continuation_volume"
        if trend_return_bps < cont_trend_min_bps:
            return 0.0, "continuation_trend"
        if effective_drawdown_from_peak_bps < 8.0:
            return 0.0, "continuation_drawdown"
        if max(0.0, rebound_bps, ret_bps) < cont_rebound_trigger_bps:
            return 0.0, "continuation_rebound_too_small"
        edge = trend_return_bps * 0.16
        edge += max(0.0, rebound_bps, ret_bps) * 0.55
        edge += max(0.0, slope_medium_bps) * 0.6
        edge += max(0.0, slope_long_bps) * 0.4
        edge -= max(0.0, spread_bps - 10.0) * 0.9
        edge -= max(0.0, context_range_pos - cont_max_range_pos) * 250.0
        edge -= max(0.0, structure_range_pos - 0.82) * 180.0
        if edge > 0.0:
            return max(12.0, edge), ""
        return 0.0, "continuation_range_no_edge"

    def _early_liftoff_edge() -> tuple[float, str]:
        if context_range_pos > early_liftoff_max_context_range_pos:
            return 0.0, "continuation_context_too_high"
        if structure_range_pos > min(early_liftoff_max_structure_range_pos, cont_max_structure_range_pos):
            return 0.0, "continuation_structure_too_high"
        if context_rebound_bps < early_liftoff_min_context_rebound_bps:
            return 0.0, "continuation_rebound_too_small"
        if spread_bps <= 0.0 or spread_bps > early_liftoff_max_spread_bps:
            return 0.0, "continuation_spread"
        if volume_z < max(cont_min_volume_z, early_liftoff_min_volume_z):
            return 0.0, "continuation_volume"
        if ret_bps < early_liftoff_min_ret_bps:
            return 0.0, "continuation_ret"
        if slope_short_bps < early_liftoff_min_slope_short_bps:
            return 0.0, "continuation_slope_short"
        if slope_medium_bps < max(float(setting_value), early_liftoff_min_slope_medium_bps):
            return 0.0, "continuation_slope_medium"
        if effective_drawdown_from_peak_bps < early_liftoff_min_drawdown_from_peak_bps:
            return 0.0, "continuation_drawdown"
        if trend_return_bps < early_liftoff_min_trend_bps:
            return 0.0, "continuation_trend"
        edge = context_rebound_bps * 0.09
        edge += max(0.0, slope_short_bps) * 1.7
        edge += max(0.0, slope_medium_bps) * 1.1
        edge += max(0.0, ret_bps + 2.0) * 0.5
        edge += max(0.0, rebound_bps) * 0.04
        edge -= max(0.0, spread_bps - 8.0) * 0.9
        edge -= max(0.0, (-trend_return_bps) - 180.0) * 0.03
        edge -= max(0.0, context_range_pos - 0.28) * 18.0
        return max(14.0, edge), ""

    def _trend_continuation_edge() -> tuple[float, str]:
        trend_floor_bps = cont_trend_min_bps * (0.35 if phase == "lift_off" else 1.0)
        max_spread_bps = max(22.0, staircase_max_spread_bps)
        if spread_bps <= 0.0 or spread_bps > max_spread_bps:
            return 0.0, "continuation_spread"
        if volume_z < cont_min_volume_z:
            return 0.0, "continuation_volume"
        if structure_range_pos > cont_max_structure_range_pos:
            return 0.0, "continuation_structure_too_high"
        if trend_return_bps < trend_floor_bps:
            return 0.0, "continuation_trend"
        if effective_drawdown_from_peak_bps < 8.0:
            return 0.0, "continuation_drawdown"
        if max(0.0, rebound_bps, ret_bps) < cont_rebound_trigger_bps:
            return 0.0, "continuation_rebound_too_small"
        edge = trend_return_bps * 0.16
        edge += max(0.0, rebound_bps, ret_bps) * 0.55
        edge += max(0.0, slope_medium_bps) * 0.6
        edge += max(0.0, slope_long_bps) * 0.4
        if phase == "lift_off":
            edge += max(0.0, slope_short_bps) * 0.6
            edge += max(0.0, rebound_bps) * 0.08
        edge -= max(0.0, spread_bps - 10.0) * 0.9
        edge -= max(0.0, context_range_pos - cont_max_range_pos) * 180.0
        edge -= max(0.0, structure_range_pos - cont_max_structure_range_pos) * 220.0
        if edge > 0.0:
            return max(12.0, edge), ""
        return 0.0, "continuation_no_edge"

    range_reason = ""
    if phase == "range":
        range_edge, range_reason = _range_continuation_edge()
        if range_edge > 0.0:
            return range_edge, ""

    trend_reason = ""
    if phase in {"lift_off", "uptrend", "stall"}:
        trend_edge, trend_reason = _trend_continuation_edge()
        if trend_edge > 0.0:
            return trend_edge, ""

    early_liftoff_reason = ""
    if phase in {"bottom", "lift_off", "range", ""}:
        early_liftoff_edge, early_liftoff_reason = _early_liftoff_edge()
        if early_liftoff_edge > 0.0:
            return early_liftoff_edge, ""

    staircase_reason = ""
    if phase in {"lift_off", "uptrend", "range", "rollover", "stall"} and not down_structure:
        staircase_edge, staircase_reason = _staircase_edge()
        if staircase_edge > 0.0:
            return staircase_edge, ""

    if early_liftoff_reason:
        return 0.0, early_liftoff_reason
    if trend_reason:
        return 0.0, trend_reason
    if staircase_reason:
        return 0.0, staircase_reason
    if range_reason:
        return 0.0, range_reason

    return _legacy_continuation_edge_for_features(payload, setting_value)


def _trial_overrides_for_report(
    baseline_values: dict[str, float],
    candidate_values: dict[str, float],
) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for key, value in sorted(candidate_values.items()):
        base_value = baseline_values.get(key)
        if base_value is not None and abs(float(value) - float(base_value)) <= 1e-9:
            continue
        env_name = replay_lab.PROFILE_KEY_ENV_MAP.get(key, key)
        overrides[str(env_name)] = replay_lab._round_float(value)
    return overrides


def _strategy_trials(spec: StrategyLabSpec) -> list[StrategyTrial]:
    primary_profile_key = replay_lab.ENV_PROFILE_KEY_MAP.get(spec.env_key, "")

    def _trial(name: str, setting_value: float, overrides: dict[str, float] | None = None) -> StrategyTrial:
        payload = dict(overrides or {})
        if primary_profile_key:
            payload.setdefault(primary_profile_key, float(setting_value))
        return StrategyTrial(name=name, setting_value=float(setting_value), overrides=payload)

    trials: list[StrategyTrial] = [
        _trial(name=f"{spec.name}_{replay_lab._round_float(value)}", setting_value=float(value))
        for value in spec.candidate_values
    ]
    seen = {
        (trial.setting_value, tuple(sorted(trial.overrides.items())))
        for trial in trials
    }
    if spec.name == "rebound":
        rebound_bundles = [
            _trial(
                name="rebound_tradeability_soft",
                setting_value=min(float(spec.current_live_value), 0.58),
                overrides={
                    "swing_min_range_bps": 48.0,
                    "swing_micro_rebound_max_spread_bps": 15.0,
                },
            ),
            _trial(
                name="rebound_tradeability_open",
                setting_value=0.5,
                overrides={
                    "swing_min_range_bps": 48.0,
                    "swing_micro_rebound_max_context_range_pos": 0.92,
                    "swing_micro_rebound_min_context_rebound_bps": 200.0,
                    "swing_micro_rebound_max_spread_bps": 15.0,
                    "swing_micro_rebound_min_ret_bps": -14.0,
                },
            ),
            _trial(
                name="rebound_tradeability_force",
                setting_value=0.30,
                overrides={
                    "swing_min_range_bps": 28.0,
                    "swing_micro_rebound_min_context_rebound_bps": 120.0,
                    "swing_micro_rebound_max_context_range_pos": 0.95,
                    "swing_micro_rebound_max_spread_bps": 20.0,
                    "swing_micro_rebound_min_ret_bps": -18.0,
                    "rebound_min_volume_z": -1.80,
                    "rebound_min_trend_bps": -420.0,
                    "entry_min_atr_to_cost_ratio": 0.66,
                    "entry_edge_bps": 1.6,
                    "entry_cost_coverage_ratio": 0.44,
                    "gate_cost_coverage_ratio": 0.30,
                },
            ),
        ]
        for trial in rebound_bundles:
            signature = (trial.setting_value, tuple(sorted(trial.overrides.items())))
            if signature in seen:
                continue
            seen.add(signature)
            trials.append(trial)
        return trials
    if spec.name == "staircase":
        staircase_bundles = [
            _trial(
                name="staircase_tradeability_soft",
                setting_value=min(float(spec.current_live_value), 0.0),
                overrides={
                    "cont_staircase_min_trend_bps": 28.0,
                    "cont_staircase_min_ret_bps": -10.0,
                    "cont_staircase_min_volume_z": -1.0,
                    "cont_staircase_min_slope_medium_bps": 0.95,
                    "cont_staircase_min_slope_long_bps": 0.18,
                    "cont_staircase_max_drawdown_from_peak_bps": 90.0,
                    "cont_staircase_max_context_range_pos": 0.99,
                    "cont_staircase_max_spread_bps": 22.0,
                    "cont_staircase_require_up_structure": 0.0,
                    "entry_edge_bps": 1.9,
                    "entry_cost_coverage_ratio": 0.50,
                    "gate_cost_coverage_ratio": 0.36,
                },
            ),
            _trial(
                name="staircase_tradeability_open",
                setting_value=0.0,
                overrides={
                    "cont_staircase_min_trend_bps": 18.0,
                    "cont_staircase_min_ret_bps": -10.0,
                    "cont_staircase_min_volume_z": -1.1,
                    "cont_staircase_min_slope_medium_bps": 0.85,
                    "cont_staircase_min_slope_long_bps": 0.10,
                    "cont_staircase_max_drawdown_from_peak_bps": 105.0,
                    "cont_staircase_max_context_range_pos": 1.0,
                    "cont_staircase_max_spread_bps": 22.0,
                    "cont_staircase_require_up_structure": 0.0,
                    "entry_edge_bps": 1.9,
                    "entry_cost_coverage_ratio": 0.50,
                    "gate_cost_coverage_ratio": 0.36,
                },
            ),
            _trial(
                name="staircase_tradeability_force",
                setting_value=0.0,
                overrides={
                    "cont_staircase_min_trend_bps": 8.0,
                    "cont_staircase_min_ret_bps": -12.0,
                    "cont_staircase_min_volume_z": -1.25,
                    "cont_staircase_min_slope_medium_bps": 0.70,
                    "cont_staircase_min_slope_long_bps": 0.0,
                    "cont_staircase_max_drawdown_from_peak_bps": 120.0,
                    "cont_staircase_max_context_range_pos": 1.0,
                    "cont_staircase_max_spread_bps": 22.0,
                    "cont_staircase_require_up_structure": 0.0,
                    "entry_edge_bps": 1.8,
                    "entry_cost_coverage_ratio": 0.48,
                    "gate_cost_coverage_ratio": 0.34,
                },
            ),
        ]
        for trial in staircase_bundles:
            signature = (trial.setting_value, tuple(sorted(trial.overrides.items())))
            if signature in seen:
                continue
            seen.add(signature)
            trials.append(trial)
        return trials
    if spec.name == "continuation":
        continuation_bundles = [
            _trial(
                name="continuation_tradeability_soft",
                setting_value=min(float(spec.current_live_value), 1.05),
                overrides={
                    "cont_trend_min_bps": 3.0,
                    "cont_rebound_trigger_bps": 6.0,
                    "cont_max_structure_range_pos": 1.0,
                    "cont_range_continuation_max_range_pos": 0.96,
                    "cont_min_volume_z": -1.0,
                    "cont_staircase_min_trend_bps": 35.0,
                    "cont_staircase_min_slope_medium_bps": 1.05,
                    "cont_staircase_min_slope_long_bps": 0.25,
                    "cont_staircase_max_context_range_pos": 0.97,
                    "cont_staircase_max_spread_bps": 20.0,
                    "cont_early_liftoff_max_context_range_pos": 0.46,
                    "cont_early_liftoff_max_structure_range_pos": 0.68,
                    "cont_early_liftoff_min_context_rebound_bps": 42.0,
                    "cont_early_liftoff_max_spread_bps": 16.0,
                    "cont_early_liftoff_min_slope_short_bps": 1.5,
                    "cont_early_liftoff_min_slope_medium_bps": 1.05,
                    "cont_early_liftoff_min_drawdown_from_peak_bps": 42.0,
                    "cont_early_liftoff_min_trend_bps": -240.0,
                    "entry_edge_bps": 1.9,
                    "entry_cost_coverage_ratio": 0.50,
                    "gate_cost_coverage_ratio": 0.36,
                },
            ),
            _trial(
                name="continuation_tradeability_open",
                setting_value=0.95,
                overrides={
                    "cont_trend_min_bps": 1.5,
                    "cont_rebound_trigger_bps": 4.0,
                    "cont_range_continuation_max_range_pos": 1.0,
                    "cont_min_volume_z": -1.2,
                    "cont_early_liftoff_max_context_range_pos": 0.58,
                    "cont_early_liftoff_max_structure_range_pos": 0.82,
                    "cont_early_liftoff_min_context_rebound_bps": 28.0,
                    "cont_early_liftoff_max_spread_bps": 18.0,
                    "cont_early_liftoff_min_slope_short_bps": 0.9,
                    "cont_early_liftoff_min_slope_medium_bps": 0.95,
                    "cont_early_liftoff_min_drawdown_from_peak_bps": 18.0,
                    "cont_early_liftoff_min_trend_bps": -320.0,
                },
            ),
            _trial(
                name="continuation_tradeability_force",
                setting_value=0.85,
                overrides={
                    "cont_trend_min_bps": 0.0,
                    "cont_rebound_trigger_bps": 2.0,
                    "cont_max_structure_range_pos": 1.0,
                    "cont_range_continuation_max_range_pos": 1.0,
                    "cont_min_volume_z": -1.4,
                    "cont_staircase_min_trend_bps": 8.0,
                    "cont_staircase_min_ret_bps": -10.0,
                    "cont_staircase_min_volume_z": -1.4,
                    "cont_staircase_min_slope_medium_bps": 0.85,
                    "cont_staircase_min_slope_long_bps": 0.05,
                    "cont_staircase_max_context_range_pos": 1.0,
                    "cont_staircase_max_spread_bps": 22.0,
                    "cont_early_liftoff_max_context_range_pos": 0.72,
                    "cont_early_liftoff_max_structure_range_pos": 0.95,
                    "cont_early_liftoff_min_context_rebound_bps": 14.0,
                    "cont_early_liftoff_max_spread_bps": 22.0,
                    "cont_early_liftoff_min_slope_short_bps": 0.45,
                    "cont_early_liftoff_min_slope_medium_bps": 0.65,
                    "cont_early_liftoff_min_drawdown_from_peak_bps": 6.0,
                    "cont_early_liftoff_min_trend_bps": -480.0,
                    "entry_edge_bps": 1.0,
                    "entry_cost_coverage_ratio": 0.40,
                    "gate_cost_coverage_ratio": 0.25,
                },
            ),
        ]
        for trial in continuation_bundles:
            signature = (trial.setting_value, tuple(sorted(trial.overrides.items())))
            if signature in seen:
                continue
            seen.add(signature)
            trials.append(trial)
        return trials
    if spec.name == "breakout":
        breakout_bundles = [
            _trial(
                name="breakout_exit_relief_soft",
                setting_value=7.0,
                overrides={
                    "late_entry_block_context_range_pos": 0.75,
                    "late_entry_block_structure_range_pos": 0.84,
                    "late_entry_block_max_context_drawdown_bps": 56.0,
                    "late_entry_block_min_trend_return_bps": 45.0,
                    "late_entry_block_min_return_bps": 4.0,
                    "hard_stop_loss_bps": 98.0,
                    "failed_start_min_bars": 8.0,
                    "failed_start_max_bars": 9.0,
                    "failed_start_min_rebound_bps": 24.0,
                    "failed_start_loss_bps": 60.0,
                    "time_break_even_floor_bars": 22.0,
                    "min_exit_profit_bps": 12.0,
                    "trailing_activation_bps": 40.0,
                    "trailing_stop_bps": 14.0,
                    "reentry_cooldown_bars_after_weak_exit": 9.0,
                },
            ),
            _trial(
                name="breakout_exit_relief_open",
                setting_value=7.0,
                overrides={
                    "late_entry_block_context_range_pos": 0.75,
                    "late_entry_block_structure_range_pos": 0.84,
                    "late_entry_block_max_context_drawdown_bps": 56.0,
                    "late_entry_block_min_trend_return_bps": 45.0,
                    "late_entry_block_min_return_bps": 4.0,
                    "hard_stop_loss_bps": 104.0,
                    "failed_start_min_bars": 9.0,
                    "failed_start_max_bars": 10.0,
                    "failed_start_min_rebound_bps": 22.0,
                    "failed_start_loss_bps": 64.0,
                    "time_break_even_floor_bars": 24.0,
                    "min_exit_profit_bps": 12.0,
                    "trailing_activation_bps": 42.0,
                    "trailing_stop_bps": 15.0,
                    "reentry_cooldown_bars_after_weak_exit": 10.0,
                    "campaign_hold_enabled": 1.0,
                    "campaign_hold_min_bars": 3.0,
                    "campaign_hold_min_profit_bps": 4.0,
                    "campaign_hold_min_trend_bps": 36.0,
                    "campaign_hold_max_drawdown_from_peak_bps": 60.0,
                },
            ),
            _trial(
                name="breakout_exit_relief_force",
                setting_value=7.0,
                overrides={
                    "late_entry_block_context_range_pos": 0.75,
                    "late_entry_block_structure_range_pos": 0.84,
                    "late_entry_block_max_context_drawdown_bps": 56.0,
                    "late_entry_block_min_trend_return_bps": 45.0,
                    "late_entry_block_min_return_bps": 4.0,
                    "hard_stop_loss_bps": 112.0,
                    "failed_start_min_bars": 10.0,
                    "failed_start_max_bars": 12.0,
                    "failed_start_min_rebound_bps": 18.0,
                    "failed_start_loss_bps": 72.0,
                    "time_break_even_floor_bars": 28.0,
                    "min_exit_profit_bps": 14.0,
                    "trailing_activation_bps": 46.0,
                    "trailing_stop_bps": 18.0,
                    "reentry_cooldown_bars_after_weak_exit": 10.0,
                    "campaign_hold_enabled": 1.0,
                    "campaign_hold_min_bars": 4.0,
                    "campaign_hold_min_profit_bps": 6.0,
                    "campaign_hold_min_trend_bps": 45.0,
                    "campaign_hold_max_drawdown_from_peak_bps": 66.0,
                },
            ),
        ]
        for trial in breakout_bundles:
            signature = (trial.setting_value, tuple(sorted(trial.overrides.items())))
            if signature in seen:
                continue
            seen.add(signature)
            trials.append(trial)
    return trials


def _breakout_edge_for_features(
    features: dict[str, float] | None,
    setting_value: float,
    *,
    values: dict[str, float] | None = None,
    decision_context: dict[str, Any] | None = None,
) -> tuple[float, str]:
    breakout_up_bps = _feature(features, "breakout_up_bps", float("nan"))
    if math.isnan(breakout_up_bps) or abs(breakout_up_bps) < 1e-9:
        breakout_up_bps = max(
            0.0,
            _feature(features, "return_bps")
            + max(0.0, _feature(features, "structure_extension_bps")) * 0.04
            + max(0.0, _feature(features, "trend_return_bps")) * 0.01,
        )
    if breakout_up_bps < float(setting_value):
        return 0.0, "breakout_trigger"
    profile_values = values or {}
    context_range_pos = max(0.0, min(1.0, _feature(features, "context_range_pos")))
    context_rebound_bps = max(0.0, _feature(features, "context_rebound_bps"))
    trend_return_bps = _feature(features, "trend_return_bps")
    spread_bps = max(0.0, _feature(features, "spread_bps"))
    volume_z = _feature(features, "volume_z")
    bottom_countertrend_block_max_context_range_pos = max(
        0.0,
        min(
            1.0,
            replay_lab._safe_float(
                profile_values.get("breakout_bottom_countertrend_block_max_context_range_pos"),
                0.22,
            ),
        ),
    )
    bottom_countertrend_block_max_context_rebound_bps = max(
        0.0,
        replay_lab._safe_float(
            profile_values.get("breakout_bottom_countertrend_block_max_context_rebound_bps"),
            140.0,
        ),
    )
    bottom_countertrend_block_max_trend_return_bps = replay_lab._safe_float(
        profile_values.get("breakout_bottom_countertrend_block_max_trend_return_bps"),
        -55.0,
    )
    top_zone_block_min_context_range_pos = max(
        0.0,
        min(
            1.0,
            replay_lab._safe_float(
                profile_values.get("breakout_top_zone_block_min_context_range_pos"),
                0.98,
            ),
        ),
    )
    top_zone_block_max_context_rebound_bps = max(
        0.0,
        replay_lab._safe_float(
            profile_values.get("breakout_top_zone_block_max_context_rebound_bps"),
            90.0,
        ),
    )
    top_zone_block_min_volume_z = replay_lab._safe_float(
        profile_values.get("breakout_top_zone_block_min_volume_z"),
        0.4,
    )
    if (
        bottom_countertrend_block_max_context_range_pos > 0.0
        and context_range_pos <= bottom_countertrend_block_max_context_range_pos
        and bottom_countertrend_block_max_context_rebound_bps > 0.0
        and context_rebound_bps <= bottom_countertrend_block_max_context_rebound_bps
        and trend_return_bps <= bottom_countertrend_block_max_trend_return_bps
    ):
        return 0.0, "breakout_bottom_countertrend_block"
    if (
        top_zone_block_max_context_rebound_bps > 0.0
        and context_range_pos >= top_zone_block_min_context_range_pos
        and context_rebound_bps <= top_zone_block_max_context_rebound_bps
        and volume_z < top_zone_block_min_volume_z
    ):
        return 0.0, "breakout_top_zone_block"
    thin_rebound_block_context_range_pos = max(
        0.0,
        min(
            1.0,
            replay_lab._safe_float(
                profile_values.get("breakout_thin_rebound_block_context_range_pos"),
                0.60,
            ),
        ),
    )
    thin_rebound_block_context_rebound_bps = max(
        0.0,
        replay_lab._safe_float(
            profile_values.get("breakout_thin_rebound_block_context_rebound_bps"),
            400.0,
        ),
    )
    thin_rebound_block_min_spread_bps = max(
        0.0,
        replay_lab._safe_float(
            profile_values.get("breakout_thin_rebound_block_min_spread_bps"),
            18.0,
        ),
    )
    if (
        thin_rebound_block_context_rebound_bps > 0.0
        and context_range_pos >= thin_rebound_block_context_range_pos
        and context_rebound_bps >= thin_rebound_block_context_rebound_bps
        and spread_bps >= thin_rebound_block_min_spread_bps
    ):
        return 0.0, "breakout_thin_rebound_spread_block"
    late_rebound_block_context_range_pos = max(
        0.0,
        min(
            1.0,
            replay_lab._safe_float(
                profile_values.get("breakout_late_rebound_block_context_range_pos"),
                0.72,
            ),
        ),
    )
    late_rebound_block_context_rebound_bps = max(
        0.0,
        replay_lab._safe_float(
            profile_values.get("breakout_late_rebound_block_context_rebound_bps"),
            1400.0,
        ),
    )
    late_rebound_block_min_volume_z = replay_lab._safe_float(
        profile_values.get("breakout_late_rebound_block_min_volume_z"),
        0.35,
    )
    if (
        late_rebound_block_context_rebound_bps > 0.0
        and context_range_pos >= late_rebound_block_context_range_pos
        and context_rebound_bps >= late_rebound_block_context_rebound_bps
        and volume_z < late_rebound_block_min_volume_z
    ):
        return 0.0, "breakout_late_rebound_block"
    mid_rebound_block_context_range_pos = max(
        0.0,
        min(
            1.0,
            replay_lab._safe_float(
                profile_values.get("breakout_mid_rebound_block_context_range_pos"),
                0.48,
            ),
        ),
    )
    mid_rebound_block_context_rebound_bps = max(
        0.0,
        replay_lab._safe_float(
            profile_values.get("breakout_mid_rebound_block_context_rebound_bps"),
            760.0,
        ),
    )
    mid_rebound_block_min_volume_z = replay_lab._safe_float(
        profile_values.get("breakout_mid_rebound_block_min_volume_z"),
        0.0,
    )
    if (
        mid_rebound_block_context_rebound_bps > 0.0
        and context_range_pos >= mid_rebound_block_context_range_pos
        and context_rebound_bps >= mid_rebound_block_context_rebound_bps
        and volume_z < mid_rebound_block_min_volume_z
    ):
        return 0.0, "breakout_mid_rebound_block"
    edge = max(12.0, breakout_up_bps)
    edge -= max(0.0, _feature(features, "spread_bps") - 10.0) * 0.6
    return max(0.0, edge), ""


def _filter_trades_for_strategy(
    trades: list[TradeSample],
    strategy: str,
    lookback_hours: float,
) -> list[TradeSample]:
    recent = replay_lab._recent_trade_samples(trades, lookback_hours)
    return [
        sample
        for sample in recent
        if str(sample.strategy_at_entry or "").strip().lower() == strategy
    ]


def _filter_no_trade_samples_for_strategy(
    samples: list[CounterfactualSample],
    strategy: str,
    lookback_hours: float,
) -> list[CounterfactualSample]:
    recent = replay_lab._recent_counterfactual_samples(samples, lookback_hours)
    return [
        sample
        for sample in recent
        if str(sample.strategy_primary or "").strip().lower() == strategy
    ]


def _technical_issue_classes(sample: TradeSample) -> list[str]:
    flags = {
        str(flag).strip().lower()
        for flag in (getattr(sample, "technical_flags", None) or [])
        if str(flag).strip()
    }
    issues: list[str] = []
    if flags.intersection(TECHNICAL_EXECUTION_FLAGS):
        issues.append("execution_reconcile_error")
    if "mid_trade_reload" in flags:
        issues.append("mid_trade_reload")
    entry_fill_count = max(1, int(getattr(sample, "entry_fill_count", 1) or 1))
    entry_fill_span_minutes = max(0.0, float(getattr(sample, "entry_fill_span_minutes", 0.0) or 0.0))
    if entry_fill_count > 1 and entry_fill_span_minutes >= MIXED_AGE_ENTRY_SPAN_MINUTES:
        issues.append("mixed_age_exit")
    return issues


def _candidate_acceptance_reasons(
    candidate: StrategyCandidateResult,
    baseline: StrategyCandidateResult,
) -> list[str]:
    reasons: list[str] = []
    if candidate.est_net_pnl >= baseline.est_net_pnl - 0.03:
        reasons.append("kept_pnl_in_range")
    if candidate.total_trade_count > baseline.total_trade_count:
        reasons.append("higher_trade_count")
    if candidate.added_winner_count > baseline.added_winner_count:
        reasons.append("more_added_winners")
    if candidate.remaining_edge_below_costs_count < baseline.remaining_edge_below_costs_count:
        reasons.append("fewer_cost_gate_blocks")
    if candidate.tradeability_score > baseline.tradeability_score + 0.1:
        reasons.append("higher_tradeability_score")
    if candidate.improved_exit_count > baseline.improved_exit_count:
        reasons.append("more_improved_exits")
    if candidate.remaining_early_exit_bugs < baseline.remaining_early_exit_bugs:
        reasons.append("fewer_early_exit_bugs")
    if candidate.remaining_weak_exit_reentries < baseline.remaining_weak_exit_reentries:
        reasons.append("fewer_weak_exit_reentries")
    return reasons


def _candidate_passes_safety_gate(
    candidate: StrategyCandidateResult,
    baseline: StrategyCandidateResult,
) -> bool:
    if candidate.score < baseline.score - MAX_ACCEPTABLE_SCORE_REGRESSION:
        return False
    weak_exit_reentry_cap = baseline.remaining_weak_exit_reentries + max(
        4,
        candidate.added_trade_count // 3,
    )
    return candidate.remaining_weak_exit_reentries <= weak_exit_reentry_cap


def _evaluate_strategy_candidate(
    *,
    strategy: str,
    candidate_name: str,
    setting_value: float,
    candidate_overrides: dict[str, float],
    baseline_policy: replay_lab.PolicyCandidate,
    trades: list[TradeSample],
    no_trade_samples: list[CounterfactualSample],
    unit_notional: float,
    baseline_est_net_pnl: float,
) -> StrategyCandidateResult:
    candidate_values = dict(baseline_policy.values or {})
    candidate_values.update(candidate_overrides or {})
    states: dict[str, replay_lab.SymbolState] = {}
    est_net_pnl = 0.0
    executed_trade_count = 0
    added_trade_count = 0
    added_winner_count = 0
    blocked_trade_count = 0
    blocked_positive_trade_count = 0
    blocked_negative_trade_count = 0
    remaining_edge_below_costs_count = 0
    improved_exit_count = 0
    remaining_early_exit_bugs = 0
    remaining_weak_exit_reentries = 0
    reason_counts: dict[str, int] = {}
    merged_events: list[tuple[datetime, str, TradeSample | CounterfactualSample]] = []

    for sample in trades:
        ts = replay_lab._parse_ts(sample.entry_ts)
        if ts is not None:
            merged_events.append((ts, "trade", sample))
    for sample in no_trade_samples:
        ts = replay_lab._parse_ts(sample.decision_ts)
        if ts is not None:
            merged_events.append((ts, "counterfactual", sample))
    merged_events.sort(key=lambda item: (item[0], item[1]))

    for event_ts, kind, item in merged_events:
        symbol = str(getattr(item, "symbol", "") or "").strip().upper()
        if not symbol:
            continue
        state = states.setdefault(symbol, replay_lab.SymbolState())
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
            synthetic_edge, strategy_reason = strategy_edge_for_features(
                strategy,
                sample.features,
                setting_value,
                values=candidate_values,
                decision_context={},
            )
            if strategy_reason:
                blocked_trade_count += 1
                reason_counts[strategy_reason] = reason_counts.get(strategy_reason, 0) + 1
                if float(sample.net_pnl or 0.0) <= 0.0:
                    blocked_negative_trade_count += 1
                else:
                    blocked_positive_trade_count += 1
                continue
            features = dict(sample.features or {})
            features["edge_bps_effective"] = max(
                synthetic_edge,
                replay_lab._safe_float(features.get("edge_bps_effective")),
            )
            entry_price = max(
                0.0,
                float(sample.buy_notional or 0.0) / max(1e-12, float(sample.buy_qty or 0.0)),
            )
            block_reason = replay_lab._entry_block_reason_for_features(
                features=features,
                expected_cost_bps=replay_lab._safe_float(features.get("expected_cost_bps")),
                entry_price=entry_price,
                state=state,
                values=candidate_values,
                ts=event_ts,
                require_gate_check=True,
            )
            if block_reason:
                blocked_trade_count += 1
                reason_counts[block_reason] = reason_counts.get(block_reason, 0) + 1
                if block_reason == "edge_below_costs":
                    remaining_edge_below_costs_count += 1
                if float(sample.net_pnl or 0.0) <= 0.0:
                    blocked_negative_trade_count += 1
                else:
                    blocked_positive_trade_count += 1
                continue
            est_trade_pnl, est_exit_ts, improved_exit, exit_mode = replay_lab._simulate_exit_for_trade(
                sample,
                candidate_values=candidate_values,
                baseline_values=baseline_policy.values,
            )
            est_net_pnl += est_trade_pnl
            executed_trade_count += 1
            if improved_exit:
                improved_exit_count += 1
                reason_counts[str(exit_mode or "improved_exit")] = (
                    reason_counts.get(str(exit_mode or "improved_exit"), 0) + 1
                )
            elif (
                replay_lab._early_exit_signal(sample)
                or replay_lab._failed_start_recovery_signal(sample)
                or replay_lab._trailing_missed_run_signal(sample)
                or replay_lab._micro_pop_loss_run_signal(sample)
                or replay_lab._shakeout_then_run_signal(sample)
            ):
                remaining_early_exit_bugs += 1
            if est_exit_ts is not None:
                state.open_until = est_exit_ts
            state.last_exit_price = replay_lab._trade_exit_price(sample)
            if (
                not improved_exit
                and str(sample.exit_reason or "").strip().lower()
                in replay_lab.EXIT_WEAK_REASONS
                and est_exit_ts is not None
            ):
                state.last_weak_exit_ts = est_exit_ts
            elif improved_exit:
                state.last_weak_exit_ts = None
            else:
                state.last_weak_exit_ts = None
            continue

        sample = item
        if str(sample.block_reason or "").strip().lower() in NON_TUNABLE_BLOCK_REASONS:
            reason_counts[str(sample.block_reason or "").strip().lower() or "non_tunable"] = (
                reason_counts.get(str(sample.block_reason or "").strip().lower() or "non_tunable", 0) + 1
            )
            continue
        synthetic_edge, strategy_reason = strategy_edge_for_features(
            strategy,
            sample.features,
            setting_value,
            values=candidate_values,
            decision_context=sample.decision_context,
        )
        if strategy_reason:
            reason_counts[strategy_reason] = reason_counts.get(strategy_reason, 0) + 1
            continue
        features = dict(sample.features or {})
        features["edge_bps_effective"] = max(
            synthetic_edge,
            replay_lab._safe_float(features.get("edge_bps_effective")),
        )
        candidate_block = replay_lab._entry_block_reason_for_features(
            features=features,
            expected_cost_bps=replay_lab._safe_float(sample.decision_context.get("expected_cost_bps")),
            entry_price=float(sample.anchor_price or 0.0),
            state=state,
            values=candidate_values,
            ts=event_ts,
            require_gate_check=True,
        )
        if candidate_block:
            reason_counts[candidate_block] = reason_counts.get(candidate_block, 0) + 1
            if candidate_block == "edge_below_costs":
                remaining_edge_below_costs_count += 1
            continue
        synth_pnl, synth_exit_ts, horizon = replay_lab._estimate_synthetic_trade_pnl(
            sample,
            unit_notional=unit_notional,
            candidate_values=candidate_values,
            baseline_values=baseline_policy.values,
        )
        if synth_exit_ts is None:
            reason_counts["synthetic_exit_missing"] = reason_counts.get("synthetic_exit_missing", 0) + 1
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

    tradeability_score = (
        float(executed_trade_count + added_trade_count)
        + (0.35 * float(added_trade_count))
        + (0.65 * float(added_winner_count))
        + (0.90 * float(improved_exit_count))
        - (0.60 * float(blocked_positive_trade_count))
        - (0.03 * float(remaining_edge_below_costs_count))
        - (1.20 * float(remaining_early_exit_bugs))
        - (1.00 * float(remaining_weak_exit_reentries))
    )
    score = (est_net_pnl * 20.0) + tradeability_score
    return StrategyCandidateResult(
        candidate_name=candidate_name,
        strategy=strategy,
        setting_value=float(setting_value),
        score=score,
        tradeability_score=tradeability_score,
        est_net_pnl=est_net_pnl,
        net_pnl_delta=est_net_pnl - baseline_est_net_pnl,
        executed_trade_count=executed_trade_count,
        added_trade_count=added_trade_count,
        added_winner_count=added_winner_count,
        blocked_trade_count=blocked_trade_count,
        blocked_positive_trade_count=blocked_positive_trade_count,
        blocked_negative_trade_count=blocked_negative_trade_count,
        remaining_edge_below_costs_count=remaining_edge_below_costs_count,
        improved_exit_count=improved_exit_count,
        remaining_early_exit_bugs=remaining_early_exit_bugs,
        remaining_weak_exit_reentries=remaining_weak_exit_reentries,
        reason_counts=dict(sorted(reason_counts.items())),
        parameter_overrides=_trial_overrides_for_report(
            baseline_policy.values,
            candidate_values,
        ),
        accepted=False,
        acceptance_reasons=[],
    )


def run_strategy_labs(
    *,
    log_dir: Path = REPO_ROOT / "logs",
    catalog_file: Path = DEFAULT_CATALOG_FILE,
    active_file: Path = DEFAULT_ACTIVE_FILE,
    runtime_env_file: Path = DEFAULT_RUNTIME_ENV_FILE,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    strategy_filter: str = "",
) -> dict[str, Any]:
    baseline_policy = replay_lab._load_live_policy(
        active_file=active_file,
        runtime_env_file=runtime_env_file,
    )
    specs = load_strategy_specs(catalog_file)
    selected = str(strategy_filter or "").strip().lower()
    if selected:
        specs = [spec for spec in specs if spec.name == selected]
    all_trades = extract_trade_samples(log_dir, mirror_verification="annotate")
    all_trade_verification = summarize_trade_sample_verification(all_trades)
    all_no_trade_samples = extract_counterfactual_samples(log_dir, lookback_hours=lookback_hours)
    output_dir.mkdir(parents=True, exist_ok=True)

    strategy_reports: list[dict[str, Any]] = []
    for spec in specs:
        primary_profile_key = replay_lab.ENV_PROFILE_KEY_MAP.get(spec.env_key, "")
        effective_live_value = replay_lab._safe_float(
            baseline_policy.values.get(primary_profile_key),
            spec.current_live_value,
        )
        effective_spec = replace(spec, current_live_value=effective_live_value)
        raw_trades = _filter_trades_for_strategy(all_trades, spec.name, lookback_hours)
        trade_verification = summarize_trade_sample_verification(raw_trades)
        verified_trades = [sample for sample in raw_trades if bool(sample.mirror_verified)]
        technical_exclusion_breakdown: Counter[str] = Counter()
        technical_excluded_trade_count = 0
        trades: list[TradeSample] = []
        for sample in verified_trades:
            issues = _technical_issue_classes(sample)
            if issues:
                technical_exclusion_breakdown.update(issues)
                technical_excluded_trade_count += 1
                continue
            trades.append(sample)
        no_trade_samples = _filter_no_trade_samples_for_strategy(all_no_trade_samples, spec.name, lookback_hours)
        unit_notional = replay_lab._default_unit_notional(trades)
        baseline_result = _evaluate_strategy_candidate(
            strategy=effective_spec.name,
            candidate_name="baseline",
            setting_value=effective_spec.current_live_value,
            candidate_overrides={},
            baseline_policy=baseline_policy,
            trades=trades,
            no_trade_samples=no_trade_samples,
            unit_notional=unit_notional,
            baseline_est_net_pnl=0.0,
        )
        baseline_result.net_pnl_delta = 0.0
        candidates: list[StrategyCandidateResult] = []
        for trial in _strategy_trials(effective_spec):
            result = _evaluate_strategy_candidate(
                strategy=effective_spec.name,
                candidate_name=trial.name,
                setting_value=trial.setting_value,
                candidate_overrides=trial.overrides,
                baseline_policy=baseline_policy,
                trades=trades,
                no_trade_samples=no_trade_samples,
                unit_notional=unit_notional,
                baseline_est_net_pnl=baseline_result.est_net_pnl,
            )
            candidates.append(result)
        for result in candidates:
            result.acceptance_reasons = _candidate_acceptance_reasons(result, baseline_result)
            result.accepted = (
                _candidate_passes_safety_gate(result, baseline_result)
                and
                "kept_pnl_in_range" in result.acceptance_reasons
                and any(
                    reason in result.acceptance_reasons
                    for reason in (
                        "higher_trade_count",
                        "more_added_winners",
                        "fewer_cost_gate_blocks",
                        "higher_tradeability_score",
                        "more_improved_exits",
                        "fewer_early_exit_bugs",
                        "fewer_weak_exit_reentries",
                    )
                )
            )
        target_value = (
            float(effective_spec.next_candidate_value)
            if effective_spec.next_candidate_value is not None
            else float(effective_spec.current_live_value)
        )
        ranked = sorted(
            candidates,
            key=lambda item: (
                -int(item.accepted),
                -item.score,
                -item.est_net_pnl,
                -item.tradeability_score,
                -item.total_trade_count,
                len(item.parameter_overrides),
                abs(item.setting_value - target_value),
                item.blocked_positive_trade_count,
                item.setting_value,
            ),
        )
        recommended = ranked[0] if ranked else baseline_result
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "strategy": effective_spec.name,
            "env_key": effective_spec.env_key,
            "runtime_path": effective_spec.runtime_path,
            "lookback_hours": float(lookback_hours),
            "current_live_value": replay_lab._round_float(effective_spec.current_live_value),
            "next_candidate_value": (
                replay_lab._round_float(effective_spec.next_candidate_value)
                if effective_spec.next_candidate_value is not None
                else None
            ),
            "sample_window": {
                "trade_count": len(trades),
                "all_trade_count": len(raw_trades),
                "no_trade_count": len(no_trade_samples),
                "unit_notional": replay_lab._round_float(unit_notional),
                "trade_verification": trade_verification,
                "technical_excluded_trade_count": technical_excluded_trade_count,
                "technical_exclusion_breakdown": dict(sorted(technical_exclusion_breakdown.items())),
            },
            "trade_sample_source": all_trade_verification["mode"],
            "baseline": baseline_result.to_dict(),
            "recommended": recommended.to_dict(),
            "candidates": [item.to_dict() for item in ranked],
        }
        out_path = output_dir / f"{effective_spec.name}.json"
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        strategy_reports.append(report)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "rotation_strategy_lab",
        "core_strategies": list(CORE_STRATEGIES),
        "catalog_file": _display_path(catalog_file),
        "output_dir": _display_path(output_dir),
        "lookback_hours": float(lookback_hours),
        "trade_sample_source": all_trade_verification["mode"],
        "trade_verification": all_trade_verification,
        "strategies": [
            {
                "strategy": report["strategy"],
                "env_key": report["env_key"],
                "current_live_value": report["current_live_value"],
                "recommended_candidate": report["recommended"]["candidate_name"],
                "recommended_value": report["recommended"]["setting_value"],
                "accepted": report["recommended"]["accepted"],
                "acceptance_reasons": report["recommended"]["acceptance_reasons"],
                "net_pnl_delta": report["recommended"]["net_pnl_delta"],
                "trade_count": report["recommended"]["total_trade_count"],
                "added_trade_count": report["recommended"]["added_trade_count"],
                "report_file": f"{report['strategy']}.json",
            }
            for report in strategy_reports
        ],
    }
    (output_dir / "index.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return summary
