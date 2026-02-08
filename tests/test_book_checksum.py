import unittest
from decimal import Decimal

from trading.kraken.book import OrderBook, BookChecksumError


class TestBookChecksum(unittest.TestCase):
    def test_checksum_matches_example(self):
        book = OrderBook(depth=100, checksum_depth=10)
        bids = [
            (Decimal("45283.5"), Decimal("0.10000000")),
            (Decimal("45283.4"), Decimal("1.54582015")),
            (Decimal("45282.1"), Decimal("0.10000000")),
            (Decimal("45281.0"), Decimal("0.10000000")),
            (Decimal("45280.3"), Decimal("1.54592586")),
            (Decimal("45279.0"), Decimal("0.07990000")),
            (Decimal("45277.6"), Decimal("0.03310103")),
            (Decimal("45277.5"), Decimal("0.30000000")),
            (Decimal("45277.3"), Decimal("1.54602737")),
            (Decimal("45276.6"), Decimal("0.15445238")),
        ]
        asks = [
            (Decimal("45285.2"), Decimal("0.00100000")),
            (Decimal("45286.4"), Decimal("1.54571953")),
            (Decimal("45286.6"), Decimal("1.54571109")),
            (Decimal("45289.6"), Decimal("1.54560911")),
            (Decimal("45290.2"), Decimal("0.15890660")),
            (Decimal("45291.8"), Decimal("1.54553491")),
            (Decimal("45294.7"), Decimal("0.04454749")),
            (Decimal("45296.1"), Decimal("0.35380000")),
            (Decimal("45297.5"), Decimal("0.09945542")),
            (Decimal("45299.5"), Decimal("0.18772827")),
        ]
        book.apply_snapshot(bids, asks)
        self.assertEqual(book.checksum(), 3310070434)

    def test_checksum_mismatch(self):
        book = OrderBook(depth=100, checksum_depth=10)
        bids = [(Decimal("100"), Decimal("1"))]
        asks = [(Decimal("101"), Decimal("1"))]
        book.apply_snapshot(bids, asks)
        with self.assertRaises(BookChecksumError):
            book.validate_checksum(123)


if __name__ == "__main__":
    unittest.main()
