import unittest
from datetime import datetime, timezone

from trading.ipc.events import OrderIntent
from trading.processes.exec import (
    _adjust_sell_qty_for_balance,
    _effective_min_entry_notional,
    _intent_reference_price,
)


class TestExecBinanceNotional(unittest.TestCase):
    def test_adjust_sell_qty_relaxes_buffer_when_full_size_meets_min_notional(self):
        qty, applied_buffer = _adjust_sell_qty_for_balance(
            requested_qty=3.2069505,
            available_qty=3.2069505,
            buffer_qty=0.2,
            reference_price=1.6215,
            min_notional=5.0,
        )
        self.assertAlmostEqual(qty, 3.2069505, places=9)
        self.assertAlmostEqual(applied_buffer, 0.0, places=9)

    def test_adjust_sell_qty_keeps_buffer_when_clamped_size_is_still_sellable(self):
        qty, applied_buffer = _adjust_sell_qty_for_balance(
            requested_qty=10.0,
            available_qty=10.0,
            buffer_qty=0.2,
            reference_price=1.6215,
            min_notional=5.0,
        )
        self.assertAlmostEqual(qty, 9.8, places=9)
        self.assertAlmostEqual(applied_buffer, 0.2, places=9)

    def test_effective_min_entry_notional_uses_maximum_constraint(self):
        self.assertAlmostEqual(
            _effective_min_entry_notional(configured_min_notional=6.0, exchange_min_notional=5.0),
            6.0,
            places=9,
        )
        self.assertAlmostEqual(
            _effective_min_entry_notional(configured_min_notional=0.0, exchange_min_notional=5.0),
            5.0,
            places=9,
        )

    def test_intent_reference_price_prefers_meta_reference_price(self):
        intent = OrderIntent(
            ts=datetime.now(timezone.utc),
            side="buy",
            qty_btc=1.0,
            order_type="market",
            meta={"reference_price": 1.625},
        )
        self.assertAlmostEqual(_intent_reference_price(intent), 1.625, places=9)


if __name__ == "__main__":
    unittest.main()
