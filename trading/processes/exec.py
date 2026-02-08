from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from datetime import datetime, timezone
from queue import Empty
from typing import Any, Dict, List

from trading.execution.backtest import BacktestExecutionConfig, BacktestSimulator
from trading.execution.state_machine import OrderStateMachine
from trading.ipc.events import ControlCommand, ExecutionReport, Heartbeat, JournalEvent, OrderIntent, TelemetryEvent
from trading.ipc.queues import try_put, queue_depth
from trading.kraken.rest import KrakenAPIError, KrakenRestClient
from trading.kraken.ws_auth import OpenOrdersWS, OwnTradesWS, OwnTradeUpdate
from trading.processes.context import ProcessContext
from trading.types import Fill, MarketEvent, Order
from trading.utils.kraken import map_pair


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.time()

    def acquire(self) -> float:
        now = time.time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens < 1.0:
            sleep_for = (1.0 - self.tokens) / self.rate
            time.sleep(sleep_for)
            now = time.time()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.tokens -= 1.0
            self.last = now
            return sleep_for
        self.tokens -= 1.0
        self.last = now
        return 0.0


class DeadmanStub:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def cancel_all_orders_after(self, timeout: int) -> dict[str, int]:
        self.calls.append(int(timeout))
        return {"timeout": int(timeout)}

    def cancel_all(self) -> dict[str, int]:
        return {"count": 0}


class OwnTradesDeduper:
    def __init__(self, maxlen: int = 50000) -> None:
        self.maxlen = maxlen
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._lock = threading.Lock()

    def first_seen(self, trade_id: str) -> bool:
        if not trade_id:
            return True
        with self._lock:
            if trade_id in self._seen:
                return False
            self._seen.add(trade_id)
            self._order.append(trade_id)
            while len(self._order) > self.maxlen:
                old = self._order.popleft()
                self._seen.discard(old)
            return True


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
    try_put(ctx.q_journal, evt)


def _send_telemetry(ctx: ProcessContext, data: Dict[str, Any]) -> None:
    evt = TelemetryEvent(ts=datetime.now(timezone.utc), process="exec", data=data)
    try_put(ctx.q_telemetry, evt)


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="exec", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _map_status(status: str, vol: float | None, vol_exec: float | None) -> str:
    status = status.lower()
    if status in {"open", "pending"}:
        return "OPEN"
    if status in {"canceled", "expired"}:
        return "CANCELED"
    if status in {"closed"}:
        if vol is not None and vol_exec is not None and vol_exec < vol:
            return "PARTIAL"
        return "FILLED"
    return "OPEN"


def _handle_own_trade(
    ctx: ProcessContext,
    update: OwnTradeUpdate,
    order_sm: OrderStateMachine,
    sm_lock: threading.Lock,
    deduper: OwnTradesDeduper,
) -> None:
    if not deduper.first_seen(update.trade_id):
        return
    fill = Fill(
        ts=update.ts,
        side=update.side or "",
        qty_btc=update.vol,
        price=update.price,
        fee_eur=update.fee,
        order_id=update.order_id,
        slippage_bps=0.0,
    )
    with sm_lock:
        order_sm.transition(update.order_id, "PARTIAL", update.ts)
    try_put(ctx.q_exec_report, fill)
    _send_journal(
        ctx,
        "fill",
        {
            "ts": fill.ts.isoformat(),
            "order_id": fill.order_id,
            "side": fill.side,
            "qty_btc": fill.qty_btc,
            "price": fill.price,
            "fee_eur": fill.fee_eur,
            "slippage_bps": fill.slippage_bps,
            "trade_id": update.trade_id,
        },
    )


