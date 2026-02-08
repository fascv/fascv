from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

from trading.kraken.book import BookChecksumError, BookSequenceError, OrderBook
from trading.types import MarketEvent
from trading.utils.kraken import map_pair


class StaleDataError(RuntimeError):
    pass


@dataclass
class Trade:
    ts: datetime
    price: Decimal
    volume: Decimal
    trade_id: Optional[int]


class BarAggregator:
    def __init__(self, interval_seconds: int):
        self.interval_seconds = interval_seconds
        self.current_start: Optional[datetime] = None
        self.open: Optional[Decimal] = None
        self.high: Optional[Decimal] = None
        self.low: Optional[Decimal] = None
        self.close: Optional[Decimal] = None
        self.volume: Decimal = Decimal("0")

    def _bucket_start(self, ts: datetime) -> datetime:
        epoch = int(ts.timestamp())
        bucket = epoch - (epoch % self.interval_seconds)
        return datetime.fromtimestamp(bucket, tz=timezone.utc)

    def update(self, trade: Trade) -> Optional[MarketEvent]:
        bucket = self._bucket_start(trade.ts)
        if self.current_start is None:
            self._start_bucket(bucket, trade)
            return None

        if bucket != self.current_start:
            event = MarketEvent(
                ts=self.current_start,
                open=float(self.open or trade.price),
                high=float(self.high or trade.price),
                low=float(self.low or trade.price),
                close=float(self.close or trade.price),
                volume=float(self.volume),
                micro={},
            )
            self._start_bucket(bucket, trade)
            return event

        self._apply_trade(trade)
        return None

    def _start_bucket(self, bucket: datetime, trade: Trade) -> None:
        self.current_start = bucket
        self.open = trade.price
        self.high = trade.price
        self.low = trade.price
        self.close = trade.price
        self.volume = trade.volume

    def _apply_trade(self, trade: Trade) -> None:
        if self.open is None:
            self._start_bucket(self._bucket_start(trade.ts), trade)
            return
        self.high = max(self.high, trade.price) if self.high is not None else trade.price
        self.low = min(self.low, trade.price) if self.low is not None else trade.price
        self.close = trade.price
        self.volume += trade.volume


class KrakenWSV2MarketData:
    def __init__(
        self,
        pair: str,
        url: str,
        depth: int = 100,
        interval_seconds: int = 300,
        stale_seconds: float = 10.0,
        stale_book_seconds: float | None = None,
        stale_trade_seconds: float | None = None,
    ):
        if websockets is None:
            raise RuntimeError("websockets library is required for live market data")
        self.pair = map_pair(pair)
        self.url = url
        self.depth = depth
        self.interval_seconds = interval_seconds
        self.stale_seconds = stale_seconds
        self.stale_book_seconds = stale_seconds if stale_book_seconds is None else stale_book_seconds
        self.stale_trade_seconds = stale_seconds if stale_trade_seconds is None else stale_trade_seconds
        self.book = OrderBook(depth=depth, checksum_depth=10)
        self._last_trade_id: Optional[int] = None

    async def stream(self) -> AsyncIterator[MarketEvent]:
        backoff = 1.0
        while True:
            try:
                async for event in self._connect_and_stream():
                    backoff = 1.0
                    yield event
            except (BookChecksumError, BookSequenceError):
                self.book.reset()
                self._last_trade_id = None
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            except StaleDataError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
                continue

    async def _connect_and_stream(self) -> AsyncIterator[MarketEvent]:
        async with websockets.connect(self.url, ping_interval=None) as ws:
            await self._subscribe(ws)
            agg = BarAggregator(self.interval_seconds)
            last_any_msg_time = time.time()
            last_book_msg_time = time.time()
            last_trade_msg_time = time.time()
            while True:
                try:
                    msg_raw = await asyncio.wait_for(ws.recv(), timeout=self.stale_seconds)
                except asyncio.TimeoutError as exc:
                    raise StaleDataError("no messages received") from exc

                now = time.time()
                last_any_msg_time = now
                msg = json.loads(msg_raw, parse_float=Decimal)
                if isinstance(msg, dict) and msg.get("channel") == "heartbeat":
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue
                if isinstance(msg, dict) and msg.get("event") in {"systemStatus", "subscriptionStatus"}:
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue
                if isinstance(msg, dict) and msg.get("channel") == "book":
                    self._handle_book_message(msg)
                    last_book_msg_time = now
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue
                if isinstance(msg, dict) and msg.get("channel") == "trade":
                    trade = self._handle_trade_message(msg)
                    last_trade_msg_time = now
                    if trade:
                        event = agg.update(trade)
                        if event is not None:
                            event.micro = self.book.micro_features()
                            yield event
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue

                self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)

    def _check_stale(
        self,
        now: float,
        last_any_msg_time: float,
        last_book_msg_time: float,
        last_trade_msg_time: float,
    ) -> None:
        if self.stale_seconds > 0 and (now - last_any_msg_time) > self.stale_seconds:
            raise StaleDataError("no messages received")
        if self.stale_book_seconds > 0 and (now - last_book_msg_time) > self.stale_book_seconds:
            raise StaleDataError("book stale")
        if self.stale_trade_seconds > 0 and (now - last_trade_msg_time) > self.stale_trade_seconds:
            raise StaleDataError("trade stale")

    async def _subscribe(self, ws) -> None:
        payload = {
            "method": "subscribe",
            "params": {
                "channel": "book",
                "symbol": [self.pair],
                "depth": self.depth,
                "snapshot": True,
            },
        }
        await ws.send(json.dumps(payload))
        payload = {
            "method": "subscribe",
            "params": {
                "channel": "trade",
                "symbol": [self.pair],
                "snapshot": False,
            },
        }
        await ws.send(json.dumps(payload))

    def _handle_book_message(self, msg: Dict[str, Any]) -> None:
        data = msg.get("data", [])
        typ = msg.get("type")
        for entry in data:
            bids = [(Decimal(p), Decimal(q)) for p, q in entry.get("bids", [])]
            asks = [(Decimal(p), Decimal(q)) for p, q in entry.get("asks", [])]
            ts = entry.get("timestamp")
            if typ == "snapshot":
                self.book.apply_snapshot(bids, asks, ts)
            else:
                self.book.apply_update(bids, asks, ts)
            checksum = entry.get("checksum")
            if checksum is not None:
                self.book.validate_checksum(int(checksum))

    def _handle_trade_message(self, msg: Dict[str, Any]) -> Optional[Trade]:
        data = msg.get("data", [])
        if not data:
            return None
        trade = data[-1]
        price = Decimal(trade.get("price"))
        qty = Decimal(trade.get("qty"))
        ts = datetime.fromtimestamp(float(trade.get("timestamp")), tz=timezone.utc)
        trade_id = trade.get("trade_id")
        if trade_id is not None:
            trade_id = int(trade_id)
            if self._last_trade_id is not None and trade_id <= self._last_trade_id:
                raise BookSequenceError("trade_id sequence inconsistency")
            self._last_trade_id = trade_id
        return Trade(ts=ts, price=price, volume=qty, trade_id=trade_id)
