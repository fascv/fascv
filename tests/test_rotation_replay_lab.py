from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import rotation_replay_lab
from trading.meta.rotation_shadow import TradeSample


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _trade_sample(
    *,
    symbol: str = "BANANAS31",
    entry_minutes_ago: int = 20,
    exit_minutes_ago: int = 15,
    buy_notional: float = 100.0,
    sell_notional: float = 98.5,
    fees: float = 0.2,
    exit_reason: str = "failed_start_exit",
    features: dict[str, float] | None = None,
    post_exit_metrics: dict[str, float] | None = None,
) -> TradeSample:
    payload = features or {
        "context_range_pos": 0.94,
        "structure_range_pos": 0.97,
        "trend_return_bps": 180.0,
        "return_bps": 18.0,
        "context_drawdown_bps": -18.0,
        "atr_bps": 55.0,
        "edge_bps_effective": 80.0,
        "expected_cost_bps": 16.0,
    }
    return TradeSample(
        symbol=symbol,
        entry_ts=_ts(entry_minutes_ago),
        exit_ts=_ts(exit_minutes_ago),
        buy_qty=10.0,
        sell_qty=10.0,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        fees=fees,
        dust_notional=0.0,
        net_pnl=sell_notional - buy_notional - fees,
        profitable=(sell_notional - buy_notional - fees) > 0.0,
        exit_reason=exit_reason,
        strategy_at_entry="breakout",
        strategy_scores={},
        features=payload,
        post_exit_metrics=post_exit_metrics or {},
    )


def _candidate_result(
    *,
    name: str,
    score: float,
    est_net_pnl: float,
    reliability_score: float,
    late_entry_losses: int = 0,
    early_exit_bugs: int = 0,
    weak_exit_reentries: int = 0,
) -> rotation_replay_lab.CandidateResult:
    return rotation_replay_lab.CandidateResult(
        name=name,
        score=score,
        reliability_score=reliability_score,
        est_net_pnl=est_net_pnl,
        net_pnl_delta=0.0,
        executed_trade_count=2,
        synthetic_trade_count=0,
        blocked_trade_count=0,
        blocked_negative_trade_count=0,
        blocked_positive_trade_count=0,
        improved_exit_count=0,
        added_trade_count=0,
        added_winner_count=0,
        remaining_late_entry_losses=late_entry_losses,
        remaining_early_exit_bugs=early_exit_bugs,
        remaining_weak_exit_reentries=weak_exit_reentries,
        reason_counts={},
        parameter_overrides={},
        notes=[],
    )


