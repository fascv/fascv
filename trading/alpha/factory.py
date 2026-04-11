from __future__ import annotations

from typing import Any, Dict

from trading.alpha.auto import AutoRegimeConfig, RegimeSwitchingAlpha
from trading.alpha.base import AlphaModel
from trading.alpha.breakout import BreakoutAlpha, BreakoutConfig
from trading.alpha.continuation import ContinuationAlpha, ContinuationConfig
from trading.alpha.mean_reversion import MeanReversionAlpha, MeanReversionConfig
from trading.alpha.momentum import MomentumAlpha, MomentumConfig
from trading.alpha.swing import SwingAlpha, SwingConfig


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _scaled_default_bars(cfg: Dict[str, Any], bars_at_1m: int, minimum: int = 1) -> int:
    interval_seconds = max(1, int(_cfg(cfg, "md.interval_seconds", 60)))
    if interval_seconds <= 60:
        return max(minimum, int(bars_at_1m))
    scaled = round(float(bars_at_1m) * (60.0 / float(interval_seconds)))
    return max(minimum, int(scaled))


def _build_momentum(cfg: Dict[str, Any], base_path: str = "alpha") -> MomentumAlpha:
    return MomentumAlpha(
        MomentumConfig(
            lookback=int(_cfg(cfg, f"{base_path}.lookback", 3)),
            threshold_bps=float(_cfg(cfg, f"{base_path}.threshold_bps", 2.0)),
            scale=float(_cfg(cfg, f"{base_path}.scale", 1.0)),
        )
    )


def _build_mean_reversion(cfg: Dict[str, Any], base_path: str = "alpha.mean_reversion") -> MeanReversionAlpha:
    return MeanReversionAlpha(
        MeanReversionConfig(
            lookback=int(_cfg(cfg, f"{base_path}.lookback", 6)),
            threshold_bps=float(_cfg(cfg, f"{base_path}.threshold_bps", 2.0)),
            scale=float(_cfg(cfg, f"{base_path}.scale", 2.0)),
            max_edge_bps=float(_cfg(cfg, f"{base_path}.max_edge_bps", 0.0)),
            soft_threshold_ratio=float(_cfg(cfg, f"{base_path}.soft_threshold_ratio", 0.0)),
            trend_bias_scale=float(_cfg(cfg, f"{base_path}.trend_bias_scale", 2.0)),
            trend_bias_max_bps=float(_cfg(cfg, f"{base_path}.trend_bias_max_bps", 260.0)),
            require_reversal_confirmation=bool(
                _cfg(cfg, f"{base_path}.require_reversal_confirmation", False)
            ),
            reversal_min_last_return_bps=float(
                _cfg(cfg, f"{base_path}.reversal_min_last_return_bps", 0.0)
            ),
            reversal_min_prev_pressure_bps=float(
                _cfg(cfg, f"{base_path}.reversal_min_prev_pressure_bps", 0.0)
            ),
            reversal_max_last_return_bps=float(
                _cfg(cfg, f"{base_path}.reversal_max_last_return_bps", 0.0)
            ),
        )
    )


