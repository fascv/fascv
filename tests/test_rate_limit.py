import unittest
import json
import urllib.error

from trading.kraken.rest import KrakenAPIError, KrakenAuthError, KrakenRestClient


class TestRateLimit(unittest.TestCase):
    def test_rate_limit_error_detection(self):
        def fake_post(url, headers, payload):
            return json.dumps({"error": ["EAPI:Rate limit exceeded"], "result": {}})

        client = KrakenRestClient(api_key="k", api_secret="c2VjcmV0", http_post=fake_post)
        with self.assertRaises(KrakenAPIError) as ctx:
            client.add_order(pair="XBT/EUR", side="buy", order_type="market", volume="0.001")
        self.assertTrue(ctx.exception.is_rate_limit())

    def test_auth_error_classification(self):
        def fake_post(url, headers, payload):
            return json.dumps({"error": ["EAPI:Invalid key"], "result": {}})

        client = KrakenRestClient(api_key="k", api_secret="c2VjcmV0", http_post=fake_post)
        with self.assertRaises(KrakenAuthError) as ctx:
            client.open_orders()
        self.assertTrue(ctx.exception.is_auth())

    def test_transient_retry_on_open_orders(self):
        calls = {"n": 0}

        def fake_post(url, headers, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("temporary network issue")
            return json.dumps({"error": [], "result": {"open": {}}})

        client = KrakenRestClient(
            api_key="k",
            api_secret="c2VjcmV0",
            http_post=fake_post,
            max_retries=2,
            retry_backoff_sec=0.0,
        )
        result = client.open_orders()
        self.assertEqual(result, {"open": {}})
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
