import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from trading.alpha.auto import AutoRegimeConfig, RegimeSwitchingAlpha
from trading.alpha.base import AlphaModel
from trading.alpha.breakout import BreakoutAlpha, BreakoutConfig
from trading.alpha.continuation import ContinuationAlpha, ContinuationConfig
from trading.alpha.factory import build_alpha_model
from trading.alpha.mean_reversion import MeanReversionAlpha, MeanReversionConfig
from trading.alpha.swing import SwingAlpha, SwingConfig
from trading.market_structure import MarketStructure
from trading.types import AlphaSignal, Features


class _FixedAlpha(AlphaModel):
    def __init__(self, edge: float, name: str):
        self.edge = float(edge)
        self.name = str(name)

    def predict(self, features: Features) -> AlphaSignal:
        return AlphaSignal(ts=features.ts, edge_bps=self.edge, p_up=None, meta={"name": self.name})


class TestAlphaSwitching(unittest.TestCase):
    def test_mean_reversion_opposes_move(self) -> None:
        model = MeanReversionAlpha(MeanReversionConfig(lookback=3, threshold_bps=0.0, scale=1.0))
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        out1 = model.predict(Features(ts=ts, values={"return_bps": 2.0}))
        out2 = model.predict(Features(ts=ts, values={"return_bps": 3.0}))
        self.assertLess(out1.edge_bps, 0.0)
        self.assertLess(out2.edge_bps, 0.0)

    def test_mean_reversion_reversal_confirmation_blocks_falling_bar(self) -> None:
        model = MeanReversionAlpha(
            MeanReversionConfig(
                lookback=2,
                threshold_bps=0.0,
                scale=1.0,
                require_reversal_confirmation=True,
                reversal_min_last_return_bps=1.0,
                reversal_min_prev_pressure_bps=2.0,
            )
        )
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"return_bps": -4.0}))
        out = model.predict(Features(ts=ts, values={"return_bps": -2.0}))
        self.assertEqual(out.edge_bps, 0.0)
        self.assertFalse(bool((out.meta or {}).get("reversal_confirmed")))
        self.assertEqual((out.meta or {}).get("reversal_reason"), "last_bar_not_up")

    def test_mean_reversion_reversal_confirmation_allows_first_upturn(self) -> None:
        model = MeanReversionAlpha(
            MeanReversionConfig(
                lookback=2,
                threshold_bps=0.0,
                scale=1.0,
                require_reversal_confirmation=True,
                reversal_min_last_return_bps=1.0,
                reversal_min_prev_pressure_bps=5.0,
            )
        )
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"return_bps": -8.0}))
        out = model.predict(Features(ts=ts, values={"return_bps": 3.0}))
        self.assertGreater(out.edge_bps, 0.0)
        self.assertTrue(bool((out.meta or {}).get("reversal_confirmed")))

    def test_breakout_up_detected(self) -> None:
        model = BreakoutAlpha(BreakoutConfig(lookback=3, trigger_bps=2.0, scale=1.0))
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(Features(ts=ts, values={"price": 100.8}))
        self.assertGreater(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "up_breakout")

    def test_breakout_blocks_late_rebound_without_volume(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                late_rebound_block_context_range_pos=0.72,
                late_rebound_block_context_rebound_bps=1400.0,
                late_rebound_block_min_volume_z=0.35,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.735,
                    "context_rebound_bps": 1720.0,
                    "volume_z": -0.15,
                },
            )
        )
        self.assertEqual(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "late_rebound_block")
        self.assertEqual((out.meta or {}).get("breakout_block_reason"), "late_rebound_low_volume")

    def test_breakout_keeps_late_rebound_with_volume_confirmation(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                late_rebound_block_context_range_pos=0.72,
                late_rebound_block_context_rebound_bps=1400.0,
                late_rebound_block_min_volume_z=0.35,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.735,
                    "context_rebound_bps": 1720.0,
                    "volume_z": 0.7,
                },
            )
        )
        self.assertGreater(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "up_breakout")

    def test_breakout_blocks_mid_rebound_without_volume(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                mid_rebound_block_context_range_pos=0.48,
                mid_rebound_block_context_rebound_bps=760.0,
                mid_rebound_block_min_volume_z=0.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.514,
                    "context_rebound_bps": 880.0,
                    "volume_z": -0.25,
                },
            )
        )
        self.assertEqual(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "mid_rebound_block")
        self.assertEqual((out.meta or {}).get("breakout_block_reason"), "mid_rebound_low_volume")

    def test_breakout_keeps_mid_rebound_with_volume_confirmation(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                mid_rebound_block_context_range_pos=0.48,
                mid_rebound_block_context_rebound_bps=760.0,
                mid_rebound_block_min_volume_z=0.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.514,
                    "context_rebound_bps": 880.0,
                    "volume_z": 0.2,
                },
            )
        )
        self.assertGreater(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "up_breakout")

    def test_breakout_blocks_thin_rebound_with_high_spread(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                thin_rebound_block_context_range_pos=0.60,
                thin_rebound_block_context_rebound_bps=400.0,
                thin_rebound_block_min_spread_bps=18.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.63,
                    "context_rebound_bps": 430.0,
                    "spread_bps": 19.2,
                    "volume_z": 4.5,
                },
            )
        )
        self.assertEqual(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "thin_rebound_spread_block")
        self.assertEqual((out.meta or {}).get("breakout_block_reason"), "thin_rebound_high_spread")

    def test_breakout_keeps_thin_rebound_when_spread_is_clean(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                thin_rebound_block_context_range_pos=0.60,
                thin_rebound_block_context_rebound_bps=400.0,
                thin_rebound_block_min_spread_bps=18.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.63,
                    "context_rebound_bps": 430.0,
                    "spread_bps": 12.0,
                    "volume_z": 4.5,
                },
            )
        )
        self.assertGreater(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "up_breakout")

    def test_breakout_blocks_bottom_countertrend_drift(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                bottom_countertrend_block_max_context_range_pos=0.22,
                bottom_countertrend_block_max_context_rebound_bps=140.0,
                bottom_countertrend_block_max_trend_return_bps=-55.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.01,
                    "context_rebound_bps": 92.0,
                    "trend_return_bps": -58.0,
                    "volume_z": -0.18,
                },
            )
        )
        self.assertEqual(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "bottom_countertrend_block")
        self.assertEqual(
            (out.meta or {}).get("breakout_block_reason"),
            "bottom_countertrend_negative_drift",
        )

    def test_breakout_keeps_bottom_reclaim_after_drift_improves(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                bottom_countertrend_block_max_context_range_pos=0.22,
                bottom_countertrend_block_max_context_rebound_bps=140.0,
                bottom_countertrend_block_max_trend_return_bps=-55.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 0.01,
                    "context_rebound_bps": 165.0,
                    "trend_return_bps": -48.0,
                    "volume_z": -0.18,
                },
            )
        )
        self.assertGreater(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "up_breakout")

    def test_breakout_blocks_top_zone_probe_without_volume(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                top_zone_block_min_context_range_pos=0.98,
                top_zone_block_max_context_rebound_bps=90.0,
                top_zone_block_min_volume_z=0.4,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 1.0,
                    "context_rebound_bps": 44.0,
                    "volume_z": -0.35,
                },
            )
        )
        self.assertEqual(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "top_zone_block")
        self.assertEqual((out.meta or {}).get("breakout_block_reason"), "top_zone_weak_volume")

    def test_breakout_keeps_top_zone_probe_with_volume_confirmation(self) -> None:
        model = BreakoutAlpha(
            BreakoutConfig(
                lookback=3,
                trigger_bps=2.0,
                scale=1.0,
                top_zone_block_min_context_range_pos=0.98,
                top_zone_block_max_context_rebound_bps=90.0,
                top_zone_block_min_volume_z=0.4,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        model.predict(Features(ts=ts, values={"price": 100.0}))
        model.predict(Features(ts=ts, values={"price": 100.3}))
        model.predict(Features(ts=ts, values={"price": 100.1}))
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "price": 100.8,
                    "context_range_pos": 1.0,
                    "context_rebound_bps": 44.0,
                    "volume_z": 0.7,
                },
            )
        )
        self.assertGreater(out.edge_bps, 0.0)
        self.assertEqual((out.meta or {}).get("breakout_state"), "up_breakout")

    def test_auto_switch_picks_breakout_regime(self) -> None:
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        model = RegimeSwitchingAlpha(
            strategies={
                "trend": _FixedAlpha(1.0, "trend"),
                "mean_reversion": _FixedAlpha(2.0, "mean_reversion"),
                "breakout": _FixedAlpha(3.0, "breakout"),
            },
            config=AutoRegimeConfig(
                lookback=4,
                trend_momentum_bps=6.0,
                range_momentum_bps=2.0,
                high_vol_atr_bps=100.0,
                low_vol_atr_bps=50.0,
                breakout_return_bps=8.0,
            ),
        )
        out = model.predict(Features(ts=ts, values={"return_bps": 12.0, "atr_bps": 40.0}))
        self.assertEqual(out.edge_bps, 3.0)
        self.assertEqual((out.meta or {}).get("active_strategy"), "breakout")

    def test_build_alpha_model_auto(self) -> None:
        cfg = {
            "alpha": {
                "type": "auto",
                "auto": {
                    "trend": {"lookback": 3, "threshold_bps": 0.5, "scale": 5.0},
                    "mean_reversion": {"lookback": 6, "threshold_bps": 1.0, "scale": 2.0},
                    "breakout": {"lookback": 8, "trigger_bps": 4.0, "scale": 2.0},
                    "regime": {
                        "lookback": 4,
                        "trend_momentum_bps": 6.0,
                        "range_momentum_bps": 2.0,
                        "high_vol_atr_bps": 120.0,
                        "low_vol_atr_bps": 60.0,
                        "breakout_return_bps": 8.0,
                    },
                },
            }
        }
        model = build_alpha_model(cfg)
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        out = model.predict(Features(ts=ts, values={"price": 100.0, "return_bps": 0.0, "atr_bps": 20.0}))
        self.assertIn("active_strategy", out.meta or {})

    def test_auto_context_pullback_switches_to_range(self) -> None:
        ts = datetime(2026, 2, 16, tzinfo=timezone.utc)
        model = RegimeSwitchingAlpha(
            strategies={
                "trend": _FixedAlpha(1.0, "trend"),
                "mean_reversion": _FixedAlpha(2.0, "mean_reversion"),
                "breakout": _FixedAlpha(3.0, "breakout"),
            },
            config=AutoRegimeConfig(
                lookback=4,
                trend_momentum_bps=6.0,
                range_momentum_bps=2.0,
                high_vol_atr_bps=120.0,
                low_vol_atr_bps=60.0,
                breakout_return_bps=12.0,
                context_pullback_bps=80.0,
                context_range_high=0.7,
                range_strategy="mean_reversion",
            ),
        )
        out = model.predict(
            Features(
                ts=ts,
                values={
                    "return_bps": 1.2,
                    "trend_return_bps": -20.0,
                    "atr_bps": 90.0,
                    "context_return_bps": -130.0,
                    "context_drawdown_bps": -95.0,
                    "context_rebound_bps": 20.0,
                    "context_range_pos": 0.4,
                },
            )
        )
        self.assertEqual((out.meta or {}).get("regime_reason"), "context_pullback_rebound")
        self.assertEqual((out.meta or {}).get("active_strategy"), "mean_reversion")

    def test_swing_alpha_detects_valley_rebound(self) -> None:
        model = SwingAlpha(
            SwingConfig(
                lookback=5,
                buy_band=0.45,
                sell_band=0.65,
                momentum_lookback=3,
                reversal_threshold_bps=0.2,
                edge_scale=2.0,
                min_range_bps=10.0,
                max_edge_bps=500.0,
            )
        )
        ts = datetime(2026, 2, 17, tzinfo=timezone.utc)
        prices = [100.0, 99.0, 98.5, 98.0, 97.8, 98.0, 98.3, 98.6]
        rets = [0.0]
        for i in range(1, len(prices)):
            rets.append((prices[i] / prices[i - 1] - 1.0) * 10000.0)
        had_rebound = False
        for px, rb in zip(prices, rets):
            out = model.predict(Features(ts=ts, values={"price": px, "return_bps": rb}))
            if (out.meta or {}).get("swing_state") == "valley_rebound" and float(out.edge_bps) > 0.0:
                had_rebound = True
        self.assertTrue(had_rebound)

    def test_build_alpha_model_swing(self) -> None:
        cfg = {
            "alpha": {
                "type": "swing",
                "swing": {
                    "lookback": 20,
                    "buy_band": 0.4,
                    "sell_band": 0.7,
                    "momentum_lookback": 6,
                    "reversal_threshold_bps": 0.4,
                    "edge_scale": 1.1,
                    "min_range_bps": 50.0,
                    "max_edge_bps": 150.0,
                },
            }
        }
        model = build_alpha_model(cfg)
        self.assertIsInstance(model, SwingAlpha)

    def test_swing_alpha_detects_micro_valley_rebound_before_full_range_expands(self) -> None:
        model = SwingAlpha(
            SwingConfig(
                lookback=5,
                buy_band=0.45,
                sell_band=0.65,
                momentum_lookback=3,
                reversal_threshold_bps=0.2,
                edge_scale=2.0,
                min_range_bps=25.0,
                max_edge_bps=500.0,
            )
        )
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        prices = [100.0, 99.96, 99.93, 99.90, 99.88, 99.89, 99.90, 99.905]

        out = None
        for i, px in enumerate(prices):
            prev = prices[i - 1] if i else px
            ret_bps = (px / prev - 1.0) * 10000.0 if i else 0.0
            out = model.predict(
                Features(
                    ts=ts + timedelta(seconds=i),
                    values={
                        "price": px,
                        "return_bps": ret_bps,
                        "context_range_pos": 0.27,
                        "context_rebound_bps": 240.0,
                        "spread_bps": 1.2,
                        "volume_z": -0.1,
                        "trend_return_bps": -210.0,
                    },
                )
            )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("swing_state"), "micro_valley_rebound")
        self.assertGreaterEqual(float(out.edge_bps), 12.0)

    def test_swing_alpha_detects_high_context_micro_rebound_when_configured(self) -> None:
        model = SwingAlpha(
            SwingConfig(
                lookback=5,
                buy_band=0.45,
                sell_band=0.65,
                momentum_lookback=3,
                reversal_threshold_bps=0.2,
                edge_scale=2.0,
                min_range_bps=48.0,
                micro_rebound_max_spread_bps=15.0,
                micro_rebound_max_context_range_pos=0.92,
                micro_rebound_min_context_rebound_bps=260.0,
                micro_rebound_min_ret_bps=-18.0,
                max_edge_bps=500.0,
            )
        )
        ts = datetime(2026, 3, 16, tzinfo=timezone.utc)
        prices = [100.0, 100.25, 100.35, 100.20, 100.05, 99.97, 100.03, 100.08]

        out = None
        for i, px in enumerate(prices):
            prev = prices[i - 1] if i else px
            ret_bps = (px / prev - 1.0) * 10000.0 if i else 0.0
            out = model.predict(
                Features(
                    ts=ts + timedelta(seconds=i),
                    values={
                        "price": px,
                        "return_bps": ret_bps,
                        "context_range_pos": 0.90,
                        "context_rebound_bps": 620.0,
                        "spread_bps": 8.0,
                        "volume_z": -0.3,
                        "trend_return_bps": 6.0,
                    },
                )
            )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("swing_state"), "micro_valley_rebound")
        self.assertGreater(float(out.edge_bps), 20.0)

    def test_swing_alpha_detects_low_context_micro_rebound_with_relaxed_tradeability_guard(self) -> None:
        model = SwingAlpha(
            SwingConfig(
                lookback=5,
                buy_band=0.45,
                sell_band=0.65,
                momentum_lookback=3,
                reversal_threshold_bps=0.0,
                edge_scale=2.0,
                min_range_bps=40.0,
                micro_rebound_max_spread_bps=18.0,
                micro_rebound_max_context_range_pos=0.92,
                micro_rebound_min_context_rebound_bps=120.0,
                micro_rebound_min_ret_bps=-18.0,
                max_edge_bps=500.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        prices = [100.0, 99.96, 99.93, 99.90, 99.88, 99.89, 99.90, 99.905]

        out = None
        for i, px in enumerate(prices):
            prev = prices[i - 1] if i else px
            ret_bps = (px / prev - 1.0) * 10000.0 if i else 0.0
            out = model.predict(
                Features(
                    ts=ts + timedelta(seconds=i),
                    values={
                        "price": px,
                        "return_bps": ret_bps,
                        "context_range_pos": 0.04,
                        "context_rebound_bps": 125.0,
                        "spread_bps": 7.4,
                        "volume_z": -0.2,
                        "trend_return_bps": -90.0,
                    },
                )
            )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("swing_state"), "micro_valley_rebound")
        self.assertGreaterEqual(float(out.edge_bps), 12.0)

    def test_swing_alpha_waits_for_green_when_micro_rebound_is_already_advanced(self) -> None:
        model = SwingAlpha(
            SwingConfig(
                lookback=5,
                buy_band=0.45,
                sell_band=0.65,
                momentum_lookback=3,
                reversal_threshold_bps=0.0,
                edge_scale=2.0,
                min_range_bps=40.0,
                micro_rebound_max_spread_bps=18.0,
                micro_rebound_max_context_range_pos=0.92,
                micro_rebound_min_context_rebound_bps=120.0,
                micro_rebound_min_ret_bps=-18.0,
                micro_rebound_confirm_rebound_bps=180.0,
                micro_rebound_confirm_min_ret_bps=0.0,
                max_edge_bps=500.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        prices = [100.0, 99.96, 99.93, 99.90, 99.88, 99.89, 99.90, 99.895]

        out = None
        for i, px in enumerate(prices):
            prev = prices[i - 1] if i else px
            ret_bps = (px / prev - 1.0) * 10000.0 if i else 0.0
            out = model.predict(
                Features(
                    ts=ts + timedelta(seconds=i),
                    values={
                        "price": px,
                        "return_bps": ret_bps,
                        "context_range_pos": 0.20,
                        "context_rebound_bps": 193.0,
                        "spread_bps": 7.7,
                        "volume_z": -0.2,
                        "trend_return_bps": 90.0,
                    },
                )
            )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("swing_state"), "micro_rebound_wait_green")
        self.assertEqual(float(out.edge_bps), 0.0)

    def test_swing_alpha_allows_confirmed_green_micro_rebound_after_advanced_reclaim(self) -> None:
        model = SwingAlpha(
            SwingConfig(
                lookback=5,
                buy_band=0.45,
                sell_band=0.65,
                momentum_lookback=3,
                reversal_threshold_bps=0.0,
                edge_scale=2.0,
                min_range_bps=40.0,
                micro_rebound_max_spread_bps=18.0,
                micro_rebound_max_context_range_pos=0.92,
                micro_rebound_min_context_rebound_bps=120.0,
                micro_rebound_min_ret_bps=-18.0,
                micro_rebound_confirm_rebound_bps=180.0,
                micro_rebound_confirm_min_ret_bps=0.0,
                max_edge_bps=500.0,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        prices = [100.0, 99.96, 99.93, 99.90, 99.88, 99.89, 99.90, 99.905]

        out = None
        for i, px in enumerate(prices):
            prev = prices[i - 1] if i else px
            ret_bps = (px / prev - 1.0) * 10000.0 if i else 0.0
            out = model.predict(
                Features(
                    ts=ts + timedelta(seconds=i),
                    values={
                        "price": px,
                        "return_bps": ret_bps,
                        "context_range_pos": 0.20,
                        "context_rebound_bps": 193.0,
                        "spread_bps": 7.7,
                        "volume_z": -0.2,
                        "trend_return_bps": 90.0,
                    },
                )
            )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("swing_state"), "micro_valley_rebound")
        self.assertGreaterEqual(float(out.edge_bps), 12.0)

    def test_continuation_alpha_detects_early_liftoff_override(self) -> None:
        model = ContinuationAlpha(
            ContinuationConfig(
                lookback=48,
                trend_min_bps=5.0,
                rebound_trigger_bps=8.0,
                rebound_confirm_bars=0,
                pullback_min_bps=0.0,
                pullback_max_bps=60.0,
                trend_scale=1.0,
                rebound_scale=1.0,
                pullback_scale=0.5,
                recent_bias_lookback=48,
                max_chase_bps=28.0,
                min_volume_z=-0.8,
                max_structure_range_pos=0.97,
                stall_recovery_max_range_pos=0.90,
                range_continuation_max_range_pos=0.90,
                hard_block_above_range_pos=0.95,
                hard_block_max_drawdown_bps=55.0,
                max_edge_bps=260.0,
                impulse_min_ret_bps=2.0,
                impulse_min_volume_z=-1.2,
                impulse_max_context_range_pos=0.96,
                impulse_max_extension_bps=70.0,
                impulse_require_up_structure=False,
                staircase_min_trend_bps=58.0,
                staircase_min_ret_bps=-8.0,
                staircase_min_volume_z=-0.9,
                staircase_min_slope_medium_bps=1.2,
                staircase_min_slope_long_bps=0.35,
                staircase_min_drawdown_from_peak_bps=6.0,
                staircase_max_drawdown_from_peak_bps=75.0,
                staircase_max_context_range_pos=0.94,
                staircase_max_spread_bps=18.0,
                staircase_require_up_structure=False,
                bar_seconds=5,
                reference_bar_seconds=60,
            )
        )
        ts = datetime(2026, 3, 14, tzinfo=timezone.utc)
        prices: list[float] = []
        price = 1.60

        for i in range(980):
            price -= 0.0007 + (0.00008 if i % 13 == 0 else 0.0) - (0.00004 if i % 17 == 0 else 0.0)
            prices.append(round(price, 6))
        for step in ([0.00025] * 20) + ([0.0005] * 20) + ([0.00085] * 20) + ([0.0011] * 20) + ([0.0014] * 20):
            price += step
            prices.append(round(price, 6))

        out = None
        for i, px in enumerate(prices):
            prev = prices[i - 1] if i else px
            ret_bps = (px / prev - 1.0) * 10000.0 if i else 0.0
            out = model.predict(
                Features(
                    ts=ts + timedelta(seconds=5 * i),
                    values={
                        "price": px,
                        "return_bps": ret_bps,
                        "trend_return_bps": -220.0,
                        "volume_z": -0.2,
                        "spread_bps": 10.9,
                        "context_range_pos": 0.07,
                        "context_drawdown_bps": -1150.0,
                        "context_rebound_bps": 88.0,
                    },
                )
            )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("continuation_state"), "early_liftoff_override")
        self.assertGreater(float(out.edge_bps), 14.0)

    def test_continuation_alpha_detects_staircase_override_from_constructive_stall(self) -> None:
        model = ContinuationAlpha(
            ContinuationConfig(
                lookback=48,
                trend_min_bps=5.0,
                rebound_trigger_bps=8.0,
                rebound_confirm_bars=0,
                pullback_min_bps=0.0,
                pullback_max_bps=60.0,
                trend_scale=1.0,
                rebound_scale=1.0,
                pullback_scale=0.5,
                recent_bias_lookback=48,
                max_chase_bps=28.0,
                min_volume_z=-0.8,
                max_structure_range_pos=0.97,
                stall_recovery_max_range_pos=0.90,
                range_continuation_max_range_pos=0.90,
                hard_block_above_range_pos=0.95,
                hard_block_max_drawdown_bps=55.0,
                max_edge_bps=260.0,
                impulse_min_ret_bps=2.0,
                impulse_min_volume_z=-1.2,
                impulse_max_context_range_pos=0.96,
                impulse_max_extension_bps=70.0,
                impulse_require_up_structure=False,
                staircase_min_trend_bps=28.0,
                staircase_min_ret_bps=-10.0,
                staircase_min_volume_z=-1.0,
                staircase_min_slope_medium_bps=0.95,
                staircase_min_slope_long_bps=0.18,
                staircase_min_drawdown_from_peak_bps=0.0,
                staircase_max_drawdown_from_peak_bps=90.0,
                staircase_max_context_range_pos=0.99,
                staircase_max_spread_bps=22.0,
                staircase_require_up_structure=False,
                bar_seconds=5,
                reference_bar_seconds=60,
            )
        )
        ts = datetime(2026, 3, 17, tzinfo=timezone.utc)
        price = 1.0
        out = None
        structure = MarketStructure(
            phase="stall",
            confidence=0.62,
            slope_short_bps=0.2,
            slope_medium_bps=1.18,
            slope_long_bps=0.07,
            curvature_bps=-0.4,
            range_pos=0.88,
            rebound_bps=72.0,
            drawdown_bps=8.0,
            extension_bps=14.0,
            level_6h=0.77,
            level_24h=0.79,
            pivot_reversal_bps=18.0,
            bars_since_valley=24,
            bars_since_peak=2,
            rebound_from_valley_bps=84.0,
            drawdown_from_peak_bps=12.0,
            up_structure=True,
            down_structure=False,
            active_leg="flat",
        )
        with mock.patch("trading.alpha.continuation.classify_market_structure", return_value=structure):
            for i in range(64):
                price += 0.00008
                prev = price - (0.00008 if i else 0.0)
                ret_bps = (price / prev - 1.0) * 10000.0 if i else 0.0
                out = model.predict(
                    Features(
                        ts=ts + timedelta(seconds=5 * i),
                        values={
                            "price": round(price, 6),
                            "return_bps": ret_bps,
                            "trend_return_bps": 14.0,
                            "volume_z": -0.2,
                            "spread_bps": 11.5,
                            "context_range_pos": 0.86,
                            "context_drawdown_bps": -42.0,
                            "context_rebound_bps": 68.0,
                        },
                    )
                )

        self.assertIsNotNone(out)
        self.assertEqual((out.meta or {}).get("continuation_state"), "staircase_override")
        self.assertTrue(bool((out.meta or {}).get("staircase_stall_phase_ready")))
        self.assertGreater(float(out.edge_bps), 14.0)


if __name__ == "__main__":
    unittest.main()
