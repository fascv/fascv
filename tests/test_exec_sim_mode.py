import os
import tempfile
import threading
import time
import unittest
from queue import Queue

import trading.processes.exec as execp
from trading.processes.context import ProcessContext


class _BoomRestClient:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("KrakenRestClient must not be constructed in sim mode")


class TestExecSimMode(unittest.TestCase):
    def test_sim_mode_uses_simulator_not_kraken(self):
        original = execp.KrakenRestClient
        execp.KrakenRestClient = _BoomRestClient
        try:
            with tempfile.TemporaryDirectory() as td:
                journal_path = os.path.join(td, "journal.jsonl")
                # Keep config minimal but complete for exec's simulator path.
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
                time.sleep(0.2)
                ctx.stop_event.set()
                t.join(timeout=2.0)
        finally:
            execp.KrakenRestClient = original


if __name__ == "__main__":
    unittest.main()

