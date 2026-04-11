from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Dict, Optional

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None

from trading.types import MarketEvent
from trading.utils.binance import to_binance_symbol


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
        self.interval_seconds = max(1, int(interval_seconds))
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


class BinanceWSMarketData:
    def __init__(
        self,
        pair: str,
        url: str,
        interval_seconds: int = 300,
        stale_seconds: float = 10.0,
        stale_book_seconds: float | None = None,
        stale_trade_seconds: float | None = None,
    ):
        if websockets is None:
            raise RuntimeError("websockets library is required for live market data")
        self.pair = pair
        self.symbol = to_binance_symbol(pair).lower()
        self.url = self._resolve_url(url)
        self.interval_seconds = max(1, int(interval_seconds))
        self.stale_seconds = float(stale_seconds)
        self.stale_book_seconds = float(stale_seconds if stale_book_seconds is None else stale_book_seconds)
        self.stale_trade_seconds = float(stale_seconds if stale_trade_seconds is None else stale_trade_seconds)
        self._last_trade_id: Optional[int] = None
        self._best_bid: Optional[Decimal] = None
        self._best_ask: Optional[Decimal] = None
        self._best_bid_qty: Decimal = Decimal("0")
        self._best_ask_qty: Decimal = Decimal("0")

    def _resolve_url(self, url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            raw = "wss://stream.binance.com:9443"
        if "@trade" in raw and "streams=" in raw:
            return raw
        if raw.endswith("/"):
            raw = raw[:-1]
        stream_path = f"/stream?streams={self.symbol}@trade/{self.symbol}@bookTicker"
        if raw.endswith("/stream") or raw.endswith("/ws"):
            return f"{raw}?streams={self.symbol}@trade/{self.symbol}@bookTicker"
        return raw + stream_path

    async def stream(self) -> AsyncIterator[MarketEvent]:
        backoff = 1.0
        while True:
            try:
                async for event in self._connect_and_stream():
                    backoff = 1.0
                    yield event
            except StaleDataError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)

    async def _connect_and_stream(self) -> AsyncIterator[MarketEvent]:
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
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
                payload = msg.get("data", msg) if isinstance(msg, dict) else {}
                if not isinstance(payload, dict):
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue

                et = str(payload.get("e", ""))
                # Spot bookTicker frames can arrive without explicit "e":"bookTicker".
                is_book_ticker = et == "bookTicker" or (
                    "b" in payload and "a" in payload and "B" in payload and "A" in payload
                )
                if is_book_ticker:
                    self._handle_book(payload)
                    last_book_msg_time = now
                    # Some symbols can publish book updates without emitting trade frames.
                    # In that case, drive bars from mid-price quotes as a fallback.
                    quote_trade = self._quote_as_trade(datetime.now(timezone.utc))
                    if quote_trade is not None:
                        event = agg.update(quote_trade)
                        if event is not None:
                            event.micro = self._micro_features()
                            yield event
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue

                if et == "trade":
                    trade = self._handle_trade(payload)
                    last_trade_msg_time = now
                    if trade is not None:
                        event = agg.update(trade)
                        if event is not None:
                            event.micro = self._micro_features()
                            yield event
                    self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)
                    continue

                self._check_stale(now, last_any_msg_time, last_book_msg_time, last_trade_msg_time)

    def _quote_as_trade(self, ts: datetime) -> Optional[Trade]:
        bid = self._best_bid
        ask = self._best_ask
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / Decimal("2")
        if mid <= 0:
            return None
        return Trade(ts=ts, price=mid, volume=Decimal("0"), trade_id=None)

    def _check_stale(
        self,
        now: float,
        last_any_msg_time: float,
        last_book_msg_time: float,
        last_trade_msg_time: float,
    ) -> None:
        if self.stale_seconds > 0 and (now - last_any_msg_time) > self.stale_seconds:
            raise StaleDataError("no messages received")
        book_stale = self.stale_book_seconds > 0 and (now - last_book_msg_time) > self.stale_book_seconds
        trade_stale = self.stale_trade_seconds > 0 and (now - last_trade_msg_time) > self.stale_trade_seconds
        # Binance @bookTicker can be bursty for some symbols. Only fail hard if both
        # feeds are stale at once; otherwise keep running.
        if book_stale and trade_stale:
            raise StaleDataError("book+trade stale")
        if trade_stale and (self._best_bid is None or self._best_ask is None):
            raise StaleDataError("trade stale")

    def _handle_book(self, payload: Dict[str, Any]) -> None:
        try:
            self._best_bid = Decimal(payload.get("b", "0"))
            self._best_ask = Decimal(payload.get("a", "0"))
            self._best_bid_qty = Decimal(payload.get("B", "0"))
            self._best_ask_qty = Decimal(payload.get("A", "0"))
        except Exception:
            return

    def _handle_trade(self, payload: Dict[str, Any]) -> Optional[Trade]:
        try:
            price = Decimal(payload.get("p", "0"))
            qty = Decimal(payload.get("q", "0"))
            ts_ms = int(payload.get("T", 0) or 0)
            trade_id = int(payload.get("t", 0) or 0)
        except Exception:
            return None
        if trade_id > 0:
            if self._last_trade_id is not None and trade_id <= self._last_trade_id:
                return None
            self._last_trade_id = trade_id
        ts = datetime.fromtimestamp(max(0, ts_ms) / 1000.0, tz=timezone.utc)
        return Trade(ts=ts, price=price, volume=qty, trade_id=(trade_id if trade_id > 0 else None))

    def _micro_features(self) -> Dict[str, float]:
        bid = self._best_bid
        ask = self._best_ask
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return {}
        mid = (bid + ask) / Decimal("2")
        if mid <= 0:
            return {}
        spread_bps = float(((ask - bid) / mid) * Decimal("10000"))
        bid_notional = bid * self._best_bid_qty
        ask_notional = ask * self._best_ask_qty
        depth = float(bid_notional + ask_notional)
        imbalance = 0.0
        denom = bid_notional + ask_notional
        if denom > 0:
            imbalance = float((bid_notional - ask_notional) / denom)
        return {
            "spread_bps": max(0.0, spread_bps),
            "depth": max(0.0, depth),
            "imbalance": max(-1.0, min(1.0, imbalance)),
        }
