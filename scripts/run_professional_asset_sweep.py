#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
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


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "asset"


def _run(cmd: List[str], cwd: Path, ok_codes: Optional[List[int]] = None) -> int:
    if ok_codes is None:
        ok_codes = [0]
    print(json.dumps({"ts": _ts(), "run": cmd}, ensure_ascii=True), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd), check=False)
    if int(proc.returncode) not in set(int(x) for x in ok_codes):
        raise subprocess.CalledProcessError(int(proc.returncode), cmd)
    return int(proc.returncode)


def _parse_csv(raw: str) -> List[str]:
    out: List[str] = []
    for item in str(raw or "").split(","):
        x = item.strip()
        if not x:
            continue
        out.append(x)
    return out


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


def _adaptive_constraints(timeframe: str) -> Dict[str, float]:
    sec = _timeframe_seconds(timeframe)
    if sec <= 300:  # 5m or faster
        return {
            "target_splits": 8,
            "train_ratio": 3,
            "val_ratio": 1,
            "min_train": 12000,
            "min_val": 3500,
            "min_test": 3500,
            "purge_pct": 0.003,
            "min_oos_trades": 30.0,
            "oos_min_trades": 30.0,
        }
    if sec <= 900:  # 15m
        return {
            "target_splits": 6,
            "train_ratio": 3,
            "val_ratio": 1,
            "min_train": 8000,
            "min_val": 2500,
            "min_test": 2500,
            "purge_pct": 0.003,
            "min_oos_trades": 20.0,
            "oos_min_trades": 20.0,
        }
    if sec <= 1800:  # 30m
        return {
            "target_splits": 4,
            "train_ratio": 3,
            "val_ratio": 1,
            "min_train": 5000,
            "min_val": 1500,
            "min_test": 1500,
            "purge_pct": 0.003,
            "min_oos_trades": 10.0,
            "oos_min_trades": 10.0,
        }
    return {
        "target_splits": 3,
        "train_ratio": 3,
        "val_ratio": 1,
        "min_train": 3000,
        "min_val": 900,
        "min_test": 900,
        "purge_pct": 0.003,
        "min_oos_trades": 8.0,
        "oos_min_trades": 8.0,
    }


def _count_candles(db_path: Path, symbol: str, timeframe: str) -> Dict[str, Any]:
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT COUNT(*), MIN(ts_unix), MAX(ts_unix) FROM candles WHERE symbol = ? AND timeframe = ?",
            (str(symbol), str(timeframe)),
        ).fetchone()
    finally:
        con.close()
    count = int((row or [0])[0] or 0)
    mn = int(row[1]) if row and row[1] is not None else None
    mx = int(row[2]) if row and row[2] is not None else None
    return {
        "count": count,
        "min_ts_unix": mn,
        "max_ts_unix": mx,
        "min_ts": datetime.fromtimestamp(mn, tz=timezone.utc).isoformat().replace("+00:00", "Z") if mn is not None else None,
        "max_ts": datetime.fromtimestamp(mx, tz=timezone.utc).isoformat().replace("+00:00", "Z") if mx is not None else None,
    }


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"not a JSON object: {path}")
    return doc


def _best_round_metrics(summary: Dict[str, Any]) -> Dict[str, Any]:
    rounds = summary.get("rounds", [])
    if not isinstance(rounds, list):
        return {}
    best: Optional[Dict[str, Any]] = None
    best_ret = -1e18
    for r in rounds:
        if not isinstance(r, dict):
            continue
        gate = r.get("gate", {})
        metrics = gate.get("metrics", {}) if isinstance(gate, dict) else {}
        ret = float(metrics.get("holdout_return_pct", -1e18) or -1e18)
        if ret > best_ret:
            best_ret = ret
            best = r
    if best is None:
        return {}
    gate = best.get("gate", {})
    metrics = gate.get("metrics", {}) if isinstance(gate, dict) else {}
    return {
        "tag": best.get("tag"),
        "best_family": best.get("best_family"),
        "passed": bool(best.get("passed", False)),
        "holdout_return_pct": float(metrics.get("holdout_return_pct", 0.0) or 0.0),
        "holdout_sharpe": float(metrics.get("holdout_sharpe", 0.0) or 0.0),
        "holdout_max_drawdown_pct": float(metrics.get("holdout_max_drawdown_pct", 0.0) or 0.0),
        "holdout_trades": float(metrics.get("holdout_trades", 0.0) or 0.0),
        "oos_return_mean_pct": float(metrics.get("oos_return_mean_pct", 0.0) or 0.0),
        "oos_trades_mean": float(metrics.get("oos_trades_mean", 0.0) or 0.0),
    }


