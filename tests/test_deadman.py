import threading
import time
import unittest
from queue import Queue

import trading.processes.exec as execp
from trading.ipc.events import ControlCommand
from trading.processes.context import ProcessContext


class _FakeRest:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def cancel_all_orders_after(self, timeout: int):
        self.calls.append(int(timeout))
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def add_order(self, *args, **kwargs):
        return {"txid": []}

    def get_ws_token(self):
        return "token"


class _FakeWS:
    def __init__(self, *args, **kwargs):
        pass

    async def stream(self):
        if False:
            yield None


class TestDeadman(unittest.TestCase):
    def test_stop_disables_deadman(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRest
        execp.OpenOrdersWS = _FakeWS
        execp.OwnTradesWS = _FakeWS
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {"deadman_timeout_sec": 60, "deadman_tick_sec": 0.1, "rate_limit_pause_sec": 0.1},
                    "live": {"api_key": "k", "api_secret": "c2VjcmV0", "rest_url": "https://api.kraken.com"},
                    "md": {},
                    "cost": {},
                    "execution": {},
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
            time.sleep(1.2)
            from datetime import datetime, timezone
            ctx.q_control_exec.put(ControlCommand(ts=datetime.now(timezone.utc), action="STOP", reason="test"))
            deadline = time.time() + 3.0
            timeouts = []
            events = []
            while time.time() < deadline:
                while not ctx.q_journal.empty():
                    evt = ctx.q_journal.get_nowait()
                    if evt.event_type == "deadman":
                        timeouts.append(evt.payload.get("timeout"))
                        events.append(evt.payload.get("event"))
                if 0 in timeouts:
                    break
                time.sleep(0.1)
            ctx.stop_event.set()
            t.join(timeout=1.0)

            self.assertIn(0, timeouts)
            self.assertIn("disable", events)
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot


if __name__ == "__main__":
    unittest.main()
