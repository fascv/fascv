import threading
import unittest
from datetime import datetime, timezone
from queue import Queue

from trading.execution.state_machine import OrderStateMachine
from trading.kraken.ws_auth import OwnTradeUpdate
from trading.processes.context import ProcessContext
from trading.processes.exec import OwnTradesDeduper, _handle_own_trade


class TestOwnTradesDedupe(unittest.TestCase):
    def test_duplicate_trade_id_processed_once(self):
        ctx = ProcessContext(
            mode="live",
            config={},
            stop_event=threading.Event(),
            q_market_core=Queue(),
            q_market_exec=Queue(),
            q_order_intent=Queue(),
            q_exec_report=Queue(),
            q_journal=Queue(),
            q_control_core=Queue(),
            q_control_exec=Queue(),
            q_telemetry=Queue(),
            q_heartbeat=Queue(),
        )
        sm = OrderStateMachine()
        lock = threading.Lock()
        deduper = OwnTradesDeduper(maxlen=10)

        update = OwnTradeUpdate(
            ts=datetime.now(timezone.utc),
            trade_id="T123",
            order_id="O1",
            side="buy",
            price=100.0,
            vol=0.01,
            fee=0.1,
        )

        _handle_own_trade(ctx, update, sm, lock, deduper)
        _handle_own_trade(ctx, update, sm, lock, deduper)

        fills = []
        while not ctx.q_exec_report.empty():
            fills.append(ctx.q_exec_report.get_nowait())
        self.assertEqual(len(fills), 1)


if __name__ == "__main__":
    unittest.main()
