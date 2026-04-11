import unittest
from datetime import datetime, timezone

from trading.execution.state_machine import OrderStateMachine


class TestOrderStateMachine(unittest.TestCase):
    def test_transitions(self):
        sm = OrderStateMachine()
        now = datetime.now(timezone.utc)
        self.assertTrue(sm.transition("o1", "NEW", now))
        self.assertTrue(sm.transition("o1", "ACK", now))
        self.assertTrue(sm.transition("o1", "OPEN", now))
        self.assertTrue(sm.transition("o1", "PARTIAL", now))
        self.assertTrue(sm.transition("o1", "FILLED", now))
        self.assertFalse(sm.transition("o1", "OPEN", now))

    def test_invalid_transition(self):
        sm = OrderStateMachine()
        now = datetime.now(timezone.utc)
        self.assertFalse(sm.transition("o2", "FILLED", now))

    def test_recovery_transition_from_unknown_order(self):
        sm = OrderStateMachine()
        now = datetime.now(timezone.utc)
        self.assertTrue(sm.transition("o3", "OPEN", now, allow_recovery=True))
        self.assertTrue(sm.transition("o3", "PARTIAL", now))


if __name__ == "__main__":
    unittest.main()
