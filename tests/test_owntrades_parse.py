import unittest

from trading.kraken.ws_auth import _parse_own_trades


class TestOwnTradesParse(unittest.TestCase):
    def test_parse_array_format(self):
        msg = [
            [
                {
                    "T1": {
                        "ordertxid": "O1",
                        "type": "buy",
                        "price": "100.0",
                        "vol": "0.01",
                        "fee": "0.1",
                        "time": 1700000000,
                    }
                }
            ],
            "ownTrades",
        ]
        updates = list(_parse_own_trades(msg))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].order_id, "O1")
        self.assertEqual(updates[0].side, "buy")


if __name__ == "__main__":
    unittest.main()
