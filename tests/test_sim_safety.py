import unittest
from datetime import datetime, timezone

from trading.execution.state import StateManager
from trading.order.builder import OrderBuilder, OrderConfig
from trading.risk.sizing import RiskConfig, RiskManager
from trading.types import Features, Fill, GateDecision, RiskDecision


class TestSimSafety(unittest.TestCase):
    def test_exit_bypass_gate_allows_flatten(self) -> None:
        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=50.0,
            cooldown_bars=1,
            allow_short=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        from trading.types import AccountState

        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=0.01,
            avg_entry_price=50000.0,
            realized_pnl_eur=0.0,
            equity_eur=500.0,
            peak_equity_eur=500.0,
            drawdown_pct=0.0,
            day_start_equity_eur=500.0,
        )
        features = Features(ts=ts, values={"price": 50000.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        decision = rm.decide(state, features, gate, predicted_edge_bps=-1.0)
        self.assertTrue(decision.allow)
        self.assertEqual(decision.target_position_btc, 0.0)
        self.assertEqual(decision.reason, "edge_exit")

    def test_order_builder_clamps_buy_qty_includes_fee(self) -> None:
        ob = OrderBuilder(
            OrderConfig(
                order_type="market",
                post_only=False,
                limit_offset_bps=0.0,
                min_trade_btc=0.00000001,
                slice_count=1,
                cycle_trade_eur=20.0,
            )
        )
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        risk = RiskDecision(ts=ts, allow=True, target_position_btc=1.0, reason=None, cooldown_remaining=0)
        orders = ob.build(
            risk,
            current_position_btc=0.0,
            price=100.0,
            cash_eur=20.0,
            buy_fee_bps=26.0,
            buy_price_buffer_bps=0.0,
        )
        self.assertEqual(len(orders), 1)
        qty = float(orders[0].qty_btc)
        fee = qty * 100.0 * 26.0 / 10000.0
        self.assertLessEqual(qty * 100.0 + fee, 20.0 + 1e-9)

    def test_state_manager_realized_pnl_includes_fees(self) -> None:
        sm = StateManager(starting_cash_eur=200.0)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        sm.apply_fill(Fill(ts=ts, side="buy", qty_btc=1.0, price=100.0, fee_eur=1.0))
        sm.apply_fill(Fill(ts=ts, side="sell", qty_btc=1.0, price=110.0, fee_eur=1.1))
        pos = sm.position
        # Net: buy spends 101.0, sell receives 108.9 => +7.9 realized.
        self.assertAlmostEqual(pos.realized_pnl_eur, 7.9, places=6)
        self.assertAlmostEqual(pos.cash_eur, 207.9, places=6)
        self.assertEqual(pos.position_btc, 0.0)
        self.assertEqual(pos.avg_entry_price, 0.0)

    def test_entry_edge_threshold_blocks_weak_entries(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=5.0,
            exit_edge_bps=0.0,
            min_hold_bars=0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        decision = rm.decide(state, features, gate, predicted_edge_bps=3.0)
        self.assertTrue(decision.allow)
        self.assertEqual(decision.target_position_btc, 0.0)
        self.assertEqual(decision.reason, "edge_below_entry")

    def test_min_hold_bars_delays_exit(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            exit_edge_bps=0.0,
            min_hold_bars=3,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        d1 = rm.decide(state, features, gate, predicted_edge_bps=-2.0)
        d2 = rm.decide(state, features, gate, predicted_edge_bps=-2.0)
        d3 = rm.decide(state, features, gate, predicted_edge_bps=-2.0)

        self.assertEqual(d1.reason, "hold_min_bars")
        self.assertEqual(d2.reason, "hold_min_bars")
        self.assertEqual(d1.target_position_btc, 1.0)
        self.assertEqual(d2.target_position_btc, 1.0)
        self.assertEqual(d3.reason, "edge_exit")
        self.assertEqual(d3.target_position_btc, 0.0)

    def test_break_even_filter_delays_exit_until_price_covers_costs(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            exit_edge_bps=0.0,
            min_hold_bars=0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            require_break_even_for_exit=True,
            min_exit_profit_bps=5.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        # Need >= 26 bps expected exit costs + 5 bps buffer => >= 100.31 price.
        low_features = Features(ts=ts, values={"price": 100.2, "atr_bps": 10.0})
        d_low = rm.decide(state, low_features, gate, predicted_edge_bps=-2.0, expected_cost_bps=26.0)
        self.assertTrue(d_low.allow)
        self.assertEqual(d_low.target_position_btc, 1.0)
        self.assertEqual(d_low.reason, "wait_break_even")

        high_features = Features(ts=ts, values={"price": 100.4, "atr_bps": 10.0})
        d_high = rm.decide(state, high_features, gate, predicted_edge_bps=-2.0, expected_cost_bps=26.0)
        self.assertTrue(d_high.allow)
        self.assertEqual(d_high.target_position_btc, 0.0)
        self.assertEqual(d_high.reason, "edge_exit")

    def test_failed_start_min_bars_blocks_immediate_failed_start_exit(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            failed_start_exit_enabled=True,
            failed_start_min_bars=3,
            failed_start_max_bars=5,
            failed_start_min_rebound_bps=12.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        features = Features(
            ts=ts,
            values={
                "price": 99.75,
                "atr_bps": 10.0,
                "alpha_continuation_await_liftoff": 1.0,
                "alpha_continuation_armed": 0.0,
                "alpha_up_structure": 0.0,
                "alpha_down_structure": 0.0,
                "alpha_active_leg_rise": 0.0,
                "alpha_structure_range_pos": 0.95,
                "alpha_structure_drawdown_from_peak_bps": 5.0,
                "trend_return_bps": 18.0,
            },
        )

        d1 = rm.decide(state, features, gate, predicted_edge_bps=1.0)
        d2 = rm.decide(state, features, gate, predicted_edge_bps=1.0)
        d3 = rm.decide(state, features, gate, predicted_edge_bps=1.0)

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d1.target_position_btc, 0.0)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertEqual(d3.reason, "failed_start_exit")

    def test_failed_start_runner_waits_for_second_red_close(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            failed_start_exit_enabled=True,
            failed_start_min_bars=2,
            failed_start_max_bars=5,
            failed_start_min_rebound_bps=40.0,
            failed_start_loss_bps=15.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 13, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        strong_runner = {
            "atr_bps": 10.0,
            "alpha_continuation_await_liftoff": 1.0,
            "alpha_continuation_armed": 0.0,
            "alpha_up_structure": 1.0,
            "alpha_down_structure": 0.0,
            "alpha_active_leg_rise": 1.0,
            "alpha_structure_range_pos": 0.55,
            "alpha_structure_drawdown_from_peak_bps": 45.0,
            "alpha_recent_bias_bps": 18.0,
            "trend_return_bps": 60.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 100.25}),
            gate,
            predicted_edge_bps=8.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 99.85}),
            gate,
            predicted_edge_bps=8.0,
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 99.75}),
            gate,
            predicted_edge_bps=8.0,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertEqual(d3.reason, "failed_start_exit")

    def test_failed_start_continuation_grace_blocks_one_bar_roundtrip(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            failed_start_exit_enabled=True,
            failed_start_min_bars=1,
            failed_start_max_bars=2,
            failed_start_min_rebound_bps=30.0,
            failed_start_loss_bps=26.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        continuation_pullback = {
            "atr_bps": 22.7,
            "alpha_continuation_await_liftoff": 1.0,
            "alpha_continuation_armed": 0.0,
            "alpha_up_structure": 1.0,
            "alpha_down_structure": 0.0,
            "alpha_active_leg_rise": 1.0,
            "alpha_structure_range_pos": 0.955,
            "alpha_structure_drawdown_from_peak_bps": 0.0,
            "trend_return_bps": 17.5,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**continuation_pullback, "price": 99.72}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=22.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**continuation_pullback, "price": 99.60}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=22.0,
        )

        self.assertIsNone(d1.reason)
        self.assertGreater(d1.target_position_btc, 0.0)
        self.assertEqual(d2.reason, "failed_start_exit")

    def test_failed_start_breakout_grace_waits_for_confirmed_downside_break(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            failed_start_exit_enabled=True,
            failed_start_min_bars=3,
            failed_start_max_bars=3,
            failed_start_min_rebound_bps=30.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        breakout_pullback = {
            "atr_bps": 12.0,
            "trend_return_bps": 140.0,
            "context_range_pos": 0.76,
            "alpha_breakout_state_up": 0.0,
            "alpha_breakout_state_down": 0.0,
            "alpha_breakout_up_bps": -14.0,
            "alpha_breakout_down_bps": 0.5,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**breakout_pullback, "price": 99.96}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**breakout_pullback, "price": 99.91}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**breakout_pullback, "price": 99.79}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d4 = rm.decide(
            state,
            Features(
                ts=ts,
                values={
                    **breakout_pullback,
                    "price": 99.74,
                    "alpha_breakout_down_bps": -8.0,
                },
            ),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertIsNone(d3.reason)
        self.assertGreater(d3.target_position_btc, 0.0)
        self.assertEqual(d4.reason, "failed_start_exit")

    def test_failed_start_breakout_grace_does_not_hide_deep_loss(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            failed_start_exit_enabled=True,
            failed_start_min_bars=3,
            failed_start_max_bars=3,
            failed_start_min_rebound_bps=30.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        weak_breakout = {
            "atr_bps": 12.0,
            "trend_return_bps": 140.0,
            "context_range_pos": 0.76,
            "alpha_breakout_state_up": 0.0,
            "alpha_breakout_state_down": 0.0,
            "alpha_breakout_up_bps": -18.0,
            "alpha_breakout_down_bps": 0.5,
        }

        rm.decide(
            state,
            Features(ts=ts, values={**weak_breakout, "price": 99.96}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        rm.decide(
            state,
            Features(ts=ts, values={**weak_breakout, "price": 99.91}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**weak_breakout, "price": 99.66}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )

        self.assertEqual(d3.reason, "failed_start_exit")

    def test_failed_start_breakout_grace_keeps_high_rebound_runner_alive(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            failed_start_exit_enabled=True,
            failed_start_min_bars=10,
            failed_start_max_bars=12,
            failed_start_min_rebound_bps=18.0,
            failed_start_loss_bps=72.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        for idx in range(9):
            warmup = rm.decide(
                state,
                Features(
                    ts=ts,
                    values={
                        "price": 100.24 + (idx * 0.02),
                        "atr_bps": 16.0,
                        "trend_return_bps": 195.0,
                        "context_range_pos": 0.74,
                        "context_rebound_bps": 1678.0,
                        "alpha_breakout_state_up": 1.0,
                        "alpha_breakout_state_down": 0.0,
                        "alpha_breakout_up_bps": 24.0,
                        "alpha_breakout_down_bps": 10.0,
                    },
                ),
                gate,
                predicted_edge_bps=18.0,
                expected_cost_bps=14.0,
            )
            self.assertIsNone(warmup.reason)

        d10 = rm.decide(
            state,
            Features(
                ts=ts,
                values={
                    "price": 100.10,
                    "atr_bps": 16.0,
                    "trend_return_bps": 195.0,
                    "context_range_pos": 0.759,
                    "context_rebound_bps": 1678.0,
                    "alpha_breakout_state_up": 0.0,
                    "alpha_breakout_state_down": 0.0,
                    "alpha_breakout_up_bps": -28.18,
                    "alpha_breakout_down_bps": 0.47,
                },
            ),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d11 = rm.decide(
            state,
            Features(
                ts=ts,
                values={
                    "price": 99.93,
                    "atr_bps": 16.0,
                    "trend_return_bps": 182.0,
                    "context_range_pos": 0.752,
                    "context_rebound_bps": 1605.0,
                    "alpha_breakout_state_up": 0.0,
                    "alpha_breakout_state_down": 0.0,
                    "alpha_breakout_up_bps": -31.0,
                    "alpha_breakout_down_bps": -1.2,
                },
            ),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )

        self.assertIsNone(d10.reason)
        self.assertIsNone(d11.reason)
        self.assertGreater(d11.target_position_btc, 0.0)

    def test_failed_start_breakout_bottom_reclaim_grace_survives_negative_trend(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            failed_start_exit_enabled=True,
            failed_start_min_bars=3,
            failed_start_max_bars=3,
            failed_start_min_rebound_bps=30.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        bottom_reclaim_breakout = {
            "atr_bps": 12.0,
            "trend_return_bps": -135.0,
            "context_range_pos": 0.01,
            "context_rebound_bps": 15.0,
            "alpha_breakout_state_up": 0.0,
            "alpha_breakout_state_down": 0.0,
            "alpha_breakout_up_bps": 0.0,
            "alpha_breakout_down_bps": 15.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**bottom_reclaim_breakout, "price": 99.96}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**bottom_reclaim_breakout, "price": 99.91}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**bottom_reclaim_breakout, "price": 99.78}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )
        d4 = rm.decide(
            state,
            Features(ts=ts, values={**bottom_reclaim_breakout, "price": 99.65}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=14.0,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertIsNone(d3.reason)
        self.assertGreater(d3.target_position_btc, 0.0)
        self.assertEqual(d4.reason, "failed_start_exit")

    def test_over_exposure_does_not_block_exit(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=50.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=3,
            allow_short=False,
            entry_edge_bps=0.0,
            exit_edge_bps=0.0,
            min_hold_bars=0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 15, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        d = rm.decide(state, features, gate, predicted_edge_bps=-1.0, expected_cost_bps=20.0)
        self.assertTrue(d.allow)
        self.assertEqual(d.target_position_btc, 0.0)
        self.assertEqual(d.reason, "edge_exit")

    def test_dust_position_does_not_trigger_hard_take_profit(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=20.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            exit_edge_bps=-2.0,
            min_hold_bars=0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            require_break_even_for_exit=True,
            min_exit_profit_bps=10.0,
            hard_take_profit_bps=40.0,
            position_epsilon_btc=0.0005,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=39.0,
            position_btc=0.00009,  # dust below minimum tradable size
            avg_entry_price=1855.0,
            realized_pnl_eur=0.0,
            equity_eur=39.2,
            peak_equity_eur=39.2,
            drawdown_pct=0.0,
            day_start_equity_eur=39.2,
        )
        features = Features(ts=ts, values={"price": 1865.0, "atr_bps": 20.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        d = rm.decide(state, features, gate, predicted_edge_bps=12.0, expected_cost_bps=10.0)
        self.assertTrue(d.allow)
        self.assertIsNone(d.reason)
        self.assertGreater(d.target_position_btc, 0.0)

    def test_hard_take_profit_only_in_range_regime(self) -> None:
        from trading.types import AccountState

        base_cfg = dict(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            hard_take_profit_bps=40.0,
            hard_take_profit_only_in_range=True,
        )
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(ts=ts, values={"price": 101.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        rm_range = RiskManager(RiskConfig(**base_cfg))
        d_range = rm_range.decide(
            state,
            features,
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=10.0,
            regime="range",
        )
        self.assertEqual(d_range.reason, "hard_take_profit")
        self.assertEqual(d_range.target_position_btc, 0.0)

        rm_breakout = RiskManager(RiskConfig(**base_cfg))
        d_breakout = rm_breakout.decide(
            state,
            features,
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=10.0,
            regime="breakout",
        )
        self.assertNotEqual(d_breakout.reason, "hard_take_profit")
        self.assertGreater(d_breakout.target_position_btc, 0.0)

    def test_hard_stop_loss_exits_without_break_even(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            require_break_even_for_exit=True,
            hard_stop_loss_bps=100.0,  # 1%
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        features = Features(ts=ts, values={"price": 98.9, "atr_bps": 10.0})
        d = rm.decide(
            state,
            features,
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=20.0,
            regime="trend",
        )
        self.assertEqual(d.reason, "hard_stop_loss")
        self.assertEqual(d.target_position_btc, 0.0)

    def test_trailing_stop_arms_after_profit_and_exits_on_pullback(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            hard_take_profit_bps=0.0,
            trailing_stop_enabled=True,
            trailing_activation_bps=20.0,
            trailing_stop_bps=50.0,
            trailing_stop_atr_mult=0.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        d1 = rm.decide(
            state,
            Features(ts=ts, values={"price": 100.05, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        self.assertIsNone(d1.reason)
        self.assertGreater(d1.target_position_btc, 0.0)

        d2 = rm.decide(
            state,
            Features(ts=ts, values={"price": 101.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)

        d3 = rm.decide(
            state,
            Features(ts=ts, values={"price": 100.4, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        self.assertEqual(d3.reason, "trailing_stop")
        self.assertEqual(d3.target_position_btc, 0.0)

    def test_time_break_even_floor_runner_waits_for_second_red_close(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            min_exit_profit_bps=10.0,
            exit_edge_bps=-100.0,
            time_break_even_floor_enabled=True,
            time_break_even_floor_bars=2,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 13, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        strong_runner = {
            "atr_bps": 10.0,
            "alpha_continuation_await_liftoff": 1.0,
            "alpha_up_structure": 1.0,
            "alpha_down_structure": 0.0,
            "alpha_active_leg_rise": 1.0,
            "alpha_structure_range_pos": 0.52,
            "alpha_recent_bias_bps": 20.0,
            "trend_return_bps": 58.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 100.28}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 100.05}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 99.98}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertEqual(d3.reason, "time_break_even_floor")

    def test_time_break_even_floor_does_not_cut_deep_loss_before_break_even(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            min_exit_profit_bps=10.0,
            time_break_even_floor_enabled=True,
            time_break_even_floor_bars=1,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 13, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        d1 = rm.decide(
            state,
            Features(ts=ts, values={"price": 99.4, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
        )

        self.assertIsNone(d1.reason)
        self.assertGreater(d1.target_position_btc, 0.0)

    def test_time_break_even_floor_waits_through_continuation_bottom_reclaim(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            min_exit_profit_bps=10.0,
            exit_edge_bps=-100.0,
            time_break_even_floor_enabled=True,
            time_break_even_floor_bars=2,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 16, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        reclaim = {
            "atr_bps": 10.8,
            "alpha_continuation_await_liftoff": 1.0,
            "alpha_continuation_armed": 0.0,
            "alpha_up_structure": 1.0,
            "alpha_down_structure": 0.0,
            "alpha_active_leg_rise": 0.0,
            "alpha_structure_range_pos": 0.02,
            "alpha_structure_slope_short_bps": 0.13,
            "alpha_structure_drawdown_from_peak_bps": 145.0,
            "alpha_recent_bias_bps": 0.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**reclaim, "price": 100.02}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=13.3,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**reclaim, "price": 99.84}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=13.3,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)

    def test_time_break_even_floor_waits_through_constructive_breakout_reclaim(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            min_exit_profit_bps=10.0,
            exit_edge_bps=-100.0,
            time_break_even_floor_enabled=True,
            time_break_even_floor_bars=2,
            trailing_activation_bps=28.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        breakout = {
            "atr_bps": 10.0,
            "alpha_breakout_state_up": 1.0,
            "alpha_breakout_state_down": 0.0,
            "alpha_breakout_up_bps": 14.0,
            "alpha_breakout_down_bps": 4.0,
            "trend_return_bps": 160.0,
            "context_rebound_bps": 440.0,
            "context_range_pos": 0.65,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**breakout, "price": 100.05}),
            gate,
            predicted_edge_bps=36.0,
            expected_cost_bps=15.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**breakout, "price": 99.92}),
            gate,
            predicted_edge_bps=34.0,
            expected_cost_bps=15.0,
        )
        d3 = rm.decide(
            state,
            Features(
                ts=ts,
                values={
                    **breakout,
                    "price": 99.84,
                    "alpha_breakout_up_bps": 0.0,
                    "alpha_breakout_down_bps": -12.0,
                    "trend_return_bps": 120.0,
                },
            ),
            gate,
            predicted_edge_bps=4.0,
            expected_cost_bps=15.0,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertEqual(d3.reason, "time_break_even_floor")

    def test_time_break_even_floor_waits_through_constructive_swing_reclaim(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            min_exit_profit_bps=10.0,
            exit_edge_bps=-100.0,
            time_break_even_floor_enabled=True,
            time_break_even_floor_bars=2,
            trailing_activation_bps=28.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        swing = {
            "atr_bps": 9.3,
            "trend_return_bps": 29.0,
            "context_rebound_bps": 740.0,
            "context_range_pos": 0.80,
            "spread_bps": 18.6,
            "alpha_down_structure": 0.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**swing, "price": 100.26}),
            gate,
            predicted_edge_bps=2.0,
            expected_cost_bps=15.0,
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**swing, "price": 99.86}),
            gate,
            predicted_edge_bps=0.0,
            expected_cost_bps=15.0,
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**swing, "price": 99.80}),
            gate,
            predicted_edge_bps=-4.0,
            expected_cost_bps=15.0,
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertEqual(d3.reason, "time_break_even_floor")

    def test_trailing_stop_runner_waits_for_second_red_close(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            hard_take_profit_bps=0.0,
            trailing_stop_enabled=True,
            trailing_activation_bps=20.0,
            trailing_stop_bps=40.0,
            trailing_stop_atr_mult=0.0,
            failed_start_loss_bps=20.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 13, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        strong_runner = {
            "atr_bps": 10.0,
            "alpha_continuation_await_liftoff": 1.0,
            "alpha_up_structure": 1.0,
            "alpha_down_structure": 0.0,
            "alpha_active_leg_rise": 1.0,
            "alpha_structure_range_pos": 0.48,
            "alpha_recent_bias_bps": 19.0,
            "trend_return_bps": 65.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 100.35}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 99.98}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**strong_runner, "price": 99.92}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertEqual(d3.reason, "trailing_stop")

    def test_trailing_stop_locks_break_even_after_arming(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            hard_take_profit_bps=0.0,
            trailing_stop_enabled=True,
            trailing_activation_bps=20.0,
            trailing_stop_bps=200.0,  # intentionally wide trailing distance
            trailing_stop_atr_mult=0.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        # Arm trailing: price moved far enough above entry.
        d1 = rm.decide(
            state,
            Features(ts=ts, values={"price": 101.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        self.assertIsNone(d1.reason)
        self.assertGreater(d1.target_position_btc, 0.0)

        # Even though drawdown from peak (to 99.8) is smaller than trailing_stop_bps=200,
        # break-even floor should trigger the trailing exit.
        d2 = rm.decide(
            state,
            Features(ts=ts, values={"price": 99.8, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        self.assertEqual(d2.reason, "trailing_stop")
        self.assertEqual(d2.target_position_btc, 0.0)

    def test_trailing_stop_waits_for_break_even_lock_headroom(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            hard_take_profit_bps=0.0,
            require_break_even_for_exit=True,
            min_exit_profit_bps=10.0,
            trailing_stop_enabled=True,
            trailing_activation_bps=18.0,
            trailing_stop_bps=9.0,
            trailing_stop_atr_mult=0.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        d1 = rm.decide(
            state,
            Features(ts=ts, values={"price": 100.385, "atr_bps": 25.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=22.0,
            regime="trend",
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={"price": 100.046, "atr_bps": 25.0}),
            gate,
            predicted_edge_bps=10.0,
            expected_cost_bps=22.0,
            regime="trend",
        )

        self.assertIsNone(d1.reason)
        self.assertGreater(d1.target_position_btc, 0.0)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)

    def test_trailing_stop_waits_for_early_rebound_slow_start(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            hard_take_profit_bps=0.0,
            trailing_stop_enabled=True,
            trailing_activation_bps=18.0,
            trailing_stop_bps=9.0,
            trailing_stop_atr_mult=0.0,
            failed_start_loss_bps=22.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        early_rebound = {
            "atr_bps": 8.0,
            "alpha_swing_micro_valley_rebound": 1.0,
            "alpha_up_structure": 1.0,
            "alpha_down_structure": 0.0,
            "alpha_active_leg_rise": 1.0,
            "alpha_structure_range_pos": 0.24,
            "alpha_recent_bias_bps": 6.0,
            "trend_return_bps": 26.0,
        }

        d1 = rm.decide(
            state,
            Features(ts=ts, values={**early_rebound, "price": 100.19}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        d2 = rm.decide(
            state,
            Features(ts=ts, values={**early_rebound, "price": 100.01}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        d3 = rm.decide(
            state,
            Features(ts=ts, values={**early_rebound, "price": 100.36}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        d4 = rm.decide(
            state,
            Features(ts=ts, values={**early_rebound, "price": 100.18}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
            regime="trend",
        )
        d5 = rm.decide(
            state,
            Features(ts=ts, values={**early_rebound, "price": 100.11}),
            gate,
            predicted_edge_bps=8.0,
            expected_cost_bps=0.0,
            regime="trend",
        )

        self.assertIsNone(d1.reason)
        self.assertIsNone(d2.reason)
        self.assertGreater(d2.target_position_btc, 0.0)
        self.assertIsNone(d3.reason)
        self.assertIsNone(d4.reason)
        self.assertGreater(d4.target_position_btc, 0.0)
        self.assertEqual(d5.reason, "trailing_stop")

    def test_profit_roll_exit_locks_peak_share_above_cost_floor(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=20.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            full_position_only=True,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            min_exit_profit_bps=14.0,
            profit_roll_exit_enabled=True,
            profit_roll_arm_eur=0.10,
            profit_roll_retrace_eur=0.0,
            profit_roll_retrace_pct=50.0,
            profit_roll_min_retrace_eur=0.02,
            profit_roll_min_keep_profit_bps=2.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=20.0,
            avg_entry_price=1.0,
            realized_pnl_eur=0.0,
            equity_eur=20.0,
            peak_equity_eur=20.0,
            drawdown_pct=0.0,
            day_start_equity_eur=20.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        d_peak = rm.decide(
            state,
            Features(ts=ts, values={"price": 1.0060, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=14.0,
            regime="trend",
        )
        d_tiny_pullback = rm.decide(
            state,
            Features(ts=ts, values={"price": 1.0058, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=14.0,
            regime="trend",
        )
        d_roll = rm.decide(
            state,
            Features(ts=ts, values={"price": 1.0030, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=14.0,
            regime="trend",
        )

        self.assertEqual(d_peak.reason, "hold_full_position")
        self.assertEqual(d_tiny_pullback.reason, "hold_full_position")
        self.assertEqual(d_roll.reason, "profit_roll_exit")
        self.assertEqual(d_roll.target_position_btc, 0.0)

    def test_profit_roll_exit_respects_cost_floor(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=20.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            full_position_only=True,
            use_vol_scaling=False,
            use_gate_size_factor=False,
            exit_edge_bps=-100.0,
            profit_roll_exit_enabled=True,
            profit_roll_arm_eur=0.10,
            profit_roll_retrace_eur=0.0,
            profit_roll_retrace_pct=50.0,
            profit_roll_min_retrace_eur=0.02,
            profit_roll_min_keep_profit_bps=2.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=20.0,
            avg_entry_price=1.0,
            realized_pnl_eur=0.0,
            equity_eur=20.0,
            peak_equity_eur=20.0,
            drawdown_pct=0.0,
            day_start_equity_eur=20.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        rm.decide(
            state,
            Features(ts=ts, values={"price": 1.0060, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=14.0,
            regime="trend",
        )
        d_below_floor = rm.decide(
            state,
            Features(ts=ts, values={"price": 1.0015, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=14.0,
            regime="trend",
        )

        self.assertEqual(d_below_floor.reason, "hold_full_position")
        self.assertEqual(d_below_floor.target_position_btc, state.position_btc)

    def test_entry_requires_cost_buffer(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=5.0,
            entry_cost_buffer_bps=4.0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        # expected_cost + buffer = 10 + 4 => 14 bps required (higher than entry_edge_bps=5)
        d_low = rm.decide(state, features, gate, predicted_edge_bps=13.0, expected_cost_bps=10.0)
        self.assertEqual(d_low.reason, "edge_below_entry")
        self.assertEqual(d_low.target_position_btc, 0.0)

        d_high = rm.decide(state, features, gate, predicted_edge_bps=16.0, expected_cost_bps=10.0)
        self.assertIsNone(d_high.reason)
        self.assertGreater(d_high.target_position_btc, 0.0)

    def test_micro_swing_rebound_relaxes_atr_requirement(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=1.7,
            entry_cost_coverage_ratio=0.47,
            entry_min_atr_to_cost_ratio=0.80,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=2000.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=2000.0,
            peak_equity_eur=2000.0,
            drawdown_pct=0.0,
            day_start_equity_eur=2000.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        features = Features(
            ts=ts,
            values={
                "price": 100.0,
                "atr_bps": 6.5,
                "spread_bps": 4.3,
                "context_range_pos": 0.34,
                "trend_return_bps": 88.0,
                "alpha_swing_micro_valley_rebound": 1.0,
            },
        )

        decision = rm.decide(
            state,
            features,
            gate,
            predicted_edge_bps=16.6,
            expected_cost_bps=12.8,
        )

        self.assertIsNone(decision.reason)
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_micro_swing_rebound_relaxes_entry_cost_coverage(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=1.7,
            entry_cost_coverage_ratio=0.47,
            entry_min_atr_to_cost_ratio=0.80,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=2000.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=2000.0,
            peak_equity_eur=2000.0,
            drawdown_pct=0.0,
            day_start_equity_eur=2000.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        features = Features(
            ts=ts,
            values={
                "price": 100.0,
                "atr_bps": 20.0,
                "spread_bps": 4.3,
                "context_range_pos": 0.34,
                "trend_return_bps": 88.0,
                "alpha_swing_micro_valley_rebound": 1.0,
            },
        )

        decision = rm.decide(
            state,
            features,
            gate,
            predicted_edge_bps=5.2,
            expected_cost_bps=12.0,
        )

        self.assertIsNone(decision.reason)
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_micro_swing_rebound_relief_stays_blocked_on_wide_spread(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=1.7,
            entry_cost_coverage_ratio=0.47,
            entry_min_atr_to_cost_ratio=0.80,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=2000.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=2000.0,
            peak_equity_eur=2000.0,
            drawdown_pct=0.0,
            day_start_equity_eur=2000.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        features = Features(
            ts=ts,
            values={
                "price": 100.0,
                "atr_bps": 6.5,
                "spread_bps": 13.0,
                "context_range_pos": 0.34,
                "trend_return_bps": 88.0,
                "alpha_swing_micro_valley_rebound": 1.0,
            },
        )

        decision = rm.decide(
            state,
            features,
            gate,
            predicted_edge_bps=16.6,
            expected_cost_bps=12.8,
        )

        self.assertEqual(decision.reason, "atr_below_entry_costs")
        self.assertEqual(decision.target_position_btc, 0.0)

    def test_late_entry_top_zone_blocks_flat_entry(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            late_entry_block_context_range_pos=0.84,
            late_entry_block_structure_range_pos=0.97,
            late_entry_block_max_context_drawdown_bps=20.0,
            late_entry_block_min_trend_return_bps=90.0,
            late_entry_block_min_return_bps=10.0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(
            ts=ts,
            values={
                "price": 100.0,
                "atr_bps": 10.0,
                "context_range_pos": 0.93,
                "alpha_structure_range_pos": 0.99,
                "context_drawdown_bps": 12.0,
                "trend_return_bps": 120.0,
                "return_bps": 18.0,
            },
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        decision = rm.decide(state, features, gate, predicted_edge_bps=40.0, expected_cost_bps=10.0)
        self.assertEqual(decision.reason, "late_entry_top_zone")
        self.assertEqual(decision.target_position_btc, 0.0)

    def test_late_entry_block_allows_entry_after_pullback(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            late_entry_block_context_range_pos=0.84,
            late_entry_block_structure_range_pos=0.97,
            late_entry_block_max_context_drawdown_bps=20.0,
            late_entry_block_min_trend_return_bps=90.0,
            late_entry_block_min_return_bps=10.0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(
            ts=ts,
            values={
                "price": 100.0,
                "atr_bps": 10.0,
                "context_range_pos": 0.93,
                "alpha_structure_range_pos": 0.99,
                "context_drawdown_bps": 32.0,
                "trend_return_bps": 120.0,
                "return_bps": 18.0,
            },
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        decision = rm.decide(state, features, gate, predicted_edge_bps=40.0, expected_cost_bps=10.0)
        self.assertIsNone(decision.reason)
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_staircase_override_relaxes_late_entry_and_peak_guards(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            late_entry_block_context_range_pos=0.84,
            late_entry_block_structure_range_pos=0.97,
            late_entry_block_max_context_drawdown_bps=20.0,
            late_entry_block_min_trend_return_bps=90.0,
            late_entry_block_min_return_bps=10.0,
            override_max_structure_range_pos=0.985,
            override_min_drawdown_from_peak_bps=6.0,
            override_min_drawdown_to_cost_ratio=0.8,
            override_min_slope_short_bps=0.35,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(
            ts=ts,
            values={
                "price": 100.0,
                "atr_bps": 10.0,
                "context_range_pos": 0.93,
                "alpha_structure_range_pos": 0.99,
                "context_drawdown_bps": 12.0,
                "alpha_structure_drawdown_from_peak_bps": 5.0,
                "alpha_structure_slope_short_bps": 0.1,
                "trend_return_bps": 120.0,
                "return_bps": 18.0,
                "alpha_staircase_override": 1.0,
            },
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        decision = rm.decide(state, features, gate, predicted_edge_bps=40.0, expected_cost_bps=10.0)
        self.assertIsNone(decision.reason)
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_staircase_override_allows_near_peak_when_edge_clearly_covers_costs(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            override_max_structure_range_pos=0.985,
            override_min_drawdown_from_peak_bps=6.0,
            override_min_drawdown_to_cost_ratio=0.8,
            override_min_slope_short_bps=0.35,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        decision = rm.decide(
            state,
            Features(
                ts=ts,
                values={
                    "price": 100.0,
                    "atr_bps": 10.0,
                    "context_range_pos": 0.96,
                    "alpha_structure_range_pos": 0.9991,
                    "alpha_structure_drawdown_from_peak_bps": 0.6,
                    "alpha_structure_slope_short_bps": 0.05,
                    "alpha_staircase_override": 1.0,
                },
            ),
            gate,
            predicted_edge_bps=24.0,
            expected_cost_bps=10.0,
        )
        self.assertIsNone(decision.reason)
        self.assertGreater(decision.target_position_btc, 0.0)

    def test_reentry_move_threshold_blocks_then_allows(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            reentry_min_move_bps=30.0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        # First call: we were long and are now flat => records exit anchor near 100.0.
        state_flat_after_exit = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        rm._last_position_sign = 1
        d_block = rm.decide(
            state_flat_after_exit,
            Features(ts=ts, values={"price": 100.1, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=10.0,
        )
        self.assertEqual(d_block.reason, "reentry_move_too_small")
        self.assertEqual(d_block.target_position_btc, 0.0)

        d_allow = rm.decide(
            state_flat_after_exit,
            Features(ts=ts, values={"price": 100.5, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=10.0,
        )
        self.assertIsNone(d_allow.reason)
        self.assertGreater(d_allow.target_position_btc, 0.0)

    def test_staircase_override_uses_shallower_reentry_threshold(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            reentry_min_move_bps=75.0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        rm_block = RiskManager(cfg)
        rm_block._last_long_exit_price = 100.0
        d_block = rm_block.decide(
            state,
            Features(ts=ts, values={"price": 100.35, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=10.0,
        )
        self.assertEqual(d_block.reason, "reentry_move_too_small")
        self.assertEqual(d_block.target_position_btc, 0.0)

        rm_allow = RiskManager(cfg)
        rm_allow._last_long_exit_price = 100.0
        d_allow = rm_allow.decide(
            state,
            Features(
                ts=ts,
                values={
                    "price": 100.35,
                    "atr_bps": 10.0,
                    "alpha_staircase_override": 1.0,
                },
            ),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=10.0,
        )
        self.assertIsNone(d_allow.reason)
        self.assertGreater(d_allow.target_position_btc, 0.0)

    def test_staircase_override_allows_tighter_reentry_move_when_edge_is_strong(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            reentry_min_move_bps=40.0,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        rm_block = RiskManager(cfg)
        rm_block._last_long_exit_price = 100.0
        d_block = rm_block.decide(
            state,
            Features(
                ts=ts,
                values={
                    "price": 100.10,
                    "atr_bps": 10.0,
                    "alpha_staircase_override": 1.0,
                },
            ),
            gate,
            predicted_edge_bps=20.0,
            expected_cost_bps=10.0,
        )
        self.assertEqual(d_block.reason, "reentry_move_too_small")
        self.assertEqual(d_block.target_position_btc, 0.0)

        rm_allow = RiskManager(cfg)
        rm_allow._last_long_exit_price = 100.0
        d_allow = rm_allow.decide(
            state,
            Features(
                ts=ts,
                values={
                    "price": 100.12,
                    "atr_bps": 10.0,
                    "alpha_staircase_override": 1.0,
                },
            ),
            gate,
            predicted_edge_bps=28.0,
            expected_cost_bps=10.0,
        )
        self.assertIsNone(d_allow.reason)
        self.assertGreater(d_allow.target_position_btc, 0.0)

    def test_trailing_stop_arms_three_bar_reentry_cooldown(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            trailing_stop_enabled=True,
            trailing_activation_bps=50.0,
            trailing_stop_bps=40.0,
            trailing_stop_atr_mult=0.0,
            reentry_cooldown_bars_after_trailing_stop=3,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        state_long = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        rm.decide(
            state_long,
            Features(ts=ts, values={"price": 101.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=100.0,
            expected_cost_bps=0.0,
        )
        rm.decide(
            state_long,
            Features(ts=ts, values={"price": 101.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=100.0,
            expected_cost_bps=0.0,
        )
        d_exit = rm.decide(
            state_long,
            Features(ts=ts, values={"price": 100.55, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=100.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d_exit.reason, "trailing_stop")

        state_flat = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        d1 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.60, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=80.0,
            expected_cost_bps=0.0,
        )
        d2 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.70, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=80.0,
            expected_cost_bps=0.0,
        )
        d3 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.80, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=80.0,
            expected_cost_bps=0.0,
        )
        d4 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.90, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=80.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d1.reason, "reentry_cooldown")
        self.assertEqual(d2.reason, "reentry_cooldown")
        self.assertEqual(d3.reason, "reentry_cooldown")
        self.assertIsNone(d4.reason)
        self.assertGreater(d4.target_position_btc, 0.0)

    def test_time_break_even_floor_arms_weak_exit_reentry_cooldown(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            min_exit_profit_bps=0.0,
            time_break_even_floor_enabled=True,
            time_break_even_floor_bars=1,
            reentry_cooldown_bars_after_weak_exit=3,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        state_long = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        d_exit = rm.decide(
            state_long,
            Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d_exit.reason, "time_break_even_floor")

        state_flat = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        d1 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        d2 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.1, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        d3 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.2, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        d4 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.3, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d1.reason, "reentry_cooldown")
        self.assertEqual(d2.reason, "reentry_cooldown")
        self.assertEqual(d3.reason, "reentry_cooldown")
        self.assertIsNone(d4.reason)
        self.assertGreater(d4.target_position_btc, 0.0)

    def test_failed_start_exit_arms_weak_exit_reentry_cooldown(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            failed_start_exit_enabled=True,
            failed_start_min_bars=3,
            failed_start_max_bars=5,
            failed_start_min_rebound_bps=12.0,
            failed_start_loss_bps=20.0,
            reentry_cooldown_bars_after_weak_exit=2,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        state_long = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        weak_start_features = Features(
            ts=ts,
            values={
                "price": 99.75,
                "atr_bps": 10.0,
                "alpha_continuation_await_liftoff": 1.0,
                "alpha_continuation_armed": 0.0,
                "alpha_up_structure": 0.0,
                "alpha_down_structure": 0.0,
                "alpha_active_leg_rise": 0.0,
                "alpha_structure_range_pos": 0.95,
                "alpha_structure_drawdown_from_peak_bps": 5.0,
                "trend_return_bps": 18.0,
            },
        )

        rm.decide(state_long, weak_start_features, gate, predicted_edge_bps=1.0)
        rm.decide(state_long, weak_start_features, gate, predicted_edge_bps=1.0)
        d_exit = rm.decide(state_long, weak_start_features, gate, predicted_edge_bps=1.0)
        self.assertEqual(d_exit.reason, "failed_start_exit")

        state_flat = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        d1 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 99.8, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        d2 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 99.9, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        d3 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=60.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d1.reason, "reentry_cooldown")
        self.assertEqual(d2.reason, "reentry_cooldown")
        self.assertIsNone(d3.reason)
        self.assertGreater(d3.target_position_btc, 0.0)

    def test_short_hard_stop_whipsaw_arms_reentry_cooldown(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            hard_stop_loss_bps=100.0,
            reentry_cooldown_bars_after_whipsaw_stop_loss=3,
            reentry_whipsaw_hard_stop_max_bars=3,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        state_long = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        rm.decide(
            state_long,
            Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        d_exit = rm.decide(
            state_long,
            Features(ts=ts, values={"price": 98.9, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d_exit.reason, "hard_stop_loss")

        state_flat = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        d1 = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 99.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=80.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d1.reason, "reentry_cooldown")

    def test_repeated_short_hard_stops_arm_longer_loss_cluster_cooldown(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            hard_stop_loss_bps=100.0,
            reentry_cooldown_bars_after_whipsaw_stop_loss=3,
            reentry_whipsaw_hard_stop_max_bars=3,
            reentry_loss_cluster_window_bars=20,
            reentry_cooldown_bars_after_loss_cluster=10,
            use_vol_scaling=False,
            use_gate_size_factor=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)

        state_long = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        state_flat = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )

        rm.decide(
            state_long,
            Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        d_exit_1 = rm.decide(
            state_long,
            Features(ts=ts, values={"price": 98.9, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d_exit_1.reason, "hard_stop_loss")

        for offset in range(1, 5):
            d = rm.decide(
                state_flat,
                Features(ts=ts, values={"price": 99.0 + (offset * 0.1), "atr_bps": 10.0}),
                gate,
                predicted_edge_bps=80.0,
                expected_cost_bps=0.0,
            )
        self.assertIsNone(d.reason)

        rm.decide(
            state_long,
            Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        d_exit_2 = rm.decide(
            state_long,
            Features(ts=ts, values={"price": 98.8, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=50.0,
            expected_cost_bps=0.0,
        )
        self.assertEqual(d_exit_2.reason, "hard_stop_loss")

        blocked = []
        for offset in range(1, 11):
            d = rm.decide(
                state_flat,
                Features(ts=ts, values={"price": 99.5 + (offset * 0.1), "atr_bps": 10.0}),
                gate,
                predicted_edge_bps=80.0,
                expected_cost_bps=0.0,
            )
            blocked.append(d.reason)

        self.assertEqual(blocked[:10], ["reentry_cooldown"] * 10)
        d_allow = rm.decide(
            state_flat,
            Features(ts=ts, values={"price": 101.0, "atr_bps": 10.0}),
            gate,
            predicted_edge_bps=80.0,
            expected_cost_bps=0.0,
        )
        self.assertIsNone(d_allow.reason)

    def test_rebalance_deadband_holds_position(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=20.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            entry_edge_bps=0.0,
            rebalance_min_delta_eur=2.0,
            use_vol_scaling=False,
            use_gate_size_factor=True,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 25, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=0.1,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        # target exposure would be 20 * 0.95 => 19 EUR => 0.19 BTC; delta is 9 EUR (>2) when fully sized
        gate_big = GateDecision(ts=ts, allow=True, size_factor=0.95, reason=None)
        d_big = rm.decide(state, features, gate_big, predicted_edge_bps=50.0, expected_cost_bps=0.0)
        self.assertIsNone(d_big.reason)
        self.assertGreater(d_big.target_position_btc, state.position_btc)

        # tiny change: 20 * 0.505 => 10.1 EUR => 0.101 BTC; delta is 0.1 EUR (<2) => hold
        gate_tiny = GateDecision(ts=ts, allow=True, size_factor=0.505, reason=None)
        d_tiny = rm.decide(state, features, gate_tiny, predicted_edge_bps=50.0, expected_cost_bps=0.0)
        self.assertEqual(d_tiny.reason, "rebalance_deadband")
        self.assertEqual(d_tiny.target_position_btc, state.position_btc)

    def test_reduction_waits_for_break_even_when_enabled(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            use_vol_scaling=False,
            use_gate_size_factor=True,
            require_break_even_for_exit=True,
            min_exit_profit_bps=0.0,
            rebalance_min_delta_eur=0.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 26, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        # Strong edge keeps gate open; tiny size factor asks for a large reduction.
        gate = GateDecision(ts=ts, allow=True, size_factor=0.01, reason=None)
        features_underwater = Features(ts=ts, values={"price": 99.0, "atr_bps": 10.0})
        d_underwater = rm.decide(
            state,
            features_underwater,
            gate,
            predicted_edge_bps=40.0,
            expected_cost_bps=20.0,
            regime="trend",
        )
        self.assertEqual(d_underwater.reason, "wait_break_even_reduce")
        self.assertEqual(d_underwater.target_position_btc, state.position_btc)

    def test_full_position_only_holds_in_position_until_exit(self) -> None:
        from trading.types import AccountState

        cfg = RiskConfig(
            max_exposure_eur=1000.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=90.0,
            cooldown_bars=1,
            allow_short=False,
            full_position_only=True,
            use_vol_scaling=False,
            use_gate_size_factor=True,
            exit_edge_bps=-1.0,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 2, 26, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=0.0,
            position_btc=1.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=100.0,
            peak_equity_eur=100.0,
            drawdown_pct=0.0,
            day_start_equity_eur=100.0,
        )
        # Without full_position_only this would reduce toward a smaller target due to size_factor.
        gate = GateDecision(ts=ts, allow=True, size_factor=0.1, reason=None)
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        d_hold = rm.decide(
            state,
            features,
            gate,
            predicted_edge_bps=30.0,
            expected_cost_bps=10.0,
            regime="trend",
        )
        self.assertEqual(d_hold.reason, "hold_full_position")
        self.assertEqual(d_hold.target_position_btc, state.position_btc)

    def test_material_scale_in_resets_position_age_tracker(self) -> None:
        from trading.types import AccountState

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
        rm._trail_peak_price = 105.0
        rm._position_trough_price = 95.0
        rm._trailing_armed = True
        rm._recent_prices.extend([101.0, 100.5, 100.25])

        state = AccountState(
            ts=datetime(2026, 3, 17, 2, 52, tzinfo=timezone.utc),
            cash_eur=45.8,
            position_btc=3441.5096,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=344150.96,
            peak_equity_eur=344150.96,
            drawdown_pct=0.0,
            day_start_equity_eur=344150.96,
        )

        rm._update_position_tracker(state, price_hint=99.8)

        self.assertEqual(rm._bars_in_position, 1)
        self.assertAlmostEqual(rm._last_effective_position_btc, 3441.5096, places=6)
        self.assertAlmostEqual(rm._trail_peak_price, 100.0, places=6)
        self.assertAlmostEqual(rm._position_trough_price, 100.0, places=6)
        self.assertFalse(rm._trailing_armed)
        self.assertEqual(len(rm._recent_prices), 0)


if __name__ == "__main__":
    unittest.main()
