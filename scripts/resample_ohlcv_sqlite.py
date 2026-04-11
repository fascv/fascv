#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from trading.data.ohlcv_db import OHLCVRow, connect, init_db, upsert_rows
from trading.utils.time import parse_ts, to_utc


def _tf_to_seconds(timeframe: str) -> int:
    tf = str(timeframe or "").strip().lower()
    if tf.endswith("s") and tf[:-1].isdigit():
        return int(tf[:-1])
    if tf.endswith("m") and tf[:-1].isdigit():
        return int(tf[:-1]) * 60
    if tf.endswith("h") and tf[:-1].isdigit():
        return int(tf[:-1]) * 3600
    if tf.endswith("d") and tf[:-1].isdigit():
        return int(tf[:-1]) * 86400
    if tf.isdigit():
        return int(tf) * 60
    raise ValueError(f"unsupported timeframe: {timeframe}")


def _parse_dt(raw: str) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    return to_utc(parse_ts(s))


@dataclass
class Acc:
    bucket: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def main() -> None:
    p = argparse.ArgumentParser(description="Resample existing SQLite OHLCV candles to a higher timeframe.")
    p.add_argument("--db", "--db-path", dest="db_path", default="data/market_robust.db")
    p.add_argument("--symbol", default="XBT/EUR")
    p.add_argument("--from-timeframe", default="5m")
    p.add_argument("--to-timeframe", default="15m")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--source-tag", default="resample")
    p.add_argument("--batch-size", type=int, default=20000)
    args = p.parse_args()

    src_tf = str(args.from_timeframe).strip()
    dst_tf = str(args.to_timeframe).strip()
    symbol = str(args.symbol).strip()

    src_sec = _tf_to_seconds(src_tf)
    dst_sec = _tf_to_seconds(dst_tf)
    if dst_sec <= src_sec:
        raise SystemExit(f"to-timeframe ({dst_tf}) must be greater than from-timeframe ({src_tf})")
    if dst_sec % src_sec != 0:
        raise SystemExit(f"to-timeframe ({dst_tf}) must be a multiple of from-timeframe ({src_tf})")

    start = _parse_dt(args.start)
    end = _parse_dt(args.end)
    start_unix = int(start.timestamp()) if start is not None else None
    end_unix = int(end.timestamp()) if end is not None else None

    conn = connect(str(args.db_path))
    init_db(conn)
    try:
        where = ["symbol = ?", "timeframe = ?"]
        sql_args: List[Any] = [symbol, src_tf]
        if start_unix is not None:
            where.append("ts_unix >= ?")
            sql_args.append(int(start_unix))
        if end_unix is not None:
            where.append("ts_unix < ?")
            sql_args.append(int(end_unix))
        sql = (
            "SELECT ts_unix, open, high, low, close, volume FROM candles "
            f"WHERE {' AND '.join(where)} ORDER BY ts_unix ASC"
        )
        cur = conn.execute(sql, sql_args)

        out_rows: List[OHLCVRow] = []
        current: Optional[Acc] = None
        source_count = 0
        for ts_unix, o, h, l, c, v in cur:
            source_count += 1
            ts = int(ts_unix)
            bucket = (ts // dst_sec) * dst_sec
            op = float(o)
            hi = float(h)
            lo = float(l)
            cl = float(c)
            vol = float(v)

            if current is None:
                current = Acc(bucket=bucket, open=op, high=hi, low=lo, close=cl, volume=vol)
                continue

            if bucket == current.bucket:
                current.high = max(current.high, hi)
                current.low = min(current.low, lo)
                current.close = cl
                current.volume += vol
                continue

            out_rows.append(
                OHLCVRow(
                    ts=datetime.fromtimestamp(int(current.bucket), tz=timezone.utc),
                    open=float(current.open),
                    high=float(current.high),
                    low=float(current.low),
                    close=float(current.close),
                    volume=float(current.volume),
                )
            )
            current = Acc(bucket=bucket, open=op, high=hi, low=lo, close=cl, volume=vol)

            if len(out_rows) >= max(1, int(args.batch_size)):
                upsert_rows(
                    conn,
                    symbol=symbol,
                    timeframe=dst_tf,
                    rows=out_rows,
                    source=f"{args.source_tag}:{src_tf}->{dst_tf}",
                )
                out_rows = []

        if current is not None:
            out_rows.append(
                OHLCVRow(
                    ts=datetime.fromtimestamp(int(current.bucket), tz=timezone.utc),
                    open=float(current.open),
                    high=float(current.high),
                    low=float(current.low),
                    close=float(current.close),
                    volume=float(current.volume),
                )
            )
        inserted = 0
        if out_rows:
            inserted = upsert_rows(
                conn,
                symbol=symbol,
                timeframe=dst_tf,
                rows=out_rows,
                source=f"{args.source_tag}:{src_tf}->{dst_tf}",
            )

        cnt_row = conn.execute(
            "SELECT COUNT(*), MIN(ts_unix), MAX(ts_unix) FROM candles WHERE symbol = ? AND timeframe = ?",
            (symbol, dst_tf),
        ).fetchone()
        cnt = int(cnt_row[0] or 0) if cnt_row else 0
        mn = int(cnt_row[1]) if cnt_row and cnt_row[1] is not None else None
        mx = int(cnt_row[2]) if cnt_row and cnt_row[2] is not None else None

        print(
            json.dumps(
                {
                    "ok": True,
                    "symbol": symbol,
                    "from_timeframe": src_tf,
                    "to_timeframe": dst_tf,
                    "source_rows_scanned": source_count,
                    "last_batch_upserted": inserted,
                    "dst_count": cnt,
                    "dst_min_ts": datetime.fromtimestamp(mn, tz=timezone.utc).isoformat() if mn is not None else None,
                    "dst_max_ts": datetime.fromtimestamp(mx, tz=timezone.utc).isoformat() if mx is not None else None,
                },
                indent=2,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
