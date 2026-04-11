import json
import os
import tempfile
import unittest

from trading.processes.control import _tail_json_lines


class TestControlTail(unittest.TestCase):
    def test_tail_last_n_json_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "journal.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for i in range(100):
                    f.write(json.dumps({"n": i}) + "\n")

            items = _tail_json_lines(path, 10)
            self.assertEqual(len(items), 10)
            self.assertEqual([it["n"] for it in items], list(range(90, 100)))

    def test_tail_without_trailing_newline(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "journal.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"n": 1}) + "\n")
                f.write(json.dumps({"n": 2}))

            items = _tail_json_lines(path, 2)
            self.assertEqual([it["n"] for it in items], [1, 2])


if __name__ == "__main__":
    unittest.main()
