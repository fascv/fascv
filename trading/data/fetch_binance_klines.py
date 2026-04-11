from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from trading.data.ohlcv_db import OHLCVRow, available_range, connect, init_db, upsert_rows
from trading.utils.time import parse_ts, to_utc


_INTERVALS: Dict[str, Tuple[str, int]] = {
    "1m": ("1m", 60),
    "3m": ("3m", 180),
    "5m": ("5m", 300),
    "15m": ("15m", 900),
    "30m": ("30m", 1800),
    "1h": ("1h", 3600),
    "2h": ("2h", 7200),
    "4h": ("4h", 14400),
    "6h": ("6h", 21600),
    "8h": ("8h", 28800),
    "12h": ("12h", 43200),
    "1d": ("1d", 86400),
    "3d": ("3d", 259200),
    "1w": ("1w", 604800),
    "1M": ("1M", 2592000),
}


def _normalize_symbol(symbol: str) -> tuple[str, str]:
    raw = str(symbol or "ETH/USDT").strip().upper()
    if "/" in raw:
        base, quote = raw.split("/", 1)
    elif "-" in raw:
        base, quote = raw.split("-", 1)
    else:
        token = raw.replace("_", "")
        for q in ("USDT", "USDC", "BUSD", "FDUSD", "EUR", "USD", "BTC", "ETH"):
            if token.endswith(q) and len(token) > len(q):
                base = token[: -len(q)]
                quote = q
                break
        else:
            # Fallback: keep token as-is, assume already compact pair.
            return token, token
    base = base.strip().upper()
    quote = quote.strip().upper()
    return f"{base}{quote}", f"{base}/{quote}"


def _interval_spec(timeframe: str) -> tuple[str, int]:
    tf = str(timeframe or "").strip()
    if tf in _INTERVALS:
        return _INTERVALS[tf]
    low = tf.lower()
    if low in _INTERVALS:
        return _INTERVALS[low]
    raise ValueError(f"unsupported timeframe for binance klines: {timeframe}")


def _to_ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


def _fetch_klines(
    *,
    symbol: str,
    interval: str,
    start_ms: Optional[int],
    end_ms: Optional[int],
    limit: int,
    base_url: str,
    timeout_sec: float,
) -> tuple[list[OHLCVRow], Optional[int], Dict[str, Any]]:
    url = base_url.rstrip("/") + "/api/v3/klines"
    params: Dict[str, Any] = {
        "symbol": symbol,
        "interval": interval,
        "limit": int(limit),
    }
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)

    resp = requests.get(url, params=params, timeout=float(timeout_sec))
    resp.raise_for_status()
    doc = resp.json()
    if not isinstance(doc, list):
        raise RuntimeError(f"invalid binance klines response type: {type(doc)!r}")

    rows: list[OHLCVRow] = []
    last_open_ms: Optional[int] = None
    for item in doc:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            open_ms = int(item[0])
            o = float(item[1])
            h = float(item[2])
            l = float(item[3])
            c = float(item[4])
            v = float(item[5])
        except Exception:
            continue
        ts = datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc)
        rows.append(OHLCVRow(ts=ts, open=o, high=h, low=l, close=c, volume=v))
        last_open_ms = open_ms

    meta = {
        "url": url,
        "symbol": symbol,
        "interval": interval,
        "count": len(rows),
        "start_ms": start_ms,
        "end_ms": end_ms,
    }
    return rows, last_open_ms, meta


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch Binance klines and store to SQLite candles DB.")
    p.add_argument("--db-path", "--db", dest="db_path", default="data/market.db")
    p.add_argument("--symbol", default="ETH/USDT", help="e.g. ETH/USDT or ETHUSDT")
    p.add_argument("--timeframe", default="15m", help="e.g. 1m,5m,15m,1h,1d")
    p.add_argument("--start", default=None, help="ISO timestamp or unix seconds")
    p.add_argument("--end", default=None, help="ISO timestamp or unix seconds (exclusive)")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--sleep-sec", type=float, default=0.12)
    p.add_argument("--timeout-sec", type=float, default=10.0)
    p.add_argument("--base-url", default="https://api.binance.com")
    args = p.parse_args()

    rest_symbol, storage_symbol = _normalize_symbol(str(args.symbol))
    interval, interval_sec = _interval_spec(str(args.timeframe))
    start_dt = to_utc(parse_ts(args.start)) if args.start else None
    end_dt = to_utc(parse_ts(args.end)) if args.end else None
    start_ms = _to_ms(start_dt) if start_dt else None
    end_ms = _to_ms(end_dt) if end_dt else None

    conn = connect(str(args.db_path))
    try:
        init_db(conn)
        mn, mx, cnt = available_range(conn, symbol=storage_symbol, timeframe=str(args.timeframe))
        print(
            json.dumps(
                {
                    "db": str(args.db_path),
                    "symbol": storage_symbol,
                    "timeframe": str(args.timeframe),
                    "have": {"min": str(mn), "max": str(mx), "count": cnt},
                },
                ensure_ascii=True,
                indent=2,
            )
        )

        inserted_total = 0
        loops = 0
        cursor_ms = start_ms

        while True:
            loops += 1
            rows, last_open_ms, meta = _fetch_klines(
                symbol=rest_symbol,
                interval=interval,
                start_ms=cursor_ms,
                end_ms=end_ms,
                limit=max(1, min(int(args.limit), 1000)),
                base_url=str(args.base_url),
                timeout_sec=float(args.timeout_sec),
            )
            if not rows:
                print(json.dumps({"ok": True, "done": True, "reason": "no_rows", "meta": meta}, ensure_ascii=True))
                break

            if end_ms is not None:
                rows = [r for r in rows if _to_ms(r.ts) < end_ms]
                if not rows:
                    print(json.dumps({"ok": True, "done": True, "reason": "reached_end", "meta": meta}, ensure_ascii=True))
                    break

            ins = upsert_rows(conn, symbol=storage_symbol, timeframe=str(args.timeframe), rows=rows, source="binance_klines")
            inserted_total += ins

            max_ts_ms = max(_to_ms(r.ts) for r in rows)
            print(
                json.dumps(
                    {
                        "loop": loops,
                        "fetched": len(rows),
                        "inserted": ins,
                        "max_ts_ms": max_ts_ms,
                        "max_ts": datetime.fromtimestamp(max_ts_ms / 1000.0, tz=timezone.utc).isoformat(),
                    },
                    ensure_ascii=True,
                )
            )

            if last_open_ms is None:
                break

            next_ms = int(last_open_ms + interval_sec * 1000)
            if cursor_ms is not None and next_ms <= cursor_ms:
                break
            cursor_ms = next_ms

            if end_ms is not None and cursor_ms >= end_ms:
                break

            if len(rows) < max(1, min(int(args.limit), 1000)):
                # At tail of available history.
                break

            time.sleep(max(0.0, float(args.sleep_sec)))

        print(json.dumps({"ok": True, "inserted_total": inserted_total}, ensure_ascii=True, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
