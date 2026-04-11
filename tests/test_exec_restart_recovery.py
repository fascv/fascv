import asyncio
import json
import os
import threading
import tempfile
import time
import unittest
from datetime import datetime, timezone
from queue import Queue

import trading.processes.exec as execp
from trading.ipc.events import OrderIntent
from trading.kraken.ws_auth import OwnTradeUpdate
from trading.processes.context import ProcessContext


class _FakeRestSnapshot:
    def __init__(self, *args, **kwargs):
        self.deadman_calls = []

    def cancel_all_orders_after(self, timeout: int):
        self.deadman_calls.append(int(timeout))
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        return {
            "open": {
                "TX1": {"vol": "0.01", "vol_exec": "0.0", "status": "open"},
                "TX2": {"vol": "0.02", "vol_exec": "0.0", "status": "open"},
            }
        }

    def query_orders(self, txid: str):
        return {}

    def add_order(self, *args, **kwargs):
        return {"txid": []}

    def get_ws_token(self):
        return "token"


class _FakeWS:
    def __init__(self, *args, **kwargs):
        pass

    async def stream(self):
        if False:
            yield None


class _FakeRestFillGap:
    def __init__(self, *args, **kwargs):
        self.deadman_calls = []
        self.open_orders_calls = 0

    def cancel_all_orders_after(self, timeout: int):
        self.deadman_calls.append(int(timeout))
        return {"timeout": int(timeout)}

    def cancel_all(self):
        return {"count": 0}

    def open_orders(self):
        self.open_orders_calls += 1
        if self.open_orders_calls == 1:
            return {"open": {"TX1": {"vol": "0.01", "vol_exec": "0.0", "status": "open"}}}
        return {"open": {}}

    def query_orders(self, txid: str):
        return {
            "TX1": {
                "status": "closed",
                "vol": "0.01",
                "vol_exec": "0.01",
                "price": "100.0",
                "fee": "0.01",
                "descr": {"type": "buy"},
            }
        }

    def add_order(self, *args, **kwargs):
        return {"txid": []}

    def get_ws_token(self):
        return "token"


class _FakeOwnTradesWSFillSoon:
    def __init__(self, *args, **kwargs):
        pass

    async def stream(self):
        await asyncio.sleep(0.02)
        yield OwnTradeUpdate(
            ts=datetime.now(timezone.utc),
            trade_id="T-fast",
            order_id="TX1",
            side="buy",
            price=100.0,
            vol=0.01,
            fee=0.01,
            pair="XBT/EUR",
            event_id="E-fast",
        )


