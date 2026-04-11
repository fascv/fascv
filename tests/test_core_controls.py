import copy
import json
import os
import threading
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from typing import List
from unittest import mock

import yaml

from trading.ipc.events import ControlCommand, JournalEvent
from trading.processes.context import ProcessContext
from trading.processes.core import (
    _inject_alpha_features,
    _queue_propagated_disable_commands,
    _recover_flat_reentry_state_from_journal,
    _restore_risk_flat_reentry_state_from_journal,
    _restore_risk_position_state_from_sync,
    run_core,
)
from trading.risk.sizing import RiskConfig, RiskManager
from trading.types import Features, MarketEvent


def _cfg_with_overrides(base: dict, overrides: dict) -> dict:
    out = dict(base)
    out.update(overrides)
    return out


class TestCoreControls(unittest.TestCase):
    def _make_ctx(self, core_overrides=None, gate_overrides=None, risk_overrides=None) -> ProcessContext:
        core_cfg = _cfg_with_overrides(
            {
                "stale_seconds": 30.0,
                "trading_enabled": True,
                "max_orders_per_min": 20,
                "rate_limit_pause_sec": 0.2,
                "heartbeat_interval": 0.1,
                "telemetry_interval": 0.1,
            },
            core_overrides or {},
        )
        gate_cfg = _cfg_with_overrides(
            {
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
            gate_overrides or {},
        )
        risk_cfg = _cfg_with_overrides(
            {
                "max_exposure_eur": 100.0,
                "vol_target_bps": 1000.0,
                "daily_loss_limit_eur": 1000.0,
                "max_drawdown_pct": 100.0,
                "cooldown_bars": 1,
                "allow_short": False,
            },
            risk_overrides or {},
        )
        config = {
            "core": core_cfg,
            "features": {"return_window": 1, "atr_window": 3, "volume_z_window": 3},
            "alpha": {"lookback": 1, "threshold_bps": 0.0, "scale": 1.0},
            "cost": {
                "maker_fee_bps": 0.0,
                "taker_fee_bps": 0.0,
                "slippage_base_bps": 0.0,
                "slippage_vol_mult": 0.0,
                "spread_component_factor": 0.0,
            },
            "gate": gate_cfg,
            "risk": risk_cfg,
            "order": {
                "type": "market",
                "post_only": False,
                "limit_offset_bps": 0.0,
                "min_trade_btc": 0.0001,
                "slice_count": 1,
            },
            "general": {"starting_cash_eur": 1000.0},
            "data": {"default_micro": {}},
        }
        return ProcessContext(
            mode="live",
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

    def _start_core(self, ctx: ProcessContext) -> threading.Thread:
        thread = threading.Thread(target=run_core, args=(ctx,), daemon=True)
        thread.start()
        return thread

    def test_inject_alpha_features_marks_early_rebound_states(self):
        features = Features(ts=datetime(2026, 3, 14, tzinfo=timezone.utc), values={})
        _inject_alpha_features(
            features,
            {
                "swing_state": "micro_valley_rebound",
                "continuation_state": "early_liftoff_override",
                "recent_bias_bps": 7.0,
                "structure": {
                    "up_structure": True,
                    "down_structure": False,
                    "active_leg": "rise",
                    "range_pos": 0.23,
                    "slope_short_bps": 2.8,
                    "drawdown_from_peak_bps": 18.0,
                },
            },
        )

        self.assertEqual(features.values["alpha_swing_micro_valley_rebound"], 1.0)
        self.assertEqual(features.values["alpha_swing_valley_rebound"], 0.0)
        self.assertEqual(features.values["alpha_continuation_early_liftoff"], 1.0)
        self.assertEqual(features.values["alpha_active_leg_rise"], 1.0)
        self.assertEqual(features.values["alpha_up_structure"], 1.0)

    def _stop_core(self, ctx: ProcessContext, thread: threading.Thread) -> None:
        ctx.stop_event.set()
        thread.join(timeout=1.5)

    def _wait_for_intent_count(self, ctx: ProcessContext, expected: int, timeout: float = 1.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ctx.q_order_intent.qsize() >= expected:
                return True
            time.sleep(0.02)
        return False

    def _push_market(self, ctx: ProcessContext, event: MarketEvent, settle_sec: float = 0.08) -> None:
        ctx.q_market_core.put(event)
        time.sleep(settle_sec)

    def _journal_events(self, ctx: ProcessContext) -> List[JournalEvent]:
        events = []
        while True:
            try:
                evt = ctx.q_journal.get_nowait()
            except Empty:
                break
            if isinstance(evt, JournalEvent):
                events.append(evt)
        return events

    def _drain_queue(self, q: Queue) -> list:
        items = []
        while True:
            try:
                items.append(q.get_nowait())
            except Empty:
                break
        return items

    def test_max_orders_per_min_disables_trading(self):
        ctx = self._make_ctx(core_overrides={"max_orders_per_min": 1})
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._push_market(ctx, self._event(base_ts, 100.0))
        self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 101.0))
        self.assertTrue(self._wait_for_intent_count(ctx, 1))
        self._push_market(ctx, self._event(base_ts + timedelta(seconds=2), 102.0))
        time.sleep(0.2)
        self._stop_core(ctx, thread)

        self.assertEqual(ctx.q_order_intent.qsize(), 1)
        reasons = [
            evt.payload.get("reason")
            for evt in self._journal_events(ctx)
            if evt.event_type == "core_trading_disabled"
        ]
        self.assertIn("max_orders_per_min", reasons)

    def test_rate_limit_pause_auto_resume(self):
        ctx = self._make_ctx(
            core_overrides={
                "rate_limit_pause_sec": 0.2,
                "max_orders_per_min": 20,
                "auto_resume_rate_limit": True,
            }
        )
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._push_market(ctx, self._event(base_ts, 100.0))
        self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 101.0))
        self.assertTrue(self._wait_for_intent_count(ctx, 1))
        baseline = ctx.q_order_intent.qsize()

        ctx.q_control_core.put(
            ControlCommand(ts=datetime.now(timezone.utc), action="PAUSE", reason="rate_limit")
        )
        time.sleep(0.6)
        self._push_market(ctx, self._event(base_ts + timedelta(seconds=2), 102.0), settle_sec=0.04)
        time.sleep(0.1)
        self.assertEqual(ctx.q_order_intent.qsize(), baseline)

        time.sleep(0.25)
        self._push_market(ctx, self._event(base_ts + timedelta(seconds=3), 103.0), settle_sec=0.04)
        self.assertTrue(self._wait_for_intent_count(ctx, baseline + 1))
        self._stop_core(ctx, thread)

        enabled_reasons = [
            evt.payload.get("reason")
            for evt in self._journal_events(ctx)
            if evt.event_type == "core_trading_enabled"
        ]
        self.assertIn("rate_limit_resume", enabled_reasons)

    def test_news_defaults_without_news_events(self):
        ctx = self._make_ctx(
            gate_overrides={
                "block_on_high_news_impact": True,
                "max_news_impact": 0.1,
                "max_news_age_sec": 1,
            }
        )
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._push_market(ctx, self._event(base_ts, 100.0))
        self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 101.0))
        self.assertTrue(self._wait_for_intent_count(ctx, 1))
        self._stop_core(ctx, thread)

        core_decisions = [
            evt.payload for evt in self._journal_events(ctx) if evt.event_type == "core_decision"
        ]
        self.assertTrue(core_decisions)
        last = core_decisions[-1]
        self.assertEqual(last["features"]["news_sentiment"], 0.0)
        self.assertEqual(last["features"]["news_impact"], 0.0)
        self.assertEqual(last["features"]["news_source_count"], 0.0)
        self.assertEqual(last["features"]["news_age_sec"], 0.0)
        self.assertFalse(last["news"]["present"])

    def test_sync_account_uses_existing_position(self):
        ctx = self._make_ctx(core_overrides={"telemetry_interval": 0.05})
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        try:
            ctx.q_control_core.put(
                ControlCommand(
                    ts=datetime.now(timezone.utc),
                    action="SYNC_ACCOUNT",
                    reason="test",
                    payload={
                        "pair": "ETH/EUR",
                        "base_asset": "ETH",
                        "quote_asset": "EUR",
                        "cash_eur": 7.5,
                        "position_btc": 0.25,
                        "reset": True,
                    },
                )
            )
            self._push_market(ctx, self._event(base_ts, 200.0), settle_sec=0.12)

            deadline = time.time() + 1.5
            telemetry_ok = False
            while time.time() < deadline and not telemetry_ok:
                while not ctx.q_telemetry.empty():
                    te = ctx.q_telemetry.get_nowait()
                    if te.process != "core":
                        continue
                    if (
                        abs(float(te.data.get("cash_eur", 0.0)) - 7.5) <= 1e-9
                        and abs(float(te.data.get("position_btc", 0.0)) - 0.25) <= 1e-9
                        and float(te.data.get("avg_entry_price", 0.0)) > 0.0
                    ):
                        telemetry_ok = True
                        break
                time.sleep(0.02)
        finally:
            self._stop_core(ctx, thread)

        self.assertTrue(telemetry_ok)
        synced = False
        for evt in self._journal_events(ctx):
            if evt.event_type == "core_account_synced":
                synced = True
        self.assertTrue(synced)

    def test_queue_propagated_disable_commands_cancels_before_stop(self):
        ctx = self._make_ctx()
        ts = datetime.now(timezone.utc)
        _queue_propagated_disable_commands(ctx, ts, "daily_loss_limit")

        core_cmds = self._drain_queue(ctx.q_control_core)
        exec_cmds = self._drain_queue(ctx.q_control_exec)

        self.assertEqual([cmd.action for cmd in core_cmds], ["CANCEL_ALL", "STOP"])
        self.assertEqual([cmd.action for cmd in exec_cmds], ["CANCEL_ALL", "STOP"])
        self.assertTrue(all(cmd.reason == "daily_loss_limit" for cmd in core_cmds + exec_cmds))
        self.assertTrue(bool(core_cmds[-1].payload.get("skip_emergency_exit")))
        self.assertTrue(bool(exec_cmds[-1].payload.get("skip_emergency_exit")))

    def test_stop_with_skip_emergency_exit_does_not_enqueue_duplicate_exit(self):
        ctx = self._make_ctx()
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        try:
            ctx.q_control_core.put(
                ControlCommand(
                    ts=base_ts,
                    action="SYNC_ACCOUNT",
                    reason="test_position",
                    payload={
                        "pair": "ETH/EUR",
                        "base_asset": "ETH",
                        "quote_asset": "EUR",
                        "cash_eur": 10.0,
                        "position_btc": 0.25,
                        "avg_entry_price": 100.0,
                        "reset": True,
                    },
                )
            )
            self._push_market(ctx, self._event(base_ts, 100.0), settle_sec=0.12)

            ctx.q_control_core.put(
                ControlCommand(
                    ts=base_ts + timedelta(seconds=1),
                    action="STOP",
                    reason="daily_loss_limit",
                    payload={"skip_emergency_exit": True},
                )
            )
            self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 99.0), settle_sec=0.12)
        finally:
            self._stop_core(ctx, thread)

        self.assertEqual(ctx.q_order_intent.qsize(), 0)

    def test_sync_account_reset_reanchors_daily_loss_reference_after_pre_sync_tick(self):
        ctx = self._make_ctx(
            core_overrides={"warmup": {"enabled": False}},
            risk_overrides={"daily_loss_limit_eur": 1.0, "cooldown_bars": 1},
        )
        ctx.config["general"]["starting_cash_eur"] = 150.0
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 3, 16, 18, 11, 40, tzinfo=timezone.utc)
        try:
            self._push_market(ctx, self._event(base_ts, 0.7902), settle_sec=0.12)
            ctx.q_control_core.put(
                ControlCommand(
                    ts=base_ts + timedelta(seconds=1),
                    action="SYNC_ACCOUNT",
                    reason="restart_recovery",
                    payload={
                        "pair": "VIRTUAL/USDC",
                        "base_asset": "VIRTUAL",
                        "quote_asset": "USDC",
                        "cash_eur": 69.11089889,
                        "position_btc": 29.03559,
                        "avg_entry_price": 0.792251925,
                        "entry_ts": (base_ts - timedelta(seconds=70)).isoformat(),
                        "reset": True,
                        "source": "startup",
                    },
                )
            )
            time.sleep(0.12)
            self._push_market(ctx, self._event(base_ts + timedelta(seconds=5), 0.7901), settle_sec=0.12)
        finally:
            self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        disabled_reasons = [
            evt.payload.get("reason")
            for evt in journal_events
            if evt.event_type == "core_trading_disabled"
        ]
        self.assertNotIn("daily_loss_limit", disabled_reasons)
        self.assertEqual(ctx.q_order_intent.qsize(), 0)

    def test_sync_account_restores_position_age_from_entry_ts(self):
        ctx = self._make_ctx(core_overrides={"telemetry_interval": 0.05})
        ctx.config["risk"]["min_hold_bars"] = 4
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        try:
            ctx.q_control_core.put(
                ControlCommand(
                    ts=base_ts,
                    action="SYNC_ACCOUNT",
                    reason="test",
                    payload={
                        "pair": "ETH/EUR",
                        "base_asset": "ETH",
                        "quote_asset": "EUR",
                        "cash_eur": 7.5,
                        "position_btc": 0.25,
                        "avg_entry_price": 200.0,
                        "entry_ts": (base_ts - timedelta(minutes=20)).isoformat(),
                        "reset": True,
                    },
                )
            )
            self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 200.0), settle_sec=0.12)
        finally:
            self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        synced = [evt.payload for evt in journal_events if evt.event_type == "core_account_synced"]
        self.assertTrue(synced)
        self.assertGreaterEqual(int(synced[-1].get("restored_bars_in_position") or 0), 20)

        decisions = [evt.payload for evt in journal_events if evt.event_type == "core_decision"]
        self.assertTrue(decisions)
        self.assertNotEqual((decisions[-1].get("risk") or {}).get("reason"), "hold_min_bars")

    def test_sync_restore_resets_age_after_material_scale_in(self):
        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            position_epsilon_btc=0.0001,
        )
        rm = RiskManager(cfg)
        rm._last_position_sign = 1
        rm._bars_in_position = 587
        rm._last_effective_position_btc = 0.7814
        rm._trail_peak_price = 0.0135
        rm._position_trough_price = 0.0132
        rm._trailing_armed = True
        rm._recent_prices.extend([0.0134, 0.01335, 0.0133])

        sync_ts = datetime(2026, 3, 17, 2, 52, 6, tzinfo=timezone.utc)
        restored = _restore_risk_position_state_from_sync(
            rm,
            position_btc=3441.5096,
            reference_price=0.013333002358468677,
            entry_ts_raw="2026-03-17T02:03:13.502000+00:00",
            sync_ts=sync_ts,
            bar_seconds=5.0,
        )

        self.assertEqual(restored, 1)
        self.assertEqual(rm._bars_in_position, 1)
        self.assertAlmostEqual(rm._last_effective_position_btc, 3441.5096, places=6)
        self.assertAlmostEqual(rm._trail_peak_price, 0.013333002358468677, places=12)
        self.assertAlmostEqual(rm._position_trough_price, 0.013333002358468677, places=12)
        self.assertFalse(rm._trailing_armed)
        self.assertEqual(len(rm._recent_prices), 0)

    def test_reload_applies_alpha_from_runtime_config(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": False}})
        runtime_cfg = copy.deepcopy(ctx.config)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "runtime.yaml")
            runtime_cfg["runtime"] = {"config_path": cfg_path}
            runtime_cfg["alpha"] = {
                "type": "momentum",
                "lookback": 1,
                "threshold_bps": 0.0,
                "scale": 1.0,
            }
            with open(cfg_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(runtime_cfg, fh, sort_keys=False)

            ctx.config = copy.deepcopy(runtime_cfg)
            thread = self._start_core(ctx)
            base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            try:
                self._push_market(ctx, self._event(base_ts, 100.0))

                reloaded_cfg = copy.deepcopy(runtime_cfg)
                reloaded_cfg["alpha"] = {
                    "type": "auto",
                    "auto": {
                        "trend": {"lookback": 1, "threshold_bps": 0.0, "scale": 1.0},
                        "mean_reversion": {"lookback": 2, "threshold_bps": 0.0, "scale": 1.0},
                        "breakout": {"lookback": 2, "trigger_bps": 1000.0, "scale": 1.0},
                        "regime": {
                            "lookback": 2,
                            "trend_momentum_bps": 999.0,
                            "range_momentum_bps": -1.0,
                            "high_vol_atr_bps": 9999.0,
                            "low_vol_atr_bps": 9999.0,
                            "breakout_return_bps": 1000.0,
                            "default_regime": "trend",
                            "trend_strategy": "trend",
                            "range_strategy": "mean_reversion",
                            "breakout_strategy": "breakout",
                        },
                    },
                }
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    yaml.safe_dump(reloaded_cfg, fh, sort_keys=False)

                ctx.q_control_core.put(
                    ControlCommand(
                        ts=datetime.now(timezone.utc),
                        action="RELOAD",
                        reason="test_reload",
                    )
                )
                time.sleep(0.15)
                self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 100.5), settle_sec=0.12)
                self._push_market(ctx, self._event(base_ts + timedelta(seconds=2), 101.0), settle_sec=0.12)
            finally:
                self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        reloads = [evt.payload for evt in journal_events if evt.event_type == "core_reload_applied"]
        self.assertTrue(reloads)
        self.assertEqual(reloads[-1]["alpha_type"], "auto")

        decisions = [evt.payload for evt in journal_events if evt.event_type == "core_decision"]
        self.assertTrue(decisions)
        self.assertEqual(decisions[-1]["alpha_type"], "auto")
        self.assertEqual(decisions[-1]["alpha_active_strategy"], "trend")
        self.assertEqual((decisions[-1].get("alpha") or {}).get("type"), "auto")
        self.assertEqual((decisions[-1].get("alpha") or {}).get("active_strategy"), "trend")
        alpha_meta = ((decisions[-1].get("alpha") or {}).get("meta") or {})
        self.assertEqual(alpha_meta.get("active_strategy"), "trend")
        self.assertEqual(alpha_meta.get("regime"), "trend")

    def test_reload_applies_warmup_by_default(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": True}})
        runtime_cfg = copy.deepcopy(ctx.config)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "runtime.yaml")
            runtime_cfg["runtime"] = {"config_path": cfg_path}
            runtime_cfg["alpha"] = {
                "type": "momentum",
                "lookback": 1,
                "threshold_bps": 0.0,
                "scale": 1.0,
            }
            with open(cfg_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(runtime_cfg, fh, sort_keys=False)

            ctx.config = copy.deepcopy(runtime_cfg)
            warmup_calls = []

            def _fake_warmup(*_args, **_kwargs):
                warmup_calls.append(time.time())
                return {"bars_loaded": 2, "_close_prices": [100.0, 101.0]}

            with mock.patch("trading.processes.core.warmup_feature_and_alpha", side_effect=_fake_warmup):
                thread = self._start_core(ctx)
                base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
                try:
                    self._push_market(ctx, self._event(base_ts, 100.0))

                    reloaded_cfg = copy.deepcopy(runtime_cfg)
                    reloaded_cfg["alpha"]["lookback"] = 2
                    with open(cfg_path, "w", encoding="utf-8") as fh:
                        yaml.safe_dump(reloaded_cfg, fh, sort_keys=False)

                    ctx.q_control_core.put(
                        ControlCommand(
                            ts=datetime.now(timezone.utc),
                            action="RELOAD",
                            reason="test_reload_warmup_default",
                        )
                    )
                    time.sleep(0.2)
                    self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 100.5), settle_sec=0.12)
                finally:
                    self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        reloads = [evt.payload for evt in journal_events if evt.event_type == "core_reload_applied"]
        self.assertTrue(reloads)
        self.assertTrue(bool(reloads[-1]["warmup_applied"]))
        warmups = [evt.payload for evt in journal_events if evt.event_type == "core_warmup"]
        self.assertEqual(len(warmup_calls), 2)
        self.assertEqual(len(warmups), 2)
        self.assertEqual(warmups[-1].get("reason"), "reload")

    def test_reload_skips_warmup_when_explicitly_disabled(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": True, "reload_enabled": False}})
        runtime_cfg = copy.deepcopy(ctx.config)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "runtime.yaml")
            runtime_cfg["runtime"] = {"config_path": cfg_path}
            runtime_cfg["alpha"] = {
                "type": "momentum",
                "lookback": 1,
                "threshold_bps": 0.0,
                "scale": 1.0,
            }
            with open(cfg_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(runtime_cfg, fh, sort_keys=False)

            ctx.config = copy.deepcopy(runtime_cfg)
            warmup_calls = []

            def _fake_warmup(*_args, **_kwargs):
                warmup_calls.append(time.time())
                return {"bars_loaded": 2, "_close_prices": [100.0, 101.0]}

            with mock.patch("trading.processes.core.warmup_feature_and_alpha", side_effect=_fake_warmup):
                thread = self._start_core(ctx)
                base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
                try:
                    self._push_market(ctx, self._event(base_ts, 100.0))

                    reloaded_cfg = copy.deepcopy(runtime_cfg)
                    reloaded_cfg["alpha"]["lookback"] = 2
                    with open(cfg_path, "w", encoding="utf-8") as fh:
                        yaml.safe_dump(reloaded_cfg, fh, sort_keys=False)

                    ctx.q_control_core.put(
                        ControlCommand(
                            ts=datetime.now(timezone.utc),
                            action="RELOAD",
                            reason="test_reload_skip_warmup",
                        )
                    )
                    time.sleep(0.2)
                    self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 100.5), settle_sec=0.12)
                finally:
                    self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        reloads = [evt.payload for evt in journal_events if evt.event_type == "core_reload_applied"]
        self.assertTrue(reloads)
        self.assertFalse(bool(reloads[-1]["warmup_applied"]))
        warmups = [evt.payload for evt in journal_events if evt.event_type == "core_warmup"]
        self.assertEqual(len(warmup_calls), 1)
        self.assertEqual(len(warmups), 1)

    def test_reload_slow_warmup_emits_progress_heartbeats(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": True}})
        runtime_cfg = copy.deepcopy(ctx.config)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "runtime.yaml")
            runtime_cfg["runtime"] = {"config_path": cfg_path}
            runtime_cfg["alpha"] = {
                "type": "momentum",
                "lookback": 1,
                "threshold_bps": 0.0,
                "scale": 1.0,
            }
            with open(cfg_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(runtime_cfg, fh, sort_keys=False)

            ctx.config = copy.deepcopy(runtime_cfg)
            warmup_calls = []
            heartbeat_calls = []

            def _fake_warmup(*_args, **_kwargs):
                warmup_calls.append(time.time())
                if len(warmup_calls) >= 2:
                    time.sleep(0.35)
                return {"bars_loaded": 2, "_close_prices": [100.0, 101.0]}

            def _fake_send_heartbeat(*_args, **_kwargs):
                heartbeat_calls.append(time.time())

            with mock.patch("trading.processes.core.warmup_feature_and_alpha", side_effect=_fake_warmup), mock.patch(
                "trading.processes.core._send_heartbeat", side_effect=_fake_send_heartbeat
            ):
                thread = self._start_core(ctx)
                base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
                try:
                    self._push_market(ctx, self._event(base_ts, 100.0))
                    heartbeat_calls.clear()

                    reloaded_cfg = copy.deepcopy(runtime_cfg)
                    reloaded_cfg["alpha"]["lookback"] = 2
                    with open(cfg_path, "w", encoding="utf-8") as fh:
                        yaml.safe_dump(reloaded_cfg, fh, sort_keys=False)

                    ctx.q_control_core.put(
                        ControlCommand(
                            ts=datetime.now(timezone.utc),
                            action="RELOAD",
                            reason="test_reload_heartbeat_progress",
                        )
                    )
                    self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 100.5), settle_sec=0.05)
                    time.sleep(0.25)
                    self.assertGreaterEqual(len(heartbeat_calls), 2)
                    self._push_market(ctx, self._event(base_ts + timedelta(seconds=2), 101.0), settle_sec=0.2)
                finally:
                    self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        reloads = [evt.payload for evt in journal_events if evt.event_type == "core_reload_applied"]
        self.assertTrue(reloads)
        self.assertTrue(bool(reloads[-1]["warmup_applied"]))

    def test_reload_commands_are_batched(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": False}})
        runtime_cfg = copy.deepcopy(ctx.config)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "runtime.yaml")
            runtime_cfg["runtime"] = {"config_path": cfg_path}
            with open(cfg_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(runtime_cfg, fh, sort_keys=False)

            ctx.config = copy.deepcopy(runtime_cfg)
            thread = self._start_core(ctx)
            base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            try:
                self._push_market(ctx, self._event(base_ts, 100.0))
                for idx in range(3):
                    ctx.q_control_core.put(
                        ControlCommand(
                            ts=datetime.now(timezone.utc),
                            action="RELOAD",
                            reason=f"test_reload_batch_{idx}",
                        )
                    )
                time.sleep(0.25)
                self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 100.5), settle_sec=0.12)
            finally:
                self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        reload_controls = [
            evt.payload
            for evt in journal_events
            if evt.event_type == "core_control" and evt.payload.get("action") == "RELOAD"
        ]
        reloads = [evt.payload for evt in journal_events if evt.event_type == "core_reload_applied"]
        self.assertEqual(len(reloads), 1)
        self.assertEqual(len(reload_controls), 1)
        self.assertEqual(int(reload_controls[0].get("batched_count") or 0), 3)
        self.assertEqual(reload_controls[0].get("reason"), "test_reload_batch_2")

    def test_recover_flat_reentry_state_from_journal_restores_recent_loss_cluster(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.00,
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:12:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 0.99},
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:13:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 0.99,
                    },
                },
                {
                    "ts": "2026-03-14T02:21:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:21:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.10,
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:22:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 1.05},
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:23:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 1.0529,
                    },
                },
                {
                    "ts": "2026-03-14T02:24:18+00:00",
                    "event_type": "core_account_synced",
                    "payload": {
                        "ts": "2026-03-14T02:24:18+00:00",
                        "position_btc": 0.0,
                        "reference_price": 1.054,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            cfg = copy.deepcopy(self._make_ctx().config)
            cfg["journal"] = {"json_path": path}
            cfg["risk"].update(
                {
                    "reentry_cooldown_bars_after_whipsaw_stop_loss": 8,
                    "reentry_whipsaw_hard_stop_max_bars": 4,
                    "reentry_loss_cluster_window_bars": 30,
                    "reentry_cooldown_bars_after_loss_cluster": 20,
                    "reentry_cooldown_bars_after_trailing_stop": 5,
                }
            )
            recovered = _recover_flat_reentry_state_from_journal(
                cfg,
                sync_ts=datetime(2026, 3, 14, 2, 27, 13, tzinfo=timezone.utc),
                bar_seconds=60.0,
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertAlmostEqual(float(recovered["last_long_exit_price"]), 1.0529, places=6)
            self.assertEqual(int(recovered["short_loss_cluster_count"]), 2)
            self.assertEqual(int(recovered["short_loss_cluster_age_bars"]), 4)
            self.assertEqual(int(recovered["reentry_cooldown_remaining"]), 16)
            self.assertEqual(recovered["exit_reason"], "hard_stop_loss")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_recover_flat_reentry_state_from_journal_ignores_future_reopen(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_future_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.00,
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:12:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 0.99},
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:13:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 0.99,
                    },
                },
                {
                    "ts": "2026-03-14T02:21:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:21:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.10,
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:22:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 1.05},
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:23:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 1.0529,
                    },
                },
                {
                    "ts": "2026-03-14T02:24:18+00:00",
                    "event_type": "core_account_synced",
                    "payload": {
                        "ts": "2026-03-14T02:24:18+00:00",
                        "position_btc": 0.0,
                        "reference_price": 1.054,
                    },
                },
                {
                    "ts": "2026-03-14T02:34:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:34:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.08,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            cfg = copy.deepcopy(self._make_ctx().config)
            cfg["journal"] = {"json_path": path}
            cfg["risk"].update(
                {
                    "reentry_cooldown_bars_after_whipsaw_stop_loss": 8,
                    "reentry_whipsaw_hard_stop_max_bars": 4,
                    "reentry_loss_cluster_window_bars": 30,
                    "reentry_cooldown_bars_after_loss_cluster": 20,
                }
            )
            recovered = _recover_flat_reentry_state_from_journal(
                cfg,
                sync_ts=datetime(2026, 3, 14, 2, 27, 13, tzinfo=timezone.utc),
                bar_seconds=60.0,
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(int(recovered["short_loss_cluster_count"]), 2)
            self.assertEqual(int(recovered["reentry_cooldown_remaining"]), 16)
            self.assertEqual(recovered["exit_reason"], "hard_stop_loss")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_recover_flat_reentry_state_prefers_latest_flat_snapshot(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_snapshot_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.00,
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:12:00+00:00",
                        "risk": {
                            "allow": True,
                            "target_btc": 0.0,
                            "reason": "hard_stop_loss",
                            "reentry_cooldown_remaining": 8,
                            "short_loss_cluster_count": 1,
                            "short_loss_cluster_age_bars": 0,
                            "last_long_exit_price": 0.99,
                        },
                        "features": {"price": 0.99},
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:13:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 0.99,
                    },
                },
                {
                    "ts": "2026-03-14T02:21:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:21:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.10,
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:22:00+00:00",
                        "risk": {
                            "allow": True,
                            "target_btc": 0.0,
                            "reason": "hard_stop_loss",
                            "reentry_cooldown_remaining": 20,
                            "short_loss_cluster_count": 2,
                            "short_loss_cluster_age_bars": 0,
                            "last_long_exit_price": 1.0529,
                        },
                        "features": {"price": 1.05},
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:23:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 1.0529,
                    },
                },
                {
                    "ts": "2026-03-14T02:24:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:24:00+00:00",
                        "risk": {
                            "allow": False,
                            "target_btc": 0.0,
                            "reason": "gate_block",
                            "reentry_cooldown_remaining": 20,
                            "short_loss_cluster_count": 2,
                            "short_loss_cluster_age_bars": 1,
                            "last_long_exit_price": 1.0529,
                        },
                        "features": {"price": 1.0540},
                    },
                },
                {
                    "ts": "2026-03-14T02:27:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:27:00+00:00",
                        "risk": {
                            "allow": False,
                            "target_btc": 0.0,
                            "reason": "gate_block",
                            "reentry_cooldown_remaining": 20,
                            "short_loss_cluster_count": 2,
                            "short_loss_cluster_age_bars": 4,
                            "last_long_exit_price": 1.0529,
                        },
                        "features": {"price": 1.0515},
                    },
                },
                {
                    "ts": "2026-03-14T02:34:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:34:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.08,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            cfg = copy.deepcopy(self._make_ctx().config)
            cfg["journal"] = {"json_path": path}
            cfg["risk"].update(
                {
                    "reentry_cooldown_bars_after_whipsaw_stop_loss": 8,
                    "reentry_whipsaw_hard_stop_max_bars": 4,
                    "reentry_loss_cluster_window_bars": 30,
                    "reentry_cooldown_bars_after_loss_cluster": 20,
                }
            )
            recovered = _recover_flat_reentry_state_from_journal(
                cfg,
                sync_ts=datetime(2026, 3, 14, 2, 27, 13, tzinfo=timezone.utc),
                bar_seconds=60.0,
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(int(recovered["reentry_cooldown_remaining"]), 20)
            self.assertEqual(int(recovered["short_loss_cluster_count"]), 2)
            self.assertEqual(int(recovered["short_loss_cluster_age_bars"]), 4)
            self.assertAlmostEqual(float(recovered["last_long_exit_price"]), 1.0529, places=6)
            self.assertEqual(recovered["exit_reason"], "hard_stop_loss")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_recover_flat_reentry_state_from_journal_restores_weak_exit_cooldown(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_weak_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.0000,
                    },
                },
                {
                    "ts": "2026-03-14T02:14:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:14:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "time_break_even_floor"},
                        "features": {"price": 1.0010},
                    },
                },
                {
                    "ts": "2026-03-14T02:14:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:14:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 1.0010,
                    },
                },
                {
                    "ts": "2026-03-14T02:15:00+00:00",
                    "event_type": "core_account_synced",
                    "payload": {
                        "ts": "2026-03-14T02:15:00+00:00",
                        "position_btc": 0.0,
                        "reference_price": 1.0010,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            cfg = copy.deepcopy(self._make_ctx().config)
            cfg["journal"] = {"json_path": path}
            cfg["risk"].update({"reentry_cooldown_bars_after_weak_exit": 4})
            recovered = _recover_flat_reentry_state_from_journal(
                cfg,
                sync_ts=datetime(2026, 3, 14, 2, 16, 30, tzinfo=timezone.utc),
                bar_seconds=60.0,
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertAlmostEqual(float(recovered["last_long_exit_price"]), 1.0010, places=6)
            self.assertEqual(int(recovered["reentry_cooldown_remaining"]), 2)
            self.assertEqual(int(recovered["short_loss_cluster_count"]), 0)
            self.assertEqual(int(recovered["short_loss_cluster_age_bars"]), 0)
            self.assertEqual(recovered["exit_reason"], "time_break_even_floor")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_recover_flat_reentry_state_from_journal_applies_external_sync_flat_cooldown(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_external_flat_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:10:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:10:00+00:00",
                        "risk": {
                            "allow": True,
                            "target_btc": 0.0,
                            "reason": "trailing_stop",
                        },
                        "features": {"price": 1.1200},
                    },
                },
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.0000,
                    },
                },
                {
                    "ts": "2026-03-14T02:14:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:14:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 1.0015,
                        "source": "account_sync_delta",
                        "synthetic": True,
                        "position_btc": 0.0,
                    },
                },
                {
                    "ts": "2026-03-14T02:15:00+00:00",
                    "event_type": "core_account_synced",
                    "payload": {
                        "ts": "2026-03-14T02:15:00+00:00",
                        "position_btc": 0.0,
                        "reference_price": 1.0015,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            cfg = copy.deepcopy(self._make_ctx().config)
            cfg["journal"] = {"json_path": path}
            cfg["risk"].update({"reentry_cooldown_bars_after_weak_exit": 4})
            recovered = _recover_flat_reentry_state_from_journal(
                cfg,
                sync_ts=datetime(2026, 3, 14, 2, 15, 30, tzinfo=timezone.utc),
                bar_seconds=60.0,
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertAlmostEqual(float(recovered["last_long_exit_price"]), 1.0015, places=6)
            self.assertEqual(int(recovered["reentry_cooldown_remaining"]), 3)
            self.assertEqual(int(recovered["short_loss_cluster_count"]), 0)
            self.assertEqual(int(recovered["short_loss_cluster_age_bars"]), 0)
            self.assertEqual(recovered["exit_reason"], "account_sync_delta")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_restore_flat_reentry_state_restores_last_long_entry_price(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_last_entry_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.0000,
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:12:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 0.9800},
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:13:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 0.9800,
                    },
                },
                {
                    "ts": "2026-03-14T02:14:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:14:00+00:00",
                        "risk": {
                            "allow": True,
                            "target_btc": 0.0,
                            "reason": "reentry_above_last_entry",
                            "last_long_exit_price": 0.9800,
                            "reentry_cooldown_remaining": 0,
                            "short_loss_cluster_count": 0,
                            "short_loss_cluster_age_bars": 0,
                        },
                        "features": {"price": 1.0100},
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            cfg = copy.deepcopy(self._make_ctx().config)
            cfg["journal"] = {"json_path": path}
            cfg["risk"].update(
                {
                    "reentry_require_price_at_or_below_last_entry": True,
                    "reentry_last_entry_tolerance_bps": 0.0,
                }
            )

            risk = RiskManager(
                RiskConfig(
                    max_exposure_eur=100.0,
                    vol_target_bps=1000.0,
                    daily_loss_limit_eur=1000.0,
                    max_drawdown_pct=100.0,
                    cooldown_bars=1,
                    allow_short=False,
                )
            )
            recovered = _restore_risk_flat_reentry_state_from_journal(
                risk,
                cfg,
                sync_ts=datetime(2026, 3, 14, 2, 14, 30, tzinfo=timezone.utc),
                bar_seconds=60.0,
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertAlmostEqual(float(recovered["last_long_exit_price"]), 0.9800, places=6)
            self.assertAlmostEqual(float(recovered["last_long_entry_price"]), 1.0000, places=6)
            self.assertAlmostEqual(float(getattr(risk, "_last_long_exit_price")), 0.9800, places=6)
            self.assertAlmostEqual(float(getattr(risk, "_last_long_entry_price")), 1.0000, places=6)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_sync_account_flat_recovery_blocks_reentry_after_restart(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_live_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.00,
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:12:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 0.99},
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:13:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 0.99,
                    },
                },
                {
                    "ts": "2026-03-14T02:21:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:21:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.10,
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:22:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 1.05},
                    },
                },
                {
                    "ts": "2026-03-14T02:23:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:23:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 1.0529,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            ctx = self._make_ctx(core_overrides={"warmup": {"enabled": False}})
            ctx.config["journal"] = {"json_path": path}
            ctx.config["risk"].update(
                {
                    "reentry_cooldown_bars_after_whipsaw_stop_loss": 8,
                    "reentry_whipsaw_hard_stop_max_bars": 4,
                    "reentry_loss_cluster_window_bars": 30,
                    "reentry_cooldown_bars_after_loss_cluster": 20,
                    "reentry_min_move_bps": 50.0,
                }
            )
            thread = self._start_core(ctx)
            base_ts = datetime(2026, 3, 14, 2, 27, tzinfo=timezone.utc)
            try:
                ctx.q_control_core.put(
                    ControlCommand(
                        ts=base_ts + timedelta(seconds=13),
                        action="SYNC_ACCOUNT",
                        reason="restart_recovery",
                        payload={
                            "pair": "TEST/EUR",
                            "base_asset": "TEST",
                            "quote_asset": "EUR",
                            "cash_eur": 100.0,
                            "position_btc": 0.0,
                            "reset": True,
                        },
                    )
                )
                self._push_market(ctx, self._event(base_ts, 100.0), settle_sec=0.25)
                self._push_market(ctx, self._event(base_ts + timedelta(minutes=1), 101.0), settle_sec=0.12)
            finally:
                self._stop_core(ctx, thread)

            self.assertEqual(ctx.q_order_intent.qsize(), 0)
            journal_events = self._journal_events(ctx)
            recovered = [evt.payload for evt in journal_events if evt.event_type == "core_reentry_state_recovered"]
            self.assertTrue(recovered)
            self.assertGreater(int(recovered[-1]["reentry_cooldown_remaining"] or 0), 0)
            decisions = [evt.payload for evt in journal_events if evt.event_type == "core_decision"]
            self.assertTrue(decisions)
            self.assertEqual(((decisions[-1].get("risk") or {}).get("reason")), "reentry_cooldown")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_sync_account_flat_recovery_is_not_reapplied_when_unchanged(self):
        fd, path = tempfile.mkstemp(prefix="core_reentry_repeat_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-14T02:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 100.0,
                        "price": 1.00,
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "ts": "2026-03-14T02:12:00+00:00",
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                        "features": {"price": 0.99},
                    },
                },
                {
                    "ts": "2026-03-14T02:13:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-14T02:13:00+00:00",
                        "side": "sell",
                        "qty_btc": 100.0,
                        "price": 0.99,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            ctx = self._make_ctx(core_overrides={"warmup": {"enabled": False}})
            ctx.config["journal"] = {"json_path": path}
            ctx.config["risk"].update(
                {
                    "reentry_cooldown_bars_after_whipsaw_stop_loss": 8,
                    "reentry_whipsaw_hard_stop_max_bars": 4,
                    "reentry_min_move_bps": 50.0,
                }
            )
            thread = self._start_core(ctx)
            base_ts = datetime(2026, 3, 14, 2, 20, tzinfo=timezone.utc)
            sync_payload = {
                "pair": "TEST/EUR",
                "base_asset": "TEST",
                "quote_asset": "EUR",
                "cash_eur": 100.0,
                "position_btc": 0.0,
                "reset": True,
            }
            try:
                ctx.q_control_core.put(
                    ControlCommand(
                        ts=base_ts,
                        action="SYNC_ACCOUNT",
                        reason="restart_recovery",
                        payload=sync_payload,
                    )
                )
                self._push_market(ctx, self._event(base_ts, 100.0), settle_sec=0.12)
                ctx.q_control_core.put(
                    ControlCommand(
                        ts=base_ts + timedelta(minutes=1),
                        action="SYNC_ACCOUNT",
                        reason="repeat_restart_recovery",
                        payload=sync_payload,
                    )
                )
                self._push_market(ctx, self._event(base_ts + timedelta(minutes=1), 100.5), settle_sec=0.12)
            finally:
                self._stop_core(ctx, thread)

            journal_events = self._journal_events(ctx)
            recovered = [evt.payload for evt in journal_events if evt.event_type == "core_reentry_state_recovered"]
            self.assertEqual(len(recovered), 1)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_reload_updates_dynamic_sizing_fraction_from_runtime_config(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": False}})
        runtime_cfg = copy.deepcopy(ctx.config)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "runtime.yaml")
            runtime_cfg["runtime"] = {"config_path": cfg_path}
            runtime_cfg["risk"].update(
                {
                    "max_exposure_mode": "equity",
                    "max_exposure_fraction": 1.0,
                }
            )
            runtime_cfg["order"].update(
                {
                    "cycle_trade_mode": "equity",
                    "cycle_trade_fraction": 1.0,
                    "cycle_trade_eur": 100.0,
                }
            )
            with open(cfg_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(runtime_cfg, fh, sort_keys=False)

            ctx.config = copy.deepcopy(runtime_cfg)
            thread = self._start_core(ctx)
            base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
            try:
                ctx.q_control_core.put(
                    ControlCommand(
                        ts=datetime.now(timezone.utc),
                        action="SYNC_ACCOUNT",
                        reason="test",
                        payload={
                            "pair": "ETH/EUR",
                            "base_asset": "ETH",
                            "quote_asset": "EUR",
                            "cash_eur": 40.0,
                            "position_btc": 0.0,
                            "reset": True,
                        },
                    )
                )
                self._push_market(ctx, self._event(base_ts, 100.0), settle_sec=0.12)

                reloaded_cfg = copy.deepcopy(runtime_cfg)
                reloaded_cfg["risk"]["max_exposure_fraction"] = 0.25
                reloaded_cfg["order"]["cycle_trade_fraction"] = 0.25
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    yaml.safe_dump(reloaded_cfg, fh, sort_keys=False)

                ctx.q_control_core.put(
                    ControlCommand(
                        ts=datetime.now(timezone.utc),
                        action="RELOAD",
                        reason="test_reload_fraction",
                    )
                )
                time.sleep(0.25)
                self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 101.0), settle_sec=0.25)
            finally:
                self._stop_core(ctx, thread)

        journal_events = self._journal_events(ctx)
        reloads = [evt.payload for evt in journal_events if evt.event_type == "core_reload_applied"]
        self.assertTrue(reloads)
        self.assertAlmostEqual(float(reloads[-1]["max_exposure_fraction"]), 0.25)
        self.assertAlmostEqual(float(reloads[-1]["cycle_trade_fraction"]), 0.25)

        decisions = [evt.payload for evt in journal_events if evt.event_type == "core_decision"]
        self.assertTrue(decisions)
        exposure_values = [round(float(dec["sizing"]["max_exposure_eur"]), 6) for dec in decisions]
        cycle_values = [round(float(dec["sizing"]["cycle_trade_eur"]), 6) for dec in decisions]
        self.assertIn(10.0, exposure_values)
        self.assertIn(10.0, cycle_values)

    def test_dynamic_sizing_floors_active_lane_to_min_entry_notional(self):
        ctx = self._make_ctx(core_overrides={"warmup": {"enabled": False}})
        runtime_cfg = copy.deepcopy(ctx.config)
        runtime_cfg["risk"].update(
            {
                "max_exposure_mode": "equity",
                "max_exposure_fraction": 0.055,
            }
        )
        runtime_cfg["order"].update(
            {
                "cycle_trade_mode": "equity",
                "cycle_trade_fraction": 0.055,
                "cycle_trade_eur": 100.0,
            }
        )
        runtime_cfg["exec"] = {"min_entry_notional_eur": 6.0}

        ctx.config = copy.deepcopy(runtime_cfg)
        thread = self._start_core(ctx)
        base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        try:
            ctx.q_control_core.put(
                ControlCommand(
                    ts=datetime.now(timezone.utc),
                    action="SYNC_ACCOUNT",
                    reason="test",
                    payload={
                        "pair": "ETH/EUR",
                        "base_asset": "ETH",
                        "quote_asset": "EUR",
                        "cash_eur": 36.99,
                        "position_btc": 0.0,
                        "reset": True,
                    },
                )
            )
            self._push_market(ctx, self._event(base_ts, 100.0), settle_sec=0.25)
            self._push_market(ctx, self._event(base_ts + timedelta(seconds=1), 101.0), settle_sec=0.25)
        finally:
            self._stop_core(ctx, thread)

        decisions = [evt.payload for evt in self._journal_events(ctx) if evt.event_type == "core_decision"]
        self.assertTrue(decisions)
        exposure_values = [round(float(dec["sizing"]["max_exposure_eur"]), 6) for dec in decisions]
        cycle_values = [round(float(dec["sizing"]["cycle_trade_eur"]), 6) for dec in decisions]
        self.assertIn(6.0, exposure_values)
        self.assertIn(6.0, cycle_values)


if __name__ == "__main__":
    unittest.main()
