#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
DEFAULT_OUT_JSONL = REPO_ROOT / "logs" / "rotation_lane_free_candidates.jsonl"
DEFAULT_LATEST_JSON = REPO_ROOT / "logs" / "rotation_lane_free_candidates_latest.json"
DEFAULT_PID_FILE = REPO_ROOT / "logs" / "rotation_lane_free_candidates.pid"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def _install_pid_file(path: Path) -> None:
    pid = os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        other = _read_pid(path)
        if other and other != pid and _pid_running(other):
            raise SystemExit(
                f"lane-free logger already running with pid={other}; "
                f"remove {path} only if stale"
            )
    path.write_text(f"{pid}\n", encoding="utf-8")

    def _cleanup() -> None:
        try:
            if not path.exists():
                return
            if _read_pid(path) == pid:
                path.unlink()
        except Exception:
            pass

    atexit.register(_cleanup)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _row_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("symbol", "")).upper(),
        "score": _safe_float(row.get("score"), 0.0),
        "setup_type": str(row.get("setup_type", "") or ""),
        "gate_reason": str(row.get("gate_reason", "") or ""),
        "strategy_primary": str(row.get("strategy_primary", "") or ""),
        "spread_bps": _safe_float(row.get("spread_bps"), 0.0),
        "ret15_bps": _safe_float(row.get("ret15_bps"), 0.0),
        "ret60_bps": _safe_float(row.get("ret60_bps"), 0.0),
        "rel15_bps": _safe_float(row.get("rel15_bps"), 0.0),
        "keep_open": bool(row.get("keep_open")),
        "eligible": bool(row.get("eligible")),
        "hard_excluded": bool(row.get("hard_excluded")),
        "auto_blacklisted": bool(row.get("auto_blacklisted")),
    }


def _eligible_lane_free_candidates(
    rows: list[dict[str, Any]],
    selected: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        if symbol in selected:
            continue
        if bool(row.get("hard_excluded", False)):
            continue
        if not bool(row.get("eligible", False)):
            continue
        # In simple-swing selector mode, rows can stay "eligible" from the base
        # selector but still fail the final entry rule (gate_reason starts with
        # "rule_"). For "would actually be tradable if a lane is free", keep
        # only rows that pass final rule gates.
        gate_reason = str(row.get("gate_reason", "") or "").strip()
        if gate_reason.startswith("rule_"):
            continue
        out.append(row)
    out.sort(key=lambda item: _safe_float(item.get("score"), 0.0), reverse=True)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously log which selector candidates would be tradable if one lane became free."
        )
    )
    parser.add_argument("--active-file", default=str(DEFAULT_ACTIVE_FILE))
    parser.add_argument("--out-jsonl", default=str(DEFAULT_OUT_JSONL))
    parser.add_argument("--latest-json", default=str(DEFAULT_LATEST_JSON))
    parser.add_argument("--pid-file", default=str(DEFAULT_PID_FILE))
    parser.add_argument("--interval-sec", type=float, default=30.0)
    parser.add_argument("--top", type=int, default=8, help="How many lane-free candidates to store per snapshot.")
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Number of snapshots to record (0 = run forever).",
    )
    parser.add_argument(
        "--record-unchanged",
        action="store_true",
        help="Also record snapshots when generated_at did not change.",
    )
    args = parser.parse_args()

    active_file = Path(str(args.active_file)).resolve()
    out_jsonl = Path(str(args.out_jsonl)).resolve()
    latest_json = Path(str(args.latest_json)).resolve()
    pid_file = Path(str(args.pid_file)).resolve()
    sleep_seconds = max(1.0, float(args.interval_sec))
    top_n = max(1, int(args.top))
    max_iterations = max(0, int(args.iterations))
    record_unchanged = bool(args.record_unchanged)

    _install_pid_file(pid_file)

    stop = False

    def _request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    seen = 0
    last_generated_at = ""

    while not stop:
        now_iso = _now_utc_iso()
        state = _load_json(active_file)
        if not state:
            record = {
                "ok": False,
                "ts": now_iso,
                "error": f"missing_or_invalid_active_file:{active_file}",
            }
            _append_jsonl(out_jsonl, record)
            _write_json(latest_json, record)
            print(json.dumps(record, ensure_ascii=True))
        else:
            generated_at = str(state.get("generated_at", "") or "")
            if record_unchanged or not generated_at or generated_at != last_generated_at:
                selected_list = [
                    str(item).upper().strip()
                    for item in (state.get("selected") or [])
                    if str(item).strip()
                ]
                selected_set = set(selected_list)
                all_rows_raw = state.get("all_rows")
                rows = [row for row in all_rows_raw if isinstance(row, dict)] if isinstance(all_rows_raw, list) else []
                if not rows:
                    rows_raw = state.get("rows")
                    rows = [row for row in rows_raw if isinstance(row, dict)] if isinstance(rows_raw, list) else []
                lane_free = _eligible_lane_free_candidates(rows, selected_set)
                top_candidates = [_row_view(row) for row in lane_free[:top_n]]
                record = {
                    "ok": True,
                    "ts": now_iso,
                    "source_generated_at": generated_at,
                    "selected": selected_list,
                    "selected_count": len(selected_list),
                    "active_candidate_count": int(state.get("active_candidate_count", 0) or 0),
                    "selection_relaxed": bool(state.get("selection_relaxed", False)),
                    "lane_free_candidate_count": len(lane_free),
                    "lane_free_candidate_top": (top_candidates[0] if top_candidates else None),
                    "lane_free_candidates_top": top_candidates,
                }
                _append_jsonl(out_jsonl, record)
                _write_json(latest_json, record)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "ts": now_iso,
                            "source_generated_at": generated_at,
                            "lane_free_candidate_count": len(lane_free),
                            "lane_free_top_symbol": (
                                top_candidates[0]["symbol"] if top_candidates else ""
                            ),
                        },
                        ensure_ascii=True,
                    )
                )
                last_generated_at = generated_at
                seen += 1

        if max_iterations > 0 and seen >= max_iterations:
            break
        for _ in range(int(sleep_seconds)):
            if stop:
                break
            time.sleep(1.0)
        fractional = sleep_seconds - int(sleep_seconds)
        if not stop and fractional > 0.0:
            time.sleep(fractional)


if __name__ == "__main__":
    main()
