from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import ccxt  # type: ignore
import pandas as pd

from trading.data.ohlcv_db import OHLCVRow, connect, init_db, upsert_rows
from trading.utils.kraken import map_pair
from trading.utils.time import parse_ts, to_utc


def _parse_dt_day(value: str, *, end: bool) -> datetime:
    # Accept YYYY-MM-DD or ISO.
    raw = str(value or "").strip()
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        raw = raw + ("T23:59:59Z" if end else "T00:00:00Z")
    return to_utc(parse_ts(raw))


def _build_altname_map(quote: str) -> Dict[str, str]:
    ex = ccxt.kraken()
    ex.load_markets()
    mapping: Dict[str, str] = {}
    for sym, m in (ex.markets or {}).items():
        if not isinstance(m, dict):
            continue
        if m.get("quote") != quote:
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else {}
        altname = info.get("altname") if isinstance(info, dict) else None
        if altname:
            mapping[str(altname)] = str(sym)
    return mapping


def _fallback_symbol(altname: str, quote: str) -> str:
    base = altname[: -len(quote)]
    if base == "XBT":
        base = "BTC"
    if base == "XDG":
        base = "DOGE"
    return f"{base}/{quote}"


def _iter_files(z: zipfile.ZipFile, timeframe_min: str, quote: str) -> List[str]:
    """
    Kraken OHLCVT zip typically contains: master_q4/ALTNAME_5.csv (minutes).
    We accept any top-level folder, but match that pattern.
    """
    suffix = f"_{timeframe_min}.csv"
    pat = re.compile(rf"^[^/]+/([A-Z0-9]+){re.escape(suffix)}$")
    out: List[str] = []
    for name in z.namelist():
        if "__MACOSX" in name:
            continue
        m = pat.match(name)
        if not m:
            continue
        altname = m.group(1)
        if not altname.endswith(quote):
            continue
        out.append(name)
    out.sort()
    return out


def _read_chunks(handle, chunksize: int) -> Iterable[pd.DataFrame]:
    # Kraken OHLCVT exports have varied schemas over time; handle both 7 and 8 columns:
    # time, open, high, low, close, (vwap?), volume, count
    cols8 = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    cols7 = ["time", "open", "high", "low", "close", "volume", "count"]
    try:
        yield from pd.read_csv(handle, header=None, names=cols8, chunksize=chunksize)
        return
    except Exception:
        handle.seek(0)
    yield from pd.read_csv(handle, header=None, names=cols7, chunksize=chunksize)


def main() -> None:
    p = argparse.ArgumentParser(description="Import Kraken OHLCVT zip into SQLite OHLCV DB.")
    p.add_argument("--zip", dest="zip_path", required=True, help="Path to Kraken_OHLCVT.zip")
    p.add_argument("--db-path", "--db", dest="db_path", default="data/market.db")
    p.add_argument("--timeframe-min", default="5", help="Timeframe in minutes (e.g. 5)")
    p.add_argument("--quote", default="EUR", help="Quote currency filter (e.g. EUR)")
    p.add_argument("--start", required=True, help="Start date (YYYY-MM-DD or ISO)")
    p.add_argument("--end", required=True, help="End date (YYYY-MM-DD or ISO)")
    p.add_argument("--symbol", default="", help="Optional symbol filter (e.g. XBT/EUR or BTC/EUR)")
    p.add_argument("--chunksize", type=int, default=200_000)
    p.add_argument("--max-files", type=int, default=0, help="Optional cap for quick tests")
    args = p.parse_args()

    zip_path = Path(args.zip_path)
    if not zip_path.exists():
        raise SystemExit(f"Zip not found: {zip_path}")

    start_dt = _parse_dt_day(str(args.start), end=False)
    end_dt = _parse_dt_day(str(args.end), end=True)
    start_ts = int(start_dt.timestamp())
    end_ts = int(end_dt.timestamp())

    quote = str(args.quote).upper().strip()
    timeframe_min = str(args.timeframe_min).strip()
    timeframe = f"{timeframe_min}m"
    symbol_filter = map_pair(str(args.symbol).strip()) if str(args.symbol).strip() else ""

    mapping = _build_altname_map(quote)

    conn = connect(str(args.db_path))
    try:
        init_db(conn)
        with zipfile.ZipFile(zip_path) as z:
            files = _iter_files(z, timeframe_min=timeframe_min, quote=quote)
            if args.max_files and int(args.max_files) > 0:
                files = files[: int(args.max_files)]
            total = len(files)
            print(json.dumps({"zip": str(zip_path), "files": total, "quote": quote, "timeframe": timeframe}, indent=2))
            if not files:
                return

            for idx, name in enumerate(files, start=1):
                altname = Path(name).stem.split("_")[0]
                symbol = mapping.get(altname) or _fallback_symbol(altname, quote)
                symbol_norm = map_pair(symbol)
                if symbol_filter and symbol_norm != symbol_filter:
                    continue
                inserted = 0
                with z.open(name) as fh:
                    for chunk in _read_chunks(fh, chunksize=int(args.chunksize)):
                        if "time" not in chunk.columns:
                            continue
                        # Filter range
                        chunk = chunk[(chunk["time"] >= start_ts) & (chunk["time"] <= end_ts)]
                        if chunk.empty:
                            continue
                        # Map to rows
                        rows: List[OHLCVRow] = []
                        # pandas series access is much faster than iterrows
                        times = chunk["time"].astype("int64").tolist()
                        opens = chunk["open"].astype("float64").tolist()
                        highs = chunk["high"].astype("float64").tolist()
                        lows = chunk["low"].astype("float64").tolist()
                        closes = chunk["close"].astype("float64").tolist()
                        if "volume" in chunk.columns:
                            vols = chunk["volume"].astype("float64").tolist()
                        else:
                            vols = [0.0 for _ in times]
                        for t, o, h, l, c, v in zip(times, opens, highs, lows, closes, vols):
                            rows.append(
                                OHLCVRow(
                                    ts=datetime.fromtimestamp(int(t), tz=timezone.utc),
                                    open=float(o),
                                    high=float(h),
                                    low=float(l),
                                    close=float(c),
                                    volume=float(v),
                                )
                            )
                        if rows:
                            inserted += upsert_rows(
                                conn,
                                symbol=symbol,
                                timeframe=timeframe,
                                rows=rows,
                                source="kraken_ohlcvt_zip",
                            )
                print(f"[{idx}/{total}] {symbol} upserted {inserted}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
