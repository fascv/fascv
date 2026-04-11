#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

COMMON8 = [
    "trend_momentum_fast",
    "trend_momentum_slow",
    "range_mean_reversion",
    "deep_mean_reversion",
    "vol_breakout_intraday",
    "vol_breakout_swing",
    "oscillation_swing",
    "regime_auto",
]


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_families(raw: str) -> List[str]:
    out: List[str] = []
    for x in str(raw or "").split(","):
        y = x.strip()
        if not y:
            continue
        if y == "common8":
            out.extend(COMMON8)
        else:
            out.append(y)
    if not out:
        out = list(COMMON8)
    seen: set[str] = set()
    dedup: List[str] = []
    for f in out:
        if f in seen:
            continue
        seen.add(f)
        dedup.append(f)
    return dedup


def _run(cmd: List[str], cwd: Path) -> int:
    print(json.dumps({"ts": _ts(), "run": cmd}, ensure_ascii=True), flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd))
    return int(proc.returncode)


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"not a json object: {path}")
    return doc


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Run governed professional optimization family-by-family and build a sweep summary. "
            "Useful to find robust champions without mixing all families in one competition."
        )
    )
    p.add_argument("--config", default="configs/sim_auto.yaml")
    p.add_argument("--db-path", default="data/market_robust.db")
    p.add_argument("--symbol", default="XBT/EUR")
    p.add_argument("--timeframe", default="30m")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--families", default="common8")
    p.add_argument("--trials-list", default="1200,2400")
    p.add_argument("--seed-list", default="42,1337")
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--min-years", type=float, default=2.5)
    p.add_argument("--max-events", type=int, default=0)
    p.add_argument("--target-splits", type=int, default=6)
    p.add_argument("--train-ratio", type=int, default=3)
    p.add_argument("--val-ratio", type=int, default=1)
    p.add_argument("--min-train", type=int, default=7000)
    p.add_argument("--min-val", type=int, default=2000)
    p.add_argument("--min-test", type=int, default=2000)
    p.add_argument("--purge-pct", type=float, default=0.003)
    p.add_argument("--min-oos-trades", type=int, default=4)
    p.add_argument("--holdout-min-return", type=float, default=0.0)
    p.add_argument("--holdout-min-sharpe", type=float, default=0.0)
    p.add_argument("--holdout-max-drawdown", type=float, default=8.0)
    p.add_argument("--holdout-min-trades", type=float, default=6.0)
    p.add_argument("--holdout-max-realized-cost-bps", type=float, default=40.0)
    p.add_argument("--oos-min-return", type=float, default=0.0)
    p.add_argument("--oos-min-trades", type=float, default=4.0)
    p.add_argument("--summary-json", default="reports/professional_family_sweep_summary.json")
    args = p.parse_args()

    root = Path(REPO_ROOT)
    families = _parse_families(args.families)
    out_summary = (root / str(args.summary_json)).resolve()
    out_summary.parent.mkdir(parents=True, exist_ok=True)

    sweep: Dict[str, Any] = {
        "started_at": _ts(),
        "config": str((root / str(args.config)).resolve()),
        "db_path": str((root / str(args.db_path)).resolve()),
        "symbol": str(args.symbol),
        "timeframe": str(args.timeframe),
        "families": list(families),
        "runs": [],
    }

    for idx, family in enumerate(families, start=1):
        family_tag = f"{idx:02d}_{family}"
        gov_json = root / f"reports/professional_governance_summary_{family_tag}.json"
        gov_log = root / f"reports/professional_governance_runs_{family_tag}.jsonl"
        out_yaml_final = root / f"configs/sim_optimized_professional_governed_{family_tag}.yaml"
        out_json_final = root / f"reports/sim_opt_report_professional_governed_{family_tag}.json"

        cmd = [
            sys.executable,
            "scripts/governed_professional_optimizer.py",
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
            "--families",
            str(family),
            "--trials-list",
            str(args.trials_list),
            "--seed-list",
            str(args.seed_list),
            "--jobs",
            str(int(args.jobs)),
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
            "--min-oos-trades",
            str(int(args.min_oos_trades)),
            "--holdout-min-return",
            str(float(args.holdout_min_return)),
            "--holdout-min-sharpe",
            str(float(args.holdout_min_sharpe)),
            "--holdout-max-drawdown",
            str(float(args.holdout_max_drawdown)),
            "--holdout-min-trades",
            str(float(args.holdout_min_trades)),
            "--holdout-max-realized-cost-bps",
            str(float(args.holdout_max_realized_cost_bps)),
            "--oos-min-return",
            str(float(args.oos_min_return)),
            "--oos-min-trades",
            str(float(args.oos_min_trades)),
            "--out-yaml-final",
            str(out_yaml_final),
            "--out-json-final",
            str(out_json_final),
            "--governance-json",
            str(gov_json),
            "--governance-log",
            str(gov_log),
        ]

        rc = _run(cmd, cwd=root)
        run_info: Dict[str, Any] = {
            "family": family,
            "tag": family_tag,
            "return_code": int(rc),
            "governance_json": str(gov_json),
            "governance_log": str(gov_log),
            "out_yaml_final": str(out_yaml_final),
            "out_json_final": str(out_json_final),
        }

        if gov_json.exists():
            try:
                gov = _load_json(gov_json)
                status = str(gov.get("status", ""))
                run_info["status"] = status
                run_info["accepted"] = status == "accepted"
                accepted = gov.get("accepted")
                if isinstance(accepted, dict):
                    run_info["accepted_tag"] = accepted.get("tag")
                    run_info["accepted_best_family"] = accepted.get("best_family")
            except Exception as exc:
                run_info["status"] = "error"
                run_info["accepted"] = False
                run_info["error"] = f"failed_to_read_governance_json: {exc}"
        else:
            run_info["status"] = "missing_governance_json"
            run_info["accepted"] = False

        sweep["runs"].append(run_info)
        print(
            json.dumps(
                {
                    "family": family,
                    "status": run_info.get("status"),
                    "accepted": run_info.get("accepted"),
                    "accepted_tag": run_info.get("accepted_tag"),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )

    accepted_runs = [r for r in sweep["runs"] if bool(r.get("accepted"))]
    sweep["accepted_count"] = int(len(accepted_runs))
    sweep["accepted_families"] = [str(r.get("family")) for r in accepted_runs]
    sweep["finished_at"] = _ts()
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump(sweep, f, indent=2)

    print(
        json.dumps(
            {
                "ok": True,
                "summary_json": str(out_summary),
                "accepted_count": int(len(accepted_runs)),
                "accepted_families": sweep["accepted_families"],
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

