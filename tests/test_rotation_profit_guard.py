from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import scripts.rotation_profit_guard as rotation_profit_guard


class TestRotationProfitGuard(unittest.TestCase):
    def test_discover_profit_window_counts_partial_sell(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            journal_path = log_dir / "journal_live_binance_test_usdc_rotation.jsonl"
            journal_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "ts": "2026-03-12T08:00:00+00:00",
                                "event_type": "fill",
                                "payload": {
                                    "side": "buy",
                                    "qty_btc": 10.0,
                                    "price": 1.0,
                                    "fee_eur": 0.1,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "ts": "2026-03-12T09:00:00+00:00",
                                "event_type": "core_decision",
                                "payload": {
                                    "risk": {
                                        "allow": True,
                                        "target_btc": 0.0,
                                        "reason": "manual_exit",
                                    }
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "ts": "2026-03-12T09:01:00+00:00",
                                "event_type": "fill",
                                "payload": {
                                    "side": "sell",
                                    "qty_btc": 4.0,
                                    "price": 0.95,
                                    "fee_eur": 0.04,
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(rotation_profit_guard, "LOG_DIR", log_dir):
                window = rotation_profit_guard._discover_profit_window(
                    datetime(2026, 3, 12, 9, 0, tzinfo=timezone.utc)
                )

        self.assertEqual(len(window.trades), 1)
        trade = window.trades[0]
        self.assertEqual(trade.symbol, "TEST")
        self.assertEqual(trade.qty, 4.0)
        self.assertEqual(trade.buy_ts, "2026-03-12T08:00:00+00:00")
        self.assertEqual(trade.sell_ts, "2026-03-12T09:01:00+00:00")
        self.assertEqual(trade.exit_reason, "manual_exit")
        self.assertAlmostEqual(trade.realized_pnl, -0.28)

    def test_effective_profile_prefers_override(self) -> None:
        env = {
            "ROTATION_PROFILE": "scalp_guarded_open",
            "ROTATION_PROFILE_OVERRIDE": "scalp_lockdown",
        }

        self.assertEqual(rotation_profit_guard._effective_profile(env), "scalp_lockdown")
        self.assertIn("scalp_lockdown", rotation_profit_guard._build_selector_cmd(env))


if __name__ == "__main__":
    unittest.main()
