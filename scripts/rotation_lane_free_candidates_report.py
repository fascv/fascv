#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JSONL = REPO_ROOT / "logs" / "rotation_lane_free_candidates.jsonl"


def _parse_iso(ts: str) -> datetime | None:
    text = str(ts or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize lane-free candidate history snapshots."
    )
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    parser.add_argument(
        "--hours",
        type=float,
        default=0.0,
        help="Only include snapshots from the last N hours (0 = all).",
    )
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    path = Path(str(args.jsonl)).resolve()
    records = [rec for rec in _load_records(path) if bool(rec.get("ok", False))]

    if float(args.hours) > 0.0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=float(args.hours))
        filtered: list[dict[str, Any]] = []
        for rec in records:
            ts = _parse_iso(str(rec.get("ts", "")))
            if ts is None:
                continue
            if ts.astimezone(timezone.utc) >= cutoff:
                filtered.append(rec)
        records = filtered

    if not records:
        print(
            json.dumps(
                {
                    "ok": False,
                    "records": 0,
                    "message": f"no records in {path}",
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return

    top_symbol_counter: Counter[str] = Counter()
    candidate_counter: Counter[str] = Counter()
    max_lane_free = 0
    first_ts = None
    last_ts = None

    for rec in records:
        ts_text = str(rec.get("ts", "") or "")
        if first_ts is None:
            first_ts = ts_text
        last_ts = ts_text
        lane_free_count = int(rec.get("lane_free_candidate_count", 0) or 0)
        max_lane_free = max(max_lane_free, lane_free_count)
        top = rec.get("lane_free_candidate_top")
        if isinstance(top, dict):
            top_symbol = str(top.get("symbol", "")).upper().strip()
            if top_symbol:
                top_symbol_counter[top_symbol] += 1
        items = rec.get("lane_free_candidates_top")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).upper().strip()
                if symbol:
                    candidate_counter[symbol] += 1

    payload = {
        "ok": True,
        "jsonl": str(path),
        "records": len(records),
        "window_first_ts": first_ts,
        "window_last_ts": last_ts,
        "max_lane_free_candidate_count": max_lane_free,
        "top_symbol_frequency": top_symbol_counter.most_common(max(1, int(args.top))),
        "candidate_presence_frequency": candidate_counter.most_common(max(1, int(args.top))),
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
