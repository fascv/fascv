from datetime import datetime, timedelta, timezone
import unittest

from trading.gate.gate import GateConfig, TradeabilityGate
from trading.types import CostEstimate, Features


class TestGateStaleRecovery(unittest.TestCase):
    def test_stale_gap_blocks_once_then_recovers(self):
        gate = TradeabilityGate(
            GateConfig(
                safety_margin_bps=0.0,
                max_spread_bps=1000.0,
                min_atr_bps=0.0,
                max_atr_bps=1000.0,
                session_start_utc=0,
                session_end_utc=24,
                stale_seconds=10,
            )
        )

        t0 = datetime(2026, 2, 16, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=30)  # stale gap
        t2 = t1 + timedelta(seconds=5)   # normal cadence again

        features0 = Features(ts=t0, values={"spread_bps": 0.1, "atr_bps": 0.1})
        features1 = Features(ts=t1, values={"spread_bps": 0.1, "atr_bps": 0.1})
        features2 = Features(ts=t2, values={"spread_bps": 0.1, "atr_bps": 0.1})
        cost = CostEstimate(ts=t0, fee_bps=1.0, spread_bps=0.0, slippage_bps=0.0, expected_cost_bps=1.0)

        d0 = gate.evaluate(features0, cost, predicted_edge_bps=10.0)
        d1 = gate.evaluate(features1, cost, predicted_edge_bps=10.0)
        d2 = gate.evaluate(features2, cost, predicted_edge_bps=10.0)

        self.assertTrue(d0.allow)
        self.assertEqual(d1.reason, "stale")
        self.assertTrue(d2.allow)


if __name__ == "__main__":
    unittest.main()
