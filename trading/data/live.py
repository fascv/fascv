from __future__ import annotations

import asyncio
from typing import Iterable, AsyncIterator

from trading.data.base import MarketDataSource
from trading.kraken.ws_public import KrakenWSV2MarketData
from trading.types import MarketEvent


class KrakenWebSocketDataSource(MarketDataSource):
    def __init__(self, pair: str, url: str, depth: int = 100, interval_seconds: int = 300, stale_seconds: float = 10.0):
        self.pair = pair
        self.url = url
        self.depth = depth
        self.interval_seconds = interval_seconds
        self.stale_seconds = stale_seconds
        self._client = KrakenWSV2MarketData(
            pair=pair,
            url=url,
            depth=depth,
            interval_seconds=interval_seconds,
            stale_seconds=stale_seconds,
        )

    def __iter__(self) -> Iterable[MarketEvent]:
        return asyncio.run(self._iter_async())

    async def _iter_async(self) -> AsyncIterator[MarketEvent]:
        async for event in self._client.stream():
            yield event
