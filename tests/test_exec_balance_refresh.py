import asyncio
import threading
import time
import unittest
from datetime import datetime, timezone
from queue import Queue

import trading.processes.exec as execp
from trading.ipc.events import ControlCommand, OrderIntent
from trading.kraken.ws_auth import OwnTradeUpdate
from trading.processes.context import ProcessContext


class _FakeRestWithBalance:
    def __init__(self, *args, **kwargs):
        self.balance_calls = 0

    def cancel_all_orders_after(self, timeout: int):
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def add_order(self, *args, **kwargs):
        return {"txid": []}

    def get_ws_token(self):
        return "token"

    def balance(self):
        self.balance_calls += 1
        return {"ZEUR": "1.0", "XXBT": "0.0"}


class _FakeWSNoop:
    def __init__(self, *args, **kwargs):
        pass

    async def stream(self):
        if False:  # pragma: no cover
            yield None


class _FakeOwnTradesWSBurst:
    def __init__(self, *args, **kwargs):
        pass

    async def stream(self):
        # Emit multiple trades quickly to verify debounce.
        for i in range(3):
            yield OwnTradeUpdate(
                ts=datetime.now(timezone.utc),
                trade_id=f"T{i}",
                order_id="TX1",
                side="buy",
                price=100.0,
                vol=0.01,
                fee=0.01,
                pair="XBT/EUR",
                event_id=str(i),
            )
            await asyncio.sleep(0.01)


class _FakeRestNoBalance:
    def __init__(self, *args, **kwargs):
        pass

    def cancel_all_orders_after(self, timeout: int):
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def add_order(self, *args, **kwargs):
        return {"txid": []}

    def get_ws_token(self):
        return "token"


class _FakeRestWithSyncBalance:
    def __init__(self, *args, **kwargs):
        pass

    def cancel_all_orders_after(self, timeout: int):
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def add_order(self, *args, **kwargs):
        return {"txid": []}

    def get_ws_token(self):
        return "token"

    def balance(self):
        return {"ZEUR": "7.5", "XETH": "0.25"}


class _FakeBinanceRestStaleSellBalance:
    last_instance = None

    def __init__(self, *args, **kwargs):
        self.balance_calls = 0
        self.orders = []
        type(self).last_instance = self

    def cancel_all_orders_after(self, timeout: int):
        return {"timeout": int(timeout), "supported": False}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def min_notional(self, symbol: str):
        return 5.0

    def add_order(self, *args, **kwargs):
        self.orders.append(kwargs)
        return {"txid": ["OID1"]}

    def balance(self):
        self.balance_calls += 1
        if self.balance_calls <= 1:
            return {
                "USDC": {"free": 20.0, "locked": 0.0, "total": 20.0},
                "TEST": {"free": 0.2, "locked": 0.0, "total": 0.2},
            }
        return {
            "USDC": {"free": 20.0, "locked": 0.0, "total": 20.0},
            "TEST": {"free": 10.0, "locked": 0.0, "total": 10.0},
        }


class _FakeBinanceRestCanceledBuySettlement:
    last_instance = None

    def __init__(self, *args, **kwargs):
        self.balance_calls = 0
        self.orders = []
        self.cancel_all_calls = 0
        type(self).last_instance = self

    def cancel_all_orders_after(self, timeout: int):
        return {"timeout": int(timeout), "supported": False}

    def cancel_all(self):
        self.cancel_all_calls += 1
        return {"count": 1}

    def open_orders(self):
        return {"open": {}}

    def query_orders(self, txid: str):
        return {}

    def min_notional(self, symbol: str):
        return 5.0

    def add_order(self, *args, **kwargs):
        self.orders.append(kwargs)
        return {"txid": ["BUY1"]}

    def balance(self):
        self.balance_calls += 1
        if self.balance_calls <= 2:
            return {
                "USDC": {"free": 200.0, "locked": 0.0, "total": 200.0},
                "TEST": {"free": 0.0, "locked": 0.0, "total": 0.0},
            }
        return {
            "USDC": {"free": 190.0, "locked": 0.0, "total": 190.0},
            "TEST": {"free": 10.0, "locked": 0.0, "total": 10.0},
        }


