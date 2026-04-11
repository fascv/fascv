import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from queue import Queue

import trading.processes.exec as execp
from trading.ipc.events import OrderIntent
from trading.processes.context import ProcessContext


class _FakeRestRejectOrder:
    def __init__(self, *args, **kwargs):
        self.deadman_calls = []

    def cancel_all_orders_after(self, timeout: int):
        self.deadman_calls.append(int(timeout))
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def add_order(self, *args, **kwargs):
        raise execp.KrakenAPIError(["EOrder:Insufficient balance"], {})

    def balance(self):
        return {"EUR": {"free": 100.0, "locked": 0.0, "total": 100.0}}


class TestExecRejectedOrderState(unittest.TestCase):
    def test_rejected_live_order_does_not_remain_open(self):
        original_rest = execp.KrakenRestClient
        execp.KrakenRestClient = _FakeRestRejectOrder
        try:
            with tempfile.TemporaryDirectory() as td:
                journal_path = os.path.join(td, "journal.jsonl")
                ctx = ProcessContext(
                    mode="live",
                    config={
                        "exec": {
                            "heartbeat_interval": 0.05,
                            "telemetry_interval": 0.05,
                            "reconcile_interval_sec": 60.0,
                            "deadman_tick_sec": 60.0,
                            "deadman_timeout_sec": 60,
                            "rate_limit_per_sec": 100.0,
                            "private_ws_enabled": False,
                        },
                        "live": {"api_key": "k", "api_secret": "c2VjcmV0", "rest_url": "https://api.kraken.com"},
                        "md": {"pair": "XBT/EUR"},
                        "cost": {},
                        "execution": {},
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

                thread = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
                thread.start()

                ctx.q_order_intent.put(
                    OrderIntent(
                        ts=datetime.now(timezone.utc),
                        side="buy",
                        qty_btc=0.01,
                        order_type="market",
                        limit_price=None,
                        post_only=False,
                        client_id="reject_me",
                    )
                )

                deadline = time.time() + 3.0
                rejected_seen = False
                last_open_orders = None
                while time.time() < deadline:
                    while not ctx.q_exec_report.empty():
                        evt = ctx.q_exec_report.get_nowait()
                        if getattr(evt, "order_id", None) == "reject_me" and getattr(evt, "status", None) == "REJECTED":
                            rejected_seen = True
                    while not ctx.q_telemetry.empty():
                        evt = ctx.q_telemetry.get_nowait()
                        if evt.process == "exec":
                            last_open_orders = evt.data.get("open_orders_count")
                    if rejected_seen and last_open_orders == 0:
                        break
                    time.sleep(0.05)

                ctx.stop_event.set()
                thread.join(timeout=1.0)

                self.assertTrue(rejected_seen)
                self.assertEqual(last_open_orders, 0)
        finally:
            execp.KrakenRestClient = original_rest
