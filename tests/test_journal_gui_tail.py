import json
import os
import sys
import tempfile
import unittest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_JOURNAL_SRC = os.path.join(_REPO_ROOT, "modules", "journal", "src")
if _JOURNAL_SRC not in sys.path:
    sys.path.insert(0, _JOURNAL_SRC)

from trading_journal.gui import _jsonl_ends_with_newline, tail_jsonl  # noqa: E402


class TestJournalGuiTail(unittest.TestCase):
    def test_tail_jsonl_counts_parse_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "journal_events.jsonl")
            lines = [
                json.dumps({"ts": "2026-02-14T00:00:00+00:00", "event_type": "a", "payload": {}}),
                "{broken json",
                json.dumps({"ts": "2026-02-14T00:00:01+00:00", "event_type": "b", "payload": {"x": 1}}),
                "",
            ]
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

            out = tail_jsonl(p, 50)
            self.assertEqual(out["lines_requested"], 50)
            self.assertEqual(out["parse_errors_in_sample"], 1)
            self.assertEqual(len(out["items"]), 2)

    def test_tail_jsonl_filter_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "journal_events.jsonl")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": "t", "event_type": "exec_report", "payload": {}}) + "\n")
                fh.write(json.dumps({"ts": "t", "event_type": "core_start", "payload": {}}) + "\n")

            out = tail_jsonl(p, 50, event_type_contains="exec_")
            self.assertEqual(len(out["items"]), 1)
            self.assertEqual(out["items"][0]["event_type"], "exec_report")

    def test_jsonl_ends_with_newline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "journal_events.jsonl")
            with open(p, "wb") as fh:
                fh.write(b"{\"a\":1}")
            self.assertEqual(_jsonl_ends_with_newline(p), False)
            with open(p, "ab") as fh:
                fh.write(b"\n")
            self.assertEqual(_jsonl_ends_with_newline(p), True)


if __name__ == "__main__":
    unittest.main()

