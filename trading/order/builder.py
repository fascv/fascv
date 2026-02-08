from __future__ import annotations

from dataclasses import dataclass
from typing import List

from trading.types import Order, RiskDecision


@dataclass
class OrderConfig:
    order_type: str  # "market" or "limit"
    post_only: bool
    limit_offset_bps: float
    min_trade_btc: float
    slice_count: int


class OrderBuilder:
    def __init__(self, config: OrderConfig):
        self.config = config

    def build(self, risk: RiskDecision, current_position_btc: float, price: float) -> List[Order]:
        delta = risk.target_position_btc - current_position_btc
        if abs(delta) < self.config.min_trade_btc:
            return []
        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        slices = max(1, self.config.slice_count)
        qty_per = qty / slices
        orders: List[Order] = []
        for i in range(slices):
            order_price = None
            if self.config.order_type == "limit":
                offset = self.config.limit_offset_bps / 10000.0
                if side == "buy":
                    order_price = price * (1.0 - offset)
                else:
                    order_price = price * (1.0 + offset)
            orders.append(
                Order(
                    ts=risk.ts,
                    side=side,
                    qty_btc=qty_per,
                    order_type=self.config.order_type,
                    price=order_price,
                    post_only=self.config.post_only,
                    id=f"order_{risk.ts.timestamp()}_{i}",
                )
            )
        return orders
