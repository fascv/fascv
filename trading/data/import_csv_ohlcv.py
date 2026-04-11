from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Dict

from trading.data.backtest import BacktestCSVDataSource
from trading.data.ohlcv_db import OHLCVRow, connect, init_db, insert_rows
from trading.utils.time import to_utc


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def main() -> None:
    p = argparse.ArgumentParser(description="Import OHLCV CSV (BacktestCSVDataSource format) into SQLite.")
    p.add_argument("--db-path", "--db", dest="db_path", default="data/market.db")
    p.add_argument("--csv-path", required=True)
    p.add_argument("--symbol", default="XBT/EUR")
    p.add_argument("--timeframe", default="5m")
    args = p.parse_args()

    default_micro = {}  # not stored in DB; keep DB purely OHLCV
    events = list(BacktestCSVDataSource(path=str(args.csv_path), default_micro=default_micro))
    rows = [
        OHLCVRow(
            ts=to_utc(e.ts),
            open=float(e.open),
            high=float(e.high),
            low=float(e.low),
            close=float(e.close),
            volume=float(e.volume),
        )
        for e in events
    ]

    conn = connect(str(args.db_path))
    try:
        init_db(conn)
        inserted = insert_rows(
            conn,
            symbol=str(args.symbol),
            timeframe=str(args.timeframe),
            rows=rows,
            source="csv_import",
        )
    finally:
        conn.close()

    if rows:
        mn = min(r.ts for r in rows)
        mx = max(r.ts for r in rows)
    else:
        mn = mx = datetime.now(timezone.utc)
    print(
        json.dumps(
            {
                "ok": True,
                "db_path": str(args.db_path),
                "csv_path": str(args.csv_path),
                "symbol": str(args.symbol),
                "timeframe": str(args.timeframe),
                "rows": len(rows),
                "inserted": inserted,
                "min_ts": mn.isoformat(),
                "max_ts": mx.isoformat(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
