from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from queue import Empty
from typing import Any, Dict

from trading.ipc.events import Heartbeat, JournalEvent, TelemetryEvent
from trading.ipc.queues import try_put
from trading.processes.context import ProcessContext


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="journal", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def run_journal(ctx: ProcessContext) -> None:
    cfg = ctx.config
    db_path = _cfg(cfg, "journal.db_path", "logs/journal.db")
    json_path = _cfg(cfg, "journal.json_path", "logs/journal_events.jsonl")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS events (ts TEXT, event_type TEXT, payload TEXT)"
    )

    json_fh = open(json_path, "a", encoding="utf-8")

    hb_seq = 0
    last_heartbeat = 0.0
    heartbeat_interval = float(_cfg(cfg, "journal.heartbeat_interval", 2.0))

    buffer_count = 0
    last_flush = time.time()

    while not ctx.stop_event.is_set():
        try:
            evt: JournalEvent = ctx.q_journal.get(timeout=0.5)
        except Empty:
            evt = None

        if evt is not None:
            payload = json.dumps(evt.payload)
            cur.execute("INSERT INTO events (ts, event_type, payload) VALUES (?, ?, ?)", (evt.ts.isoformat(), evt.event_type, payload))
            json_fh.write(json.dumps({"ts": evt.ts.isoformat(), "event_type": evt.event_type, "payload": evt.payload}) + "\n")
            buffer_count += 1

        now = time.time()
        if buffer_count >= 100 or (now - last_flush) > 1.0:
            json_fh.flush()
            buffer_count = 0
            last_flush = now

        if now - last_heartbeat >= heartbeat_interval:
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            last_heartbeat = now

    json_fh.flush()
    json_fh.close()
    conn.close()
