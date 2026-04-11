#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def _date_days_before(end_day: date, days: int) -> str:
    dt = end_day - timedelta(days=max(1, int(days)))
    return dt.isoformat()


def _symbol_slug(symbol: str) -> str:
    return str(symbol or "").strip().replace("/", "_").replace("-", "_").lower()


def _to_storage_pair(symbol: str) -> str:
    raw = str(symbol or "").strip().upper().replace("-", "").replace("_", "")
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI", "EUR", "USD", "BTC", "ETH"):
        if raw.endswith(quote) and len(raw) > len(quote):
            return f"{raw[:-len(quote)]}/{quote}"
    return raw


def _load_symbols(report_path: Path, list_key: str, limit: int) -> List[str]:
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    rows = doc.get(list_key)
    if not isinstance(rows, list):
        raise SystemExit(f"missing or invalid list key in report: {list_key}")
    out: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        if not sym:
            continue
        out.append(sym)
        if len(out) >= max(1, int(limit)):
            break
    if not out:
        raise SystemExit(f"no symbols found in report key: {list_key}")
    return out


def _run(cmd: List[str], *, dry_run: bool) -> None:
    print(json.dumps({"run": cmd}, ensure_ascii=True))
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def _has_any_candles(db_path: str, symbol: str, timeframe: str) -> bool:
    path = Path(str(db_path))
    if not path.exists():
        return False
    con = sqlite3.connect(str(path))
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol = ? AND timeframe = ?",
            (str(symbol), str(timeframe)),
        )
        row = cur.fetchone()
        return bool(row and int(row[0] or 0) > 0)
    except sqlite3.Error:
        return False
    finally:
        con.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Fetch Binance history and run professional walk-forward optimization for "
            "a scanner-derived shortlist."
        )
    )
    p.add_argument("--report", default="reports/binance_universe_strength_scan.json")
    p.add_argument("--list-key", default="active_top_ranked", help="e.g. active_top_ranked or stage2_top_ranked")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--db-path", default="data/market.binance.db")
    p.add_argument("--config", default="configs/sim_binance_professional.yaml")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--lookback-days", type=int, default=730)
    p.add_argument("--end", default="", help="Exclusive end date (YYYY-MM-DD). Default: today UTC.")
    p.add_argument("--trials", type=int, default=120)
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--families", default="common8")
    p.add_argument("--max-events", type=int, default=70000)
    p.add_argument("--min-oos-trades", type=int, default=10)
    p.add_argument("--selection-candidates", type=int, default=30)
    p.add_argument("--out-dir-configs", default="configs/binance_shortlist")
    p.add_argument("--out-dir-reports", default="reports/binance_shortlist")
    p.add_argument("--summary-json", default="reports/binance_shortlist/summary.json")
    p.add_argument("--skip-fetch", action="store_true")
    p.add_argument("--force-fetch", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    report_path = Path(str(args.report))
    symbols = _load_symbols(report_path, str(args.list_key), int(args.limit))

    out_cfg_dir = Path(str(args.out_dir_configs))
    out_rep_dir = Path(str(args.out_dir_reports))
    out_cfg_dir.mkdir(parents=True, exist_ok=True)
    out_rep_dir.mkdir(parents=True, exist_ok=True)

    end_day = datetime.now(timezone.utc).date()
    if str(args.end).strip():
        end_day = date.fromisoformat(str(args.end).strip())
    end_date = end_day.isoformat()
    start_date = _date_days_before(end_day, int(args.lookback_days))
    py = sys.executable or "python3"

    summary: List[Dict[str, Any]] = []
    for idx, symbol in enumerate(symbols, start=1):
        slug = _symbol_slug(symbol)
        storage_pair = _to_storage_pair(symbol)
        print(json.dumps({"stage": "symbol_start", "index": idx, "total": len(symbols), "symbol": symbol}, ensure_ascii=True))
        fetched = False
        fetch_skipped_existing = False
        if not bool(args.skip_fetch):
            if not bool(args.force_fetch) and _has_any_candles(str(args.db_path), storage_pair, str(args.timeframe)):
                fetch_skipped_existing = True
            else:
                fetched = True
        if fetched:
            fetch_cmd = [
                py,
                str(SCRIPT_DIR / "fetch_binance_klines.py"),
                "--db",
                str(args.db_path),
                "--symbol",
                symbol,
                "--timeframe",
                str(args.timeframe),
                "--start",
                start_date,
                "--end",
                end_date,
            ]
            _run(fetch_cmd, dry_run=bool(args.dry_run))

        out_yaml = out_cfg_dir / f"sim_optimized_{slug}_{args.timeframe}.yaml"
        out_json = out_rep_dir / f"sim_opt_report_{slug}_{args.timeframe}.json"
        opt_cmd = [
            py,
            str(SCRIPT_DIR / "optimize_sim_professional.py"),
            "--config",
            str(args.config),
            "--db-path",
            str(args.db_path),
            "--symbol",
            storage_pair,
            "--timeframe",
            str(args.timeframe),
            "--max-events",
            str(int(args.max_events)),
            "--families",
            str(args.families),
            "--trials",
            str(int(args.trials)),
            "--selection-candidates",
            str(int(args.selection_candidates)),
            "--jobs",
            str(int(args.jobs)),
            "--min-oos-trades",
            str(int(args.min_oos_trades)),
            "--enforce-positive-oos",
            "--out-yaml",
            str(out_yaml),
            "--out-json",
            str(out_json),
        ]
        _run(opt_cmd, dry_run=bool(args.dry_run))

        summary.append(
            {
                "symbol": symbol,
                "storage_pair": storage_pair,
                "out_yaml": str(out_yaml),
                "out_json": str(out_json),
                "fetched": fetched and not bool(args.dry_run),
                "fetch_skipped_existing": bool(fetch_skipped_existing),
                "optimized": not bool(args.dry_run),
            }
        )

    summary_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report": str(report_path),
        "list_key": str(args.list_key),
        "symbols": symbols,
        "config": str(args.config),
        "timeframe": str(args.timeframe),
        "start": start_date,
        "end": end_date,
        "trials": int(args.trials),
        "jobs": int(args.jobs),
        "families": str(args.families),
        "items": summary,
    }
    summary_path = Path(str(args.summary_json))
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary_doc, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "summary_json": str(summary_path), "symbols": symbols}, ensure_ascii=True))


if __name__ == "__main__":
    main()
