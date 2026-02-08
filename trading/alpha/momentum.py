from __future__ import annotations

from dataclasses import dataclass

from trading.alpha.base import AlphaModel
from trading.types import AlphaSignal, Features


@dataclass
class MomentumConfig:
    lookback: int
    threshold_bps: float
    scale: float


class MomentumAlpha(AlphaModel):
    def __init__(self, config: MomentumConfig):
        self.config = config
        self._buffer = []

    def predict(self, features: Features) -> AlphaSignal:
        ret_bps = float(features.values.get("return_bps", 0.0))
        self._buffer.append(ret_bps)
        if len(self._buffer) > self.config.lookback:
            self._buffer = self._buffer[-self.config.lookback :]
        momentum = sum(self._buffer)
        edge = 0.0
        if abs(momentum) >= self.config.threshold_bps:
            edge = momentum * self.config.scale
        return AlphaSignal(ts=features.ts, edge_bps=edge, p_up=None, meta={"momentum": momentum})
