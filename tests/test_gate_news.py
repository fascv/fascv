import unittest
from datetime import datetime, timedelta, timezone

from trading.gate.gate import GateConfig, TradeabilityGate
from trading.types import CostEstimate, Features


class TestGateNews(unittest.TestCase):
    def _base_gate(self) -> TradeabilityGate:
        return TradeabilityGate(
            GateConfig(
                safety_margin_bps=0.5,
                max_spread_bps=20.0,
                min_atr_bps=0.0,
                max_atr_bps=250.0,
                session_start_utc=0,
                session_end_utc=24,
                stale_seconds=3600,
                block_on_high_news_impact=True,
                max_news_impact=0.6,
                max_news_age_sec=120,
                news_safety_margin_bps=0.0,
            )
        )

    def _features(self, ts: datetime, news_impact: float, news_age_sec: float, source_count: float = 1.0) -> Features:
        return Features(
            ts=ts,
            values={
                "spread_bps": 8.0,
                "atr_bps": 50.0,
                "news_impact": news_impact,
                "news_age_sec": news_age_sec,
                "news_source_count": source_count,
            },
        )

    def _cost(self, ts: datetime) -> CostEstimate:
        return CostEstimate(
            ts=ts,
            fee_bps=3.0,
            spread_bps=4.0,
            slippage_bps=0.5,
            expected_cost_bps=7.5,
        )

    def test_gate_blocks_high_news_impact(self):
        gate = self._base_gate()
        ts = datetime.now(timezone.utc)
        decision = gate.evaluate(self._features(ts, news_impact=0.8, news_age_sec=10), self._cost(ts), predicted_edge_bps=20.0)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "news_impact")

    def test_gate_blocks_stale_news_when_configured(self):
        gate = self._base_gate()
        ts = datetime.now(timezone.utc)
        decision = gate.evaluate(self._features(ts, news_impact=0.1, news_age_sec=360), self._cost(ts), predicted_edge_bps=20.0)
        self.assertFalse(decision.allow)
        self.assertEqual(decision.reason, "news_stale")

    def test_gate_adds_news_margin_to_required_edge(self):
        gate = TradeabilityGate(
            GateConfig(
                safety_margin_bps=0.5,
                max_spread_bps=20.0,
                min_atr_bps=0.0,
                max_atr_bps=250.0,
                session_start_utc=0,
                session_end_utc=24,
                stale_seconds=3600,
                block_on_high_news_impact=False,
                max_news_impact=10.0,
                max_news_age_sec=3600,
                news_safety_margin_bps=2.0,
            )
        )
        ts = datetime.now(timezone.utc)
        # required_edge = 7.5 + 0.5 + (0.5*2.0) = 9.0
        blocked = gate.evaluate(self._features(ts, news_impact=0.5, news_age_sec=10), self._cost(ts), predicted_edge_bps=8.9)
        allowed = gate.evaluate(self._features(ts + timedelta(seconds=1), news_impact=0.5, news_age_sec=10), self._cost(ts), predicted_edge_bps=9.1)
        self.assertFalse(blocked.allow)
        self.assertEqual(blocked.reason, "edge_below_costs")
        self.assertTrue(allowed.allow)

    def test_gate_missing_news_is_neutral(self):
        gate = self._base_gate()
        ts = datetime.now(timezone.utc)
        no_news = Features(
            ts=ts,
            values={
                "spread_bps": 8.0,
                "atr_bps": 50.0,
                "news_impact": 0.9,
                "news_age_sec": 999.0,
                "news_source_count": 0.0,
            },
        )
        decision = gate.evaluate(no_news, self._cost(ts), predicted_edge_bps=20.0)
        self.assertTrue(decision.allow)
        self.assertIsNone(decision.reason)


if __name__ == "__main__":
    unittest.main()
