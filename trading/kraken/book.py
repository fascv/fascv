from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple
import zlib


class BookChecksumError(RuntimeError):
    pass


class BookSequenceError(RuntimeError):
    pass


@dataclass
class BookLevel:
    price: Decimal
    qty: Decimal


class OrderBook:
    def __init__(self, depth: int = 100, checksum_depth: int = 10):
        self.depth = depth
        self.checksum_depth = checksum_depth
        self.bids: Dict[Decimal, Decimal] = {}
        self.asks: Dict[Decimal, Decimal] = {}
        self.last_ts: str | None = None

    def reset(self) -> None:
        self.bids = {}
        self.asks = {}
        self.last_ts = None

    def apply_snapshot(self, bids: Iterable[Tuple[Decimal, Decimal]], asks: Iterable[Tuple[Decimal, Decimal]], ts: str | None = None) -> None:
        self.bids = {price: qty for price, qty in bids if qty > 0}
        self.asks = {price: qty for price, qty in asks if qty > 0}
        self._trim()
        self.last_ts = ts

    def apply_update(self, bids: Iterable[Tuple[Decimal, Decimal]], asks: Iterable[Tuple[Decimal, Decimal]], ts: str | None = None) -> None:
        for price, qty in bids:
            if qty <= 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for price, qty in asks:
            if qty <= 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self._trim()
        self.last_ts = ts

    def _trim(self) -> None:
        if len(self.bids) > self.depth:
            for price in sorted(self.bids.keys(), reverse=True)[self.depth:]:
                self.bids.pop(price, None)
        if len(self.asks) > self.depth:
            for price in sorted(self.asks.keys())[self.depth:]:
                self.asks.pop(price, None)

    def top_levels(self) -> Tuple[List[BookLevel], List[BookLevel]]:
        bids = [BookLevel(p, self.bids[p]) for p in sorted(self.bids.keys(), reverse=True)[: self.checksum_depth]]
        asks = [BookLevel(p, self.asks[p]) for p in sorted(self.asks.keys())[: self.checksum_depth]]
        return bids, asks

    def checksum(self) -> int:
        bids, asks = self.top_levels()
        parts: List[str] = []
        for lvl in asks:
            parts.append(_fmt_decimal(lvl.price))
            parts.append(_fmt_decimal(lvl.qty))
        for lvl in bids:
            parts.append(_fmt_decimal(lvl.price))
            parts.append(_fmt_decimal(lvl.qty))
        checksum_str = "".join(parts)
        return zlib.crc32(checksum_str.encode()) & 0xFFFFFFFF

    def validate_checksum(self, expected: int) -> None:
        actual = self.checksum()
        if actual != expected:
            raise BookChecksumError(f"checksum mismatch: expected={expected} actual={actual}")

    def micro_features(self) -> Dict[str, float]:
        if not self.bids or not self.asks:
            return {"spread_bps": 0.0, "depth": 0.0, "imbalance": 0.0}
        best_bid = max(self.bids.keys())
        best_ask = min(self.asks.keys())
        mid = (best_bid + best_ask) / Decimal("2")
        spread_bps = float((best_ask - best_bid) / mid * Decimal("10000")) if mid > 0 else 0.0

        bid_qty = sum(self.bids[p] for p in sorted(self.bids.keys(), reverse=True)[:5])
        ask_qty = sum(self.asks[p] for p in sorted(self.asks.keys())[:5])
        depth = float(bid_qty + ask_qty)
        if bid_qty + ask_qty > 0:
            imbalance = float((bid_qty - ask_qty) / (bid_qty + ask_qty))
        else:
            imbalance = 0.0
        return {"spread_bps": spread_bps, "depth": depth, "imbalance": imbalance}


def _fmt_decimal(value: Decimal) -> str:
    s = format(value, "f")
    if "." in s:
        s = s.replace(".", "")
    s = s.lstrip("0")
    return s if s else "0"
