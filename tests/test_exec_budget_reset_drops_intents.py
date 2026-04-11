import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from queue import Empty, Queue

import trading.processes.exec as execp
from trading.ipc.events import ControlCommand, JournalEvent, OrderIntent
from trading.processes.context import ProcessContext


class TestExecBudgetResetDropsIntents(unittest.TestCase):
    def test_set_budget_drops_queued_intents(self):
        with tempfile.TemporaryDirectory() as td:
            journal_path = os.path.join(td, "journal.jsonl")
            ctx = ProcessContext(
                mode="sim",
                config={
                    "exec": {
                        "heartbeat_interval": 0.1,
                        "telemetry_interval": 0.1,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "deadman_in_paper": False,
                    },
                    "execution": {"latency_bars": 0, "partial_fill_ratio": 1.0, "slippage_bps": 1.0},
                    "cost": {"maker_fee_bps": 0.0, "taker_fee_bps": 0.0},
                    "journal": {"json_path": journal_path},
                },
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

            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()

            # Queue a few intents.
            base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for i in range(5):
                ctx.q_order_intent.put(
                    OrderIntent(
                        ts=base_ts,
                        side="buy",
                        qty_btc=0.001,
                        order_type="market",
                        client_id=f"intent_test_{i}",
                        reason="test",
                    )
                )

            ctx.q_control_exec.put(
                ControlCommand(
                    ts=datetime.now(timezone.utc),
                    action="SET_BUDGET",
                    reason="budget_reset",
                    payload={"starting_cash_eur": 1000.0, "max_exposure_eur": 200.0, "reset": True},
                )
            )

            deadline = time.time() + 3.0
            seen = None
            while time.time() < deadline and seen is None:
                try:
                    evt = ctx.q_journal.get_nowait()
                except Empty:
                    time.sleep(0.05)
                    continue
                if isinstance(evt, JournalEvent) and evt.event_type == "exec_intents_dropped":
                    seen = evt
                    break

            ctx.stop_event.set()
            t.join(timeout=2.0)

            self.assertIsNotNone(seen)
            self.assertGreaterEqual(int(seen.payload.get("count", 0)), 5)


if __name__ == "__main__":
    unittest.main()