@dataclass
class AssetResult:
    symbol: str
    timeframe: str
    fetch_ok: bool
    optimize_ok: bool
    accepted: bool
    best_family: str
    holdout_return_pct: float
    holdout_sharpe: float
    holdout_max_drawdown_pct: float
    holdout_trades: float
    oos_return_mean_pct: float
    oos_trades_mean: float
    candles: int
    min_ts: str
    max_ts: str
    governance_json: str
    governance_log: str
    notes: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "fetch_ok": self.fetch_ok,
            "optimize_ok": self.optimize_ok,
            "accepted": self.accepted,
            "best_family": self.best_family,
            "holdout_return_pct": self.holdout_return_pct,
            "holdout_sharpe": self.holdout_sharpe,
            "holdout_max_drawdown_pct": self.holdout_max_drawdown_pct,
            "holdout_trades": self.holdout_trades,
            "oos_return_mean_pct": self.oos_return_mean_pct,
            "oos_trades_mean": self.oos_trades_mean,
            "candles": self.candles,
            "min_ts": self.min_ts,
            "max_ts": self.max_ts,
            "governance_json": self.governance_json,
            "governance_log": self.governance_log,
            "notes": self.notes,
        }


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Run a professional multi-asset sweep: historical Kraken trade aggregation + "
            "governed walk-forward optimization, then rank assets by acceptance and return potential."
        )
    )
    p.add_argument("--symbols", default="ETH/EUR,SOL/EUR,XRP/EUR")
    p.add_argument("--config", default="configs/sim_auto.yaml")
    p.add_argument("--db-path", default="data/market_robust.db")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--start-ts", default="2023-01-01T00:00:00Z")
    p.add_argument("--start-date", default="2023-01-01")
    p.add_argument("--min-years", type=float, default=2.4)
    p.add_argument("--families", default="momentum,mean_reversion,breakout,swing,auto")
    p.add_argument("--trials-list", default="1200,2400")
    p.add_argument("--seed-list", default="42,1337")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.add_argument("--wf-mode", choices=["adaptive", "fixed"], default="adaptive")
    p.add_argument("--target-splits", type=int, default=8)
    p.add_argument("--train-ratio", type=int, default=3)
    p.add_argument("--val-ratio", type=int, default=1)
    p.add_argument("--min-train", type=int, default=20000)
    p.add_argument("--min-val", type=int, default=7000)
    p.add_argument("--min-test", type=int, default=7000)
    p.add_argument("--purge-pct", type=float, default=0.003)
    p.add_argument("--min-oos-trades", type=float, default=-1.0)
    p.add_argument("--holdout-min-return", type=float, default=0.0)
    p.add_argument("--holdout-min-sharpe", type=float, default=0.0)
    p.add_argument("--holdout-max-drawdown", type=float, default=8.0)
    p.add_argument("--holdout-max-realized-cost-bps", type=float, default=40.0)
    p.add_argument("--oos-min-return", type=float, default=0.0)
    p.add_argument("--oos-min-trades", type=float, default=-1.0)
    p.add_argument("--fetch-sleep-sec", type=float, default=0.35)
    p.add_argument("--fetch-timeout-sec", type=float, default=20.0)
    p.add_argument("--fetch-retry-max", type=int, default=20)
    p.add_argument("--fetch-retry-backoff-sec", type=float, default=1.5)
    p.add_argument("--fetch-retry-backoff-max-sec", type=float, default=60.0)
    p.add_argument("--fetch-progress-every", type=int, default=500)
    p.add_argument("--fetch-max-pages", type=int, default=0, help="Optional cap for faster test runs.")
    p.add_argument("--skip-fetch", action="store_true", help="Skip data fetch and run optimization on existing DB data.")
    p.add_argument(
        "--skip-fetch-symbols",
        default="",
        help="Comma-separated symbols to skip fetch for (e.g. XBT/EUR,LINK/EUR).",
    )
    p.add_argument("--out-json", default="reports/professional_asset_sweep_summary.json")
    p.add_argument("--out-csv", default="reports/professional_asset_sweep_summary.csv")
    args = p.parse_args()

    root = Path(REPO_ROOT)
    db_path = (root / str(args.db_path)).resolve()
    out_json = (root / str(args.out_json)).resolve()
    out_csv = (root / str(args.out_csv)).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    symbols = _parse_csv(args.symbols)
    if not symbols:
        raise SystemExit("no symbols")
    skip_fetch_symbols = {s.upper() for s in _parse_csv(args.skip_fetch_symbols)}

    constraints = (
        _adaptive_constraints(str(args.timeframe))
        if str(args.wf_mode).strip().lower() == "adaptive"
        else {
            "target_splits": int(args.target_splits),
            "train_ratio": int(args.train_ratio),
            "val_ratio": int(args.val_ratio),
            "min_train": int(args.min_train),
            "min_val": int(args.min_val),
            "min_test": int(args.min_test),
            "purge_pct": float(args.purge_pct),
            "min_oos_trades": float(args.min_oos_trades if args.min_oos_trades >= 0 else 30.0),
            "oos_min_trades": float(args.oos_min_trades if args.oos_min_trades >= 0 else 30.0),
        }
    )
    if float(args.min_oos_trades) >= 0.0:
        constraints["min_oos_trades"] = float(args.min_oos_trades)
    if float(args.oos_min_trades) >= 0.0:
        constraints["oos_min_trades"] = float(args.oos_min_trades)

    results: List[AssetResult] = []
    for symbol in symbols:
        sym_tag = _slug(symbol)
        gov_json = root / f"reports/professional_governance_summary_{sym_tag}.json"
        gov_log = root / f"reports/professional_governance_runs_{sym_tag}.jsonl"
        out_yaml_final = root / f"configs/sim_optimized_professional_governed_{sym_tag}.yaml"
        out_json_final = root / f"reports/sim_opt_report_professional_governed_{sym_tag}.json"

        fetch_ok = False
        optimize_ok = False
        accepted = False
        best_family = ""
        holdout_return_pct = 0.0
        holdout_sharpe = 0.0
        holdout_max_drawdown_pct = 0.0
        holdout_trades = 0.0
        oos_return_mean_pct = 0.0
        oos_trades_mean = 0.0
        notes = ""

        if args.skip_fetch or str(symbol).upper() in skip_fetch_symbols:
            fetch_ok = True
        else:
            fetch_cmd = [
                sys.executable,
                "scripts/fetch_kraken_trades_ohlcv.py",
                "--db",
                str(db_path),
                "--symbol",
                str(symbol),
                "--timeframe",
                str(args.timeframe),
                "--start",
                str(args.start_ts),
                "--sleep-sec",
                str(float(args.fetch_sleep_sec)),
                "--timeout-sec",
                str(float(args.fetch_timeout_sec)),
                "--retry-max",
                str(int(args.fetch_retry_max)),
                "--retry-backoff-sec",
                str(float(args.fetch_retry_backoff_sec)),
                "--retry-backoff-max-sec",
                str(float(args.fetch_retry_backoff_max_sec)),
                "--progress-every",
                str(int(args.fetch_progress_every)),
            ]
            if int(args.fetch_max_pages) > 0:
                fetch_cmd.extend(["--max-pages", str(int(args.fetch_max_pages))])
            try:
                _run(fetch_cmd, cwd=root, ok_codes=[0])
                fetch_ok = True
            except Exception as exc:
                notes = f"fetch_error:{exc}"

        candles = _count_candles(db_path, symbol, str(args.timeframe))

        if fetch_ok:
            try:
                rc = _run(
                    [
                        sys.executable,
                        "scripts/governed_professional_optimizer.py",
                        "--config",
                        str(args.config),
                        "--db-path",
                        str(db_path),
                        "--symbol",
                        str(symbol),
                        "--timeframe",
                        str(args.timeframe),
                        "--start",
                        str(args.start_date),
                        "--families",
                        str(args.families),
                        "--trials-list",
                        str(args.trials_list),
                        "--seed-list",
                        str(args.seed_list),
                        "--jobs",
                        str(int(args.jobs)),
                        "--min-years",
                        str(float(args.min_years)),
                        "--max-events",
                        "0",
                        "--target-splits",
                        str(int(constraints["target_splits"])),
                        "--train-ratio",
                        str(int(constraints["train_ratio"])),
                        "--val-ratio",
                        str(int(constraints["val_ratio"])),
                        "--min-train",
                        str(int(constraints["min_train"])),
                        "--min-val",
                        str(int(constraints["min_val"])),
                        "--min-test",
                        str(int(constraints["min_test"])),
                        "--purge-pct",
                        str(float(constraints["purge_pct"])),
                        "--min-oos-trades",
                        str(int(round(float(constraints["min_oos_trades"])))),
                        "--holdout-min-return",
                        str(float(args.holdout_min_return)),
                        "--holdout-min-sharpe",
                        str(float(args.holdout_min_sharpe)),
                        "--holdout-max-drawdown",
                        str(float(args.holdout_max_drawdown)),
                        "--holdout-max-realized-cost-bps",
                        str(float(args.holdout_max_realized_cost_bps)),
                        "--oos-min-return",
                        str(float(args.oos_min_return)),
                        "--oos-min-trades",
                        str(float(constraints["oos_min_trades"])),
                        "--out-yaml-final",
                        str(out_yaml_final),
                        "--out-json-final",
                        str(out_json_final),
                        "--governance-json",
                        str(gov_json),
                        "--governance-log",
                        str(gov_log),
                        "--round-prefix",
                        str(sym_tag),
                    ],
                    cwd=root,
                    ok_codes=[0, 2],
                )
                optimize_ok = True
                if int(rc) == 2:
                    notes = (notes + "; " if notes else "") + "governance_rejected"
            except Exception as exc:
                notes = (notes + "; " if notes else "") + f"opt_error:{exc}"

        if gov_json.exists():
            try:
                summary = _load_json(gov_json)
                accepted = bool(summary.get("accepted"))
                best = _best_round_metrics(summary)
                best_family = str(best.get("best_family", ""))
                holdout_return_pct = float(best.get("holdout_return_pct", 0.0) or 0.0)
                holdout_sharpe = float(best.get("holdout_sharpe", 0.0) or 0.0)
                holdout_max_drawdown_pct = float(best.get("holdout_max_drawdown_pct", 0.0) or 0.0)
                holdout_trades = float(best.get("holdout_trades", 0.0) or 0.0)
                oos_return_mean_pct = float(best.get("oos_return_mean_pct", 0.0) or 0.0)
                oos_trades_mean = float(best.get("oos_trades_mean", 0.0) or 0.0)
            except Exception as exc:
                notes = (notes + "; " if notes else "") + f"summary_error:{exc}"

        results.append(
            AssetResult(
                symbol=str(symbol),
                timeframe=str(args.timeframe),
                fetch_ok=bool(fetch_ok),
                optimize_ok=bool(optimize_ok),
                accepted=bool(accepted),
                best_family=best_family,
                holdout_return_pct=holdout_return_pct,
                holdout_sharpe=holdout_sharpe,
                holdout_max_drawdown_pct=holdout_max_drawdown_pct,
                holdout_trades=holdout_trades,
                oos_return_mean_pct=oos_return_mean_pct,
                oos_trades_mean=oos_trades_mean,
                candles=int(candles["count"]),
                min_ts=str(candles.get("min_ts") or ""),
                max_ts=str(candles.get("max_ts") or ""),
                governance_json=str(gov_json),
                governance_log=str(gov_log),
                notes=notes,
            )
        )

    # Ranking: accepted first, then holdout return, then OOS return, then lower drawdown.
    ranked = sorted(
        results,
        key=lambda r: (
            1 if r.accepted else 0,
            float(r.holdout_return_pct),
            float(r.oos_return_mean_pct),
            -float(r.holdout_max_drawdown_pct),
        ),
        reverse=True,
    )

    payload = {
        "generated_at": _ts(),
        "config": str((root / str(args.config)).resolve()),
        "db_path": str(db_path),
        "timeframe": str(args.timeframe),
        "wf_mode": str(args.wf_mode),
        "wf_constraints": {
            "target_splits": int(constraints["target_splits"]),
            "train_ratio": int(constraints["train_ratio"]),
            "val_ratio": int(constraints["val_ratio"]),
            "min_train": int(constraints["min_train"]),
            "min_val": int(constraints["min_val"]),
            "min_test": int(constraints["min_test"]),
            "purge_pct": float(constraints["purge_pct"]),
            "min_oos_trades": float(constraints["min_oos_trades"]),
            "oos_min_trades": float(constraints["oos_min_trades"]),
        },
        "start_ts": str(args.start_ts),
        "start_date": str(args.start_date),
        "min_years": float(args.min_years),
        "symbols": symbols,
        "ranking": [r.as_dict() for r in ranked],
        "winner": ranked[0].as_dict() if ranked else None,
    }

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)

    fields = [
        "symbol",
        "timeframe",
        "fetch_ok",
        "optimize_ok",
        "accepted",
        "best_family",
        "holdout_return_pct",
        "holdout_sharpe",
        "holdout_max_drawdown_pct",
        "holdout_trades",
        "oos_return_mean_pct",
        "oos_trades_mean",
        "candles",
        "min_ts",
        "max_ts",
        "governance_json",
        "governance_log",
        "notes",
    ]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in ranked:
            writer.writerow(row.as_dict())

    print(
        json.dumps(
            {
                "ok": True,
                "out_json": str(out_json),
                "out_csv": str(out_csv),
                "winner": ranked[0].as_dict() if ranked else None,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