class TestRotationReplayLab(unittest.TestCase):
    def test_parse_evaluation_windows_keeps_primary_first_and_unique(self) -> None:
        windows = rotation_replay_lab._parse_evaluation_windows(
            primary_lookback_hours=24.0,
            evaluation_windows=[6.0, 24.0, 48.0, 6.0],
        )
        self.assertEqual(windows, [24.0, 6.0, 48.0])

    def test_load_live_policy_ignores_disabled_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active_file = root / "active.json"
            runtime_env_file = root / "runtime.env"
            active_file.write_text(
                json.dumps(
                    {
                        "profile": "scalp_guarded_open",
                        "risk_mode": "normal",
                        "profile_values": {
                            "entry_edge_bps": 1.9,
                            "cont_staircase_min_slope_medium_bps": 0.95,
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env_file.write_text(
                "\n".join(
                    (
                        "ROTATION_META_MODE=disabled",
                        "ROTATION_ENTRY_EDGE_BPS=12.6703",
                        "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS=1.0",
                    )
                ),
                encoding="utf-8",
            )

            policy = rotation_replay_lab._load_live_policy(
                active_file=active_file,
                runtime_env_file=runtime_env_file,
            )

        self.assertEqual(policy.source, "active_state")
        self.assertEqual(policy.values["entry_edge_bps"], 1.9)
        self.assertEqual(policy.values["cont_staircase_min_slope_medium_bps"], 0.95)

    def test_load_live_policy_applies_enabled_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            active_file = root / "active.json"
            runtime_env_file = root / "runtime.env"
            active_file.write_text(
                json.dumps(
                    {
                        "profile": "scalp_guarded_open",
                        "risk_mode": "normal",
                        "profile_values": {
                            "entry_edge_bps": 1.9,
                            "cont_staircase_min_slope_medium_bps": 0.95,
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env_file.write_text(
                "\n".join(
                    (
                        "ROTATION_META_MODE=apply",
                        "ROTATION_ENTRY_EDGE_BPS=12.6703",
                        "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS=1.0",
                    )
                ),
                encoding="utf-8",
            )

            policy = rotation_replay_lab._load_live_policy(
                active_file=active_file,
                runtime_env_file=runtime_env_file,
            )

        self.assertEqual(policy.source, "active_state+runtime_env")
        self.assertEqual(policy.values["entry_edge_bps"], 12.6703)
        self.assertEqual(policy.values["cont_staircase_min_slope_medium_bps"], 1.0)

    def test_entry_block_reason_flags_late_entry_top_zone(self) -> None:
        state = rotation_replay_lab.SymbolState()
        values = rotation_replay_lab._normalize_policy_values(
            {
                "entry_edge_bps": 1.0,
                "entry_cost_buffer_bps": 0.0,
                "entry_cost_coverage_ratio": 0.7,
                "entry_cost_roundtrip_multiplier": 2.0,
                "entry_min_atr_to_cost_ratio": 0.9,
                "late_entry_block_context_range_pos": 0.82,
                "late_entry_block_structure_range_pos": 0.94,
                "late_entry_block_max_context_drawdown_bps": 24.0,
                "late_entry_block_min_trend_return_bps": 90.0,
                "late_entry_block_min_return_bps": 8.0,
                "md_interval_seconds": 60.0,
            }
        )
        reason = rotation_replay_lab._entry_block_reason_for_features(
            features={
                "context_range_pos": 0.95,
                "structure_range_pos": 0.98,
                "trend_return_bps": 160.0,
                "return_bps": 22.0,
                "context_drawdown_bps": -14.0,
                "atr_bps": 60.0,
                "edge_bps_effective": 120.0,
            },
            expected_cost_bps=16.0,
            entry_price=1.0,
            state=state,
            values=values,
            ts=datetime.now(timezone.utc),
        )
        self.assertEqual(reason, "late_entry_top_zone")

    def test_simulate_exit_for_trade_can_hold_failed_start_longer(self) -> None:
        sample = _trade_sample(
            buy_notional=100.0,
            sell_notional=98.5,
            fees=0.2,
            exit_reason="failed_start_exit",
            post_exit_metrics={
                "post_exit_bars_15m": 1.0,
                "post_exit_close_15m_bps": 90.0,
                "post_exit_bars_30m": 1.0,
                "post_exit_close_30m_bps": 230.0,
            },
        )
        baseline = rotation_replay_lab._normalize_policy_values(
            {
                "failed_start_min_bars": 1.0,
                "failed_start_max_bars": 2.0,
                "failed_start_loss_bps": 24.0,
                "trailing_activation_bps": 14.0,
                "trailing_stop_bps": 8.0,
                "time_break_even_floor_bars": 4.0,
                "min_exit_profit_bps": 4.0,
                "campaign_hold_enabled": 0.0,
                "campaign_hold_min_bars": 0.0,
            }
        )
        candidate = rotation_replay_lab._normalize_policy_values(
            {
                "failed_start_min_bars": 4.0,
                "failed_start_max_bars": 5.0,
                "failed_start_loss_bps": 40.0,
                "trailing_activation_bps": 24.0,
                "trailing_stop_bps": 14.0,
                "time_break_even_floor_bars": 10.0,
                "min_exit_profit_bps": 12.0,
                "campaign_hold_enabled": 1.0,
                "campaign_hold_min_bars": 4.0,
            }
        )
        pnl, exit_ts, improved, exit_mode = rotation_replay_lab._simulate_exit_for_trade(
            sample,
            candidate_values=candidate,
            baseline_values=baseline,
        )
        self.assertTrue(improved)
        self.assertIsNotNone(exit_ts)
        self.assertEqual(exit_mode, "hold_30m")
        self.assertGreater(pnl, float(sample.net_pnl))

    def test_simulate_exit_for_trade_can_hold_hard_stop_loss_longer(self) -> None:
        sample = _trade_sample(
            buy_notional=100.0,
            sell_notional=99.0,
            fees=0.2,
            exit_reason="hard_stop_loss",
            post_exit_metrics={
                "post_exit_bars_15m": 1.0,
                "post_exit_mfe_15m_bps": 90.0,
                "post_exit_mae_15m_bps": -6.0,
                "post_exit_close_15m_bps": 70.0,
                "post_exit_bars_30m": 1.0,
                "post_exit_mfe_30m_bps": 240.0,
                "post_exit_close_30m_bps": 210.0,
            },
        )
        baseline = rotation_replay_lab._normalize_policy_values(
            {
                "hard_stop_loss_bps": 90.0,
                "failed_start_min_bars": 7.0,
                "failed_start_max_bars": 7.0,
                "failed_start_loss_bps": 52.0,
                "trailing_activation_bps": 36.0,
                "trailing_stop_bps": 12.0,
                "time_break_even_floor_bars": 18.0,
                "min_exit_profit_bps": 10.0,
            }
        )
        candidate = rotation_replay_lab._normalize_policy_values(
            {
                "hard_stop_loss_bps": 104.0,
                "failed_start_min_bars": 9.0,
                "failed_start_max_bars": 10.0,
                "failed_start_loss_bps": 64.0,
                "trailing_activation_bps": 42.0,
                "trailing_stop_bps": 15.0,
                "time_break_even_floor_bars": 24.0,
                "min_exit_profit_bps": 12.0,
            }
        )
        pnl, exit_ts, improved, exit_mode = rotation_replay_lab._simulate_exit_for_trade(
            sample,
            candidate_values=candidate,
            baseline_values=baseline,
        )
        self.assertTrue(improved)
        self.assertIsNotNone(exit_ts)
        self.assertEqual(exit_mode, "hold_30m")
        self.assertGreater(pnl, float(sample.net_pnl))

    def test_simulate_candidate_blocks_losing_late_entry_trade(self) -> None:
        trade = _trade_sample(
            buy_notional=100.0,
            sell_notional=98.0,
            fees=0.2,
            exit_reason="hard_stop_loss",
            features={
                "context_range_pos": 0.96,
                "structure_range_pos": 0.98,
                "trend_return_bps": 170.0,
                "return_bps": 20.0,
                "context_drawdown_bps": -18.0,
                "atr_bps": 60.0,
                "edge_bps_effective": 120.0,
                "expected_cost_bps": 15.0,
            },
        )
        baseline = rotation_replay_lab.PolicyCandidate(
            name="baseline",
            profile="scalp_guarded",
            risk_mode="",
            values=rotation_replay_lab._normalize_policy_values(
                {
                    "entry_edge_bps": 1.0,
                    "entry_cost_buffer_bps": 0.0,
                    "entry_cost_coverage_ratio": 0.7,
                    "entry_cost_roundtrip_multiplier": 2.0,
                    "entry_min_atr_to_cost_ratio": 0.8,
                    "late_entry_block_context_range_pos": 1.0,
                    "late_entry_block_structure_range_pos": 1.0,
                    "late_entry_block_max_context_drawdown_bps": 0.0,
                    "late_entry_block_min_trend_return_bps": 0.0,
                    "late_entry_block_min_return_bps": 0.0,
                    "md_interval_seconds": 60.0,
                }
            ),
            source="test",
        )
        candidate = rotation_replay_lab.PolicyCandidate(
            name="late_entry_guard",
            profile="scalp_guarded",
            risk_mode="",
            values=rotation_replay_lab._normalize_policy_values(
                {
                    **baseline.values,
                    "late_entry_block_context_range_pos": 0.82,
                    "late_entry_block_structure_range_pos": 0.94,
                    "late_entry_block_max_context_drawdown_bps": 24.0,
                    "late_entry_block_min_trend_return_bps": 90.0,
                    "late_entry_block_min_return_bps": 8.0,
                }
            ),
            source="test",
        )
        baseline_result = rotation_replay_lab._baseline_result(baseline=baseline, trades=[trade])
        result = rotation_replay_lab._simulate_candidate(
            candidate=candidate,
            baseline=baseline,
            trades=[trade],
            no_trade_samples=[],
            unit_notional=100.0,
            baseline_result=baseline_result,
        )
        self.assertEqual(result.blocked_trade_count, 1)
        self.assertEqual(result.blocked_negative_trade_count, 1)
        self.assertEqual(result.reason_counts["late_entry_top_zone"], 1)
        self.assertGreater(result.est_net_pnl, baseline_result.est_net_pnl)

    def test_technical_bug_report_detects_mid_trade_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            journal = log_dir / "journal_live_binance_test_usdc_rotation.jsonl"
            now = datetime.now(timezone.utc)
            rows = [
                {
                    "ts": (now - timedelta(minutes=9)).isoformat(),
                    "event_type": "core_reload_applied",
                    "payload": {"alpha_type": "breakout"},
                },
                {
                    "ts": (now - timedelta(minutes=8)).isoformat(),
                    "event_type": "fill",
                    "payload": {"side": "buy", "qty_btc": 1.0, "price": 1.0},
                },
                {
                    "ts": (now - timedelta(minutes=7)).isoformat(),
                    "event_type": "core_reload_applied",
                    "payload": {"alpha_type": "trend"},
                },
                {
                    "ts": (now - timedelta(minutes=6)).isoformat(),
                    "event_type": "fill",
                    "payload": {"side": "sell", "qty_btc": 1.0, "price": 1.0},
                },
            ]
            journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = rotation_replay_lab._technical_bug_report(
                log_dir=log_dir,
                lookback_hours=24.0,
                trades=[],
            )
            self.assertEqual(report["mid_trade_reload_count"], 1)
            self.assertEqual(report["mid_trade_reload_examples"][0]["symbol"], "TEST")

    def test_aggregate_candidate_results_accepts_stable_multi_window_improvement(self) -> None:
        baseline = rotation_replay_lab.PolicyCandidate(
            name="baseline",
            profile="scalp_guarded",
            risk_mode="",
            values=rotation_replay_lab._normalize_policy_values({"entry_edge_bps": 2.0}),
            source="test",
        )
        candidate = rotation_replay_lab.PolicyCandidate(
            name="stable",
            profile="scalp_guarded",
            risk_mode="",
            values=rotation_replay_lab._normalize_policy_values({"entry_edge_bps": 2.6}),
            source="test",
        )
        windows = [
            rotation_replay_lab.ReplayWindow(
                lookback_hours=24.0,
                trades=[],
                no_trade_samples=[],
                trade_summary=None,
                unit_notional=10.0,
                baseline_result=_candidate_result(
                    name="baseline",
                    score=-4.0,
                    est_net_pnl=-0.4,
                    reliability_score=0.40,
                    late_entry_losses=2,
                    early_exit_bugs=3,
                    weak_exit_reentries=1,
                ),
            ),
            rotation_replay_lab.ReplayWindow(
                lookback_hours=48.0,
                trades=[],
                no_trade_samples=[],
                trade_summary=None,
                unit_notional=10.0,
                baseline_result=_candidate_result(
                    name="baseline",
                    score=-3.0,
                    est_net_pnl=-0.3,
                    reliability_score=0.42,
                    late_entry_losses=2,
                    early_exit_bugs=2,
                    weak_exit_reentries=1,
                ),
            ),
            rotation_replay_lab.ReplayWindow(
                lookback_hours=72.0,
                trades=[],
                no_trade_samples=[],
                trade_summary=None,
                unit_notional=10.0,
                baseline_result=_candidate_result(
                    name="baseline",
                    score=-2.0,
                    est_net_pnl=-0.2,
                    reliability_score=0.45,
                    late_entry_losses=1,
                    early_exit_bugs=2,
                    weak_exit_reentries=1,
                ),
            ),
        ]
        aggregate = rotation_replay_lab._aggregate_candidate_results(
            candidate=candidate,
            window_results=[
                (
                    windows[0],
                    _candidate_result(
                        name="stable",
                        score=-1.5,
                        est_net_pnl=-0.1,
                        reliability_score=0.51,
                        late_entry_losses=1,
                        early_exit_bugs=1,
                        weak_exit_reentries=0,
                    ),
                ),
                (
                    windows[1],
                    _candidate_result(
                        name="stable",
                        score=-0.8,
                        est_net_pnl=-0.05,
                        reliability_score=0.55,
                        late_entry_losses=1,
                        early_exit_bugs=1,
                        weak_exit_reentries=0,
                    ),
                ),
                (
                    windows[2],
                    _candidate_result(
                        name="stable",
                        score=-0.3,
                        est_net_pnl=-0.02,
                        reliability_score=0.59,
                        late_entry_losses=0,
                        early_exit_bugs=1,
                        weak_exit_reentries=0,
                    ),
                ),
            ],
            primary_lookback_hours=24.0,
            baseline=baseline,
        )
        self.assertTrue(aggregate.accepted)
        self.assertEqual(aggregate.improving_window_count, 3)
        self.assertEqual(aggregate.worsening_window_count, 0)
        self.assertEqual(aggregate.acceptance_reasons, ["stable_multi_window_improvement"])

    def test_aggregate_candidate_results_rejects_worsening_window_mix(self) -> None:
        baseline = rotation_replay_lab.PolicyCandidate(
            name="baseline",
            profile="scalp_guarded",
            risk_mode="",
            values=rotation_replay_lab._normalize_policy_values({"entry_edge_bps": 2.0}),
            source="test",
        )
        candidate = rotation_replay_lab.PolicyCandidate(
            name="mixed",
            profile="scalp_guarded",
            risk_mode="",
            values=rotation_replay_lab._normalize_policy_values({"entry_edge_bps": 2.8}),
            source="test",
        )
        window_a = rotation_replay_lab.ReplayWindow(
            lookback_hours=24.0,
            trades=[],
            no_trade_samples=[],
            trade_summary=None,
            unit_notional=10.0,
            baseline_result=_candidate_result(
                name="baseline",
                score=-2.0,
                est_net_pnl=-0.2,
                reliability_score=0.45,
                late_entry_losses=1,
                early_exit_bugs=1,
                weak_exit_reentries=0,
            ),
        )
        window_b = rotation_replay_lab.ReplayWindow(
            lookback_hours=48.0,
            trades=[],
            no_trade_samples=[],
            trade_summary=None,
            unit_notional=10.0,
            baseline_result=_candidate_result(
                name="baseline",
                score=-1.5,
                est_net_pnl=-0.12,
                reliability_score=0.47,
                late_entry_losses=1,
                early_exit_bugs=1,
                weak_exit_reentries=0,
            ),
        )
        aggregate = rotation_replay_lab._aggregate_candidate_results(
            candidate=candidate,
            window_results=[
                (
                    window_a,
                    _candidate_result(
                        name="mixed",
                        score=-0.9,
                        est_net_pnl=-0.05,
                        reliability_score=0.54,
                        late_entry_losses=0,
                        early_exit_bugs=1,
                        weak_exit_reentries=0,
                    ),
                ),
                (
                    window_b,
                    _candidate_result(
                        name="mixed",
                        score=-2.6,
                        est_net_pnl=-0.25,
                        reliability_score=0.39,
                        late_entry_losses=2,
                        early_exit_bugs=2,
                        weak_exit_reentries=1,
                    ),
                ),
            ],
            primary_lookback_hours=24.0,
            baseline=baseline,
        )
        self.assertFalse(aggregate.accepted)
        self.assertEqual(aggregate.worsening_window_count, 1)
        self.assertIn("too_many_worsening_windows", aggregate.acceptance_reasons)


if __name__ == "__main__":
    unittest.main()