def run_exec(ctx: ProcessContext) -> None:
    cfg = ctx.config
    mode = ctx.mode
    accept_new = True

    rate_per_sec = float(_cfg(cfg, "exec.rate_limit_per_sec", _cfg(cfg, "live.rate_limit_per_sec", 1.0)))
    bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=max(1.0, rate_per_sec))
    bucket_lock = threading.Lock()

    simulator = None
    adapter = None
    deadman_adapter = None
    if mode == "paper":
        simulator = BacktestSimulator(
            BacktestExecutionConfig(
                latency_bars=int(_cfg(cfg, "execution.latency_bars", 0)),
                partial_fill_ratio=float(_cfg(cfg, "execution.partial_fill_ratio", 1.0)),
                slippage_bps=float(_cfg(cfg, "execution.slippage_bps", 1.0)),
            ),
            maker_fee_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
            taker_fee_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
        )
        if bool(_cfg(cfg, "exec.deadman_in_paper", False)):
            deadman_adapter = DeadmanStub()
    else:
        adapter = KrakenRestClient(
            api_key=_cfg(cfg, "live.api_key", ""),
            api_secret=_cfg(cfg, "live.api_secret", ""),
            base_url=_cfg(cfg, "live.rest_url", "https://api.kraken.com"),
        )
        deadman_adapter = adapter

    order_sm = OrderStateMachine()
    sm_lock = threading.Lock()
    own_trades_deduper = OwnTradesDeduper(maxlen=int(_cfg(cfg, "exec.owntrades_dedupe_size", 50000)))
    seen_client_ids: set[str] = set()
    order_recv_ts: Dict[str, float] = {}
    last_latency_ms = 0.0
    pair = map_pair(_cfg(cfg, "exec.pair", _cfg(cfg, "md.pair", _cfg(cfg, "live.kraken_pair", "XBT/EUR"))))
    known_txids: set[str] = set()
    canary = bool(_cfg(cfg, "exec.canary_mode", False))
    rate_limit_hits = 0
    last_rate_signal = 0.0
    deadman_timeout = int(_cfg(cfg, "exec.deadman_timeout_sec", 60))
    deadman_tick = float(_cfg(cfg, "exec.deadman_tick_sec", 20.0))
    deadman_state = {"enabled": False}
    deadman_lock = threading.Lock()
    rate_limit_pause_sec = float(_cfg(cfg, "exec.rate_limit_pause_sec", 2.0))
    resume_after: float | None = None
    deadman_budget_wait_ms = 0.0
    deadman_rate_limit_hits = 0

    hb_seq = 0
    last_heartbeat = 0.0
    last_telemetry = 0.0
    heartbeat_interval = float(_cfg(cfg, "exec.heartbeat_interval", 1.0))
    telemetry_interval = float(_cfg(cfg, "exec.telemetry_interval", 1.0))

    _send_journal(ctx, "exec_start", {"mode": mode})

    def _acquire_budget() -> float:
        with bucket_lock:
            return bucket.acquire()

    if deadman_adapter is not None:
        if canary:
            try:
                _acquire_budget()
                resp = deadman_adapter.cancel_all_orders_after(0)
                _send_journal(ctx, "deadman", {"timeout": 0, "response": resp})
            except Exception as exc:
                _send_journal(ctx, "deadman_error", {"error": str(exc)})
        def _deadman_worker() -> None:
            nonlocal deadman_budget_wait_ms, deadman_rate_limit_hits
            last_enabled = False
            last_tick = 0.0
            while not ctx.stop_event.is_set():
                with deadman_lock:
                    enabled = deadman_state["enabled"]
                now = time.time()
                if enabled:
                    if now - last_tick >= deadman_tick:
                        try:
                            wait_s = _acquire_budget()
                            if wait_s > 0:
                                deadman_budget_wait_ms += wait_s * 1000.0
                                deadman_rate_limit_hits += 1
                            resp = deadman_adapter.cancel_all_orders_after(deadman_timeout)
                            _send_journal(ctx, "deadman", {"timeout": deadman_timeout, "response": resp})
                        except Exception as exc:
                            _send_journal(ctx, "deadman_error", {"error": str(exc)})
                        last_tick = now
                    else:
                        time.sleep(min(0.1, max(deadman_tick / 4.0, 0.01)))
                else:
                    if last_enabled:
                        try:
                            _acquire_budget()
                            resp = deadman_adapter.cancel_all_orders_after(0)
                            _send_journal(ctx, "deadman", {"timeout": 0, "response": resp})
                        except Exception as exc:
                            _send_journal(ctx, "deadman_error", {"error": str(exc)})
                        last_tick = now
                    time.sleep(0.1)
                last_enabled = enabled

        dm = threading.Thread(target=_deadman_worker, daemon=True)
        dm.start()

    if adapter is not None:
        try:
            _acquire_budget()
            open_orders = adapter.open_orders()
            for txid in open_orders.get("open", {}).keys():
                with sm_lock:
                    order_sm.transition(txid, "OPEN", datetime.now(timezone.utc))
        except Exception as exc:
            _send_journal(ctx, "exec_error", {"error": str(exc)})

        def _open_orders_worker() -> None:
            try:
                ws_url = _cfg(cfg, "exec.ws_auth_url", _cfg(cfg, "live.ws_auth_url", "wss://ws-auth.kraken.com"))
                ws = OpenOrdersWS(adapter, url=ws_url)

                async def _run() -> None:
                    async for update in ws.stream():
                        status = update.status.lower()
                        mapped = "OPEN"
                        if status in {"closed"}:
                            if update.vol is not None and update.vol_exec is not None and update.vol_exec < update.vol:
                                mapped = "PARTIAL"
                            else:
                                mapped = "FILLED"
                        elif status in {"canceled", "expired"}:
                            mapped = "CANCELED"
                        with sm_lock:
                            order_sm.transition(update.order_id, mapped, update.ts)
                        if update.order_id:
                            known_txids.add(update.order_id)
                        report = ExecutionReport(
                            ts=update.ts,
                            order_id=update.order_id,
                            status=mapped,
                            filled_qty_btc=float(update.vol_exec or 0.0),
                            avg_price=0.0,
                            fee_eur=0.0,
                            latency_ms=0.0,
                        )
                        try_put(ctx.q_exec_report, report)
                        _send_journal(ctx, "exec_report", report.__dict__)

                asyncio.run(_run())
            except Exception as exc:
                _send_journal(ctx, "exec_ws_error", {"error": str(exc)})

        t = threading.Thread(target=_open_orders_worker, daemon=True)
        t.start()

        def _own_trades_worker() -> None:
            try:
                ws_url = _cfg(cfg, "exec.ws_auth_url", _cfg(cfg, "live.ws_auth_url", "wss://ws-auth.kraken.com"))
                ws = OwnTradesWS(adapter, url=ws_url)

                async def _run() -> None:
                    async for update in ws.stream():
                        _handle_own_trade(ctx, update, order_sm, sm_lock, own_trades_deduper)

                asyncio.run(_run())
            except Exception as exc:
                _send_journal(ctx, "exec_owntrades_error", {"error": str(exc)})

        ot = threading.Thread(target=_own_trades_worker, daemon=True)
        ot.start()

        def _reconcile_worker() -> None:
            interval = float(_cfg(cfg, "exec.reconcile_interval_sec", 30.0))
            while not ctx.stop_event.is_set():
                try:
                    _acquire_budget()
                    open_orders = adapter.open_orders()
                    open_txids = set(open_orders.get("open", {}).keys())
                    for txid in open_txids:
                        known_txids.add(txid)
                        with sm_lock:
                            order_sm.transition(txid, "OPEN", datetime.now(timezone.utc))

                    closed = list(known_txids - open_txids)
                    if closed:
                        _acquire_budget()
                        result = adapter.query_orders(",".join(closed))
                        for txid, payload in result.items():
                            status = str(payload.get("status", "")).lower()
                            vol = _safe_float(payload.get("vol"))
                            vol_exec = _safe_float(payload.get("vol_exec"))
                            mapped = _map_status(status, vol, vol_exec)
                            with sm_lock:
                                order_sm.transition(txid, mapped, datetime.now(timezone.utc))
                            report = ExecutionReport(
                                ts=datetime.now(timezone.utc),
                                order_id=txid,
                                status=mapped,
                                filled_qty_btc=float(vol_exec or 0.0),
                                avg_price=0.0,
                                fee_eur=0.0,
                                latency_ms=0.0,
                            )
                            try_put(ctx.q_exec_report, report)
                            _send_journal(ctx, "exec_report", report.__dict__)
                except Exception as exc:
                    _send_journal(ctx, "exec_reconcile_error", {"error": str(exc)})
                time.sleep(interval)

        r = threading.Thread(target=_reconcile_worker, daemon=True)
        r.start()

    if deadman_adapter is not None:
        with deadman_lock:
            deadman_state["enabled"] = accept_new and not canary

    while not ctx.stop_event.is_set():
        # control commands
        try:
            cmd: ControlCommand = ctx.q_control_exec.get_nowait()
            if cmd.action in {"STOP", "PAUSE"}:
                accept_new = False
                if deadman_adapter is not None:
                    with deadman_lock:
                        deadman_state["enabled"] = False
            elif cmd.action in {"START", "RESUME"}:
                accept_new = True
                if deadman_adapter is not None:
                    with deadman_lock:
                        deadman_state["enabled"] = accept_new and not canary
            elif cmd.action == "CANCEL_ALL":
                canceled = order_sm.cancel_all(datetime.now(timezone.utc))
                if simulator is not None:
                    simulator.cancel_all()
                if adapter is not None:
                    try:
                        _acquire_budget()
                        adapter.cancel_all()
                    except Exception as exc:
                        _send_journal(ctx, "exec_error", {"error": str(exc)})
                for order_id in canceled:
                    report = ExecutionReport(
                        ts=datetime.now(timezone.utc),
                        order_id=order_id,
                        status="CANCELED",
                        filled_qty_btc=0.0,
                        avg_price=0.0,
                        fee_eur=0.0,
                        latency_ms=0.0,
                        reason=cmd.reason,
                    )
                    try_put(ctx.q_exec_report, report)
                    _send_journal(ctx, "exec_report", report.__dict__)
        except Empty:
            pass

        if resume_after is not None and time.time() >= resume_after:
            accept_new = True
            resume_after = None
            if deadman_adapter is not None:
                with deadman_lock:
                    deadman_state["enabled"] = accept_new and not canary

        # order intents
        try:
            intent: OrderIntent = ctx.q_order_intent.get_nowait()
            if intent.client_id and intent.client_id in seen_client_ids:
                continue
            if not accept_new:
                continue
            if intent.client_id:
                seen_client_ids.add(intent.client_id)
            order_id = intent.client_id or f"order_{int(time.time()*1000)}"
            order_recv_ts[order_id] = time.time()

            sleep_for = _acquire_budget()
            if sleep_for > 0 and (time.time() - last_rate_signal) > 1.0:
                last_rate_signal = time.time()
                try_put(ctx.q_control_core, ControlCommand(ts=datetime.now(timezone.utc), action="PAUSE", reason="rate_limit"))
                accept_new = False
                resume_after = time.time() + rate_limit_pause_sec
                if deadman_adapter is not None:
                    with deadman_lock:
                        deadman_state["enabled"] = False

            order = Order(
                ts=intent.ts,
                side=intent.side,
                qty_btc=intent.qty_btc,
                order_type=intent.order_type,
                price=intent.limit_price,
                post_only=intent.post_only,
                id=order_id,
            )

            with sm_lock:
                order_sm.transition(order_id, "NEW", datetime.now(timezone.utc))
                order_sm.transition(order_id, "ACK", datetime.now(timezone.utc))
                order_sm.transition(order_id, "OPEN", datetime.now(timezone.utc))

            if simulator is not None:
                simulator.submit([order])
            elif adapter is not None:
                try:
                    result = adapter.add_order(
                        pair=pair,
                        side=order.side,
                        order_type=order.order_type,
                        volume=str(order.qty_btc),
                        price=str(order.price) if order.price is not None else None,
                        cl_ord_id=order.id,
                        post_only=order.post_only,
                        validate=canary,
                    )
                    if not canary:
                        txids = result.get("txid", [])
                        if txids:
                            with sm_lock:
                                order_sm.transition(txids[0], "OPEN", datetime.now(timezone.utc))
                except KrakenAPIError as exc:
                    if exc.is_rate_limit():
                        rate_limit_hits += 1
                        try_put(ctx.q_control_core, ControlCommand(ts=datetime.now(timezone.utc), action="PAUSE", reason="rate_limit"))
                        accept_new = False
                        resume_after = time.time() + rate_limit_pause_sec
                        if deadman_adapter is not None:
                            with deadman_lock:
                                deadman_state["enabled"] = False
                        time.sleep(1.0)
                        try_put(ctx.q_order_intent, intent)
                    else:
                        report = ExecutionReport(
                            ts=datetime.now(timezone.utc),
                            order_id=order.id or "",
                            status="REJECTED",
                            filled_qty_btc=0.0,
                            avg_price=0.0,
                            fee_eur=0.0,
                            latency_ms=0.0,
                            reason=str(exc),
                        )
                        try_put(ctx.q_exec_report, report)
                        _send_journal(ctx, "exec_report", report.__dict__)

            latency_ms = (time.time() - order_recv_ts[order_id]) * 1000.0
            last_latency_ms = latency_ms
            status = "VALIDATED" if canary else "OPEN"
            report = ExecutionReport(
                ts=datetime.now(timezone.utc),
                order_id=order_id,
                status=status,
                filled_qty_btc=0.0,
                avg_price=0.0,
                fee_eur=0.0,
                latency_ms=latency_ms,
            )
            try_put(ctx.q_exec_report, report)
            _send_journal(ctx, "exec_report", report.__dict__)
        except Empty:
            pass

        # market events for paper fills
        if simulator is not None:
            try:
                event: MarketEvent = ctx.q_market_exec.get(timeout=0.1)
                fills: List[Fill] = simulator.process(event, spread_bps=event.micro.get("spread_bps", 0.0))
                for fill in fills:
                    status = "FILLED" if simulator.config.partial_fill_ratio >= 1.0 else "PARTIAL"
                    with sm_lock:
                        order_sm.transition(fill.order_id or "", status, datetime.now(timezone.utc))
                    report = ExecutionReport(
                        ts=fill.ts,
                        order_id=fill.order_id or "",
                        status=status,
                        filled_qty_btc=fill.qty_btc,
                        avg_price=fill.price,
                        fee_eur=fill.fee_eur,
                        latency_ms=0.0,
                    )
                    try_put(ctx.q_exec_report, report)
                    try_put(ctx.q_exec_report, fill)
                    _send_journal(ctx, "exec_report", report.__dict__)
                    _send_journal(ctx, "fill", {
                        "ts": fill.ts.isoformat(),
                        "order_id": fill.order_id,
                        "side": fill.side,
                        "qty_btc": fill.qty_btc,
                        "price": fill.price,
                        "fee_eur": fill.fee_eur,
                        "slippage_bps": fill.slippage_bps,
                    })
            except Empty:
                pass

        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            last_heartbeat = now
        if now - last_telemetry >= telemetry_interval:
            last_telemetry = now
            with sm_lock:
                open_count = order_sm.open_orders_count()
            _send_telemetry(
                ctx,
                {
                    "mode": ctx.mode,
                    "open_orders_count": open_count,
                    "queue_order_intent": queue_depth(ctx.q_order_intent),
                    "queue_exec_report": queue_depth(ctx.q_exec_report),
                    "order_latency_ms": last_latency_ms,
                    "rate_limit_hits": rate_limit_hits,
                    "canary_mode": canary,
                    "deadman_rate_limit_hits": deadman_rate_limit_hits,
                    "deadman_budget_wait_ms": deadman_budget_wait_ms,
                },
            )

    _send_journal(ctx, "exec_stop", {})
