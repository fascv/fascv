#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    p = argparse.ArgumentParser(description="Minimal bounded walk-forward training preset (fast smoke test).")
    p.add_argument("--config", default="configs/paper.yaml")
    p.add_argument("--out-yaml", default="configs/alpha_trained.yaml")
    p.add_argument("--out-json", default="reports/alpha_train_report.json")
    args = p.parse_args()

    cmd = [
        sys.executable,
        "-m",
        "trading.alpha.train_momentum",
        "--config",
        str(args.config),
        "--out-yaml",
        str(args.out_yaml),
        "--out-json",
        str(args.out_json),
        "--max-events",
        # Keep this bounded; note that the default sample dataset only has ~200 rows.
        "2000",
        # Explicitly set small windows so training works even on the tiny sample CSV.
        "--train-size",
        "80",
        "--val-size",
        "40",
        "--test-size",
        "40",
        "--purge",
        "1",
        "--threshold-grid",
        "2",
        "--scale-grid",
        "1",
        "--lookback-grid",
        "3",
        "--top-k-train",
        "1",
    ]
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
