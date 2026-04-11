from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trading.risk.exit_observer import ExitLearningObserver
from trading.types import Fill


def _position(qty: float, avg_entry: float, realized: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        position_btc=qty,
        avg_entry_price=avg_entry,
        realized_pnl_eur=realized,
    )


class TestExitLearningObserver(unittest.TestCase):
    def test_records_peak_giveback_on_flatten_sell(self) -> None:
        observer = ExitLearningObserver(symbol="XLM", pair="XLM/USDC", position_epsilon=1e-9)
        ts = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)

        observer.on_fill(
            fill=Fill(ts=ts, side="buy", qty_btc=20.0, price=1.0, fee_eur=0.0, order_id="buy-1"),
            before_position=_position(0.0, 0.0),
            after_position=_position(20.0, 1.0),
        )
        observer.on_mark(
            ts=ts + timedelta(minutes=1),
            position=_position(20.0, 1.0),
            price=1.006,
            expected_cost_bps=14.0,
            alpha_type="auto",
            active_strategy="breakout",
        )
        observer.on_decision(
            ts=ts + timedelta(minutes=2),
            position=_position(20.0, 1.0),
            price=1.003,
            reason="profit_roll_exit",
            target_qty=0.0,
            expected_cost_bps=14.0,
            alpha_type="auto",
            active_strategy="breakout",
        )
        events = observer.on_fill(
            fill=Fill(
                ts=ts + timedelta(minutes=2),
                side="sell",
                qty_btc=20.0,
                price=1.003,
                fee_eur=0.01,
                order_id="sell-1",
            ),
            before_position=_position(20.0, 1.0, realized=0.0),
            after_position=_position(0.0, 0.0, realized=0.05),
        )

        self.assertEqual(len(events), 1)
        payload = events[0]
        self.assertEqual(payload["symbol"], "XLM")
        self.assertEqual(payload["pair"], "XLM/USDC")
        self.assertEqual(payload["exit_reason"], "profit_roll_exit")
        self.assertAlmostEqual(payload["peak_pnl_eur"], 0.12, places=9)
        self.assertAlmostEqual(payload["exit_open_pnl_eur"], 0.06, places=9)
        self.assertAlmostEqual(payload["retrace_from_peak_eur"], 0.06, places=9)
        self.assertAlmostEqual(payload["peak_pnl_bps"], 60.0, places=9)
        self.assertAlmostEqual(payload["exit_pnl_bps"], 30.0, places=9)
        self.assertAlmostEqual(payload["realized_pnl_delta_eur"], 0.05, places=9)
        self.assertEqual(payload["active_strategy"], "breakout")
        self.assertEqual(observer.snapshot(), None)

    def test_mark_detected_open_position_emits_when_flat_detected(self) -> None:
        observer = ExitLearningObserver(symbol="CVX", pair="CVX/USDC", position_epsilon=1e-9)
        ts = datetime(2026, 4, 11, 10, 0, tzinfo=timezone.utc)

        observer.on_mark(ts=ts, position=_position(5.0, 2.0), price=2.02)
        observer.on_mark(ts=ts + timedelta(minutes=1), position=_position(5.0, 2.0), price=2.05)
        events = observer.on_mark(
            ts=ts + timedelta(minutes=2),
            position=_position(0.0, 0.0, realized=0.20),
            price=2.03,
        )

        self.assertEqual(len(events), 1)
        payload = events[0]
        self.assertEqual(payload["entry_source"], "mark_detected")
        self.assertEqual(payload["exit_source"], "position_flat_detected")
        self.assertGreater(payload["peak_pnl_eur"], payload["exit_open_pnl_eur"])


if __name__ == "__main__":
    unittest.main()