def _build_breakout(cfg: Dict[str, Any], base_path: str = "alpha.breakout") -> BreakoutAlpha:
    return BreakoutAlpha(
        BreakoutConfig(
            lookback=int(_cfg(cfg, f"{base_path}.lookback", 12)),
            trigger_bps=float(_cfg(cfg, f"{base_path}.trigger_bps", 6.0)),
            scale=float(_cfg(cfg, f"{base_path}.scale", 3.0)),
            max_edge_bps=float(_cfg(cfg, f"{base_path}.max_edge_bps", 0.0)),
            bottom_countertrend_block_max_context_range_pos=float(
                _cfg(cfg, f"{base_path}.bottom_countertrend_block_max_context_range_pos", 0.0)
            ),
            bottom_countertrend_block_max_context_rebound_bps=float(
                _cfg(cfg, f"{base_path}.bottom_countertrend_block_max_context_rebound_bps", 0.0)
            ),
            bottom_countertrend_block_max_trend_return_bps=float(
                _cfg(cfg, f"{base_path}.bottom_countertrend_block_max_trend_return_bps", 0.0)
            ),
            thin_rebound_block_context_range_pos=float(
                _cfg(cfg, f"{base_path}.thin_rebound_block_context_range_pos", 1.0)
            ),
            thin_rebound_block_context_rebound_bps=float(
                _cfg(cfg, f"{base_path}.thin_rebound_block_context_rebound_bps", 0.0)
            ),
            thin_rebound_block_min_spread_bps=float(
                _cfg(cfg, f"{base_path}.thin_rebound_block_min_spread_bps", 0.0)
            ),
            mid_rebound_block_context_range_pos=float(
                _cfg(cfg, f"{base_path}.mid_rebound_block_context_range_pos", 1.0)
            ),
            mid_rebound_block_context_rebound_bps=float(
                _cfg(cfg, f"{base_path}.mid_rebound_block_context_rebound_bps", 0.0)
            ),
            mid_rebound_block_min_volume_z=float(
                _cfg(cfg, f"{base_path}.mid_rebound_block_min_volume_z", -999.0)
            ),
            late_rebound_block_context_range_pos=float(
                _cfg(cfg, f"{base_path}.late_rebound_block_context_range_pos", 1.0)
            ),
            late_rebound_block_context_rebound_bps=float(
                _cfg(cfg, f"{base_path}.late_rebound_block_context_rebound_bps", 0.0)
            ),
            late_rebound_block_min_volume_z=float(
                _cfg(cfg, f"{base_path}.late_rebound_block_min_volume_z", -999.0)
            ),
        )
    )


