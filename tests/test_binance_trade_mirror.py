from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trading.binance import trade_mirror


class _FakeClient:
    def __init__(self, batches: list[list[dict[str, object]]]) -> None:
        self._batches = list(batches)

    def my_trades_window(
        self,
        *,
        symbol: str | None = None,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
        from_id: int | None = None,
    ) -> list[dict[str, object]]:
        if not self._batches:
            return []
        return list(self._batches.pop(0))

    def commission_to_quote(
        self,
        *,
        symbol: str,
        commission: float,
        commission_asset: str,
        trade_price: float = 0.0,
    ) -> float:
        if commission_asset == "USDC":
            return float(commission)
        if commission_asset == "RENDER":
            return float(commission) * float(trade_price or 0.0)
        return 0.0


class TestBinanceTradeMirror(unittest.TestCase):
    def test_normalize_binance_trade_base_fee_not_double_counted(self) -> None:
        buy = trade_mirror.normalize_binance_trade(
            "RENDERUSDC",
            {
                "id": 1,
                "orderId": 11,
                "price": "2.0",
                "qty": "10.0",
                "quoteQty": "20.0",
                "commission": "0.01",
                "commissionAsset": "RENDER",
                "time": 1000,
                "isBuyer": True,
            },
        )
        sell = trade_mirror.normalize_binance_trade(
            "RENDERUSDC",
            {
                "id": 2,
                "orderId": 12,
                "price": "2.1",
                "qty": "10.0",
                "quoteQty": "21.0",
                "commission": "0.01",
                "commissionAsset": "RENDER",
                "time": 2000,
                "isBuyer": False,
            },
        )

        assert buy is not None
        assert sell is not None
        self.assertAlmostEqual(buy["quantity"], 9.99, places=9)
        self.assertAlmostEqual(buy["grossUsdc"], 20.0, places=9)
        self.assertAlmostEqual(sell["quantity"], 10.01, places=9)
        self.assertAlmostEqual(sell["grossUsdc"], 21.0, places=9)

    def test_collect_trades_mirror_builds_fifo_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror_dir = Path(tmp)
            rows = [
                {
                    "symbol": "RENDERUSDC",
                    "coin": "RENDER",
                    "side": "BUY",
                    "orderId": "1",
                    "tradeId": "11",
                    "timeMs": 1773619201000,
                    "timeIso": "2026-03-16T00:00:01Z",
                    "quantity": 4.13,
                    "grossUsdc": 7.66529417,
                },
                {
                    "symbol": "RENDERUSDC",
                    "coin": "RENDER",
                    "side": "SELL",
                    "orderId": "2",
                    "tradeId": "12",
                    "timeMs": 1773619202000,
                    "timeIso": "2026-03-16T00:00:02Z",
                    "quantity": 4.12,
                    "grossUsdc": 7.62687519,
                },
            ]
            trade_mirror.write_mirror_rows("RENDERUSDC", rows, mirror_dir)
            report = trade_mirror.collect_trades_mirror(
                "2026-03-16T00:00:00Z",
                "2026-03-16T01:00:00Z",
                ["RENDERUSDC"],
                mirror_dir=mirror_dir,
            )

        self.assertEqual(report["source"], "binance_mirror")
        self.assertEqual(report["fillEvents"], 2)
        self.assertEqual(report["daySummary"]["bundleCount"], 1)
        self.assertAlmostEqual(report["daySummary"]["buyGrossUsdc"], 7.64673414, places=6)
        self.assertAlmostEqual(report["daySummary"]["sellGrossUsdc"], 7.62687519, places=6)
        self.assertAlmostEqual(report["daySummary"]["proceedsUsdc"], -0.01985895, places=6)
        self.assertAlmostEqual(report["daySummary"]["matchedBuyGrossUsdc"], 7.64673414, places=6)

    def test_collect_trades_mirror_reprices_legacy_base_fee_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror_dir = Path(tmp)
            rows = [
                {
                    "symbol": "RENDERUSDC",
                    "coin": "RENDER",
                    "side": "BUY",
                    "orderId": "1",
                    "tradeId": "11",
                    "timeMs": 1773619201000,
                    "timeIso": "2026-03-16T00:00:01Z",
                    "quantity": 10.0,
                    # Legacy mirror overstated BUY cost when fee asset was base.
                    "grossUsdc": 20.02,
                    "price": 2.0,
                    "quoteQty": 20.0,
                    "feeUsdc": 0.02,
                    "commissionAsset": "RENDER",
                },
                {
                    "symbol": "RENDERUSDC",
                    "coin": "RENDER",
                    "side": "SELL",
                    "orderId": "2",
                    "tradeId": "12",
                    "timeMs": 1773619202000,
                    "timeIso": "2026-03-16T00:00:02Z",
                    "quantity": 10.0,
                    "grossUsdc": 20.18,
                    "price": 2.02,
                    "quoteQty": 20.2,
                    "feeUsdc": 0.02,
                    "commissionAsset": "USDC",
                },
            ]
            trade_mirror.write_mirror_rows("RENDERUSDC", rows, mirror_dir)
            report = trade_mirror.collect_trades_mirror(
                "2026-03-16T00:00:00Z",
                "2026-03-16T01:00:00Z",
                ["RENDERUSDC"],
                mirror_dir=mirror_dir,
            )

        self.assertEqual(report["source"], "binance_mirror")
        self.assertEqual(report["fillEvents"], 2)
        self.assertAlmostEqual(report["daySummary"]["buyGrossUsdc"], 20.0, places=9)
        self.assertAlmostEqual(report["daySummary"]["sellGrossUsdc"], 20.18, places=9)
        self.assertAlmostEqual(report["daySummary"]["proceedsUsdc"], 0.18, places=9)

    def test_sync_symbol_window_merges_and_dedupes_trade_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror_dir = Path(tmp)
            existing = [
                {
                    "symbol": "RENDERUSDC",
                    "coin": "RENDER",
                    "side": "BUY",
                    "orderId": "100",
                    "tradeId": "900",
                    "timeMs": 1000,
                    "timeIso": "2026-03-16T00:00:01Z",
                    "quantity": 1.0,
                    "grossUsdc": 1.01,
                    "price": 1.0,
                    "quoteQty": 1.0,
                    "feeUsdc": 0.01,
                    "commissionAsset": "USDC",
                }
            ]
            trade_mirror.write_mirror_rows("RENDERUSDC", existing, mirror_dir)
            client = _FakeClient(
                [
                    [
                        {
                            "id": 900,
                            "orderId": 100,
                            "price": "1.0",
                            "qty": "1.0",
                            "quoteQty": "1.0",
                            "commission": "0.01",
                            "commissionAsset": "USDC",
                            "time": 1000,
                            "isBuyer": True,
                        },
                        {
                            "id": 901,
                            "orderId": 101,
                            "price": "1.2",
                            "qty": "2.0",
                            "quoteQty": "2.4",
                            "commission": "0.02",
                            "commissionAsset": "USDC",
                            "time": 2000,
                            "isBuyer": False,
                        },
                    ]
                ]
            )
            stats = trade_mirror.sync_symbol_window(
                client,
                symbol="RENDERUSDC",
                start_ms=0,
                end_ms=4000,
                mirror_dir=mirror_dir,
            )
            rows = trade_mirror.load_mirror_rows("RENDERUSDC", mirror_dir)

        self.assertEqual(stats["fetchedRows"], 2)
        self.assertEqual(stats["newRows"], 1)
        self.assertEqual(stats["totalRows"], 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["tradeId"], "901")


if __name__ == "__main__":
    unittest.main()
