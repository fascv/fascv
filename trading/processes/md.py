from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict

from trading.data.backtest import BacktestCSVDataSource
from trading.kraken.ws_public import KrakenWSV2MarketData, StaleDataError
from trading.ipc.events import ControlCommand, Heartbeat, JournalEvent, TelemetryEvent
from trading.ipc.queues import put_latest, try_put, queue_depth
from trading.processes.context import ProcessContext


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


def _run_paper(ctx: ProcessContext) -> None:
    cfg = ctx.config
    data_path = _cfg(cfg, "md.mock_data_path", _cfg(cfg, "data.path", ""))
    default_micro = _cfg(cfg, "data.default_micro", {})
    replay_speed = float(_cfg(cfg, "md.replay_speed", 0.0))
    data_source = BacktestCSVDataSource(path=data_path, default_micro=default_micro)

    last_ts = None
    seq = 0
    for event in data_source:
        if ctx.stop_event.is_set():
            break
        seq += 1
        put_latest(ctx.q_market_core, event)
        put_latest(ctx.q_market_exec, event)
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

        if seq % 10 == 0:
            _send_heartbeat(ctx, seq)
            _send_telemetry(
                ctx,
                {
                    "mode": ctx.mode,
                    "last_market_ts": event.ts.isoformat(),
                    "queue_market_core": queue_depth(ctx.q_market_core),
                    "queue_market_exec": queue_depth(ctx.q_market_exec),
                },
            )


async def _run_live_async(ctx: ProcessContext) -> None:
    cfg = ctx.config
    pair = _cfg(cfg, "md.pair", _cfg(cfg, "live.kraken_pair", "XBT/EUR"))
    ws_url = _cfg(cfg, "md.ws_url", _cfg(cfg, "live.websocket_url", "wss://ws.kraken.com/v2"))
    stale_seconds = float(_cfg(cfg, "md.stale_seconds", 10.0))
    stale_book_seconds = float(_cfg(cfg, "md.stale_book_seconds", stale_seconds))
    stale_trade_seconds = float(_cfg(cfg, "md.stale_trade_seconds", stale_seconds))
    backoff_min = float(_cfg(cfg, "md.reconnect_min", 1.0))
    backoff_max = float(_cfg(cfg, "md.reconnect_max", 30.0))
    depth = int(_cfg(cfg, "md.depth", 100))
    interval_seconds = int(_cfg(cfg, "md.interval_seconds", 300))

    client = KrakenWSV2MarketData(
        pair=pair,
        url=ws_url,
        depth=depth,
        interval_seconds=interval_seconds,
        stale_seconds=stale_seconds,
        stale_book_seconds=stale_book_seconds,
        stale_trade_seconds=stale_trade_seconds,
    )
    seq = 0
    backoff = backoff_min

    while not ctx.stop_event.is_set():
        try:
            async for event in client.stream():
                if ctx.stop_event.is_set():
                    break
                seq += 1
                put_latest(ctx.q_market_core, event)
                put_latest(ctx.q_market_exec, event)
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
                if seq % 10 == 0:
                    _send_heartbeat(ctx, seq)
                    _send_telemetry(
                        ctx,
                        {
                            "mode": ctx.mode,
                            "last_market_ts": event.ts.isoformat(),
                            "queue_market_core": queue_depth(ctx.q_market_core),
                            "queue_market_exec": queue_depth(ctx.q_market_exec),
                        },
                    )
        except StaleDataError as exc:
            _send_control(ctx, "STOP", "stale_market_data")
            _send_control(ctx, "CANCEL_ALL", "stale_market_data")
            _send_journal(ctx, "md_stale", {"reason": str(exc)})
            await asyncio.sleep(backoff)
            backoff = min(backoff_max, backoff * 2)
        except Exception as exc:
            _send_journal(ctx, "md_error", {"error": str(exc)})
            await asyncio.sleep(backoff)
            backoff = min(backoff_max, backoff * 2)
            continue


def run_md(ctx: ProcessContext) -> None:
    _send_journal(ctx, "md_start", {"mode": ctx.mode})
    if ctx.mode == "paper":
        _run_paper(ctx)
    else:
        asyncio.run(_run_live_async(ctx))
    _send_journal(ctx, "md_stop", {})