def _build_continuation(cfg: Dict[str, Any], base_path: str = "alpha.continuation") -> ContinuationAlpha:
    return ContinuationAlpha(
        ContinuationConfig(
            lookback=int(_cfg(cfg, f"{base_path}.lookback", _scaled_default_bars(cfg, 4, minimum=1))),
            trend_min_bps=float(_cfg(cfg, f"{base_path}.trend_min_bps", 8.0)),
            rebound_trigger_bps=float(_cfg(cfg, f"{base_path}.rebound_trigger_bps", 0.0)),
            rebound_confirm_bars=int(
                _cfg(cfg, f"{base_path}.rebound_confirm_bars", _scaled_default_bars(cfg, 1, minimum=1))
            ),
            pullback_min_bps=float(_cfg(cfg, f"{base_path}.pullback_min_bps", 0.0)),
            pullback_max_bps=float(_cfg(cfg, f"{base_path}.pullback_max_bps", 45.0)),
            trend_scale=float(_cfg(cfg, f"{base_path}.trend_scale", 0.9)),
            rebound_scale=float(_cfg(cfg, f"{base_path}.rebound_scale", 2.2)),
            pullback_scale=float(_cfg(cfg, f"{base_path}.pullback_scale", 0.7)),
            recent_bias_lookback=int(
                _cfg(cfg, f"{base_path}.recent_bias_lookback", _scaled_default_bars(cfg, 4, minimum=1))
            ),
            recent_bias_min_bps=float(_cfg(cfg, f"{base_path}.recent_bias_min_bps", 0.0)),
            max_chase_bps=float(_cfg(cfg, f"{base_path}.max_chase_bps", 16.0)),
            max_range_pos=float(_cfg(cfg, f"{base_path}.max_range_pos", 0.75)),
            min_volume_z=float(_cfg(cfg, f"{base_path}.min_volume_z", -999.0)),
            max_structure_range_pos=float(
                _cfg(cfg, f"{base_path}.max_structure_range_pos", 1.0)
            ),
            stall_recovery_max_range_pos=float(
                _cfg(cfg, f"{base_path}.stall_recovery_max_range_pos", 0.86)
            ),
            range_continuation_max_range_pos=float(
                _cfg(cfg, f"{base_path}.range_continuation_max_range_pos", 0.88)
            ),
            hard_block_above_range_pos=float(
                _cfg(cfg, f"{base_path}.hard_block_above_range_pos", 1.0)
            ),
            hard_block_max_drawdown_bps=float(
                _cfg(cfg, f"{base_path}.hard_block_max_drawdown_bps", 120.0)
            ),
            max_edge_bps=float(_cfg(cfg, f"{base_path}.max_edge_bps", 260.0)),
            impulse_min_ret_bps=float(_cfg(cfg, f"{base_path}.impulse_min_ret_bps", -10.0)),
            impulse_min_volume_z=float(_cfg(cfg, f"{base_path}.impulse_min_volume_z", -1.6)),
            impulse_max_context_range_pos=float(
                _cfg(cfg, f"{base_path}.impulse_max_context_range_pos", 1.0)
            ),
            impulse_max_extension_bps=float(
                _cfg(cfg, f"{base_path}.impulse_max_extension_bps", 0.0)
            ),
            impulse_require_up_structure=bool(
                _cfg(cfg, f"{base_path}.impulse_require_up_structure", False)
            ),
            staircase_min_trend_bps=float(
                _cfg(cfg, f"{base_path}.staircase_min_trend_bps", 0.0)
            ),
            staircase_min_ret_bps=float(
                _cfg(cfg, f"{base_path}.staircase_min_ret_bps", -12.0)
            ),
            staircase_min_volume_z=float(
                _cfg(cfg, f"{base_path}.staircase_min_volume_z", -999.0)
            ),
            staircase_min_slope_medium_bps=float(
                _cfg(cfg, f"{base_path}.staircase_min_slope_medium_bps", 0.0)
            ),
            staircase_min_slope_long_bps=float(
                _cfg(cfg, f"{base_path}.staircase_min_slope_long_bps", 0.0)
            ),
            staircase_min_drawdown_from_peak_bps=float(
                _cfg(cfg, f"{base_path}.staircase_min_drawdown_from_peak_bps", 0.0)
            ),
            staircase_max_drawdown_from_peak_bps=float(
                _cfg(cfg, f"{base_path}.staircase_max_drawdown_from_peak_bps", 120.0)
            ),
            staircase_max_context_range_pos=float(
                _cfg(cfg, f"{base_path}.staircase_max_context_range_pos", 1.0)
            ),
            staircase_max_spread_bps=float(
                _cfg(cfg, f"{base_path}.staircase_max_spread_bps", 22.0)
            ),
            staircase_require_up_structure=bool(
                _cfg(cfg, f"{base_path}.staircase_require_up_structure", False)
            ),
            early_liftoff_max_context_range_pos=float(
                _cfg(cfg, f"{base_path}.early_liftoff_max_context_range_pos", -1.0)
            ),
            early_liftoff_max_structure_range_pos=float(
                _cfg(cfg, f"{base_path}.early_liftoff_max_structure_range_pos", 0.55)
            ),
            early_liftoff_min_context_rebound_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_context_rebound_bps", -1.0)
            ),
            early_liftoff_max_spread_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_max_spread_bps", 14.0)
            ),
            early_liftoff_min_volume_z=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_volume_z", -1.1)
            ),
            early_liftoff_min_ret_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_ret_bps", -1.0)
            ),
            early_liftoff_min_slope_short_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_slope_short_bps", 2.4)
            ),
            early_liftoff_min_slope_medium_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_slope_medium_bps", 2.4)
            ),
            early_liftoff_min_drawdown_from_peak_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_drawdown_from_peak_bps", 100.0)
            ),
            early_liftoff_min_trend_bps=float(
                _cfg(cfg, f"{base_path}.early_liftoff_min_trend_bps", -260.0)
            ),
            bar_seconds=int(_cfg(cfg, f"{base_path}.bar_seconds", _cfg(cfg, "md.interval_seconds", 60))),
            reference_bar_seconds=int(_cfg(cfg, f"{base_path}.reference_bar_seconds", 60)),
        )
    )


