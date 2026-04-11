from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

from trading.binance.rest import BinanceRestClient
from trading.utils.binance import normalize_pair


@dataclass
class BinanceExecutionUpdate:
    ts: datetime
    symbol: str
    pair: str
    order_id: str
    client_order_id: str
    status: str
    exec_type: str
    side: str
    orig_qty: float
    cum_qty: float
    cum_quote: float
    last_qty: float
    last_price: float
    fee_quote: float
    trade_id: Optional[str] = None
    event_id: Optional[str] = None


class BinanceUserDataWS:
    def __init__(
        self,
        rest: BinanceRestClient,
        *,
        url: str = "wss://stream.binance.com:9443",
        keepalive_sec: float = 30 * 60.0,
    ):
        if websockets is None:
            raise RuntimeError("websockets library is required for authenticated WS")
        self.rest = rest
        self.url = str(url or "").strip()
        self.keepalive_sec = max(30.0, float(keepalive_sec))

    def _resolve_ws_url(self, listen_key: str) -> str:
        key = str(listen_key or "").strip()
        raw = self.url or "wss://stream.binance.com:9443"
        if "{listenKey}" in raw:
            return raw.replace("{listenKey}", key)
        raw = raw.rstrip("/")
        if "/ws/" in raw:
            return f"{raw}{key}"
        if raw.endswith("/ws"):
            return f"{raw}/{key}"
        return f"{raw}/ws/{key}"

    async def stream(self) -> AsyncIterator[BinanceExecutionUpdate]:
        backoff = 1.0
        while True:
            try:
                listen_key = await asyncio.to_thread(self.rest.start_user_data_stream)
                async for item in self._stream_with_key(listen_key):
                    backoff = 1.0
                    yield item
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    async def _stream_with_key(self, listen_key: str) -> AsyncIterator[BinanceExecutionUpdate]:
        ws_url = self._resolve_ws_url(listen_key)
        keepalive_task = asyncio.create_task(self._keepalive_loop(listen_key))
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        continue
                    et = str(msg.get("e", "")).strip()
                    if et == "listenKeyExpired":
                        break
                    if et != "executionReport":
                        continue
                    update = self._parse_execution_report(msg)
                    if update is not None:
                        yield update
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except BaseException:
                pass
            await asyncio.to_thread(self.rest.close_user_data_stream, listen_key)

    async def _keepalive_loop(self, listen_key: str) -> None:
        while True:
            await asyncio.sleep(self.keepalive_sec)
            await asyncio.to_thread(self.rest.keepalive_user_data_stream, listen_key)

    def _parse_execution_report(self, msg: dict[str, Any]) -> Optional[BinanceExecutionUpdate]:
        try:
            symbol = str(msg.get("s", "")).upper()
            order_id = str(msg.get("i", "")).strip()
            client_order_id = str(msg.get("c", "")).strip()
            status = str(msg.get("X", "")).strip().upper()
            exec_type = str(msg.get("x", "")).strip().upper()
            side = str(msg.get("S", "")).strip().lower()
            orig_qty = float(msg.get("q", "0") or 0.0)
            cum_qty = float(msg.get("z", "0") or 0.0)
            cum_quote = float(msg.get("Z", "0") or 0.0)
            last_qty = float(msg.get("l", "0") or 0.0)
            last_price = float(msg.get("L", "0") or 0.0)
            commission = float(msg.get("n", "0") or 0.0)
            commission_asset = str(msg.get("N", "")).upper()
        except Exception:
            return None

        event_ms = int(msg.get("E", 0) or 0)
        trade_ms = int(msg.get("T", 0) or 0)
        ts_ms = trade_ms if trade_ms > 0 else event_ms
        if ts_ms <= 0:
            ts = datetime.now(timezone.utc)
        else:
            ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

        trade_id_raw = msg.get("t")
        trade_id = None if trade_id_raw in (None, "", -1) else str(trade_id_raw)
        event_id = f"{order_id}:{event_ms or int(time.time()*1000)}"

        fee_quote = 0.0
        if commission > 0.0:
            fee_quote = self.rest.commission_to_quote(
                symbol=symbol,
                commission=commission,
                commission_asset=commission_asset,
                trade_price=last_price,
            )

        pair = normalize_pair(symbol)
        return BinanceExecutionUpdate(
            ts=ts,
            symbol=symbol,
            pair=pair,
            order_id=order_id,
            client_order_id=client_order_id,
            status=status,
            exec_type=exec_type,
            side=side,
            orig_qty=max(0.0, orig_qty),
            cum_qty=max(0.0, cum_qty),
            cum_quote=max(0.0, cum_quote),
            last_qty=max(0.0, last_qty),
            last_price=max(0.0, last_price),
            fee_quote=max(0.0, fee_quote),
            trade_id=trade_id,
            event_id=event_id,
        )
