#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.binance import trade_mirror

RELAY_PATH = REPO_ROOT / "app" / "relay-server" / "relay_server.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare local journal trade report against Binance trade mirror")
    parser.add_argument("--from-iso", default="")
    parser.add_argument("--to-iso", default="")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--write-json", default=str(REPO_ROOT / "logs" / "trade_mirror_audit.json"))
    return parser.parse_args()


def parse_iso_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_relay_module():
    spec = importlib.util.spec_from_file_location("relay_server_audit", RELAY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load relay module from {RELAY_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def keyed(items: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            out[symbol] = item
    return out


def main() -> None:
    args = parse_args()
    if args.from_iso and args.to_iso:
        from_dt = parse_iso_utc(args.from_iso)
        to_dt = parse_iso_utc(args.to_iso)
    else:
        to_dt = datetime.now(timezone.utc)
        from_dt = to_dt - timedelta(hours=max(0.1, float(args.hours)))
    if to_dt <= from_dt:
        raise SystemExit("Ungueltiges Zeitfenster.")

    symbols = [trade_mirror.normalize_usdc_symbol(item) for item in (args.symbol or []) if trade_mirror.normalize_usdc_symbol(item)]
    relay = load_relay_module()
    from_iso = from_dt.isoformat().replace("+00:00", "Z")
    to_iso = to_dt.isoformat().replace("+00:00", "Z")

    local_report = relay.collect_trades_local(from_iso, to_iso, symbols or None)
    mirror_report = trade_mirror.collect_trades_mirror(from_iso, to_iso, symbols or None)

    local_map = keyed(local_report.get("symbolSummaries") or [])
    mirror_map = keyed(mirror_report.get("symbolSummaries") or [])
    all_symbols = sorted(set(local_map) | set(mirror_map))

    mismatches: list[dict[str, object]] = []
    for symbol in all_symbols:
        local_item = local_map.get(symbol, {})
        mirror_item = mirror_map.get(symbol, {})
        local_proceeds = float(local_item.get("proceedsUsdc") or 0.0)
        mirror_proceeds = float(mirror_item.get("proceedsUsdc") or 0.0)
        local_bundles = int(local_item.get("bundleCount") or 0)
        mirror_bundles = int(mirror_item.get("bundleCount") or 0)
        if abs(local_proceeds - mirror_proceeds) <= 1e-8 and local_bundles == mirror_bundles:
            continue
        mismatches.append(
            {
                "symbol": symbol,
                "localBundleCount": local_bundles,
                "mirrorBundleCount": mirror_bundles,
                "localProceedsUsdc": round(local_proceeds, 8),
                "mirrorProceedsUsdc": round(mirror_proceeds, 8),
                "deltaProceedsUsdc": round(local_proceeds - mirror_proceeds, 8),
            }
        )

    payload = {
        "fromIso": from_iso,
        "toIso": to_iso,
        "localSource": local_report.get("source"),
        "mirrorSource": mirror_report.get("source"),
        "symbolCountLocal": len(local_map),
        "symbolCountMirror": len(mirror_map),
        "mismatchCount": len(mismatches),
        "mismatches": mismatches,
    }
    output_path = Path(args.write_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
