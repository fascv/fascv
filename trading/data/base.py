from __future__ import annotations

from typing import Iterable

from trading.types import MarketEvent


class MarketDataSource:
    def __iter__(self) -> Iterable[MarketEvent]:
        raise NotImplementedError
