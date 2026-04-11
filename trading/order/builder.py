from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List

from trading.types import Order, RiskDecision


@dataclass
class OrderConfig:
    order_type: str  # "market" or "limit"
    post_only: bool
    limit_offset_bps: float
    min_trade_btc: float
    slice_count: int
    # If >0, use "cycle" mode: enter with a fixed notional once when flat, and exit fully back to flat.
    cycle_trade_eur: float = 0.0


class OrderBuilder:
    def __init__(self, config: OrderConfig):
        self.config = config

    def _clamp_buy_qty_by_cash(
        self,
        qty_btc: float,
        price: float,
        *,
        cash_eur: float | None,
        fee_bps: float | None,
        price_buffer_bps: float | None,
    ) -> float:
        if cash_eur is None:
            return qty_btc
        if not (price > 0.0):
            return 0.0
        qty = max(0.0, float(qty_btc))
        cash = max(0.0, float(cash_eur))
        if cash <= 0.0 or qty <= 0.0:
            return 0.0

        # Conservative clamp: include a price buffer (spread+slippage) and fee reserve,
        # so the simulation never spends more cash than available.
        buf = max(0.0, float(price_buffer_bps or 0.0))
        fee = max(0.0, float(fee_bps or 0.0))
        exec_price = float(price) * (1.0 + buf / 10000.0)
        denom = exec_price * (1.0 + fee / 10000.0)
        if not (denom > 0.0):
            return 0.0

        max_qty = cash / denom
        return min(qty, max(0.0, max_qty))

    def build(
        self,
        risk: RiskDecision,
        current_position_btc: float,
        price: float,
        *,
        cash_eur: float | None = None,
        buy_fee_bps: float | None = None,
        buy_price_buffer_bps: float | None = None,
    ) -> List[Order]:
        if self.config.cycle_trade_eur and self.config.cycle_trade_eur > 0:
            return self._build_cycle(
                risk,
                current_position_btc,
                price,
                cash_eur=cash_eur,
                buy_fee_bps=buy_fee_bps,
                buy_price_buffer_bps=buy_price_buffer_bps,
            )
        delta = risk.target_position_btc - current_position_btc
        if abs(delta) < self.config.min_trade_btc:
            return []
        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        if side == "buy":
            qty = self._clamp_buy_qty_by_cash(
                qty,
                price,
                cash_eur=cash_eur,
                fee_bps=buy_fee_bps,
                price_buffer_bps=buy_price_buffer_bps,
            )
            if qty < self.config.min_trade_btc:
                return []
        return self._slice_orders(ts=risk.ts, side=side, qty_btc=qty, price=price, id_prefix="order")

    def _slice_orders(
        self,
        *,
        ts: datetime,
        side: str,
        qty_btc: float,
        price: float,
        id_prefix: str,
    ) -> List[Order]:
        slices = max(1, self.config.slice_count)
        qty_per = qty_btc / slices
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
                    ts=ts,
                    side=side,
                    qty_btc=qty_per,
                    order_type=self.config.order_type,
                    price=order_price,
                    post_only=self.config.post_only,
                    id=f"{id_prefix}_{ts.timestamp()}_{i}",
                )
            )
        return orders

    def _build_cycle(
        self,
        risk: RiskDecision,
        current_position_btc: float,
        price: float,
        *,
        cash_eur: float | None,
        buy_fee_bps: float | None,
        buy_price_buffer_bps: float | None,
    ) -> List[Order]:
        # Cycle mode:
        # - When flat, enter once with a fixed notional (clamped to risk target, and optionally cash).
        # - When in a position, only exit back to flat when risk targets flat (or flips direction).
        eps = float(self.config.min_trade_btc)
        target = float(risk.target_position_btc)
        pos = float(current_position_btc)
        if price <= 0.0:
            return []

        if abs(pos) < eps:
            if abs(target) < eps:
                return []
            side = "buy" if target > 0 else "sell"
            qty = float(self.config.cycle_trade_eur) / float(price)
            qty = min(qty, abs(target))  # never exceed risk target
            if side == "buy":
                qty = self._clamp_buy_qty_by_cash(
                    qty,
                    price,
                    cash_eur=cash_eur,
                    fee_bps=buy_fee_bps,
                    price_buffer_bps=buy_price_buffer_bps,
                )
            if qty < eps:
                return []
            return self._slice_orders(ts=risk.ts, side=side, qty_btc=qty, price=price, id_prefix="cycle_entry")

        # In a position: only exit when the model wants to be flat or flips direction.
        want_exit = (abs(target) < eps) or (pos > 0 and target <= 0) or (pos < 0 and target >= 0)
        if not want_exit:
            return []
        side = "sell" if pos > 0 else "buy"
        qty = abs(pos)
        if qty < eps:
            return []
        return self._slice_orders(ts=risk.ts, side=side, qty_btc=qty, price=price, id_prefix="cycle_exit")
