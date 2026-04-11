from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from trading.meta.rotation_shadow import CounterfactualSample, TradeSample, _extract_vector_from_decision
from trading.meta.rotation_strategy_lab import (
    StrategyLabSpec,
    _technical_issue_classes,
    _strategy_trials,
    run_strategy_labs,
    strategy_edge_for_features,
)


class RotationStrategyLabTests(unittest.TestCase):
    def test_extract_vector_keeps_strategy_specific_alpha_metrics(self) -> None:
        event = {
            "payload": {
                "features": {
                    "return_bps": 8.0,
                    "trend_return_bps": 120.0,
                    "atr_bps": 14.0,
                    "volume_z": 0.4,
                    "context_return_bps": 90.0,
                    "context_drawdown_bps": -40.0,
                    "context_rebound_bps": 110.0,
                    "context_range_pos": 0.22,
                    "spread_bps": 4.0,
                    "depth": 20000.0,
                    "imbalance": 0.1,
                },
                "alpha": {
                    "edge_bps_effective": 22.0,
                    "meta": {
                        "swing_state": "micro_valley_rebound",
                        "swing_momentum_bps": 4.5,
                        "swing_range_bps": 38.0,
                        "swing_oscillator": 0.18,
                        "breakout_state": "up_breakout",
                        "breakout_up_bps": 5.8,
                        "breakout_down_bps": -2.0,
                        "continuation_state": "early_liftoff_override",
                        "structure": {
                            "phase": "lift_off",
                            "confidence": 0.8,
                            "slope_short_bps": 4.0,
                            "slope_medium_bps": 3.1,
                            "slope_long_bps": 1.5,
                            "drawdown_from_peak_bps": 120.0,
                            "extension_bps": 35.0,
                            "range_pos": 0.2,
                            "rebound_bps": 95.0,
                            "bars_since_peak": 12,
                            "up_structure": True,
                            "down_structure": False,
                        },
                    },
                },
                "cost": {"expected_cost_bps": 9.5},
            }
        }

        vector = _extract_vector_from_decision(event)

        self.assertEqual(vector["swing_momentum_bps"], 4.5)
        self.assertEqual(vector["breakout_up_bps"], 5.8)
        self.assertEqual(vector["swing_state_micro_valley_rebound"], 1.0)
        self.assertEqual(vector["continuation_state_early_liftoff"], 1.0)
        self.assertEqual(vector["breakout_state_up"], 1.0)

    def test_continuation_edge_respects_strategy_setting(self) -> None:
        features = {
            "context_range_pos": 0.18,
            "structure_range_pos": 0.25,
            "context_rebound_bps": 88.0,
            "spread_bps": 5.0,
            "volume_z": 0.2,
            "return_bps": 3.0,
            "trend_return_bps": -120.0,
            "structure_slope_short_bps": 3.6,
            "structure_slope_medium_bps": 1.18,
            "structure_rebound_bps": 94.0,
            "structure_drawdown_from_peak_bps": 140.0,
        }

        edge_ok, reason_ok = strategy_edge_for_features("continuation", features, 1.15)
        edge_blocked, reason_blocked = strategy_edge_for_features("continuation", features, 1.25)

        self.assertGreater(edge_ok, 0.0)
        self.assertEqual(reason_ok, "")
        self.assertEqual(edge_blocked, 0.0)
        self.assertEqual(reason_blocked, "continuation_slope_medium")

    def test_continuation_edge_allows_early_liftoff_before_staircase_fallback(self) -> None:
        features = {
            "context_range_pos": 0.22,
            "structure_range_pos": 0.44,
            "context_rebound_bps": 72.0,
            "spread_bps": 6.0,
            "volume_z": -0.2,
            "return_bps": 4.0,
            "trend_return_bps": -120.0,
            "structure_slope_short_bps": 1.2,
            "structure_slope_medium_bps": 1.02,
            "structure_slope_long_bps": 0.05,
            "structure_rebound_bps": 82.0,
            "structure_drawdown_from_peak_bps": 44.0,
            "up_structure": 1.0,
            "down_structure": 0.0,
        }
        values = {
            "cont_min_volume_z": -1.2,
            "cont_max_structure_range_pos": 1.0,
            "cont_range_continuation_max_range_pos": 1.0,
            "cont_staircase_min_slope_medium_bps": 0.95,
            "cont_staircase_min_slope_long_bps": 0.18,
            "cont_staircase_min_trend_bps": 28.0,
            "cont_staircase_min_ret_bps": -10.0,
            "cont_staircase_min_volume_z": -1.0,
            "cont_staircase_max_context_range_pos": 0.99,
            "cont_staircase_max_spread_bps": 22.0,
            "cont_early_liftoff_max_context_range_pos": 0.58,
            "cont_early_liftoff_max_structure_range_pos": 0.82,
            "cont_early_liftoff_min_context_rebound_bps": 28.0,
            "cont_early_liftoff_max_spread_bps": 18.0,
            "cont_early_liftoff_min_slope_short_bps": 0.9,
            "cont_early_liftoff_min_slope_medium_bps": 0.95,
            "cont_early_liftoff_min_drawdown_from_peak_bps": 18.0,
            "cont_early_liftoff_min_trend_bps": -320.0,
        }

        edge, reason = strategy_edge_for_features(
            "continuation",
            features,
            0.95,
            values=values,
            decision_context={"phase": "range"},
        )

        self.assertGreater(edge, 0.0)
        self.assertEqual(reason, "")

    def test_rebound_edge_respects_configured_range_floor(self) -> None:
        features = {
            "context_rebound_bps": 84.0,
            "context_range_pos": 0.58,
            "structure_range_pos": 0.60,
            "spread_bps": 10.0,
            "volume_z": -0.4,
            "trend_return_bps": -90.0,
            "return_bps": 5.0,
            "structure_rebound_bps": 52.0,
            "structure_extension_bps": 34.0,
            "structure_drawdown_from_peak_bps": 28.0,
            "atr_bps": 11.0,
        }
        strict_values = {
            "swing_min_range_bps": 52.0,
            "swing_micro_rebound_min_context_rebound_bps": 70.0,
            "swing_micro_rebound_max_context_range_pos": 0.62,
            "swing_micro_rebound_min_ret_bps": -2.0,
            "rebound_max_structure_range_pos": 0.62,
            "rebound_max_spread_bps": 14.0,
            "rebound_min_volume_z": -1.25,
            "rebound_min_trend_bps": -300.0,
        }
        soft_values = dict(strict_values)
        soft_values["swing_min_range_bps"] = 32.0
        soft_values["swing_micro_rebound_max_context_range_pos"] = 0.68
        soft_values["rebound_max_structure_range_pos"] = 0.68

        edge_ok, reason_ok = strategy_edge_for_features(
            "rebound",
            features,
            0.6,
            values=soft_values,
            decision_context={"phase": "bottom"},
        )
        edge_blocked, reason_blocked = strategy_edge_for_features(
            "rebound",
            features,
            0.6,
            values=strict_values,
            decision_context={"phase": "bottom"},
        )

        self.assertGreater(edge_ok, 0.0)
        self.assertEqual(reason_ok, "")
        self.assertEqual(edge_blocked, 0.0)
        self.assertEqual(reason_blocked, "rebound_range_too_small")

    def test_rebound_edge_waits_for_green_when_micro_rebound_is_already_extended(self) -> None:
        features = {
            "context_rebound_bps": 193.0,
            "context_range_pos": 0.20,
            "structure_range_pos": 0.20,
            "spread_bps": 7.7,
            "volume_z": -0.2,
            "trend_return_bps": 90.0,
            "return_bps": -3.8,
            "structure_rebound_bps": 55.0,
            "atr_bps": 15.0,
        }
        values = {
            "swing_min_range_bps": 40.0,
            "swing_micro_rebound_min_context_rebound_bps": 120.0,
            "swing_micro_rebound_max_context_range_pos": 0.92,
            "swing_micro_rebound_max_spread_bps": 18.0,
            "swing_micro_rebound_min_ret_bps": -18.0,
            "swing_micro_rebound_confirm_rebound_bps": 180.0,
            "swing_micro_rebound_confirm_min_ret_bps": 0.0,
            "rebound_max_structure_range_pos": 0.92,
            "rebound_min_volume_z": -1.25,
            "rebound_min_trend_bps": -300.0,
        }

        edge, reason = strategy_edge_for_features(
            "rebound",
            features,
            0.0,
            values=values,
            decision_context={"phase": "bottom"},
        )

        self.assertEqual(edge, 0.0)
        self.assertEqual(reason, "rebound_wait_green")

    def test_staircase_edge_respects_configured_thresholds(self) -> None:
        features = {
            "context_range_pos": 0.965,
            "spread_bps": 6.0,
            "volume_z": 0.1,
            "return_bps": -3.0,
            "trend_return_bps": 48.0,
            "structure_slope_short_bps": 1.2,
            "structure_slope_medium_bps": 1.0,
            "structure_slope_long_bps": 0.22,
            "structure_drawdown_from_peak_bps": 10.0,
            "up_structure": 1.0,
            "down_structure": 0.0,
        }
        soft_values = {
            "cont_staircase_min_trend_bps": 28.0,
            "cont_staircase_min_ret_bps": -10.0,
            "cont_staircase_min_volume_z": -1.0,
            "cont_staircase_min_slope_medium_bps": 0.95,
            "cont_staircase_min_slope_long_bps": 0.18,
            "cont_staircase_max_drawdown_from_peak_bps": 90.0,
            "cont_staircase_max_context_range_pos": 0.99,
            "cont_staircase_max_spread_bps": 22.0,
            "cont_staircase_require_up_structure": 0.0,
        }
        strict_values = dict(soft_values)
        strict_values["cont_staircase_min_slope_medium_bps"] = 1.05

        edge_ok, reason_ok = strategy_edge_for_features(
            "staircase",
            features,
            0.0,
            values=soft_values,
            decision_context={"phase": "range"},
        )
        edge_blocked, reason_blocked = strategy_edge_for_features(
            "staircase",
            features,
            0.0,
            values=strict_values,
            decision_context={"phase": "range"},
        )

        self.assertGreater(edge_ok, 0.0)
        self.assertEqual(reason_ok, "")
        self.assertEqual(edge_blocked, 0.0)
        self.assertEqual(reason_blocked, "staircase_slope_medium_floor")

    def test_breakout_edge_respects_trigger(self) -> None:
        features = {
            "breakout_up_bps": 5.6,
            "spread_bps": 3.0,
            "return_bps": 6.0,
            "structure_extension_bps": 48.0,
            "trend_return_bps": 160.0,
        }

        edge_ok, reason_ok = strategy_edge_for_features("breakout", features, 5.5)
        edge_blocked, reason_blocked = strategy_edge_for_features("breakout", features, 6.0)

        self.assertGreater(edge_ok, 0.0)
        self.assertEqual(reason_ok, "")
        self.assertEqual(edge_blocked, 0.0)
        self.assertEqual(reason_blocked, "breakout_trigger")

    def test_breakout_edge_blocks_late_rebound_without_volume(self) -> None:
        features = {
            "breakout_up_bps": 21.1337,
            "context_range_pos": 0.7353,
            "context_rebound_bps": 1720.57,
            "volume_z": -0.156,
            "spread_bps": 8.45,
        }
        values = {
            "breakout_late_rebound_block_context_range_pos": 0.72,
            "breakout_late_rebound_block_context_rebound_bps": 1400.0,
            "breakout_late_rebound_block_min_volume_z": 0.35,
        }

        edge, reason = strategy_edge_for_features("breakout", features, 7.0, values=values)

        self.assertEqual(edge, 0.0)
        self.assertEqual(reason, "breakout_late_rebound_block")

    def test_breakout_edge_keeps_late_rebound_with_volume_confirmation(self) -> None:
        features = {
            "breakout_up_bps": 21.1337,
            "context_range_pos": 0.7353,
            "context_rebound_bps": 1720.57,
            "volume_z": 0.7,
            "spread_bps": 8.45,
        }
        values = {
            "breakout_late_rebound_block_context_range_pos": 0.72,
            "breakout_late_rebound_block_context_rebound_bps": 1400.0,
            "breakout_late_rebound_block_min_volume_z": 0.35,
        }

        edge, reason = strategy_edge_for_features("breakout", features, 7.0, values=values)

        self.assertGreater(edge, 0.0)
        self.assertEqual(reason, "")

    def test_breakout_edge_blocks_mid_rebound_without_volume(self) -> None:
        features = {
            "breakout_up_bps": 21.1337,
            "context_range_pos": 0.514,
            "context_rebound_bps": 880.47,
            "volume_z": -0.25,
            "spread_bps": 8.45,
        }
        values = {
            "breakout_mid_rebound_block_context_range_pos": 0.48,
            "breakout_mid_rebound_block_context_rebound_bps": 760.0,
            "breakout_mid_rebound_block_min_volume_z": 0.0,
        }

        edge, reason = strategy_edge_for_features("breakout", features, 7.0, values=values)

        self.assertEqual(edge, 0.0)
        self.assertEqual(reason, "breakout_mid_rebound_block")

    def test_breakout_edge_keeps_mid_rebound_with_volume_confirmation(self) -> None:
        features = {
            "breakout_up_bps": 21.1337,
            "context_range_pos": 0.514,
            "context_rebound_bps": 880.47,
            "volume_z": 0.2,
            "spread_bps": 8.45,
        }
        values = {
            "breakout_mid_rebound_block_context_range_pos": 0.48,
            "breakout_mid_rebound_block_context_rebound_bps": 760.0,
            "breakout_mid_rebound_block_min_volume_z": 0.0,
        }

        edge, reason = strategy_edge_for_features("breakout", features, 7.0, values=values)

        self.assertGreater(edge, 0.0)
        self.assertEqual(reason, "")

    def test_technical_issue_classes_mark_execution_and_mixed_age(self) -> None:
        sample = TradeSample(
            symbol="PLUME",
            entry_ts="2026-03-17T02:03:13+00:00",
            exit_ts="2026-03-17T02:53:30+00:00",
            buy_qty=1.0,
            sell_qty=1.0,
            buy_notional=10.0,
            sell_notional=9.8,
            fees=0.02,
            dust_notional=0.0,
            net_pnl=-0.22,
            profitable=False,
            exit_reason="time_break_even_floor",
            strategy_at_entry="breakout",
            strategy_scores={"breakout": 1.0},
            features={"return_bps": 5.0},
            mirror_verified=True,
            mirror_match_mode="time_qty_pnl",
            entry_fill_count=2,
            entry_fill_span_minutes=48.0,
            technical_flags=["exec_sell_qty_clamped_to_balance", "account_sync_delta"],
        )

        self.assertEqual(
            _technical_issue_classes(sample),
            ["execution_reconcile_error", "mixed_age_exit"],
        )

    def test_staircase_strategy_trials_include_tradeability_bundles(self) -> None:
        spec = StrategyLabSpec(
            name="staircase",
            env_key="ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS",
            runtime_path="alpha.continuation.staircase_min_drawdown_from_peak_bps",
            current_live_value=0.0,
            candidate_values=[0.0, 1.0, 2.0],
        )

        trials = _strategy_trials(spec)
        names = {trial.name for trial in trials}
        trial_map = {trial.name: trial for trial in trials}

        self.assertIn("staircase_tradeability_soft", names)
        self.assertIn("staircase_tradeability_open", names)
        self.assertIn("staircase_tradeability_force", names)
        self.assertEqual(
            trial_map["staircase_1.0"].overrides["cont_staircase_min_drawdown_from_peak_bps"],
            1.0,
        )

    def test_continuation_strategy_trials_include_tradeability_bundles(self) -> None:
        spec = StrategyLabSpec(
            name="continuation",
            env_key="ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS",
            runtime_path="alpha.continuation.staircase_min_slope_medium_bps",
            current_live_value=1.15,
            candidate_values=[1.0, 1.15, 1.2],
        )

        names = {trial.name for trial in _strategy_trials(spec)}
        trial_map = {trial.name: trial for trial in _strategy_trials(spec)}

        self.assertIn("continuation_tradeability_soft", names)
        self.assertIn("continuation_tradeability_open", names)
        self.assertIn("continuation_tradeability_force", names)
        self.assertEqual(
            trial_map["continuation_1.0"].overrides["cont_staircase_min_slope_medium_bps"],
            1.0,
        )
        self.assertNotIn("entry_edge_bps", trial_map["continuation_tradeability_open"].overrides)
        self.assertNotIn("cont_staircase_min_trend_bps", trial_map["continuation_tradeability_open"].overrides)

    def test_run_strategy_labs_filters_to_mirror_verified_trade_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            catalog_path = root / "catalog.yaml"
            output_dir = root / "reports"
            catalog_path.write_text(
                yaml.safe_dump(
                    {
                        "strategies": {
                            "rebound": {
                                "primary_setting": {
                                    "env": "ROTATION_SWING_REVERSAL_THRESHOLD_BPS",
                                    "runtime_path": "alpha.swing.reversal_threshold_bps",
                                    "current_live_value": 0.6,
                                }
                            },
                            "staircase": {
                                "primary_setting": {
                                    "env": "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS",
                                    "runtime_path": "alpha.continuation.staircase_min_drawdown_from_peak_bps",
                                    "current_live_value": 0.0,
                                }
                            },
                            "continuation": {
                                "primary_setting": {
                                    "env": "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS",
                                    "runtime_path": "alpha.continuation.staircase_min_slope_medium_bps",
                                    "current_live_value": 1.15,
                                }
                            },
                            "breakout": {
                                "primary_setting": {
                                    "env": "ROTATION_BREAKOUT_TRIGGER_BPS",
                                    "runtime_path": "alpha.breakout.trigger_bps",
                                    "current_live_value": 6.0,
                                }
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            baseline_policy = run_strategy_labs.__globals__["replay_lab"].PolicyCandidate(
                name="baseline",
                profile="scalp_guarded_open",
                risk_mode="normal",
                values={
                    "entry_edge_bps": 4.0,
                    "entry_cost_buffer_bps": 0.0,
                    "entry_cost_coverage_ratio": 0.55,
                    "entry_cost_roundtrip_multiplier": 1.0,
                    "entry_min_atr_to_cost_ratio": 0.0,
                    "gate_cost_coverage_ratio": 0.40,
                    "gate_cost_roundtrip_multiplier": 1.0,
                    "late_entry_block_context_range_pos": 0.90,
                    "late_entry_block_structure_range_pos": 0.95,
                    "late_entry_block_max_context_drawdown_bps": 0.0,
                    "late_entry_block_min_trend_return_bps": 0.0,
                    "late_entry_block_min_return_bps": 0.0,
                    "reentry_cooldown_bars_after_weak_exit": 0.0,
                    "reentry_min_move_bps": 0.0,
                    "cont_staircase_min_slope_medium_bps": 1.15,
                    "md_interval_seconds": 60.0,
                },
                source="test",
            )
            verified = TradeSample(
                symbol="RENDER",
                entry_ts="2026-03-15T09:00:00+00:00",
                exit_ts="2026-03-15T09:15:00+00:00",
                buy_qty=1.0,
                sell_qty=1.0,
                buy_notional=10.0,
                sell_notional=10.3,
                fees=0.02,
                dust_notional=0.0,
                net_pnl=0.28,
                profitable=True,
                exit_reason="trailing_stop",
                strategy_at_entry="continuation",
                strategy_scores={"continuation": 1.0},
                features={
                    "context_range_pos": 0.18,
                    "structure_range_pos": 0.25,
                    "context_rebound_bps": 88.0,
                    "spread_bps": 5.0,
                    "volume_z": 0.2,
                    "return_bps": 3.0,
                    "trend_return_bps": -120.0,
                    "structure_slope_short_bps": 3.6,
                    "structure_slope_medium_bps": 1.18,
                    "structure_rebound_bps": 94.0,
                    "structure_drawdown_from_peak_bps": 140.0,
                    "expected_cost_bps": 7.0,
                    "edge_bps_effective": 14.0,
                },
                mirror_verified=True,
                mirror_match_mode="time_qty_pnl",
            )
            unverified = TradeSample(
                symbol="RENDER",
                entry_ts="2026-03-15T10:00:00+00:00",
                exit_ts="2026-03-15T10:08:00+00:00",
                buy_qty=1.0,
                sell_qty=1.0,
                buy_notional=10.0,
                sell_notional=9.7,
                fees=0.02,
                dust_notional=0.0,
                net_pnl=-0.32,
                profitable=False,
                exit_reason="failed_start_exit",
                strategy_at_entry="continuation",
                strategy_scores={"continuation": 1.0},
                features=dict(verified.features),
                mirror_verified=False,
                mirror_match_mode="",
            )

            with (
                patch(
                    "trading.meta.rotation_strategy_lab.extract_trade_samples",
                    return_value=[verified, unverified],
                ),
                patch(
                    "trading.meta.rotation_strategy_lab.extract_counterfactual_samples",
                    return_value=[],
                ),
                patch(
                    "trading.meta.rotation_strategy_lab.replay_lab._load_live_policy",
                    return_value=baseline_policy,
                ),
            ):
                run_strategy_labs(
                    log_dir=root,
                    catalog_file=catalog_path,
                    active_file=root / "active.json",
                    runtime_env_file=root / "runtime.env",
                    output_dir=output_dir,
                    lookback_hours=72.0,
                    strategy_filter="continuation",
                )

            payload = json.loads((output_dir / "continuation.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["sample_window"]["all_trade_count"], 2)
            self.assertEqual(payload["sample_window"]["trade_count"], 1)
            self.assertEqual(payload["sample_window"]["trade_verification"]["verified_trade_count"], 1)
            self.assertEqual(payload["sample_window"]["trade_verification"]["unmatched_trade_count"], 1)

    def test_run_strategy_labs_excludes_technical_trade_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            catalog_path = root / "catalog.yaml"
            output_dir = root / "reports"
            catalog_path.write_text(
                yaml.safe_dump(
                    {
                        "strategies": {
                            "rebound": {
                                "primary_setting": {
                                    "env": "ROTATION_SWING_REVERSAL_THRESHOLD_BPS",
                                    "runtime_path": "alpha.swing.reversal_threshold_bps",
                                    "current_live_value": 0.6,
                                }
                            },
                            "staircase": {
                                "primary_setting": {
                                    "env": "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS",
                                    "runtime_path": "alpha.continuation.staircase_min_drawdown_from_peak_bps",
                                    "current_live_value": 0.0,
                                }
                            },
                            "continuation": {
                                "primary_setting": {
                                    "env": "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS",
                                    "runtime_path": "alpha.continuation.staircase.min_slope_medium_bps",
                                    "current_live_value": 1.15,
                                }
                            },
                            "breakout": {
                                "primary_setting": {
                                    "env": "ROTATION_BREAKOUT_TRIGGER_BPS",
                                    "runtime_path": "alpha.breakout.trigger_bps",
                                    "current_live_value": 6.0,
                                }
                            },
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            baseline_policy = run_strategy_labs.__globals__["replay_lab"].PolicyCandidate(
                name="baseline",
                profile="scalp_guarded_open",
                risk_mode="normal",
                values={
                    "entry_edge_bps": 4.0,
                    "entry_cost_buffer_bps": 0.0,
                    "entry_cost_coverage_ratio": 0.55,
                    "entry_cost_roundtrip_multiplier": 1.0,
                    "entry_min_atr_to_cost_ratio": 0.0,
                    "gate_cost_coverage_ratio": 0.40,
                    "gate_cost_roundtrip_multiplier": 1.0,
                    "reentry_cooldown_bars_after_weak_exit": 0.0,
                    "reentry_min_move_bps": 0.0,
                    "breakout_trigger_bps": 6.0,
                    "md_interval_seconds": 60.0,
                },
                source="test",
            )
            clean_trade = TradeSample(
                symbol="FET",
                entry_ts="2026-03-15T09:00:00+00:00",
                exit_ts="2026-03-15T09:15:00+00:00",
                buy_qty=1.0,
                sell_qty=1.0,
                buy_notional=10.0,
                sell_notional=10.3,
                fees=0.02,
                dust_notional=0.0,
                net_pnl=0.28,
                profitable=True,
                exit_reason="trailing_stop",
                strategy_at_entry="breakout",
                strategy_scores={"breakout": 1.0},
                features={
                    "breakout_up_bps": 8.0,
                    "spread_bps": 5.0,
                    "return_bps": 12.0,
                    "trend_return_bps": 120.0,
                    "expected_cost_bps": 6.0,
                    "edge_bps_effective": 16.0,
                },
                mirror_verified=True,
                mirror_match_mode="time_qty_pnl",
            )
            technical_trade = TradeSample(
                symbol="PLUME",
                entry_ts="2026-03-15T10:00:00+00:00",
                exit_ts="2026-03-15T10:05:00+00:00",
                buy_qty=1.0,
                sell_qty=1.0,
                buy_notional=10.0,
                sell_notional=9.8,
                fees=0.02,
                dust_notional=0.0,
                net_pnl=-0.22,
                profitable=False,
                exit_reason="failed_start_exit",
                strategy_at_entry="breakout",
                strategy_scores={"breakout": 1.0},
                features=dict(clean_trade.features),
                mirror_verified=True,
                mirror_match_mode="time_qty_pnl",
                technical_flags=["exec_sell_qty_clamped_to_balance"],
            )

            with (
                patch(
                    "trading.meta.rotation_strategy_lab.extract_trade_samples",
                    return_value=[clean_trade, technical_trade],
                ),
                patch(
                    "trading.meta.rotation_strategy_lab.extract_counterfactual_samples",
                    return_value=[],
                ),
                patch(
                    "trading.meta.rotation_strategy_lab.replay_lab._load_live_policy",
                    return_value=baseline_policy,
                ),
            ):
                run_strategy_labs(
                    log_dir=root,
                    catalog_file=catalog_path,
                    active_file=root / "active.json",
                    runtime_env_file=root / "runtime.env",
                    output_dir=output_dir,
                    lookback_hours=72.0,
                    strategy_filter="breakout",
                )

            payload = json.loads((output_dir / "breakout.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["sample_window"]["all_trade_count"], 2)
            self.assertEqual(payload["sample_window"]["trade_count"], 1)
            self.assertEqual(payload["sample_window"]["technical_excluded_trade_count"], 1)
            self.assertEqual(
                payload["sample_window"]["technical_exclusion_breakdown"],
                {"execution_reconcile_error": 1},
            )

    def test_rebound_strategy_trials_include_tradeability_bundles(self) -> None:
        spec = StrategyLabSpec(
            name="rebound",
            env_key="ROTATION_SWING_REVERSAL_THRESHOLD_BPS",
            runtime_path="alpha.swing.reversal_threshold_bps",
            current_live_value=0.6,
            candidate_values=[0.4, 0.6, 0.8],
        )

        names = {trial.name for trial in _strategy_trials(spec)}
        trial_map = {trial.name: trial for trial in _strategy_trials(spec)}

        self.assertIn("rebound_tradeability_soft", names)
        self.assertIn("rebound_tradeability_open", names)
        self.assertIn("rebound_tradeability_force", names)
        self.assertEqual(
            trial_map["rebound_0.4"].overrides["swing_reversal_threshold_bps"],
            0.4,
        )
        self.assertEqual(trial_map["rebound_tradeability_soft"].overrides["swing_min_range_bps"], 48.0)
        self.assertEqual(
            trial_map["rebound_tradeability_open"].overrides["swing_micro_rebound_min_context_rebound_bps"],
            200.0,
        )

    def test_breakout_strategy_trials_include_tradeability_bundles(self) -> None:
        spec = StrategyLabSpec(
            name="breakout",
            env_key="ROTATION_BREAKOUT_TRIGGER_BPS",
            runtime_path="alpha.breakout.trigger_bps",
            current_live_value=6.0,
            candidate_values=[5.5, 6.0, 7.0],
        )

        names = {trial.name for trial in _strategy_trials(spec)}
        trial_map = {trial.name: trial for trial in _strategy_trials(spec)}

        self.assertIn("breakout_exit_relief_soft", names)
        self.assertIn("breakout_exit_relief_open", names)
        self.assertIn("breakout_exit_relief_force", names)
        self.assertEqual(trial_map["breakout_5.5"].overrides["breakout_trigger_bps"], 5.5)
        self.assertEqual(
            trial_map["breakout_exit_relief_soft"].overrides["late_entry_block_context_range_pos"],
            0.75,
        )
        self.assertEqual(trial_map["breakout_exit_relief_soft"].overrides["hard_stop_loss_bps"], 98.0)
        self.assertEqual(trial_map["breakout_exit_relief_open"].overrides["campaign_hold_enabled"], 1.0)

    def test_run_strategy_labs_writes_strategy_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            catalog_path = root / "catalog.yaml"
            output_dir = root / "reports"
            catalog_path.write_text(
                yaml.safe_dump(
                    {
                        "strategies": {
                            "rebound": {
                                "primary_setting": {
                                    "env": "ROTATION_SWING_REVERSAL_THRESHOLD_BPS",
                                    "runtime_path": "alpha.auto.swing.reversal_threshold_bps",
                                    "current_live_value": 0.0,
                                }
                            },
                            "staircase": {
                                "primary_setting": {
                                    "env": "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS",
                                    "runtime_path": "alpha.continuation.staircase_min_drawdown_from_peak_bps",
                                    "current_live_value": 0.0,
                                }
                            },
                            "continuation": {
                                "primary_setting": {
                                    "env": "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS",
                                    "runtime_path": "alpha.continuation.staircase_min_slope_medium_bps",
                                    "current_live_value": 1.2,
                                    "next_candidate_value": 1.15,
                                }
                            },
                            "breakout": {
                                "primary_setting": {
                                    "env": "ROTATION_BREAKOUT_TRIGGER_BPS",
                                    "runtime_path": "alpha.breakout.trigger_bps",
                                    "current_live_value": 6.0,
                                }
                            },
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            sample = CounterfactualSample(
                symbol="RENDER",
                decision_ts="2026-03-15T10:00:00+00:00",
                anchor_price=1.0,
                block_reason="edge_below_costs",
                gate_reason="edge_below_costs",
                risk_reason="gate_block",
                strategy_primary="continuation",
                features={
                    "context_range_pos": 0.18,
                    "structure_range_pos": 0.22,
                    "context_rebound_bps": 92.0,
                    "spread_bps": 4.0,
                    "volume_z": 0.0,
                    "return_bps": 4.0,
                    "trend_return_bps": -140.0,
                    "structure_slope_short_bps": 3.8,
                    "structure_slope_medium_bps": 1.16,
                    "structure_rebound_bps": 96.0,
                    "structure_drawdown_from_peak_bps": 150.0,
                    "expected_cost_bps": 8.0,
                    "edge_bps_effective": 0.0,
                },
                decision_context={
                    "expected_cost_bps": 8.0,
                    "gate_allow": False,
                    "gate_reason": "edge_below_costs",
                },
                post_decision_metrics={
                    "post_decision_bars_15m": 15,
                    "post_decision_close_15m_bps": 28.0,
                    "post_decision_close_30m_bps": 35.0,
                    "post_decision_close_60m_bps": 46.0,
                    "post_decision_mfe_15m_bps": 34.0,
                    "post_decision_mfe_30m_bps": 44.0,
                    "post_decision_mfe_60m_bps": 52.0,
                    "post_decision_mae_15m_bps": -6.0,
                    "post_decision_mae_30m_bps": -9.0,
                },
            )

            baseline_policy = run_strategy_labs.__globals__["replay_lab"].PolicyCandidate(
                name="baseline",
                profile="scalp_guarded_open",
                risk_mode="normal",
                values={
                    "entry_edge_bps": 6.0,
                    "entry_cost_buffer_bps": 0.0,
                    "entry_cost_coverage_ratio": 0.55,
                    "entry_cost_roundtrip_multiplier": 1.0,
                    "entry_min_atr_to_cost_ratio": 0.0,
                    "gate_cost_coverage_ratio": 0.40,
                    "gate_cost_roundtrip_multiplier": 1.0,
                    "late_entry_block_context_range_pos": 0.90,
                    "late_entry_block_structure_range_pos": 0.95,
                    "late_entry_block_max_context_drawdown_bps": 0.0,
                    "late_entry_block_min_trend_return_bps": 0.0,
                    "late_entry_block_min_return_bps": 0.0,
                    "reentry_cooldown_bars_after_weak_exit": 0.0,
                    "reentry_min_move_bps": 0.0,
                    "cont_staircase_min_slope_medium_bps": 0.95,
                    "md_interval_seconds": 60.0,
                },
                source="test",
            )

            with (
                patch(
                    "trading.meta.rotation_strategy_lab.extract_trade_samples",
                    return_value=[],
                ),
                patch(
                    "trading.meta.rotation_strategy_lab.extract_counterfactual_samples",
                    return_value=[sample],
                ),
                patch(
                    "trading.meta.rotation_strategy_lab.replay_lab._load_live_policy",
                    return_value=baseline_policy,
                ),
            ):
                summary = run_strategy_labs(
                    log_dir=root,
                    catalog_file=catalog_path,
                    active_file=root / "active.json",
                    runtime_env_file=root / "runtime.env",
                    output_dir=output_dir,
                    lookback_hours=72.0,
                    strategy_filter="continuation",
                )

            report_path = output_dir / "continuation.json"
            self.assertTrue(report_path.exists())
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["current_live_value"], 0.95)
            self.assertEqual(payload["baseline"]["setting_value"], 0.95)
            self.assertTrue(payload["recommended"]["candidate_name"])
            self.assertTrue(payload["recommended"]["accepted"])
            self.assertGreaterEqual(payload["recommended"]["total_trade_count"], 1)
            self.assertGreaterEqual(len(payload["recommended"]["parameter_overrides"]), 0)
            self.assertEqual(summary["strategies"][0]["strategy"], "continuation")


if __name__ == "__main__":
    unittest.main()
