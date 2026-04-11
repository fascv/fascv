#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


DEFAULT_SYMBOLS = ("OP", "NEAR", "ENA", "RENDER")


@dataclass
class Snapshot:
    ts: datetime
    price: float
    low: float
    high: float
    anchor: float

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def width_pct(self) -> float:
        if self.low <= 0.0:
            return 0.0
        return ((self.high / self.low) - 1.0) * 100.0

    @property
    def range_pos(self) -> float:
        width = self.high - self.low
        if width <= 0.0:
            return 0.5
        return (self.price - self.low) / width

    @property
    def outside(self) -> bool:
        return self.price < self.low or self.price > self.high


def _journal_path(symbol: str) -> Path:
    slug = symbol.strip().lower()
    return REPO_ROOT / "logs" / f"journal_live_binance_{slug}_usdc_rotation.jsonl"


def _iter_snapshots(path: Path) -> Iterable[Snapshot]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("event_type") != "core_decision":
                continue
            data = obj.get("data") or obj.get("payload") or {}
            features = data.get("features") or {}
            low = float(features.get("corridor_low_price") or 0.0)
            high = float(features.get("corridor_high_price") or 0.0)
            anchor = float(features.get("corridor_break_even_anchor_price") or 0.0)
            ready = float(features.get("corridor_ready") or 0.0)
            price = float(features.get("price") or 0.0)
            ts_raw = data.get("ts") or obj.get("ts")
            if ready <= 0.0 or low <= 0.0 or high <= low or price <= 0.0 or not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:
                continue
            yield Snapshot(ts=ts, price=price, low=low, high=high, anchor=anchor)


def _status(latest: Snapshot, first: Snapshot, outside_ratio: float) -> str:
    midpoint_drift_bps = 0.0
    if first.midpoint > 0.0:
        midpoint_drift_bps = ((latest.midpoint / first.midpoint) - 1.0) * 10000.0
    width_ratio = 1.0
    if first.width_pct > 0.0:
        width_ratio = latest.width_pct / first.width_pct

    if latest.price < latest.low * 0.995 or latest.price > latest.high * 1.005:
        return "broken"
    if outside_ratio >= 0.20:
        return "broken"
    if abs(midpoint_drift_bps) >= 300.0:
        return "shifting"
    if width_ratio <= 0.70 or width_ratio >= 1.30:
        return "shifting"
    return "stable"


def main() -> None:
    ap = argparse.ArgumentParser(description="Check corridor drift/break state for the active rotation coins.")
    ap.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS), help="Symbols without quote, e.g. OP NEAR")
    ap.add_argument("--lookback", type=int, default=120, help="How many recent core_decision snapshots to inspect")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of plain text")
    args = ap.parse_args()

    rows = []
    for symbol in args.symbols:
        path = _journal_path(symbol)
        snaps = list(_iter_snapshots(path))
        snaps = snaps[-max(1, int(args.lookback)) :]
        if not snaps:
            rows.append(
                {
                    "symbol": symbol.upper(),
                    "status": "no_data",
                    "journal": str(path.relative_to(REPO_ROOT)),
                }
            )
            continue
        first = snaps[0]
        latest = snaps[-1]
        outside_count = sum(1 for s in snaps if s.outside)
        outside_ratio = outside_count / float(len(snaps))
        midpoint_drift_bps = 0.0
        if first.midpoint > 0.0:
            midpoint_drift_bps = ((latest.midpoint / first.midpoint) - 1.0) * 10000.0
        status = _status(latest, first, outside_ratio)
        rows.append(
            {
                "symbol": symbol.upper(),
                "status": status,
                "samples": len(snaps),
                "ts": latest.ts.isoformat(),
                "price": latest.price,
                "corridor_low": latest.low,
                "corridor_high": latest.high,
                "corridor_anchor": latest.anchor,
                "width_pct": latest.width_pct,
                "range_pos_pct": latest.range_pos * 100.0,
                "outside_ratio_pct": outside_ratio * 100.0,
                "midpoint_drift_bps": midpoint_drift_bps,
                "journal": str(path.relative_to(REPO_ROOT)),
            }
        )

    if args.json:
        print(json.dumps({"ok": True, "rows": rows}, ensure_ascii=True, indent=2))
        return

    for row in rows:
        if row["status"] == "no_data":
            print(f'{row["symbol"]}: no_data ({row["journal"]})')
            continue
        print(
            f'{row["symbol"]}: {row["status"]} '
            f'price={row["price"]:.6f} '
            f'corridor=[{row["corridor_low"]:.6f}, {row["corridor_high"]:.6f}] '
            f'anchor={row["corridor_anchor"]:.6f} '
            f'width={row["width_pct"]:.2f}% '
            f'pos={row["range_pos_pct"]:.1f}% '
            f'outside={row["outside_ratio_pct"]:.1f}% '
            f'mid_drift={row["midpoint_drift_bps"]:+.1f}bps'
        )


if __name__ == "__main__":
    main()
