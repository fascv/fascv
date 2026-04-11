import os
import sqlite3
import sys
import tempfile
import unittest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_JOURNAL_SRC = os.path.join(_REPO_ROOT, "modules", "journal", "src")
if _JOURNAL_SRC not in sys.path:
    sys.path.insert(0, _JOURNAL_SRC)

from trading_journal.gui import sqlite_recent_rows_snapshot, sqlite_schema_snapshot  # noqa: E402


class TestJournalGuiSQLiteSnapshot(unittest.TestCase):
    def test_schema_and_recent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "journal.db")
            conn = sqlite3.connect(db)
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, ts_unix REAL, event_type TEXT, payload_json TEXT)"
            )
            cur.execute("CREATE INDEX idx_events_ts_unix ON events (ts_unix)")
            cur.execute(
                "INSERT INTO events (ts, ts_unix, event_type, payload_json) VALUES (?, ?, ?, ?)",
                ("2026-02-14T00:00:00+00:00", 0.0, "x", "{\"a\":1}"),
            )
            conn.commit()
            conn.close()

            schema = sqlite_schema_snapshot(db)
            self.assertTrue(schema["db_ok"])
            cols = {c["name"] for c in schema["columns"]}
            self.assertIn("id", cols)
            self.assertIn("payload_json", cols)

            recent = sqlite_recent_rows_snapshot(db, limit=10)
            self.assertTrue(recent["db_ok"])
            self.assertEqual(recent["max_id"], 1)
            self.assertEqual(len(recent["rows"]), 1)
            self.assertEqual(recent["rows"][0]["event_type"], "x")


if __name__ == "__main__":
    unittest.main()

