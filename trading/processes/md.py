from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from trading.data.backtest import BacktestCSVDataSource
from trading.binance.ws_public import BinanceWSMarketData, StaleDataError as BinanceStaleDataError
from trading.ipc.events import ControlCommand, Heartbeat, JournalEvent, TelemetryEvent
from trading.ipc.queues import put_latest, queue_depth, try_get, try_put
from trading.kraken.ws_public import KrakenWSV2MarketData, StaleDataError as KrakenStaleDataError
from trading.processes.context import ProcessContext


class StaleDataError(Exception):
    pass


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="md", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def _send_telemetry(ctx: ProcessContext, data: Dict[str, Any]) -> None:
    evt = TelemetryEvent(ts=datetime.now(timezone.utc), process="md", data=data)
    try_put(ctx.q_telemetry, evt)


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
    try_put(ctx.q_journal, evt)


def _send_control(ctx: ProcessContext, action: str, reason: str) -> None:
    cmd = ControlCommand(ts=datetime.now(timezone.utc), action=action, reason=reason)
    try_put(ctx.q_control_core, cmd)
    try_put(ctx.q_control_exec, cmd)


def _put_market_latest(ctx: ProcessContext, event) -> None:
    # Runtime queues are bounded (ipc.market_queue_size default 1), but tests may use
    # unbounded `queue.Queue`. Drain to "latest-only" to avoid ever accumulating.
    for _ in range(128):
        if try_get(ctx.q_market_core) is None:
            break
    for _ in range(128):
        if try_get(ctx.q_market_exec) is None:
            break
    put_latest(ctx.q_market_core, event)
    put_latest(ctx.q_market_exec, event)


def _telemetry_payload(
    ctx: ProcessContext,
    last_market_ts: Optional[datetime],
    last_market_arrival_ts: Optional[datetime],
) -> Dict[str, Any]:
    return {
        "mode": ctx.mode,
        "last_market_ts": last_market_ts.isoformat() if last_market_ts else None,
        # `event.ts` is often a bar/bucket timestamp. For UI staleness, wall-clock arrival is more useful.
        "last_market_arrival_ts": last_market_arrival_ts.isoformat() if last_market_arrival_ts else None,
        "queue_market_core": queue_depth(ctx.q_market_core),
        "queue_market_exec": queue_depth(ctx.q_market_exec),
    }


def _run_paper(ctx: ProcessContext) -> None:
    cfg = ctx.config
    data_path = _cfg(cfg, "md.mock_data_path", _cfg(cfg, "data.path", ""))
    default_micro = _cfg(cfg, "data.default_micro", {})
    replay_speed = float(_cfg(cfg, "md.replay_speed", 0.0))
    loop_enabled = bool(_cfg(cfg, "md.loop", False))
    loop_sleep_sec = float(_cfg(cfg, "md.loop_sleep_sec", 0.0))
    heartbeat_interval = float(_cfg(cfg, "md.heartbeat_interval", 1.0))
    telemetry_interval = float(_cfg(cfg, "md.telemetry_interval", 2.0))
    telemetry_every_n = int(_cfg(cfg, "md.telemetry_every_n", 10))
    data_source = BacktestCSVDataSource(path=data_path, default_micro=default_micro)

    seq = 0
    hb_seq = 0
    last_market_ts: Optional[datetime] = None
    last_market_arrival_ts: Optional[datetime] = None
    last_hb = 0.0
    last_tel = 0.0
    while not ctx.stop_event.is_set():
        last_ts = None
        for event in data_source:
            if ctx.stop_event.is_set():
                break
            seq += 1
            last_market_ts = event.ts
            last_market_arrival_ts = datetime.now(timezone.utc)
            _put_market_latest(ctx, event)
            _send_journal(ctx, "market", {
                "ts": event.ts.isoformat(),
                "open": event.open,
                "high": event.high,
                "low": event.low,
                "close": event.close,
                "volume": event.volume,
                "micro": event.micro,
                "seq": seq,
            })

            if replay_speed > 0.0 and last_ts is not None:
                delta = (event.ts - last_ts).total_seconds()
                if delta > 0:
                    time.sleep(delta / replay_speed)
            last_ts = event.ts

            now = time.time()
            if heartbeat_interval > 0 and (now - last_hb) >= heartbeat_interval:
                hb_seq += 1
                _send_heartbeat(ctx, hb_seq)
                last_hb = now

            if telemetry_every_n > 0 and (seq % telemetry_every_n) == 0:
                _send_telemetry(ctx, _telemetry_payload(ctx, last_market_ts, last_market_arrival_ts))
                last_tel = now
            elif telemetry_interval > 0 and (now - last_tel) >= telemetry_interval:
                _send_telemetry(ctx, _telemetry_payload(ctx, last_market_ts, last_market_arrival_ts))
                last_tel = now

        if not loop_enabled:
            break
        if loop_sleep_sec > 0.0:
            time.sleep(loop_sleep_sec)


