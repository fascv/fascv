import unittest
import json

from trading.kraken.rest import KrakenRestClient, KrakenAPIError


class TestRateLimit(unittest.TestCase):
    def test_rate_limit_error_detection(self):
        def fake_post(url, headers, payload):
            return json.dumps({"error": ["EAPI:Rate limit exceeded"], "result": {}})

        client = KrakenRestClient(api_key="k", api_secret="c2VjcmV0", http_post=fake_post)
        with self.assertRaises(KrakenAPIError) as ctx:
            client.add_order(pair="XBT/EUR", side="buy", order_type="market", volume="0.001")
        self.assertTrue(ctx.exception.is_rate_limit())


if __name__ == "__main__":
    unittest.main()
