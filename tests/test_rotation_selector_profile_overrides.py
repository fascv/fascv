from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import scripts.rotation_auto_coin_selector as rotation_auto_coin_selector
import yaml
from scripts.rotation_auto_coin_selector import _profile_values


class TestRotationSelectorProfileOverrides(unittest.TestCase):
    def test_recent_live_decision_feedback_ignores_pause_disable_reason(self) -> None:
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            rotation_auto_coin_selector,
            "_load_recent_core_decisions",
            return_value=[
                {
                    "ts": now,
                    "trading_enabled": False,
                    "trading_disable_reason": "pause",
                    "gate_reason": "",
                    "risk_reason": "",
                    "edge_bps": 0.0,
                    "expected_cost_bps": 0.0,
                    "alpha_type": "continuation",
                    "alpha_meta": {},
                }
            ],
        ):
            feedback = rotation_auto_coin_selector._recent_live_decision_feedback("TRX")

        self.assertFalse(feedback["recent_live_selection_block"])
        self.assertEqual(feedback["recent_live_selection_block_reason"], "")

    def test_recent_live_decision_feedback_ignores_pause_gate_blocks(self) -> None:
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            rotation_auto_coin_selector,
            "_load_recent_core_decisions",
            return_value=[
                {
                    "ts": now,
                    "trading_enabled": False,
                    "trading_disable_reason": "pause",
                    "gate_reason": "edge_below_costs",
                    "risk_reason": "gate_block",
                    "edge_bps": 0.0,
                    "expected_cost_bps": 0.0,
                    "alpha_type": "continuation",
                    "alpha_meta": {},
                },
                {
                    "ts": now,
                    "trading_enabled": False,
                    "trading_disable_reason": "pause",
                    "gate_reason": "edge_below_costs",
                    "risk_reason": "gate_block",
                    "edge_bps": 0.0,
                    "expected_cost_bps": 0.0,
                    "alpha_type": "continuation",
                    "alpha_meta": {},
                },
            ],
        ):
            feedback = rotation_auto_coin_selector._recent_live_decision_feedback("TRX")

        self.assertFalse(feedback["recent_live_selection_block"])
        self.assertEqual(feedback["recent_live_selection_block_reason"], "")

    def test_recent_live_decision_feedback_keeps_enabled_gate_blocks(self) -> None:
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            rotation_auto_coin_selector,
            "_load_recent_core_decisions",
            return_value=[
                {
                    "ts": now,
                    "trading_enabled": True,
                    "trading_disable_reason": "",
                    "gate_reason": "edge_below_costs",
                    "risk_reason": "gate_block",
                    "edge_bps": 0.0,
                    "expected_cost_bps": 0.0,
                    "alpha_type": "continuation",
                    "alpha_meta": {},
                },
                {
                    "ts": now,
                    "trading_enabled": True,
                    "trading_disable_reason": "",
                    "gate_reason": "edge_below_costs",
                    "risk_reason": "gate_block",
                    "edge_bps": 0.0,
                    "expected_cost_bps": 0.0,
                    "alpha_type": "continuation",
                    "alpha_meta": {},
                },
            ],
        ):
            feedback = rotation_auto_coin_selector._recent_live_decision_feedback("TRX")

        self.assertTrue(feedback["recent_live_selection_block"])
        self.assertEqual(feedback["recent_live_selection_block_reason"], "edge_below_costs")

    def test_recent_live_decision_feedback_keeps_hard_disable_reason(self) -> None:
        now = datetime.now(timezone.utc)
        with mock.patch.object(
            rotation_auto_coin_selector,
            "_load_recent_core_decisions",
            return_value=[
                {
                    "ts": now,
                    "trading_enabled": False,
                    "trading_disable_reason": "daily_loss_limit",
                    "gate_reason": "",
                    "risk_reason": "",
                    "edge_bps": 0.0,
                    "expected_cost_bps": 0.0,
                    "alpha_type": "continuation",
                    "alpha_meta": {},
                }
            ],
        ):
            feedback = rotation_auto_coin_selector._recent_live_decision_feedback("TRX")

        self.assertTrue(feedback["recent_live_selection_block"])
        self.assertEqual(feedback["recent_live_selection_block_reason"], "daily_loss_limit")

    def test_recent_live_selection_block_breaks_sticky_retention_in_favor_of_fresh_candidate(self) -> None:
        rows = [
            {
                "symbol": "TRX",
                "score": 180.0,
                "keep_open": False,
                "eligible": False,
                "pos_pct": 42.0,
                "spread_bps": 3.4,
                "strategy_tags": ["staircase"],
                "strategy_scores": {"staircase": 92.0},
                "strategy_primary": "staircase",
                "strategy_meta_score": 92.0,
                "recent_live_selection_block": True,
                "recent_live_selection_block_reason": "edge_below_costs",
                "selected_active": False,
            },
            {
                "symbol": "ENA",
                "score": 175.0,
                "keep_open": False,
                "eligible": False,
                "pos_pct": 28.0,
                "spread_bps": 8.5,
                "strategy_tags": ["rebound"],
                "strategy_scores": {"rebound": 118.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 138.0,
                "recent_live_selection_block": False,
                "selected_active": False,
            }
        ]

        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=["TRX"],
            previous_selected_since={"TRX": "2026-03-14T20:30:00+00:00"},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=4.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 14, 20, 33, tzinfo=timezone.utc),
        )

        self.assertEqual(selected, ["ENA"])
        self.assertEqual(selected_strategy_map, {"ENA": "rebound"})
        self.assertEqual(strategy_sequence, ["rebound"])
        self.assertEqual(rows[0]["selection_path"], "")
        self.assertEqual(rows[1]["selection_path"], "strategy_plan")

    def test_keep_open_bypasses_recent_live_selection_block(self) -> None:
        rows = [
            {
                "symbol": "TRX",
                "score": 180.0,
                "keep_open": True,
                "eligible": True,
                "pos_pct": 42.0,
                "spread_bps": 3.4,
                "strategy_tags": ["staircase"],
                "strategy_scores": {"staircase": 120.0},
                "strategy_primary": "staircase",
                "strategy_meta_score": 140.0,
                "recent_live_selection_block": True,
                "recent_live_selection_block_reason": "edge_below_costs",
            }
        ]

        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=["TRX"],
            previous_selected_since={"TRX": "2026-03-14T20:30:00+00:00"},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=4.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 14, 20, 33, tzinfo=timezone.utc),
        )

        self.assertEqual(selected, ["TRX"])
        self.assertEqual(selected_strategy_map, {"TRX": "staircase"})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["selection_path"], "keep_open")

    def test_choose_selected_blocks_persistent_downdrift_new_entry(self) -> None:
        rows = [
            {
                "symbol": "SAGA",
                "score": 320.0,
                "keep_open": False,
                "eligible": False,
                "gate_reason": "persistent_downdrift_strict",
                "persistent_downdrift_blocked": True,
                "pos_pct": 22.0,
                "spread_bps": 6.0,
                "strategy_tags": ["rebound"],
                "strategy_scores": {"rebound": 180.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 180.0,
                "selected_active": False,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["rebound"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 31, 20, 44, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, [])
        self.assertEqual(selected_strategy_map, {})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["selection_path"], "")

    def test_choose_selected_keeps_open_position_despite_persistent_downdrift(self) -> None:
        rows = [
            {
                "symbol": "SAGA",
                "score": -200.0,
                "keep_open": True,
                "eligible": True,
                "gate_reason": "keep_open",
                "persistent_downdrift_blocked": True,
                "pos_pct": 22.0,
                "spread_bps": 6.0,
                "strategy_tags": ["rebound"],
                "strategy_scores": {"rebound": 120.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 120.0,
                "selected_active": False,
            }
        ]

        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=["SAGA"],
            previous_selected_since={"SAGA": "2026-03-31T20:30:00+00:00"},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=4.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 31, 20, 44, tzinfo=timezone.utc),
        )

        self.assertEqual(selected, ["SAGA"])
        self.assertEqual(selected_strategy_map, {"SAGA": "rebound"})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["selection_path"], "keep_open")

    def test_choose_selected_blocks_non_falling_longtrend_new_entry(self) -> None:
        rows = [
            {
                "symbol": "ONDO",
                "score": 380.0,
                "keep_open": False,
                "eligible": False,
                "gate_reason": "non_falling_longtrend_ret180_below_min",
                "non_falling_longtrend_blocked": True,
                "pos_pct": 24.0,
                "spread_bps": 7.0,
                "strategy_tags": ["rebound"],
                "strategy_scores": {"rebound": 160.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 160.0,
                "selected_active": False,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["rebound"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 31, 20, 59, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, [])
        self.assertEqual(selected_strategy_map, {})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["selection_path"], "")

    def test_choose_selected_keeps_previous_strategy_on_same_symbol_without_clear_edge(self) -> None:
        rows = [
            {
                "symbol": "PEOPLE",
                "score": 210.0,
                "keep_open": False,
                "eligible": True,
                "pos_pct": 46.0,
                "spread_bps": 8.4,
                "strategy_tags": ["continuation", "rebound"],
                "strategy_scores": {"continuation": 112.0, "rebound": 118.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 118.0,
                "selected_active": False,
            }
        ]

        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=["PEOPLE"],
            previous_selected_since={"PEOPLE": "2026-03-16T20:29:00+00:00"},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=30.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 16, 20, 31, tzinfo=timezone.utc),
            previous_selected_strategy_map={"PEOPLE": "continuation"},
        )

        self.assertEqual(selected, ["PEOPLE"])
        self.assertEqual(selected_strategy_map, {"PEOPLE": "continuation"})
        self.assertEqual(strategy_sequence, ["continuation"])
        self.assertEqual(rows[0]["selection_path"], "sticky_retention")

    def test_choose_selected_switches_strategy_when_new_one_is_clearly_stronger(self) -> None:
        rows = [
            {
                "symbol": "PEOPLE",
                "score": 230.0,
                "keep_open": False,
                "eligible": True,
                "pos_pct": 46.0,
                "spread_bps": 8.4,
                "strategy_tags": ["continuation", "rebound"],
                "strategy_scores": {"continuation": 88.0, "rebound": 176.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 176.0,
                "selected_active": False,
            }
        ]

        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=["PEOPLE"],
            previous_selected_since={"PEOPLE": "2026-03-16T20:29:00+00:00"},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=30.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 16, 20, 31, tzinfo=timezone.utc),
            previous_selected_strategy_map={"PEOPLE": "continuation"},
        )

        self.assertEqual(selected, ["PEOPLE"])
        self.assertEqual(selected_strategy_map, {"PEOPLE": "rebound"})
        self.assertEqual(strategy_sequence, ["rebound"])
        self.assertEqual(rows[0]["selection_path"], "sticky_retention")

    def test_choose_selected_requires_extra_margin_for_alpha_family_switch(self) -> None:
        rows = [
            {
                "symbol": "AXS",
                "score": 260.0,
                "keep_open": False,
                "eligible": True,
                "pos_pct": 52.0,
                "spread_bps": 7.8,
                "strategy_tags": ["staircase", "breakout"],
                "strategy_scores": {"staircase": 110.0, "breakout": 138.0},
                "strategy_primary": "breakout",
                "strategy_meta_score": 138.0,
                "selected_active": False,
            }
        ]

        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=["AXS"],
            previous_selected_since={"AXS": "2026-03-16T20:29:00+00:00"},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=30.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 16, 20, 31, tzinfo=timezone.utc),
            previous_selected_strategy_map={"AXS": "staircase"},
        )

        self.assertEqual(selected, ["AXS"])
        self.assertEqual(selected_strategy_map, {"AXS": "staircase"})
        self.assertEqual(strategy_sequence, ["staircase"])

    def test_eligible_candidate_can_still_fill_slot_despite_recent_live_block(self) -> None:
        rows = [
            {
                "symbol": "FIL",
                "score": 300.0,
                "keep_open": False,
                "eligible": True,
                "pos_pct": 52.0,
                "spread_bps": 6.0,
                "strategy_tags": ["continuation"],
                "strategy_scores": {"continuation": 220.0},
                "strategy_primary": "continuation",
                "strategy_meta_score": 240.0,
                "recent_live_selection_block": True,
                "recent_live_selection_block_reason": "edge_below_costs",
                "selected_active": False,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["continuation"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 16, 20, 33, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ["FIL"])
        self.assertEqual(selected_strategy_map, {"FIL": "continuation"})
        self.assertEqual(strategy_sequence, ["continuation"])
        self.assertEqual(rows[0]["selection_path"], "strategy_plan")

    def test_strong_staircase_can_survive_base_score_floor(self) -> None:
        rows = [
            {
                "symbol": "ORCA",
                "score": -9600.0,
                "keep_open": False,
                "eligible": False,
                "net24_pct": -0.10,
                "macro_down_context": False,
                "macro_up_context": True,
                "spread_bps": 10.6,
                "pos_24h_pct": 73.2,
                "ret60_bps": 64.3,
                "ret120_bps": 96.7,
                "current_slope_bps_h": 15.5,
                "up_structure": True,
                "staircase_trend": False,
                "staircase_score": 111.6,
                "staircase_pullback_count": 1.0,
                "staircase_positive_share": 0.83,
                "staircase_max_pullback_run_bps": 10.7,
                "gate_reason": "no_valley_context",
            }
        ]

        rankings = rotation_auto_coin_selector._annotate_rows_with_strategy_views(
            rows, "scalp_guarded_open"
        )

        self.assertEqual(rows[0]["strategy_primary"], "staircase")
        self.assertIn("staircase", rows[0]["strategy_scores"])
        self.assertEqual(rows[0]["strategy_tags"], ["staircase"])
        self.assertEqual(len(rankings["staircase"]), 1)

    def test_choose_selected_skips_late_staircase_without_valley_context(self) -> None:
        rows = [
            {
                "symbol": "ATOM",
                "score": -9406.0,
                "keep_open": False,
                "eligible": False,
                "net24_pct": 7.2,
                "macro_down_context": False,
                "macro_up_context": False,
                "spread_bps": 5.0,
                "pos_pct": 98.6,
                "pos_24h_pct": 100.0,
                "ret60_bps": 20.1,
                "ret120_bps": 106.8,
                "current_slope_bps_h": 91.6,
                "up_structure": True,
                "fast_staircase": False,
                "staircase_trend": False,
                "staircase_score": 78.0,
                "staircase_pullback_count": 5.0,
                "staircase_positive_share": 0.67,
                "staircase_max_pullback_run_bps": 60.4,
                "gate_reason": "no_valley_context",
                "strategy_tags": ["staircase"],
                "strategy_scores": {"staircase": 176.0},
                "strategy_primary": "staircase",
                "strategy_meta_score": 731.0,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["staircase"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 16, 20, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, [])
        self.assertEqual(selected_strategy_map, {})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["selection_path"], "")

    def test_choose_selected_keeps_strong_staircase_exception(self) -> None:
        rows = [
            {
                "symbol": "ORCA",
                "score": -9600.0,
                "keep_open": False,
                "eligible": False,
                "net24_pct": -0.10,
                "macro_down_context": False,
                "macro_up_context": True,
                "spread_bps": 10.6,
                "pos_pct": 73.2,
                "pos_24h_pct": 73.2,
                "ret60_bps": 64.3,
                "ret120_bps": 96.7,
                "current_slope_bps_h": 15.5,
                "up_structure": True,
                "fast_staircase": False,
                "staircase_trend": False,
                "staircase_score": 111.6,
                "staircase_pullback_count": 1.0,
                "staircase_positive_share": 0.83,
                "staircase_max_pullback_run_bps": 10.7,
                "gate_reason": "no_valley_context",
                "strategy_tags": ["staircase"],
                "strategy_scores": {"staircase": 118.0},
                "strategy_primary": "staircase",
                "strategy_meta_score": 118.0,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["staircase"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 16, 20, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ["ORCA"])
        self.assertEqual(selected_strategy_map, {"ORCA": "staircase"})
        self.assertEqual(strategy_sequence, ["staircase"])
        self.assertEqual(rows[0]["selection_path"], "strategy_plan")

    def test_choose_selected_allows_continuation_despite_generic_valley_gate(self) -> None:
        rows = [
            {
                "symbol": "ZRO",
                "score": -9150.0,
                "keep_open": False,
                "eligible": False,
                "gate_reason": "no_valley_context",
                "pos_pct": 66.0,
                "pos_24h_pct": 66.0,
                "spread_bps": 7.0,
                "up_structure": False,
                "down_structure": False,
                "macro_down_context": False,
                "still_dumping": False,
                "rebound_in_downtrend": False,
                "countertrend_rebound": False,
                "score_trend": 165.0,
                "trend_ready": False,
                "strong_continuation_context": False,
                "staircase_trend": False,
                "strategy_tags": ["continuation"],
                "strategy_scores": {"continuation": 210.0},
                "strategy_primary": "continuation",
                "strategy_meta_score": 210.0,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["continuation"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 17, 19, 40, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ["ZRO"])
        self.assertEqual(selected_strategy_map, {"ZRO": "continuation"})
        self.assertEqual(strategy_sequence, ["continuation"])
        self.assertEqual(rows[0]["selection_path"], "strategy_plan")

    def test_choose_selected_allows_constructive_staircase_stall(self) -> None:
        rows = [
            {
                "symbol": "ZK",
                "score": -9400.0,
                "keep_open": False,
                "eligible": False,
                "gate_reason": "structure_stall",
                "pos_pct": 68.0,
                "pos_24h_pct": 68.0,
                "spread_bps": 8.0,
                "up_structure": False,
                "down_structure": False,
                "macro_down_context": False,
                "still_dumping": False,
                "staircase_trend": False,
                "fast_staircase": False,
                "staircase_score": 92.0,
                "staircase_pullback_count": 3.0,
                "staircase_positive_share": 0.66,
                "staircase_max_pullback_run_bps": 28.0,
                "strategy_tags": ["staircase"],
                "strategy_scores": {"staircase": 184.0},
                "strategy_primary": "staircase",
                "strategy_meta_score": 184.0,
            }
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["staircase"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 17, 19, 40, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ["ZK"])
        self.assertEqual(selected_strategy_map, {"ZK": "staircase"})
        self.assertEqual(strategy_sequence, ["staircase"])
        self.assertEqual(rows[0]["selection_path"], "strategy_plan")

    def test_staircase_candidate_accepts_constructive_true_structure_without_fast_flag(self) -> None:
        row = {
            "symbol": "TIA",
            "macro_down_context": False,
            "macro_up_context": True,
            "still_dumping": False,
            "spread_bps": 11.2,
            "pos_pct": 74.0,
            "pos_24h_pct": 91.5,
            "ret60_bps": 19.5,
            "current_slope_bps_h": 1.2,
            "up_structure": True,
            "fast_staircase": False,
            "staircase_trend": False,
            "staircase_score": 84.0,
            "staircase_positive_share": 0.68,
            "staircase_pullback_count": 2.0,
            "staircase_max_pullback_run_bps": 42.0,
        }

        self.assertTrue(rotation_auto_coin_selector._is_staircase_candidate(row))
        self.assertGreater(rotation_auto_coin_selector._staircase_strategy_rank(row), 80.0)

    def test_fast_track_skips_unqualified_guarded_impulse(self) -> None:
        rows = [
            {
                "symbol": "MANTRA",
                "score": -42.0,
                "fast_impulse": True,
                "short_horizon_scalp_ok": True,
                "spread_bps": 13.0,
                "ret15_bps": 45.0,
                "structure_slope_short_bps": 2.2,
                "pos_pct": 18.5,
                "pos_24h_pct": 18.5,
                "macro_down_context": False,
                "keep_open": False,
                "eligible": False,
                "up_structure": False,
                "net24_pct": -4.05,
            }
        ]

        rotation_auto_coin_selector._annotate_rows_with_strategy_views(rows, "scalp_guarded")
        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=[],
            previous_selected_since={},
            top=1,
            profile_name="scalp_guarded",
            switch_margin_score=8.0,
            min_active_minutes=2.0,
            active_retain_min_score=70.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(selected, [])
        self.assertEqual(selected_strategy_map, {})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["strategy_scores"], {})
        self.assertEqual(rows[0]["selection_path"], "")

    def test_fast_track_keeps_qualified_breakout_impulse(self) -> None:
        rows = [
            {
                "symbol": "FAST",
                "score": 180.0,
                "fast_impulse": True,
                "short_horizon_scalp_ok": True,
                "spread_bps": 7.0,
                "ret15_bps": 55.0,
                "rel15_bps": 48.0,
                "rel60_bps": 60.0,
                "structure_slope_short_bps": 2.5,
                "pos_pct": 44.0,
                "pos_24h_pct": 44.0,
                "macro_down_context": False,
                "macro_up_context": True,
                "keep_open": False,
                "eligible": False,
                "up_structure": True,
                "net24_pct": 1.5,
                "gate_reason": "",
            }
        ]

        rotation_auto_coin_selector._annotate_rows_with_strategy_views(rows, "scalp_guarded")
        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=[],
            previous_selected_since={},
            top=1,
            profile_name="scalp_guarded",
            switch_margin_score=8.0,
            min_active_minutes=2.0,
            active_retain_min_score=70.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(selected, ["FAST"])
        self.assertEqual(selected_strategy_map, {"FAST": "breakout"})
        self.assertEqual(strategy_sequence, ["breakout"])
        self.assertEqual(rows[0]["selection_path"], "fast_track")

    def test_override_only_candidate_without_strategy_is_not_selected(self) -> None:
        rows = [
            {
                "symbol": "ZK",
                "score": -10809.81,
                "spread_bps": 5.2,
                "ret15_bps": 5.2,
                "rel15_bps": 12.0,
                "rel60_bps": -19.4,
                "pos_pct": 34.7,
                "pos_24h_pct": 46.1,
                "macro_down_context": True,
                "keep_open": False,
                "eligible": False,
                "up_structure": True,
                "net24_pct": -2.0,
                "gate_reason": "post_dump_recovery_pending",
            }
        ]

        with mock.patch.dict(
            os.environ,
            {"ROTATION_META_CANDIDATE_OVERRIDES": "ZK"},
            clear=False,
        ):
            rotation_auto_coin_selector._annotate_rows_with_strategy_views(rows, "scalp_guarded")
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded",
                switch_margin_score=8.0,
                min_active_minutes=2.0,
                active_retain_min_score=70.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 14, tzinfo=timezone.utc),
            )

        self.assertEqual(rows[0]["strategy_meta_score"], 24.0)
        self.assertEqual(rows[0]["strategy_scores"], {})
        self.assertEqual(selected, [])
        self.assertEqual(selected_strategy_map, {})
        self.assertEqual(strategy_sequence, [])
        self.assertEqual(rows[0]["selection_path"], "")

    def test_guarded_profile_allows_controlled_rebound_bottom_liftoff(self) -> None:
        row = {
            "symbol": "ROBO",
            "gate_reason": "no_setup_lift_off",
            "bottom_zone": True,
            "in_valley_context": True,
            "structure_phase": "lift_off",
            "structure_confidence": 0.9,
            "rebound_from_30m_low_bps": 79.0,
            "rebound_from_60m_low_bps": 130.0,
            "bars_since_30m_low": 6.0,
            "bars_since_swing_low": 7.0,
            "previous_selloff": True,
            "spread_bps": 7.5,
            "pos_24h_pct": 19.0,
            "pos_pct": 24.0,
            "still_dumping": False,
            "rebound_in_downtrend": False,
            "macro_down_context": False,
            "bottom_candidate": False,
            "recent_rebound_ready": False,
            "post_dump_recovery_ready": False,
            "base_ready": False,
            "fresh_bottom": False,
            "higher_low_ready": False,
        }

        self.assertTrue(rotation_auto_coin_selector._is_rebound_candidate(row))
        self.assertFalse(
            rotation_auto_coin_selector._local_live_strategy_block(row, "rebound", "scalp_guarded")
        )

    def test_guarded_profile_keeps_rebound_blocked_while_falling_now(self) -> None:
        row = {
            "symbol": "ROBO",
            "gate_reason": "falling_now",
            "bottom_zone": True,
            "in_valley_context": True,
            "structure_phase": "bottom",
            "structure_confidence": 0.72,
            "rebound_from_30m_low_bps": 47.0,
            "rebound_from_60m_low_bps": 108.0,
            "bars_since_30m_low": 5.0,
            "bars_since_swing_low": 8.0,
            "previous_selloff": True,
            "spread_bps": 7.4,
            "pos_24h_pct": 18.7,
            "pos_pct": 24.2,
            "still_dumping": False,
            "rebound_in_downtrend": False,
            "macro_down_context": False,
            "bottom_candidate": False,
            "recent_rebound_ready": False,
            "post_dump_recovery_ready": False,
            "base_ready": False,
            "fresh_bottom": False,
            "higher_low_ready": False,
        }

        self.assertTrue(rotation_auto_coin_selector._is_rebound_candidate(row))
        self.assertTrue(
            rotation_auto_coin_selector._local_live_strategy_block(row, "rebound", "scalp_guarded")
        )

    def test_guarded_open_selects_early_bottom_reversal_rebound(self) -> None:
        rows = [
            {
                "symbol": "ENA",
                "score": -11134.18,
                "score_bottom": -465.05,
                "spread_bps": 9.3,
                "gate_reason": "rebound_in_downtrend",
                "keep_open": False,
                "eligible": False,
                "bottom_zone": True,
                "recent_rebound_ready": True,
                "base_ready": True,
                "higher_low_ready": True,
                "fresh_bottom": False,
                "previous_selloff": True,
                "still_dumping": False,
                "post_dump_recovery_ready": False,
                "rebound_in_downtrend": True,
                "macro_down_context": True,
                "macro_up_context": False,
                "up_structure": True,
                "down_structure": False,
                "structure_phase": "bottom",
                "active_leg": "rise",
                "pos_pct": 34.8,
                "pos_24h_pct": 23.1,
                "bars_since_30m_low": 4.0,
                "bars_since_swing_low": 4.0,
                "rebound_from_30m_low_bps": 18.6,
                "ret15_bps": 9.3,
                "rel15_bps": 2.0,
                "ret60_bps": -37.0,
                "ret120_bps": -64.6,
                "geo_drawdown_from_peak_bps": 69.5,
                "net24_pct": -1.82,
            }
        ]

        self.assertTrue(rotation_auto_coin_selector._is_rebound_candidate(rows[0]))
        self.assertFalse(
            rotation_auto_coin_selector._local_live_strategy_block(
                rows[0], "rebound", "scalp_guarded_open"
            )
        )

        rotation_auto_coin_selector._annotate_rows_with_strategy_views(rows, "scalp_guarded_open")
        selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
            rows=rows,
            previous=[],
            previous_selected_since={},
            top=1,
            profile_name="scalp_guarded_open",
            switch_margin_score=12.0,
            min_active_minutes=4.0,
            active_retain_min_score=65.0,
            max_retain_position_pct=70.0,
            generated_at=datetime(2026, 3, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(selected, ["ENA"])
        self.assertEqual(selected_strategy_map, {"ENA": "rebound"})
        self.assertEqual(strategy_sequence, ["rebound"])

    def test_guarded_open_keeps_hard_countertrend_rebound_out(self) -> None:
        row = {
            "symbol": "ENA",
            "spread_bps": 9.3,
            "gate_reason": "rebound_in_downtrend",
            "keep_open": False,
            "bottom_zone": True,
            "recent_rebound_ready": True,
            "base_ready": True,
            "higher_low_ready": True,
            "previous_selloff": True,
            "still_dumping": False,
            "post_dump_recovery_ready": False,
            "rebound_in_downtrend": True,
            "macro_down_context": True,
            "up_structure": True,
            "down_structure": False,
            "structure_phase": "bottom",
            "active_leg": "rise",
            "pos_pct": 34.8,
            "pos_24h_pct": 22.6,
            "bars_since_30m_low": 3.0,
            "bars_since_swing_low": 3.0,
            "rebound_from_30m_low_bps": 18.6,
            "ret15_bps": 18.6,
            "rel15_bps": 12.1,
            "ret60_bps": -49.0,
            "ret120_bps": -128.0,
            "geo_drawdown_from_peak_bps": 70.9,
            "net24_pct": -4.2,
        }

        self.assertFalse(rotation_auto_coin_selector._is_rebound_candidate(row))

    def test_choose_selected_skips_new_candidate_above_retain_position_cap(self) -> None:
        rows = [
            {
                "symbol": "SKY",
                "score": -10091.85,
                "keep_open": False,
                "eligible": False,
                "pos_pct": 88.3,
                "spread_bps": 7.6,
                "strategy_tags": ["rebound"],
                "strategy_scores": {"rebound": 55.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 55.0,
                "selected_active": False,
            },
            {
                "symbol": "ENA",
                "score": -10110.25,
                "keep_open": False,
                "eligible": False,
                "pos_pct": 34.0,
                "spread_bps": 9.1,
                "strategy_tags": ["rebound"],
                "strategy_scores": {"rebound": 52.0},
                "strategy_primary": "rebound",
                "strategy_meta_score": 52.0,
                "selected_active": False,
            },
        ]

        with mock.patch.object(rotation_auto_coin_selector, "_strategy_slot_plan", return_value=["rebound"]):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=1,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 17, 4, 40, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ["ENA"])
        self.assertEqual(selected_strategy_map, {"ENA": "rebound"})
        self.assertEqual(strategy_sequence, ["rebound"])
        self.assertEqual(rows[0]["selection_path"], "")
        self.assertEqual(rows[1]["selection_path"], "strategy_plan")

    def test_guarded_strategy_plan_injects_single_rebound_slot(self) -> None:
        plan = rotation_auto_coin_selector._inject_guarded_rebound_slot(
            ["staircase", "pullback_continuation", "continuation", "breakout_retest"],
            "scalp_guarded",
            4,
        )

        self.assertEqual(plan.count("rebound"), 1)
        self.assertEqual(plan, ["staircase", "pullback_continuation", "continuation", "rebound"])

    def test_enabled_strategies_override_limits_live_selector_to_core_four(self) -> None:
        row = {
            "symbol": "SOL",
            "score": 180.0,
            "keep_open": False,
            "eligible": False,
        }

        with (
            mock.patch.dict(
                os.environ,
                {
                    "ROTATION_ENABLED_STRATEGIES": "staircase,continuation,breakout,rebound",
                    "ROTATION_STRATEGY_SLOT_PLAN": "staircase,continuation,breakout,rebound",
                    "ROTATION_STRATEGY_WEIGHT_STAIRCASE": "0.25",
                    "ROTATION_STRATEGY_WEIGHT_CONTINUATION": "0.25",
                    "ROTATION_STRATEGY_WEIGHT_BREAKOUT": "0.25",
                    "ROTATION_STRATEGY_WEIGHT_REBOUND": "0.25",
                    "ROTATION_STRATEGY_WEIGHT_PULLBACK_CONTINUATION": "9.0",
                    "ROTATION_STRATEGY_WEIGHT_BREAKOUT_RETEST": "9.0",
                    "ROTATION_STRATEGY_WEIGHT_RELATIVE_STRENGTH": "9.0",
                },
                clear=False,
            ),
            mock.patch.object(rotation_auto_coin_selector, "_is_staircase_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_staircase_strategy_rank", return_value=90.0),
            mock.patch.object(rotation_auto_coin_selector, "_is_pullback_continuation_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_pullback_continuation_rank", return_value=88.0),
            mock.patch.object(rotation_auto_coin_selector, "_is_breakout_retest_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_breakout_retest_rank", return_value=86.0),
            mock.patch.object(rotation_auto_coin_selector, "_is_continuation_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_continuation_rank", return_value=84.0),
            mock.patch.object(rotation_auto_coin_selector, "_is_breakout_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_breakout_rank", return_value=82.0),
            mock.patch.object(rotation_auto_coin_selector, "_is_relative_strength_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_relative_strength_rank", return_value=80.0),
            mock.patch.object(rotation_auto_coin_selector, "_is_rebound_candidate", return_value=True),
            mock.patch.object(rotation_auto_coin_selector, "_rebound_rank", return_value=78.0),
            mock.patch.object(rotation_auto_coin_selector, "_local_live_strategy_block", return_value=False),
        ):
            rankings = rotation_auto_coin_selector._annotate_rows_with_strategy_views(
                [row], "scalp_guarded_open"
            )
            weights, source = rotation_auto_coin_selector._strategy_weight_overrides()
            plan = rotation_auto_coin_selector._strategy_slot_plan("scalp_guarded_open", 4)

        self.assertEqual(
            tuple(row["strategy_scores"].keys()),
            ("staircase", "continuation", "breakout", "rebound"),
        )
        self.assertEqual(set(rankings.keys()), {"staircase", "continuation", "breakout", "rebound"})
        self.assertEqual(set(weights.keys()), {"staircase", "continuation", "breakout", "rebound"})
        self.assertEqual(source, "env_override")
        self.assertEqual(plan, ["staircase", "continuation", "breakout", "rebound"])

    def test_continuation_structure_overrides_apply_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_CONT_MAX_STRUCTURE_RANGE_POS": "1.0",
                "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS": "1.15",
                "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS": "0.0",
                "ROTATION_CONT_STAIRCASE_MAX_CONTEXT_RANGE_POS": "0.98",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["cont_max_structure_range_pos"], 1.0)
        self.assertEqual(profile["cont_staircase_min_slope_medium_bps"], 1.15)
        self.assertEqual(profile["cont_staircase_min_drawdown_from_peak_bps"], 0.0)
        self.assertEqual(profile["cont_staircase_max_context_range_pos"], 0.98)

    def test_breakout_trigger_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_BREAKOUT_TRIGGER_BPS": "5.5",
                "ROTATION_BREAKOUT_BOTTOM_COUNTERTREND_BLOCK_MAX_CONTEXT_RANGE_POS": "0.23",
                "ROTATION_BREAKOUT_BOTTOM_COUNTERTREND_BLOCK_MAX_CONTEXT_REBOUND_BPS": "150.0",
                "ROTATION_BREAKOUT_BOTTOM_COUNTERTREND_BLOCK_MAX_TREND_RETURN_BPS": "-75.0",
                "ROTATION_BREAKOUT_TOP_ZONE_BLOCK_MIN_CONTEXT_RANGE_POS": "0.985",
                "ROTATION_BREAKOUT_TOP_ZONE_BLOCK_MAX_CONTEXT_REBOUND_BPS": "95.0",
                "ROTATION_BREAKOUT_TOP_ZONE_BLOCK_MIN_VOLUME_Z": "0.45",
                "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_CONTEXT_RANGE_POS": "0.60",
                "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_CONTEXT_REBOUND_BPS": "400.0",
                "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_MIN_SPREAD_BPS": "18.0",
                "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_CONTEXT_RANGE_POS": "0.49",
                "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_CONTEXT_REBOUND_BPS": "780.0",
                "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_MIN_VOLUME_Z": "0.1",
                "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_CONTEXT_RANGE_POS": "0.71",
                "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_CONTEXT_REBOUND_BPS": "1350.0",
                "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_MIN_VOLUME_Z": "0.42",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["breakout_trigger_bps"], 5.5)
        self.assertEqual(profile["breakout_bottom_countertrend_block_max_context_range_pos"], 0.23)
        self.assertEqual(profile["breakout_bottom_countertrend_block_max_context_rebound_bps"], 150.0)
        self.assertEqual(profile["breakout_bottom_countertrend_block_max_trend_return_bps"], -75.0)
        self.assertEqual(profile["breakout_top_zone_block_min_context_range_pos"], 0.985)
        self.assertEqual(profile["breakout_top_zone_block_max_context_rebound_bps"], 95.0)
        self.assertEqual(profile["breakout_top_zone_block_min_volume_z"], 0.45)
        self.assertEqual(profile["breakout_thin_rebound_block_context_range_pos"], 0.60)
        self.assertEqual(profile["breakout_thin_rebound_block_context_rebound_bps"], 400.0)
        self.assertEqual(profile["breakout_thin_rebound_block_min_spread_bps"], 18.0)
        self.assertEqual(profile["breakout_mid_rebound_block_context_range_pos"], 0.49)
        self.assertEqual(profile["breakout_mid_rebound_block_context_rebound_bps"], 780.0)
        self.assertEqual(profile["breakout_mid_rebound_block_min_volume_z"], 0.1)
        self.assertEqual(profile["breakout_late_rebound_block_context_range_pos"], 0.71)
        self.assertEqual(profile["breakout_late_rebound_block_context_rebound_bps"], 1350.0)
        self.assertEqual(profile["breakout_late_rebound_block_min_volume_z"], 0.42)

    def test_exit_profile_overrides_apply_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_GREEN_CANDLE_TAKE_MIN_BARS": "4",
                "ROTATION_GREEN_CANDLE_TAKE_MAX_BARS": "6",
                "ROTATION_GREEN_CANDLE_TAKE_REQUIRED_GREEN_BARS": "3",
                "ROTATION_GREEN_CANDLE_TAKE_MIN_PROFIT_BPS": "7.5",
                "ROTATION_TIME_BREAK_EVEN_FLOOR_BARS": "15",
                "ROTATION_FAILED_START_MIN_BARS": "4",
                "ROTATION_HARD_STOP_LOSS_BPS": "112",
                "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WEAK_EXIT": "7",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["green_candle_take_min_bars"], 4.0)
        self.assertEqual(profile["green_candle_take_max_bars"], 6.0)
        self.assertEqual(profile["green_candle_take_required_green_bars"], 3.0)
        self.assertEqual(profile["green_candle_take_min_profit_bps"], 7.5)
        self.assertEqual(profile["time_break_even_floor_bars"], 15.0)
        self.assertEqual(profile["failed_start_min_bars"], 4.0)
        self.assertEqual(profile["hard_stop_loss_bps"], 112.0)
        self.assertEqual(profile["reentry_cooldown_bars_after_weak_exit"], 7.0)

    def test_late_entry_profile_overrides_apply_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS": "0.83",
                "ROTATION_LATE_ENTRY_BLOCK_STRUCTURE_RANGE_POS": "0.96",
                "ROTATION_LATE_ENTRY_BLOCK_MAX_CONTEXT_DRAWDOWN_BPS": "27",
                "ROTATION_LATE_ENTRY_BLOCK_MIN_TREND_RETURN_BPS": "88",
                "ROTATION_LATE_ENTRY_BLOCK_MIN_RETURN_BPS": "9",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["late_entry_block_context_range_pos"], 0.83)
        self.assertEqual(profile["late_entry_block_structure_range_pos"], 0.96)
        self.assertEqual(profile["late_entry_block_max_context_drawdown_bps"], 27.0)
        self.assertEqual(profile["late_entry_block_min_trend_return_bps"], 88.0)
        self.assertEqual(profile["late_entry_block_min_return_bps"], 9.0)

    def test_swing_reversal_threshold_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_REVERSAL_THRESHOLD_BPS": "0.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_reversal_threshold_bps"], 0.0)

    def test_simple_swing_buy_band_prefers_selector_override(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_BUY_BAND": "0.30",
                "ROTATION_SELECTOR_SIMPLE_SWING_BUY_BAND": "0.55",
            },
            clear=False,
        ):
            band = rotation_auto_coin_selector._simple_swing_buy_band()

        self.assertEqual(band, 0.55)

    def test_simple_swing_micro_valley_gate_blocks_unconfirmed_setup(self) -> None:
        row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 12.0,
            "bars_since_swing_low": 3.0,
            "bars_since_30m_low": 2.0,
            "rebound_from_30m_low_bps": 4.0,
            "ret15_bps": -0.5,
            "structure_slope_short_bps": -0.2,
            "active_leg": "fall",
            "recent_rebound_ready": False,
            "in_valley_context": True,
        }

        reason = rotation_auto_coin_selector._simple_swing_gate_reason(row)
        self.assertEqual(reason, "rule_micro_valley_unconfirmed")

    def test_simple_swing_micro_valley_gate_blocks_late_rebound(self) -> None:
        row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 12.0,
            "bars_since_swing_low": 2.0,
            "bars_since_30m_low": 2.0,
            "rebound_from_30m_low_bps": 64.0,
            "ret15_bps": 6.0,
            "structure_slope_short_bps": 1.0,
            "active_leg": "rise",
            "recent_rebound_ready": True,
            "in_valley_context": True,
        }

        reason = rotation_auto_coin_selector._simple_swing_gate_reason(row)
        self.assertEqual(reason, "rule_micro_valley_too_late")

    def test_simple_swing_micro_valley_gate_blocks_missing_context_keys(self) -> None:
        row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 12.0,
            "bars_since_swing_low": 2.0,
            "bars_since_30m_low": 2.0,
            "rebound_from_30m_low_bps": 10.0,
            "ret15_bps": 6.0,
            "structure_slope_short_bps": 1.0,
            "active_leg": "rise",
            "recent_rebound_ready": True,
        }

        reason = rotation_auto_coin_selector._simple_swing_gate_reason(row)
        self.assertEqual(reason, "rule_micro_valley_context_miss")

    def test_simple_swing_entry_ready_requires_fresh_micro_valley_by_default(self) -> None:
        row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 10.0,
            "bars_since_swing_low": 26.0,
            "bars_since_30m_low": 10.0,
            "rebound_from_30m_low_bps": 11.0,
            "ret15_bps": 5.0,
            "structure_slope_short_bps": 0.6,
            "active_leg": "rise",
            "recent_rebound_ready": True,
            "in_valley_context": True,
        }

        self.assertFalse(rotation_auto_coin_selector._simple_swing_entry_ready(row))
        self.assertEqual(rotation_auto_coin_selector._simple_swing_gate_reason(row), "rule_micro_valley_too_old")

    def test_simple_swing_entry_ready_can_disable_micro_valley_gate(self) -> None:
        row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 10.0,
            "bars_since_swing_low": 26.0,
            "bars_since_30m_low": 10.0,
            "rebound_from_30m_low_bps": 11.0,
            "ret15_bps": 5.0,
            "structure_slope_short_bps": 0.6,
            "active_leg": "rise",
            "recent_rebound_ready": True,
            "in_valley_context": True,
        }

        with mock.patch.dict(
            os.environ,
            {"ROTATION_SELECTOR_SIMPLE_SWING_REQUIRE_MICRO_VALLEY": "0"},
            clear=False,
        ):
            self.assertTrue(rotation_auto_coin_selector._simple_swing_entry_ready(row))
            self.assertEqual(rotation_auto_coin_selector._simple_swing_gate_reason(row), "")

    def test_simple_swing_selector_score_prefers_cleaner_micro_valley(self) -> None:
        clean_row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 8.0,
            "bars_since_swing_low": 2.0,
            "bars_since_30m_low": 1.0,
            "rebound_from_30m_low_bps": 9.0,
            "ret15_bps": 3.5,
            "structure_slope_short_bps": 0.9,
            "active_leg": "rise",
            "recent_rebound_ready": True,
            "in_valley_context": True,
        }
        weak_row = {
            "keep_open": False,
            "turns24": 72.0,
            "width72_pct": 18.0,
            "cycle_fit_profit_step_ok": True,
            "cycle_fit_ok": True,
            "pos_24h_pct": 18.0,
            "bars_since_swing_low": 11.0,
            "bars_since_30m_low": 7.0,
            "rebound_from_30m_low_bps": 30.0,
            "ret15_bps": 3.5,
            "structure_slope_short_bps": 0.9,
            "active_leg": "rise",
            "recent_rebound_ready": True,
            "in_valley_context": True,
        }

        clean_score = rotation_auto_coin_selector._simple_swing_selector_score(clean_row)
        weak_score = rotation_auto_coin_selector._simple_swing_selector_score(weak_row)
        self.assertGreater(clean_score, weak_score)

    def test_profile_values_sanitizes_invalid_swing_band_pair(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_BUY_BAND": "0.55",
                "ROTATION_SWING_SELL_BAND": "0.50",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertGreater(profile["swing_buy_band"], 0.0)
        self.assertLess(profile["swing_sell_band"], 1.0)
        self.assertLess(profile["swing_buy_band"], profile["swing_sell_band"])

    def test_swing_min_range_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MIN_RANGE_BPS": "36.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_min_range_bps"], 36.0)

    def test_swing_micro_rebound_spread_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MICRO_REBOUND_MAX_SPREAD_BPS": "15.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_micro_rebound_max_spread_bps"], 15.0)

    def test_swing_micro_rebound_context_range_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MICRO_REBOUND_MAX_CONTEXT_RANGE_POS": "0.92",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_micro_rebound_max_context_range_pos"], 0.92)

    def test_swing_micro_rebound_context_rebound_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MICRO_REBOUND_MIN_CONTEXT_REBOUND_BPS": "260.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_micro_rebound_min_context_rebound_bps"], 260.0)

    def test_swing_micro_rebound_min_ret_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MICRO_REBOUND_MIN_RET_BPS": "-18.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_micro_rebound_min_ret_bps"], -18.0)

    def test_swing_micro_rebound_confirm_rebound_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MICRO_REBOUND_CONFIRM_REBOUND_BPS": "180.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_micro_rebound_confirm_rebound_bps"], 180.0)

    def test_swing_micro_rebound_confirm_min_ret_override_applies_from_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ROTATION_SWING_MICRO_REBOUND_CONFIRM_MIN_RET_BPS": "0.0",
            },
            clear=False,
        ):
            profile = _profile_values("scalp_guarded")

        self.assertEqual(profile["swing_micro_rebound_confirm_min_ret_bps"], 0.0)

    def test_set_fraction_deduplicates_runtime_risk_and_exec_keys(self) -> None:
        config_text = textwrap.dedent(
            """\
            alpha:
              type: auto
              override_path: configs/test.yaml
              auto:
                swing:
                  momentum_lookback: 2
                  threshold_bps: 4.0
            features:
              return_window: 1
              atr_window: 3
              volume_z_window: 4
              trend_window: 48
              context_window: 576
            gate:
              safety_margin_bps: 1.0
              cost_coverage_ratio: 1.0
              cost_roundtrip_multiplier: 1.0
              max_spread_bps: 22.0
              max_atr_bps: 180.0
            risk:
              max_exposure_mode: fixed
              max_exposure_fraction: 1.0
              entry_edge_bps: 1.0
              entry_cost_buffer_bps: 1.0
              entry_cost_coverage_ratio: 1.0
              entry_cost_roundtrip_multiplier: 1.0
              entry_min_atr_to_cost_ratio: 1.0
              late_entry_block_context_range_pos: 0.91
              late_entry_block_structure_range_pos: 0.98
              late_entry_block_max_context_drawdown_bps: 10.0
              late_entry_block_min_trend_return_bps: 120.0
              late_entry_block_min_return_bps: 12.0
              override_max_structure_range_pos: 1.0
              override_min_drawdown_from_peak_bps: 1.0
              override_min_drawdown_to_cost_ratio: 1.0
              override_min_slope_short_bps: 1.0
              override_max_trend_return_bps: 1.0
              override_max_context_range_pos: 1.0
              late_entry_block_context_range_pos: 0.92
              late_entry_block_structure_range_pos: 0.99
              late_entry_block_max_context_drawdown_bps: 11.0
              late_entry_block_min_trend_return_bps: 121.0
              late_entry_block_min_return_bps: 13.0
              cooldown_bars: 1
              min_hold_bars: 1
              reentry_min_move_bps: 10.0
              reentry_cooldown_bars_after_trailing_stop: 3
              reentry_cooldown_bars_after_whipsaw_stop_loss: 3
              reentry_whipsaw_hard_stop_max_bars: 3
              reentry_loss_cluster_window_bars: 12
              reentry_cooldown_bars_after_loss_cluster: 9
              reentry_cooldown_bars_after_weak_exit: 2
              reentry_cooldown_bars_after_trailing_stop: 3
              reentry_cooldown_bars_after_whipsaw_stop_loss: 3
              reentry_whipsaw_hard_stop_max_bars: 3
              reentry_loss_cluster_window_bars: 11
              reentry_cooldown_bars_after_loss_cluster: 8
              reentry_cooldown_bars_after_weak_exit: 1
              position_epsilon_eur: 9.0
              min_entry_depth_eur: 99.0
              max_entry_notional_to_depth_ratio: 9.0
              min_entry_depth_eur: 88.0
              max_entry_notional_to_depth_ratio: 8.0
              full_position_only: false
              require_break_even_for_exit: false
              allow_reversal_exit_after_break_even: false
              hard_stop_loss_bps: 100.0
              min_exit_profit_bps: 1.0
              time_break_even_floor_enabled: false
              time_break_even_floor_bars: 1
              hard_take_profit_bps: 10.0
              hard_take_profit_only_in_range: true
              trailing_stop_enabled: false
              trailing_activation_bps: 20.0
              trailing_stop_bps: 10.0
              trailing_stop_atr_mult: 1.0
              campaign_hold_enabled: false
              campaign_hold_min_bars: 1
              campaign_hold_min_profit_bps: 1.0
              campaign_hold_min_trend_bps: 1.0
              campaign_hold_max_range_pos: 1.0
              campaign_hold_max_drawdown_from_peak_bps: 1.0
              campaign_hold_min_recent_bias_bps: 1.0
              red_candle_exit_enabled: false
              red_candle_window_bars: 1
              green_candle_take_exit_enabled: false
              green_candle_take_min_bars: 1
              green_candle_take_max_bars: 1
              green_candle_take_required_green_bars: 1
              green_candle_take_min_profit_bps: 1.0
              failed_start_exit_enabled: false
              failed_start_min_bars: 1
              failed_start_max_bars: 1
              failed_start_min_rebound_bps: 1.0
              failed_start_loss_bps: 1.0
              chop_break_even_reclaim_enabled: false
              chop_break_even_reclaim_min_bars: 1
              chop_break_even_reclaim_min_drawdown_bps: 1.0
              chop_break_even_reclaim_max_edge_bps: 1.0
              chop_break_even_reclaim_cross_window_bars: 1
              chop_break_even_reclaim_min_crosses: 1
              peak_profit_retrace_enabled: false
              peak_profit_retrace_arm_bps: 1.0
              peak_profit_retrace_pct: 1.0
            order:
              cycle_trade_mode: fixed
              cycle_trade_fraction: 1.0
            md:
              interval_seconds: 60
              stale_seconds: 60
              stale_book_seconds: 60
              stale_trade_seconds: 60
            exec:
              sync_min_position_eur: 1.0
              min_entry_notional_eur: 3.0
              sell_balance_buffer_btc: 0.2
              min_entry_notional_eur: 2.0
              sell_balance_buffer_btc: 0.1
            impact:
              enabled: true
              interval_seconds: 60
            news:
              require_long_bias_for_entries: true
            core:
              stale_seconds: 60
              warmup:
                enabled: false
                min_bars: 1
                max_bars: 2
            policy:
                window_bars: 10
                min_bars: 5
            """
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "live_binance_test_usdc_rotation.yaml"
            path.write_text(config_text, encoding="utf-8")
            profile = _profile_values("scalp_guarded")
            with mock.patch.object(rotation_auto_coin_selector, "_ensure_lane_config", return_value=path):
                rotation_auto_coin_selector._set_fraction("TEST", 0.25, profile)

            updated = path.read_text(encoding="utf-8")
            data = yaml.safe_load(updated)

        self.assertEqual(updated.count("reentry_cooldown_bars_after_trailing_stop:"), 1)
        self.assertEqual(updated.count("reentry_cooldown_bars_after_whipsaw_stop_loss:"), 1)
        self.assertEqual(updated.count("reentry_whipsaw_hard_stop_max_bars:"), 1)
        self.assertEqual(updated.count("reentry_loss_cluster_window_bars:"), 1)
        self.assertEqual(updated.count("reentry_cooldown_bars_after_loss_cluster:"), 1)
        self.assertEqual(updated.count("reentry_cooldown_bars_after_weak_exit:"), 1)
        self.assertEqual(updated.count("late_entry_block_context_range_pos:"), 1)
        self.assertEqual(updated.count("late_entry_block_structure_range_pos:"), 1)
        self.assertEqual(updated.count("late_entry_block_max_context_drawdown_bps:"), 1)
        self.assertEqual(updated.count("late_entry_block_min_trend_return_bps:"), 1)
        self.assertEqual(updated.count("late_entry_block_min_return_bps:"), 1)
        self.assertEqual(updated.count("min_entry_depth_eur:"), 1)
        self.assertEqual(updated.count("max_entry_notional_to_depth_ratio:"), 1)
        self.assertEqual(updated.count("min_entry_notional_eur:"), 1)
        self.assertEqual(updated.count("sell_balance_buffer_btc:"), 1)
        self.assertEqual(updated.count("reversal_threshold_bps:"), 2)
        self.assertEqual(updated.count("micro_rebound_max_context_range_pos:"), 2)
        self.assertEqual(updated.count("micro_rebound_min_context_rebound_bps:"), 2)
        self.assertEqual(updated.count("micro_rebound_min_ret_bps:"), 2)
        self.assertEqual(updated.count("micro_rebound_confirm_rebound_bps:"), 2)
        self.assertEqual(updated.count("micro_rebound_confirm_min_ret_bps:"), 2)
        self.assertEqual(updated.count("breakout:"), 1)
        self.assertEqual(updated.count("mid_rebound_block_context_range_pos:"), 1)
        self.assertEqual(updated.count("mid_rebound_block_context_rebound_bps:"), 1)
        self.assertEqual(updated.count("mid_rebound_block_min_volume_z:"), 1)
        self.assertEqual(updated.count("late_rebound_block_context_range_pos:"), 1)
        self.assertEqual(updated.count("late_rebound_block_context_rebound_bps:"), 1)
        self.assertEqual(updated.count("late_rebound_block_min_volume_z:"), 1)
        self.assertEqual(updated.count("thin_rebound_block_context_range_pos:"), 1)
        self.assertEqual(updated.count("thin_rebound_block_context_rebound_bps:"), 1)
        self.assertEqual(updated.count("thin_rebound_block_min_spread_bps:"), 1)
        self.assertEqual(data["risk"]["reentry_cooldown_bars_after_trailing_stop"], 24)
        self.assertEqual(data["risk"]["reentry_cooldown_bars_after_whipsaw_stop_loss"], 8)
        self.assertEqual(data["risk"]["reentry_whipsaw_hard_stop_max_bars"], 4)
        self.assertEqual(data["risk"]["reentry_loss_cluster_window_bars"], 30)
        self.assertEqual(data["risk"]["reentry_cooldown_bars_after_loss_cluster"], 20)
        self.assertEqual(data["risk"]["reentry_cooldown_bars_after_weak_exit"], 5)
        self.assertEqual(data["risk"]["late_entry_block_context_range_pos"], 0.84)
        self.assertEqual(data["risk"]["late_entry_block_structure_range_pos"], 0.97)
        self.assertEqual(data["risk"]["late_entry_block_max_context_drawdown_bps"], 24.0)
        self.assertEqual(data["risk"]["late_entry_block_min_trend_return_bps"], 70.0)
        self.assertEqual(data["risk"]["late_entry_block_min_return_bps"], 8.0)
        self.assertEqual(data["risk"]["min_entry_depth_eur"], 40.0)
        self.assertEqual(data["risk"]["max_entry_notional_to_depth_ratio"], 1.0)
        self.assertEqual(data["exec"]["min_entry_notional_eur"], 6.0)
        self.assertEqual(data["exec"]["sell_balance_buffer_btc"], 0.0)
        self.assertEqual(data["alpha"]["auto"]["swing"]["threshold_bps"], 0.4)
        self.assertEqual(data["alpha"]["auto"]["swing"]["reversal_threshold_bps"], 0.4)
        self.assertEqual(data["alpha"]["auto"]["swing"]["min_range_bps"], 52.0)
        self.assertEqual(data["alpha"]["auto"]["swing"]["micro_rebound_max_spread_bps"], 8.0)
        self.assertEqual(data["alpha"]["auto"]["swing"]["micro_rebound_max_context_range_pos"], 0.38)
        self.assertEqual(data["alpha"]["auto"]["swing"]["micro_rebound_min_context_rebound_bps"], 0.0)
        self.assertEqual(data["alpha"]["auto"]["swing"]["micro_rebound_min_ret_bps"], -2.0)
        self.assertEqual(data["alpha"]["auto"]["swing"]["micro_rebound_confirm_rebound_bps"], 0.0)
        self.assertEqual(data["alpha"]["auto"]["swing"]["micro_rebound_confirm_min_ret_bps"], 0.0)
        self.assertEqual(data["alpha"]["swing"]["reversal_threshold_bps"], 0.4)
        self.assertEqual(data["alpha"]["swing"]["min_range_bps"], 52.0)
        self.assertEqual(data["alpha"]["swing"]["micro_rebound_max_spread_bps"], 8.0)
        self.assertEqual(data["alpha"]["swing"]["micro_rebound_max_context_range_pos"], 0.38)
        self.assertEqual(data["alpha"]["swing"]["micro_rebound_min_context_rebound_bps"], 0.0)
        self.assertEqual(data["alpha"]["swing"]["micro_rebound_min_ret_bps"], -2.0)
        self.assertEqual(data["alpha"]["swing"]["micro_rebound_confirm_rebound_bps"], 0.0)
        self.assertEqual(data["alpha"]["swing"]["micro_rebound_confirm_min_ret_bps"], 0.0)
        self.assertEqual(data["alpha"]["breakout"]["trigger_bps"], 6.0)
        self.assertEqual(data["alpha"]["breakout"]["bottom_countertrend_block_max_context_range_pos"], 0.22)
        self.assertEqual(data["alpha"]["breakout"]["bottom_countertrend_block_max_context_rebound_bps"], 140.0)
        self.assertEqual(data["alpha"]["breakout"]["bottom_countertrend_block_max_trend_return_bps"], -55.0)
        self.assertEqual(data["alpha"]["breakout"]["top_zone_block_min_context_range_pos"], 0.98)
        self.assertEqual(data["alpha"]["breakout"]["top_zone_block_max_context_rebound_bps"], 90.0)
        self.assertEqual(data["alpha"]["breakout"]["top_zone_block_min_volume_z"], 0.4)
        self.assertEqual(data["alpha"]["breakout"]["thin_rebound_block_context_range_pos"], 0.60)
        self.assertEqual(data["alpha"]["breakout"]["thin_rebound_block_context_rebound_bps"], 400.0)
        self.assertEqual(data["alpha"]["breakout"]["thin_rebound_block_min_spread_bps"], 18.0)
        self.assertEqual(data["alpha"]["breakout"]["mid_rebound_block_context_range_pos"], 0.48)
        self.assertEqual(data["alpha"]["breakout"]["mid_rebound_block_context_rebound_bps"], 760.0)
        self.assertEqual(data["alpha"]["breakout"]["mid_rebound_block_min_volume_z"], 0.0)
        self.assertEqual(data["alpha"]["breakout"]["late_rebound_block_context_range_pos"], 0.72)
        self.assertEqual(data["alpha"]["breakout"]["late_rebound_block_context_rebound_bps"], 1400.0)
        self.assertEqual(data["alpha"]["breakout"]["late_rebound_block_min_volume_z"], 0.35)

    def test_main_uses_payload_for_plaintext_output(self) -> None:
        payload = {
            "ok": True,
            "generated_at": "2026-03-12T11:32:11.331798+00:00",
            "selected": ["BTC"],
            "watch_symbols": ["BTC", "ETH"],
            "fraction": 1.0,
            "profile": "scalp_lockdown",
            "switch_margin_score": 8.0,
            "max_retain_position_pct": 70.0,
            "selector_fallback_rows_used": False,
            "selector_rate_limit_detected": False,
            "selected_strategy_map": {"BTC": "breakout"},
            "rows": [
                {
                    "symbol": "BTC",
                    "score": 123.4,
                    "ret15_bps": 12.0,
                    "rel15_bps": 8.0,
                    "spread_bps": 5.0,
                    "pos_pct": 42.0,
                    "width72_pct": 10.0,
                    "turns24": 3,
                    "net24_pct": 2.5,
                    "open_notional": 0.0,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            active_file = Path(tmpdir) / "rotation_active_lanes.json"
            stdout = io.StringIO()
            with (
                mock.patch.object(rotation_auto_coin_selector, "ACTIVE_FILE", active_file),
                mock.patch.object(rotation_auto_coin_selector, "_load_previous_payload", return_value=None),
                mock.patch.object(rotation_auto_coin_selector, "build_selector_payload", return_value=payload),
                mock.patch("sys.argv", ["rotation_auto_coin_selector.py", "--profile", "scalp_lockdown"]),
                contextlib.redirect_stdout(stdout),
            ):
                rotation_auto_coin_selector.main()

        output = stdout.getvalue()
        self.assertIn("selected: BTC", output)
        self.assertIn("watch: BTC, ETH", output)
        self.assertIn("profile: scalp_lockdown", output)
        self.assertIn("strategy=breakout", output)

    def test_selector_scan_timeout_defaults_to_210_seconds(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rotation_auto_coin_selector._selector_scan_timeout_sec(), 210.0)

    def test_run_selector_uses_extended_timeout_for_subprocess(self) -> None:
        selector_result = {"ok": True, "rows": [{"symbol": "BTC"}], "selected": ["BTC"]}
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(rotation_auto_coin_selector, "_load_selector_cache", return_value=None),
            mock.patch.object(rotation_auto_coin_selector, "_write_selector_cache"),
            mock.patch(
                "scripts.rotation_auto_coin_selector.subprocess.check_output",
                return_value=json.dumps(selector_result),
            ) as check_output,
        ):
            result = rotation_auto_coin_selector._run_selector()

        self.assertTrue(result["ok"])
        self.assertEqual(check_output.call_args.kwargs["timeout"], 210.0)

    def test_default_enabled_strategies_fall_back_to_core_four(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                rotation_auto_coin_selector._enabled_strategy_names(),
                rotation_auto_coin_selector.CORE_STRATEGY_NAMES,
            )

    def test_choose_selected_does_not_fill_with_ineligible_continuation_candidate(self) -> None:
        rows = [
            {
                "symbol": "PLUME",
                "score": 820.0,
                "keep_open": False,
                "eligible": True,
                "pos_pct": 68.0,
                "spread_bps": 8.1,
                "strategy_tags": ["continuation"],
                "strategy_scores": {"continuation": 180.0},
                "strategy_primary": "continuation",
                "strategy_meta_score": 180.0,
            },
            {
                "symbol": "PEOPLE",
                "score": -9559.0,
                "keep_open": False,
                "eligible": False,
                "gate_reason": "no_valley_context",
                "pos_pct": 75.0,
                "spread_bps": 13.2,
                "strategy_tags": ["continuation"],
                "strategy_scores": {"continuation": 140.0},
                "strategy_primary": "continuation",
                "strategy_meta_score": 140.0,
            },
        ]

        with mock.patch.object(
            rotation_auto_coin_selector,
            "_strategy_slot_plan",
            return_value=["continuation", "breakout"],
        ):
            selected, selected_strategy_map, strategy_sequence = rotation_auto_coin_selector._choose_selected(
                rows=rows,
                previous=[],
                previous_selected_since={},
                top=2,
                profile_name="scalp_guarded_open",
                switch_margin_score=12.0,
                min_active_minutes=4.0,
                active_retain_min_score=65.0,
                max_retain_position_pct=70.0,
                generated_at=datetime(2026, 3, 17, 0, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(selected, ["PLUME"])
        self.assertEqual(selected_strategy_map, {"PLUME": "continuation"})
        self.assertEqual(strategy_sequence, ["continuation"])
        self.assertEqual(rows[0]["selection_path"], "strategy_plan")
        self.assertEqual(rows[1]["selection_path"], "")

    def test_coin_experience_priors_decay_old_trade_outcomes(self) -> None:
        now = datetime(2026, 4, 11, 8, 0, tzinfo=timezone.utc)
        settings = {
            "lookback_days": 21.0,
            "half_life_days": 5.0,
            "min_trades": 2,
            "min_weighted_trades": 1.2,
            "full_weight_trades": 5.0,
            "max_abs_score": 36.0,
            "min_abs_score": 0.0,
        }
        trade_rows = [
            {
                "symbol": "XLMUSDC",
                "sellTime": (now - timedelta(days=1)).isoformat(),
                "buyGrossUsdc": 10.0,
                "proceedsUsdc": 0.12,
                "closed": True,
            },
            {
                "symbol": "XLMUSDC",
                "sellTime": (now - timedelta(days=2)).isoformat(),
                "buyGrossUsdc": 10.0,
                "proceedsUsdc": 0.10,
                "closed": True,
            },
            {
                "symbol": "XLMUSDC",
                "sellTime": (now - timedelta(days=20)).isoformat(),
                "buyGrossUsdc": 10.0,
                "proceedsUsdc": -1.00,
                "closed": True,
            },
            {
                "symbol": "PLUMEUSDC",
                "sellTime": (now - timedelta(hours=8)).isoformat(),
                "buyGrossUsdc": 10.0,
                "proceedsUsdc": -0.10,
                "closed": True,
            },
            {
                "symbol": "PLUMEUSDC",
                "sellTime": (now - timedelta(days=1)).isoformat(),
                "buyGrossUsdc": 10.0,
                "proceedsUsdc": -0.08,
                "closed": True,
            },
            {
                "symbol": "CVXUSDC",
                "sellTime": (now - timedelta(hours=2)).isoformat(),
                "buyGrossUsdc": 10.0,
                "proceedsUsdc": 0.20,
                "closed": True,
            },
        ]

        priors, info = rotation_auto_coin_selector._build_coin_experience_priors_from_rows(
            trade_rows,
            now=now,
            settings=settings,
        )

        self.assertGreater(priors["XLM"]["score"], 0.0)
        self.assertLessEqual(priors["XLM"]["score"], settings["max_abs_score"])
        self.assertLess(priors["PLUME"]["score"], 0.0)
        self.assertFalse(priors["CVX"]["sample_ok"])
        self.assertEqual(priors["CVX"]["score"], 0.0)
        self.assertEqual(info["trade_rows_seen"], len(trade_rows))

    def test_coin_experience_prior_does_not_create_strategy_candidate(self) -> None:
        rows = [
            {
                "symbol": "XLM",
                "score": -20.0,
                "strategy_scores": {},
                "strategy_tags": [],
                "strategy_primary": "",
                "strategy_meta_score": 0.0,
            }
        ]

        rotation_auto_coin_selector._apply_coin_experience_priors(
            rows,
            {"XLM": {"score": 30.0, "sample_ok": True, "raw_trade_count": 3}},
            enabled=True,
        )

        self.assertEqual(rows[0]["score"], -20.0)
        self.assertEqual(rows[0]["strategy_scores"], {})
        self.assertEqual(rows[0]["strategy_tags"], [])
        self.assertEqual(rows[0]["strategy_primary"], "")
        self.assertEqual(rows[0]["strategy_meta_score"], 0.0)
        self.assertEqual(rows[0]["coin_experience_score"], 30.0)

    def test_coin_experience_prior_can_remove_weak_strategy_candidate(self) -> None:
        rows = [
            {
                "symbol": "PLUME",
                "score": 100.0,
                "strategy_scores": {"rebound": 20.0},
                "strategy_tags": ["rebound"],
                "strategy_primary": "rebound",
                "strategy_primary_score": 20.0,
                "strategy_meta_score": 20.0,
            }
        ]

        rotation_auto_coin_selector._apply_coin_experience_priors(
            rows,
            {"PLUME": {"score": -36.0, "sample_ok": True, "raw_trade_count": 3}},
            enabled=True,
        )

        self.assertEqual(rows[0]["score"], 64.0)
        self.assertEqual(rows[0]["strategy_scores"], {})
        self.assertEqual(rows[0]["strategy_tags"], [])
        self.assertEqual(rows[0]["strategy_primary"], "")
        self.assertEqual(rows[0]["strategy_primary_score"], 0.0)
        self.assertLessEqual(rows[0]["strategy_meta_score"], 0.0)


if __name__ == "__main__":
    unittest.main()