async def _run_live_async(ctx: ProcessContext) -> None:
    cfg = ctx.config
    exchange = str(_cfg(cfg, "md.exchange", _cfg(cfg, "live.exchange", "kraken"))).strip().lower()
    if exchange == "binance":
        pair = _cfg(cfg, "md.pair", _cfg(cfg, "live.symbol", "ETH/USDT"))
        ws_url = _cfg(cfg, "md.ws_url", _cfg(cfg, "live.ws_url", "wss://stream.binance.com:9443"))
    else:
        pair = _cfg(cfg, "md.pair", _cfg(cfg, "live.kraken_pair", "BTC/EUR"))
        ws_url = _cfg(cfg, "md.ws_url", _cfg(cfg, "live.websocket_url", "wss://ws.kraken.com/v2"))
    stale_seconds = float(_cfg(cfg, "md.stale_seconds", 10.0))
    stale_book_seconds = float(_cfg(cfg, "md.stale_book_seconds", stale_seconds))
    stale_trade_seconds = float(_cfg(cfg, "md.stale_trade_seconds", stale_seconds))
    backoff_min = float(_cfg(cfg, "md.reconnect_min", 1.0))
    backoff_max = float(_cfg(cfg, "md.reconnect_max", 30.0))
    depth = int(_cfg(cfg, "md.depth", 100))
    interval_seconds = int(_cfg(cfg, "md.interval_seconds", 300))
    heartbeat_interval = float(_cfg(cfg, "md.heartbeat_interval", 1.0))
    telemetry_interval = float(_cfg(cfg, "md.telemetry_interval", 2.0))
    telemetry_every_n = int(_cfg(cfg, "md.telemetry_every_n", 10))

    def make_client():
        if exchange == "binance":
            return BinanceWSMarketData(
                pair=pair,
                url=ws_url,
                interval_seconds=interval_seconds,
                stale_seconds=stale_seconds,
                stale_book_seconds=stale_book_seconds,
                stale_trade_seconds=stale_trade_seconds,
            )
        return KrakenWSV2MarketData(
            pair=pair,
            url=ws_url,
            depth=depth,
            interval_seconds=interval_seconds,
            stale_seconds=stale_seconds,
            stale_book_seconds=stale_book_seconds,
            stale_trade_seconds=stale_trade_seconds,
        )

    seq = 0
    hb_seq = 0
    last_market_ts: Optional[datetime] = None
    last_market_arrival_ts: Optional[datetime] = None
    backoff = backoff_min
    reconnects = 0
    stale_count = 0

    async def heartbeat_loop() -> None:
        nonlocal hb_seq
        if heartbeat_interval <= 0:
            return
        while not ctx.stop_event.is_set():
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            await asyncio.sleep(heartbeat_interval)

    async def telemetry_loop() -> None:
        if telemetry_interval <= 0:
            return
        while not ctx.stop_event.is_set():
            payload = _telemetry_payload(ctx, last_market_ts, last_market_arrival_ts)
            payload["reconnects"] = reconnects
            payload["stale_count"] = stale_count
            _send_telemetry(ctx, payload)
            await asyncio.sleep(telemetry_interval)

    hb_task = asyncio.create_task(heartbeat_loop())
    tel_task = asyncio.create_task(telemetry_loop())

    try:
        while not ctx.stop_event.is_set():
            client = make_client()
            got_any = False
            try:
                async for event in client.stream():
                    if ctx.stop_event.is_set():
                        break
                    got_any = True
                    seq += 1
                    last_market_ts = event.ts
                    last_market_arrival_ts = datetime.now(timezone.utc)
                    _put_market_latest(ctx, event)
                    _send_journal(ctx, "market", {
                        "ts": event.ts.isoformat(),
                        "open": event.open,
                        "high": event.high,
                        "low": event.low,
                        "close": event.close,
                        "volume": event.volume,
                        "micro": event.micro,
                        "seq": seq,
                    })

                    if telemetry_every_n > 0 and (seq % telemetry_every_n) == 0:
                        payload = _telemetry_payload(ctx, last_market_ts, last_market_arrival_ts)
                        payload["reconnects"] = reconnects
                        payload["stale_count"] = stale_count
                        _send_telemetry(ctx, payload)

                if ctx.stop_event.is_set():
                    break
                # If the stream ends without an error, avoid spinning.
                if not got_any:
                    raise RuntimeError("market data stream ended without events")
                raise RuntimeError("market data stream ended unexpectedly")
            except (StaleDataError, KrakenStaleDataError, BinanceStaleDataError) as exc:
                stale_count += 1
                # STOP now only disables new trading; it no longer forces emergency exits.
                _send_control(ctx, "STOP", "stale_market_data")
                _send_control(ctx, "CANCEL_ALL", "stale_market_data")
                _send_journal(ctx, "md_stale", {"reason": str(exc)})
                reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff_max, backoff * 2)
            except Exception as exc:
                _send_journal(ctx, "md_error", {"error": str(exc), "type": type(exc).__name__})
                reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff_max, backoff * 2)
                continue
            else:
                backoff = backoff_min
    finally:
        for t in (hb_task, tel_task):
            t.cancel()
        for t in (hb_task, tel_task):
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass


def run_md(ctx: ProcessContext) -> None:
    _send_journal(ctx, "md_start", {"mode": ctx.mode})
    if ctx.mode == "paper":
        _run_paper(ctx)
    else:
        asyncio.run(_run_live_async(ctx))
    _send_journal(ctx, "md_stop", {})
