#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_int_list(raw: str) -> List[int]:
    out: List[int] = []
    for x in str(raw or "").split(","):
        x = x.strip()
        if not x:
            continue
        out.append(int(x))
    return out


def _run(cmd: List[str], cwd: Path) -> None:
    print(json.dumps({"ts": _ts(), "run": cmd}, ensure_ascii=True), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"not a JSON object: {path}")
    return doc


def _gate_report(doc: Dict[str, Any], *, holdout_min_return: float, holdout_min_sharpe: float, holdout_max_drawdown: float, min_oos_return: float, min_oos_trades: float) -> Tuple[bool, Dict[str, Any]]:
    holdout = doc.get("holdout", {}) if isinstance(doc.get("holdout"), dict) else {}
    best = doc.get("best", {}) if isinstance(doc.get("best"), dict) else {}

    holdout_return = float(holdout.get("return_pct", 0.0) or 0.0)
    holdout_sharpe = float(holdout.get("sharpe", 0.0) or 0.0)
    holdout_dd = float(holdout.get("max_drawdown_pct", 0.0) or 0.0)
    oos_return = float(best.get("oos_return_mean_pct", 0.0) or 0.0)
    oos_trades = float(best.get("oos_trades_mean", 0.0) or 0.0)

    checks = {
        "holdout_return": holdout_return >= float(holdout_min_return),
        "holdout_sharpe": holdout_sharpe >= float(holdout_min_sharpe),
        "holdout_drawdown": holdout_dd <= float(holdout_max_drawdown),
        "oos_return_mean": oos_return >= float(min_oos_return),
        "oos_trades_mean": oos_trades >= float(min_oos_trades),
    }
    passed = all(checks.values())
    metrics = {
        "holdout_return_pct": holdout_return,
        "holdout_sharpe": holdout_sharpe,
        "holdout_max_drawdown_pct": holdout_dd,
        "oos_return_mean_pct": oos_return,
        "oos_trades_mean": oos_trades,
    }
    return passed, {"checks": checks, "metrics": metrics}


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Governed optimization runner: executes multiple walk-forward optimization rounds and "
            "accepts only runs that pass strict holdout/OOS risk gates."
        )
    )
    p.add_argument("--config", default="configs/sim.yaml")
    p.add_argument("--db-path", default="data/market_robust.db")
    p.add_argument("--symbol", default="XBT/EUR")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--start", default="2023-01-01")

    p.add_argument("--trials-list", default="1200,2400,3600")
    p.add_argument("--seed-list", default="42,1337,7")
    p.add_argument("--jobs", type=int, default=12)

    p.add_argument("--min-years", type=float, default=2.5)
    p.add_argument("--max-events", type=int, default=0)
    p.add_argument("--target-splits", type=int, default=8)
    p.add_argument("--train-ratio", type=int, default=3)
    p.add_argument("--val-ratio", type=int, default=1)
    p.add_argument("--min-train", type=int, default=20000)
    p.add_argument("--min-val", type=int, default=7000)
    p.add_argument("--min-test", type=int, default=7000)
    p.add_argument("--purge-pct", type=float, default=0.003)
    p.add_argument("--min-oos-trades", type=int, default=40)

    p.add_argument("--holdout-min-return", type=float, default=0.0)
    p.add_argument("--holdout-min-sharpe", type=float, default=0.0)
    p.add_argument("--holdout-max-drawdown", type=float, default=8.0)
    p.add_argument("--oos-min-return", type=float, default=0.0)
    p.add_argument("--oos-min-trades", type=float, default=40.0)

    p.add_argument("--out-yaml-final", default="configs/sim_optimized_momentum_governed.yaml")
    p.add_argument("--out-json-final", default="reports/sim_opt_report_momentum_governed.json")
    p.add_argument("--governance-json", default="reports/momentum_governance_summary.json")
    p.add_argument("--governance-log", default="reports/momentum_governance_runs.jsonl")
    args = p.parse_args()

    root = Path(REPO_ROOT)
    trials_list = _parse_int_list(args.trials_list)
    seed_list = _parse_int_list(args.seed_list)
    if not trials_list or not seed_list:
        raise SystemExit("trials-list and seed-list must be non-empty")

    rounds: List[Tuple[int, int]] = []
    # Cartesian product: each trial budget tested with each seed.
    for trials in trials_list:
        for seed in seed_list:
            rounds.append((int(trials), int(seed)))

    gov_log = (root / str(args.governance_log)).resolve()
    gov_log.parent.mkdir(parents=True, exist_ok=True)

    summary: Dict[str, Any] = {
        "started_at": _ts(),
        "config": str((root / str(args.config)).resolve()),
        "db_path": str((root / str(args.db_path)).resolve()),
        "rounds": [],
        "accepted": None,
        "gates": {
            "holdout_min_return": float(args.holdout_min_return),
            "holdout_min_sharpe": float(args.holdout_min_sharpe),
            "holdout_max_drawdown": float(args.holdout_max_drawdown),
            "oos_min_return": float(args.oos_min_return),
            "oos_min_trades": float(args.oos_min_trades),
        },
    }

    accepted_round: Dict[str, Any] | None = None

    for i, (trials, seed) in enumerate(rounds, start=1):
        tag = f"r{i:02d}_t{trials}_s{seed}"
        out_yaml = root / f"configs/sim_optimized_{tag}.yaml"
        out_json = root / f"reports/sim_opt_report_{tag}.json"
        profile_json = root / f"reports/data_profile_{tag}.json"

        cmd = [
            sys.executable,
            "scripts/prepare_kraken_data_and_optimize.py",
            "--skip-topup",
            "--config",
            str(args.config),
            "--db-path",
            str(args.db_path),
            "--symbol",
            str(args.symbol),
            "--timeframe",
            str(args.timeframe),
            "--start",
            str(args.start),
            "--min-years",
            str(float(args.min_years)),
            "--max-events",
            str(int(args.max_events)),
            "--target-splits",
            str(int(args.target_splits)),
            "--train-ratio",
            str(int(args.train_ratio)),
            "--val-ratio",
            str(int(args.val_ratio)),
            "--min-train",
            str(int(args.min_train)),
            "--min-val",
            str(int(args.min_val)),
            "--min-test",
            str(int(args.min_test)),
            "--purge-pct",
            str(float(args.purge_pct)),
            "--trials",
            str(int(trials)),
            "--seed",
            str(int(seed)),
            "--jobs",
            str(int(args.jobs)),
            "--min-oos-trades",
            str(int(args.min_oos_trades)),
            "--out-yaml",
            str(out_yaml),
            "--out-json",
            str(out_json),
            "--profile-json",
            str(profile_json),
        ]

        round_info: Dict[str, Any] = {
            "tag": tag,
            "trials": int(trials),
            "seed": int(seed),
            "started_at": _ts(),
            "out_yaml": str(out_yaml),
            "out_json": str(out_json),
            "profile_json": str(profile_json),
        }

        try:
            _run(cmd, cwd=root)
            report = _load_json(out_json)
            passed, gate = _gate_report(
                report,
                holdout_min_return=float(args.holdout_min_return),
                holdout_min_sharpe=float(args.holdout_min_sharpe),
                holdout_max_drawdown=float(args.holdout_max_drawdown),
                min_oos_return=float(args.oos_min_return),
                min_oos_trades=float(args.oos_min_trades),
            )
            round_info["status"] = "ok"
            round_info["gate"] = gate
            round_info["passed"] = bool(passed)
        except Exception as exc:
            round_info["status"] = "error"
            round_info["error"] = str(exc)
            round_info["passed"] = False

        round_info["finished_at"] = _ts()
        summary["rounds"].append(round_info)

        with open(gov_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(round_info, ensure_ascii=True) + "\n")

        print(json.dumps({"round": tag, "passed": round_info.get("passed"), "status": round_info.get("status")}, ensure_ascii=True), flush=True)

        if round_info.get("passed"):
            accepted_round = round_info
            break

    if accepted_round is not None:
        out_yaml_final = (root / str(args.out_yaml_final)).resolve()
        out_json_final = (root / str(args.out_json_final)).resolve()
        out_yaml_final.parent.mkdir(parents=True, exist_ok=True)
        out_json_final.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(accepted_round["out_yaml"], out_yaml_final)
        shutil.copy2(accepted_round["out_json"], out_json_final)

        summary["accepted"] = {
            "tag": accepted_round["tag"],
            "out_yaml_final": str(out_yaml_final),
            "out_json_final": str(out_json_final),
        }
        summary["finished_at"] = _ts()
        summary["status"] = "accepted"

        with open((root / str(args.governance_json)).resolve(), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

        print(json.dumps({"ok": True, "accepted": summary["accepted"]}, indent=2), flush=True)
        return

    summary["finished_at"] = _ts()
    summary["status"] = "rejected"
    with open((root / str(args.governance_json)).resolve(), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps({"ok": False, "status": "rejected", "reason": "no_round_passed_gates"}, indent=2), flush=True)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
