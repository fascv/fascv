from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from trading.alpha.base import AlphaModel
from trading.types import AlphaSignal, Features


@dataclass
class AutoRegimeConfig:
    lookback: int = 6
    trend_momentum_bps: float = 6.0
    range_momentum_bps: float = 2.0
    high_vol_atr_bps: float = 120.0
    low_vol_atr_bps: float = 60.0
    breakout_return_bps: float = 8.0
    context_pullback_bps: float = 0.0
    context_rebound_bps: float = 0.0
    context_trend_bps: float = 0.0
    context_range_low: float = 0.35
    context_range_high: float = 0.65
    default_regime: str = "trend"
    trend_strategy: str = "trend"
    range_strategy: str = "mean_reversion"
    breakout_strategy: str = "breakout"


class RegimeSwitchingAlpha(AlphaModel):
    def __init__(
        self,
        strategies: Dict[str, AlphaModel],
        config: AutoRegimeConfig,
    ):
        self.strategies = dict(strategies)
        self.config = config
        self._ret_buffer: list[float] = []

    def _select_regime(self, features: Features) -> tuple[str, str]:
        ret_bps = float(features.values.get("return_bps", 0.0))
        trend_ret_bps = float(features.values.get("trend_return_bps", ret_bps))
        atr_bps = abs(float(features.values.get("atr_bps", 0.0)))
        ctx_return_bps = float(features.values.get("context_return_bps", 0.0))
        ctx_drawdown_bps = float(features.values.get("context_drawdown_bps", 0.0))
        ctx_rebound_bps = float(features.values.get("context_rebound_bps", 0.0))
        ctx_range_pos = float(features.values.get("context_range_pos", 0.5))
        self._ret_buffer.append(ret_bps)
        lb = max(1, int(self.config.lookback))
        if len(self._ret_buffer) > lb:
            self._ret_buffer = self._ret_buffer[-lb:]
        momentum_bps = sum(self._ret_buffer)

        if abs(ret_bps) >= float(self.config.breakout_return_bps):
            return "breakout", "breakout_return"
        if (
            float(self.config.context_pullback_bps) > 0.0
            and ctx_drawdown_bps <= -abs(float(self.config.context_pullback_bps))
            and ret_bps > 0.0
            and ctx_range_pos <= float(self.config.context_range_high)
        ):
            return "range", "context_pullback_rebound"
        if (
            float(self.config.context_rebound_bps) > 0.0
            and ctx_rebound_bps >= abs(float(self.config.context_rebound_bps))
            and ret_bps < 0.0
            and ctx_range_pos >= float(self.config.context_range_low)
        ):
            return "range", "context_rebound_fade"
        if (
            float(self.config.context_trend_bps) > 0.0
            and abs(ctx_return_bps) >= abs(float(self.config.context_trend_bps))
            and (ctx_return_bps * trend_ret_bps) > 0.0
        ):
            return "trend", "context_trend"
        if atr_bps >= float(self.config.high_vol_atr_bps) and abs(momentum_bps) >= float(self.config.trend_momentum_bps):
            return "trend", "high_vol_trend"
        if atr_bps <= float(self.config.low_vol_atr_bps) and abs(momentum_bps) <= float(self.config.range_momentum_bps):
            return "range", "low_vol_range"
        return str(self.config.default_regime), "default"

    def _strategy_name_for_regime(self, regime: str) -> str:
        if regime == "trend":
            return str(self.config.trend_strategy)
        if regime == "range":
            return str(self.config.range_strategy)
        if regime == "breakout":
            return str(self.config.breakout_strategy)
        return str(self.config.trend_strategy)

    def predict(self, features: Features) -> AlphaSignal:
        regime, reason = self._select_regime(features)
        strategy_name = self._strategy_name_for_regime(regime)
        strategy = self.strategies.get(strategy_name)
        if strategy is None:
            strategy_name = str(self.config.trend_strategy)
            strategy = self.strategies.get(strategy_name)
        if strategy is None:
            # Fallback safety: no strategy configured.
            return AlphaSignal(ts=features.ts, edge_bps=0.0, p_up=None, meta={"regime": regime, "reason": "no_strategy"})

        out = strategy.predict(features)
        meta = dict(out.meta or {})
        meta.update(
            {
                "regime": regime,
                "regime_reason": reason,
                "active_strategy": strategy_name,
            }
        )
        return AlphaSignal(ts=out.ts, edge_bps=float(out.edge_bps), p_up=out.p_up, meta=meta)
