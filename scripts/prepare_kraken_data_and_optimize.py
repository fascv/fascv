#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from trading.alpha.training import make_walk_forward_splits
from trading.config import load_config
from trading.utils.time import parse_ts, to_utc


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _parse_utc(raw: str | None) -> Optional[datetime]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return to_utc(parse_ts(text))


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timeframe_seconds(timeframe: str) -> int:
    tf = str(timeframe or "").strip().lower()
    if tf.endswith("s") and tf[:-1].isdigit():
        return int(tf[:-1])
    if tf.endswith("m") and tf[:-1].isdigit():
        return int(tf[:-1]) * 60
    if tf.endswith("h") and tf[:-1].isdigit():
        return int(tf[:-1]) * 3600
    if tf.endswith("d") and tf[:-1].isdigit():
        return int(tf[:-1]) * 86400
    if tf.isdigit():
        return int(tf) * 60
    raise ValueError(f"unsupported timeframe: {timeframe}")


def _run(cmd: List[str], *, cwd: Path) -> None:
    print(json.dumps({"run": cmd}, ensure_ascii=True), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


@dataclass
class DataProfile:
    symbol: str
    timeframe: str
    interval_sec: int
    count: int
    min_ts: Optional[datetime]
    max_ts: Optional[datetime]
    expected_bars: int
    missing_bars: int
    coverage_ratio: float
    max_gap_bars: int

    def as_dict(self) -> Dict[str, Any]:
        span_days = 0.0
        if self.min_ts and self.max_ts:
            span_days = max(0.0, (self.max_ts - self.min_ts).total_seconds() / 86400.0)
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "interval_sec": self.interval_sec,
            "count": self.count,
            "min_ts": _iso_utc(self.min_ts) if self.min_ts else None,
            "max_ts": _iso_utc(self.max_ts) if self.max_ts else None,
            "span_days": round(span_days, 3),
            "expected_bars": self.expected_bars,
            "missing_bars": self.missing_bars,
            "coverage_ratio": round(self.coverage_ratio, 6),
            "max_gap_bars": self.max_gap_bars,
        }


