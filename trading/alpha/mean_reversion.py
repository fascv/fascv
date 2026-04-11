from __future__ import annotations

from dataclasses import dataclass

from trading.alpha.base import AlphaModel
from trading.types import AlphaSignal, Features


@dataclass
class MeanReversionConfig:
    lookback: int
    threshold_bps: float
    scale: float
    max_edge_bps: float = 0.0
    soft_threshold_ratio: float = 0.0
    trend_bias_scale: float = 0.0
    trend_bias_max_bps: float = 0.0
    require_reversal_confirmation: bool = False
    reversal_min_last_return_bps: float = 0.0
    reversal_min_prev_pressure_bps: float = 0.0
    reversal_max_last_return_bps: float = 0.0


class MeanReversionAlpha(AlphaModel):
    def __init__(self, config: MeanReversionConfig):
        self.config = config
        self._buffer: list[float] = []

    def predict(self, features: Features) -> AlphaSignal:
        ret_bps = float(features.values.get("return_bps", 0.0))
        trend_return_bps = max(0.0, float(features.values.get("trend_return_bps", 0.0)))
        self._buffer.append(ret_bps)
        if len(self._buffer) > self.config.lookback:
            self._buffer = self._buffer[-self.config.lookback :]

        pressure = sum(self._buffer)
        edge = 0.0
        raw_edge = -pressure * self.config.scale
        threshold = max(0.0, float(self.config.threshold_bps))
        if threshold <= 0.0 or abs(pressure) >= threshold:
            # Mean-reversion: go against the accumulated move.
            edge = raw_edge
        else:
            soft_ratio = max(0.0, float(getattr(self.config, "soft_threshold_ratio", 0.0) or 0.0))
            if soft_ratio > 0.0:
                # Smoothly ramp sub-threshold signals instead of hard-clipping them to zero.
                ramp = abs(pressure) / threshold
                edge = raw_edge * soft_ratio * ramp

        trend_bias_bps = 0.0
        trend_bias_scale = max(0.0, float(getattr(self.config, "trend_bias_scale", 0.0) or 0.0))
        if trend_bias_scale > 0.0 and trend_return_bps > 0.0:
            trend_bias_bps = trend_return_bps * trend_bias_scale
            trend_bias_cap = max(0.0, float(getattr(self.config, "trend_bias_max_bps", 0.0) or 0.0))
            if trend_bias_cap > 0.0:
                trend_bias_bps = min(trend_bias_bps, trend_bias_cap)
            edge += trend_bias_bps

        reversal_confirmed = True
        reversal_reason = ""
        last_return_bps = float(self._buffer[-1]) if self._buffer else 0.0
        prior_pressure_bps = float(sum(self._buffer[:-1])) if len(self._buffer) > 1 else 0.0
        if edge > 0.0 and bool(getattr(self.config, "require_reversal_confirmation", False)):
            min_last_up_bps = max(0.0, float(getattr(self.config, "reversal_min_last_return_bps", 0.0) or 0.0))
            min_prev_drop_bps = max(
                0.0,
                float(getattr(self.config, "reversal_min_prev_pressure_bps", 0.0) or 0.0),
            )
            max_last_up_bps = max(0.0, float(getattr(self.config, "reversal_max_last_return_bps", 0.0) or 0.0))
            if last_return_bps < min_last_up_bps:
                reversal_confirmed = False
                reversal_reason = "last_bar_not_up"
            elif prior_pressure_bps > -min_prev_drop_bps:
                reversal_confirmed = False
                reversal_reason = "prior_drop_too_small"
            elif max_last_up_bps > 0.0 and last_return_bps > max_last_up_bps:
                reversal_confirmed = False
                reversal_reason = "reversal_too_extended"
            if not reversal_confirmed:
                edge = 0.0

        max_edge = float(getattr(self.config, "max_edge_bps", 0.0) or 0.0)
        if max_edge > 0.0:
            edge = max(-max_edge, min(max_edge, edge))

        return AlphaSignal(
            ts=features.ts,
            edge_bps=edge,
            p_up=None,
            meta={
                "reversion_pressure_bps": pressure,
                "trend_bias_bps": trend_bias_bps,
                "last_return_bps": last_return_bps,
                "prior_pressure_bps": prior_pressure_bps,
                "reversal_confirmed": reversal_confirmed,
                "reversal_reason": reversal_reason,
            },
        )
