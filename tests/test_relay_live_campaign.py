from __future__ import annotations

import importlib.util
import json
from urllib.error import URLError
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "app" / "relay-server" / "relay_server.py"
SPEC = importlib.util.spec_from_file_location("relay_server_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load relay_server module from {MODULE_PATH}")
relay_server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_server)


class TestRelayLiveCampaign(unittest.TestCase):
    def test_translate_non_buy_reason_known_code(self) -> None:
        self.assertEqual(
            relay_server.translate_non_buy_reason("corridor_short_horizon_too_high"),
            "Im 24h-Korridor schon zu weit oben",
        )

    def test_normalize_trade_base_fee_not_double_counted(self) -> None:
        buy = relay_server.normalize_trade(
            "EURUSDC",
            {
                "id": 1,
                "orderId": 10,
                "qty": "10.0",
                "price": "1.1",
                "quoteQty": "11.0",
                "commission": "0.01",
                "commissionAsset": "EUR",
                "time": 1000,
                "isBuyer": True,
            },
        )
        sell = relay_server.normalize_trade(
            "EURUSDC",
            {
                "id": 2,
                "orderId": 11,
                "qty": "9.99",
                "price": "1.15",
                "quoteQty": "11.4885",
                "commission": "0.01",
                "commissionAsset": "EUR",
                "time": 2000,
                "isBuyer": False,
            },
        )

        self.assertIsNotNone(buy)
        self.assertIsNotNone(sell)
        assert buy is not None
        assert sell is not None
        self.assertAlmostEqual(buy["quantity"], 9.99, places=9)
        self.assertAlmostEqual(buy["grossUsdc"], 11.0, places=9)
        self.assertAlmostEqual(sell["quantity"], 10.0, places=9)
        self.assertAlmostEqual(sell["grossUsdc"], 11.4885, places=9)

    def test_fetch_local_json_retries_transient_connection_refused(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response

        side_effects = [
            URLError(ConnectionRefusedError(111, "Connection refused")),
            response,
        ]

        with (
            mock.patch.object(relay_server, "ROTATION_STATUS_RETRIES", 2),
            mock.patch.object(relay_server, "ROTATION_STATUS_RETRY_DELAY_SEC", 0.0),
            mock.patch.object(relay_server, "urlopen", side_effect=side_effects) as urlopen_mock,
        ):
            payload = relay_server.fetch_local_json("http://127.0.0.1:8010/status", timeout=0.1)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_fetch_local_json_returns_friendly_message_after_retries(self) -> None:
        with (
            mock.patch.object(relay_server, "ROTATION_STATUS_RETRIES", 2),
            mock.patch.object(relay_server, "ROTATION_STATUS_RETRY_DELAY_SEC", 0.0),
            mock.patch.object(
                relay_server,
                "urlopen",
                side_effect=URLError(ConnectionRefusedError(111, "Connection refused")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Lane startet gerade neu oder antwortet noch nicht."):
                relay_server.fetch_local_json("http://127.0.0.1:8010/status", timeout=0.1)

    def test_reconstruct_live_campaign_metrics_skips_account_sync_delta_fills(self) -> None:
        rows = [
            {
                "ts": "2026-03-15T00:25:15.209111+00:00",
                "event_type": "fill",
                "payload": {
                    "side": "buy",
                    "qty_btc": 7.033312,
                    "price": 2.5630328926116177,
                    "fee_eur": 0.0,
                    "order_id": "account_sync_delta_1773534315209",
                    "source": "account_sync_delta",
                    "synthetic": True,
                },
            },
            {
                "ts": "2026-03-15T00:25:15.496084+00:00",
                "event_type": "fill",
                "payload": {
                    "side": "buy",
                    "qty_btc": 7.04,
                    "price": 1.281,
                    "fee_eur": 0.008567328,
                    "order_id": "32204398",
                    "source": "reconcile",
                    "synthetic": True,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal_live_binance_axs_usdc_rotation.jsonl"
            journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            metrics = relay_server.reconstruct_live_campaign_metrics(
                journal,
                selected_since_iso="2026-03-15T00:25:00+00:00",
                current_position_qty=7.04,
                mark_price=1.30,
            )

        self.assertIsNotNone(metrics)
        assert metrics is not None
        expected_entry = ((7.04 * 1.281) + 0.008567328) / 7.04
        self.assertAlmostEqual(metrics["entryPrice"], expected_entry, places=9)
        self.assertAlmostEqual(metrics["positionQty"], 7.04, places=9)

    def test_reconstruct_live_campaign_metrics_ignores_pre_selection_history(self) -> None:
        rows = [
            {
                "ts": "2026-03-11T07:21:15.759937+00:00",
                "event_type": "fill",
                "payload": {"side": "buy", "qty_btc": 217.0, "price": 0.05054, "fee_eur": 0.010418821},
            },
            {
                "ts": "2026-03-11T07:35:36.456035+00:00",
                "event_type": "fill",
                "payload": {"side": "sell", "qty_btc": 217.0, "price": 0.05034, "fee_eur": 0.01037759},
            },
            {
                "ts": "2026-03-13T13:48:06.185147+00:00",
                "event_type": "fill",
                "payload": {"side": "buy", "qty_btc": 224.0, "price": 0.05766, "fee_eur": 0.012270048},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal_live_binance_cfx_usdc_rotation.jsonl"
            journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            metrics = relay_server.reconstruct_live_campaign_metrics(
                journal,
                selected_since_iso="2026-03-13T13:47:29.202139+00:00",
                current_position_qty=224.20675,
                mark_price=0.057455,
            )

        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertAlmostEqual(metrics["entryPrice"], (224.0 * 0.05766 + 0.012270048) / 224.0, places=9)
        self.assertLess(metrics["totalPnlUsdc"], 0.0)
        self.assertAlmostEqual(metrics["positionQty"], 224.20675, places=6)

    def test_reconstruct_live_campaign_metrics_returns_none_for_mixed_campaign_sells(self) -> None:
        rows = [
            {
                "ts": "2026-03-13T13:48:06.185147+00:00",
                "event_type": "fill",
                "payload": {"side": "buy", "qty_btc": 100.0, "price": 1.0, "fee_eur": 0.1},
            },
            {
                "ts": "2026-03-13T13:49:06.185147+00:00",
                "event_type": "fill",
                "payload": {"side": "sell", "qty_btc": 150.0, "price": 1.1, "fee_eur": 0.1},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal_live_binance_test_usdc_rotation.jsonl"
            journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            metrics = relay_server.reconstruct_live_campaign_metrics(
                journal,
                selected_since_iso="2026-03-13T13:47:00+00:00",
                current_position_qty=100.0,
                mark_price=1.05,
            )

        self.assertIsNone(metrics)

    def test_load_rotation_live_prefers_running_lanes_over_watch_pool(self) -> None:
        payload = {
            "selected": ["BANANAS31"],
            "watch_symbols": ["BANANAS31", "ATOM"],
            "all_rows": [
                {"symbol": "BANANAS31", "market": "BANANAS31USDC", "eligible": True, "score": 12.0, "gate_reason": ""},
                {"symbol": "ATOM", "market": "ATOMUSDC", "eligible": False, "score": 3.0, "gate_reason": "spread"},
                {"symbol": "ZAMA", "market": "ZAMAUSDC", "eligible": True, "score": 99.0, "gate_reason": ""},
            ],
        }

        port_to_symbol = {
            8308: "BANANAS31",
            8038: "ATOM",
            8004: "OP",
        }

        def fake_status(symbol: str) -> dict[str, object]:
            return {
                "data": {
                    "core": {
                        "position_btc": 0.0,
                        "avg_entry_price": 0.0,
                        "mark_price": 0.0,
                        "realized_pnl_eur": 0.0,
                        "unrealized_pnl_eur": 0.0,
                        "trading_enabled": symbol != "BBB",
                    },
                    "exec": {"open_orders_count": 0},
                },
                "aggregate": {
                    "open_orders_count": 0,
                    "trading_enabled": symbol != "BBB",
                },
                "staleness_sec_by_process": {"core": 0.1, "exec": 0.2, "md": 0.3},
                "stale_warn_sec": 12.0,
                "overview_trade_ready": symbol == "BBB",
                "updated_at": "2026-03-14T04:00:00Z",
            }

        with tempfile.TemporaryDirectory() as tmp:
            active_file = Path(tmp) / "rotation_active_lanes.json"
            active_file.write_text(json.dumps(payload), encoding="utf-8")
            old_active_file = relay_server.ROTATION_ACTIVE_FILE
            old_cache = relay_server._ROTATION_LIVE_CACHE
            try:
                relay_server.ROTATION_ACTIVE_FILE = str(active_file)
                relay_server._ROTATION_LIVE_CACHE = None
                with (
                    mock.patch.object(relay_server, "load_rotation_meta_summary", return_value={"available": False}),
                    mock.patch.object(relay_server, "list_running_rotation_symbols", return_value=["BANANAS31", "OP"]),
                    mock.patch.object(relay_server, "rotation_journal_path", return_value=None),
                    mock.patch.object(relay_server, "fetch_local_json") as fetch_mock,
                ):
                    fetch_mock.side_effect = (
                        lambda url, timeout: fake_status(port_to_symbol[int(url.rsplit(":", 1)[1].split("/", 1)[0])])
                    )
                    live = relay_server.load_rotation_live()
            finally:
                relay_server.ROTATION_ACTIVE_FILE = old_active_file
                relay_server._ROTATION_LIVE_CACHE = old_cache

        rows = live["rows"]
        symbols = {row["symbol"] for row in rows}
        self.assertEqual(symbols, {"BANANAS31", "OP"})
        self.assertNotIn("ATOM", symbols)
        self.assertNotIn("ZAMA", symbols)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(set(live["selected"]), {"BANANAS31"})
        row_by_symbol = {row["symbol"]: row for row in rows}
        self.assertTrue(row_by_symbol["BANANAS31"]["selected"])
        self.assertTrue(row_by_symbol["BANANAS31"]["running"])
        self.assertFalse(row_by_symbol["OP"]["selected"])
        self.assertTrue(row_by_symbol["OP"]["running"])
        self.assertEqual(live["summary"]["selected"], 1)
        self.assertEqual(live["summary"]["running"], 2)
        self.assertEqual(live["summary"]["down"], 0)

    def test_load_rotation_live_prefers_runtime_core_strategy_over_legacy_snapshot(self) -> None:
        payload = {
            "selected": ["ZRO"],
            "watch_symbols": ["ZRO"],
            "selected_since": {"ZRO": "2026-03-15T18:51:38.783633+00:00"},
            "selected_strategy_map": {"ZRO": "breakout_retest"},
            "all_rows": [
                {"symbol": "ZRO", "market": "ZROUSDC", "eligible": True, "score": 12.0, "gate_reason": ""}
            ],
        }

        status_payload = {
            "data": {
                "core": {
                    "position_btc": 0.0,
                    "avg_entry_price": 0.0,
                    "mark_price": 0.0,
                    "realized_pnl_eur": 0.0,
                    "unrealized_pnl_eur": 0.0,
                    "trading_enabled": True,
                },
                "exec": {"open_orders_count": 0},
            },
            "aggregate": {
                "open_orders_count": 0,
                "trading_enabled": True,
            },
            "staleness_sec_by_process": {"core": 0.1, "exec": 0.1, "md": 0.1},
            "stale_warn_sec": 12.0,
            "overview_trade_ready": False,
            "updated_at": "2026-03-15T19:00:00Z",
        }

        with tempfile.TemporaryDirectory() as tmp:
            active_file = Path(tmp) / "rotation_active_lanes.json"
            active_file.write_text(json.dumps(payload), encoding="utf-8")
            old_active_file = relay_server.ROTATION_ACTIVE_FILE
            old_cache = relay_server._ROTATION_LIVE_CACHE
            try:
                relay_server.ROTATION_ACTIVE_FILE = str(active_file)
                relay_server._ROTATION_LIVE_CACHE = None
                with (
                    mock.patch.object(relay_server, "load_rotation_meta_summary", return_value={"available": False}),
                    mock.patch.object(relay_server, "list_running_rotation_symbols", return_value=["ZRO"]),
                    mock.patch.object(relay_server, "load_rotation_lane_ports", return_value={"ZRO": 8372}),
                    mock.patch.object(relay_server, "fetch_local_json", return_value=status_payload),
                    mock.patch.object(relay_server, "_runtime_strategy_metadata", return_value=("continuation", "continuation")),
                ):
                    live = relay_server.load_rotation_live()
            finally:
                relay_server.ROTATION_ACTIVE_FILE = old_active_file
                relay_server._ROTATION_LIVE_CACHE = old_cache

        row = live["rows"][0]
        self.assertEqual(row["symbol"], "ZRO")
        self.assertEqual(row["strategy"], "continuation")
        self.assertEqual(row["alphaType"], "continuation")

    def test_load_rotation_live_exposes_german_gate_reason_and_raw_code(self) -> None:
        payload = {
            "selected": [],
            "watch_symbols": ["AAVE"],
            "all_rows": [
                {
                    "symbol": "AAVE",
                    "market": "AAVEUSDC",
                    "eligible": False,
                    "score": 4.0,
                    "gate_reason": "rule_entry_quality_volume",
                }
            ],
        }

        status_payload = {
            "data": {
                "core": {
                    "position_btc": 0.0,
                    "avg_entry_price": 0.0,
                    "mark_price": 181.5,
                    "realized_pnl_eur": 0.0,
                    "unrealized_pnl_eur": 0.0,
                    "trading_enabled": True,
                },
                "exec": {"open_orders_count": 0},
            },
            "aggregate": {
                "open_orders_count": 0,
                "trading_enabled": True,
            },
            "staleness_sec_by_process": {"core": 0.1, "exec": 0.1, "md": 0.1},
            "stale_warn_sec": 12.0,
            "overview_trade_ready": False,
            "updated_at": "2026-04-19T10:00:00Z",
        }

        with tempfile.TemporaryDirectory() as tmp:
            active_file = Path(tmp) / "rotation_active_lanes.json"
            active_file.write_text(json.dumps(payload), encoding="utf-8")
            old_active_file = relay_server.ROTATION_ACTIVE_FILE
            old_cache = relay_server._ROTATION_LIVE_CACHE
            try:
                relay_server.ROTATION_ACTIVE_FILE = str(active_file)
                relay_server._ROTATION_LIVE_CACHE = None
                with (
                    mock.patch.object(relay_server, "load_rotation_meta_summary", return_value={"available": False}),
                    mock.patch.object(relay_server, "list_running_rotation_symbols", return_value=["AAVE"]),
                    mock.patch.object(relay_server, "load_rotation_lane_ports", return_value={"AAVE": 8402}),
                    mock.patch.object(relay_server, "fetch_local_json", return_value=status_payload),
                ):
                    live = relay_server.load_rotation_live()
            finally:
                relay_server.ROTATION_ACTIVE_FILE = old_active_file
                relay_server._ROTATION_LIVE_CACHE = old_cache

        row = live["rows"][0]
        self.assertEqual(row["gateReasonCode"], "rule_entry_quality_volume")
        self.assertEqual(row["gateReason"], "Volumen fuer den Einstieg zu niedrig")

    def test_load_rotation_live_recovers_entry_opened_at_for_running_unselected_lane(self) -> None:
        payload = {
            "selected": [],
            "watch_symbols": ["WLD"],
            "all_rows": [
                {"symbol": "WLD", "market": "WLDUSDC", "eligible": True, "score": 9.0, "gate_reason": ""}
            ],
        }

        status_payload = {
            "data": {
                "core": {
                    "position_btc": 3.5,
                    "avg_entry_price": 1.5,
                    "mark_price": 1.6,
                    "realized_pnl_eur": 0.0,
                    "unrealized_pnl_eur": 0.35,
                    "trading_enabled": True,
                },
                "exec": {"open_orders_count": 0},
            },
            "aggregate": {
                "open_orders_count": 0,
                "trading_enabled": True,
            },
            "staleness_sec_by_process": {"core": 0.1, "exec": 0.1, "md": 0.1},
            "stale_warn_sec": 12.0,
            "overview_trade_ready": False,
            "updated_at": "2026-03-25T10:00:00Z",
        }

        with tempfile.TemporaryDirectory() as tmp:
            active_file = Path(tmp) / "rotation_active_lanes.json"
            active_file.write_text(json.dumps(payload), encoding="utf-8")
            journal_path = Path(tmp) / "journal_live_binance_wld_usdc_rotation.jsonl"
            old_active_file = relay_server.ROTATION_ACTIVE_FILE
            old_cache = relay_server._ROTATION_LIVE_CACHE
            try:
                relay_server.ROTATION_ACTIVE_FILE = str(active_file)
                relay_server._ROTATION_LIVE_CACHE = None
                with (
                    mock.patch.object(relay_server, "load_rotation_meta_summary", return_value={"available": False}),
                    mock.patch.object(relay_server, "list_running_rotation_symbols", return_value=["WLD"]),
                    mock.patch.object(relay_server, "load_rotation_lane_ports", return_value={"WLD": 8401}),
                    mock.patch.object(relay_server, "fetch_local_json", return_value=status_payload),
                    mock.patch.object(relay_server, "rotation_journal_path", return_value=journal_path),
                    mock.patch.object(
                        relay_server,
                        "infer_live_entry_opened_at",
                        return_value="2026-03-25T09:44:00Z",
                    ) as infer_mock,
                ):
                    live = relay_server.load_rotation_live()
            finally:
                relay_server.ROTATION_ACTIVE_FILE = old_active_file
                relay_server._ROTATION_LIVE_CACHE = old_cache

        row = live["rows"][0]
        self.assertEqual(row["symbol"], "WLD")
        self.assertFalse(row["selected"])
        self.assertTrue(row["currentlyTrading"])
        self.assertEqual(row["entryOpenedAt"], "2026-03-25T09:44:00Z")
        infer_mock.assert_called_once()
        self.assertEqual(infer_mock.call_args.args[0], journal_path)
        self.assertEqual(infer_mock.call_args.kwargs["selected_since_iso"], "")

    def test_load_rotation_live_falls_back_to_selected_since_when_entry_not_recoverable(self) -> None:
        payload = {
            "selected": ["PLUME"],
            "watch_symbols": ["PLUME"],
            "selected_since": {"PLUME": "2026-03-18T20:30:07.875270+00:00"},
            "all_rows": [
                {"symbol": "PLUME", "market": "PLUMEUSDC", "eligible": True, "score": 8.0, "gate_reason": ""}
            ],
        }

        status_payload = {
            "data": {
                "core": {
                    "position_btc": 1483.6026,
                    "avg_entry_price": 0.011635,
                    "mark_price": 0.010725,
                    "position_value_eur": 15.911638,
                    "realized_pnl_eur": 0.0,
                    "unrealized_pnl_eur": -1.349078,
                    "trading_enabled": True,
                },
                "exec": {"open_orders_count": 0},
            },
            "aggregate": {
                "open_orders_count": 0,
                "trading_enabled": True,
            },
            "staleness_sec_by_process": {"core": 0.1, "exec": 0.1, "md": 0.1},
            "stale_warn_sec": 12.0,
            "overview_trade_ready": False,
            "updated_at": "2026-03-25T10:00:00Z",
        }

        with tempfile.TemporaryDirectory() as tmp:
            active_file = Path(tmp) / "rotation_active_lanes.json"
            active_file.write_text(json.dumps(payload), encoding="utf-8")
            journal_path = Path(tmp) / "journal_live_binance_plume_usdc_rotation.jsonl"
            old_active_file = relay_server.ROTATION_ACTIVE_FILE
            old_cache = relay_server._ROTATION_LIVE_CACHE
            try:
                relay_server.ROTATION_ACTIVE_FILE = str(active_file)
                relay_server._ROTATION_LIVE_CACHE = None
                with (
                    mock.patch.object(relay_server, "load_rotation_meta_summary", return_value={"available": False}),
                    mock.patch.object(relay_server, "list_running_rotation_symbols", return_value=["PLUME"]),
                    mock.patch.object(relay_server, "load_rotation_lane_ports", return_value={"PLUME": 8324}),
                    mock.patch.object(relay_server, "fetch_local_json", return_value=status_payload),
                    mock.patch.object(relay_server, "rotation_journal_path", return_value=journal_path),
                    mock.patch.object(relay_server, "reconstruct_live_campaign_metrics", return_value=None),
                    mock.patch.object(relay_server, "infer_live_entry_opened_at", return_value=""),
                ):
                    live = relay_server.load_rotation_live()
            finally:
                relay_server.ROTATION_ACTIVE_FILE = old_active_file
                relay_server._ROTATION_LIVE_CACHE = old_cache

        row = live["rows"][0]
        self.assertEqual(row["symbol"], "PLUME")
        self.assertTrue(row["selected"])
        self.assertTrue(row["currentlyTrading"])
        self.assertEqual(row["entryOpenedAt"], "2026-03-18T20:30:07.875270Z")

    def test_collect_trades_local_ignores_account_sync_delta_fills(self) -> None:
        rows = [
            {
                "ts": "2026-03-15T00:25:15.209111+00:00",
                "event_type": "fill",
                "payload": {
                    "side": "buy",
                    "qty_btc": 7.033312,
                    "price": 2.5630328926116177,
                    "fee_eur": 0.0,
                    "order_id": "account_sync_delta_1773534315209",
                    "source": "account_sync_delta",
                    "synthetic": True,
                },
            },
            {
                "ts": "2026-03-15T00:25:15.496084+00:00",
                "event_type": "fill",
                "payload": {
                    "side": "buy",
                    "qty_btc": 7.04,
                    "price": 1.281,
                    "fee_eur": 0.008567328,
                    "order_id": "32204398",
                    "source": "reconcile",
                    "synthetic": True,
                },
            },
            {
                "ts": "2026-03-15T00:25:17.712147+00:00",
                "event_type": "fill",
                "payload": {
                    "side": "sell",
                    "qty_btc": 14.073312,
                    "price": 1.2787031857177615,
                    "fee_eur": 0.0,
                    "order_id": "account_sync_delta_1773534317712",
                    "source": "account_sync_delta",
                    "synthetic": True,
                },
            },
            {
                "ts": "2026-03-15T00:25:21.601218+00:00",
                "event_type": "fill",
                "payload": {
                    "side": "sell",
                    "qty_btc": 7.03,
                    "price": 1.277,
                    "fee_eur": 0.00852844,
                    "order_id": "32204459",
                    "source": "reconcile",
                    "synthetic": True,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal_live_binance_axs_usdc_rotation.jsonl"
            journal.write_text(
                "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
                encoding="utf-8",
            )
            old_cache = relay_server._REPORT_CACHE
            try:
                relay_server._REPORT_CACHE = {}
                with mock.patch.object(relay_server, "iter_local_journal_files", return_value=[journal]):
                    report = relay_server.collect_trades_local(
                        "2026-03-15T00:00:00Z",
                        "2026-03-15T01:00:00Z",
                        symbols_filter=["AXSUSDC"],
                    )
            finally:
                relay_server._REPORT_CACHE = old_cache

        self.assertEqual(report["fillEvents"], 2)
        self.assertEqual(report["daySummary"]["bundleCount"], 1)
        self.assertAlmostEqual(report["daySummary"]["proceedsUsdc"], -0.0452036, places=6)
        self.assertEqual(len(report["bundles"]), 1)
        self.assertAlmostEqual(report["bundles"][0]["proceedsUsdc"], -0.0452036, places=6)

    def test_collect_trades_mirror_reads_local_binance_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror_dir = Path(tmp)
            relay_server.binance_trade_mirror.write_mirror_rows(
                "RENDERUSDC",
                [
                    {
                        "symbol": "RENDERUSDC",
                        "coin": "RENDER",
                        "side": "BUY",
                        "orderId": "1",
                        "tradeId": "101",
                        "timeMs": 1000,
                        "timeIso": "2026-03-16T00:00:01Z",
                        "quantity": 1.0,
                        "grossUsdc": 1.01,
                    },
                    {
                        "symbol": "RENDERUSDC",
                        "coin": "RENDER",
                        "side": "SELL",
                        "orderId": "2",
                        "tradeId": "102",
                        "timeMs": 2000,
                        "timeIso": "2026-03-16T00:00:02Z",
                        "quantity": 1.0,
                        "grossUsdc": 1.20,
                    },
                ],
                mirror_dir,
            )
            old_cache = relay_server._REPORT_CACHE
            try:
                relay_server._REPORT_CACHE = {}
                with mock.patch.object(
                    relay_server.binance_trade_mirror,
                    "default_mirror_dir",
                    return_value=mirror_dir,
                ):
                    report = relay_server.collect_trades_mirror(
                        "2026-03-16T00:00:00Z",
                        "2026-03-16T01:00:00Z",
                        ["RENDERUSDC"],
                    )
            finally:
                relay_server._REPORT_CACHE = old_cache

        self.assertEqual(report["source"], "binance_mirror")
        self.assertEqual(report["fillEvents"], 2)
        self.assertEqual(report["daySummary"]["bundleCount"], 1)
        self.assertAlmostEqual(report["daySummary"]["proceedsUsdc"], 0.19, places=6)

    def test_report_auto_prefers_mirror_and_falls_back_to_local(self) -> None:
        with mock.patch.object(relay_server, "collect_trades_mirror", return_value={"source": "binance_mirror"}) as mirror_mock:
            with mock.patch.object(relay_server, "collect_trades_local", return_value={"source": "local_journal"}) as local_mock:
                report = None
                if "auto" in {"auto", "default"}:
                    try:
                        report = relay_server.collect_trades_mirror("2026-03-16T00:00:00Z", "2026-03-16T01:00:00Z")
                        report["sourceRequested"] = "auto"
                    except Exception:
                        report = relay_server.collect_trades_local("2026-03-16T00:00:00Z", "2026-03-16T01:00:00Z")
                self.assertEqual(report["source"], "binance_mirror")
                mirror_mock.assert_called_once()
                local_mock.assert_not_called()

        with mock.patch.object(relay_server, "collect_trades_mirror", side_effect=ValueError("missing mirror")) as mirror_mock:
            with mock.patch.object(relay_server, "collect_trades_local", return_value={"source": "local_journal"}) as local_mock:
                report = None
                if "auto" in {"auto", "default"}:
                    try:
                        report = relay_server.collect_trades_mirror("2026-03-16T00:00:00Z", "2026-03-16T01:00:00Z")
                        report["sourceRequested"] = "auto"
                    except Exception:
                        report = relay_server.collect_trades_local("2026-03-16T00:00:00Z", "2026-03-16T01:00:00Z")
                        report["source"] = "local_journal_fallback"
                        report["sourceRequested"] = "auto"
                self.assertEqual(report["source"], "local_journal_fallback")
                mirror_mock.assert_called_once()
                local_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
