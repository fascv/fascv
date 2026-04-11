from __future__ import annotations

from dataclasses import dataclass

from trading.alpha.base import AlphaModel
from trading.market_structure import classify_market_structure
from trading.types import AlphaSignal, Features


@dataclass
class ContinuationConfig:
    lookback: int
    trend_min_bps: float
    rebound_trigger_bps: float
    rebound_confirm_bars: int
    pullback_min_bps: float
    pullback_max_bps: float
    trend_scale: float
    rebound_scale: float
    pullback_scale: float
    recent_bias_lookback: int = 0
    recent_bias_min_bps: float = 0.0
    max_chase_bps: float = 0.0
    max_range_pos: float = 1.0
    min_volume_z: float = -999.0
    max_structure_range_pos: float = 1.0
    stall_recovery_max_range_pos: float = 0.86
    range_continuation_max_range_pos: float = 0.88
    hard_block_above_range_pos: float = 1.0
    hard_block_max_drawdown_bps: float = 120.0
    max_edge_bps: float = 0.0
    impulse_min_ret_bps: float = -10.0
    impulse_min_volume_z: float = -1.6
    impulse_max_context_range_pos: float = 1.0
    impulse_max_extension_bps: float = 0.0
    impulse_require_up_structure: bool = False
    staircase_min_trend_bps: float = 0.0
    staircase_min_ret_bps: float = -12.0
    staircase_min_volume_z: float = -999.0
    staircase_min_slope_medium_bps: float = 0.0
    staircase_min_slope_long_bps: float = 0.0
    staircase_min_drawdown_from_peak_bps: float = 0.0
    staircase_max_drawdown_from_peak_bps: float = 120.0
    staircase_max_context_range_pos: float = 1.0
    staircase_max_spread_bps: float = 22.0
    staircase_require_up_structure: bool = False
    early_liftoff_max_context_range_pos: float = -1.0
    early_liftoff_max_structure_range_pos: float = 0.55
    early_liftoff_min_context_rebound_bps: float = -1.0
    early_liftoff_max_spread_bps: float = 14.0
    early_liftoff_min_volume_z: float = -1.1
    early_liftoff_min_ret_bps: float = -1.0
    early_liftoff_min_slope_short_bps: float = 2.4
    early_liftoff_min_slope_medium_bps: float = 2.4
    early_liftoff_min_drawdown_from_peak_bps: float = 100.0
    early_liftoff_min_trend_bps: float = -260.0
    bar_seconds: int = 60
    reference_bar_seconds: int = 60


