import os
import sys
import unittest
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from trading_exec.gui import build_exec_snapshot


class TestExecGuiSnapshot(unittest.TestCase):
    def test_detects_status_regression(self):
        status_doc = {
            "data": {
                "exec": {"mode": "paper", "canary_mode": False},
                "core": {"trading_enabled": True},
            }
        }
        journal_items = [
            {
                "ts": "2026-02-14T10:00:00+00:00",
                "event_type": "exec_report",
                "payload": {"order_id": "o1", "status": "OPEN"},
            },
            {
                "ts": "2026-02-14T10:00:01+00:00",
                "event_type": "exec_report",
                "payload": {"order_id": "o1", "status": "ACK"},
            },
        ]

        snap = build_exec_snapshot(status_doc, journal_items)
        self.assertEqual(len(snap["orders"]), 1)
        self.assertIn("status_regression:OPEN->ACK", snap["orders"][0]["issues"])

    def test_detects_unbalanced_rate_limit_pause(self):
        status_doc = {"data": {"exec": {}, "core": {}}}
        journal_items = [
            {
                "ts": "2026-02-14T10:00:00+00:00",
                "event_type": "exec_rate_limit_pause",
                "payload": {"source": "order_intent"},
            }
        ]

        snap = build_exec_snapshot(status_doc, journal_items, now=datetime.now(timezone.utc))
        issue_codes = {issue["code"] for issue in snap["issues"]}
        self.assertIn("rate_limit_unbalanced", issue_codes)

    def test_deadman_disable_not_warned_in_paper_mode(self):
        status_doc = {
            "data": {
                "exec": {"mode": "paper", "canary_mode": False},
                "core": {"trading_enabled": True},
            }
        }
        journal_items = [
            {
                "ts": "2026-02-14T10:00:00+00:00",
                "event_type": "deadman",
                "payload": {"event": "disable", "timeout": 0, "reason": "pause"},
            }
        ]
        snap = build_exec_snapshot(
            status_doc,
            journal_items,
            now=datetime(2026, 2, 14, 10, 0, 5, tzinfo=timezone.utc),
        )
        issue_codes = {issue["code"] for issue in snap["issues"]}
        self.assertNotIn("deadman_disabled_while_trading", issue_codes)

    def test_deadman_disable_warned_live_when_recent_and_no_tick(self):
        status_doc = {
            "data": {
                "exec": {"mode": "live", "canary_mode": False},
                "core": {"trading_enabled": True},
            }
        }
        journal_items = [
            {
                "ts": "2026-02-14T10:00:00+00:00",
                "event_type": "deadman",
                "payload": {"event": "disable", "timeout": 0, "reason": "pause"},
            }
        ]
        snap = build_exec_snapshot(
            status_doc,
            journal_items,
            now=datetime(2026, 2, 14, 10, 0, 5, tzinfo=timezone.utc),
        )
        issue_codes = {issue["code"] for issue in snap["issues"]}
        self.assertIn("deadman_disabled_while_trading", issue_codes)

    def test_deadman_disable_not_warned_when_newer_tick_exists(self):
        status_doc = {
            "data": {
                "exec": {"mode": "live", "canary_mode": False},
                "core": {"trading_enabled": True},
            }
        }
        journal_items = [
            {
                "ts": "2026-02-14T10:00:00+00:00",
                "event_type": "deadman",
                "payload": {"event": "disable", "timeout": 0, "reason": "pause"},
            },
            {
                "ts": "2026-02-14T10:00:10+00:00",
                "event_type": "deadman",
                "payload": {"event": "tick", "timeout": 60},
            },
        ]
        snap = build_exec_snapshot(
            status_doc,
            journal_items,
            now=datetime(2026, 2, 14, 10, 0, 15, tzinfo=timezone.utc),
        )
        issue_codes = {issue["code"] for issue in snap["issues"]}
        self.assertNotIn("deadman_disabled_while_trading", issue_codes)


if __name__ == "__main__":
    unittest.main()