class TestExecRestartRecovery(unittest.TestCase):
    def test_open_orders_snapshot_recovers_state(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestSnapshot
        execp.OpenOrdersWS = _FakeWS
        execp.OwnTradesWS = _FakeWS
        try:
            with tempfile.TemporaryDirectory() as td:
                journal_path = os.path.join(td, "journal.jsonl")
                ctx = ProcessContext(
                    mode="live",
                    config={
                        "exec": {
                            "heartbeat_interval": 0.1,
                            "telemetry_interval": 0.1,
                            "reconcile_interval_sec": 60.0,
                            "deadman_tick_sec": 60.0,
                            "deadman_timeout_sec": 60,
                        },
                        "live": {"api_key": "k", "api_secret": "c2VjcmV0", "rest_url": "https://api.kraken.com"},
                        "md": {},
                        "cost": {},
                        "execution": {},
                        # Ensure exec startup doesn't scan a potentially large real journal file.
                        "journal": {"json_path": journal_path},
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
                open_orders_count = None
                snapshot_seen = False
                while time.time() < deadline:
                    while not ctx.q_telemetry.empty():
                        evt = ctx.q_telemetry.get_nowait()
                        if evt.process == "exec":
                            open_orders_count = evt.data.get("open_orders_count")
                    while not ctx.q_journal.empty():
                        evt = ctx.q_journal.get_nowait()
                        if evt.event_type == "exec_recovery_snapshot":
                            snapshot_seen = evt.payload.get("open_orders") == 2
                    if open_orders_count == 2 and snapshot_seen:
                        break
                    time.sleep(0.05)

                ctx.stop_event.set()
                t.join(timeout=1.0)

                self.assertEqual(open_orders_count, 2)
                self.assertTrue(snapshot_seen)
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_reconcile_emits_fill_when_owntrades_missing(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestFillGap
        execp.OpenOrdersWS = _FakeWS
        execp.OwnTradesWS = _FakeWS
        try:
            with tempfile.TemporaryDirectory() as td:
                journal_path = os.path.join(td, "journal.jsonl")
                ctx = ProcessContext(
                    mode="live",
                    config={
                        "exec": {
                            "heartbeat_interval": 0.1,
                            "telemetry_interval": 0.1,
                            "reconcile_interval_sec": 0.05,
                            "deadman_tick_sec": 60.0,
                            "deadman_timeout_sec": 60,
                            "fill_truth_grace_sec": 0.1,
                        },
                        "live": {"api_key": "k", "api_secret": "c2VjcmV0", "rest_url": "https://api.kraken.com"},
                        "md": {},
                        "cost": {},
                        "execution": {},
                        "journal": {"json_path": journal_path},
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

                deadline = time.time() + 10.0
                gap_seen = False
                reconcile_fill_seen = False
                while time.time() < deadline and not reconcile_fill_seen:
                    while not ctx.q_journal.empty():
                        evt = ctx.q_journal.get_nowait()
                        if (
                            evt.event_type == "fill"
                            and evt.payload.get("order_id") == "TX1"
                            and evt.payload.get("source") == "reconcile"
                        ):
                            reconcile_fill_seen = True
                            break
                        if evt.event_type == "exec_fill_truth_gap" and evt.payload.get("order_id") == "TX1":
                            gap_seen = True
                    time.sleep(0.05)

                ctx.stop_event.set()
                t.join(timeout=1.0)
                self.assertTrue(reconcile_fill_seen)
                self.assertFalse(gap_seen)
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_no_fill_truth_gap_when_owntrade_arrives_in_grace(self):
        original_rest = execp.KrakenRestClient
        original_oo = execp.OpenOrdersWS
        original_ot = execp.OwnTradesWS
        execp.KrakenRestClient = _FakeRestFillGap
        execp.OpenOrdersWS = _FakeWS
        execp.OwnTradesWS = _FakeOwnTradesWSFillSoon
        try:
            with tempfile.TemporaryDirectory() as td:
                journal_path = os.path.join(td, "journal.jsonl")
                ctx = ProcessContext(
                    mode="live",
                    config={
                        "exec": {
                            "heartbeat_interval": 0.1,
                            "telemetry_interval": 0.1,
                            "reconcile_interval_sec": 0.05,
                            "deadman_tick_sec": 60.0,
                            "deadman_timeout_sec": 60,
                            "fill_truth_grace_sec": 0.3,
                        },
                        "live": {"api_key": "k", "api_secret": "c2VjcmV0", "rest_url": "https://api.kraken.com"},
                        "md": {},
                        "cost": {},
                        "execution": {},
                        "journal": {"json_path": journal_path},
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
                gap_seen = False
                fill_seen = False
                while time.time() < deadline:
                    while not ctx.q_journal.empty():
                        evt = ctx.q_journal.get_nowait()
                        if evt.event_type == "fill" and evt.payload.get("order_id") == "TX1":
                            fill_seen = True
                        if evt.event_type == "exec_fill_truth_gap" and evt.payload.get("order_id") == "TX1":
                            gap_seen = True
                    if fill_seen and time.time() > (deadline - 1.0):
                        break
                    time.sleep(0.05)

                ctx.stop_event.set()
                t.join(timeout=1.0)
                self.assertTrue(fill_seen)
                self.assertFalse(gap_seen)
        finally:
            execp.KrakenRestClient = original_rest
            execp.OpenOrdersWS = original_oo
            execp.OwnTradesWS = original_ot

    def test_load_seen_client_ids_from_journal(self):
        fd, path = tempfile.mkstemp(prefix="exec_seen_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {"event_type": "exec_start", "payload": {"mode": "live"}},
                {"event_type": "exec_intent_seen", "payload": {"client_id": "cid-1"}},
                {"event_type": "exec_intent_seen", "payload": {"client_id": "cid-2"}},
                {"event_type": "exec_intent_seen", "payload": {"client_id": "cid-1"}},
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
            seen = execp._load_seen_client_ids_from_journal({"journal": {"json_path": path}}, max_events=100)
            self.assertEqual(seen, {"cid-1", "cid-2"})
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_load_entry_reference_from_journal_replays_open_position(self):
        fd, path = tempfile.mkstemp(prefix="exec_entry_ref_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-13T18:00:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-13T18:00:00+00:00",
                        "side": "buy",
                        "qty_btc": 0.01,
                        "price": 100.0,
                        "fee_eur": 0.01,
                    },
                },
                {
                    "ts": "2026-03-13T18:01:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-13T18:01:00+00:00",
                        "side": "buy",
                        "qty_btc": 0.02,
                        "price": 110.0,
                        "fee_eur": 0.02,
                    },
                },
                {
                    "ts": "2026-03-13T18:02:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-13T18:02:00+00:00",
                        "side": "sell",
                        "qty_btc": 0.015,
                        "price": 120.0,
                        "fee_eur": 0.03,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            out = execp._load_entry_reference_from_journal(
                {"journal": {"json_path": path}},
                expected_position_btc=0.015,
                position_tolerance_btc=1e-6,
                max_events=100,
            )
            self.assertIsNotNone(out)
            assert out is not None
            self.assertAlmostEqual(float(out["position_btc"]), 0.015, places=9)
            self.assertAlmostEqual(float(out["avg_entry_price"]), 107.6666666667, places=6)
            self.assertEqual(out["entry_ts"], "2026-03-13T18:00:00+00:00")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_load_entry_reference_from_journal_skips_mismatched_position(self):
        fd, path = tempfile.mkstemp(prefix="exec_entry_ref_mismatch_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {"event_type": "fill", "payload": {"side": "buy", "qty_btc": 0.01, "price": 100.0, "fee_eur": 0.0}},
                {"event_type": "fill", "payload": {"side": "buy", "qty_btc": 0.01, "price": 101.0, "fee_eur": 0.0}},
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            out = execp._load_entry_reference_from_journal(
                {"journal": {"json_path": path}},
                expected_position_btc=0.03,
                position_tolerance_btc=1e-6,
                max_events=100,
            )
            self.assertIsNone(out)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_load_entry_reference_from_journal_accepts_replayed_larger_position(self):
        fd, path = tempfile.mkstemp(prefix="exec_entry_ref_larger_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {"event_type": "fill", "payload": {"side": "buy", "qty_btc": 0.01, "price": 100.0, "fee_eur": 0.01}},
                {"event_type": "fill", "payload": {"side": "buy", "qty_btc": 0.02, "price": 110.0, "fee_eur": 0.02}},
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            out = execp._load_entry_reference_from_journal(
                {"journal": {"json_path": path}},
                expected_position_btc=0.02,
                position_tolerance_btc=1e-6,
                max_events=100,
            )
            self.assertIsNotNone(out)
            assert out is not None
            self.assertAlmostEqual(float(out["avg_entry_price"]), 107.6666666667, places=6)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_load_entry_reference_from_journal_ignores_stale_campaign_before_flat_sync(self):
        fd, path = tempfile.mkstemp(prefix="exec_entry_ref_cutoff_", suffix=".jsonl")
        os.close(fd)
        try:
            rows = [
                {
                    "ts": "2026-03-13T18:00:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-13T18:00:00+00:00",
                        "side": "buy",
                        "qty_btc": 0.02,
                        "price": 100.0,
                        "fee_eur": 0.0,
                    },
                },
                {
                    "ts": "2026-03-13T18:10:00+00:00",
                    "event_type": "core_account_synced",
                    "payload": {
                        "ts": "2026-03-13T18:10:00+00:00",
                        "position_btc": 0.0,
                        "reference_price": 0.0,
                    },
                },
                {
                    "ts": "2026-03-13T18:11:00+00:00",
                    "event_type": "fill",
                    "payload": {
                        "ts": "2026-03-13T18:11:00+00:00",
                        "side": "buy",
                        "qty_btc": 0.03,
                        "price": 200.0,
                        "fee_eur": 0.0,
                    },
                },
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            out = execp._load_entry_reference_from_journal(
                {"journal": {"json_path": path}},
                expected_position_btc=0.03,
                position_tolerance_btc=1e-6,
                max_events=100,
            )
            self.assertIsNotNone(out)
            assert out is not None
            self.assertAlmostEqual(float(out["position_btc"]), 0.03, places=9)
            self.assertAlmostEqual(float(out["avg_entry_price"]), 200.0, places=9)
            self.assertEqual(out["entry_ts"], "2026-03-13T18:11:00+00:00")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_restart_dedupes_client_id_from_journal(self):
        fd, path = tempfile.mkstemp(prefix="exec_restart_seen_", suffix=".jsonl")
        os.close(fd)
        try:
            base_cfg = {
                "exec": {"heartbeat_interval": 0.05, "telemetry_interval": 0.05},
                "md": {},
                "cost": {},
                "execution": {},
                "journal": {"json_path": path},
            }

            first = ProcessContext(
                mode="paper",
                config=base_cfg,
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
            t1 = threading.Thread(target=execp.run_exec, args=(first,), daemon=True)
            t1.start()
            first.q_order_intent.put(
                OrderIntent(
                    ts=datetime.now(timezone.utc),
                    side="buy",
                    qty_btc=0.001,
                    order_type="market",
                    client_id="cid-restart",
                )
            )

            deadline = time.time() + 2.0
            first_events = []
            while time.time() < deadline:
                while not first.q_journal.empty():
                    evt = first.q_journal.get_nowait()
                    first_events.append(evt)
                if any(e.event_type == "exec_intent_seen" for e in first_events):
                    break
                time.sleep(0.05)
            first.stop_event.set()
            t1.join(timeout=1.0)

            with open(path, "w", encoding="utf-8") as fh:
                for evt in first_events:
                    fh.write(
                        json.dumps(
                            {"ts": evt.ts.isoformat(), "event_type": evt.event_type, "payload": evt.payload},
                            default=str,
                        )
                        + "\n"
                    )

            second = ProcessContext(
                mode="paper",
                config=base_cfg,
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
            t2 = threading.Thread(target=execp.run_exec, args=(second,), daemon=True)
            t2.start()
            second.q_order_intent.put(
                OrderIntent(
                    ts=datetime.now(timezone.utc),
                    side="buy",
                    qty_btc=0.001,
                    order_type="market",
                    client_id="cid-restart",
                )
            )

            deadline = time.time() + 2.0
            dedup_seen = False
            while time.time() < deadline and not dedup_seen:
                while not second.q_journal.empty():
                    evt = second.q_journal.get_nowait()
                    if evt.event_type == "exec_intent_dedup_skip" and evt.payload.get("client_id") == "cid-restart":
                        dedup_seen = True
                        break
                time.sleep(0.05)
            second.stop_event.set()
            t2.join(timeout=1.0)

            self.assertTrue(dedup_seen)
        finally:
            if os.path.exists(path):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main()
