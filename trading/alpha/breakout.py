from __future__ import annotations

from dataclasses import dataclass

from trading.alpha.base import AlphaModel
from trading.types import AlphaSignal, Features


@dataclass
class BreakoutConfig:
    lookback: int
    trigger_bps: float
    scale: float
    max_edge_bps: float = 0.0
    bottom_countertrend_block_max_context_range_pos: float = 0.0
    bottom_countertrend_block_max_context_rebound_bps: float = 0.0
    bottom_countertrend_block_max_trend_return_bps: float = 0.0
    top_zone_block_min_context_range_pos: float = 1.0
    top_zone_block_max_context_rebound_bps: float = 0.0
    top_zone_block_min_volume_z: float = -999.0
    thin_rebound_block_context_range_pos: float = 1.0
    thin_rebound_block_context_rebound_bps: float = 0.0
    thin_rebound_block_min_spread_bps: float = 0.0
    mid_rebound_block_context_range_pos: float = 1.0
    mid_rebound_block_context_rebound_bps: float = 0.0
    mid_rebound_block_min_volume_z: float = -999.0
    late_rebound_block_context_range_pos: float = 1.0
    late_rebound_block_context_rebound_bps: float = 0.0
    late_rebound_block_min_volume_z: float = -999.0


class BreakoutAlpha(AlphaModel):
    def __init__(self, config: BreakoutConfig):
        self.config = config
        self._prices: list[float] = []

    def predict(self, features: Features) -> AlphaSignal:
        price = float(features.values.get("price", 0.0))
        if price <= 0.0:
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"breakout_state": "invalid_price"})

        self._prices.append(price)
        window_size = max(2, int(self.config.lookback) + 1)
        if len(self._prices) > window_size:
            self._prices = self._prices[-window_size:]
        if len(self._prices) < window_size:
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"breakout_state": "warmup"})

        prev = self._prices[:-1]
        high_ref = max(prev)
        low_ref = min(prev)
        if high_ref <= 0.0 or low_ref <= 0.0:
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"breakout_state": "invalid_ref"})

        up_break_bps = (price / high_ref - 1.0) * 10000.0
        down_break_bps = (price / low_ref - 1.0) * 10000.0
        trigger = float(self.config.trigger_bps)
        context_range_pos = max(
            0.0,
            min(1.0, float(features.values.get("context_range_pos", 0.0) or 0.0)),
        )
        context_rebound_bps = max(
            0.0,
            float(features.values.get("context_rebound_bps", 0.0) or 0.0),
        )
        spread_bps = max(
            0.0,
            float(features.values.get("spread_bps", 0.0) or 0.0),
        )
        volume_z = float(features.values.get("volume_z", 0.0) or 0.0)
        trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
        bottom_countertrend_block_max_context_range_pos_raw = getattr(
            self.config,
            "bottom_countertrend_block_max_context_range_pos",
            0.0,
        )
        bottom_countertrend_block_max_context_range_pos = max(
            0.0,
            min(
                1.0,
                float(
                    0.0
                    if bottom_countertrend_block_max_context_range_pos_raw is None
                    else bottom_countertrend_block_max_context_range_pos_raw
                ),
            ),
        )
        bottom_countertrend_block_max_context_rebound_bps_raw = getattr(
            self.config,
            "bottom_countertrend_block_max_context_rebound_bps",
            0.0,
        )
        bottom_countertrend_block_max_context_rebound_bps = max(
            0.0,
            float(
                0.0
                if bottom_countertrend_block_max_context_rebound_bps_raw is None
                else bottom_countertrend_block_max_context_rebound_bps_raw
            ),
        )
        bottom_countertrend_block_max_trend_return_bps_raw = getattr(
            self.config,
            "bottom_countertrend_block_max_trend_return_bps",
            0.0,
        )
        bottom_countertrend_block_max_trend_return_bps = float(
            0.0
            if bottom_countertrend_block_max_trend_return_bps_raw is None
            else bottom_countertrend_block_max_trend_return_bps_raw
        )
        top_zone_block_min_context_range_pos_raw = getattr(
            self.config,
            "top_zone_block_min_context_range_pos",
            1.0,
        )
        top_zone_block_min_context_range_pos = max(
            0.0,
            min(
                1.0,
                float(
                    1.0
                    if top_zone_block_min_context_range_pos_raw is None
                    else top_zone_block_min_context_range_pos_raw
                ),
            ),
        )
        top_zone_block_max_context_rebound_bps_raw = getattr(
            self.config,
            "top_zone_block_max_context_rebound_bps",
            0.0,
        )
        top_zone_block_max_context_rebound_bps = max(
            0.0,
            float(
                0.0
                if top_zone_block_max_context_rebound_bps_raw is None
                else top_zone_block_max_context_rebound_bps_raw
            ),
        )
        top_zone_block_min_volume_z_raw = getattr(
            self.config,
            "top_zone_block_min_volume_z",
            -999.0,
        )
        top_zone_block_min_volume_z = float(
            -999.0 if top_zone_block_min_volume_z_raw is None else top_zone_block_min_volume_z_raw
        )
        thin_rebound_block_context_range_pos_raw = getattr(
            self.config,
            "thin_rebound_block_context_range_pos",
            1.0,
        )
        thin_rebound_block_context_range_pos = max(
            0.0,
            min(
                1.0,
                float(
                    1.0
                    if thin_rebound_block_context_range_pos_raw is None
                    else thin_rebound_block_context_range_pos_raw
                ),
            ),
        )
        thin_rebound_block_context_rebound_bps_raw = getattr(
            self.config,
            "thin_rebound_block_context_rebound_bps",
            0.0,
        )
        thin_rebound_block_context_rebound_bps = max(
            0.0,
            float(
                0.0
                if thin_rebound_block_context_rebound_bps_raw is None
                else thin_rebound_block_context_rebound_bps_raw
            ),
        )
        thin_rebound_block_min_spread_bps_raw = getattr(
            self.config,
            "thin_rebound_block_min_spread_bps",
            0.0,
        )
        thin_rebound_block_min_spread_bps = max(
            0.0,
            float(
                0.0
                if thin_rebound_block_min_spread_bps_raw is None
                else thin_rebound_block_min_spread_bps_raw
            ),
        )
        mid_rebound_block_context_range_pos_raw = getattr(
            self.config,
            "mid_rebound_block_context_range_pos",
            1.0,
        )
        mid_rebound_block_context_range_pos = max(
            0.0,
            min(
                1.0,
                float(
                    1.0
                    if mid_rebound_block_context_range_pos_raw is None
                    else mid_rebound_block_context_range_pos_raw
                ),
            ),
        )
        mid_rebound_block_context_rebound_bps_raw = getattr(
            self.config,
            "mid_rebound_block_context_rebound_bps",
            0.0,
        )
        mid_rebound_block_context_rebound_bps = max(
            0.0,
            float(
                0.0
                if mid_rebound_block_context_rebound_bps_raw is None
                else mid_rebound_block_context_rebound_bps_raw
            ),
        )
        mid_rebound_block_min_volume_z_raw = getattr(
            self.config,
            "mid_rebound_block_min_volume_z",
            -999.0,
        )
        mid_rebound_block_min_volume_z = float(
            -999.0 if mid_rebound_block_min_volume_z_raw is None else mid_rebound_block_min_volume_z_raw
        )
        late_rebound_block_context_range_pos_raw = getattr(
            self.config,
            "late_rebound_block_context_range_pos",
            1.0,
        )
        late_rebound_block_context_range_pos = max(
            0.0,
            min(
                1.0,
                float(
                    1.0
                    if late_rebound_block_context_range_pos_raw is None
                    else late_rebound_block_context_range_pos_raw
                ),
            ),
        )
        late_rebound_block_context_rebound_bps_raw = getattr(
            self.config,
            "late_rebound_block_context_rebound_bps",
            0.0,
        )
        late_rebound_block_context_rebound_bps = max(
            0.0,
            float(
                0.0
                if late_rebound_block_context_rebound_bps_raw is None
                else late_rebound_block_context_rebound_bps_raw
            ),
        )
        late_rebound_block_min_volume_z_raw = getattr(
            self.config,
            "late_rebound_block_min_volume_z",
            -999.0,
        )
        late_rebound_block_min_volume_z = float(
            -999.0
            if late_rebound_block_min_volume_z_raw is None
            else late_rebound_block_min_volume_z_raw
        )
        late_rebound_blocked = (
            up_break_bps >= trigger
            and late_rebound_block_context_rebound_bps > 0.0
            and context_range_pos >= late_rebound_block_context_range_pos
            and context_rebound_bps >= late_rebound_block_context_rebound_bps
            and volume_z < late_rebound_block_min_volume_z
        )
        bottom_countertrend_blocked = (
            up_break_bps >= trigger
            and bottom_countertrend_block_max_context_range_pos > 0.0
            and context_range_pos <= bottom_countertrend_block_max_context_range_pos
            and bottom_countertrend_block_max_context_rebound_bps > 0.0
            and context_rebound_bps <= bottom_countertrend_block_max_context_rebound_bps
            and trend_return_bps <= bottom_countertrend_block_max_trend_return_bps
        )
        top_zone_blocked = (
            up_break_bps >= trigger
            and top_zone_block_max_context_rebound_bps > 0.0
            and context_range_pos >= top_zone_block_min_context_range_pos
            and context_rebound_bps <= top_zone_block_max_context_rebound_bps
            and volume_z < top_zone_block_min_volume_z
        )
        thin_rebound_blocked = (
            up_break_bps >= trigger
            and thin_rebound_block_context_rebound_bps > 0.0
            and context_range_pos >= thin_rebound_block_context_range_pos
            and context_rebound_bps >= thin_rebound_block_context_rebound_bps
            and spread_bps >= thin_rebound_block_min_spread_bps
        )
        mid_rebound_blocked = (
            up_break_bps >= trigger
            and mid_rebound_block_context_rebound_bps > 0.0
            and context_range_pos >= mid_rebound_block_context_range_pos
            and context_rebound_bps >= mid_rebound_block_context_rebound_bps
            and volume_z < mid_rebound_block_min_volume_z
        )
        edge = 0.0
        state = "none"
        if late_rebound_blocked:
            state = "late_rebound_block"
        elif top_zone_blocked:
            state = "top_zone_block"
        elif bottom_countertrend_blocked:
            state = "bottom_countertrend_block"
        elif thin_rebound_blocked:
            state = "thin_rebound_spread_block"
        elif mid_rebound_blocked:
            state = "mid_rebound_block"
        elif up_break_bps >= trigger:
            state = "up_breakout"
            edge = up_break_bps * float(self.config.scale)
        elif down_break_bps <= -trigger:
            state = "down_breakout"
            edge = down_break_bps * float(self.config.scale)

        max_edge = float(getattr(self.config, "max_edge_bps", 0.0) or 0.0)
        if max_edge > 0.0:
            edge = max(-max_edge, min(max_edge, edge))

        return AlphaSignal(
            ts=features.ts,
            edge_bps=edge,
            p_up=None,
            meta={
                "breakout_state": state,
                "breakout_up_bps": up_break_bps,
                "breakout_down_bps": down_break_bps,
                "breakout_high_ref": high_ref,
                "breakout_low_ref": low_ref,
                "breakout_context_range_pos": context_range_pos,
                "breakout_context_rebound_bps": context_rebound_bps,
                "breakout_trend_return_bps": trend_return_bps,
                "breakout_spread_bps": spread_bps,
                "breakout_volume_z": volume_z,
                "breakout_block_reason": (
                    "late_rebound_low_volume"
                    if late_rebound_blocked
                    else (
                        "top_zone_weak_volume"
                        if top_zone_blocked
                        else (
                            "bottom_countertrend_negative_drift"
                            if bottom_countertrend_blocked
                            else (
                                "thin_rebound_high_spread"
                                if thin_rebound_blocked
                                else ("mid_rebound_low_volume" if mid_rebound_blocked else "")
                            )
                        )
                    )
                ),
            },
        )
