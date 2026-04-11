from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scripts.rotation_meta_shadow as rotation_meta_shadow
from trading.binance import trade_mirror
from trading.meta.openai_meta import (
    _build_compact_prompt_payload,
    build_prompt_payload,
    call_openai_meta,
    fallback_recommendation,
)
from trading.meta.rotation_shadow import (
    CounterfactualSample,
    ShadowCandidate,
    TradeSample,
    build_recent_no_trade_examples,
    build_recent_trade_examples,
    build_trade_summary,
    extract_counterfactual_samples,
    extract_trade_samples,
    train_trade_model,
)


class TestRotationMetaShadow(unittest.TestCase):
    def test_preload_env_file_applies_openai_disable_before_arg_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "rotation_meta_shadow.env"
            env_file.write_text(
                "\n".join(
                    [
                        "ROTATION_OPENAI_ENABLED=0",
                        "ROTATION_META_TRADE_LOOKBACK_HOURS=11",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = rotation_meta_shadow._preload_env_file(["--env-file", str(env_file)])
                self.assertEqual(loaded, env_file)
                self.assertFalse(rotation_meta_shadow._env_bool("ROTATION_OPENAI_ENABLED", True))
                self.assertEqual(rotation_meta_shadow._env_float("ROTATION_META_TRADE_LOOKBACK_HOURS", 6.0), 11.0)

    def test_extract_trade_samples_from_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T10:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "alpha_type": "breakout",
                        "features": {
                            "return_bps": 20.0,
                            "trend_return_bps": 12.0,
                            "atr_bps": 4.0,
                            "volume_z": 0.8,
                            "context_return_bps": 90.0,
                            "context_drawdown_bps": -40.0,
                            "context_rebound_bps": 120.0,
                            "context_range_pos": 0.45,
                            "spread_bps": 6.0,
                            "depth": 150.0,
                            "imbalance": 0.4,
                        },
                        "alpha": {
                            "model_class": "BreakoutAlpha",
                            "edge_bps_effective": 24.0,
                            "meta": {
                                "structure": {
                                    "phase": "range",
                                    "confidence": 0.6,
                                    "slope_short_bps": 3.0,
                                    "slope_medium_bps": 1.5,
                                    "drawdown_from_peak_bps": 18.0,
                                    "extension_bps": 22.0,
                                    "up_structure": True,
                                    "down_structure": False,
                                }
                            },
                        },
                        "cost": {"expected_cost_bps": 12.0},
                    },
                },
                {
                    "ts": "2026-03-10T10:00:02+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "buy",
                        "qty_btc": 10.0,
                        "price": 1.0,
                        "fee_eur": 0.01,
                    },
                },
                {
                    "ts": "2026-03-10T10:01:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "trailing_stop"},
                    },
                },
                {
                    "ts": "2026-03-10T10:01:05+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "sell",
                        "qty_btc": 10.0,
                        "price": 1.03,
                        "fee_eur": 0.01,
                    },
                },
                {
                    "ts": "2026-03-10T10:02:00+00:00",
                    "event_type": "market",
                    "payload": {
                        "ts": "2026-03-10T10:01:00+00:00",
                        "open": 1.03,
                        "high": 1.05,
                        "low": 1.02,
                        "close": 1.045,
                        "volume": 1000.0,
                        "micro": {"spread_bps": 4.0, "depth": 120.0, "imbalance": 0.3},
                    },
                },
                {
                    "ts": "2026-03-10T10:03:00+00:00",
                    "event_type": "market",
                    "payload": {
                        "ts": "2026-03-10T10:02:00+00:00",
                        "open": 1.045,
                        "high": 1.055,
                        "low": 1.04,
                        "close": 1.05,
                        "volume": 900.0,
                        "micro": {"spread_bps": 4.0, "depth": 130.0, "imbalance": 0.4},
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            samples = extract_trade_samples(logs)
            self.assertEqual(len(samples), 1)
            sample = samples[0]
            self.assertEqual(sample.symbol, "TEST")
            self.assertTrue(sample.profitable)
            self.assertEqual(sample.exit_reason, "trailing_stop")
            self.assertGreater(sample.net_pnl, 0.0)
            self.assertEqual(sample.strategy_at_entry, "breakout")
            self.assertEqual(sample.strategy_at_entry_source, "alpha_type")
            self.assertEqual(sample.alpha_model_class_at_entry, "BreakoutAlpha")
            self.assertAlmostEqual(sample.features["edge_bps_effective"], 24.0)
            self.assertGreater(sample.post_exit_metrics["post_exit_mfe_15m_bps"], 180.0)
            self.assertGreater(sample.post_exit_metrics["post_exit_close_15m_bps"], 140.0)

    def test_extract_trade_samples_prefers_executed_strategy_over_inferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T10:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "features": {
                            "return_bps": 85.0,
                            "trend_return_bps": 303.0,
                            "atr_bps": 22.0,
                            "volume_z": 1.7,
                            "context_return_bps": 420.0,
                            "context_drawdown_bps": -18.0,
                            "context_rebound_bps": 160.0,
                            "context_range_pos": 1.0,
                            "spread_bps": 7.0,
                            "depth": 220.0,
                            "imbalance": 0.5,
                        },
                        "alpha": {
                            "model_class": "BreakoutAlpha",
                            "edge_bps_effective": 40.0,
                            "meta": {
                                "breakout_state": "up_breakout",
                                "structure": {
                                    "phase": "peak",
                                    "confidence": 0.9,
                                    "slope_short_bps": 12.0,
                                    "drawdown_from_peak_bps": 5.0,
                                    "extension_bps": 90.0,
                                    "up_structure": True,
                                    "down_structure": False,
                                }
                            },
                        },
                        "cost": {"expected_cost_bps": 14.0},
                    },
                },
                {
                    "ts": "2026-03-10T10:00:02+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "buy",
                        "qty_btc": 10.0,
                        "price": 1.0,
                        "fee_eur": 0.01,
                    },
                },
                {
                    "ts": "2026-03-10T10:01:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "hard_stop_loss"},
                    },
                },
                {
                    "ts": "2026-03-10T10:01:05+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "sell",
                        "qty_btc": 10.0,
                        "price": 0.98,
                        "fee_eur": 0.01,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            samples = extract_trade_samples(logs)

        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.strategy_at_entry, "breakout")
        self.assertEqual(sample.strategy_at_entry_source, "alpha.model_class")
        self.assertEqual(sample.inferred_strategy_at_entry, "continuation")

    def test_extract_trade_samples_counts_partial_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T10:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "features": {
                            "return_bps": 12.0,
                            "spread_bps": 5.0,
                            "depth": 120.0,
                        },
                        "alpha": {"edge_bps_effective": 18.0, "meta": {"structure": {"phase": "range"}}},
                        "cost": {"expected_cost_bps": 10.0},
                    },
                },
                {
                    "ts": "2026-03-10T10:00:01+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "buy",
                        "qty_btc": 10.0,
                        "price": 1.0,
                        "fee_eur": 0.10,
                    },
                },
                {
                    "ts": "2026-03-10T10:00:59+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "trim"},
                    },
                },
                {
                    "ts": "2026-03-10T10:01:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "sell",
                        "qty_btc": 4.0,
                        "price": 0.95,
                        "fee_eur": 0.04,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            samples = extract_trade_samples(logs)

        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.exit_reason, "trim")
        self.assertEqual(sample.buy_qty, 4.0)
        self.assertEqual(sample.sell_qty, 4.0)
        self.assertAlmostEqual(sample.buy_notional, 4.0)
        self.assertAlmostEqual(sample.net_pnl, -0.28)
        self.assertFalse(sample.profitable)

    def test_extract_trade_samples_marks_binance_mirror_verified_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            mirror_dir = root / "mirror"
            logs.mkdir()
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T10:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "alpha_type": "breakout",
                        "features": {"return_bps": 20.0, "spread_bps": 6.0, "depth": 150.0},
                        "alpha": {"edge_bps_effective": 24.0, "meta": {"structure": {"phase": "range"}}},
                        "cost": {"expected_cost_bps": 12.0},
                    },
                },
                {
                    "ts": "2026-03-10T10:00:02+00:00",
                    "event_type": "fill",
                    "payload": {"side": "buy", "qty_btc": 10.0, "price": 1.0, "fee_eur": 0.01},
                },
                {
                    "ts": "2026-03-10T10:01:00+00:00",
                    "event_type": "core_decision",
                    "payload": {"risk": {"allow": True, "target_btc": 0.0, "reason": "trailing_stop"}},
                },
                {
                    "ts": "2026-03-10T10:01:05+00:00",
                    "event_type": "fill",
                    "payload": {"side": "sell", "qty_btc": 10.0, "price": 1.03, "fee_eur": 0.01},
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            trade_mirror.write_mirror_rows(
                "TESTUSDC",
                [
                    {
                        "symbol": "TESTUSDC",
                        "coin": "TEST",
                        "side": "BUY",
                        "orderId": "1",
                        "tradeId": "1",
                        "timeMs": 1773136802000,
                        "timeIso": "2026-03-10T10:00:02Z",
                        "quantity": 10.0,
                        "grossUsdc": 10.01,
                    },
                    {
                        "symbol": "TESTUSDC",
                        "coin": "TEST",
                        "side": "SELL",
                        "orderId": "2",
                        "tradeId": "2",
                        "timeMs": 1773136865000,
                        "timeIso": "2026-03-10T10:01:05Z",
                        "quantity": 10.0,
                        "grossUsdc": 10.29,
                    },
                ],
                mirror_dir,
            )

            samples = extract_trade_samples(
                logs,
                mirror_verification="annotate",
                mirror_dir=mirror_dir,
            )

        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertTrue(sample.mirror_verified)
        self.assertEqual(sample.mirror_match_mode, "time_qty_pnl")
        self.assertAlmostEqual(sample.mirror_net_pnl, 0.28)
        self.assertAlmostEqual(sample.mirror_pnl_delta, 0.0)

    def test_extract_trade_samples_require_binance_verified_filters_partial_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            mirror_dir = root / "mirror"
            logs.mkdir()
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T10:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "alpha_type": "breakout",
                        "features": {"return_bps": 20.0, "spread_bps": 6.0, "depth": 150.0},
                        "alpha": {"edge_bps_effective": 24.0, "meta": {"structure": {"phase": "range"}}},
                        "cost": {"expected_cost_bps": 12.0},
                    },
                },
                {
                    "ts": "2026-03-10T10:00:02+00:00",
                    "event_type": "fill",
                    "payload": {"side": "buy", "qty_btc": 10.0, "price": 1.0, "fee_eur": 0.01},
                },
                {
                    "ts": "2026-03-10T10:01:00+00:00",
                    "event_type": "core_decision",
                    "payload": {"risk": {"allow": True, "target_btc": 0.0, "reason": "trailing_stop"}},
                },
                {
                    "ts": "2026-03-10T10:01:05+00:00",
                    "event_type": "fill",
                    "payload": {"side": "sell", "qty_btc": 10.0, "price": 1.03, "fee_eur": 0.01},
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            trade_mirror.write_mirror_rows(
                "TESTUSDC",
                [
                    {
                        "symbol": "TESTUSDC",
                        "coin": "TEST",
                        "side": "BUY",
                        "orderId": "1",
                        "tradeId": "1",
                        "timeMs": 1773136802000,
                        "timeIso": "2026-03-10T10:00:02Z",
                        "quantity": 10.0,
                        "grossUsdc": 10.01,
                    },
                    {
                        "symbol": "TESTUSDC",
                        "coin": "TEST",
                        "side": "SELL",
                        "orderId": "2",
                        "tradeId": "2",
                        "timeMs": 1773136865000,
                        "timeIso": "2026-03-10T10:01:05Z",
                        "quantity": 10.0,
                        "grossUsdc": 10.65,
                    },
                ],
                mirror_dir,
            )

            annotated = extract_trade_samples(
                logs,
                mirror_verification="annotate",
                mirror_dir=mirror_dir,
            )
            verified_only = extract_trade_samples(
                logs,
                mirror_verification="require",
                mirror_dir=mirror_dir,
            )

        self.assertEqual(len(annotated), 1)
        self.assertFalse(annotated[0].mirror_verified)
        self.assertEqual(annotated[0].mirror_match_mode, "time_qty_only")
        self.assertEqual(verified_only, [])

    def test_extract_trade_samples_resets_stale_residual_after_sync_dust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T09:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "features": {"return_bps": 8.0, "spread_bps": 4.0, "depth": 120.0},
                        "alpha": {"edge_bps_effective": 16.0, "meta": {"structure": {"phase": "range"}}},
                        "cost": {"expected_cost_bps": 10.0},
                    },
                },
                {
                    "ts": "2026-03-10T09:00:01+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "buy",
                        "qty_btc": 10.0,
                        "price": 1.0,
                        "fee_eur": 0.10,
                    },
                },
                {
                    "ts": "2026-03-10T09:01:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "trim"},
                    },
                },
                {
                    "ts": "2026-03-10T09:01:05+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "sell",
                        "qty_btc": 4.0,
                        "price": 1.1,
                        "fee_eur": 0.04,
                    },
                },
                {
                    "ts": "2026-03-10T09:02:00+00:00",
                    "event_type": "exec_core_account_sync",
                    "payload": {
                        "pair": "TEST/USDC",
                        "base_asset": "TEST",
                        "quote_asset": "USDC",
                        "cash_eur": 10.0,
                        "position_btc": 0.2,
                        "avg_entry_price": 1.0,
                        "source": "periodic",
                    },
                },
                {
                    "ts": "2026-03-10T09:03:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "features": {"return_bps": 18.0, "spread_bps": 3.0, "depth": 140.0},
                        "alpha": {"edge_bps_effective": 22.0, "meta": {"structure": {"phase": "range"}}},
                        "cost": {"expected_cost_bps": 10.0},
                    },
                },
                {
                    "ts": "2026-03-10T09:03:01+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "buy",
                        "qty_btc": 20.0,
                        "price": 2.0,
                        "fee_eur": 0.20,
                    },
                },
                {
                    "ts": "2026-03-10T09:04:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "trailing_stop"},
                    },
                },
                {
                    "ts": "2026-03-10T09:04:05+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "sell",
                        "qty_btc": 20.0,
                        "price": 2.03,
                        "fee_eur": 0.20,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            samples = extract_trade_samples(logs)

        self.assertEqual(len(samples), 2)
        sample = samples[-1]
        self.assertEqual(sample.exit_reason, "trailing_stop")
        self.assertAlmostEqual(sample.buy_qty, 20.0)
        self.assertAlmostEqual(sample.buy_notional, 40.0)
        self.assertAlmostEqual(sample.sell_notional, 40.6)
        self.assertAlmostEqual(sample.net_pnl, 0.2)
        self.assertTrue(sample.profitable)

    def test_extract_counterfactual_samples_tracks_missed_entry_when_blocked_symbol_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "journal_live_binance_test_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-10T15:00:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "features": {
                            "price": 1.0,
                            "return_bps": 6.0,
                            "trend_return_bps": 10.0,
                            "atr_bps": 4.0,
                            "volume_z": 0.6,
                            "context_return_bps": 40.0,
                            "context_drawdown_bps": -20.0,
                            "context_rebound_bps": 60.0,
                            "context_range_pos": 0.35,
                            "spread_bps": 5.0,
                            "depth": 120.0,
                            "imbalance": 0.3,
                        },
                        "alpha": {
                            "edge_bps_effective": 12.0,
                            "meta": {"structure": {"phase": "lift_off", "up_structure": True, "down_structure": False}},
                        },
                        "cost": {"expected_cost_bps": 10.0},
                        "gate": {"allow": False, "reason": "edge_below_costs"},
                        "risk": {"allow": False, "target_btc": 0.0, "reason": "gate_block"},
                        "intents": [],
                    },
                },
                {
                    "ts": "2026-03-10T15:01:00+00:00",
                    "event_type": "market",
                    "payload": {
                        "ts": "2026-03-10T15:01:00+00:00",
                        "open": 1.0,
                        "high": 1.015,
                        "low": 0.998,
                        "close": 1.011,
                        "volume": 1000.0,
                        "micro": {"spread_bps": 4.0, "depth": 130.0, "imbalance": 0.4},
                    },
                },
                {
                    "ts": "2026-03-10T15:02:00+00:00",
                    "event_type": "market",
                    "payload": {
                        "ts": "2026-03-10T15:02:00+00:00",
                        "open": 1.011,
                        "high": 1.03,
                        "low": 1.009,
                        "close": 1.024,
                        "volume": 1200.0,
                        "micro": {"spread_bps": 4.0, "depth": 140.0, "imbalance": 0.5},
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            samples = extract_counterfactual_samples(logs)

        self.assertEqual(len(samples), 1)
        sample = samples[0]
        self.assertEqual(sample.block_reason, "edge_below_costs")
        self.assertEqual(sample.regime_tag, "lift_off")
        self.assertEqual(sample.session_tag, "us")
        self.assertGreater(sample.post_decision_metrics["post_decision_close_15m_bps"], 80.0)

    def test_extract_trade_samples_drops_trade_when_sync_expands_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            path = logs / "journal_live_binance_zro_usdc_rotation.jsonl"
            lines = [
                {
                    "ts": "2026-03-12T22:11:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "features": {"return_bps": 10.0, "spread_bps": 5.0, "depth": 120.0},
                        "alpha": {"edge_bps_effective": 16.0, "meta": {"structure": {"phase": "range"}}},
                        "cost": {"expected_cost_bps": 10.0},
                        "risk": {"allow": True, "target_btc": 4.75},
                    },
                },
                {
                    "ts": "2026-03-12T22:11:07+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "buy",
                        "qty_btc": 4.75,
                        "price": 2.091,
                        "fee_eur": 0.0094356375,
                    },
                },
                {
                    "ts": "2026-03-12T22:11:26+00:00",
                    "event_type": "exec_core_account_sync",
                    "payload": {
                        "pair": "ZRO/USDC",
                        "base_asset": "ZRO",
                        "quote_asset": "USDC",
                        "position_btc": 4.9461835,
                        "avg_entry_price": 2.09298645,
                        "source": "startup",
                    },
                },
                {
                    "ts": "2026-03-12T22:14:00+00:00",
                    "event_type": "core_decision",
                    "payload": {
                        "risk": {"allow": True, "target_btc": 0.0, "reason": "failed_start_exit"},
                    },
                },
                {
                    "ts": "2026-03-12T22:14:05+00:00",
                    "event_type": "fill",
                    "payload": {
                        "side": "sell",
                        "qty_btc": 4.94,
                        "price": 2.084,
                        "fee_eur": 0.00978021,
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            samples = extract_trade_samples(logs)

        self.assertEqual(samples, [])

    def test_train_trade_model_separates_simple_samples(self) -> None:
        samples: list[TradeSample] = []
        for idx in range(10):
            samples.append(
                TradeSample(
                    symbol="WIN",
                    entry_ts=f"2026-03-10T10:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T10:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=10.2,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=0.19,
                    profitable=True,
                    exit_reason="take_profit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": 25.0, "spread_bps": 4.0, "edge_bps_effective": 18.0},
                )
            )
        for idx in range(10, 20):
            samples.append(
                TradeSample(
                    symbol="LOSS",
                    entry_ts=f"2026-03-10T10:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T10:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=9.8,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=-0.21,
                    profitable=False,
                    exit_reason="failed_start_exit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": -18.0, "spread_bps": 16.0, "edge_bps_effective": 4.0},
                )
            )
        model = train_trade_model(samples, epochs=300)
        p_win = model.predict_proba({"return_bps": 20.0, "spread_bps": 5.0, "edge_bps_effective": 16.0})
        p_loss = model.predict_proba({"return_bps": -12.0, "spread_bps": 18.0, "edge_bps_effective": 2.0})
        self.assertGreater(p_win, p_loss)
        self.assertGreater(model.train_metrics["accuracy"], 0.6)

    def test_train_trade_model_calibrates_average_probability_to_train_base_rate(self) -> None:
        samples: list[TradeSample] = []
        for idx in range(12):
            samples.append(
                TradeSample(
                    symbol="LOSS",
                    entry_ts=f"2026-03-10T11:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T11:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=9.8,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=-0.21,
                    profitable=False,
                    exit_reason="failed_start_exit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": -6.0 + (idx * 0.1), "spread_bps": 14.0, "edge_bps_effective": 8.0},
                )
            )
        for idx in range(12, 18):
            samples.append(
                TradeSample(
                    symbol="WIN",
                    entry_ts=f"2026-03-10T11:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T11:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=10.2,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=0.19,
                    profitable=True,
                    exit_reason="take_profit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": 7.0 + ((idx - 18) * 0.1), "spread_bps": 4.0, "edge_bps_effective": 18.0},
                )
            )
        for idx in range(18, 24):
            samples.append(
                TradeSample(
                    symbol="LOSS",
                    entry_ts=f"2026-03-10T11:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T11:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=9.8,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=-0.21,
                    profitable=False,
                    exit_reason="failed_start_exit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": -5.0 + ((idx - 18) * 0.1), "spread_bps": 13.0, "edge_bps_effective": 7.0},
                )
            )

        model = train_trade_model(samples, epochs=300)
        train_samples = samples[:18]
        weighted_prob_sum = 0.0
        weighted_label_sum = 0.0
        weight_total = 0.0
        for sample in train_samples:
            weight = float(sample.learning_weight or 1.0)
            weighted_prob_sum += model.predict_proba(sample.features) * weight
            weighted_label_sum += (1.0 if sample.profitable else 0.0) * weight
            weight_total += weight
        avg_train_probability = weighted_prob_sum / float(weight_total)
        train_positive_rate = weighted_label_sum / float(weight_total)

        self.assertNotEqual(model.calibration_bias, 0.0)
        self.assertAlmostEqual(avg_train_probability, train_positive_rate, delta=0.06)
        self.assertGreater(model.training_diagnostics["last_train_weight"], model.training_diagnostics["first_train_weight"])

    def test_train_trade_model_downweights_ambiguous_loss_samples(self) -> None:
        samples: list[TradeSample] = []
        for idx in range(8):
            samples.append(
                TradeSample(
                    symbol="WIN",
                    entry_ts=f"2026-03-10T12:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T12:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=10.2,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=0.19,
                    profitable=True,
                    exit_reason="take_profit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": 18.0, "spread_bps": 4.0, "edge_bps_effective": 18.0},
                )
            )
        samples.append(
            TradeSample(
                symbol="AMB",
                entry_ts="2026-03-10T12:08:00+00:00",
                exit_ts="2026-03-10T12:08:30+00:00",
                buy_qty=1.0,
                sell_qty=1.0,
                buy_notional=10.0,
                sell_notional=9.96,
                fees=0.01,
                dust_notional=0.0,
                net_pnl=-0.05,
                profitable=False,
                exit_reason="red_candle_exit",
                strategy_at_entry="continuation",
                strategy_scores={"continuation": 1.0},
                features={"return_bps": 9.0, "spread_bps": 5.0, "edge_bps_effective": 14.0},
                post_exit_metrics={
                    "post_exit_bars_15m": 1.0,
                    "post_exit_close_15m_bps": 32.0,
                    "post_exit_bars_30m": 2.0,
                    "post_exit_close_30m_bps": 84.0,
                    "post_exit_bars_60m": 3.0,
                    "post_exit_close_60m_bps": 96.0,
                    "post_exit_mae_15m_bps": -2.0,
                    "post_exit_mae_30m_bps": -4.0,
                },
            )
        )
        for idx in range(9, 16):
            samples.append(
                TradeSample(
                    symbol="LOSS",
                    entry_ts=f"2026-03-10T12:{idx:02d}:00+00:00",
                    exit_ts=f"2026-03-10T12:{idx:02d}:30+00:00",
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=9.8,
                    fees=0.01,
                    dust_notional=0.0,
                    net_pnl=-0.21,
                    profitable=False,
                    exit_reason="failed_start_exit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": -12.0, "spread_bps": 14.0, "edge_bps_effective": 6.0},
                    post_exit_metrics={
                        "post_exit_bars_15m": 1.0,
                        "post_exit_close_15m_bps": -16.0,
                        "post_exit_bars_30m": 2.0,
                        "post_exit_close_30m_bps": -24.0,
                        "post_exit_mae_15m_bps": -18.0,
                        "post_exit_mae_30m_bps": -26.0,
                    },
                )
            )

        model = train_trade_model(samples, epochs=200)

        self.assertEqual(model.training_diagnostics["ambiguous_loss_count"], 1.0)
        self.assertGreater(model.training_diagnostics["last_train_weight"], model.training_diagnostics["first_train_weight"])
        self.assertLess(
            model.training_diagnostics["avg_ambiguous_loss_weight"],
            model.training_diagnostics["avg_train_weight"],
        )

    def test_build_trade_summary_marks_early_exit_pressure(self) -> None:
        exit_base_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        summary = build_trade_summary(
            [
                TradeSample(
                    symbol="FAST",
                    entry_ts=(exit_base_ts - timedelta(minutes=2)).isoformat(),
                    exit_ts=exit_base_ts.isoformat(),
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=9.8,
                    fees=0.02,
                    dust_notional=0.0,
                    net_pnl=-0.22,
                    profitable=False,
                    exit_reason="failed_start_exit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": -8.0},
                    post_exit_metrics={
                        "post_exit_bars_15m": 2.0,
                        "post_exit_mfe_15m_bps": 24.0,
                        "post_exit_mae_15m_bps": -26.0,
                        "post_exit_close_15m_bps": -4.0,
                        "post_exit_bars_30m": 3.0,
                        "post_exit_mfe_30m_bps": 36.0,
                        "post_exit_mae_30m_bps": -26.0,
                        "post_exit_close_30m_bps": 12.0,
                        "post_exit_bars_60m": 6.0,
                        "post_exit_mfe_60m_bps": 58.0,
                        "post_exit_mae_60m_bps": -26.0,
                        "post_exit_close_60m_bps": 24.0,
                        "post_exit_bars_120m": 10.0,
                        "post_exit_mfe_120m_bps": 74.0,
                        "post_exit_mae_120m_bps": -26.0,
                        "post_exit_close_120m_bps": 36.0,
                    },
                )
            ],
            lookback_hours=72.0,
        )

        self.assertGreater(summary.exit_path_summary["failed_start_recovery_rate"], 0.5)
        self.assertGreater(summary.exit_path_summary["shakeout_then_run_rate"], 0.5)
        self.assertGreater(summary.exit_path_summary["hold_opportunity_rate"], 0.5)
        self.assertGreater(summary.exit_path_summary["avg_post_exit_close_60m_bps"], 20.0)
        self.assertIn("exit_path_summary", summary.strategy_breakdown["continuation"])

    def test_build_trade_summary_marks_micro_pop_loss_run(self) -> None:
        exit_base_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        summary = build_trade_summary(
            [
                TradeSample(
                    symbol="FAST",
                    entry_ts=(exit_base_ts - timedelta(minutes=4)).isoformat(),
                    exit_ts=exit_base_ts.isoformat(),
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=10.015,
                    fees=0.03,
                    dust_notional=0.0,
                    net_pnl=-0.015,
                    profitable=False,
                    exit_reason="time_break_even_floor",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": 4.0},
                    post_exit_metrics={
                        "post_exit_bars_15m": 4.0,
                        "post_exit_mfe_15m_bps": 26.0,
                        "post_exit_mae_15m_bps": -3.0,
                        "post_exit_close_15m_bps": 10.0,
                        "post_exit_bars_30m": 8.0,
                        "post_exit_mfe_30m_bps": 44.0,
                        "post_exit_mae_30m_bps": -3.0,
                        "post_exit_close_30m_bps": 18.0,
                        "post_exit_bars_60m": 16.0,
                        "post_exit_mfe_60m_bps": 62.0,
                        "post_exit_mae_60m_bps": -3.0,
                        "post_exit_close_60m_bps": 28.0,
                    },
                )
            ],
            lookback_hours=72.0,
        )

        self.assertGreater(summary.exit_path_summary["micro_pop_loss_run_rate"], 0.5)
        self.assertGreater(summary.exit_path_summary["hold_opportunity_rate"], 0.5)
        self.assertGreater(
            summary.symbol_breakdown["FAST"]["exit_path_summary"]["micro_pop_loss_run_rate"],
            0.5,
        )

    def test_build_trade_summary_includes_no_trade_and_segment_breakdown(self) -> None:
        base_ts = datetime.now(timezone.utc) - timedelta(hours=1)
        summary = build_trade_summary(
            [
                TradeSample(
                    symbol="FAST",
                    entry_ts=(base_ts - timedelta(minutes=5)).isoformat(),
                    exit_ts=base_ts.isoformat(),
                    buy_qty=1.0,
                    sell_qty=1.0,
                    buy_notional=10.0,
                    sell_notional=9.9,
                    fees=0.02,
                    dust_notional=0.0,
                    net_pnl=-0.12,
                    profitable=False,
                    exit_reason="failed_start_exit",
                    strategy_at_entry="continuation",
                    strategy_scores={"continuation": 1.0},
                    features={"return_bps": 4.0},
                    regime_tag="lift_off",
                    session_tag="us",
                    post_exit_metrics={
                        "post_exit_bars_30m": 6.0,
                        "post_exit_mfe_30m_bps": 26.0,
                        "post_exit_close_30m_bps": 12.0,
                    },
                )
            ],
            lookback_hours=24.0,
            no_trade_samples=[
                CounterfactualSample(
                    symbol="FAST",
                    decision_ts=(base_ts + timedelta(minutes=5)).isoformat(),
                    anchor_price=1.0,
                    block_reason="edge_below_costs",
                    gate_reason="edge_below_costs",
                    risk_reason="gate_block",
                    strategy_primary="continuation",
                    features={"return_bps": 5.0},
                    decision_context={"phase": "lift_off"},
                    regime_tag="lift_off",
                    session_tag="us",
                    post_decision_metrics={
                        "post_decision_bars_30m": 6.0,
                        "post_decision_mfe_30m_bps": 32.0,
                        "post_decision_close_30m_bps": 16.0,
                    },
                )
            ],
        )

        self.assertEqual(summary.no_trade_summary["sample_count"], 1)
        self.assertGreater(summary.no_trade_summary["missed_entry_rate"], 0.5)
        self.assertIn("lift_off", summary.regime_breakdown)
        self.assertEqual(summary.regime_breakdown["lift_off"]["no_trade_count"], 1)
        self.assertGreater(
            summary.regime_breakdown["lift_off"]["no_trade_summary"]["missed_entry_rate"],
            0.5,
        )
        self.assertIn("us", summary.session_breakdown)

    def test_openai_meta_falls_back_without_key(self) -> None:
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            candidates = [
                ShadowCandidate(
                    symbol="ROBO",
                    ts="2026-03-10T10:00:00+00:00",
                    age_sec=5.0,
                    selected=True,
                    watch=True,
                    current_profile="scalp_guarded",
                    strategy_primary="breakout",
                    gate_reason="",
                    open_notional=0.0,
                    has_position=False,
                    p_profit=0.62,
                    feature_vector={"return_bps": 25.0},
                    decision_context={"phase": "range", "edge_bps_effective": 18.0, "expected_cost_bps": 11.0},
                )
            ]
            summary = build_trade_summary(
                [
                    TradeSample(
                        symbol="ROBO",
                        entry_ts="2026-03-10T09:00:00+00:00",
                        exit_ts="2026-03-10T09:02:00+00:00",
                        buy_qty=1.0,
                        sell_qty=1.0,
                        buy_notional=10.0,
                        sell_notional=9.7,
                        fees=0.02,
                        dust_notional=0.0,
                        net_pnl=-0.32,
                        profitable=False,
                        exit_reason="failed_start_exit",
                        strategy_at_entry="breakout",
                        strategy_scores={"breakout": 1.0},
                        features={"return_bps": -10.0},
                    )
                ],
                lookback_hours=24.0,
            )
            fallback = fallback_recommendation(
                current_profile="scalp_guarded",
                trade_summary=summary,
                candidates=candidates,
            )
            result = call_openai_meta(prompt_payload={"hello": "world"}, fallback=fallback)
            self.assertEqual(result.mode, "fallback")
            self.assertEqual(result.recommendation["profile"], fallback["profile"])
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old

    def test_build_prompt_payload_includes_recent_trade_examples(self) -> None:
        samples = [
            TradeSample(
                symbol="FAST",
                entry_ts="2026-03-10T09:00:00+00:00",
                exit_ts="2026-03-10T09:05:00+00:00",
                buy_qty=100.0,
                sell_qty=100.0,
                buy_notional=10.0,
                sell_notional=10.01,
                fees=0.03,
                dust_notional=0.0,
                net_pnl=-0.02,
                profitable=False,
                exit_reason="green_candle_take_exit",
                strategy_at_entry="continuation",
                strategy_scores={"continuation": 1.0},
                features={
                    "return_bps": 8.0,
                    "trend_return_bps": 12.0,
                    "spread_bps": 6.0,
                    "expected_cost_bps": 10.0,
                    "context_range_pos": 0.42,
                    "up_structure": 1.0,
                    "down_structure": 0.0,
                    "phase_range": 1.0,
                },
                post_exit_metrics={
                    "post_exit_bars_15m": 4.0,
                    "post_exit_mfe_15m_bps": 26.0,
                    "post_exit_mae_15m_bps": -3.0,
                    "post_exit_close_15m_bps": 10.0,
                    "post_exit_bars_30m": 8.0,
                    "post_exit_mfe_30m_bps": 44.0,
                    "post_exit_mae_30m_bps": -3.0,
                    "post_exit_close_30m_bps": 18.0,
                    "post_exit_bars_60m": 16.0,
                    "post_exit_mfe_60m_bps": 62.0,
                    "post_exit_close_60m_bps": 28.0,
                    "post_exit_bars_120m": 24.0,
                    "post_exit_mfe_120m_bps": 74.0,
                    "post_exit_close_120m_bps": 36.0,
                },
            )
        ]
        no_trade_samples = [
            CounterfactualSample(
                symbol="FAST",
                decision_ts="2026-03-10T09:07:00+00:00",
                anchor_price=10.0,
                block_reason="edge_below_costs",
                gate_reason="edge_below_costs",
                risk_reason="gate_block",
                strategy_primary="continuation",
                features={"return_bps": 6.0, "expected_cost_bps": 10.0},
                decision_context={"phase": "range", "edge_bps_effective": 8.0},
                regime_tag="range",
                session_tag="europe",
                post_decision_metrics={
                    "post_decision_bars_15m": 4.0,
                    "post_decision_mfe_15m_bps": 24.0,
                    "post_decision_mae_15m_bps": -2.0,
                    "post_decision_close_15m_bps": 10.0,
                    "post_decision_bars_30m": 8.0,
                    "post_decision_mfe_30m_bps": 36.0,
                    "post_decision_close_30m_bps": 16.0,
                    "post_decision_bars_60m": 16.0,
                    "post_decision_mfe_60m_bps": 50.0,
                    "post_decision_close_60m_bps": 22.0,
                },
            )
        ]
        recent_examples = build_recent_trade_examples(samples, lookback_hours=240.0, limit=4)
        recent_no_trade_examples = build_recent_no_trade_examples(no_trade_samples, lookback_hours=240.0, limit=4)
        summary = build_trade_summary(samples, lookback_hours=240.0, no_trade_samples=no_trade_samples)
        candidates = [
            ShadowCandidate(
                symbol="FAST",
                ts="2026-03-10T09:05:00+00:00",
                age_sec=5.0,
                selected=False,
                watch=True,
                current_profile="scalp_guarded",
                strategy_primary="continuation",
                gate_reason="",
                open_notional=0.0,
                has_position=False,
                p_profit=0.61,
                feature_vector={"return_bps": 8.0},
                decision_context={"phase": "range", "edge_bps_effective": 18.0, "expected_cost_bps": 10.0},
            )
        ]
        payload = build_prompt_payload(
            current_profile="scalp_guarded",
            active_state={
                "selected": ["FAST"],
                "watch_symbols": ["FAST", "ROBO"],
                "selected_strategy_map": {"FAST": "continuation"},
            },
            trade_summary=summary,
            candidates=candidates,
            model_info={"available": True, "source": "fresh_train"},
            watch_pool_strategy_summary={
                "continuation": {
                    "candidate_count": 3,
                    "buy_ready_count": 2,
                    "ml_positive_count": 1,
                    "avg_top_p_profit": 0.61,
                    "dominant_gate_reasons": {"structure_stall": 2},
                    "top_candidates": [{"symbol": "FAST", "eligible": True, "p_profit": 0.61}],
                }
            },
            recent_trade_examples=recent_examples,
            recent_no_trade_examples=recent_no_trade_examples,
        )
        compact_payload = _build_compact_prompt_payload(payload)

        self.assertEqual(payload["watch_symbols"], ["FAST", "ROBO"])
        self.assertEqual(payload["recent_trade_examples"][0]["diagnosis_hint"], "likely_exit_too_early")
        self.assertIn("micro_pop_loss_run", payload["recent_trade_examples"][0]["flags"])
        self.assertEqual(payload["recent_no_trade_examples"][0]["diagnosis_hint"], "likely_gate_too_strict")
        self.assertGreater(payload["trade_summary"]["no_trade_summary"]["missed_entry_rate"], 0.5)
        self.assertIn("regime_breakdown", payload["trade_summary"])
        self.assertIn("entry_path_summary", payload["trade_summary"])
        self.assertIn("parameter_overrides", payload["response_schema"])
        self.assertIn(
            "ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS",
            payload["response_schema"]["parameter_overrides"],
        )
        self.assertEqual(compact_payload["recent_trade_examples"][0]["symbol"], "FAST")
        self.assertEqual(compact_payload["recent_no_trade_examples"][0]["symbol"], "FAST")
        self.assertIn("watch_pool_market_health", compact_payload)
        self.assertNotIn("symbol_breakdown", compact_payload["trade_summary"])
        self.assertIn("entry_path_summary", compact_payload["trade_summary"])
        self.assertIn("scalp_guarded_open", payload["response_schema"]["profile"])

    def test_reuse_recent_openai_report_within_interval(self) -> None:
        reused = rotation_meta_shadow._reuse_recent_openai_report(
            {
                "generated_at": "2026-03-12T22:00:00+00:00",
                "source_generated_at": "2026-03-12T22:00:00+00:00",
                "source_mode": "llm_http",
                "mode": "llm_http",
                "model": "gpt-5.1",
                "recommendation": {"profile": "scalp_uptrend"},
                "response_payload": {"profile": "scalp_uptrend"},
            },
            now=rotation_meta_shadow._parse_iso8601("2026-03-12T22:09:00+00:00"),
            requested_model="gpt-5.1",
            min_interval_minutes=15.0,
        )

        self.assertIsNotNone(reused)
        self.assertEqual(reused["mode"], "reused_recent")
        self.assertEqual(reused["recommendation"]["profile"], "scalp_uptrend")

    def test_reuse_recent_openai_report_skips_on_model_change(self) -> None:
        reused = rotation_meta_shadow._reuse_recent_openai_report(
            {
                "generated_at": "2026-03-12T22:00:00+00:00",
                "source_generated_at": "2026-03-12T22:00:00+00:00",
                "source_mode": "llm_http",
                "mode": "llm_http",
                "model": "gpt-5-mini",
                "recommendation": {"profile": "scalp_guarded"},
            },
            now=rotation_meta_shadow._parse_iso8601("2026-03-12T22:09:00+00:00"),
            requested_model="gpt-5.1",
            min_interval_minutes=15.0,
        )

        self.assertIsNone(reused)

    def test_load_local_secret_envs_prefers_openai_specific_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            openai_env = tmp_path / "rotation-openai.env"
            trading_env = tmp_path / "trading-secrets.env"
            openai_env.write_text("openai_specific\n", encoding="utf-8")
            trading_env.write_text(
                "OPENAI_API_KEY=openai_shared\nBINANCE_API_KEY=binance_key\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ROTATION_OPENAI_SECRET_ENV_FILE": str(openai_env),
                    "CODEX_TRADING_SECRETS_ENV": str(trading_env),
                },
                clear=True,
            ):
                rotation_meta_shadow._load_local_secret_envs()

                self.assertEqual(os.environ["OPENAI_API_KEY"], "openai_specific")
                self.assertEqual(os.environ["BINANCE_API_KEY"], "binance_key")

    def test_write_selector_runtime_env_writes_profile_override_and_parameter_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime.env"
            report = rotation_meta_shadow._write_selector_runtime_env(
                path,
                generated_at="2026-03-12T10:00:00+00:00",
                recommendation={
                    "profile": "scalp_uptrend",
                    "profile_override": "scalp_uptrend",
                    "risk_mode": "normal",
                    "confidence": 0.72,
                    "strategy_weights": {"continuation": 1.0},
                    "strategy_actions": {
                        "continuation": {"mode": "primary", "slot_target": 2, "top_symbols": ["FET", "SOL"]}
                    },
                    "parameter_overrides": {
                        "ROTATION_ENTRY_EDGE_BPS": 4.2,
                        "ROTATION_GATE_COST_COVERAGE_RATIO": 0.86,
                    },
                },
                current_profile="scalp_guarded",
                top=4,
                mode="fallback",
            )

            text = path.read_text(encoding="utf-8")

        self.assertIn("ROTATION_PROFILE_OVERRIDE=scalp_uptrend", text)
        self.assertIn("ROTATION_ENTRY_EDGE_BPS=4.2", text)
        self.assertIn("ROTATION_GATE_COST_COVERAGE_RATIO=0.86", text)
        self.assertNotIn("ROTATION_STRATEGY_WEIGHT_PULLBACK_CONTINUATION", text)
        self.assertNotIn("ROTATION_STRATEGY_WEIGHT_BREAKOUT_RETEST", text)
        self.assertNotIn("ROTATION_STRATEGY_WEIGHT_RELATIVE_STRENGTH", text)
        self.assertEqual(report["profile"], "scalp_uptrend")
        self.assertEqual(report["parameter_overrides"]["ROTATION_ENTRY_EDGE_BPS"], 4.2)

    def test_merge_autotune_softens_stop_new_entries_for_early_exit_signal(self) -> None:
        merged = rotation_meta_shadow._merge_autotune_recommendation(
            {
                "profile": "scalp_lockdown",
                "risk_mode": "stop_new_entries",
                "avoid_symbols": ["ALGO", "ZAMA", "FET"],
            },
            {
                "enabled": True,
                "confidence": 0.66,
                "score_margin": 24.0,
                "trade_count": 10,
                "no_trade_sample_count": 6,
                "recommended_profile": "scalp_uptrend",
                "risk_mode_override": "cautious",
                "early_exit_bias": 0.72,
                "protective_exit_bias": 0.10,
                "missed_entry_bias": 0.28,
                "correct_block_bias": 0.04,
                "failed_start_recovery_rate": 0.82,
                "shakeout_then_run_rate": 0.28,
                "micro_pop_loss_run_rate": 0.36,
                "parameter_overrides": {"ROTATION_TRAILING_STOP_BPS": 14.0},
                "avoid_symbols": [],
                "exit_problem_symbols": ["ALGO", "ZAMA"],
                "reason": "early_exit_recovery_detected",
            },
        )

        self.assertEqual(merged["risk_mode"], "cautious")
        self.assertEqual(merged["profile"], "scalp_uptrend")
        self.assertEqual(merged["parameter_overrides"]["ROTATION_TRAILING_STOP_BPS"], 14.0)
        self.assertEqual(merged["local_autotune"]["failed_start_recovery_rate"], 0.82)
        self.assertEqual(merged["local_autotune"]["shakeout_then_run_rate"], 0.28)
        self.assertEqual(merged["local_autotune"]["micro_pop_loss_run_rate"], 0.36)
        self.assertEqual(merged["avoid_symbols"], ["FET"])

    def test_merge_autotune_keeps_profile_stable_in_reused_window_without_enough_evidence(self) -> None:
        merged = rotation_meta_shadow._merge_autotune_recommendation(
            {
                "profile": "scalp_breakout",
                "risk_mode": "cautious",
            },
            {
                "enabled": True,
                "confidence": 0.72,
                "score_margin": 14.0,
                "trade_count": 5,
                "no_trade_sample_count": 2,
                "recommended_profile": "scalp_uptrend",
                "risk_mode_override": "normal",
                "early_exit_bias": 0.44,
                "protective_exit_bias": 0.11,
                "missed_entry_bias": 0.18,
                "correct_block_bias": 0.05,
                "shakeout_then_run_rate": 0.2,
                "micro_pop_loss_run_rate": 0.1,
                "parameter_overrides": {"ROTATION_ENTRY_EDGE_BPS": 3.2},
                "avoid_symbols": [],
                "reason": "best_profile=scalp_uptrend margin=14.0",
            },
            stable_window=True,
        )

        self.assertEqual(merged["profile"], "scalp_breakout")
        self.assertEqual(merged["risk_mode"], "cautious")
        self.assertEqual(merged["parameter_overrides"]["ROTATION_ENTRY_EDGE_BPS"], 3.2)


if __name__ == "__main__":
    unittest.main()
