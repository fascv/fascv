import threading
import time
import unittest
from datetime import datetime, timezone
from queue import Empty, Queue

from trading.ipc.events import ControlCommand, JournalEvent, TelemetryEvent
from trading.processes.context import ProcessContext
from trading.processes.core import run_core


def _cfg_with_overrides(base: dict, overrides: dict) -> dict:
    out = dict(base)
    out.update(overrides)
    return out


class TestCoreBudgetReset(unittest.TestCase):
    def _make_ctx(self) -> ProcessContext:
        config = {
            "core": _cfg_with_overrides(
                {
                    "stale_seconds": 30.0,
                    "trading_enabled": True,
                    "max_orders_per_min": 20,
                    "rate_limit_pause_sec": 0.2,
                    "heartbeat_interval": 0.1,
                    "telemetry_interval": 0.1,
                },
                {},
            ),
            "features": {"return_window": 1, "atr_window": 3, "volume_z_window": 3},
            "alpha": {"lookback": 1, "threshold_bps": 0.0, "scale": 1.0},
            "cost": {
                "maker_fee_bps": 0.0,
                "taker_fee_bps": 0.0,
                "slippage_base_bps": 0.0,
                "slippage_vol_mult": 0.0,
                "spread_component_factor": 0.0,
            },
            "gate": {
                "safety_margin_bps": 0.0,
                "max_spread_bps": 1000.0,
                "max_atr_bps": 1000.0,
                "session_start_utc": 0,
                "session_end_utc": 24,
                "stale_seconds": 3600,
                "block_on_high_news_impact": False,
                "max_news_impact": 1.0,
                "max_news_age_sec": 3600,
                "news_safety_margin_bps": 0.0,
            },
            "risk": {
                "max_exposure_eur": 100.0,
                "vol_target_bps": 1000.0,
                "daily_loss_limit_eur": 1000.0,
                "max_drawdown_pct": 100.0,
                "cooldown_bars": 1,
                "allow_short": False,
            },
            "order": {
                "type": "market",
                "post_only": False,
                "limit_offset_bps": 0.0,
                "min_trade_btc": 0.0001,
                "slice_count": 1,
            },
            "general": {"starting_cash_eur": 100.0},
            "data": {"default_micro": {}},
        }
        return ProcessContext(
            mode="sim",
            config=config,
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
            q_impact_core=Queue(),
        )

    def test_budget_reset_emits_journal_and_telemetry(self):
        ctx = self._make_ctx()
        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()

        ctx.q_control_core.put(
            ControlCommand(
                ts=datetime.now(timezone.utc),
                action="SET_BUDGET",
                reason="budget_reset",
                payload={"starting_cash_eur": 1000.0, "max_exposure_eur": 200.0, "reset": True},
            )
        )

        deadline = time.time() + 3.0
        saw_budget = False
        saw_telemetry = False
        while time.time() < deadline and not (saw_budget and saw_telemetry):
            try:
                evt = ctx.q_journal.get_nowait()
                if isinstance(evt, JournalEvent) and evt.event_type == "core_budget_reset":
                    saw_budget = True
            except Empty:
                pass
            try:
                te = ctx.q_telemetry.get_nowait()
                if isinstance(te, TelemetryEvent) and te.process == "core":
                    if te.data.get("starting_cash_eur") == 1000.0 and te.data.get("max_exposure_eur") == 200.0:
                        # New budget observability fields should always be present.
                        self.assertIn("cash_eur", te.data)
                        self.assertIn("position_btc", te.data)
                        self.assertIn("equity_eur", te.data)
                        saw_telemetry = True
            except Empty:
                pass
            time.sleep(0.05)

        ctx.stop_event.set()
        t.join(timeout=2.0)

        self.assertTrue(saw_budget)
        self.assertTrue(saw_telemetry)

    def test_budget_reset_accepts_single_amount_payload(self):
        ctx = self._make_ctx()
        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()

        ctx.q_control_core.put(
            ControlCommand(
                ts=datetime.now(timezone.utc),
                action="SET_BUDGET",
                reason="budget_reset",
                payload={"amount_eur": 333.0, "reset": True},
            )
        )

        deadline = time.time() + 3.0
        saw_telemetry = False
        while time.time() < deadline and not saw_telemetry:
            try:
                te = ctx.q_telemetry.get_nowait()
                if isinstance(te, TelemetryEvent) and te.process == "core":
                    if te.data.get("starting_cash_eur") == 333.0 and te.data.get("max_exposure_eur") == 333.0:
                        saw_telemetry = True
                        break
            except Empty:
                pass
            time.sleep(0.05)

        ctx.stop_event.set()
        t.join(timeout=2.0)

        self.assertTrue(saw_telemetry)


if __name__ == "__main__":
    unittest.main()
