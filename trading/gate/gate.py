from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from trading.types import CostEstimate, Features, GateDecision


@dataclass
class GateConfig:
    safety_margin_bps: float
    max_spread_bps: float
    max_atr_bps: float
    session_start_utc: int
    session_end_utc: int
    stale_seconds: int


class TradeabilityGate:
    def __init__(self, config: GateConfig):
        self.config = config
        self._last_ts: Optional[datetime] = None

    def _session_allowed(self, ts: datetime) -> bool:
        hour = ts.hour
        if self.config.session_start_utc <= self.config.session_end_utc:
            return self.config.session_start_utc <= hour < self.config.session_end_utc
        return hour >= self.config.session_start_utc or hour < self.config.session_end_utc

    def evaluate(self, features: Features, cost: CostEstimate, predicted_edge_bps: float) -> GateDecision:
        now_ts = features.ts
        if self._last_ts is not None:
            delta = (now_ts - self._last_ts).total_seconds()
            if delta > self.config.stale_seconds:
                return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="stale_data")
        self._last_ts = now_ts

        spread_bps = float(features.values.get("spread_bps", 0.0))
        atr_bps = float(features.values.get("atr_bps", 0.0))
        if spread_bps > self.config.max_spread_bps:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="spread_too_wide")
        if atr_bps > self.config.max_atr_bps:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="vol_too_high")
        if not self._session_allowed(now_ts):
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="session_block")

        required_edge = cost.expected_cost_bps + self.config.safety_margin_bps
        if predicted_edge_bps <= required_edge:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="edge_below_costs")

        size_factor = max(0.0, 1.0 - (cost.expected_cost_bps / max(predicted_edge_bps, 1e-9)))
        return GateDecision(ts=now_ts, allow=True, size_factor=size_factor, reason=None)
