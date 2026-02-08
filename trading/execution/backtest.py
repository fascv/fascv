from __future__ import annotations

from dataclasses import dataclass
from typing import List

from trading.execution.base import ExecutionAdapter
from trading.types import Fill, MarketEvent, Order


@dataclass
class BacktestExecutionConfig:
    latency_bars: int
    partial_fill_ratio: float
    slippage_bps: float


class BacktestSimulator(ExecutionAdapter):
    def __init__(self, config: BacktestExecutionConfig, maker_fee_bps: float, taker_fee_bps: float):
        self.config = config
        self.maker_fee_bps = maker_fee_bps
        self.taker_fee_bps = taker_fee_bps
        self._pending: List[dict] = []
        self._seen_ids: set[str] = set()
        self.order_state: dict[str, str] = {}

    def submit(self, orders: List[Order]) -> None:
        for order in orders:
            if order.id in self._seen_ids:
                continue
            if order.id:
                self._seen_ids.add(order.id)
                self.order_state[order.id] = "submitted"
            self._pending.append({"order": order, "latency": self.config.latency_bars})

    def cancel_all(self) -> List[str]:
        canceled: List[str] = []
        for item in list(self._pending):
            order: Order = item["order"]
            if order.id:
                self.order_state[order.id] = "canceled"
                canceled.append(order.id)
            self._pending.remove(item)
        return canceled

    def process(self, event: MarketEvent, spread_bps: float) -> List[Fill]:
        fills: List[Fill] = []
        for item in list(self._pending):
            if item["latency"] > 0:
                item["latency"] -= 1
                continue
            order: Order = item["order"]
            fill = self._try_fill(order, event, spread_bps)
            if fill:
                fills.append(fill)
            self._pending.remove(item)
        return fills

    def _try_fill(self, order: Order, event: MarketEvent, spread_bps: float) -> Fill | None:
        side = order.side
        sign = 1.0 if side == "buy" else -1.0
        qty = order.qty_btc * self.config.partial_fill_ratio

        if order.order_type == "market":
            slip = self.config.slippage_bps
            price = event.close * (1.0 + sign * (spread_bps / 20000.0 + slip / 10000.0))
            fee_bps = self.taker_fee_bps
            fee = qty * price * fee_bps / 10000.0
            if order.id:
                self.order_state[order.id] = "filled" if self.config.partial_fill_ratio >= 1.0 else "partial"
            return Fill(
                ts=event.ts,
                side=side,
                qty_btc=qty,
                price=price,
                fee_eur=fee,
                order_id=order.id,
                slippage_bps=slip,
            )

        # limit order
        limit_price = order.price if order.price is not None else event.close
        if side == "buy" and event.low <= limit_price:
            fee_bps = self.maker_fee_bps if order.post_only else self.taker_fee_bps
            fee = qty * limit_price * fee_bps / 10000.0
            if order.id:
                self.order_state[order.id] = "filled" if self.config.partial_fill_ratio >= 1.0 else "partial"
            return Fill(
                ts=event.ts,
                side=side,
                qty_btc=qty,
                price=limit_price,
                fee_eur=fee,
                order_id=order.id,
                slippage_bps=0.0,
            )
        if side == "sell" and event.high >= limit_price:
            fee_bps = self.maker_fee_bps if order.post_only else self.taker_fee_bps
            fee = qty * limit_price * fee_bps / 10000.0
            if order.id:
                self.order_state[order.id] = "filled" if self.config.partial_fill_ratio >= 1.0 else "partial"
            return Fill(
                ts=event.ts,
                side=side,
                qty_btc=qty,
                price=limit_price,
                fee_eur=fee,
                order_id=order.id,
                slippage_bps=0.0,
            )
        return None
