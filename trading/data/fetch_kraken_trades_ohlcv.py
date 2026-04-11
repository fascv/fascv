from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from trading.data.ohlcv_db import OHLCVRow, connect, init_db, upsert_rows
from trading.utils.kraken import to_kraken_rest_pair
from trading.utils.time import parse_ts, to_utc


@dataclass
class CandleAcc:
    bucket_unix: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def _tf_to_seconds(timeframe: str) -> int:
    tf = str(timeframe or "").strip().lower()
    if tf.endswith("s") and tf[:-1].isdigit():
        return int(tf[:-1])
    if tf.endswith("m") and tf[:-1].isdigit():
        return int(tf[:-1]) * 60
    if tf.endswith("h") and tf[:-1].isdigit():
        return int(tf[:-1]) * 3600
    if tf.isdigit():
        return int(tf) * 60
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def _fetch_trades_page(pair_alt: str, since: int | str, timeout_sec: float) -> tuple[list[list[Any]], int]:
    q = urllib.parse.urlencode({"pair": pair_alt, "since": str(since)})
    url = f"https://api.kraken.com/0/public/Trades?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "bitcoin-md/1.0"})
    with urllib.request.urlopen(req, timeout=float(timeout_sec)) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise RuntimeError("invalid Kraken response")
    if doc.get("error"):
        raise RuntimeError(f"Kraken error: {doc.get('error')}")

    result = doc.get("result", {})
    if not isinstance(result, dict):
        raise RuntimeError("invalid Kraken result")

    trades: list[list[Any]] = []
    for k, v in result.items():
        if k == "last":
            continue
        if isinstance(v, list):
            trades = v
            break
    last = int(result.get("last") or 0)
    return trades, last


def _is_retryable_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "too many requests" in msg
        or "timed out" in msg
        or "temporarily unavailable" in msg
        or "service unavailable" in msg
    )


def _flush_rows(conn, symbol: str, timeframe: str, acc_rows: list[CandleAcc]) -> int:
    if not acc_rows:
        return 0
    rows = [
        OHLCVRow(
            ts=datetime.fromtimestamp(int(a.bucket_unix), tz=timezone.utc),
            open=float(a.open),
            high=float(a.high),
            low=float(a.low),
            close=float(a.close),
            volume=float(a.volume),
        )
        for a in acc_rows
    ]
    return upsert_rows(conn, symbol=symbol, timeframe=timeframe, rows=rows, source="kraken_trades")


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch Kraken public trades history and aggregate to OHLCV in SQLite.")
    p.add_argument("--db", "--db-path", dest="db_path", default="data/market.db")
    p.add_argument("--symbol", default="XBT/EUR")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--start", required=True, help="ISO timestamp or unix seconds")
    p.add_argument("--end", default="", help="ISO timestamp or unix seconds (exclusive). default: now")
    p.add_argument("--sleep-sec", type=float, default=0.25)
    p.add_argument("--timeout-sec", type=float, default=20.0)
    p.add_argument("--retry-max", type=int, default=12)
    p.add_argument("--retry-backoff-sec", type=float, default=1.5)
    p.add_argument("--retry-backoff-max-sec", type=float, default=45.0)
    p.add_argument("--max-pages", type=int, default=0, help="Optional safety cap for test runs")
    p.add_argument("--progress-every", type=int, default=200)
    args = p.parse_args()

    symbol = str(args.symbol)
    timeframe = str(args.timeframe)
    interval_sec = _tf_to_seconds(timeframe)

    start_dt = to_utc(parse_ts(str(args.start)))
    end_dt = to_utc(parse_ts(str(args.end))) if str(args.end).strip() else datetime.now(timezone.utc)
    start_unix = int(start_dt.timestamp())
    end_unix = int(end_dt.timestamp())

    pair_alt = to_kraken_rest_pair(symbol)
    since: int | str = int(start_unix)

    conn = connect(str(args.db_path))
    init_db(conn)

    pages = 0
    inserted_total = 0
    seen_trades = 0
    current: Optional[CandleAcc] = None

    try:
        while True:
            pages += 1
            attempt = 0
            while True:
                try:
                    trades, last = _fetch_trades_page(pair_alt, since=since, timeout_sec=float(args.timeout_sec))
                    break
                except Exception as exc:
                    if not _is_retryable_error(exc):
                        raise
                    attempt += 1
                    if attempt > int(args.retry_max):
                        raise
                    sleep_for = min(
                        float(args.retry_backoff_max_sec),
                        float(args.retry_backoff_sec) * (2 ** (attempt - 1)),
                    )
                    print(
                        json.dumps(
                            {
                                "warning": "retryable_fetch_error",
                                "attempt": attempt,
                                "since": str(since),
                                "error": str(exc),
                                "sleep_sec": sleep_for,
                            }
                        ),
                        flush=True,
                    )
                    time.sleep(max(0.0, sleep_for))
            if not trades:
                break

            rows_to_flush: list[CandleAcc] = []
            max_trade_ts = 0.0

            for t in trades:
                if not isinstance(t, list) or len(t) < 3:
                    continue
                try:
                    price = float(t[0])
                    volume = float(t[1])
                    ts = float(t[2])
                except Exception:
                    continue

                max_trade_ts = max(max_trade_ts, ts)
                ts_unix = int(ts)
                if ts_unix < start_unix:
                    continue
                if ts_unix >= end_unix:
                    continue

                bucket = (ts_unix // interval_sec) * interval_sec
                if current is None:
                    current = CandleAcc(bucket_unix=bucket, open=price, high=price, low=price, close=price, volume=volume)
                elif bucket == current.bucket_unix:
                    current.high = max(current.high, price)
                    current.low = min(current.low, price)
                    current.close = price
                    current.volume += volume
                elif bucket > current.bucket_unix:
                    rows_to_flush.append(current)
                    current = CandleAcc(bucket_unix=bucket, open=price, high=price, low=price, close=price, volume=volume)

                seen_trades += 1

            if rows_to_flush:
                inserted_total += _flush_rows(conn, symbol=symbol, timeframe=timeframe, acc_rows=rows_to_flush)

            if int(args.progress_every) > 0 and (pages % int(args.progress_every)) == 0:
                print(
                    json.dumps(
                        {
                            "pages": pages,
                            "since": str(since),
                            "last": int(last),
                            "seen_trades": seen_trades,
                            "inserted_total": inserted_total,
                            "max_trade_ts": datetime.fromtimestamp(max_trade_ts, tz=timezone.utc).isoformat() if max_trade_ts else None,
                        }
                    ),
                    flush=True,
                )

            if max_trade_ts >= float(end_unix):
                break
            if int(args.max_pages) > 0 and pages >= int(args.max_pages):
                break
            if int(last) <= int(since):
                break

            since = int(last)
            time.sleep(max(0.0, float(args.sleep_sec)))

        if current is not None and current.bucket_unix < end_unix:
            inserted_total += _flush_rows(conn, symbol=symbol, timeframe=timeframe, acc_rows=[current])

        print(
            json.dumps(
                {
                    "ok": True,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "pages": pages,
                    "seen_trades": seen_trades,
                    "inserted_total": inserted_total,
                    "pair_alt": pair_alt,
                },
                indent=2,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