def _build_swing(cfg: Dict[str, Any], base_path: str = "alpha.swing") -> SwingAlpha:
    return SwingAlpha(
        SwingConfig(
            lookback=int(_cfg(cfg, f"{base_path}.lookback", 20)),
            buy_band=float(_cfg(cfg, f"{base_path}.buy_band", 0.42)),
            sell_band=float(_cfg(cfg, f"{base_path}.sell_band", 0.67)),
            momentum_lookback=int(_cfg(cfg, f"{base_path}.momentum_lookback", 7)),
            reversal_threshold_bps=float(_cfg(cfg, f"{base_path}.reversal_threshold_bps", 0.4)),
            edge_scale=float(_cfg(cfg, f"{base_path}.edge_scale", 1.1)),
            min_range_bps=float(_cfg(cfg, f"{base_path}.min_range_bps", 52.0)),
            micro_rebound_max_spread_bps=float(
                _cfg(cfg, f"{base_path}.micro_rebound_max_spread_bps", 8.0)
            ),
            micro_rebound_max_context_range_pos=float(
                _cfg(cfg, f"{base_path}.micro_rebound_max_context_range_pos", 0.38)
            ),
            micro_rebound_min_context_rebound_bps=float(
                _cfg(cfg, f"{base_path}.micro_rebound_min_context_rebound_bps", 0.0)
            ),
            micro_rebound_min_ret_bps=float(
                _cfg(cfg, f"{base_path}.micro_rebound_min_ret_bps", -2.0)
            ),
            micro_rebound_confirm_rebound_bps=float(
                _cfg(cfg, f"{base_path}.micro_rebound_confirm_rebound_bps", 0.0)
            ),
            micro_rebound_confirm_min_ret_bps=float(
                _cfg(cfg, f"{base_path}.micro_rebound_confirm_min_ret_bps", 0.0)
            ),
            max_edge_bps=float(_cfg(cfg, f"{base_path}.max_edge_bps", 155.0)),
        )
    )


def build_alpha_model(cfg: Dict[str, Any]) -> AlphaModel:
    alpha_type = str(_cfg(cfg, "alpha.type", "momentum") or "momentum").strip().lower()
    if alpha_type in {"momentum", "trend"}:
        return _build_momentum(cfg, base_path="alpha")
    if alpha_type in {"mean_reversion", "reversion"}:
        return _build_mean_reversion(cfg, base_path="alpha.mean_reversion")
    if alpha_type in {"continuation", "followthrough"}:
        return _build_continuation(cfg, base_path="alpha.continuation")
    if alpha_type == "breakout":
        return _build_breakout(cfg, base_path="alpha.breakout")
    if alpha_type == "swing":
        return _build_swing(cfg, base_path="alpha.swing")
    if alpha_type == "auto":
        strategies: Dict[str, AlphaModel] = {
            "trend": _build_momentum(cfg, base_path="alpha.auto.trend"),
            "mean_reversion": _build_mean_reversion(cfg, base_path="alpha.auto.mean_reversion"),
            "continuation": _build_continuation(cfg, base_path="alpha.auto.continuation"),
            "breakout": _build_breakout(cfg, base_path="alpha.auto.breakout"),
            "swing": _build_swing(cfg, base_path="alpha.auto.swing"),
        }
        auto_cfg = AutoRegimeConfig(
            lookback=int(_cfg(cfg, "alpha.auto.regime.lookback", 6)),
            trend_momentum_bps=float(_cfg(cfg, "alpha.auto.regime.trend_momentum_bps", 6.0)),
            range_momentum_bps=float(_cfg(cfg, "alpha.auto.regime.range_momentum_bps", 2.0)),
            high_vol_atr_bps=float(_cfg(cfg, "alpha.auto.regime.high_vol_atr_bps", 120.0)),
            low_vol_atr_bps=float(_cfg(cfg, "alpha.auto.regime.low_vol_atr_bps", 60.0)),
            breakout_return_bps=float(_cfg(cfg, "alpha.auto.regime.breakout_return_bps", 8.0)),
            context_pullback_bps=float(_cfg(cfg, "alpha.auto.regime.context_pullback_bps", 0.0)),
            context_rebound_bps=float(_cfg(cfg, "alpha.auto.regime.context_rebound_bps", 0.0)),
            context_trend_bps=float(_cfg(cfg, "alpha.auto.regime.context_trend_bps", 0.0)),
            context_range_low=float(_cfg(cfg, "alpha.auto.regime.context_range_low", 0.35)),
            context_range_high=float(_cfg(cfg, "alpha.auto.regime.context_range_high", 0.65)),
            default_regime=str(_cfg(cfg, "alpha.auto.regime.default_regime", "trend")),
            trend_strategy=str(_cfg(cfg, "alpha.auto.regime.trend_strategy", "trend")),
            range_strategy=str(_cfg(cfg, "alpha.auto.regime.range_strategy", "mean_reversion")),
            breakout_strategy=str(_cfg(cfg, "alpha.auto.regime.breakout_strategy", "breakout")),
        )
        return RegimeSwitchingAlpha(strategies=strategies, config=auto_cfg)
    raise ValueError(f"Unsupported alpha type: {alpha_type}")
