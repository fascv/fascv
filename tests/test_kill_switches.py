import unittest
from datetime import datetime, timezone

from trading.risk.sizing import RiskConfig, RiskManager
from trading.types import AccountState, Features, GateDecision


class TestKillSwitches(unittest.TestCase):
    def test_daily_loss_limit(self):
        cfg = RiskConfig(
            max_exposure_eur=50.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=5.0,
            max_drawdown_pct=10.0,
            cooldown_bars=3,
            allow_short=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=-6.0,
            equity_eur=94.0,
            peak_equity_eur=100.0,
            drawdown_pct=6.0,
            day_start_equity_eur=100.0,
        )
        reason = rm.update_kill_switches(state)
        self.assertEqual(reason, "daily_loss_limit")

    def test_max_drawdown(self):
        cfg = RiskConfig(
            max_exposure_eur=50.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=50.0,
            max_drawdown_pct=5.0,
            cooldown_bars=3,
            allow_short=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=90.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=-10.0,
            equity_eur=90.0,
            peak_equity_eur=100.0,
            drawdown_pct=10.0,
            day_start_equity_eur=100.0,
        )
        reason = rm.update_kill_switches(state)
        self.assertEqual(reason, "max_drawdown")

    def test_max_exposure_reduces_target_instead_of_blocking(self):
        cfg = RiskConfig(
            max_exposure_eur=50.0,
            vol_target_bps=100.0,
            daily_loss_limit_eur=1000.0,
            max_drawdown_pct=50.0,
            cooldown_bars=3,
            allow_short=False,
        )
        rm = RiskManager(cfg)
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = AccountState(
            ts=ts,
            cash_eur=100.0,
            position_btc=2.0,
            avg_entry_price=100.0,
            realized_pnl_eur=0.0,
            equity_eur=300.0,
            peak_equity_eur=300.0,
            drawdown_pct=0.0,
            day_start_equity_eur=300.0,
        )
        features = Features(ts=ts, values={"price": 100.0, "atr_bps": 10.0})
        gate = GateDecision(ts=ts, allow=True, size_factor=1.0, reason=None)
        decision = rm.decide(state, features, gate, predicted_edge_bps=10.0)
        self.assertTrue(decision.allow)
        self.assertAlmostEqual(decision.target_position_btc, 0.5, places=9)


if __name__ == "__main__":
    unittest.main()
