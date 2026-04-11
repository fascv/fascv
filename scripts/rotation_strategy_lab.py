#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.meta.rotation_strategy_lab import (  # noqa: E402
    DEFAULT_ACTIVE_FILE,
    DEFAULT_CATALOG_FILE,
    DEFAULT_LOOKBACK_HOURS,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RUNTIME_ENV_FILE,
    run_strategy_labs,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run per-strategy rotation labs for the 4 live core strategies and persist reusable reports."
    )
    parser.add_argument("--log-dir", default=str(REPO_ROOT / "logs"))
    parser.add_argument("--catalog-file", default=str(DEFAULT_CATALOG_FILE))
    parser.add_argument("--active-file", default=str(DEFAULT_ACTIVE_FILE))
    parser.add_argument("--runtime-env-file", default=str(DEFAULT_RUNTIME_ENV_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--lookback-hours", type=float, default=float(DEFAULT_LOOKBACK_HOURS))
    parser.add_argument(
        "--strategy",
        default="",
        help="Optional single strategy filter: rebound, staircase, continuation, breakout",
    )
    args = parser.parse_args()

    summary = run_strategy_labs(
        log_dir=Path(args.log_dir),
        catalog_file=Path(args.catalog_file),
        active_file=Path(args.active_file),
        runtime_env_file=Path(args.runtime_env_file),
        output_dir=Path(args.output_dir),
        lookback_hours=float(args.lookback_hours),
        strategy_filter=str(args.strategy or ""),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
