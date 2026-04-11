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

    def test_parse_sequence_event_id(self):
        msg = [
            [
                {"sequence": 77},
                {
                    "T2": {
                        "ordertxid": "O2",
                        "type": "sell",
                        "price": "101.0",
                        "vol": "0.02",
                        "fee": "0.2",
                        "time": 1700000001,
                    }
                },
            ],
            "ownTrades",
        ]
        updates = list(_parse_own_trades(msg))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].event_id, "77")

    def test_parse_auth_ws_common_format(self):
        msg = [
            42,
            {
                "T3": {
                    "ordertxid": "O3",
                    "type": "buy",
                    "price": "102.0",
                    "vol": "0.03",
                    "fee": "0.3",
                    "time": 1700000002,
                }
            },
            "ownTrades",
            88,
        ]
        updates = list(_parse_own_trades(msg))
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].order_id, "O3")
        self.assertEqual(updates[0].event_id, "88")


if __name__ == "__main__":
    unittest.main()
