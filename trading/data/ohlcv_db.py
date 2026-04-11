from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class OHLCVRow:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _to_unix(ts: datetime) -> int:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.astimezone(timezone.utc).timestamp())


def _from_unix(ts_unix: int) -> datetime:
    return datetime.fromtimestamp(int(ts_unix), tz=timezone.utc)


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=2500;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
          symbol TEXT NOT NULL,
          timeframe TEXT NOT NULL,
          ts_utc TEXT NOT NULL,
          ts_unix INTEGER NOT NULL,
          open REAL NOT NULL,
          high REAL NOT NULL,
          low REAL NOT NULL,
          close REAL NOT NULL,
          volume REAL NOT NULL,
          source TEXT,
          inserted_at_unix INTEGER NOT NULL,
          PRIMARY KEY(symbol, timeframe, ts_utc)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_range ON candles(symbol, timeframe, ts_unix)")
    conn.commit()


def insert_rows(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timeframe: str,
    rows: Iterable[OHLCVRow],
    source: str = "unknown",
) -> int:
    now = int(time.time())
    payload = [
        (
            str(symbol),
            str(timeframe),
            r.ts.astimezone(timezone.utc).isoformat(),
            _to_unix(r.ts),
            float(r.open),
            float(r.high),
            float(r.low),
            float(r.close),
            float(r.volume),
            str(source),
            now,
        )
        for r in rows
    ]
    if not payload:
        return 0
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT OR IGNORE INTO candles
        (symbol, timeframe, ts_utc, ts_unix, open, high, low, close, volume, source, inserted_at_unix)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        payload,
    )
    conn.commit()
    return int(cur.rowcount or 0)


def upsert_rows(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timeframe: str,
    rows: Iterable[OHLCVRow],
    source: str = "unknown",
) -> int:
    """
    Upsert OHLCV rows. This is useful for live backfills where the last candle may update.
    Returns sqlite cursor rowcount (inserted + updated).
    """
    now = int(time.time())
    payload = [
        (
            str(symbol),
            str(timeframe),
            r.ts.astimezone(timezone.utc).isoformat(),
            _to_unix(r.ts),
            float(r.open),
            float(r.high),
            float(r.low),
            float(r.close),
            float(r.volume),
            str(source),
            now,
        )
        for r in rows
    ]
    if not payload:
        return 0
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO candles
        (symbol, timeframe, ts_utc, ts_unix, open, high, low, close, volume, source, inserted_at_unix)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol, timeframe, ts_utc) DO UPDATE SET
          open=excluded.open,
          high=excluded.high,
          low=excluded.low,
          close=excluded.close,
          volume=excluded.volume,
          source=excluded.source,
          inserted_at_unix=excluded.inserted_at_unix
        """,
        payload,
    )
    conn.commit()
    return int(cur.rowcount or 0)


def load_rows(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timeframe: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: Optional[int] = None,
    order_desc: bool = False,
) -> List[OHLCVRow]:
    where = ["symbol = ?", "timeframe = ?"]
    args: List[object] = [str(symbol), str(timeframe)]
    if start is not None:
        where.append("ts_unix >= ?")
        args.append(_to_unix(start))
    if end is not None:
        where.append("ts_unix < ?")
        args.append(_to_unix(end))
    sql = (
        "SELECT ts_unix, open, high, low, close, volume FROM candles "
        f"WHERE {' AND '.join(where)} ORDER BY ts_unix {'DESC' if order_desc else 'ASC'}"
    )
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    cur = conn.execute(sql, args)
    out: List[OHLCVRow] = []
    for ts_unix, o, h, l, c, v in cur.fetchall():
        out.append(
            OHLCVRow(
                ts=_from_unix(int(ts_unix)),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v),
            )
        )
    return out


def available_range(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    timeframe: str,
) -> Tuple[Optional[datetime], Optional[datetime], int]:
    cur = conn.execute(
        "SELECT MIN(ts_unix), MAX(ts_unix), COUNT(*) FROM candles WHERE symbol = ? AND timeframe = ?",
        (str(symbol), str(timeframe)),
    )
    row = cur.fetchone()
    if not row:
        return None, None, 0
    mn, mx, cnt = row
    if mn is None or mx is None:
        return None, None, 0
    return _from_unix(int(mn)), _from_unix(int(mx)), int(cnt or 0)
