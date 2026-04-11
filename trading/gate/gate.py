from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from trading.types import CostEstimate, Features, GateDecision


@dataclass
class GateConfig:
    safety_margin_bps: float
    max_spread_bps: float
    min_atr_bps: float
    max_atr_bps: float
    session_start_utc: int
    session_end_utc: int
    stale_seconds: int
    # Fraction of expected execution costs that must be covered by edge at gate level.
    # 1.0 = strict full-cost coverage (default), <1.0 = earlier entries for scalp profiles.
    cost_coverage_ratio: float = 1.0
    # New entries should typically pay for the full roundtrip, not just one execution side.
    cost_roundtrip_multiplier: float = 1.0
    block_on_high_news_impact: bool = False
    max_news_impact: float = 1.0
    max_news_age_sec: int = 3600
    news_safety_margin_bps: float = 0.0


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
        prev_ts = self._last_ts
        # Always advance the watermark first. A stale gap should block the current tick only,
        # not permanently block all future ticks after the first gap.
        self._last_ts = now_ts
        if prev_ts is not None:
            delta = (now_ts - prev_ts).total_seconds()
            if self.config.stale_seconds > 0 and delta > self.config.stale_seconds:
                return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="stale")

        spread_bps = float(features.values.get("spread_bps", 0.0))
        atr_bps = float(features.values.get("atr_bps", 0.0))
        if spread_bps > self.config.max_spread_bps:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="spread")
        if atr_bps < self.config.min_atr_bps:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="volatility_low")
        if atr_bps > self.config.max_atr_bps:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="volatility")
        if not self._session_allowed(now_ts):
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="session")

        news_impact = abs(float(features.values.get("news_impact", 0.0)))
        news_age_sec = max(0.0, float(features.values.get("news_age_sec", 0.0)))
        news_source_count = float(features.values.get("news_source_count", 0.0))
        news_active = news_source_count > 0.0
        if self.config.block_on_high_news_impact and news_active and news_impact > self.config.max_news_impact:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="news_impact")
        if self.config.block_on_high_news_impact and news_active and news_age_sec > self.config.max_news_age_sec:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="news_stale")

        safety_margin = self.config.safety_margin_bps
        if news_active:
            safety_margin += news_impact * self.config.news_safety_margin_bps
        available_edge_bps = predicted_edge_bps - safety_margin
        cost_coverage_ratio = max(0.0, min(1.0, float(getattr(self.config, "cost_coverage_ratio", 1.0))))
        cost_roundtrip_multiplier = max(
            1.0, float(getattr(self.config, "cost_roundtrip_multiplier", 1.0) or 1.0)
        )
        effective_cost_bps = (
            max(0.0, float(cost.expected_cost_bps))
            * cost_coverage_ratio
            * cost_roundtrip_multiplier
        )
        if effective_cost_bps >= available_edge_bps:
            return GateDecision(ts=now_ts, allow=False, size_factor=0.0, reason="edge_below_costs")

        size_factor = max(0.0, 1.0 - (effective_cost_bps / max(available_edge_bps, 1e-9)))
        return GateDecision(ts=now_ts, allow=True, size_factor=size_factor, reason=None)
