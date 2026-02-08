from __future__ import annotations

from dataclasses import dataclass

from trading.types import CostEstimate, Features


@dataclass
class FeeConfig:
    maker_bps: float
    taker_bps: float


@dataclass
class SlippageConfig:
    base_bps: float
    vol_mult: float


class CostModel:
    def __init__(
        self,
        fee_config: FeeConfig,
        slippage_config: SlippageConfig,
        spread_component_factor: float = 0.5,
    ):
        self.fee_config = fee_config
        self.slippage_config = slippage_config
        self.spread_component_factor = spread_component_factor

    def estimate(self, features: Features, order_type: str) -> CostEstimate:
        spread_bps = float(features.values.get("spread_bps", 0.0))
        atr_bps = float(features.values.get("atr_bps", 0.0))
        fee_bps = self.fee_config.maker_bps if order_type == "limit" else self.fee_config.taker_bps
        spread_component = spread_bps * self.spread_component_factor
        slippage_component = self.slippage_config.base_bps + self.slippage_config.vol_mult * atr_bps
        expected = fee_bps + spread_component + slippage_component
        return CostEstimate(
            ts=features.ts,
            fee_bps=fee_bps,
            spread_bps=spread_component,
            slippage_bps=slippage_component,
            expected_cost_bps=expected,
        )
