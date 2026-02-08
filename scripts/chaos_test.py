from __future__ import annotations

import threading
import time
from queue import Queue

import trading.processes.md as md
import trading.processes.exec as execp
from trading.ipc.events import ControlCommand
from trading.processes.context import ProcessContext
from trading.kraken.book import BookChecksumError
from trading.kraken.rest import KrakenAPIError


class FakeMarketData:
    def __init__(self):
        self._count = 0

    async def stream(self):
        # emit a few dummy events, then checksum mismatch
        for _ in range(3):
            self._count += 1
            yield _dummy_event(self._count)
        raise BookChecksumError("forced checksum mismatch")


def _dummy_event(seq: int):
    from datetime import datetime, timezone
    from trading.types import MarketEvent

    return MarketEvent(
        ts=datetime.now(timezone.utc),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=0.1,
        micro={"spread_bps": 5.0, "depth": 1000.0, "imbalance": 0.1},
    )


class FakeRest:
    def __init__(self, *args, **kwargs):
        pass

    def add_order(self, *args, **kwargs):
        raise KrakenAPIError(["EAPI:Rate limit exceeded"], {})

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def get_ws_token(self):
        return "token"


def main() -> None:
    # Patch live components with fakes
    md.KrakenWSV2MarketData = lambda *args, **kwargs: FakeMarketData()
    execp.KrakenRestClient = FakeRest

    ctx = ProcessContext(
        mode="live",
        config={
            "md": {"stale_seconds": 0.1, "reconnect_min": 0.1, "reconnect_max": 0.2},
            "core": {"stale_seconds": 0.1, "trading_enabled": True, "max_orders_per_min": 5, "rate_limit_pause_sec": 0.1},
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

    t_md = threading.Thread(target=md.run_md, args=(ctx,), daemon=True)
    t_exec = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
    t_md.start()
    t_exec.start()

    # Allow some time for events and failures
    time.sleep(1.0)

    # Check for CancelAll/STOP
    actions_core = []
    actions_exec = []
    while not ctx.q_control_core.empty():
        cmd = ctx.q_control_core.get_nowait()
        if isinstance(cmd, ControlCommand):
            actions_core.append(cmd.action)
    while not ctx.q_control_exec.empty():
        cmd = ctx.q_control_exec.get_nowait()
        if isinstance(cmd, ControlCommand):
            actions_exec.append(cmd.action)

    print("core actions:", actions_core)
    print("exec actions:", actions_exec)

    ctx.stop_event.set()
    t_md.join(timeout=1.0)
    t_exec.join(timeout=1.0)


if __name__ == "__main__":
    main()
