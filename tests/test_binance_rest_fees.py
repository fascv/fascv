import unittest

from trading.binance.rest import BinanceRestClient


class _FakeBinanceClient(BinanceRestClient):
    def __init__(self):
        super().__init__(api_key="k", api_secret="s", symbol="ETHUSDC")
        self.calls = []

    def _request(self, method, path, params=None, *, signed, api_key=False, max_retries=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": dict(params or {}),
                "signed": bool(signed),
                "api_key": bool(api_key),
            }
        )
        p = path.strip()
        if p == "/api/v3/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": "ETHUSDC",
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.0000", "stepSize": "0.0000"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "NOTIONAL", "minNotional": "5.0"},
                        ],
                    }
                ]
            }
        if p == "/api/v3/myTrades":
            return [
                {
                    "qty": "0.001",
                    "price": "2000",
                    "quoteQty": "2.0",
                    "commission": "0.0001",
                    "commissionAsset": "BNB",
                },
                {
                    "qty": "0.002",
                    "price": "2001",
                    "quoteQty": "4.002",
                    "commission": "0.0002",
                    "commissionAsset": "BNB",
                },
            ]
        if p == "/api/v3/ticker/price":
            sym = str((params or {}).get("symbol", ""))
            if sym == "BNBUSDC":
                return {"symbol": "BNBUSDC", "price": "300"}
            return {"symbol": sym, "price": "0"}
        if p == "/api/v3/userDataStream":
            m = method.upper()
            if m == "POST":
                return {"listenKey": "lk_test_1"}
            return {}
        return {}


class TestBinanceRestFees(unittest.TestCase):
    def test_order_trade_summary_converts_bnb_fee_to_quote(self):
        c = _FakeBinanceClient()
        fee_quote, qty, quote = c._order_trade_summary({"orderId": 123, "symbol": "ETHUSDC"})
        self.assertAlmostEqual(qty, 0.003, places=9)
        self.assertAlmostEqual(quote, 6.002, places=9)
        # (0.0001 + 0.0002) BNB * 300 USDC/BNB
        self.assertAlmostEqual(fee_quote, 0.09, places=9)

    def test_user_data_stream_endpoints_use_api_key_without_signature(self):
        c = _FakeBinanceClient()
        key = c.start_user_data_stream()
        c.keepalive_user_data_stream(key)
        c.close_user_data_stream(key)

        ud_calls = [x for x in c.calls if x["path"] == "/api/v3/userDataStream"]
        self.assertEqual(len(ud_calls), 3)
        self.assertTrue(all(call["api_key"] for call in ud_calls))
        self.assertTrue(all(not call["signed"] for call in ud_calls))

    def test_min_notional_exposed_from_exchange_info(self):
        c = _FakeBinanceClient()
        self.assertAlmostEqual(c.min_notional("ETHUSDC"), 5.0, places=9)


if __name__ == "__main__":
    unittest.main()
