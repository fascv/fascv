import os
import tempfile
import unittest
from datetime import datetime, timezone

from trading.data.ohlcv_db import OHLCVRow, available_range, connect, init_db, insert_rows, load_rows


class TestOHLCVDB(unittest.TestCase):
    def test_insert_and_load_range(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "market.db")
            conn = connect(db_path)
            try:
                init_db(conn)
                rows = [
                    OHLCVRow(
                        ts=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
                        open=1.0,
                        high=2.0,
                        low=0.5,
                        close=1.5,
                        volume=10.0,
                    ),
                    OHLCVRow(
                        ts=datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc),
                        open=1.5,
                        high=2.5,
                        low=1.0,
                        close=2.0,
                        volume=12.0,
                    ),
                ]
                ins = insert_rows(conn, symbol="XBT/EUR", timeframe="5m", rows=rows, source="test")
                self.assertEqual(ins, 2)

                mn, mx, cnt = available_range(conn, symbol="XBT/EUR", timeframe="5m")
                self.assertEqual(cnt, 2)
                self.assertIsNotNone(mn)
                self.assertIsNotNone(mx)

                loaded = load_rows(conn, symbol="XBT/EUR", timeframe="5m")
                self.assertEqual(len(loaded), 2)
                self.assertEqual(loaded[0].close, 1.5)
                self.assertEqual(loaded[1].close, 2.0)

                # idempotent insert (dedupe by PK)
                ins2 = insert_rows(conn, symbol="XBT/EUR", timeframe="5m", rows=rows, source="test")
                self.assertEqual(ins2, 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

