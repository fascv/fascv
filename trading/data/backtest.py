from __future__ import annotations

import csv
import os
from typing import Dict, Iterable, Optional

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

from trading.data.base import MarketDataSource
from trading.types import MarketEvent
from trading.utils.time import parse_ts, to_utc


class BacktestCSVDataSource(MarketDataSource):
    def __init__(self, path: str, default_micro: Optional[Dict[str, float]] = None):
        self.path = path
        self.default_micro = default_micro or {}
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    def __iter__(self) -> Iterable[MarketEvent]:
        if self.path.endswith(".parquet"):
            if pd is None:
                raise RuntimeError("pandas is required to read parquet")
            df = pd.read_parquet(self.path)
            rows = df.to_dict(orient="records")
        else:
            with open(self.path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        for row in rows:
            micro = dict(self.default_micro)
            for key in ["spread_bps", "depth", "imbalance"]:
                if key in row and row[key] not in (None, ""):
                    try:
                        micro[key] = float(row[key])
                    except Exception:
                        pass
            yield MarketEvent(
                ts=to_utc(parse_ts(str(row["timestamp"]))),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
                micro=micro,
            )
