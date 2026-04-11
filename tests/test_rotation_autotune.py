from __future__ import annotations

import unittest

from trading.meta.rotation_autotune import recommend_rotation_autotune
from trading.meta.rotation_shadow import ShadowCandidate, TradeSummary


def _market_rows() -> list[dict]:
    rows = []
    for idx in range(8):
        rows.append(
            {
                "symbol": f"SYM{idx}",
                "eligible": True,
                "keep_open": False,
                "fast_impulse": idx < 4,
                "fast_staircase": idx < 3,
                "strong_continuation_context": idx < 5,
                "staircase_trend": idx < 4,
                "macro_up_context": True,
                "mid_trend_up_1h": True,
                "mid_trend_up_6h_balanced": True,
                "broad_uptrend_context": True,
                "spread_bps": 8.0,
            }
        )
    return rows


class TestRotationAutotune(unittest.TestCase):
    def test_recommends_uptrend_in_breakout_market_after_losses(self) -> None:
        rows = _market_rows()
        candidates = [
            ShadowCandidate(
                symbol="FET",
                ts="2026-03-12T10:00:00+00:00",
                age_sec=5.0,
                selected=False,
                watch=True,
                current_profile="scalp_guarded",
                strategy_primary="continuation",
                gate_reason="",
                open_notional=0.0,
                has_position=False,
                p_profit=0.66,
                feature_vector={},
                decision_context={},
            ),
            ShadowCandidate(
                symbol="SOL",
                ts="2026-03-12T10:00:00+00:00",
                age_sec=5.0,
                selected=False,
                watch=True,
                current_profile="scalp_guarded",
                strategy_primary="staircase",
                gate_reason="",
                open_notional=0.0,
                has_position=False,
                p_profit=0.69,
                feature_vector={},
                decision_context={},
            ),
            ShadowCandidate(
                symbol="ETH",
                ts="2026-03-12T10:00:00+00:00",
                age_sec=5.0,
                selected=False,
                watch=True,
                current_profile="scalp_guarded",
                strategy_primary="continuation",
                gate_reason="",
                open_notional=0.0,
                has_position=False,
                p_profit=0.64,
                feature_vector={},
                decision_context={},
            ),
            ShadowCandidate(
                symbol="ROBO",
                ts="2026-03-12T10:00:00+00:00",
                age_sec=5.0,
                selected=False,
                watch=True,
                current_profile="scalp_guarded",
                strategy_primary="breakout_retest",
                gate_reason="",
                open_notional=0.0,
                has_position=False,
                p_profit=0.58,
                feature_vector={},
                decision_context={},
            ),
        ]
        trade_summary = TradeSummary(
            trade_count=8,
            net_pnl=-0.31,
            win_rate=0.25,
            avg_win=0.05,
            avg_loss=-0.09,
            last_exit_ts="2026-03-12T10:10:00+00:00",
            exit_reasons={"failed_start_exit": 5, "trailing_stop": 3},
            strategy_breakdown={
                "continuation": {"trade_count": 5, "net_pnl": -0.21, "win_rate": 0.2, "exit_reasons": {"failed_start_exit": 3}},
                "staircase": {"trade_count": 2, "net_pnl": 0.04, "win_rate": 0.5, "exit_reasons": {"trailing_stop": 1}},
            },
            symbol_breakdown={
                "SENT": {
                    "trade_count": 4,
                    "net_pnl": -0.18,
                    "win_rate": 0.0,
                    "exit_reasons": {"failed_start_exit": 3},
                    "exit_path_summary": {
                        "sample_count": 4,
                        "early_exit_rate": 0.8,
                        "failed_start_recovery_rate": 0.75,
                        "hold_opportunity_rate": 0.75,
                    },
                },
                "FET": {"trade_count": 2, "net_pnl": -0.02, "win_rate": 0.5, "exit_reasons": {}},
            },
            exit_path_summary={
                "sample_count": 8,
                "early_exit_rate": 0.625,
                "protective_exit_rate": 0.125,
                "failed_start_recovery_rate": 0.6,
                "trailing_missed_run_rate": 0.5,
                "shakeout_then_run_rate": 0.375,
                "micro_pop_loss_run_rate": 0.5,
                "hold_opportunity_rate": 0.75,
                "avg_post_exit_close_15m_bps": 12.0,
                "avg_post_exit_close_30m_bps": 18.0,
                "avg_post_exit_close_60m_bps": 26.0,
                "avg_post_exit_close_120m_bps": 34.0,
            },
            entry_path_summary={
                "sample_count": 8,
                "poor_entry_rate": 0.5,
                "top_zone_entry_rate": 0.625,
                "late_entry_rate": 0.5,
                "avg_entry_quality_score_bps": -18.0,
            },
            no_trade_summary={
                "sample_count": 6,
                "missed_entry_rate": 0.5,
                "correct_block_rate": 0.1,
                "avg_post_decision_close_30m_bps": 14.0,
                "avg_post_decision_close_60m_bps": 22.0,
            },
        )
        base_profile_values = {
            "entry_edge_bps": 3.4,
            "entry_cost_coverage_ratio": 0.88,
            "gate_cost_coverage_ratio": 0.78,
            "entry_min_atr_to_cost_ratio": 1.15,
            "late_entry_block_context_range_pos": 0.82,
            "late_entry_block_structure_range_pos": 0.95,
            "late_entry_block_max_context_drawdown_bps": 22.0,
            "late_entry_block_min_trend_return_bps": 105.0,
            "late_entry_block_min_return_bps": 10.0,
            "reentry_cooldown_bars_after_weak_exit": 2,
            "reentry_min_move_bps": 95.0,
            "failed_start_min_bars": 1,
            "failed_start_max_bars": 2,
            "failed_start_loss_bps": 24.0,
            "failed_start_min_rebound_bps": 34.0,
            "trailing_activation_bps": 24.0,
            "trailing_stop_bps": 12.0,
            "min_exit_profit_bps": 12.0,
            "green_candle_take_min_bars": 2,
            "green_candle_take_max_bars": 0,
            "green_candle_take_required_green_bars": 2,
            "green_candle_take_min_profit_bps": 2.0,
            "time_break_even_floor_bars": 8,
            "campaign_hold_enabled": 1,
            "campaign_hold_min_bars": 5,
            "campaign_hold_min_profit_bps": 12.0,
            "campaign_hold_min_trend_bps": 130.0,
            "campaign_hold_max_drawdown_from_peak_bps": 24.0,
        }
        payloads = {
            "scalp_uptrend": {
                "selected": ["FET", "SOL", "ETH", "ROBO"],
                "selected_strategy_map": {"FET": "continuation", "SOL": "staircase", "ETH": "continuation", "ROBO": "breakout_retest"},
                "all_rows": rows,
                "active_candidate_count": 4,
                "selection_relaxed": False,
                "profile_values": dict(base_profile_values),
            },
            "scalp_lockdown": {
                "selected": ["FET", "ETH"],
                "selected_strategy_map": {"FET": "continuation", "ETH": "continuation"},
                "all_rows": rows,
                "active_candidate_count": 2,
                "selection_relaxed": False,
                "profile_values": {
                    **base_profile_values,
                    "entry_edge_bps": 6.0,
                    "entry_cost_coverage_ratio": 1.0,
                    "gate_cost_coverage_ratio": 0.95,
                    "entry_min_atr_to_cost_ratio": 1.25,
                    "reentry_min_move_bps": 120.0,
                    "failed_start_loss_bps": 24.0,
                },
            },
        }

        report = recommend_rotation_autotune(
            current_profile="scalp_guarded",
            trade_summary=trade_summary,
            candidates=candidates,
            profile_payloads=payloads,
        )

        self.assertTrue(report["enabled"])
        self.assertEqual(report["recommended_profile"], "scalp_uptrend")
        self.assertGreater(report["confidence"], 0.4)
        self.assertNotIn("SENT", report["avoid_symbols"])
        self.assertEqual(report["risk_mode_override"], "normal")
        self.assertGreater(report["shakeout_then_run_rate"], 0.3)
        self.assertGreater(report["micro_pop_loss_run_rate"], 0.4)
        self.assertIn("shakeout_then_run_detected", report["reason"])
        self.assertIn("micro_pop_loss_run_detected", report["reason"])
        self.assertLess(report["parameter_overrides"]["ROTATION_ENTRY_EDGE_BPS"], 4.1)
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_FAILED_START_LOSS_BPS"],
            base_profile_values["failed_start_loss_bps"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_TRAILING_STOP_BPS"],
            base_profile_values["trailing_stop_bps"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_FAILED_START_MIN_BARS"],
            base_profile_values["failed_start_min_bars"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS"],
            base_profile_values["campaign_hold_max_drawdown_from_peak_bps"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_CAMPAIGN_HOLD_MIN_BARS"],
            base_profile_values["campaign_hold_min_bars"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_TIME_BREAK_EVEN_FLOOR_BARS"],
            base_profile_values["time_break_even_floor_bars"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_GREEN_CANDLE_TAKE_MIN_PROFIT_BPS"],
            base_profile_values["green_candle_take_min_profit_bps"],
        )
        self.assertLess(
            report["parameter_overrides"]["ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS"],
            base_profile_values["late_entry_block_context_range_pos"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_LATE_ENTRY_BLOCK_MAX_CONTEXT_DRAWDOWN_BPS"],
            base_profile_values["late_entry_block_max_context_drawdown_bps"],
        )
        self.assertLess(
            report["parameter_overrides"]["ROTATION_LATE_ENTRY_BLOCK_MIN_TREND_RETURN_BPS"],
            base_profile_values["late_entry_block_min_trend_return_bps"],
        )
        self.assertGreater(
            report["parameter_overrides"]["ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WEAK_EXIT"],
            base_profile_values["reentry_cooldown_bars_after_weak_exit"],
        )
        self.assertGreater(report["missed_entry_bias"], 0.3)
        self.assertGreater(report["late_entry_bias"], 0.6)
        self.assertIn("late_entry_top_chase_detected", report["reason"])
        self.assertIn("gates_too_strict_detected", report["reason"])
        self.assertEqual(report["exit_problem_symbols"], ["SENT"])
        self.assertGreater(report["failed_start_recovery_rate"], 0.5)
        self.assertGreater(report["hold_opportunity_rate"], 0.5)

    def test_keeps_current_profile_when_switch_evidence_is_too_thin(self) -> None:
        rows = _market_rows()
        candidates = [
            ShadowCandidate(
                symbol="FAST",
                ts="2026-03-12T10:00:00+00:00",
                age_sec=5.0,
                selected=False,
                watch=True,
                current_profile="scalp_guarded",
                strategy_primary="continuation",
                gate_reason="",
                open_notional=0.0,
                has_position=False,
                p_profit=0.63,
                feature_vector={},
                decision_context={},
            )
        ]
        trade_summary = TradeSummary(
            trade_count=3,
            net_pnl=-0.04,
            win_rate=0.33,
            avg_win=0.03,
            avg_loss=-0.04,
            last_exit_ts="2026-03-12T10:10:00+00:00",
            exit_reasons={"failed_start_exit": 2, "trailing_stop": 1},
            strategy_breakdown={},
            symbol_breakdown={},
            exit_path_summary={"sample_count": 3, "early_exit_rate": 0.2},
        )
        payloads = {
            "scalp_uptrend": {
                "selected": ["FAST"],
                "selected_strategy_map": {"FAST": "continuation"},
                "all_rows": rows,
                "active_candidate_count": 1,
                "selection_relaxed": False,
                "profile_values": {"entry_edge_bps": 3.5},
            },
            "scalp_guarded": {
                "selected": ["FAST"],
                "selected_strategy_map": {"FAST": "continuation"},
                "all_rows": rows,
                "active_candidate_count": 1,
                "selection_relaxed": False,
                "profile_values": {"entry_edge_bps": 4.2},
            },
        }

        report = recommend_rotation_autotune(
            current_profile="scalp_guarded",
            trade_summary=trade_summary,
            candidates=candidates,
            profile_payloads=payloads,
        )

        self.assertEqual(report["recommended_profile"], "scalp_guarded")


if __name__ == "__main__":
    unittest.main()
