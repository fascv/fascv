from __future__ import annotations

import argparse
import io
import json
import math
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from scripts import select_rotation_watchlist
from scripts.select_rotation_watchlist import (
    _adaptive_corridor_pos_pct,
    _fresh_liftoff_bottom_candidate,
    _fresh_late_rebound_override,
    _micro_valley_context_position_pct,
    _net_range_step_after_fees_pct,
    _non_falling_longtrend_block,
    _persistent_downdrift_metrics,
    _relaxed_valley_context,
    _strict_valley_context,
    _window_pos_pct,
)


class TestSelectRotationWatchlist(unittest.TestCase):
    @staticmethod
    def _build_klines(*, start: float, step: float, count: int = 600, quote_volume: float = 400.0) -> list[list[object]]:
        rows: list[list[object]] = []
        for idx in range(count):
            close = start + (step * idx)
            rows.append(
                [
                    idx,
                    "0",
                    "0",
                    "0",
                    f"{close:.6f}",
                    "0",
                    "0",
                    f"{quote_volume:.2f}",
                ]
            )
        return rows

    @staticmethod
    def _market_structure(*, phase: str) -> SimpleNamespace:
        return SimpleNamespace(
            phase=phase,
            confidence=0.82,
            level_6h=0.42,
            level_24h=0.49,
            pivot_reversal_bps=24.0,
            rebound_from_valley_bps=36.0,
            drawdown_from_peak_bps=28.0,
            bars_since_valley=5,
            bars_since_peak=21,
            active_leg="rise",
            up_structure=True,
            down_structure=False,
            slope_short_bps=2.8,
            slope_medium_bps=1.4,
            slope_long_bps=0.6,
            curvature_bps=0.4,
            drawdown_bps=18.0,
        )

    def test_strict_valley_context_matches_existing_thresholds(self) -> None:
        self.assertTrue(
            _strict_valley_context(
                pos_6h_pct=43.5,
                pos_24h_pct=55.5,
                long_up_hot=False,
                down_structure=False,
            )
        )
        self.assertFalse(
            _strict_valley_context(
                pos_6h_pct=46.0,
                pos_24h_pct=28.0,
                long_up_hot=False,
                down_structure=False,
            )
        )

    def test_relaxed_valley_context_allows_fresh_bottom_rebound(self) -> None:
        self.assertTrue(
            _relaxed_valley_context(
                pos_6h_pct=48.0,
                pos_24h_pct=24.0,
                long_up_hot=False,
                down_structure=False,
                macro_down_context=False,
                falling_now=False,
                spread_bps=12.5,
                structure_phase="lift_off",
                recent_rebound_ready=True,
                post_dump_recovery_ready=False,
                previous_selloff=True,
                rebound_from_30m_low_bps=42.0,
                rebound_from_60m_low_bps=64.0,
                bars_since_30m_low=3,
                bars_since_swing_low=3,
            )
        )

    def test_relaxed_valley_context_allows_wider_rebound_window(self) -> None:
        self.assertTrue(
            _relaxed_valley_context(
                pos_6h_pct=65.0,
                pos_24h_pct=63.5,
                long_up_hot=False,
                down_structure=False,
                macro_down_context=False,
                falling_now=False,
                spread_bps=18.0,
                structure_phase="range",
                recent_rebound_ready=False,
                post_dump_recovery_ready=False,
                previous_selloff=True,
                rebound_from_30m_low_bps=13.0,
                rebound_from_60m_low_bps=33.0,
                bars_since_30m_low=7,
                bars_since_swing_low=8,
            )
        )

    def test_relaxed_valley_context_rejects_falling_now(self) -> None:
        self.assertFalse(
            _relaxed_valley_context(
                pos_6h_pct=32.0,
                pos_24h_pct=18.0,
                long_up_hot=False,
                down_structure=False,
                macro_down_context=False,
                falling_now=True,
                spread_bps=8.0,
                structure_phase="bottom",
                recent_rebound_ready=True,
                post_dump_recovery_ready=False,
                previous_selloff=True,
                rebound_from_30m_low_bps=36.0,
                rebound_from_60m_low_bps=58.0,
                bars_since_30m_low=2,
                bars_since_swing_low=2,
            )
        )

    def test_relaxed_valley_context_rejects_macro_down_without_recovery(self) -> None:
        self.assertFalse(
            _relaxed_valley_context(
                pos_6h_pct=36.0,
                pos_24h_pct=22.0,
                long_up_hot=False,
                down_structure=False,
                macro_down_context=True,
                falling_now=False,
                spread_bps=9.0,
                structure_phase="bottom",
                recent_rebound_ready=True,
                post_dump_recovery_ready=False,
                previous_selloff=True,
                rebound_from_30m_low_bps=30.0,
                rebound_from_60m_low_bps=76.0,
                bars_since_30m_low=3,
                bars_since_swing_low=3,
            )
        )

    def test_fresh_late_rebound_override_allows_rising_low_range_rebound(self) -> None:
        self.assertTrue(
            _fresh_late_rebound_override(
                active_leg="rise",
                pos_24h_pct=13.5,
                spread_bps=12.4,
                bars_since_30m_low=5,
                ret10_bps=7.5,
                ret15_bps=17.3,
            )
        )

    def test_fresh_late_rebound_override_rejects_falling_leg(self) -> None:
        self.assertFalse(
            _fresh_late_rebound_override(
                active_leg="fall",
                pos_24h_pct=13.5,
                spread_bps=12.4,
                bars_since_30m_low=5,
                ret10_bps=7.5,
                ret15_bps=17.3,
            )
        )

    def test_fresh_liftoff_bottom_candidate_allows_sign_like_rebound(self) -> None:
        self.assertTrue(
            _fresh_liftoff_bottom_candidate(
                structure_phase="lift_off",
                active_leg="rise",
                pos_24h_pct=15.3,
                spread_bps=12.4,
                bars_since_30m_low=6,
                ret10_bps=9.9,
                ret15_bps=24.8,
                ret60_bps=37.2,
                previous_selloff=True,
                in_valley_context=True,
                macro_down_context=False,
            )
        )

    def test_fresh_liftoff_bottom_candidate_rejects_macro_down_context(self) -> None:
        self.assertFalse(
            _fresh_liftoff_bottom_candidate(
                structure_phase="lift_off",
                active_leg="rise",
                pos_24h_pct=15.3,
                spread_bps=12.4,
                bars_since_30m_low=6,
                ret10_bps=9.9,
                ret15_bps=24.8,
                ret60_bps=37.2,
                previous_selloff=True,
                in_valley_context=True,
                macro_down_context=True,
            )
        )

    def test_net_range_step_after_fees_pct(self) -> None:
        self.assertAlmostEqual(
            _net_range_step_after_fees_pct(width_pct=20.0, step_fraction=0.10, roundtrip_fee_bps=20.0),
            1.8,
            places=6,
        )
        self.assertLess(
            _net_range_step_after_fees_pct(width_pct=1.5, step_fraction=0.10, roundtrip_fee_bps=20.0),
            0.0,
        )

    def test_adaptive_corridor_short_series_applies_bounded_trend_shift(self) -> None:
        closes = [1.0 + (0.01 * idx) for idx in range(24)]
        metrics = _adaptive_corridor_pos_pct(closes)
        base_pos = _window_pos_pct(closes, len(closes))
        self.assertAlmostEqual(metrics["base_pos_pct"], base_pos, places=6)
        self.assertGreater(metrics["trend_bias"], 0.0)
        self.assertLess(metrics["trend_shift_pct"], 0.0)
        self.assertLess(metrics["pos_pct"], base_pos)
        self.assertGreaterEqual(
            metrics["pos_pct"],
            base_pos - metrics["max_shift_from_base_pct"],
        )

    def test_adaptive_corridor_biases_downtrend_higher(self) -> None:
        closes = [100.0 + (-0.18 * idx) + (2.6 * math.sin(idx * 0.42)) for idx in range(240)]
        metrics = _adaptive_corridor_pos_pct(closes)
        self.assertLess(metrics["trend_bias"], 0.0)
        self.assertGreater(metrics["trend_shift_pct"], 0.0)
        self.assertGreater(metrics["pos_pct"], metrics["anchor_pos_pct"])
        self.assertGreaterEqual(metrics["trough_count"], 2.0)

    def test_adaptive_corridor_biases_uptrend_lower(self) -> None:
        closes = [100.0 + (0.18 * idx) + (2.6 * math.sin(idx * 0.42)) for idx in range(240)]
        metrics = _adaptive_corridor_pos_pct(closes)
        self.assertGreater(metrics["trend_bias"], 0.0)
        self.assertLess(metrics["trend_shift_pct"], 0.0)
        self.assertLess(metrics["pos_pct"], metrics["anchor_pos_pct"])
        self.assertGreaterEqual(metrics["trough_count"], 2.0)

    def test_micro_valley_context_position_prefers_valley_to_peak(self) -> None:
        near_closes = [
            120.0,
            118.0,
            115.0,
            112.0,
            108.0,
            104.0,
            100.0,
            102.0,
            105.0,
            109.0,
            113.0,
            117.0,
            121.0,
            124.0,
            126.0,
        ]
        metrics = _micro_valley_context_position_pct(
            near_closes,
            near_closes=near_closes,
            near_bar_minutes=60.0,
            fallback_pos_pct=33.0,
        )
        self.assertEqual(metrics["mode"], "valley_to_peak")
        self.assertFalse(metrics["fallback_used"])
        self.assertGreater(float(metrics["pos_pct"]), 95.0)

    def test_micro_valley_context_position_uses_fallback_on_flat_series(self) -> None:
        near_closes = [100.0] * 24
        metrics = _micro_valley_context_position_pct(
            near_closes,
            near_closes=near_closes,
            near_bar_minutes=60.0,
            fallback_pos_pct=37.0,
        )
        self.assertEqual(metrics["mode"], "fallback")
        self.assertTrue(metrics["fallback_used"])
        self.assertAlmostEqual(float(metrics["pos_pct"]), 37.0, places=6)

    def test_persistent_downdrift_metrics_blocks_strict_down_drift(self) -> None:
        symbol_closes = [100.0 * (0.988 ** idx) for idx in range(240)]
        btc_closes = [100.0 * (0.997 ** idx) for idx in range(240)]
        metrics = _persistent_downdrift_metrics(
            symbol_closes,
            btc_closes,
            level="strict",
        )
        self.assertTrue(metrics["enabled"])
        self.assertTrue(metrics["data_ok"])
        self.assertTrue(metrics["blocked"])

    def test_persistent_downdrift_metrics_does_not_block_recovering_symbol(self) -> None:
        symbol_closes = [50.0 + (0.22 * idx) + (1.8 * math.sin(idx * 0.12)) for idx in range(240)]
        btc_closes = [100.0 + (0.05 * idx) for idx in range(240)]
        metrics = _persistent_downdrift_metrics(
            symbol_closes,
            btc_closes,
            level="strict",
        )
        self.assertTrue(metrics["enabled"])
        self.assertTrue(metrics["data_ok"])
        self.assertFalse(metrics["blocked"])

    def test_non_falling_longtrend_block_blocks_downward_metrics(self) -> None:
        blocked, reason = _non_falling_longtrend_block(
            {
                "data_ok": True,
                "ret180_pct": -12.0,
                "long_high_shift_pct": -4.0,
                "long_low_shift_pct": -3.0,
                "long_log_slope": -0.0012,
                "rel180_pct": -8.0,
            },
            enabled=True,
            ret180_min_pct=0.0,
            long_high_shift_min_pct=0.0,
            long_low_shift_min_pct=0.0,
            long_log_slope_min=0.0,
            rel180_min_pct=None,
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "ret180_below_min")

    def test_non_falling_longtrend_block_accepts_non_falling_metrics(self) -> None:
        blocked, reason = _non_falling_longtrend_block(
            {
                "data_ok": True,
                "ret180_pct": 8.0,
                "long_high_shift_pct": 1.2,
                "long_low_shift_pct": 0.8,
                "long_log_slope": 0.0005,
                "rel180_pct": -25.0,
            },
            enabled=True,
            ret180_min_pct=0.0,
            long_high_shift_min_pct=0.0,
            long_low_shift_min_pct=0.0,
            long_log_slope_min=0.0,
            rel180_min_pct=None,
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_non_falling_longtrend_block_handles_missing_macro_history(self) -> None:
        blocked, reason = _non_falling_longtrend_block(
            {"data_ok": False},
            enabled=True,
            ret180_min_pct=0.0,
            long_high_shift_min_pct=0.0,
            long_low_shift_min_pct=0.0,
            long_log_slope_min=0.0,
            rel180_min_pct=None,
        )
        self.assertTrue(blocked)
        self.assertEqual(reason, "insufficient_macro_history")

    def test_build_auto_blacklist_keeps_cached_entry_on_partial_refresh_error(self) -> None:
        btc_macro_closes = [100.0 + (0.2 * idx) for idx in range(240)]
        cache_payload = {
            "schema_version": select_rotation_watchlist.AUTO_BLACKLIST_CACHE_SCHEMA_VERSION,
            "generated_at_ts": 1,
            "quote_asset": "USDC",
            "downdrift_level": "mild",
            "entries": {
                "BAD": {
                    "symbol": "BAD",
                    "reason": "persistent_downdrift_mild",
                }
            },
            "scanned_symbols": ["BAD", "GOOD"],
        }

        def _fake_get_klines(market: str, **kwargs: object) -> list[list[object]]:
            if market == "BADUSDC":
                raise RuntimeError("temporary market data failure")
            return self._build_klines(start=1.0, step=0.001, count=260, quote_volume=450.0)

        with (
            patch.dict(
                select_rotation_watchlist.os.environ,
                {
                    "ROTATION_AUTO_BLACKLIST_ENABLED": "1",
                    "ROTATION_AUTO_BLACKLIST_DOWNTREND_LEVEL": "mild",
                    "ROTATION_AUTO_BLACKLIST_FORCE_REFRESH": "1",
                    "ROTATION_AUTO_BLACKLIST_USE_LISTING_STRUCTURAL_BLOCK": "0",
                },
                clear=False,
            ),
            patch.object(
                select_rotation_watchlist,
                "_load_auto_blacklist_cache",
                return_value=cache_payload,
            ),
            patch.object(select_rotation_watchlist, "_save_auto_blacklist_cache"),
            patch.object(select_rotation_watchlist, "_get_klines", side_effect=_fake_get_klines),
            patch.object(
                select_rotation_watchlist,
                "_persistent_downdrift_metrics",
                return_value={"enabled": True, "data_ok": True, "blocked": False},
            ),
        ):
            entries, info = select_rotation_watchlist._build_auto_blacklist(
                quote_asset="USDC",
                scan_symbols=["BAD", "GOOD"],
                btc_macro_closes=btc_macro_closes,
            )

        self.assertIn("BAD", entries)
        self.assertEqual(entries["BAD"]["reason"], "persistent_downdrift_mild")
        self.assertNotIn("GOOD", entries)
        self.assertFalse(info["refresh_ok"])
        self.assertIn("BAD", info["refresh_failed_symbols"])

    def test_contract_prefilter_blocks_nondefault_risk_when_default_unknown(self) -> None:
        scans = [
            {
                "network": "ETH",
                "is_default": True,
                "honeypot": {"status": "ok", "trade_risk": "unknown", "flags": []},
                "goplus": {"status": "ok"},
            },
            {
                "network": "BSC",
                "is_default": False,
                "honeypot": {
                    "status": "ok",
                    "trade_risk": "honeypot",
                    "risk_level": 100,
                    "is_honeypot": True,
                    "flags": ["medium_fail_rate"],
                },
                "goplus": {"status": "ok"},
            },
        ]

        decision = select_rotation_watchlist._decide_contract_prefilter(
            scans,
            block_nondefault_when_default_unknown=True,
        )

        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["reason"], "token_contract_risk_nondefault_default_unknown")

    def test_contract_prefilter_does_not_block_nondefault_risk_when_default_low(self) -> None:
        scans = [
            {
                "network": "ETH",
                "is_default": True,
                "honeypot": {"status": "ok", "trade_risk": "low", "risk_level": 1, "flags": []},
                "goplus": {"status": "ok"},
            },
            {
                "network": "BSC",
                "is_default": False,
                "honeypot": {
                    "status": "ok",
                    "trade_risk": "honeypot",
                    "risk_level": 100,
                    "is_honeypot": True,
                    "flags": ["medium_fail_rate"],
                },
                "goplus": {"status": "ok"},
            },
        ]

        decision = select_rotation_watchlist._decide_contract_prefilter(
            scans,
            block_nondefault_when_default_unknown=True,
        )

        self.assertFalse(decision["blocked"])

    def test_contract_prefilter_warns_but_does_not_block_proxy_contract(self) -> None:
        scans = [
            {
                "network": "ETH",
                "is_default": True,
                "honeypot": {"status": "ok", "trade_risk": "low", "risk_level": 1, "flags": []},
                "goplus": {"status": "ok", "is_proxy": "1"},
            },
        ]

        decision = select_rotation_watchlist._decide_contract_prefilter(
            scans,
            block_nondefault_when_default_unknown=True,
        )

        self.assertFalse(decision["blocked"])
        self.assertIn("proxy_contract", decision["warnings"])

    def test_token_prefilter_blocks_seed_tag_from_universe_policy_fallback(self) -> None:
        with (
            patch.dict(
                select_rotation_watchlist.os.environ,
                {"ROTATION_TOKEN_PREFILTER_BLOCK_TAGS": "Seed"},
                clear=False,
            ),
            patch.object(
                select_rotation_watchlist,
                "_fetch_24h_ticker_map",
                return_value={"TESTUSDC": {"quoteVolume": 1_000_000.0, "count": 100.0}},
            ),
            patch.object(select_rotation_watchlist, "_fetch_binance_product_map", return_value={}),
            patch.object(
                select_rotation_watchlist,
                "_build_contract_risk_cache",
                return_value=({}, {"enabled": True}),
            ),
        ):
            entries, info = select_rotation_watchlist._build_token_prefilter(
                quote_asset="USDC",
                scan_symbols=["TEST"],
                book_ticker_map={
                    "TESTUSDC": {
                        "bidPrice": 0.999,
                        "askPrice": 1.001,
                        "bidQty": 1_000.0,
                        "askQty": 1_000.0,
                    }
                },
                universe_policy_map={"TESTUSDC": {"monitoring_tags": ["Seed"]}},
            )

        self.assertEqual(info["blocked_count"], 1)
        self.assertTrue(entries["TEST"]["blocked"])
        self.assertEqual(entries["TEST"]["reason"], "token_prefilter_tag_seed")
        self.assertEqual(entries["TEST"]["matched_block_tags"], ["seed"])

    def test_build_universe_policy_marks_unknown_when_refresh_fails(self) -> None:
        with (
            patch.dict(
                select_rotation_watchlist.os.environ,
                {"ROTATION_UNIVERSE_POLICY_FORCE_REFRESH": "1"},
                clear=False,
            ),
            patch.object(
                select_rotation_watchlist,
                "_load_universe_policy_cache",
                return_value={},
            ),
            patch.object(select_rotation_watchlist, "_save_universe_policy_cache"),
            patch.object(select_rotation_watchlist, "_fetch_binance_product_map", return_value={}),
            patch.object(select_rotation_watchlist, "_fetch_exchange_symbol_meta", return_value={}),
        ):
            policy_map, info = select_rotation_watchlist._build_universe_policy(
                markets=["TESTUSDC"],
                quote_asset="USDC",
            )

        self.assertIn("TESTUSDC", policy_map)
        policy = policy_map["TESTUSDC"]
        self.assertFalse(policy["is_monitoring"])
        self.assertFalse(policy["is_problem"])
        self.assertTrue(policy["monitoring_data_unknown"])
        self.assertTrue(policy["problem_data_unknown"])
        self.assertTrue(policy["policy_data_unknown"])
        self.assertFalse(info["monitoring_refresh_ok"])
        self.assertFalse(info["problem_refresh_ok"])
        self.assertEqual(info["policy_data_unknown_total"], 1)

    def test_main_blocks_symbol_when_universe_policy_is_unknown(self) -> None:
        args = argparse.Namespace(
            setup_mode="trend",
            universe_source="pool",
            quote_asset="USDC",
            symbols="TEST",
            ignore_balances=True,
        )
        btc_klines = self._build_klines(start=50000.0, step=3.0, quote_volume=5000.0)
        symbol_klines = self._build_klines(start=1.0, step=0.002, quote_volume=500.0)
        structure = self._market_structure(phase="stall")
        stdout = io.StringIO()

        with (
            patch.dict(select_rotation_watchlist.os.environ, {}, clear=True),
            patch.object(select_rotation_watchlist, "_load_env"),
            patch.object(select_rotation_watchlist, "_parse_args", return_value=args),
            patch.object(
                select_rotation_watchlist,
                "_resolve_candidates",
                return_value=(["TEST"], "explicit_symbols"),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_universe_policy",
                return_value=(
                    {
                        "TESTUSDC": {
                            "is_monitoring": False,
                            "monitoring_tags": [],
                            "is_problem": False,
                            "problem_reasons": [],
                            "monitoring_data_unknown": True,
                            "problem_data_unknown": True,
                            "policy_data_unknown": True,
                        }
                    },
                    {},
                ),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_auto_blacklist",
                return_value=({}, {"enabled": True}),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_token_prefilter",
                return_value=({}, {"enabled": True}),
            ),
            patch.object(select_rotation_watchlist, "_load_slope_profiles", return_value={}),
            patch.object(
                select_rotation_watchlist,
                "_get_book_ticker_map",
                return_value={
                    "TESTUSDC": {
                        "bidPrice": 2.199,
                        "askPrice": 2.201,
                        "bidQty": 60.0,
                        "askQty": 60.0,
                    }
                },
            ),
            patch.object(
                select_rotation_watchlist,
                "_get_klines",
                side_effect=lambda market, **kwargs: btc_klines if market == "BTCUSDC" else symbol_klines,
            ),
            patch.object(
                select_rotation_watchlist,
                "classify_market_structure",
                return_value=structure,
            ),
            redirect_stdout(stdout),
        ):
            select_rotation_watchlist.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["errors_total"], 0)
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertTrue(row["hard_excluded"])
        self.assertEqual(row["hard_exclusion_reason"], "universe_policy_data_unknown")
        self.assertEqual(row["gate_reason"], "universe_policy_data_unknown")

    def test_main_keeps_open_balance_despite_universe_policy_block(self) -> None:
        args = argparse.Namespace(
            setup_mode="trend",
            universe_source="pool",
            quote_asset="USDC",
            symbols="TEST",
            ignore_balances=False,
        )
        btc_klines = self._build_klines(start=50000.0, step=3.0, quote_volume=5000.0)
        symbol_klines = self._build_klines(start=1.0, step=0.002, quote_volume=500.0)
        structure = self._market_structure(phase="stall")
        stdout = io.StringIO()

        with (
            patch.dict(select_rotation_watchlist.os.environ, {}, clear=True),
            patch.object(select_rotation_watchlist, "_load_env"),
            patch.object(select_rotation_watchlist, "_parse_args", return_value=args),
            patch.object(
                select_rotation_watchlist,
                "_resolve_candidates",
                return_value=(["TEST"], "explicit_symbols"),
            ),
            patch.object(
                select_rotation_watchlist,
                "_signed_get",
                return_value={
                    "balances": [
                        {"asset": "TEST", "free": "50", "locked": "0"},
                    ]
                },
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_universe_policy",
                return_value=(
                    {
                        "TESTUSDC": {
                            "is_monitoring": False,
                            "monitoring_tags": [],
                            "is_problem": False,
                            "problem_reasons": [],
                            "monitoring_data_unknown": True,
                            "problem_data_unknown": True,
                            "policy_data_unknown": True,
                        }
                    },
                    {},
                ),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_auto_blacklist",
                return_value=({}, {"enabled": True}),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_token_prefilter",
                return_value=({}, {"enabled": True}),
            ),
            patch.object(select_rotation_watchlist, "_load_slope_profiles", return_value={}),
            patch.object(
                select_rotation_watchlist,
                "_get_book_ticker_map",
                return_value={
                    "TESTUSDC": {
                        "bidPrice": 2.199,
                        "askPrice": 2.201,
                        "bidQty": 60.0,
                        "askQty": 60.0,
                    }
                },
            ),
            patch.object(
                select_rotation_watchlist,
                "_get_klines",
                side_effect=lambda market, **kwargs: btc_klines if market == "BTCUSDC" else symbol_klines,
            ),
            patch.object(
                select_rotation_watchlist,
                "classify_market_structure",
                return_value=structure,
            ),
            redirect_stdout(stdout),
        ):
            select_rotation_watchlist.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["errors_total"], 0)
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["symbol"], "TEST")
        self.assertTrue(row["keep_open"])
        self.assertTrue(row["eligible"])
        self.assertFalse(row["hard_excluded"])
        self.assertEqual(row["gate_reason"], "keep_open")
        self.assertGreater(row["open_notional"], 2.0)

    def test_main_processes_stall_rows_without_unbound_macro_context(self) -> None:
        args = argparse.Namespace(
            setup_mode="trend",
            universe_source="pool",
            quote_asset="USDC",
            symbols="TEST",
            ignore_balances=True,
        )
        btc_klines = self._build_klines(start=50000.0, step=3.0, quote_volume=5000.0)
        symbol_klines = self._build_klines(start=1.0, step=0.002, quote_volume=500.0)
        structure = self._market_structure(phase="stall")
        stdout = io.StringIO()

        with (
            patch.dict(select_rotation_watchlist.os.environ, {}, clear=True),
            patch.object(select_rotation_watchlist, "_load_env"),
            patch.object(select_rotation_watchlist, "_parse_args", return_value=args),
            patch.object(
                select_rotation_watchlist,
                "_resolve_candidates",
                return_value=(["TEST"], "explicit_symbols"),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_universe_policy",
                return_value=({}, {}),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_auto_blacklist",
                return_value=({}, {"enabled": True}),
            ),
            patch.object(
                select_rotation_watchlist,
                "_build_token_prefilter",
                return_value=({}, {"enabled": True}),
            ),
            patch.object(select_rotation_watchlist, "_load_slope_profiles", return_value={}),
            patch.object(
                select_rotation_watchlist,
                "_get_book_ticker_map",
                return_value={
                    "TESTUSDC": {
                        "bidPrice": 2.199,
                        "askPrice": 2.201,
                        "bidQty": 60.0,
                        "askQty": 60.0,
                    }
                },
            ),
            patch.object(
                select_rotation_watchlist,
                "_get_klines",
                side_effect=lambda market, **kwargs: btc_klines if market == "BTCUSDC" else symbol_klines,
            ),
            patch.object(
                select_rotation_watchlist,
                "classify_market_structure",
                return_value=structure,
            ),
            redirect_stdout(stdout),
        ):
            select_rotation_watchlist.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["errors_total"], 0)
        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["symbol"], "TEST")
        self.assertIn("macro_down_context", row)
        self.assertIn("long_term_uptrend_context", row)


if __name__ == "__main__":
    unittest.main()
