#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import yaml


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(raw or "").strip()).strip("_").lower()
    return s or "run"


def _summarize_journal(path: Path, *, starting_cash: float) -> Dict[str, Any]:
    counts = Counter()
    strategies = Counter()
    regimes = Counter()
    fills = []
    first_ts = None
    last_ts = None

    if not path.exists():
        return {
            "events": 0,
            "decisions": 0,
            "fills": 0,
            "buys": 0,
            "sells": 0,
            "fees_eur": 0.0,
            "pnl_eur": 0.0,
            "pnl_pct": 0.0,
            "open_position_btc": 0.0,
            "strategy_counts": {},
            "regime_counts": {},
            "first_ts": None,
            "last_ts": None,
        }

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            ts = evt.get("ts")
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            et = str(evt.get("event_type", ""))
            counts[et] += 1
            if et == "core_decision":
                meta = (((evt.get("payload") or {}).get("alpha") or {}).get("meta") or {})
                s = meta.get("active_strategy")
                r = meta.get("regime")
                if s:
                    strategies[str(s)] += 1
                if r:
                    regimes[str(r)] += 1
            elif et == "fill":
                fills.append(evt.get("payload") or {})

    cash = float(starting_cash)
    pos = 0.0
    fees = 0.0
    buys = 0
    sells = 0
    last_close = 0.0

    # Need last close for MTM.
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if '"event_type":"market"' in line:
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                last_close = float(((evt.get("payload") or {}).get("close")) or last_close or 0.0)

    for fl in fills:
        side = str(fl.get("side", "")).lower()
        qty = float(fl.get("qty_btc", 0.0) or 0.0)
        px = float(fl.get("price", 0.0) or 0.0)
        fee = float(fl.get("fee_eur", 0.0) or 0.0)
        nt = qty * px
        fees += fee
        if side == "buy":
            buys += 1
            cash -= nt + fee
            pos += qty
        elif side == "sell":
            sells += 1
            cash += nt - fee
            pos -= qty

    equity = cash + pos * last_close
    pnl = equity - float(starting_cash)
    pnl_pct = (pnl / float(starting_cash) * 100.0) if starting_cash > 0 else 0.0

    return {
        "events": sum(counts.values()),
        "decisions": int(counts.get("core_decision", 0)),
        "fills": int(counts.get("fill", 0)),
        "buys": buys,
        "sells": sells,
        "fees_eur": round(fees, 6),
        "pnl_eur": round(pnl, 6),
        "pnl_pct": round(pnl_pct, 6),
        "open_position_btc": round(pos, 12),
        "strategy_counts": dict(strategies),
        "regime_counts": dict(regimes),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run long sim-auto session with periodic checkpoints.")
    parser.add_argument("--config", default="configs/sim_auto.yaml")
    parser.add_argument("--duration-sec", type=int, default=21600)
    parser.add_argument("--checkpoint-sec", type=int, default=1800)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--disable-control", action="store_true", default=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    cfg_path = root / str(args.config)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    cfg_tag = _slug(Path(str(args.config)).stem)
    run_id = f"sim_auto_long_{cfg_tag}_{stamp}"

    journal_json = root / f"logs/journal_{run_id}.jsonl"
    journal_db = root / f"logs/journal_{run_id}.db"
    checkpoints_path = root / f"logs/{run_id}_checkpoints.jsonl"
    summary_path = root / f"logs/{run_id}_summary.json"
    run_log = root / f"logs/{run_id}_runner.log"
    resolved_cfg = Path(f"/tmp/{run_id}.yaml")

    cfg.setdefault("journal", {})
    cfg["journal"]["json_path"] = str(journal_json.relative_to(root))
    cfg["journal"]["db_path"] = str(journal_db.relative_to(root))
    if args.disable_control:
        cfg.setdefault("control", {})
        cfg["control"]["enabled"] = False

    resolved_cfg.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    starting_cash = float((cfg.get("general") or {}).get("starting_cash_eur", 150.0) or 150.0)
    cmd = [
        "timeout",
        str(int(args.duration_sec)),
        "python3",
        "-m",
        "trading.launch",
        "--mode",
        "sim",
        "--config",
        str(resolved_cfg),
        "--env",
        str(args.env),
    ]

    with run_log.open("w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=lf, stderr=lf)

    meta = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "config_source": str(cfg_path),
        "config_resolved": str(resolved_cfg),
        "journal_json": str(journal_json),
        "journal_db": str(journal_db),
        "checkpoints_jsonl": str(checkpoints_path),
        "summary_json": str(summary_path),
        "runner_log": str(run_log),
        "pid": int(proc.pid),
        "duration_sec": int(args.duration_sec),
        "checkpoint_sec": int(args.checkpoint_sec),
    }
    print(json.dumps({"phase": "started", **meta}, indent=2), flush=True)

    next_cp = time.time() + int(args.checkpoint_sec)
    while True:
        rc = proc.poll()
        now = time.time()
        if now >= next_cp or rc is not None:
            summary = _summarize_journal(journal_json, starting_cash=starting_cash)
            payload = {
                "ts": _now_iso(),
                "phase": "checkpoint" if rc is None else "final",
                "run_id": run_id,
                "pid": int(proc.pid),
                "exit_code": rc,
                "summary": summary,
            }
            with checkpoints_path.open("a", encoding="utf-8") as cp:
                cp.write(json.dumps(payload, ensure_ascii=True) + "\n")
            print(json.dumps(payload, ensure_ascii=True), flush=True)
            next_cp = now + int(args.checkpoint_sec)
        if rc is not None:
            final = {
                "run_id": run_id,
                "finished_at": _now_iso(),
                "exit_code": int(rc),
                "summary": _summarize_journal(journal_json, starting_cash=starting_cash),
                "meta": meta,
            }
            summary_path.write_text(json.dumps(final, indent=2), encoding="utf-8")
            print(json.dumps({"phase": "saved", "summary_json": str(summary_path)}, ensure_ascii=True), flush=True)
            break
        time.sleep(5.0)


if __name__ == "__main__":
    main()
