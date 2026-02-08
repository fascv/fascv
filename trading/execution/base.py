from __future__ import annotations

from typing import List

from trading.types import Fill, MarketEvent, Order


class ExecutionAdapter:
    def submit(self, orders: List[Order]) -> None:
        raise NotImplementedError

    def process(self, event: MarketEvent, spread_bps: float) -> List[Fill]:
        raise NotImplementedError
