import unittest

from trading.utils.kraken import map_pair, to_kraken_rest_pair


class TestKrakenPairMappingWSV2(unittest.TestCase):
    def test_map_pair_normalizes_xbt_to_btc(self) -> None:
        self.assertEqual(map_pair("XBT/EUR"), "BTC/EUR")
        self.assertEqual(map_pair("xbt/eur"), "BTC/EUR")
        self.assertEqual(map_pair("XBT-USD"), "BTC/USD")

    def test_map_pair_keeps_btc(self) -> None:
        self.assertEqual(map_pair("BTC/EUR"), "BTC/EUR")
        self.assertEqual(map_pair("btc/eur"), "BTC/EUR")

    def test_to_kraken_rest_pair_accepts_btc_or_xbt(self) -> None:
        self.assertEqual(to_kraken_rest_pair("BTC/EUR"), "XBTEUR")
        self.assertEqual(to_kraken_rest_pair("XBT/EUR"), "XBTEUR")