def _profile_candles(
    *,
    db_path: Path,
    symbol: str,
    timeframe: str,
    start: Optional[datetime],
    end: Optional[datetime],
) -> DataProfile:
    interval_sec = _timeframe_seconds(timeframe)
    sql = "SELECT ts_unix FROM candles WHERE symbol = ? AND timeframe = ?"
    args: List[Any] = [symbol, timeframe]
    if start is not None:
        sql += " AND ts_unix >= ?"
        args.append(int(start.timestamp()))
    if end is not None:
        sql += " AND ts_unix < ?"
        args.append(int(end.timestamp()))
    sql += " ORDER BY ts_unix ASC"

    conn = sqlite3.connect(str(db_path))
    try:
        try:
            rows = [int(r[0]) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                rows = []
            else:
                raise
    finally:
        conn.close()

    if not rows:
        return DataProfile(
            symbol=symbol,
            timeframe=timeframe,
            interval_sec=interval_sec,
            count=0,
            min_ts=None,
            max_ts=None,
            expected_bars=0,
            missing_bars=0,
            coverage_ratio=0.0,
            max_gap_bars=0,
        )

    mn = rows[0]
    mx = rows[-1]
    expected = int((mx - mn) // interval_sec) + 1
    missing = 0
    max_gap_bars = 1
    prev = rows[0]
    for ts in rows[1:]:
        if ts <= prev:
            continue
        bars = int((ts - prev) // interval_sec)
        if bars > 1:
            missing += bars - 1
            if bars > max_gap_bars:
                max_gap_bars = bars
        prev = ts

    coverage = (float(len(rows)) / float(expected)) if expected > 0 else 0.0
    return DataProfile(
        symbol=symbol,
        timeframe=timeframe,
        interval_sec=interval_sec,
        count=len(rows),
        min_ts=datetime.fromtimestamp(mn, tz=timezone.utc),
        max_ts=datetime.fromtimestamp(mx, tz=timezone.utc),
        expected_bars=expected,
        missing_bars=missing,
        coverage_ratio=coverage,
        max_gap_bars=max_gap_bars,
    )


@dataclass
class WFWindow:
    train_size: int
    val_size: int
    test_size: int
    purge: int
    splits: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "train_size": self.train_size,
            "val_size": self.val_size,
            "test_size": self.test_size,
            "purge": self.purge,
            "splits": self.splits,
        }


def _derive_wf_window(
    *,
    n_search: int,
    target_splits: int,
    train_ratio: int,
    val_ratio: int,
    min_train: int,
    min_val: int,
    min_test: int,
    purge_pct: float,
) -> WFWindow:
    target_splits = max(1, int(target_splits))
    train_ratio = max(1, int(train_ratio))
    val_ratio = max(1, int(val_ratio))
    min_train = max(1, int(min_train))
    min_val = max(1, int(min_val))
    min_test = max(1, int(min_test))

    best: Optional[WFWindow] = None

    for wanted in range(target_splits, 0, -1):
        purge = max(1, int(n_search * float(purge_pct)))
        # keep purge bounded relative to test-size later
        denom = wanted * (train_ratio + val_ratio) + 1
        usable = n_search - max(0, (wanted - 1) * 2 * purge)
        if usable <= 0:
            continue
        unit = usable // denom
        if unit <= 0:
            continue

        train = train_ratio * unit
        val = val_ratio * unit
        test = unit

        if train < min_train or val < min_val or test < min_test:
            continue

        purge = min(purge, max(1, test // 4))

        splits = make_walk_forward_splits(
            n=n_search,
            train_size=train,
            val_size=val,
            test_size=test,
            purge=purge,
        )
        got = len(splits)
        if got <= 0:
            continue

        cand = WFWindow(
            train_size=train,
            val_size=val,
            test_size=test,
            purge=purge,
            splits=got,
        )
        if best is None:
            best = cand
        if got >= wanted:
            return cand

    if best is None:
        raise ValueError(f"cannot derive walk-forward windows from n_search={n_search}")
    return best


def _required_events_for_years(*, timeframe_sec: int, min_years: float) -> int:
    bars_per_day = 86400.0 / float(timeframe_sec)
    return max(1, int(round(bars_per_day * 365.0 * float(min_years))))


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Prepare Kraken historical data (bulk zip + live top-up), validate coverage, "
            "derive robust walk-forward windows, and launch optimization."
        )
    )
    p.add_argument("--config", default="configs/sim.yaml")
    p.add_argument("--db-path", default="data/market.db")
    p.add_argument("--symbol", default="", help="e.g. XBT/EUR; defaults to config data.symbol/md.pair")
    p.add_argument("--timeframe", default="", help="e.g. 5m; defaults to config data.timeframe or 5m")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="")
    p.add_argument("--zip", action="append", default=[], help="Path to Kraken_OHLCVT.zip (repeatable)")
    p.add_argument("--skip-topup", action="store_true")
    p.add_argument("--min-years", type=float, default=2.0)
    p.add_argument("--min-events", type=int, default=0)
    p.add_argument("--max-events", type=int, default=120000, help="0 = use all rows for optimization")
    p.add_argument("--min-opt-events", type=int, default=12000)
    p.add_argument("--target-splits", type=int, default=5)
    p.add_argument("--train-ratio", type=int, default=3)
    p.add_argument("--val-ratio", type=int, default=1)
    p.add_argument("--min-train", type=int, default=6000)
    p.add_argument("--min-val", type=int, default=2000)
    p.add_argument("--min-test", type=int, default=2000)
    p.add_argument("--purge-pct", type=float, default=0.0025)
    p.add_argument("--trials", type=int, default=160)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--optimizer-script",
        default="scripts/optimize_sim_all.py",
        help="Optimizer script path relative to repo root (default: scripts/optimize_sim_all.py).",
    )
    p.add_argument(
        "--optimizer-arg",
        action="append",
        default=[],
        help="Extra argument passed verbatim to optimizer script (repeatable).",
    )
    p.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) // 2),
        help="Parallel worker processes for optimization (default: half logical CPUs).",
    )
    p.add_argument("--min-oos-trades", type=int, default=30)
    p.add_argument("--allow-negative-oos", action="store_true")
    p.add_argument("--out-yaml", default="configs/sim_optimized.yaml")
    p.add_argument("--out-json", default="reports/sim_opt_report.json")
    p.add_argument("--profile-json", default="reports/data_profile.json")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    root = Path(REPO_ROOT)
    cfg_path = Path(str(args.config))
    if not cfg_path.is_absolute():
        cfg_path = (root / cfg_path).resolve()
    cfg = load_config(str(cfg_path)).raw

    symbol = str(args.symbol).strip() or str(_cfg(cfg, "data.symbol", _cfg(cfg, "md.pair", "XBT/EUR")))
    timeframe = str(args.timeframe).strip() or str(_cfg(cfg, "data.timeframe", "5m"))

    start = _parse_utc(args.start)
    end = _parse_utc(args.end)
    if end is None:
        end = datetime.now(timezone.utc)

    db_path = (root / str(args.db_path)).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    interval_sec = _timeframe_seconds(timeframe)
    timeframe_min = max(1, interval_sec // 60)
    quote = symbol.split("/")[-1].upper() if "/" in symbol else "EUR"

    # 1) Optional bulk imports from Kraken OHLCVT zip(s).
    for raw_zip in list(args.zip or []):
        zip_path = Path(raw_zip)
        if not zip_path.is_absolute():
            zip_path = (root / zip_path).resolve()
        if not zip_path.exists():
            raise SystemExit(f"zip not found: {zip_path}")

        cmd = [
            sys.executable,
            "-m",
            "trading.data.import_kraken_ohlcvt_zip",
            "--zip",
            str(zip_path),
            "--db",
            str(db_path),
            "--timeframe-min",
            str(timeframe_min),
            "--quote",
            str(quote),
            "--symbol",
            symbol,
            "--start",
            _iso_utc(start).split("T", 1)[0] if start else "2024-01-01",
            "--end",
            _iso_utc(end).split("T", 1)[0],
        ]
        _run(cmd, cwd=root)

    profile_before_topup = _profile_candles(
        db_path=db_path,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
    )

    # 2) REST top-up for the newest window (Kraken OHLC is recent-window only).
    if not args.skip_topup:
        topup_start = start
        if profile_before_topup.max_ts is not None:
            back = max(interval_sec * 3, 300)
            topup_start = datetime.fromtimestamp(
                int(profile_before_topup.max_ts.timestamp()) - back,
                tz=timezone.utc,
            )
        cmd = [
            sys.executable,
            "-m",
            "trading.data.fetch_kraken_ohlcv",
            "--db",
            str(db_path),
            "--symbol",
            symbol,
            "--timeframe",
            timeframe,
            "--start",
            _iso_utc(topup_start if topup_start is not None else datetime.now(timezone.utc)),
        ]
        _run(cmd, cwd=root)

    profile = _profile_candles(
        db_path=db_path,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
    )

    if profile.count <= 0:
        raise SystemExit(
            "no candles found in DB for symbol/timeframe. "
            "Import Kraken bulk data via --zip /path/to/Kraken_OHLCVT.zip first."
        )

    min_events_years = _required_events_for_years(timeframe_sec=interval_sec, min_years=float(args.min_years))
    min_events = max(int(args.min_events), int(min_events_years))
    if profile.count < min_events:
        raise SystemExit(
            f"insufficient data for robust optimization: need >= {min_events} bars, have {profile.count} "
            f"(symbol={symbol}, timeframe={timeframe})"
        )

    use_events = profile.count if int(args.max_events) <= 0 else min(profile.count, int(args.max_events))
    if use_events < int(args.min_opt_events):
        raise SystemExit(f"need at least {int(args.min_opt_events)} bars for robust optimization run, got {use_events}")

    n_search = int(use_events * 0.8)
    wf = _derive_wf_window(
        n_search=n_search,
        target_splits=int(args.target_splits),
        train_ratio=int(args.train_ratio),
        val_ratio=int(args.val_ratio),
        min_train=int(args.min_train),
        min_val=int(args.min_val),
        min_test=int(args.min_test),
        purge_pct=float(args.purge_pct),
    )

    reports_dir = (root / "reports").resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)

    profile_payload = {
        "generated_at": _iso_utc(datetime.now(timezone.utc)),
        "config": str(cfg_path),
        "db_path": str(db_path),
        "symbol": symbol,
        "timeframe": timeframe,
        "requested_range": {
            "start": _iso_utc(start) if start else None,
            "end": _iso_utc(end) if end else None,
        },
        "profile_before_topup": profile_before_topup.as_dict(),
        "profile_after_topup": profile.as_dict(),
        "robustness_thresholds": {
            "min_years": float(args.min_years),
            "min_events_from_years": int(min_events_years),
            "min_events_effective": int(min_events),
            "target_splits": int(args.target_splits),
            "min_oos_trades": int(args.min_oos_trades),
        },
        "optimization_plan": {
            "use_events": int(use_events),
            "search_events": int(n_search),
            "holdout_events": int(use_events - n_search),
            "walk_forward": wf.as_dict(),
            "trials": int(args.trials),
            "seed": int(args.seed),
            "jobs": int(args.jobs),
            "enforce_positive_oos": not bool(args.allow_negative_oos),
        },
    }

    profile_json = (root / str(args.profile_json)).resolve()
    profile_json.parent.mkdir(parents=True, exist_ok=True)
    profile_json.write_text(json.dumps(profile_payload, indent=2), encoding="utf-8")

    print(json.dumps({"profile_json": str(profile_json), "plan": profile_payload["optimization_plan"]}, indent=2), flush=True)

    optimizer_script = str(args.optimizer_script).strip()
    if not optimizer_script:
        raise SystemExit("optimizer-script must not be empty")

    cmd_opt = [
        sys.executable,
        optimizer_script,
        "--config",
        str(cfg_path),
        "--db-path",
        str(db_path),
        "--symbol",
        symbol,
        "--timeframe",
        timeframe,
        "--max-events",
        str(int(use_events) if int(args.max_events) > 0 else 0),
        "--trials",
        str(int(args.trials)),
        "--seed",
        str(int(args.seed)),
        "--jobs",
        str(int(args.jobs)),
        "--train-size",
        str(int(wf.train_size)),
        "--val-size",
        str(int(wf.val_size)),
        "--test-size",
        str(int(wf.test_size)),
        "--purge",
        str(int(wf.purge)),
        "--min-oos-trades",
        str(int(args.min_oos_trades)),
        "--out-yaml",
        str(args.out_yaml),
        "--out-json",
        str(args.out_json),
    ]
    for extra in list(args.optimizer_arg or []):
        s = str(extra).strip()
        if s:
            cmd_opt.append(s)
    if not bool(args.allow_negative_oos):
        cmd_opt.append("--enforce-positive-oos")

    if args.dry_run:
        print(json.dumps({"dry_run": True, "optimize_command": cmd_opt}, indent=2), flush=True)
        return

    _run(cmd_opt, cwd=root)


if __name__ == "__main__":
    main()
