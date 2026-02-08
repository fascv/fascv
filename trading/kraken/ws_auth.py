from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Iterable

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

from trading.kraken.rest import KrakenRestClient


@dataclass
class OpenOrderUpdate:
    ts: datetime
    order_id: str
    status: str
    vol: Optional[float] = None
    vol_exec: Optional[float] = None
    reason: Optional[str] = None


@dataclass
class OwnTradeUpdate:
    ts: datetime
    trade_id: str
    order_id: str
    side: str
    price: float
    vol: float
    fee: float
    pair: Optional[str] = None


class OpenOrdersWS:
    def __init__(self, rest: KrakenRestClient, url: str = "wss://ws-auth.kraken.com"):
        if websockets is None:
            raise RuntimeError("websockets library is required for authenticated WS")
        self.rest = rest
        self.url = url

    async def stream(self) -> AsyncIterator[OpenOrderUpdate]:
        token = self.rest.get_ws_token()
        async with websockets.connect(self.url, ping_interval=None) as ws:
            sub = {
                "event": "subscribe",
                "subscription": {"name": "openOrders", "token": token},
            }
            await ws.send(json.dumps(sub))
            while True:
                msg = json.loads(await ws.recv())
                if isinstance(msg, dict) and msg.get("event") in {"heartbeat", "subscriptionStatus"}:
                    continue
                for update in _parse_open_orders(msg):
                    yield update


class OwnTradesWS:
    def __init__(self, rest: KrakenRestClient, url: str = "wss://ws-auth.kraken.com"):
        if websockets is None:
            raise RuntimeError("websockets library is required for authenticated WS")
        self.rest = rest
        self.url = url

    async def stream(self) -> AsyncIterator[OwnTradeUpdate]:
        token = self.rest.get_ws_token()
        async with websockets.connect(self.url, ping_interval=None) as ws:
            sub = {
                "event": "subscribe",
                "subscription": {"name": "ownTrades", "token": token},
            }
            await ws.send(json.dumps(sub))
            while True:
                msg = json.loads(await ws.recv())
                if isinstance(msg, dict) and msg.get("event") in {"heartbeat", "subscriptionStatus"}:
                    continue
                for update in _parse_own_trades(msg):
                    yield update


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_open_orders(msg: Any) -> Iterable[OpenOrderUpdate]:
    if isinstance(msg, dict) and "openOrders" in msg:
        updates = msg.get("openOrders", {})
        for order_id, payload in updates.items():
            status = payload.get("status", "")
            vol = _to_float(payload.get("vol"))
            vol_exec = _to_float(payload.get("vol_exec"))
            yield OpenOrderUpdate(
                ts=datetime.now(timezone.utc),
                order_id=str(order_id),
                status=str(status),
                vol=vol,
                vol_exec=vol_exec,
            )
        return

    if isinstance(msg, list) and msg and msg[-1] == "openOrders":
        payloads = msg[0] if msg else []
        if isinstance(payloads, list):
            for entry in payloads:
                if isinstance(entry, dict):
                    for order_id, payload in entry.items():
                        status = payload.get("status", "")
                        vol = _to_float(payload.get("vol"))
                        vol_exec = _to_float(payload.get("vol_exec"))
                        yield OpenOrderUpdate(
                            ts=datetime.now(timezone.utc),
                            order_id=str(order_id),
                            status=str(status),
                            vol=vol,
                            vol_exec=vol_exec,
                        )


def _parse_own_trades(msg: Any) -> Iterable[OwnTradeUpdate]:
    if isinstance(msg, dict) and "ownTrades" in msg:
        updates = msg.get("ownTrades", {})
        for trade_id, payload in updates.items():
            yield _build_own_trade(str(trade_id), payload)
        return

    if isinstance(msg, list) and msg and msg[-1] == "ownTrades":
        payloads = msg[0] if msg else []
        if isinstance(payloads, list):
            for entry in payloads:
                if isinstance(entry, dict):
                    for trade_id, payload in entry.items():
                        yield _build_own_trade(str(trade_id), payload)


def _build_own_trade(trade_id: str, payload: Dict[str, Any]) -> OwnTradeUpdate:
    ts = datetime.fromtimestamp(float(payload.get("time", time.time())), tz=timezone.utc)
    return OwnTradeUpdate(
        ts=ts,
        trade_id=trade_id,
        order_id=str(payload.get("ordertxid", "")),
        side=str(payload.get("type", "")),
        price=float(payload.get("price", 0.0)),
        vol=float(payload.get("vol", 0.0)),
        fee=float(payload.get("fee", 0.0)),
        pair=payload.get("pair"),
    )