class ContinuationAlpha(AlphaModel):
    """
    Long-only continuation model:
    - only engage when medium-term trend is already positive
    - require a confirmed multi-bar upward rebound
    - reward a shallow recent pullback followed by a positive rebound
    - suppress entries that are already too extended or still fading
    """

    def __init__(self, config: ContinuationConfig):
        self.config = config
        self._returns: list[float] = []
        self._prices: list[float] = []

    def predict(self, features: Features) -> AlphaSignal:
        price = float(features.values.get("price", 0.0))
        if price <= 0.0:
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "invalid_price"},
            )
        ret_bps = float(features.values.get("return_bps", 0.0))
        trend_return_bps = float(features.values.get("trend_return_bps", 0.0))
        volume_z = float(features.values.get("volume_z", 0.0))
        spread_bps = float(features.values.get("spread_bps", 0.0))
        context_range_pos = float(features.values.get("context_range_pos", 0.0))
        context_rebound_bps = max(0.0, float(features.values.get("context_rebound_bps", 0.0) or 0.0))
        context_drawdown_bps = float(features.values.get("context_drawdown_bps", 0.0))
        context_drawdown_from_peak_bps = max(0.0, -context_drawdown_bps)
        self._returns.append(ret_bps)
        self._prices.append(price)
        confirm_bars = max(0, int(getattr(self.config, "rebound_confirm_bars", 1) or 0))
        recent_bias_lookback = max(0, int(getattr(self.config, "recent_bias_lookback", 0) or 0))
        bar_seconds = max(1, int(getattr(self.config, "bar_seconds", 60) or 60))
        reference_bar_seconds = max(
            1,
            int(getattr(self.config, "reference_bar_seconds", 60) or 60),
        )

        def _scaled_structure_bars(base: int, minimum: int = 1) -> int:
            scaled = round(float(base) * (float(reference_bar_seconds) / float(bar_seconds)))
            return max(minimum, int(scaled))

        structure_short_window = _scaled_structure_bars(4, minimum=4)
        structure_medium_window = _scaled_structure_bars(12, minimum=12)
        structure_long_window = _scaled_structure_bars(36, minimum=36)
        structure_smooth_window = _scaled_structure_bars(3, minimum=3)
        structure_level_6h_window = _scaled_structure_bars(72, minimum=72)
        structure_level_24h_window = _scaled_structure_bars(288, minimum=288)

        keep_n = max(48, int(self.config.lookback), confirm_bars + 1, recent_bias_lookback)
        if len(self._returns) > keep_n:
            self._returns = self._returns[-keep_n:]
        price_keep_n = max(keep_n * 3, structure_level_24h_window + 4)
        if len(self._prices) > price_keep_n:
            self._prices = self._prices[-price_keep_n:]
        structure = classify_market_structure(
            self._prices,
            short_window=structure_short_window,
            medium_window=structure_medium_window,
            long_window=structure_long_window,
            smooth_window=structure_smooth_window,
            bar_seconds=bar_seconds,
            slope_normalize_seconds=reference_bar_seconds,
            level_6h_window=structure_level_6h_window,
            level_24h_window=structure_level_24h_window,
        )
        structure_meta = {
            "phase": structure.phase,
            "confidence": structure.confidence,
            "slope_short_bps": structure.slope_short_bps,
            "slope_medium_bps": structure.slope_medium_bps,
            "slope_long_bps": structure.slope_long_bps,
            "curvature_bps": structure.curvature_bps,
            "range_pos": structure.range_pos,
            "rebound_bps": structure.rebound_bps,
            "drawdown_bps": structure.drawdown_bps,
            "drawdown_from_peak_bps": structure.drawdown_from_peak_bps,
            "extension_bps": structure.extension_bps,
            "bars_since_peak": structure.bars_since_peak,
            "active_leg": structure.active_leg,
            "up_structure": structure.up_structure,
            "down_structure": structure.down_structure,
        }
        phase = structure.phase
        effective_drawdown_from_peak_bps = max(0.0, float(structure.drawdown_from_peak_bps))

        def _rocket_cooldown_blocked() -> bool:
            # After a strong vertical extension, force a cool-down before re-entry.
            # This prevents buying directly into the top after a "rocket" move.
            if phase not in {"lift_off", "uptrend", "range"}:
                return False
            if structure.active_leg != "rise" or structure.down_structure:
                return False
            if trend_return_bps < 110.0:
                return False
            if structure.range_pos < 0.97:
                return False
            if structure.extension_bps < 70.0:
                return False
            cooloff_drawdown_needed = max(12.0, structure.pivot_reversal_bps * 0.35)
            return effective_drawdown_from_peak_bps <= cooloff_drawdown_needed

        def _impulse_override_signal(block_reason: str) -> AlphaSignal | None:
            # Fast scalp override for strong live momentum bursts that would otherwise be
            # filtered by continuation staging gates (await_liftoff / rebound_not_ready / weak_flow).
            if phase not in {"lift_off", "uptrend", "range"}:
                return None
            if _rocket_cooldown_blocked():
                return None
            if structure.active_leg == "fall" or structure.down_structure:
                return None
            if trend_return_bps < 120.0:
                return None
            impulse_min_ret_bps = float(getattr(self.config, "impulse_min_ret_bps", -10.0) or -10.0)
            if ret_bps < impulse_min_ret_bps:
                return None
            if spread_bps <= 0.0 or spread_bps > 22.0:
                return None
            impulse_min_volume_z = float(
                getattr(self.config, "impulse_min_volume_z", -1.6) or -1.6
            )
            if volume_z < impulse_min_volume_z:
                return None
            if bool(getattr(self.config, "impulse_require_up_structure", False)) and not structure.up_structure:
                return None
            impulse_max_context_range_pos = max(
                0.0,
                min(1.0, float(getattr(self.config, "impulse_max_context_range_pos", 1.0) or 1.0)),
            )
            if context_range_pos > impulse_max_context_range_pos:
                return None
            impulse_max_extension_bps = max(
                0.0, float(getattr(self.config, "impulse_max_extension_bps", 0.0) or 0.0)
            )
            if impulse_max_extension_bps > 0.0 and structure.extension_bps > impulse_max_extension_bps:
                return None
            if structure.slope_short_bps < 0.8 or structure.slope_medium_bps < 2.0:
                return None
            if context_range_pos < 0.45:
                return None
            edge = (
                trend_return_bps * 0.20
                + max(0.0, ret_bps) * 0.6
                + max(0.0, structure.slope_short_bps) * 1.5
                + max(0.0, structure.slope_medium_bps)
            )
            edge -= max(0.0, spread_bps - 10.0) * 0.9
            edge = max(16.0, edge)
            max_edge = max(0.0, float(getattr(self.config, "max_edge_bps", 0.0) or 0.0))
            if max_edge > 0.0:
                edge = min(edge, max_edge)
            if edge <= 0.0:
                return None
            return AlphaSignal(
                ts=features.ts,
                edge_bps=edge,
                p_up=None,
                meta={
                    "continuation_state": "impulse_override",
                    "impulse_block_reason": block_reason,
                    "trend_return_bps": trend_return_bps,
                    "ret_bps": ret_bps,
                    "spread_bps": spread_bps,
                    "volume_z": volume_z,
                    "structure": structure_meta,
                },
            )

        def _staircase_override_signal(block_reason: str) -> AlphaSignal | None:
            # Early continuation override for "staircase" trends with shallow pullbacks.
            # This keeps the trader engaged before structure degrades into peak/rollover.
            staircase_max_spread_bps = max(
                0.0, float(getattr(self.config, "staircase_max_spread_bps", 22.0) or 22.0)
            )
            staircase_min_trend_bps = max(
                0.0, float(getattr(self.config, "staircase_min_trend_bps", 0.0) or 0.0)
            )
            staircase_min_ret_bps = float(
                getattr(self.config, "staircase_min_ret_bps", -12.0) or -12.0
            )
            staircase_min_slope_medium_bps = float(
                getattr(self.config, "staircase_min_slope_medium_bps", 0.0) or 0.0
            )
            staircase_min_slope_long_bps = float(
                getattr(self.config, "staircase_min_slope_long_bps", 0.0) or 0.0
            )
            staircase_min_drawdown_from_peak_bps = max(
                0.0,
                float(
                    getattr(self.config, "staircase_min_drawdown_from_peak_bps", 0.0) or 0.0
                ),
            )
            staircase_max_drawdown_from_peak_bps = max(
                staircase_min_drawdown_from_peak_bps,
                float(
                    getattr(self.config, "staircase_max_drawdown_from_peak_bps", 120.0)
                    or 120.0
                ),
            )
            if effective_drawdown_from_peak_bps < staircase_min_drawdown_from_peak_bps:
                return None
            if effective_drawdown_from_peak_bps > staircase_max_drawdown_from_peak_bps:
                return None
            staircase_max_context_range_pos = max(
                0.0,
                min(
                    1.0,
                    float(getattr(self.config, "staircase_max_context_range_pos", 1.0) or 1.0),
                ),
            )
            stall_phase_ready = (
                phase == "stall"
                and structure.active_leg != "fall"
                and not structure.down_structure
                and (
                    structure.up_structure
                    or structure.slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.75)
                )
                and structure.slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.70)
                and structure.slope_long_bps >= max(-0.05, staircase_min_slope_long_bps - 0.14)
                and context_range_pos <= min(staircase_max_context_range_pos, 0.94)
                and effective_drawdown_from_peak_bps >= max(4.0, staircase_min_drawdown_from_peak_bps)
            )
            if phase not in {"lift_off", "uptrend", "range", "rollover"} and not stall_phase_ready:
                return None
            if _rocket_cooldown_blocked():
                return None
            if structure.down_structure:
                return None
            if spread_bps <= 0.0 or spread_bps > staircase_max_spread_bps:
                return None
            staircase_min_volume_z = float(
                getattr(self.config, "staircase_min_volume_z", -999.0) or -999.0
            )
            if volume_z < staircase_min_volume_z:
                return None
            constructive_context = (
                not structure.down_structure
                and structure.active_leg != "fall"
                and (
                    phase in {"uptrend", "range", "stall", "lift_off"}
                    or structure.up_structure
                )
            )
            effective_staircase_min_trend_bps = staircase_min_trend_bps
            if constructive_context and (
                structure.slope_medium_bps >= max(0.7, staircase_min_slope_medium_bps * 0.70)
                and structure.slope_long_bps >= max(-0.05, staircase_min_slope_long_bps - 0.14)
            ):
                effective_staircase_min_trend_bps = max(0.0, staircase_min_trend_bps - 14.0)
            if stall_phase_ready:
                effective_staircase_min_trend_bps = max(
                    0.0, effective_staircase_min_trend_bps - 10.0
                )
            if trend_return_bps < effective_staircase_min_trend_bps:
                return None
            if ret_bps < staircase_min_ret_bps:
                return None
            if structure.slope_medium_bps < staircase_min_slope_medium_bps:
                return None
            effective_staircase_min_slope_long_bps = staircase_min_slope_long_bps
            if constructive_context and structure.up_structure:
                effective_staircase_min_slope_long_bps = max(
                    0.0, staircase_min_slope_long_bps - 0.06
                )
            if stall_phase_ready:
                effective_staircase_min_slope_long_bps = max(
                    -0.05, effective_staircase_min_slope_long_bps - 0.08
                )
            if structure.slope_long_bps < effective_staircase_min_slope_long_bps:
                return None
            if bool(getattr(self.config, "staircase_require_up_structure", False)) and not structure.up_structure:
                return None
            if context_range_pos > staircase_max_context_range_pos:
                return None
            if phase == "rollover" and structure.slope_short_bps < -3.5:
                return None
            if structure.active_leg == "fall" and ret_bps < -8.0:
                return None
            edge = (
                trend_return_bps * 0.16
                + max(0.0, structure.slope_medium_bps) * 2.1
                + max(0.0, structure.slope_long_bps) * 1.4
                + max(
                    0.0,
                    effective_drawdown_from_peak_bps - staircase_min_drawdown_from_peak_bps,
                )
                * 0.20
                + max(0.0, ret_bps + 4.0) * 0.35
            )
            edge -= max(0.0, spread_bps - 8.0) * 0.75
            edge -= max(0.0, context_range_pos - 0.78) * 90.0
            if phase == "rollover":
                edge -= 3.0
            elif stall_phase_ready:
                edge -= 2.5
            edge = max(18.0, edge)
            max_edge = max(0.0, float(getattr(self.config, "max_edge_bps", 0.0) or 0.0))
            if max_edge > 0.0:
                edge = min(edge, max_edge)
            if edge <= 0.0:
                return None
            return AlphaSignal(
                ts=features.ts,
                edge_bps=edge,
                p_up=None,
                meta={
                    "continuation_state": "staircase_override",
                    "staircase_block_reason": block_reason,
                    "trend_return_bps": trend_return_bps,
                    "ret_bps": ret_bps,
                    "spread_bps": spread_bps,
                    "volume_z": volume_z,
                    "context_range_pos": context_range_pos,
                    "effective_drawdown_from_peak_bps": effective_drawdown_from_peak_bps,
                    "staircase_stall_phase_ready": stall_phase_ready,
                    "staircase_effective_min_trend_bps": effective_staircase_min_trend_bps,
                    "staircase_effective_min_slope_long_bps": effective_staircase_min_slope_long_bps,
                    "structure": structure_meta,
                },
            )

        def _early_liftoff_override_signal(block_reason: str) -> AlphaSignal | None:
            # Earlier bottom/liftoff entry for fresh rebounds that are still low in the
            # broader range. This addresses the "confirmation arrives too late" problem.
            if phase not in {"bottom", "lift_off", "range"}:
                return None
            if _rocket_cooldown_blocked():
                return None
            if structure.active_leg == "fall":
                return None
            early_liftoff_max_context_range_pos = float(
                getattr(self.config, "early_liftoff_max_context_range_pos", -1.0) or -1.0
            )
            if early_liftoff_max_context_range_pos <= 0.0:
                early_liftoff_max_context_range_pos = min(
                    0.38,
                    max(0.26, float(self.config.max_range_pos) * 0.46),
                )
            if context_range_pos > early_liftoff_max_context_range_pos:
                return None
            early_liftoff_max_structure_range_pos = max(
                0.0,
                min(
                    1.0,
                    float(getattr(self.config, "early_liftoff_max_structure_range_pos", 0.55) or 0.55),
                ),
            )
            if structure.range_pos > early_liftoff_max_structure_range_pos:
                return None
            early_liftoff_min_context_rebound_bps = float(
                getattr(self.config, "early_liftoff_min_context_rebound_bps", -1.0) or -1.0
            )
            if early_liftoff_min_context_rebound_bps <= 0.0:
                early_liftoff_min_context_rebound_bps = max(
                    60.0,
                    float(self.config.rebound_trigger_bps) * 6.0 + 20.0,
                )
            if context_rebound_bps < early_liftoff_min_context_rebound_bps:
                return None
            early_liftoff_max_spread_bps = max(
                0.0,
                float(getattr(self.config, "early_liftoff_max_spread_bps", 14.0) or 14.0),
            )
            if spread_bps <= 0.0 or spread_bps > early_liftoff_max_spread_bps:
                return None
            early_liftoff_min_volume_z = float(
                getattr(self.config, "early_liftoff_min_volume_z", -1.1) or -1.1
            )
            if volume_z < max(float(getattr(self.config, "min_volume_z", -999.0) or -999.0), early_liftoff_min_volume_z):
                return None
            early_liftoff_min_ret_bps = float(
                getattr(self.config, "early_liftoff_min_ret_bps", -1.0) or -1.0
            )
            if ret_bps < early_liftoff_min_ret_bps:
                return None
            early_liftoff_min_slope_short_bps = float(
                getattr(self.config, "early_liftoff_min_slope_short_bps", 2.4) or 2.4
            )
            early_liftoff_min_slope_medium_bps = float(
                getattr(self.config, "early_liftoff_min_slope_medium_bps", 2.4) or 2.4
            )
            if (
                structure.slope_short_bps < early_liftoff_min_slope_short_bps
                or structure.slope_medium_bps < early_liftoff_min_slope_medium_bps
            ):
                return None
            early_liftoff_min_drawdown_from_peak_bps = max(
                0.0,
                float(
                    getattr(self.config, "early_liftoff_min_drawdown_from_peak_bps", 100.0) or 100.0
                ),
            )
            if max(effective_drawdown_from_peak_bps, context_drawdown_from_peak_bps) < early_liftoff_min_drawdown_from_peak_bps:
                return None
            early_liftoff_min_trend_bps = float(
                getattr(self.config, "early_liftoff_min_trend_bps", -260.0) or -260.0
            )
            if trend_return_bps < early_liftoff_min_trend_bps:
                return None

            edge = context_rebound_bps * 0.09
            edge += max(0.0, structure.slope_short_bps) * 1.7
            edge += max(0.0, structure.slope_medium_bps) * 1.1
            edge += max(0.0, ret_bps + 2.0) * 0.5
            edge += max(0.0, structure.rebound_bps) * 0.04
            edge -= max(0.0, spread_bps - 8.0) * 0.9
            edge -= max(0.0, (-trend_return_bps) - 180.0) * 0.03
            edge -= max(0.0, context_range_pos - 0.28) * 18.0
            edge = max(14.0, edge)

            max_edge = max(0.0, float(getattr(self.config, "max_edge_bps", 0.0) or 0.0))
            if max_edge > 0.0:
                edge = min(edge, max_edge)
            if edge <= 0.0:
                return None

            return AlphaSignal(
                ts=features.ts,
                edge_bps=edge,
                p_up=None,
                meta={
                    "continuation_state": "early_liftoff_override",
                    "early_liftoff_block_reason": block_reason,
                    "context_rebound_bps": context_rebound_bps,
                    "context_range_pos": context_range_pos,
                    "trend_return_bps": trend_return_bps,
                    "spread_bps": spread_bps,
                    "ret_bps": ret_bps,
                    "structure": structure_meta,
                },
            )

        if phase == "unknown":
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "warmup_structure", "structure": structure_meta},
            )
        if phase in {"downtrend", "rollover", "peak"}:
            if phase == "rollover":
                staircase = _staircase_override_signal("structure_rollover")
                if staircase is not None:
                    return staircase
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "structure_blocked", "structure": structure_meta},
            )
        stall_recovery_ready = (
            phase == "stall"
            and structure.active_leg != "fall"
            and not structure.down_structure
            and structure.slope_medium_bps >= 0.8
            and structure.slope_long_bps >= 0.2
            and structure.range_pos
            <= max(
                0.0,
                min(1.0, float(getattr(self.config, "stall_recovery_max_range_pos", 0.86))),
            )
            and effective_drawdown_from_peak_bps
            >= max(10.0, structure.pivot_reversal_bps * 0.35)
        )
        if phase == "stall" and not stall_recovery_ready:
            staircase = _staircase_override_signal("structure_stall")
            if staircase is not None:
                return staircase
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "structure_blocked", "structure": structure_meta},
            )
        range_continuation_ready = (
            phase == "range"
            and structure.active_leg != "fall"
            and not structure.down_structure
            and structure.slope_medium_bps >= 1.2
            and structure.slope_long_bps >= 0.4
            and structure.range_pos
            <= max(
                0.0,
                min(
                    1.0,
                    float(getattr(self.config, "range_continuation_max_range_pos", 0.88)),
                ),
            )
            and effective_drawdown_from_peak_bps
            >= max(8.0, structure.pivot_reversal_bps * 0.25)
        )
        if phase == "range" and not range_continuation_ready:
            early_liftoff = _early_liftoff_override_signal("await_liftoff")
            if early_liftoff is not None:
                return early_liftoff
            staircase = _staircase_override_signal("await_liftoff")
            if staircase is not None:
                return staircase
            impulse = _impulse_override_signal("await_liftoff")
            if impulse is not None:
                return impulse
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "await_liftoff", "structure": structure_meta},
            )
        if phase == "bottom":
            early_liftoff = _early_liftoff_override_signal("await_liftoff")
            if early_liftoff is not None:
                return early_liftoff
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "await_liftoff", "structure": structure_meta},
            )
        min_volume_z = float(getattr(self.config, "min_volume_z", -999.0) or -999.0)
        if volume_z < min_volume_z:
            staircase = _staircase_override_signal("weak_flow")
            if staircase is not None:
                return staircase
            impulse = _impulse_override_signal("weak_flow")
            if impulse is not None:
                return impulse
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "continuation_state": "weak_flow",
                    "volume_z": volume_z,
                    "min_volume_z": min_volume_z,
                    "structure": structure_meta,
                },
            )
        max_structure_range_pos = max(
            0.0, min(1.0, float(getattr(self.config, "max_structure_range_pos", 1.0)))
        )
        if structure.range_pos > max_structure_range_pos:
            staircase = _staircase_override_signal("structure_too_high")
            if staircase is not None:
                return staircase
            impulse = _impulse_override_signal("structure_too_high")
            if impulse is not None:
                return impulse
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "continuation_state": "structure_too_high",
                    "structure_range_pos": structure.range_pos,
                    "max_structure_range_pos": max_structure_range_pos,
                    "structure": structure_meta,
                },
            )
        if len(self._returns) < keep_n:
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={"continuation_state": "warmup", "structure": structure_meta},
            )

        trend_min_bps = float(self.config.trend_min_bps)
        if phase == "lift_off":
            trend_min_bps *= 0.35
        if trend_return_bps < trend_min_bps:
            early_liftoff = _early_liftoff_override_signal("trend_too_weak")
            if early_liftoff is not None:
                return early_liftoff
            staircase = _staircase_override_signal("trend_too_weak")
            if staircase is not None:
                return staircase
            impulse = _impulse_override_signal("trend_too_weak")
            if impulse is not None:
                return impulse
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "continuation_state": "trend_too_weak",
                    "pullback_bps": 0.0,
                    "rebound_bps": max(0.0, ret_bps),
                    "structure": structure_meta,
                },
            )

        recent = self._returns[-keep_n:]
        if confirm_bars > 0:
            rebound_window = recent[-confirm_bars:]
            rebound_bps = sum(rebound_window)
            rebound_ready = (
                not any(value <= 0.0 for value in rebound_window)
                and rebound_bps >= float(self.config.rebound_trigger_bps)
            )
        else:
            rebound_window = [ret_bps]
            rebound_bps = float(ret_bps)
            rebound_ready = rebound_bps >= float(self.config.rebound_trigger_bps)

        if not rebound_ready:
            early_liftoff = _early_liftoff_override_signal("rebound_not_ready")
            if early_liftoff is not None:
                return early_liftoff
            staircase = _staircase_override_signal("rebound_not_ready")
            if staircase is not None:
                return staircase
            impulse = _impulse_override_signal("rebound_not_ready")
            if impulse is not None:
                return impulse
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "continuation_state": "rebound_not_ready",
                    "pullback_bps": 0.0,
                    "rebound_bps": max(0.0, rebound_bps),
                    "structure": structure_meta,
                },
            )

        recent_bias_bps = 0.0
        if recent_bias_lookback > 0:
            recent_bias_bps = sum(recent[-recent_bias_lookback:])
            if recent_bias_bps < float(getattr(self.config, "recent_bias_min_bps", 0.0) or 0.0):
                impulse = _impulse_override_signal("recent_trend_down")
                if impulse is not None:
                    return impulse
                return AlphaSignal(
                    ts=features.ts,
                    edge_bps=0.0,
                    p_up=None,
                    meta={
                        "continuation_state": "recent_trend_down",
                        "pullback_bps": 0.0,
                        "rebound_bps": max(0.0, rebound_bps),
                        "recent_bias_bps": recent_bias_bps,
                        "structure": structure_meta,
                    },
                )

        if confirm_bars > 0:
            prior = recent[:-confirm_bars]
        else:
            prior = recent[:-1]
        pullback_bps = max(0.0, -sum(min(0.0, value) for value in prior))
        if pullback_bps < float(self.config.pullback_min_bps):
            pullback_bps = 0.0
        if float(self.config.pullback_max_bps) > 0.0:
            pullback_bps = min(pullback_bps, float(self.config.pullback_max_bps))

        chase_penalty_bps = 0.0
        max_chase_bps = max(0.0, float(self.config.max_chase_bps))
        if max_chase_bps > 0.0 and rebound_bps > max_chase_bps:
            chase_penalty_bps = rebound_bps - max_chase_bps

        range_penalty_bps = 0.0
        max_range_pos = max(0.0, min(1.0, float(self.config.max_range_pos)))
        if context_range_pos > max_range_pos:
            # Penalize buying too close to the local top of the recent context range.
            range_penalty_bps = (context_range_pos - max_range_pos) * 250.0

        hard_block_above_range_pos = max(
            0.0, min(1.0, float(getattr(self.config, "hard_block_above_range_pos", 1.0)))
        )
        hard_block_max_drawdown_bps = max(
            0.0, float(getattr(self.config, "hard_block_max_drawdown_bps", 120.0))
        )
        if (
            context_range_pos >= hard_block_above_range_pos
            and context_drawdown_from_peak_bps <= hard_block_max_drawdown_bps
        ):
            impulse = _impulse_override_signal("top_zone_blocked")
            if impulse is not None:
                return impulse
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "continuation_state": "top_zone_blocked",
                    "context_range_pos": context_range_pos,
                    "context_drawdown_from_peak_bps": context_drawdown_from_peak_bps,
                    "hard_block_above_range_pos": hard_block_above_range_pos,
                    "hard_block_max_drawdown_bps": hard_block_max_drawdown_bps,
                    "structure": structure_meta,
                },
            )
        if _rocket_cooldown_blocked():
            return AlphaSignal(
                ts=features.ts,
                edge_bps=0.0,
                p_up=None,
                meta={
                    "continuation_state": "cooldown_after_rocket",
                    "trend_return_bps": trend_return_bps,
                    "range_pos": structure.range_pos,
                    "extension_bps": structure.extension_bps,
                    "drawdown_from_peak_bps": effective_drawdown_from_peak_bps,
                    "cooloff_drawdown_needed_bps": max(12.0, structure.pivot_reversal_bps * 0.35),
                    "structure": structure_meta,
                },
            )

        edge = 0.0
        edge += trend_return_bps * float(self.config.trend_scale)
        edge += max(0.0, rebound_bps) * float(self.config.rebound_scale)
        edge += pullback_bps * float(self.config.pullback_scale)
        edge += max(0.0, structure.slope_medium_bps) * 0.6
        edge += max(0.0, structure.slope_long_bps) * 0.4
        if phase == "lift_off":
            edge += max(0.0, structure.curvature_bps) * 0.8
            edge += max(0.0, structure.rebound_bps) * 0.08
        edge -= chase_penalty_bps * 2.0
        edge -= range_penalty_bps
        edge -= max(0.0, structure.drawdown_bps - 20.0) * 0.8
        if structure.range_pos >= 0.82:
            edge -= (structure.range_pos - 0.82) * 280.0
        edge = max(0.0, edge)

        max_edge = max(0.0, float(getattr(self.config, "max_edge_bps", 0.0) or 0.0))
        if max_edge > 0.0:
            edge = min(edge, max_edge)

        return AlphaSignal(
            ts=features.ts,
            edge_bps=edge,
            p_up=None,
            meta={
                "continuation_state": "armed" if edge > 0.0 else "filtered",
                "pullback_bps": pullback_bps,
                "rebound_bps": max(0.0, rebound_bps),
                "rebound_confirm_bars": confirm_bars,
                "recent_bias_bps": recent_bias_bps,
                "trend_bias_bps": trend_return_bps * float(self.config.trend_scale),
                "chase_penalty_bps": chase_penalty_bps,
                "range_penalty_bps": range_penalty_bps,
                "structure": structure_meta,
            },
        )
