#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Small bounded walk-forward training preset (one step above minimal): "
            "a small grid + 2 splits on the tiny sample dataset."
        )
    )
    p.add_argument("--config", default="configs/paper.yaml")
    p.add_argument("--out-yaml", default="configs/alpha_trained_small.yaml")
    p.add_argument("--out-json", default="reports/alpha_train_report_small.json")
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
        # Cap runtime; if the dataset is larger, only use the most recent N points.
        "--max-events",
        "5000",
        # Window sizes: chosen so that ~200 events produce 2 walk-forward splits.
        "--train-size",
        "50",
        "--val-size",
        "25",
        "--test-size",
        "25",
        "--purge",
        "1",
        # Small but non-trivial grid.
        "--threshold-grid",
        "1,2,3",
        "--scale-grid",
        "0.5,1,2",
        "--lookback-grid",
        "2,3,4",
        "--top-k-train",
        "3",
    ]
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()

