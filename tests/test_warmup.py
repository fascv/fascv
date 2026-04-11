import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from trading.alpha.breakout import BreakoutAlpha, BreakoutConfig
from trading.features.engine import FeatureEngine
from trading.types import MarketEvent
from trading.warmup import estimate_warmup_bars, warmup_feature_and_alpha


class TestWarmup(unittest.TestCase):
    def test_estimate_warmup_bars_respects_24h_window(self) -> None:
        cfg = {
            "features": {"return_window": 1, "atr_window": 14, "volume_z_window": 20},
            "alpha": {"type": "momentum", "lookback": 3},
            "md": {"interval_seconds": 30},
            "core": {"warmup": {"window_hours": 24}},
        }
        # For sub-minute bars, estimator uses 1m effective buckets for preload feasibility.
        self.assertEqual(estimate_warmup_bars(cfg), 1440)

    def test_warmup_hydrates_breakout_from_journal_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "journal.db")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE events (
                      id INTEGER PRIMARY KEY,
                      ts TEXT,
                      ts_unix REAL,
                      event_type TEXT,
                      payload_json TEXT
                    )
                    """
                )
                base = datetime(2026, 2, 24, 10, 0, tzinfo=timezone.utc)
                prices = [100.0, 100.5, 101.0, 101.4]
                for i, px in enumerate(prices):
                    ts = base + timedelta(minutes=i)
                    payload = (
                        "{"
                        f"\"ts\":\"{ts.isoformat()}\","
                        f"\"open\":{px},\"high\":{px},\"low\":{px},\"close\":{px},"
                        "\"volume\":1.0,"
                        "\"micro\":{\"spread_bps\":1.0}"
                        "}"
                    )
                    conn.execute(
                        "INSERT INTO events(ts, ts_unix, event_type, payload_json) VALUES (?,?,?,?)",
                        (ts.isoformat(), ts.timestamp(), "market", payload),
                    )
                conn.commit()
            finally:
                conn.close()

            cfg = {
                "journal": {"db_path": db_path},
                "data": {"default_micro": {"spread_bps": 2.0}},
                "core": {
                    "warmup": {
                        "enabled": True,
                        "window_hours": 0.0,
                        "min_bars": 4,
                        "rest_backfill": False,
                        "use_journal_db": True,
                        "use_journal_json": False,
                    }
                },
                "features": {"return_window": 1, "atr_window": 2, "volume_z_window": 2},
                "alpha": {"type": "breakout", "breakout": {"lookback": 3}},
                "md": {"interval_seconds": 60},
            }

            feature_engine = FeatureEngine(return_window=1, atr_window=2, volume_z_window=2)
            alpha = BreakoutAlpha(BreakoutConfig(lookback=3, trigger_bps=0.1, scale=1.0))
            report = warmup_feature_and_alpha(cfg, feature_engine, alpha)

            self.assertEqual(report.get("hydrated_bars"), 4)

            next_ts = datetime(2026, 2, 24, 10, 4, tzinfo=timezone.utc)
            next_event = MarketEvent(
                ts=next_ts,
                open=101.5,
                high=101.8,
                low=101.3,
                close=101.8,
                volume=1.0,
                micro={"spread_bps": 1.0},
            )
            out = alpha.predict(feature_engine.compute(next_event))
            self.assertNotEqual((out.meta or {}).get("breakout_state"), "warmup")


if __name__ == "__main__":
    unittest.main()
