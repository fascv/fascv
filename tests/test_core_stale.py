import threading
import time
import unittest
from queue import Queue

from trading.processes.context import ProcessContext
from trading.processes.core import run_core
from trading.ipc.events import ControlCommand


class TestCoreStale(unittest.TestCase):
    def test_core_stale_sends_stop_and_cancel(self):
        ctx = ProcessContext(
            mode="live",
            config={
                "core": {"stale_seconds": 0.1, "trading_enabled": True, "max_orders_per_min": 1},
                "features": {},
                "alpha": {},
                "cost": {},
                "gate": {},
                "risk": {},
                "order": {},
                "general": {"starting_cash_eur": 100.0},
                "data": {"default_micro": {}},
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

        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()
        time.sleep(0.5)
        ctx.stop_event.set()
        t.join(timeout=1.0)

        actions_exec = []
        while not ctx.q_control_exec.empty():
            cmd = ctx.q_control_exec.get_nowait()
            if isinstance(cmd, ControlCommand):
                actions_exec.append(cmd.action)
        self.assertIn("CANCEL_ALL", actions_exec)
        self.assertIn("STOP", actions_exec)


if __name__ == "__main__":
    unittest.main()
