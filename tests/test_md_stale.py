import asyncio
import threading
import unittest
from queue import Queue

import trading.processes.md as md
from trading.ipc.events import ControlCommand
from trading.processes.context import ProcessContext


class _FakeClient:
    async def stream(self):
        raise md.StaleDataError("no messages")
        if False:
            yield None


class TestMDStale(unittest.TestCase):
    def test_stale_triggers_stop(self):
        original = md.KrakenWSV2MarketData
        md.KrakenWSV2MarketData = lambda *args, **kwargs: _FakeClient()
        try:
            ctx = ProcessContext(
                mode="live",
                config={"md": {"stale_seconds": 0.1}},
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
            async def runner():
                task = asyncio.create_task(md._run_live_async(ctx))
                # wait for STOP to appear
                for _ in range(20):
                    await asyncio.sleep(0.1)
                    if not ctx.q_control_core.empty():
                        break
                ctx.stop_event.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(runner())
            cmd = ctx.q_control_core.get_nowait()
            self.assertIsInstance(cmd, ControlCommand)
            self.assertEqual(cmd.action, "STOP")
        finally:
            md.KrakenWSV2MarketData = original


if __name__ == "__main__":
    unittest.main()
