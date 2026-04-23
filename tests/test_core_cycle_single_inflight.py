import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from queue import Queue

from trading.ipc.events import ExecutionReport
from trading.processes.context import ProcessContext
from trading.processes.core import run_core
from trading.types import Fill, MarketEvent


class TestCoreCycleSingleInflight(unittest.TestCase):
    def _event(self, ts: datetime, close: float) -> MarketEvent:
        return MarketEvent(
            ts=ts,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1.0,
            micro={"spread_bps": 0.0},
        )

    def test_cycle_mode_emits_only_one_intent_without_fills(self) -> None:
        # If fills are delayed, core must not emit multiple cycle entries while still flat.
        config = {
            "core": {
                "stale_seconds": 30.0,
                "trading_enabled": True,
                "max_orders_per_min": 999,
                "rate_limit_pause_sec": 0.2,
                "heartbeat_interval": 0.05,
                "telemetry_interval": 0.05,
            },
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
                "max_spread_bps": 9999.0,
                "max_atr_bps": 9999.0,
                "session_start_utc": 0,
                "session_end_utc": 24,
                "stale_seconds": 3600,
            },
            "risk": {
                "max_exposure_eur": 1000.0,
                "vol_target_bps": 10000.0,
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
                "cycle_trade_eur": 20.0,
            },
            "general": {"starting_cash_eur": 35.0},
            "data": {"default_micro": {}},
        }

        ctx = ProcessContext(
            mode="sim",
            config=config,
            stop_event=threading.Event(),
            q_market_core=Queue(),
            q_market_exec=Queue(),
            q_order_intent=Queue(),
            q_exec_report=Queue(),  # no fills will be injected
            q_journal=Queue(),
            q_control_core=Queue(),
            q_control_exec=Queue(),
            q_telemetry=Queue(),
            q_heartbeat=Queue(),
            q_impact_core=Queue(),
        )

        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()
        try:
            base = datetime(2026, 2, 15, tzinfo=timezone.utc)
            # First tick: edge=0 => no order. Second tick: edge>0 => entry intent.
            # Subsequent ticks should not emit further entry intents until a fill arrives.
            for i, px in enumerate([100.0, 101.0, 102.0, 103.0, 104.0]):
                ctx.q_market_core.put(self._event(base + timedelta(seconds=i), px))
                time.sleep(0.05)

            deadline = time.time() + 1.0
            while time.time() < deadline:
                if ctx.q_order_intent.qsize() >= 1:
                    break
                time.sleep(0.02)

            self.assertEqual(ctx.q_order_intent.qsize(), 1)
        finally:
            ctx.stop_event.set()
            t.join(timeout=2.0)

    def test_exit_emits_only_one_flatten_intent_without_fill(self) -> None:
        config = {
            "core": {
                "stale_seconds": 30.0,
                "trading_enabled": True,
                "max_orders_per_min": 999,
                "rate_limit_pause_sec": 0.2,
                "heartbeat_interval": 0.05,
                "telemetry_interval": 0.05,
            },
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
                "max_spread_bps": 9999.0,
                "max_atr_bps": 9999.0,
                "session_start_utc": 0,
                "session_end_utc": 24,
                "stale_seconds": 3600,
            },
            "risk": {
                "max_exposure_eur": 1000.0,
                "vol_target_bps": 10000.0,
                "daily_loss_limit_eur": 1000.0,
                "max_drawdown_pct": 100.0,
                "cooldown_bars": 1,
                "allow_short": False,
                "min_exit_profit_bps": 0.0,
                "trailing_stop_enabled": True,
                "trailing_activation_bps": 0.0,
                "trailing_stop_bps": 10.0,
                "trailing_stop_atr_mult": 0.0,
            },
            "order": {
                "type": "market",
                "post_only": False,
                "limit_offset_bps": 0.0,
                "min_trade_btc": 0.0001,
                "slice_count": 1,
            },
            "general": {"starting_cash_eur": 35.0},
            "data": {"default_micro": {}},
        }

        ctx = ProcessContext(
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

        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()
        try:
            base = datetime(2026, 2, 15, tzinfo=timezone.utc)
            ctx.q_exec_report.put(
                Fill(
                    ts=base,
                    side="buy",
                    qty_btc=1.0,
                    price=100.0,
                    fee_eur=0.0,
                    order_id="BUY1",
                )
            )
            time.sleep(0.05)
            for i, px in enumerate([101.0, 100.0, 99.8], start=1):
                ctx.q_market_core.put(self._event(base + timedelta(seconds=i), px))
                time.sleep(0.05)

            deadline = time.time() + 1.0
            while time.time() < deadline:
                if ctx.q_order_intent.qsize() >= 1:
                    break
                time.sleep(0.02)

            self.assertEqual(ctx.q_order_intent.qsize(), 1)
            intent = ctx.q_order_intent.get_nowait()
            self.assertEqual(intent.side, "sell")
        finally:
            ctx.stop_event.set()
            t.join(timeout=2.0)

    def test_entry_cancel_blocks_reentry_until_settlement_grace_expires(self) -> None:
        config = {
            "core": {
                "stale_seconds": 30.0,
                "trading_enabled": True,
                "max_orders_per_min": 999,
                "rate_limit_pause_sec": 0.2,
                "heartbeat_interval": 0.05,
                "telemetry_interval": 0.05,
            },
            "exec": {"entry_settlement_grace_sec": 5.0},
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
                "max_spread_bps": 9999.0,
                "max_atr_bps": 9999.0,
                "session_start_utc": 0,
                "session_end_utc": 24,
                "stale_seconds": 3600,
            },
            "risk": {
                "max_exposure_eur": 1000.0,
                "vol_target_bps": 10000.0,
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
                "cycle_trade_eur": 20.0,
            },
            "general": {"starting_cash_eur": 35.0},
            "data": {"default_micro": {}},
        }

        ctx = ProcessContext(
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

        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()
        try:
            base = datetime(2026, 2, 15, tzinfo=timezone.utc)
            for i, px in enumerate([100.0, 101.0]):
                ctx.q_market_core.put(self._event(base + timedelta(seconds=i), px))
                time.sleep(0.05)

            deadline = time.time() + 1.0
            while time.time() < deadline and ctx.q_order_intent.qsize() < 1:
                time.sleep(0.02)

            self.assertEqual(ctx.q_order_intent.qsize(), 1)
            first_intent = ctx.q_order_intent.get_nowait()
            ctx.q_exec_report.put(
                ExecutionReport(
                    ts=base + timedelta(seconds=2),
                    order_id=first_intent.client_id or "",
                    status="CANCELED",
                    filled_qty_btc=0.0,
                    avg_price=0.0,
                    fee_eur=0.0,
                    latency_ms=0.0,
                    reason="cancel_all",
                )
            )
            time.sleep(0.08)

            ctx.q_market_core.put(self._event(base + timedelta(seconds=3), 102.0))
            time.sleep(0.08)
            ctx.q_market_core.put(self._event(base + timedelta(seconds=4), 103.0))
            time.sleep(0.08)
            self.assertEqual(ctx.q_order_intent.qsize(), 0)

            time.sleep(5.2)
            ctx.q_market_core.put(self._event(base + timedelta(seconds=5), 104.0))
            deadline = time.time() + 1.0
            while time.time() < deadline and ctx.q_order_intent.qsize() < 1:
                time.sleep(0.02)

            self.assertEqual(ctx.q_order_intent.qsize(), 1)
        finally:
            ctx.stop_event.set()
            t.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
