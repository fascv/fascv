import unittest

from trading.processes.impact import _to_news_event


class TestImpactMapping(unittest.TestCase):
    def test_maps_arrow_payload_to_news_event(self):
        payload = {
            "final_score": -0.35,
            "news_driven_probability": 0.72,
            "contributing_items": 3,
            "attribution_event": {"id": "evt-1"},
            "signal_state": "risk_off",
            "attribution_state": "news_driven",
            "mode_effective": "llm",
        }
        evt = _to_news_event(payload, "XBT/EUR")
        self.assertEqual(evt.symbol, "XBT/EUR")
        self.assertAlmostEqual(evt.sentiment_score, -0.35)
        self.assertAlmostEqual(evt.impact_score, 0.72)
        self.assertEqual(evt.source_count, 3)
        self.assertEqual(evt.event_id, "evt-1")

    def test_clamps_values(self):
        payload = {
            "final_score": 3.0,
            "confidence": 5.0,
            "reasons": [1, 2],
            "attribution_event": "abc",
        }
        evt = _to_news_event(payload, "XBT/USD")
        self.assertEqual(evt.symbol, "XBT/USD")
        self.assertAlmostEqual(evt.sentiment_score, 1.0)
        self.assertAlmostEqual(evt.impact_score, 1.0)
        self.assertEqual(evt.source_count, 2)
        self.assertEqual(evt.event_id, "abc")


if __name__ == "__main__":
    unittest.main()
