import unittest
from datetime import datetime, timezone

from trading.ipc.events import ControlCommand


class TestControlCommandPayload(unittest.TestCase):
    def test_payload_default_is_dict(self):
        cmd = ControlCommand(ts=datetime.now(timezone.utc), action="STOP", reason="x")
        self.assertIsInstance(cmd.payload, dict)
        self.assertEqual(cmd.payload, {})

    def test_payload_roundtrip(self):
        cmd = ControlCommand(
            ts=datetime.now(timezone.utc),
            action="SET_BUDGET",
            reason="budget_reset",
            payload={"starting_cash_eur": 1000.0, "max_exposure_eur": 200.0, "reset": True},
        )
        self.assertEqual(cmd.payload["starting_cash_eur"], 1000.0)
        self.assertEqual(cmd.payload["max_exposure_eur"], 200.0)
        self.assertTrue(cmd.payload["reset"])


if __name__ == "__main__":
    unittest.main()

