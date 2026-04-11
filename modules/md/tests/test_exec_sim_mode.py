import threading
import time
import unittest
from queue import Queue

import trading.processes.exec as ex
from trading.processes.context import ProcessContext


class _ExplodingKrakenRestClient:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("KrakenRestClient must not be constructed in sim mode")


class TestExecSimMode(unittest.TestCase):
    def test_sim_mode_does_not_construct_kraken_rest_client(self):
        original = ex.KrakenRestClient
        ex.KrakenRestClient = _ExplodingKrakenRestClient
        try:
            ctx = ProcessContext(
                mode="sim",
                config={
                    "exec": {"deadman_in_paper": False},
                    "execution": {"latency_bars": 0, "partial_fill_ratio": 1.0, "slippage_bps": 1.0},
                    "cost": {"maker_fee_bps": 1.0, "taker_fee_bps": 3.0},
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

            t = threading.Thread(target=ex.run_exec, args=(ctx,), daemon=True)
            t.start()
            time.sleep(0.2)
            ctx.stop_event.set()
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive(), "exec thread did not stop promptly")
        finally:
            ex.KrakenRestClient = original

    def test_launch_parser_accepts_sim(self):
        import trading.launch as launch

        p = launch.build_parser()
        args = p.parse_args(["--mode", "sim"])
        self.assertEqual(args.mode, "sim")


if __name__ == "__main__":
    unittest.main()
