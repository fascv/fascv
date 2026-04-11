#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.binance.rest import BinanceRestClient
from trading.binance.trade_mirror import normalize_usdc_symbol, sync_symbol_window
from trading.utils.env import load_env

DEFAULT_ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
LOGS_DIR = REPO_ROOT / "logs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Binance myTrades into the local trade mirror")
    parser.add_argument("--env", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--active-file", default=str(DEFAULT_ACTIVE_FILE))
    parser.add_argument("--hours", type=float, default=72.0)
    parser.add_argument("--from-iso", default="")
    parser.add_argument("--to-iso", default="")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--selected", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--journals", action="store_true", help="Use symbols seen in local rotation journals")
    parser.add_argument("--all-active", action="store_true", help="Use selected + watch symbols")
    return parser.parse_args()


def parse_iso_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_active_symbols(path: Path) -> tuple[list[str], list[str]]:
    if not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], []
    selected = [normalize_usdc_symbol(item) for item in (payload.get("selected") or [])]
    watch = [normalize_usdc_symbol(item) for item in (payload.get("watch_symbols") or [])]
    return [item for item in selected if item], [item for item in watch if item]


def load_journal_symbols(log_dir: Path) -> list[str]:
    symbols: list[str] = []
    for path in sorted(log_dir.glob("journal_live_binance_*_usdc_rotation.jsonl")):
        stem = path.stem
        prefix = "journal_live_binance_"
        if not stem.startswith(prefix) or not stem.endswith("_usdc_rotation"):
            continue
        base = stem[len(prefix) : -len("_usdc_rotation")].strip("_")
        symbol = normalize_usdc_symbol(base)
        if symbol:
            symbols.append(symbol)
    return symbols


def resolve_symbols(args: argparse.Namespace) -> list[str]:
    symbols = [normalize_usdc_symbol(item) for item in (args.symbol or [])]
    selected, watch = load_active_symbols(Path(args.active_file))
    if args.all_active:
        args.selected = True
        args.watch = True
    if args.selected:
        symbols.extend(selected)
    if args.watch:
        symbols.extend(watch)
    if args.journals:
        symbols.extend(load_journal_symbols(LOGS_DIR))
    unique: list[str] = []
    seen: set[str] = set()
    for item in symbols:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def main() -> None:
    args = parse_args()
    load_env(args.env)

    import os

    api_key = str(os.getenv("BINANCE_API_KEY", "") or "").strip()
    api_secret = str(os.getenv("BINANCE_API_SECRET", "") or "").strip()
    base_url = str(os.getenv("BINANCE_BASE_URL", "https://api.binance.com") or "").strip()
    if not api_key or not api_secret:
        raise SystemExit("BINANCE_API_KEY / BINANCE_API_SECRET fehlen.")

    if args.from_iso and args.to_iso:
        start_dt = parse_iso_utc(args.from_iso)
        end_dt = parse_iso_utc(args.to_iso)
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(hours=max(0.1, float(args.hours)))
    if end_dt <= start_dt:
        raise SystemExit("Ungueltiges Zeitfenster.")

    symbols = resolve_symbols(args)
    if not symbols:
        raise SystemExit("Keine Symbole ausgewaehlt. Nutze --symbol oder --selected/--watch/--all-active.")

    client = BinanceRestClient(api_key=api_key, api_secret=api_secret, base_url=base_url, symbol=symbols[0])
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    results: list[dict[str, object]] = []
    for symbol in symbols:
        results.append(sync_symbol_window(client, symbol=symbol, start_ms=start_ms, end_ms=end_ms))

    payload = {
        "fromIso": start_dt.isoformat().replace("+00:00", "Z"),
        "toIso": end_dt.isoformat().replace("+00:00", "Z"),
        "symbolCount": len(symbols),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
