#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from trading.rotation_universe import POOL, PORTS
from rotation_auto_coin_selector import ACTIVE_FILE, _profile_values, _set_fraction


def _unique_symbols(raw_symbols: list[str]) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    selected: list[str] = []
    invalid: list[str] = []
    for raw in raw_symbols:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if symbol not in PORTS:
            invalid.append(symbol)
            continue
        selected.append(symbol)
    return selected, invalid


def main() -> None:
    ap = argparse.ArgumentParser(description="Activate a fixed live rotation pool.")
    ap.add_argument("--symbols", nargs="+", required=True, help="Base symbols without quote, e.g. TON SOL ARB")
    ap.add_argument("--profile", default="scalp", help="Lane profile to apply before launch")
    ap.add_argument("--apply", action="store_true", help="Start/stop live lanes to match the fixed pool")
    ap.add_argument("--json", action="store_true", help="Print JSON only")
    args = ap.parse_args()

    selected, invalid = _unique_symbols(list(args.symbols))
    if invalid:
        raise SystemExit(f"unknown rotation symbols: {', '.join(invalid)}")
    if not selected:
        raise SystemExit("no valid symbols selected")

    profile = _profile_values(str(args.profile))
    fraction = 1.0 / float(len(selected))
    selected_set = set(selected)
    selected_fraction_map: dict[str, float] = {}

    for symbol in POOL:
        symbol_fraction = fraction if symbol in selected_set else 0.0
        selected_fraction_map[symbol] = symbol_fraction
        _set_fraction(symbol, symbol_fraction, profile)

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "ok": True,
        "generated_at": now_iso,
        "source": "fixed_rotation_pool",
        "selected": selected,
        "watch_symbols": selected,
        "selected_since": {symbol: now_iso for symbol in selected},
        "fraction": fraction,
        "profile": args.profile,
        "selected_fraction_map": selected_fraction_map,
        "rows": [],
        "all_rows": [],
    }
    ACTIVE_FILE.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    if args.apply:
        subprocess.check_call(
            ["python3", "scripts/rotation_apply_active_lanes.py"],
            cwd=REPO_ROOT,
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return

    print("selected:", ", ".join(selected))
    print("watch:", ", ".join(selected))
    print(f"profile: {args.profile}")
    print(f"fraction_per_active: {fraction:.6f}")
    if args.apply:
        print("applied: true")


if __name__ == "__main__":
    main()
