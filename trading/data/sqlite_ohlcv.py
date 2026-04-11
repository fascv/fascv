from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, Optional

from trading.data.base import MarketDataSource
from trading.data.ohlcv_db import connect, init_db, load_rows
from trading.types import MarketEvent
from trading.utils.time import parse_ts, to_utc


def _parse_iso_dt(raw: str | None) -> Optional[datetime]:
    if not raw:
        return None
    return to_utc(parse_ts(str(raw)))


class SQLiteOHLCVDataSource(MarketDataSource):
    """
    Read-only MarketDataSource backed by a SQLite OHLCV table.
    """

    def __init__(
        self,
        *,
        db_path: str,
        symbol: str,
        timeframe: str,
        default_micro: Optional[Dict[str, float]] = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        latest: bool = False,
    ):
        self.db_path = str(db_path)
        self.symbol = str(symbol)
        self.timeframe = str(timeframe)
        self.default_micro = default_micro or {}
        self.start = _parse_iso_dt(start)
        self.end = _parse_iso_dt(end)
        self.limit = int(limit) if limit is not None else None
        self.latest = bool(latest)

    def __iter__(self) -> Iterable[MarketEvent]:
        conn = connect(self.db_path)
        try:
            init_db(conn)
            rows = load_rows(
                conn,
                symbol=self.symbol,
                timeframe=self.timeframe,
                start=self.start,
                end=self.end,
                limit=self.limit,
                order_desc=self.latest,
            )
        finally:
            conn.close()

        if self.latest:
            rows = list(reversed(rows))
        for r in rows:
            yield MarketEvent(
                ts=r.ts,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                micro=dict(self.default_micro),
            )
