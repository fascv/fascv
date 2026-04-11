import unittest
from datetime import datetime, timezone

from trading.order.builder import OrderBuilder, OrderConfig
from trading.types import RiskDecision


class TestOrderBuilderCycle(unittest.TestCase):
    def _builder(self, *, cycle_eur: float, slice_count: int = 1) -> OrderBuilder:
        return OrderBuilder(
            OrderConfig(
                order_type="market",
                post_only=False,
                limit_offset_bps=0.0,
                min_trade_btc=0.0001,
                slice_count=slice_count,
                cycle_trade_eur=cycle_eur,
            )
        )

    def test_cycle_enters_once_with_fixed_notional(self) -> None:
        b = self._builder(cycle_eur=20.0)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        # Risk target allows it (max exposure etc), so cycle should enter with 20 EUR notional.
        r = RiskDecision(ts=ts, allow=True, target_position_btc=1.0)
        orders = b.build(r, current_position_btc=0.0, price=10000.0, cash_eur=1000.0)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, "buy")
        self.assertAlmostEqual(orders[0].qty_btc, 0.002, places=12)

    def test_cycle_clamps_to_risk_target(self) -> None:
        b = self._builder(cycle_eur=20.0)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        # Risk only wants 0.001 BTC, so even though 20 EUR would be 0.002 BTC, we clamp to 0.001.
        r = RiskDecision(ts=ts, allow=True, target_position_btc=0.001)
        orders = b.build(r, current_position_btc=0.0, price=10000.0, cash_eur=1000.0)
        self.assertEqual(len(orders), 1)
        self.assertAlmostEqual(orders[0].qty_btc, 0.001, places=12)

    def test_cycle_clamps_to_cash(self) -> None:
        b = self._builder(cycle_eur=20.0)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        r = RiskDecision(ts=ts, allow=True, target_position_btc=1.0)
        # Only 5 EUR cash => 0.0005 BTC at 10k.
        orders = b.build(r, current_position_btc=0.0, price=10000.0, cash_eur=5.0)
        self.assertEqual(len(orders), 1)
        self.assertAlmostEqual(orders[0].qty_btc, 0.0005, places=12)

    def test_cycle_does_not_resize_while_in_position(self) -> None:
        b = self._builder(cycle_eur=20.0)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        r = RiskDecision(ts=ts, allow=True, target_position_btc=1.0)
        orders = b.build(r, current_position_btc=0.001, price=10000.0, cash_eur=1000.0)
        self.assertEqual(orders, [])

    def test_cycle_exits_fully_when_target_is_flat(self) -> None:
        b = self._builder(cycle_eur=20.0)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        r = RiskDecision(ts=ts, allow=True, target_position_btc=0.0)
        orders = b.build(r, current_position_btc=0.0015, price=10000.0, cash_eur=1000.0)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].side, "sell")
        self.assertAlmostEqual(orders[0].qty_btc, 0.0015, places=12)

    def test_cycle_respects_slicing(self) -> None:
        b = self._builder(cycle_eur=20.0, slice_count=2)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        r = RiskDecision(ts=ts, allow=True, target_position_btc=1.0)
        orders = b.build(r, current_position_btc=0.0, price=10000.0, cash_eur=1000.0)
        self.assertEqual(len(orders), 2)
        self.assertAlmostEqual(orders[0].qty_btc, 0.001, places=12)
        self.assertAlmostEqual(orders[1].qty_btc, 0.001, places=12)


if __name__ == "__main__":
    unittest.main()