class TestExecBalanceRefresh(unittest.TestCase):
    def test_apply_fill_to_sync_state_tracks_known_fill(self):
        cash, position, avg = execp._apply_fill_to_sync_state(
            cash_eur=100.0,
            position_btc=0.0,
            avg_entry_price=0.0,
            side="buy",
            qty_btc=2.0,
            price=10.0,
            fee_eur=1.0,
        )
        self.assertAlmostEqual(cash, 79.0, places=9)
        self.assertAlmostEqual(position, 2.0, places=9)
        self.assertAlmostEqual(avg, 10.0, places=9)

        cash, position, avg = execp._apply_fill_to_sync_state(
            cash_eur=cash,
            position_btc=position,
            avg_entry_price=avg,
            side="sell",
            qty_btc=2.0,
            price=11.0,
            fee_eur=0.5,
        )
        self.assertAlmostEqual(cash, 100.5, places=9)
        self.assertAlmostEqual(position, 0.0, places=9)
        self.assertAlmostEqual(avg, 0.0, places=9)

    def test_infer_account_sync_delta_fill_detects_unexpected_exit(self):
        delta = execp._infer_account_sync_delta_fill(
            previous_cash_eur=18.1240821,
            previous_position_btc=91.51298,
            previous_avg_entry_price=0.1321254,
            cash_eur=30.12661885,
            position_btc=0.0,
            avg_entry_price=0.0,
            position_tolerance_btc=0.1,
            cash_tolerance_eur=0.25,
        )
        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["side"], "sell")
        self.assertAlmostEqual(delta["qty_btc"], 91.51298, places=6)
        self.assertGreater(delta["price"], 0.13)
        self.assertEqual(delta["price_source"], "cash_delta")

    def test_infer_account_sync_delta_fill_ignores_dust_below_min_notional(self):
        delta = execp._infer_account_sync_delta_fill(
            previous_cash_eur=38.04401835870001,
            previous_position_btc=1465.0,
            previous_avg_entry_price=0.00000619,
            cash_eur=38.05308219,
            position_btc=0.0,
            avg_entry_price=0.0,
            position_tolerance_btc=14.65,
            cash_tolerance_eur=0.25,
            min_notional_eur=1.0,
        )
        self.assertIsNone(delta)

    def test_balance_refresh_requested_on_owntrades(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestWithBalance
        execp.OpenOrdersWS = _FakeWSNoop
        execp.OwnTradesWS = _FakeOwnTradesWSBurst
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {
                        "heartbeat_interval": 0.05,
                        "telemetry_interval": 0.05,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "rate_limit_per_sec": 100.0,
                        "balance_refresh_sec": 0.0,  # disable periodic
                        "balance_refresh_on_owntrades": True,
                        "balance_refresh_debounce_sec": 30.0,
                    },
                    "live": {"api_key": "k", "api_secret": "s", "rest_url": "https://api.kraken.com"},
                    "md": {},
                    "cost": {},
                    "execution": {},
                },
                stop_event=threading.Event(),
                q_market_core=Queue(),
                q_market_exec=Queue(),
                q_order_intent=Queue(),
                q_exec_report=Queue(),
                q_journal=Queue(),
                q_control_core=Queue(),
                q_control_exec=Queue(),
                q_telemetry=Queue(),
                q_heartbeat=Queue(),
            )
            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()

            deadline = time.time() + 3.0
            snap_seen = False
            while time.time() < deadline and not snap_seen:
                while not ctx.q_journal.empty():
                    evt = ctx.q_journal.get_nowait()
                    if evt.event_type == "exec_balance_snapshot" and evt.payload.get("source", "").startswith("event:"):
                        snap_seen = True
                        break
                time.sleep(0.05)

            ctx.stop_event.set()
            t.join(timeout=1.0)
            self.assertTrue(snap_seen)
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_balance_refresh_debounced(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestWithBalance
        execp.OpenOrdersWS = _FakeWSNoop
        execp.OwnTradesWS = _FakeOwnTradesWSBurst
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {
                        "heartbeat_interval": 0.05,
                        "telemetry_interval": 0.05,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "rate_limit_per_sec": 100.0,
                        "balance_refresh_sec": 0.0,
                        "balance_refresh_on_owntrades": True,
                        "balance_refresh_debounce_sec": 30.0,
                    },
                    "live": {"api_key": "k", "api_secret": "s", "rest_url": "https://api.kraken.com"},
                    "md": {},
                    "cost": {},
                    "execution": {},
                },
                stop_event=threading.Event(),
                q_market_core=Queue(),
                q_market_exec=Queue(),
                q_order_intent=Queue(),
                q_exec_report=Queue(),
                q_journal=Queue(),
                q_control_core=Queue(),
                q_control_exec=Queue(),
                q_telemetry=Queue(),
                q_heartbeat=Queue(),
            )
            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()

            # Let the burst arrive + refresh run.
            time.sleep(0.8)

            ctx.stop_event.set()
            t.join(timeout=1.0)

            event_snaps = 0
            while not ctx.q_journal.empty():
                evt = ctx.q_journal.get_nowait()
                if evt.event_type == "exec_balance_snapshot" and str(evt.payload.get("source", "")).startswith("event:"):
                    event_snaps += 1

            # Multiple ownTrades in a burst should lead to at most 1 balance refresh within debounce window.
            self.assertEqual(event_snaps, 1)
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_no_balance_method_is_ok(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestNoBalance
        execp.OpenOrdersWS = _FakeWSNoop
        execp.OwnTradesWS = _FakeOwnTradesWSBurst
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {
                        "heartbeat_interval": 0.05,
                        "telemetry_interval": 0.05,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "rate_limit_per_sec": 100.0,
                        "balance_refresh_sec": 0.0,
                        "balance_refresh_on_owntrades": True,
                    },
                    "live": {"api_key": "k", "api_secret": "s", "rest_url": "https://api.kraken.com"},
                    "md": {},
                    "cost": {},
                    "execution": {},
                },
                stop_event=threading.Event(),
                q_market_core=Queue(),
                q_market_exec=Queue(),
                q_order_intent=Queue(),
                q_exec_report=Queue(),
                q_journal=Queue(),
                q_control_core=Queue(),
                q_control_exec=Queue(),
                q_telemetry=Queue(),
                q_heartbeat=Queue(),
            )
            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()
            time.sleep(0.2)
            ctx.stop_event.set()
            t.join(timeout=1.0)

            # Ensure no balance snapshots were emitted (since adapter lacks method).
            while not ctx.q_journal.empty():
                evt = ctx.q_journal.get_nowait()
                self.assertNotEqual(evt.event_type, "exec_balance_snapshot")
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_startup_balance_syncs_core_account(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestWithSyncBalance
        execp.OpenOrdersWS = _FakeWSNoop
        execp.OwnTradesWS = _FakeWSNoop
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {
                        "heartbeat_interval": 0.05,
                        "telemetry_interval": 0.05,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "rate_limit_per_sec": 100.0,
                        "balance_refresh_sec": 0.0,
                        "sync_core_account_on_start": True,
                        "pair": "ETH/EUR",
                    },
                    "live": {"api_key": "k", "api_secret": "s", "rest_url": "https://api.kraken.com"},
                    "md": {},
                    "cost": {},
                    "execution": {},
                },
                stop_event=threading.Event(),
                q_market_core=Queue(),
                q_market_exec=Queue(),
                q_order_intent=Queue(),
                q_exec_report=Queue(),
                q_journal=Queue(),
                q_control_core=Queue(),
                q_control_exec=Queue(),
                q_telemetry=Queue(),
                q_heartbeat=Queue(),
            )
            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()

            deadline = time.time() + 3.0
            sync_cmd = None
            while time.time() < deadline and sync_cmd is None:
                while not ctx.q_control_core.empty():
                    cmd = ctx.q_control_core.get_nowait()
                    if cmd.action == "SYNC_ACCOUNT":
                        sync_cmd = cmd
                        break
                time.sleep(0.05)

            ctx.stop_event.set()
            t.join(timeout=1.0)

            self.assertIsNotNone(sync_cmd)
            assert sync_cmd is not None
            self.assertAlmostEqual(float(sync_cmd.payload.get("cash_eur", 0.0)), 7.5, places=9)
            self.assertAlmostEqual(float(sync_cmd.payload.get("position_btc", 0.0)), 0.25, places=9)
            self.assertEqual(sync_cmd.payload.get("base_asset"), "ETH")
            self.assertEqual(sync_cmd.payload.get("quote_asset"), "EUR")
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_sell_retries_after_stale_balance_refresh(self):
        original_binance = execp.BinanceRestClient
        execp.BinanceRestClient = _FakeBinanceRestStaleSellBalance
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {
                        "exchange": "binance",
                        "heartbeat_interval": 0.05,
                        "telemetry_interval": 0.05,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "rate_limit_per_sec": 100.0,
                        "balance_refresh_sec": 0.0,
                        "private_ws_enabled": False,
                        "pair": "TEST/USDC",
                    },
                    "live": {
                        "exchange": "binance",
                        "api_key": "k",
                        "api_secret": "s",
                        "base_url": "https://api.binance.com",
                        "symbol": "TEST/USDC",
                    },
                    "md": {"pair": "TEST/USDC"},
                    "cost": {},
                    "order": {"min_trade_btc": 1.0},
                    "execution": {},
                },
                stop_event=threading.Event(),
                q_market_core=Queue(),
                q_market_exec=Queue(),
                q_order_intent=Queue(),
                q_exec_report=Queue(),
                q_journal=Queue(),
                q_control_core=Queue(),
                q_control_exec=Queue(),
                q_telemetry=Queue(),
                q_heartbeat=Queue(),
            )
            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()

            ctx.q_order_intent.put(
                OrderIntent(
                    ts=datetime.now(timezone.utc),
                    side="sell",
                    qty_btc=10.0,
                    order_type="market",
                    limit_price=None,
                    post_only=False,
                    client_id="sell_retry",
                    meta={"reference_price": 1.0, "emergency_exit": True},
                )
            )

            deadline = time.time() + 3.0
            retry_seen = False
            skipped_seen = False
            open_seen = False
            while time.time() < deadline and not open_seen:
                while not ctx.q_journal.empty():
                    evt = ctx.q_journal.get_nowait()
                    if evt.event_type == "exec_sell_balance_refresh_retry":
                        retry_seen = True
                    if evt.event_type == "exec_sell_skipped_below_min_notional":
                        skipped_seen = True
                while not ctx.q_exec_report.empty():
                    evt = ctx.q_exec_report.get_nowait()
                    if getattr(evt, "order_id", None) == "OID1" and getattr(evt, "status", None) == "OPEN":
                        open_seen = True
                        break
                time.sleep(0.05)

            ctx.stop_event.set()
            t.join(timeout=1.0)

            self.assertTrue(retry_seen)
            self.assertFalse(skipped_seen)
            self.assertTrue(open_seen)
            self.assertIsNotNone(_FakeBinanceRestStaleSellBalance.last_instance)
            assert _FakeBinanceRestStaleSellBalance.last_instance is not None
            self.assertEqual(len(_FakeBinanceRestStaleSellBalance.last_instance.orders), 1)
            self.assertGreaterEqual(_FakeBinanceRestStaleSellBalance.last_instance.balance_calls, 2)
        finally:
            execp.BinanceRestClient = original_binance

    def test_buy_cancel_requires_settlement_balance_check_before_reentry(self):
        original_binance = execp.BinanceRestClient
        execp.BinanceRestClient = _FakeBinanceRestCanceledBuySettlement
        try:
            ctx = ProcessContext(
                mode="live",
                config={
                    "exec": {
                        "exchange": "binance",
                        "heartbeat_interval": 0.05,
                        "telemetry_interval": 0.05,
                        "reconcile_interval_sec": 60.0,
                        "deadman_tick_sec": 60.0,
                        "deadman_timeout_sec": 60,
                        "rate_limit_per_sec": 100.0,
                        "balance_refresh_sec": 0.0,
                        "private_ws_enabled": False,
                        "pair": "TEST/USDC",
                    },
                    "live": {
                        "exchange": "binance",
                        "api_key": "k",
                        "api_secret": "s",
                        "base_url": "https://api.binance.com",
                        "symbol": "TEST/USDC",
                    },
                    "md": {"pair": "TEST/USDC"},
                    "cost": {},
                    "order": {"min_trade_btc": 1.0},
                    "execution": {},
                },
                stop_event=threading.Event(),
                q_market_core=Queue(),
                q_market_exec=Queue(),
                q_order_intent=Queue(),
                q_exec_report=Queue(),
                q_journal=Queue(),
                q_control_core=Queue(),
                q_control_exec=Queue(),
                q_telemetry=Queue(),
                q_heartbeat=Queue(),
            )
            t = threading.Thread(target=execp.run_exec, args=(ctx,), daemon=True)
            t.start()

            ctx.q_order_intent.put(
                OrderIntent(
                    ts=datetime.now(timezone.utc),
                    side="buy",
                    qty_btc=10.0,
                    order_type="market",
                    limit_price=None,
                    post_only=False,
                    client_id="buy_first",
                    meta={"reference_price": 1.0},
                )
            )

            deadline = time.time() + 3.0
            open_seen = False
            while time.time() < deadline and not open_seen:
                while not ctx.q_exec_report.empty():
                    evt = ctx.q_exec_report.get_nowait()
                    if getattr(evt, "order_id", None) == "BUY1" and getattr(evt, "status", None) == "OPEN":
                        open_seen = True
                        break
                time.sleep(0.05)

            self.assertTrue(open_seen)

            ctx.q_control_exec.put(
                ControlCommand(ts=datetime.now(timezone.utc), action="CANCEL_ALL", reason="test_cancel")
            )
            time.sleep(0.2)

            ctx.q_order_intent.put(
                OrderIntent(
                    ts=datetime.now(timezone.utc),
                    side="buy",
                    qty_btc=10.0,
                    order_type="market",
                    limit_price=None,
                    post_only=False,
                    client_id="buy_second",
                    meta={"reference_price": 1.0},
                )
            )

            deadline = time.time() + 3.0
            settlement_check_seen = False
            skipped_seen = False
            while time.time() < deadline and not skipped_seen:
                while not ctx.q_journal.empty():
                    evt = ctx.q_journal.get_nowait()
                    if evt.event_type == "exec_buy_settlement_check_requested":
                        settlement_check_seen = True
                    if evt.event_type == "exec_buy_skipped_existing_balance":
                        skipped_seen = True
                        break
                time.sleep(0.05)

            ctx.stop_event.set()
            t.join(timeout=1.0)

            self.assertTrue(settlement_check_seen)
            self.assertTrue(skipped_seen)
            self.assertIsNotNone(_FakeBinanceRestCanceledBuySettlement.last_instance)
            assert _FakeBinanceRestCanceledBuySettlement.last_instance is not None
            self.assertEqual(len(_FakeBinanceRestCanceledBuySettlement.last_instance.orders), 1)
            self.assertGreaterEqual(_FakeBinanceRestCanceledBuySettlement.last_instance.balance_calls, 2)
        finally:
            execp.BinanceRestClient = original_binance


if __name__ == "__main__":
    unittest.main()
