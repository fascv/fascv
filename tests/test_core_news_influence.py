import threading
import time
import unittest
from datetime import datetime, timezone
from queue import Empty, Queue

from trading.ipc.events import NewsEvent, OrderIntent
from trading.processes.context import ProcessContext
from trading.processes.core import run_core
from trading.types import MarketEvent


class TestCoreNewsInfluence(unittest.TestCase):
    def _make_ctx(self) -> ProcessContext:
        # Keep the pipeline permissive so only the news edge controls whether we enter.
        cfg = {
            "core": {
                "stale_seconds": 30.0,
                "trading_enabled": True,
                "max_orders_per_min": 1000,
                "rate_limit_pause_sec": 0.1,
                "heartbeat_interval": 0.1,
                "telemetry_interval": 0.1,
            },
            "features": {"return_window": 1, "atr_window": 3, "volume_z_window": 3},
            # Momentum alpha produces edge=0.0 on the first bar; we rely on news bias to get >0.
            "alpha": {"lookback": 1, "threshold_bps": 9999.0, "scale": 1.0},
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
                "max_exposure_eur": 50.0,
                "vol_target_bps": 1000.0,
                "daily_loss_limit_eur": 1000.0,
                "max_drawdown_pct": 100.0,
                "cooldown_bars": 1,
                "allow_short": False,
                "use_vol_scaling": False,
                "use_gate_size_factor": False,
            },
            "order": {
                "type": "market",
                "post_only": False,
                "limit_offset_bps": 0.0,
                "min_trade_btc": 0.0000001,
                "slice_count": 1,
            },
            "general": {"starting_cash_eur": 100.0},
            "data": {"default_micro": {}},
            "news": {"edge_scale_bps": 20.0},
        }
        return ProcessContext(
            mode="sim",
            config=cfg,
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

    def test_news_bias_can_trigger_entry(self):
        ctx = self._make_ctx()
        t = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        t.start()

        ts = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
        ctx.q_impact_core.put(
            NewsEvent(
                ts=ts,
                symbol="BTC/EUR",
                sentiment_score=1.0,
                impact_score=1.0,
                source_count=5,
                event_id="evt",
            )
        )
        ctx.q_market_core.put(
            MarketEvent(
                ts=ts,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=0.0,
                micro={"spread_bps": 0.0},
            )
        )

        deadline = time.time() + 3.0
        saw_intent = False
        while time.time() < deadline and not saw_intent:
            try:
                msg = ctx.q_order_intent.get_nowait()
            except Empty:
                time.sleep(0.05)
                continue
            if isinstance(msg, OrderIntent):
                saw_intent = True
                # Should reflect the effective edge used by gate/risk.
                self.assertGreater(float(msg.meta.get("edge_bps", 0.0)), 0.0)
                break

        ctx.stop_event.set()
        t.join(timeout=2.0)
        self.assertTrue(saw_intent)


if __name__ == "__main__":
    unittest.main()

