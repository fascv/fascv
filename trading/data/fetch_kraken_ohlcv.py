from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from trading.data.ohlcv_db import OHLCVRow, available_range, connect, init_db, upsert_rows
from trading.utils.kraken import to_kraken_rest_pair
from trading.utils.time import parse_ts, to_utc


def _interval_minutes(timeframe: str) -> int:
    tf = str(timeframe or "").strip().lower()
    if tf.endswith("m"):
        return int(tf[:-1])
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    if tf.isdigit():
        # assume minutes
        return int(tf)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def _fetch_ohlc(
    *,
    pair: str,
    interval_min: int,
    since: Optional[int],
    base_url: str = "https://api.kraken.com/0/public/OHLC",
    timeout_sec: float = 10.0,
) -> Tuple[List[OHLCVRow], Optional[int], Dict[str, Any]]:
    params: Dict[str, Any] = {"pair": pair, "interval": int(interval_min)}
    if since is not None and since > 0:
        params["since"] = int(since)
    url = base_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "bitcoin-md/1.0"})
    with urllib.request.urlopen(req, timeout=float(timeout_sec)) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise RuntimeError("Invalid Kraken response")
    if doc.get("error"):
        raise RuntimeError(f"Kraken error: {doc.get('error')}")
    result = doc.get("result", {})
    if not isinstance(result, dict):
        raise RuntimeError("Kraken result missing")

    last = result.get("last")
    last_i: Optional[int] = None
    try:
        if last is not None:
            last_i = int(last)
    except Exception:
        last_i = None

    series = None
    series_key = None
    for k, v in result.items():
        if k == "last":
            continue
        if isinstance(v, list):
            series = v
            series_key = k
            break
    if series is None:
        return [], last_i, {"url": url, "pair_key": series_key}

    rows: List[OHLCVRow] = []
    for item in series:
        if not isinstance(item, list) or len(item) < 8:
            continue
        try:
            ts = datetime.fromtimestamp(float(item[0]), tz=timezone.utc)
            o = float(item[1])
            h = float(item[2])
            l = float(item[3])
            c = float(item[4])
            v = float(item[6])  # volume
        except Exception:
            continue
        rows.append(OHLCVRow(ts=ts, open=o, high=h, low=l, close=c, volume=v))

    # Kraken includes the current in-progress candle. Keep it; consumers can decide.
    meta = {"url": url, "pair_key": series_key, "count": len(rows)}
    return rows, last_i, meta


def _to_unix(ts: datetime) -> int:
    return int(to_utc(ts).timestamp())


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch Kraken public OHLCV and store into SQLite.")
    p.add_argument("--db-path", "--db", dest="db_path", default="data/market.db")
    p.add_argument("--symbol", default="XBT/EUR", help="Human symbol like XBT/EUR or BTC/EUR")
    p.add_argument("--timeframe", default="5m", help="e.g. 1m,5m,15m,1h")
    p.add_argument("--start", default=None, help="ISO timestamp or unix seconds")
    p.add_argument("--end", default=None, help="ISO timestamp or unix seconds (exclusive)")
    p.add_argument("--sleep-sec", type=float, default=1.0)
    p.add_argument("--timeout-sec", type=float, default=10.0)
    args = p.parse_args()

    db_path = str(args.db_path)
    symbol = str(args.symbol)
    timeframe = str(args.timeframe)
    interval_min = _interval_minutes(timeframe)

    start_dt = to_utc(parse_ts(args.start)) if args.start else None
    end_dt = to_utc(parse_ts(args.end)) if args.end else None

    pair = to_kraken_rest_pair(symbol)

    conn = connect(db_path)
    try:
        init_db(conn)
        mn, mx, cnt = available_range(conn, symbol=symbol, timeframe=timeframe)
        print(json.dumps({"db": db_path, "symbol": symbol, "timeframe": timeframe, "have": {"min": str(mn), "max": str(mx), "count": cnt}}, indent=2))

        since = _to_unix(start_dt) if start_dt else None
        end_unix = _to_unix(end_dt) if end_dt else None

        inserted_total = 0
        loops = 0
        last_seen: Optional[int] = None

        while True:
            loops += 1
            rows, last, meta = _fetch_ohlc(pair=pair, interval_min=interval_min, since=since, timeout_sec=float(args.timeout_sec))
            if not rows:
                print(json.dumps({"ok": True, "done": True, "reason": "no_rows", "meta": meta}, indent=2))
                break

            if start_dt is not None:
                available_first = min(_to_unix(r.ts) for r in rows)
                if available_first > _to_unix(start_dt):
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "warning": "kraken_ohlc_returns_recent_window_only",
                                "requested_start": start_dt.isoformat(),
                                "available_first_ts": datetime.fromtimestamp(available_first, tz=timezone.utc).isoformat(),
                                "meta": meta,
                            },
                            indent=2,
                        )
                    )

            if end_unix is not None:
                rows = [r for r in rows if _to_unix(r.ts) < end_unix]
                if not rows:
                    print(json.dumps({"ok": True, "done": True, "reason": "reached_end", "meta": meta}, indent=2))
                    break

            ins = upsert_rows(conn, symbol=symbol, timeframe=timeframe, rows=rows, source="kraken_public_ohlc")
            inserted_total += ins

            max_ts = max(_to_unix(r.ts) for r in rows)
            progress = {"loop": loops, "fetched": len(rows), "inserted": ins, "max_ts": max_ts, "last": last, "meta": meta}
            print(json.dumps(progress, indent=2))

            # Advance since (avoid tight loops if Kraken returns same window).
            if last is not None and (last_seen is None or last > last_seen):
                since = last
                last_seen = last
            else:
                since = max_ts + interval_min * 60

            if end_unix is not None and since >= end_unix:
                break
            time.sleep(max(0.0, float(args.sleep_sec)))

        print(json.dumps({"ok": True, "inserted_total": inserted_total}, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
