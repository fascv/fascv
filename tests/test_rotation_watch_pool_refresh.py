from __future__ import annotations

import unittest

from scripts.rotation_refresh_watch_pool import build_watch_pool


class TestRotationWatchPoolRefresh(unittest.TestCase):
    def test_recent_live_selection_block_removes_current_selected_sticky_priority(self) -> None:
        active_state = {
            "selected": ["CHZ"],
            "watch_symbols": ["CHZ", "ENA"],
            "all_rows": [
                {
                    "symbol": "CHZ",
                    "keep_open": False,
                    "eligible": False,
                    "gate_reason": "edge_below_costs",
                    "recent_live_selection_block": True,
                },
                {
                    "symbol": "ENA",
                    "keep_open": False,
                    "eligible": True,
                    "gate_reason": "",
                    "recent_live_selection_block": False,
                },
            ],
        }
        universe_report = {
            "top_candidates": [{"symbol": "ENA", "bucket": "keep"}],
            "recommended_pool": ["ENA"],
            "strategy_rankings": {
                "rebound": [{"symbol": "ENA", "bucket": "keep"}],
            },
        }
        meta_report = {}

        watch_symbols, scored = build_watch_pool(
            active_state=active_state,
            universe_report=universe_report,
            meta_report=meta_report,
            target_size=4,
        )

        self.assertGreater(scored["ENA"]["score"], scored["CHZ"]["score"])
        reasons = {entry["reason"] for entry in scored["CHZ"]["reasons"]}
        self.assertIn("recent_live_selection_block", reasons)
        self.assertIn("current_selected_recent_live_block", reasons)
        self.assertIn("CHZ", watch_symbols)
        self.assertIn("ENA", watch_symbols)

    def test_exit_only_watch_symbol_is_not_dropped_from_current_watch_scope(self) -> None:
        active_state = {
            "selected": [],
            "watch_symbols": ["SIGN", "AAVE"],
            "all_rows": [
                {
                    "symbol": "AAVE",
                    "keep_open": False,
                    "eligible": True,
                    "gate_reason": "",
                    "recent_live_selection_block": False,
                },
            ],
        }
        universe_report = {
            "top_candidates": [{"symbol": "AAVE", "bucket": "keep"}],
            "recommended_pool": ["AAVE"],
            "strategy_rankings": {
                "rebound": [{"symbol": "AAVE", "bucket": "keep"}],
            },
        }
        meta_report = {}

        watch_symbols, scored = build_watch_pool(
            active_state=active_state,
            universe_report=universe_report,
            meta_report=meta_report,
            target_size=4,
        )

        self.assertIn("SIGN", scored)
        self.assertIn("SIGN", watch_symbols)
        self.assertIn("AAVE", watch_symbols)

    def test_allowed_symbols_filters_external_candidates_and_fills_allowed_pool(self) -> None:
        active_state = {
            "selected": ["ZRO"],
            "watch_symbols": ["ZRO", "BONK"],
            "all_rows": [
                {"symbol": "ZRO", "keep_open": False, "eligible": True, "gate_reason": ""},
                {"symbol": "BONK", "keep_open": False, "eligible": True, "gate_reason": ""},
                {"symbol": "ZK", "keep_open": False, "eligible": True, "gate_reason": ""},
            ],
        }
        universe_report = {
            "top_candidates": [
                {"symbol": "ZK", "bucket": "keep"},
                {"symbol": "BONK", "bucket": "keep"},
            ],
            "recommended_pool": ["ZK", "BONK"],
            "strategy_rankings": {
                "rebound": [
                    {"symbol": "ZK", "bucket": "keep"},
                    {"symbol": "BONK", "bucket": "keep"},
                ],
            },
        }
        meta_report = {
            "recommendation": {
                "candidate_overrides": ["ZK", "BONK"],
            },
        }

        watch_symbols, scored = build_watch_pool(
            active_state=active_state,
            universe_report=universe_report,
            meta_report=meta_report,
            target_size=4,
            allowed_symbols=["ZRO", "BONK", "ADA", "TRX"],
        )

        self.assertNotIn("ZK", scored)
        self.assertEqual(watch_symbols, ["ZRO", "BONK", "ADA", "TRX"])


if __name__ == "__main__":
    unittest.main()
