from __future__ import annotations

import time
from typing import List

from trading.types import Order
from trading.execution.base import ExecutionAdapter
from trading.types import Fill, MarketEvent


class KrakenRestExecutionAdapter(ExecutionAdapter):
    def __init__(self, api_key: str, api_secret: str, max_rate_per_sec: float = 1.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.max_rate_per_sec = max_rate_per_sec
        self._last_call = 0.0
        self._seen_ids: set[str] = set()
        self.order_state: dict[str, str] = {}

    def _rate_limit(self) -> None:
        now = time.time()
        min_interval = 1.0 / max(self.max_rate_per_sec, 1e-9)
        if now - self._last_call < min_interval:
            time.sleep(min_interval - (now - self._last_call))
        self._last_call = time.time()

    def submit_orders(self, orders: List[Order]) -> None:
        # Stub: implement REST calls with idempotency + retries.
        for order in orders:
            if order.id and order.id in self._seen_ids:
                continue
            if order.id:
                self._seen_ids.add(order.id)
                self.order_state[order.id] = "submitted"
            self._rate_limit()

    def submit(self, orders: List[Order]) -> None:
        self.submit_orders(orders)

    def process(self, event: MarketEvent, spread_bps: float) -> List[Fill]:
        # Live adapter would reconcile fills from exchange in real-time.
        # Stub returns no fills here.
        return []
