from __future__ import annotations

from datetime import datetime, timezone
import unittest

from trading.risk.sizing import RiskConfig, RiskManager
from trading.types import AccountState, Features, GateDecision


class TestRiskCorridorStagedMode(unittest.TestCase):
    def _config(self) -> RiskConfig:
        return RiskConfig(
            max_exposure_eur=10.0,
            vol_target_bps=200.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=100.0,
            cooldown_bars=0,
        )

    def _state(self, *, position_btc: float, price: float = 1.0, avg_entry_price: float = 1.0) -> AccountState:
        now = datetime.now(timezone.utc)
        return AccountState(
            ts=now,
            cash_eur=100.0,
            position_btc=position_btc,
            avg_entry_price=avg_entry_price,
            realized_pnl_eur=0.0,
            equity_eur=100.0 + (position_btc * price),
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )

    def _features(self, *, corridor_pos_pct: float, price: float = 1.0, entry_wait_bars: float = 6.0) -> Features:
        now = datetime.now(timezone.utc)
        return Features(
            ts=now,
            values={
                "price": price,
                "atr_bps": 0.0,
                "corridor_ready": 1.0,
                "corridor_position_pct": float(corridor_pos_pct),
                "corridor_staged_mode_enabled": 1.0,
                "corridor_staged_entry_1_pct": 10.0,
                "corridor_staged_entry_2_pct": 20.0,
                "corridor_staged_entry_3_pct": 30.0,
                "corridor_staged_entry_4_pct": 40.0,
                "corridor_staged_no_buy_above_pct": 50.0,
                "corridor_staged_exit_step_pct": 10.0,
                "corridor_staged_hysteresis_pct": 0.0,
                "corridor_staged_exit_retrace_pct": 1.0,
                "corridor_staged_entry_wait_bars": float(entry_wait_bars),
                "corridor_staged_transition_smoothing_bars": 1.0,
                "corridor_staged_require_rising": 1.0,
                "corridor_staged_profit_target_base_pct": 0.6,
            },
        )

    def test_corridor_mode_buys_immediately_after_stage_breach_even_if_gate_blocks(self) -> None:
        manager = RiskManager(self._config())
        gate_block = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=False,
            size_factor=0.0,
            reason="spread",
        )

        flat_state = self._state(position_btc=0.0)
        manager.decide(flat_state, self._features(corridor_pos_pct=55.0), gate_block, predicted_edge_bps=-50.0)
        decision = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=8.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )

        self.assertEqual(decision.reason, "corridor_stage_entry")
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_corridor_mode_exits_on_next_stage_roll(self) -> None:
        manager = RiskManager(self._config())
        gate_block = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=False,
            size_factor=0.0,
            reason="spread",
        )

        flat_state = self._state(position_btc=0.0)
        manager.decide(flat_state, self._features(corridor_pos_pct=8.0), gate_block, predicted_edge_bps=-50.0)
        manager.decide(flat_state, self._features(corridor_pos_pct=11.0), gate_block, predicted_edge_bps=-50.0)

        long_state = self._state(position_btc=10.0, price=1.01, avg_entry_price=1.0)
        hold_decision = manager.decide(
            long_state,
            self._features(corridor_pos_pct=22.0, price=1.01),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(hold_decision.reason, "corridor_stage_hold")
        long_state_retrace = self._state(position_btc=10.0, price=1.008, avg_entry_price=1.0)
        exit_decision = manager.decide(
            long_state_retrace,
            self._features(corridor_pos_pct=19.0, price=1.008),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(exit_decision.reason, "profit_roll_exit")
        self.assertEqual(exit_decision.target_position_btc, 0.0)

    def test_corridor_mode_open_position_seeds_to_next_higher_stage(self) -> None:
        manager = RiskManager(self._config())
        gate_block = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=False,
            size_factor=0.0,
            reason="spread",
        )

        long_state = self._state(position_btc=10.0, price=1.0, avg_entry_price=1.0)
        seed_decision = manager.decide(
            long_state,
            self._features(corridor_pos_pct=28.0, price=1.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(seed_decision.reason, "corridor_stage_hold")
        arm_attempt = manager.decide(
            long_state,
            self._features(corridor_pos_pct=31.0, price=1.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(arm_attempt.reason, "corridor_stage_hold")
        no_early_exit = manager.decide(
            long_state,
            self._features(corridor_pos_pct=29.0, price=1.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(no_early_exit.reason, "corridor_stage_hold")
        self.assertEqual(no_early_exit.target_position_btc, 10.0)

    def test_corridor_mode_blocks_entries_above_cap(self) -> None:
        manager = RiskManager(self._config())
        gate_allow = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=True,
            size_factor=1.0,
            reason=None,
        )
        decision = manager.decide(
            self._state(position_btc=0.0),
            self._features(corridor_pos_pct=65.0),
            gate_allow,
            predicted_edge_bps=100.0,
        )
        self.assertEqual(decision.reason, "corridor_too_high")
        self.assertEqual(decision.target_position_btc, 0.0)

    def test_corridor_mode_ignores_profit_roll_exit(self) -> None:
        cfg = self._config()
        cfg.profit_roll_exit_enabled = True
        cfg.profit_roll_arm_eur = 0.10
        cfg.profit_roll_retrace_eur = 0.0
        manager = RiskManager(cfg)
        gate_allow = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=True,
            size_factor=1.0,
            reason=None,
        )

        long_state_peak = self._state(position_btc=10.0, price=1.0200, avg_entry_price=1.0)
        hold_decision = manager.decide(
            long_state_peak,
            self._features(corridor_pos_pct=25.0, price=1.0200),
            gate_allow,
            predicted_edge_bps=20.0,
        )
        self.assertEqual(hold_decision.reason, "corridor_stage_hold")
        self.assertEqual(hold_decision.target_position_btc, 10.0)

        long_state_retrace = self._state(position_btc=10.0, price=1.0180, avg_entry_price=1.0)
        exit_decision = manager.decide(
            long_state_retrace,
            self._features(corridor_pos_pct=25.0, price=1.0180),
            gate_allow,
            predicted_edge_bps=20.0,
        )
        self.assertEqual(exit_decision.reason, "corridor_stage_hold")
        self.assertEqual(exit_decision.target_position_btc, 10.0)

    def test_profit_only_corridor_mode_defers_to_profit_roll_exit(self) -> None:
        cfg = self._config()
        cfg.profit_only_auto_exits = True
        cfg.require_break_even_for_exit = True
        cfg.profit_roll_exit_enabled = True
        cfg.profit_roll_arm_eur = 0.10
        cfg.profit_roll_retrace_eur = 0.0
        cfg.profit_roll_retrace_pct = 50.0
        cfg.profit_roll_min_retrace_eur = 0.02
        cfg.profit_roll_min_keep_profit_bps = 0.0
        manager = RiskManager(cfg)
        gate_allow = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=True,
            size_factor=1.0,
            reason=None,
        )

        long_state_peak = self._state(position_btc=10.0, price=1.0200, avg_entry_price=1.0)
        hold_decision = manager.decide(
            long_state_peak,
            self._features(corridor_pos_pct=25.0, price=1.0200),
            gate_allow,
            predicted_edge_bps=20.0,
        )
        self.assertGreater(hold_decision.target_position_btc, 0.0)

        long_state_retrace = self._state(position_btc=10.0, price=1.0090, avg_entry_price=1.0)
        exit_decision = manager.decide(
            long_state_retrace,
            self._features(corridor_pos_pct=25.0, price=1.0090),
            gate_allow,
            predicted_edge_bps=20.0,
        )
        self.assertEqual(exit_decision.reason, "profit_roll_exit")
        self.assertEqual(exit_decision.target_position_btc, 0.0)

    def test_profit_only_corridor_mode_arms_profit_roll_from_percent_sockel(self) -> None:
        cfg = self._config()
        cfg.profit_only_auto_exits = True
        cfg.require_break_even_for_exit = True
        cfg.profit_roll_exit_enabled = True
        cfg.profit_roll_arm_eur = 0.0
        cfg.profit_roll_retrace_eur = 0.0
        cfg.profit_roll_retrace_pct = 50.0
        cfg.profit_roll_min_retrace_eur = 0.02
        cfg.profit_roll_min_keep_profit_bps = 0.0
        manager = RiskManager(cfg)
        gate_allow = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=True,
            size_factor=1.0,
            reason=None,
        )

        long_state_peak = self._state(position_btc=10.0, price=1.0080, avg_entry_price=1.0)
        hold_decision = manager.decide(
            long_state_peak,
            self._features(corridor_pos_pct=25.0, price=1.0080),
            gate_allow,
            predicted_edge_bps=20.0,
        )
        self.assertGreater(hold_decision.target_position_btc, 0.0)

        long_state_retrace = self._state(position_btc=10.0, price=1.0030, avg_entry_price=1.0)
        exit_decision = manager.decide(
            long_state_retrace,
            self._features(corridor_pos_pct=25.0, price=1.0030),
            gate_allow,
            predicted_edge_bps=20.0,
        )
        self.assertEqual(exit_decision.reason, "profit_roll_exit")
        self.assertEqual(exit_decision.target_position_btc, 0.0)

    def test_corridor_mode_waits_in_10_to_20_band_before_entry(self) -> None:
        manager = RiskManager(self._config())
        gate_block = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=False,
            size_factor=0.0,
            reason="spread",
        )
        flat_state = self._state(position_btc=0.0)
        manager.decide(flat_state, self._features(corridor_pos_pct=55.0), gate_block, predicted_edge_bps=-50.0)

        first = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=18.0, entry_wait_bars=6.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(first.reason, "corridor_wait_lower_stage_window")
        self.assertEqual(first.target_position_btc, 0.0)

        for _ in range(4):
            manager.decide(
                flat_state,
                self._features(corridor_pos_pct=18.0, entry_wait_bars=6.0),
                gate_block,
                predicted_edge_bps=-50.0,
            )
        decision = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=18.0, entry_wait_bars=6.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(decision.reason, "corridor_stage_entry")
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_corridor_mode_waits_in_20_to_30_band_before_entry(self) -> None:
        manager = RiskManager(self._config())
        gate_block = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=False,
            size_factor=0.0,
            reason="spread",
        )
        flat_state = self._state(position_btc=0.0)
        manager.decide(flat_state, self._features(corridor_pos_pct=55.0), gate_block, predicted_edge_bps=-50.0)

        first = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=25.0, entry_wait_bars=3.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(first.reason, "corridor_wait_lower_stage_window")

        manager.decide(
            flat_state,
            self._features(corridor_pos_pct=25.0, entry_wait_bars=3.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        third = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=25.0, entry_wait_bars=3.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(third.reason, "corridor_stage_entry")
        self.assertGreater(third.target_position_btc, 0.0)

    def test_corridor_mode_waits_in_30_to_40_band_before_entry(self) -> None:
        manager = RiskManager(self._config())
        gate_block = GateDecision(
            ts=datetime.now(timezone.utc),
            allow=False,
            size_factor=0.0,
            reason="spread",
        )
        flat_state = self._state(position_btc=0.0)
        manager.decide(flat_state, self._features(corridor_pos_pct=55.0), gate_block, predicted_edge_bps=-50.0)

        first = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=35.0, entry_wait_bars=3.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(first.reason, "corridor_wait_lower_stage_window")

        manager.decide(
            flat_state,
            self._features(corridor_pos_pct=35.0, entry_wait_bars=3.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        third = manager.decide(
            flat_state,
            self._features(corridor_pos_pct=35.0, entry_wait_bars=3.0),
            gate_block,
            predicted_edge_bps=-50.0,
        )
        self.assertEqual(third.reason, "corridor_stage_entry")
        self.assertGreater(third.target_position_btc, 0.0)


if __name__ == "__main__":
    unittest.main()
