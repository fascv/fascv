from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent.parent / "app" / "relay-server" / "relay_server.py"
SPEC = importlib.util.spec_from_file_location("relay_server_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load relay_server module from {MODULE_PATH}")
relay_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_server)


class TestRelayRotationMetaSummary(unittest.TestCase):
    def test_load_rotation_meta_summary_includes_notes_and_lookback(self) -> None:
        payload = {
            "generated_at": "2026-03-12T22:30:00+00:00",
            "current_profile": "scalp_uptrend",
            "trade_lookback_hours": 6.0,
            "meta_mode": "reused_recent",
            "recommendation": {
                "risk_mode": "cautious",
                "confidence": 0.73,
                "notes": "exit_too_early_watch_healthy; local_autotune:hold_longer",
                "strategy_weights": {"continuation": 0.5},
                "strategy_actions": {
                    "continuation": {
                        "mode": "primary",
                        "slot_target": 2,
                        "top_symbols": ["FET", "LDO"],
                    }
                },
            },
            "trade_summary": {
                "strategy_breakdown": {
                    "continuation": {
                        "trade_count": 3,
                        "net_pnl": 1.25,
                        "win_rate": 2.0 / 3.0,
                        "avg_hold_sec": 540.0,
                        "avg_win": 0.9,
                        "avg_loss": -0.35,
                        "last_exit_ts": "2026-03-12T22:25:00+00:00",
                        "exit_reasons": {"failed_start_exit": 1, "trailing_stop": 2},
                        "top_symbols": ["FET", "LDO"],
                    }
                }
            },
            "watch_pool_strategy_summary": {
                "continuation": {
                    "candidate_count": 7,
                    "buy_ready_count": 3,
                    "ml_positive_count": 2,
                    "avg_top_p_profit": 0.61,
                    "avg_top_strategy_score": 14.0,
                    "dominant_gate_reasons": {"structure_stall": 2},
                    "top_candidates": [{"symbol": "FET"}, {"symbol": "LDO"}],
                }
            },
            "universe_strategy_rankings": {
                "continuation": [{"symbol": "FET"}, {"symbol": "LDO"}, {"symbol": "CFX"}]
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "rotation_meta_shadow_report.json"
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            old_report_path = relay_server.ROTATION_META_SHADOW_REPORT_FILE
            try:
                relay_server.ROTATION_META_SHADOW_REPORT_FILE = str(report_path)
                summary = relay_server.load_rotation_meta_summary()
            finally:
                relay_server.ROTATION_META_SHADOW_REPORT_FILE = old_report_path

        self.assertTrue(summary["available"])
        self.assertEqual(summary["currentProfile"], "scalp_uptrend")
        self.assertEqual(summary["metaMode"], "reused_recent")
        self.assertEqual(summary["lookbackHours"], 6.0)
        self.assertIn("exit_too_early_watch_healthy", summary["notes"])
        self.assertEqual(len(summary["rows"]), 4)
        continuation = next(item for item in summary["rows"] if item["strategy"] == "continuation")
        self.assertEqual(continuation["tradeCount"], 3)
        self.assertEqual(continuation["action"]["slotTarget"], 2)
        self.assertEqual(continuation["watchTopSymbols"], ["FET", "LDO"])
        self.assertEqual(continuation["universeTopSymbols"], ["FET", "LDO", "CFX"])
        self.assertEqual(continuation["dominantGateReasons"], {"Struktur kommt nicht voran": 2})
        self.assertEqual(continuation["dominantGateReasonCodes"], {"structure_stall": 2})
        self.assertEqual(
            [item["strategy"] for item in summary["rows"]],
            ["staircase", "continuation", "breakout", "rebound"],
        )


if __name__ == "__main__":
    unittest.main()
