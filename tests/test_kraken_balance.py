import json
import unittest

from trading.kraken.rest import KrakenRestClient


class TestKrakenBalance(unittest.TestCase):
    def test_balance_calls_private_endpoint(self):
        calls = {"url": None}

        def fake_post(url, headers, payload):
            calls["url"] = url
            return json.dumps({"error": [], "result": {"ZEUR": "1.23"}})

        client = KrakenRestClient(api_key="k", api_secret="c2VjcmV0", http_post=fake_post)
        out = client.balance()
        self.assertEqual(out, {"ZEUR": "1.23"})
        self.assertTrue(str(calls["url"]).endswith("/0/private/Balance"))


if __name__ == "__main__":
    unittest.main()

