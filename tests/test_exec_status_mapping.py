import unittest

from trading.processes.exec import _map_status


class TestExecStatusMapping(unittest.TestCase):
    def test_partial_fill_mapping(self):
        self.assertEqual(_map_status("closed", 1.0, 0.5), "PARTIAL")
        self.assertEqual(_map_status("closed", 1.0, 1.0), "FILLED")
        self.assertEqual(_map_status("canceled", 1.0, 0.2), "CANCELED")


if __name__ == "__main__":
    unittest.main()
