import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from trading.ipc.events import JournalEvent
from trading.processes.context import ProcessContext
from trading.processes.journal import run_journal


def _wait_until(pred, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class TestJournalPersistence(unittest.TestCase):
    def test_jsonl_and_sqlite_persist_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "journal.db")
            json_path = os.path.join(td, "journal_events.jsonl")

            stop_event = threading.Event()
            q = queue.Queue()
            ctx = ProcessContext(
                mode="test",
                config={
                    "journal": {
                        "db_path": db_path,
                        "json_path": json_path,
                        "flush_every_n": 1,
                        "flush_every_sec": 0.05,
                        "fsync_on_flush": False,
                        "heartbeat_interval": 999.0,
                        "telemetry_interval": 999.0,
                    }
                },
                stop_event=stop_event,
                q_market_core=queue.Queue(),
                q_market_exec=queue.Queue(),
                q_order_intent=queue.Queue(),
                q_exec_report=queue.Queue(),
                q_journal=q,
                q_control_core=queue.Queue(),
                q_control_exec=queue.Queue(),
                q_telemetry=queue.Queue(),
                q_heartbeat=queue.Queue(),
                q_impact_core=None,
            )

            t = threading.Thread(target=run_journal, args=(ctx,), daemon=True)
            t.start()

            n = 50
            for i in range(n):
                payload = {
                    "i": i,
                    "dec": Decimal("1.23"),
                    "dt": datetime.now(timezone.utc),
                }
                q.put(JournalEvent(ts=datetime.now(timezone.utc), event_type="test_evt", payload=payload))

            def jsonl_has_n() -> bool:
                if not os.path.exists(json_path):
                    return False
                try:
                    with open(json_path, "r", encoding="utf-8") as fh:
                        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
                    if len(lines) != n:
                        return False
                    for ln in lines:
                        obj = json.loads(ln)
                        if not isinstance(obj, dict):
                            return False
                        if set(obj.keys()) != {"ts", "event_type", "payload"}:
                            return False
                    return True
                except Exception:
                    return False

            def sqlite_has_n() -> bool:
                if not os.path.exists(db_path):
                    return False
                try:
                    conn = sqlite3.connect(db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM events")
                    (cnt,) = cur.fetchone()
                    conn.close()
                    return int(cnt) == n
                except Exception:
                    return False

            self.assertTrue(_wait_until(jsonl_has_n, timeout=5.0))
            self.assertTrue(_wait_until(sqlite_has_n, timeout=5.0))

            stop_event.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())

            # JSONL: every line parseable and matches the stable format.
            with open(json_path, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), n)
            for ln in lines:
                obj = json.loads(ln)
                self.assertEqual(obj["event_type"], "test_evt")
                self.assertIsInstance(obj["ts"], str)
                self.assertIn("i", obj["payload"])

            # SQLite: schema contains new columns and rows are present.
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(events)")
            cols = [r[1] for r in cur.fetchall()]
            for expected in ["id", "ts", "ts_unix", "event_type", "payload_json"]:
                self.assertIn(expected, cols)
            cur.execute("SELECT event_type, ts, payload_json FROM events ORDER BY id LIMIT 5")
            rows = cur.fetchall()
            conn.close()
            self.assertTrue(rows)
            for et, ts, payload_json in rows:
                self.assertEqual(et, "test_evt")
                self.assertIsInstance(ts, str)
                self.assertIsInstance(payload_json, str)
                json.loads(payload_json)

    def test_jsonl_rotation_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "journal.db")
            json_path = os.path.join(td, "journal_events.jsonl")

            stop_event = threading.Event()
            q = queue.Queue()
            ctx = ProcessContext(
                mode="test",
                config={
                    "journal": {
                        "db_path": db_path,
                        "json_path": json_path,
                        "flush_every_n": 50,
                        "flush_every_sec": 0.05,
                        "fsync_on_flush": False,
                        "heartbeat_interval": 999.0,
                        "telemetry_interval": 999.0,
                        "rotate_max_bytes": 600,
                        "rotate_keep": 2,
                    }
                },
                stop_event=stop_event,
                q_market_core=queue.Queue(),
                q_market_exec=queue.Queue(),
                q_order_intent=queue.Queue(),
                q_exec_report=queue.Queue(),
                q_journal=q,
                q_control_core=queue.Queue(),
                q_control_exec=queue.Queue(),
                q_telemetry=queue.Queue(),
                q_heartbeat=queue.Queue(),
                q_impact_core=None,
            )

            t = threading.Thread(target=run_journal, args=(ctx,), daemon=True)
            t.start()

            for i in range(200):
                q.put(
                    JournalEvent(
                        ts=datetime.now(timezone.utc),
                        event_type="rot_evt",
                        payload={"i": i, "msg": "x" * 40},
                    )
                )

            def rotated_exists() -> bool:
                root, ext = os.path.splitext(os.path.basename(json_path))
                ext = ext or ".jsonl"
                names = os.listdir(td)
                base = os.path.basename(json_path)
                rotated = [n for n in names if n != base and n.startswith(root + ".") and n.endswith(ext)]
                return len(rotated) >= 1

            self.assertTrue(_wait_until(rotated_exists, timeout=10.0))

            stop_event.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())

            root, ext = os.path.splitext(os.path.basename(json_path))
            ext = ext or ".jsonl"
            names = os.listdir(td)
            base = os.path.basename(json_path)
            rotated = sorted([n for n in names if n != base and n.startswith(root + ".") and n.endswith(ext)])
            self.assertLessEqual(len(rotated), 2)
            self.assertTrue(os.path.exists(json_path))


if __name__ == "__main__":
    unittest.main()
