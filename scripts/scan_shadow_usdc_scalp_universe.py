#!/usr/bin/env python3
from __future__ import annotations

import atexit
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.meta.strategy_views import (
    STRATEGY_NAMES,
    annotate_rows_with_strategy_views,
    serialize_strategy_rankings,
)


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_SNAPSHOT_DIR = REPO_ROOT / "logs" / "shadow_usdc_scalp_snapshots"
DEFAULT_INDEX_JSONL = REPO_ROOT / "logs" / "shadow_usdc_scalp_scan_index.jsonl"
DEFAULT_LATEST_JSON = REPO_ROOT / "logs" / "shadow_usdc_scalp_latest.json"
DEFAULT_PID_FILE = REPO_ROOT / "logs" / "shadow_usdc_scalp.pid"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_slug(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        text = _iso_utc(_utc_now())
    return (
        text.replace(":", "")
        .replace("-", "")
        .replace("+00:00", "Z")
        .replace(".", "_")
    )


def _selector_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable or "python3",
        str(SCRIPT_DIR / "select_rotation_watchlist.py"),
        "--setup-mode",
        str(args.setup_mode),
        "--universe-source",
        "exchange",
        "--quote-asset",
        str(args.quote_asset).upper(),
        "--ignore-balances",
    ]
    if str(args.symbols or "").strip():
        cmd.extend(["--symbols", str(args.symbols).strip()])
    return cmd


def _run_selector(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    cmd = _selector_cmd(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"selector failed: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        excerpt = proc.stdout[:2000].strip()
        raise RuntimeError(f"selector returned invalid json: {excerpt}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("selector returned non-object payload")
    return payload, cmd


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _snapshot_path(snapshot_dir: Path, generated_at: str) -> Path:
    slug = _timestamp_slug(generated_at)
    return snapshot_dir / f"shadow_usdc_scalp_{slug}.json"


def _install_pid_file(path: Path) -> None:
    pid = os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")

    def _cleanup() -> None:
        try:
            if not path.exists():
                return
            if path.read_text(encoding="utf-8").strip() == str(pid):
                path.unlink()
        except Exception:
            pass

    atexit.register(_cleanup)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Periodically snapshot the full Binance USDC shadow universe with the "
            "existing scalp selector logic."
        )
    )
    parser.add_argument("--quote-asset", default="USDC")
    parser.add_argument("--setup-mode", default="trend", choices=("trend", "hybrid", "bottom_strict"))
    parser.add_argument("--symbols", default="", help="Optional comma-separated base symbols override.")
    parser.add_argument("--iterations", type=int, default=1, help="How many snapshots to collect.")
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=15.0,
        help="Sleep time between snapshots when iterations > 1.",
    )
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--index-jsonl", default=str(DEFAULT_INDEX_JSONL))
    parser.add_argument("--latest-json", default=str(DEFAULT_LATEST_JSON))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_FILE))
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep collecting later snapshots after a failed selector run.",
    )
    args = parser.parse_args()

    snapshot_dir = Path(str(args.snapshot_dir)).resolve()
    index_jsonl = Path(str(args.index_jsonl)).resolve()
    latest_json = Path(str(args.latest_json)).resolve()
    pid_file = Path(str(args.pid_file)).resolve()
    total = max(1, int(args.iterations))
    sleep_seconds = max(0.0, float(args.interval_minutes)) * 60.0
    _install_pid_file(pid_file)

    for iteration in range(1, total + 1):
        started_at = _utc_now()
        try:
            payload, cmd = _run_selector(args)
            generated_at = str(payload.get("generated_at") or _iso_utc(started_at))
            rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
            strategy_rankings = annotate_rows_with_strategy_views(rows)
            payload["strategy_rankings"] = serialize_strategy_rankings(strategy_rankings, limit=20)
            payload["strategy_candidate_counts"] = {
                strategy: len(strategy_rankings.get(strategy, []))
                for strategy in STRATEGY_NAMES
            }
            payload["strategy_top_symbols"] = {
                strategy: [
                    str(row.get("symbol", "")).upper()
                    for row in strategy_rankings.get(strategy, [])[:8]
                ]
                for strategy in STRATEGY_NAMES
            }
            eligible_count = sum(1 for row in rows if isinstance(row, dict) and bool(row.get("eligible")))
            trend_count = sum(
                1 for row in rows if isinstance(row, dict) and bool(row.get("trend_candidate"))
            )
            snapshot_path = _snapshot_path(snapshot_dir, generated_at)
            _write_json(snapshot_path, payload)
            _write_json(latest_json, payload)
            record = {
                "ok": True,
                "iteration": iteration,
                "iterations": total,
                "started_at": _iso_utc(started_at),
                "generated_at": generated_at,
                "snapshot_path": str(snapshot_path),
                "latest_json": str(latest_json),
                "selector_cmd": cmd,
                "candidate_source": payload.get("candidate_source"),
                "candidate_count": int(payload.get("candidate_count", len(rows)) or 0),
                "rows": len(rows),
                "eligible_rows": eligible_count,
                "trend_candidate_rows": trend_count,
                "errors_total": int(payload.get("errors_total", 0) or 0),
                "rate_limit_detected": bool(payload.get("rate_limit_detected")),
            }
            _append_jsonl(index_jsonl, record)
            print(json.dumps(record, ensure_ascii=True))
        except Exception as exc:
            error_text = str(exc).strip() or repr(exc)
            record = {
                "ok": False,
                "iteration": iteration,
                "iterations": total,
                "started_at": _iso_utc(started_at),
                "error": error_text,
            }
            _append_jsonl(index_jsonl, record)
            print(json.dumps(record, ensure_ascii=True), file=sys.stderr)
            if not bool(args.continue_on_error):
                raise SystemExit(error_text)

        if iteration < total and sleep_seconds > 0.0:
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
