from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from queue import Empty
from typing import Any, Dict, Optional, Tuple

from trading.ipc.events import Heartbeat, JournalEvent, TelemetryEvent
from trading.ipc.queues import queue_depth, try_put
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


def _utc_ts_unix(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.timestamp()


def _safe_json_default(obj: Any) -> Any:
    # Keep it JSON-serializable without crashing the journal process.
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return {"__type__": "datetime", "value": obj.isoformat()}
    if isinstance(obj, Decimal):
        return {"__type__": "decimal", "value": str(obj)}
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        try:
            return {"__type__": "bytes", "value": bytes(obj).hex()}
        except Exception:
            return {"__type__": "bytes", "repr": repr(obj)}
    if isinstance(obj, Exception):
        return {"__type__": "exception", "repr": repr(obj)}
    if dataclasses.is_dataclass(obj):
        try:
            return dataclasses.asdict(obj)
        except Exception:
            return {"__type__": "dataclass", "repr": repr(obj)}
    return {"__type__": type(obj).__name__, "repr": repr(obj)}


def _safe_json_dumps(obj: Any) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Returns (json_str, meta_or_none). Never raises.
    """
    try:
        return (
            json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_safe_json_default),
            None,
        )
    except Exception as exc:
        meta: Dict[str, Any] = {
            "_journal_serialization_error": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            meta["traceback"] = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:]
        except Exception:
            pass
        # Best-effort fallback: stringify whole object.
        fallback = {"_journal_unserializable": True, "repr": repr(obj), "meta": meta}
        try:
            return (
                json.dumps(fallback, ensure_ascii=False, separators=(",", ":"), default=_safe_json_default),
                meta,
            )
        except Exception:
            return ('{"_journal_unserializable":true}', meta)


def _write_all(fd: int, data: bytes) -> None:
    # Short writes on regular files are rare but possible; handle them anyway.
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _truncate_to_last_newline(path: str) -> None:
    """
    Ensure the JSONL file ends at a newline boundary.
    This "heals" an interrupted/partial last line after crashes.
    """
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "rb+") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            if end <= 0:
                return
            fh.seek(end - 1)
            if fh.read(1) == b"\n":
                return

            # Scan backwards for the last newline.
            chunk = 8192
            pos = end
            while pos > 0:
                to_read = min(chunk, pos)
                pos -= to_read
                fh.seek(pos)
                buf = fh.read(to_read)
                idx = buf.rfind(b"\n")
                if idx >= 0:
                    fh.truncate(pos + idx + 1)
                    return

            # No newline at all -> truncate to empty.
            fh.truncate(0)
    except Exception:
        # Best-effort only; tail reader ignores broken lines anyway.
        return


class _JSONLWriter:
    def __init__(
        self,
        path: str,
        *,
        rotate_max_bytes: int = 0,
        rotate_keep: int = 0,
        fsync_on_flush: bool = True,
    ) -> None:
        self.path = path
        self.rotate_max_bytes = max(0, int(rotate_max_bytes or 0))
        self.rotate_keep = max(0, int(rotate_keep or 0))
        self.fsync_on_flush = bool(fsync_on_flush)
        self.fd: Optional[int] = None
        self.size_bytes = 0

    def open(self) -> None:
        dir_ = os.path.dirname(self.path)
        if dir_:
            os.makedirs(dir_, exist_ok=True)
        _truncate_to_last_newline(self.path)
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            self.size_bytes = int(os.fstat(self.fd).st_size)
        except Exception:
            self.size_bytes = 0

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
        self.fd = None

    def flush(self) -> None:
        if self.fd is None:
            return
        if self.fsync_on_flush:
            try:
                os.fsync(self.fd)
            except Exception:
                pass

    def _rotated_path(self, ts: datetime) -> str:
        root, ext = os.path.splitext(self.path)
        stamp = ts.strftime("%Y%m%d_%H%M%S")
        ext = ext or ".jsonl"
        rotated = f"{root}.{stamp}{ext}"
        if not os.path.exists(rotated):
            return rotated
        for i in range(1, 1000):
            cand = f"{root}.{stamp}.{i}{ext}"
            if not os.path.exists(cand):
                return cand
        return rotated

    def _apply_retention(self) -> None:
        if self.rotate_keep <= 0:
            return
        base = os.path.basename(self.path)
        root, ext = os.path.splitext(base)
        ext = ext or ".jsonl"
        dir_ = os.path.dirname(self.path) or "."
        prefix = f"{root}."
        try:
            items: list[str] = []
            for name in os.listdir(dir_):
                if name == base:
                    continue
                if name.startswith(prefix) and name.endswith(ext):
                    items.append(os.path.join(dir_, name))
            items.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for old in items[self.rotate_keep :]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            return

    def maybe_rotate(self, now_dt: datetime) -> bool:
        if self.rotate_max_bytes <= 0:
            return False
        if self.size_bytes < self.rotate_max_bytes:
            return False
        try:
            self.close()
            rotated = self._rotated_path(now_dt)
            os.replace(self.path, rotated)
            self._apply_retention()
        except Exception:
            # If rotation fails, keep writing to the same file.
            pass
        try:
            self.open()
            self.size_bytes = 0
        except Exception:
            pass
        return True

    def write_line(self, *, ts_iso: str, event_type: str, payload_json: str, now_dt: datetime) -> None:
        if self.fd is None:
            self.open()

        # Rotate before writing if we're already over the threshold.
        self.maybe_rotate(now_dt)

        # Keep the legacy "payload" field for compatibility, but also mirror it to
        # "data" so downstream readers can use one consistent key.
        ts_json = json.dumps(ts_iso, ensure_ascii=False, separators=(",", ":"))
        et_json = json.dumps(event_type, ensure_ascii=False, separators=(",", ":"))
        line = (
            f'{{"ts":{ts_json},"event_type":{et_json},'
            f'"payload":{payload_json},"data":{payload_json}}}'
        )
        data = (line + "\n").encode("utf-8", errors="replace")
        _write_all(self.fd, data)
        self.size_bytes += len(data)


def _sqlite_connect(db_path: str, *, busy_timeout_ms: int) -> sqlite3.Connection:
    dir_ = os.path.dirname(db_path)
    if dir_:
        os.makedirs(dir_, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute(f"PRAGMA busy_timeout={int(max(0, busy_timeout_ms))};")
    cur.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _sqlite_table_info(cur: sqlite3.Cursor, table: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        for cid, name, ctype, notnull, dflt, pk in cur.execute(f"PRAGMA table_info({table})"):
            out[str(name)] = {
                "cid": cid,
                "type": ctype,
                "notnull": notnull,
                "default": dflt,
                "pk": pk,
            }
    except Exception:
        pass
    return out


def _sqlite_init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            ts_unix REAL,
            event_type TEXT,
            payload_json TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_unix ON events (ts_unix)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_event_type_ts_unix ON events (event_type, ts_unix)"
    )
    conn.commit()


def _sqlite_migrate_if_needed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    info = _sqlite_table_info(cur, "events")
    if not info:
        _sqlite_init_schema(conn)
        return

    legacy_cols = set(info.keys())
    if legacy_cols == {"ts", "event_type", "payload"}:
        # Migrate legacy schema to the new one, keep the old table as backup.
        ts_suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        legacy_name = f"events_legacy_{ts_suffix}"
        cur.execute("BEGIN")
        cur.execute(f"ALTER TABLE events RENAME TO {legacy_name}")
        _sqlite_init_schema(conn)
        cur.execute(
            f"INSERT INTO events (ts, ts_unix, event_type, payload_json) "
            f"SELECT ts, NULL, event_type, payload FROM {legacy_name}"
        )
        conn.commit()
        return

    # Otherwise: ensure at least the expected columns exist (additive).
    expected = {
        "ts": "TEXT",
        "ts_unix": "REAL",
        "event_type": "TEXT",
        "payload_json": "TEXT",
    }
    for name, ctype in expected.items():
        if name not in info:
            try:
                cur.execute(f"ALTER TABLE events ADD COLUMN {name} {ctype}")
            except Exception:
                pass
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_unix ON events (ts_unix)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_event_type_ts_unix ON events (event_type, ts_unix)"
        )
    except Exception:
        pass
    conn.commit()


def run_journal(ctx: ProcessContext) -> None:
    cfg = ctx.config
    db_path = _cfg(cfg, "journal.db_path", "logs/journal.db")
    json_path = _cfg(cfg, "journal.json_path", "logs/journal_events.jsonl")

    flush_every_n = max(1, int(_cfg(cfg, "journal.flush_every_n", 100)))
    flush_every_sec = max(0.05, float(_cfg(cfg, "journal.flush_every_sec", 1.0)))

    rotate_max_bytes = int(_cfg(cfg, "journal.rotate_max_bytes", 0) or 0)
    rotate_keep = int(_cfg(cfg, "journal.rotate_keep", 0) or 0)
    fsync_on_flush = bool(_cfg(cfg, "journal.fsync_on_flush", True))

    writer = _JSONLWriter(
        json_path,
        rotate_max_bytes=rotate_max_bytes,
        rotate_keep=rotate_keep,
        fsync_on_flush=fsync_on_flush,
    )
    writer.open()

    conn: Optional[sqlite3.Connection] = None
    cur: Optional[sqlite3.Cursor] = None
    sqlite_errors_total = 0
    sqlite_busy_timeout_ms = int(_cfg(cfg, "journal.sqlite_busy_timeout_ms", 5000))
    sqlite_reopen_backoff_sec = float(_cfg(cfg, "journal.sqlite_reopen_backoff_sec", 2.0))
    next_sqlite_reopen = 0.0

    hb_seq = 0
    last_heartbeat = 0.0
    heartbeat_interval = max(0.25, float(_cfg(cfg, "journal.heartbeat_interval", 2.0)))

    telemetry_interval = max(0.5, float(_cfg(cfg, "journal.telemetry_interval", 2.0)))
    last_telemetry = 0.0

    json_errors_total = 0
    events_written_total = 0
    last_eps_ts = time.time()
    last_eps_count = 0

    buffer_count = 0
    last_flush = time.time()
    db_buffer: list[Tuple[str, float, str, str]] = []

    def _sqlite_open_if_needed(now: float) -> None:
        nonlocal conn, cur, next_sqlite_reopen, sqlite_errors_total
        if conn is not None and cur is not None:
            return
        if now < next_sqlite_reopen:
            return
        try:
            conn = _sqlite_connect(db_path, busy_timeout_ms=sqlite_busy_timeout_ms)
            _sqlite_migrate_if_needed(conn)
            cur = conn.cursor()
        except Exception:
            sqlite_errors_total += 1
            conn = None
            cur = None
            next_sqlite_reopen = now + max(0.25, sqlite_reopen_backoff_sec)

    def _flush(now: float) -> None:
        nonlocal conn, cur, next_sqlite_reopen, sqlite_errors_total
        nonlocal buffer_count, last_flush, db_buffer

        if buffer_count <= 0:
            return
        if buffer_count < flush_every_n and (now - last_flush) < flush_every_sec:
            return

        try:
            writer.flush()
        except Exception:
            pass

        if db_buffer and conn is not None and cur is not None:
            try:
                cur.execute("BEGIN")
                cur.executemany(
                    "INSERT INTO events (ts, ts_unix, event_type, payload_json) VALUES (?, ?, ?, ?)",
                    db_buffer,
                )
                conn.commit()
                db_buffer.clear()
            except sqlite3.Error:
                sqlite_errors_total += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                cur = None
                next_sqlite_reopen = now + max(0.25, sqlite_reopen_backoff_sec)

        buffer_count = 0
        last_flush = now

    while not ctx.stop_event.is_set():
        try:
            evt: JournalEvent = ctx.q_journal.get(timeout=0.5)
        except Empty:
            evt = None

        now = time.time()
        _sqlite_open_if_needed(now)

        if evt is not None:
            ts_iso = evt.ts.isoformat()
            ts_unix = _utc_ts_unix(evt.ts)

            # SQLite payload: always store a JSON string (safe).
            payload_json, payload_meta = _safe_json_dumps(evt.payload)
            if payload_meta is not None:
                try:
                    if isinstance(evt.payload, dict):
                        patched_payload: Dict[str, Any] = dict(evt.payload)
                    else:
                        patched_payload = {"value": evt.payload}
                    patched_payload.setdefault("meta", {})
                    if isinstance(patched_payload["meta"], dict):
                        patched_payload["meta"].setdefault("_journal", {}).update(payload_meta)
                    payload_json, _ = _safe_json_dumps(patched_payload)
                except Exception:
                    pass

            # JSONL: preserve format {"ts","event_type","payload"} per line.
            try:
                writer.write_line(
                    ts_iso=ts_iso,
                    event_type=str(evt.event_type),
                    payload_json=payload_json,
                    now_dt=datetime.now(timezone.utc),
                )
            except Exception:
                json_errors_total += 1

            if conn is not None and cur is not None:
                db_buffer.append((ts_iso, ts_unix, str(evt.event_type), payload_json))

            buffer_count += 1
            events_written_total += 1

        _flush(now)

        if now - last_heartbeat >= heartbeat_interval:
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            last_heartbeat = now

        if now - last_telemetry >= telemetry_interval:
            now_dt = datetime.now(timezone.utc)
            dt = max(0.001, now - last_eps_ts)
            eps = (events_written_total - last_eps_count) / dt
            last_eps_ts = now
            last_eps_count = events_written_total

            def _fsize(path: str) -> int:
                try:
                    return int(os.path.getsize(path))
                except Exception:
                    return -1

            data = {
                "events_written_total": int(events_written_total),
                "events_per_sec": float(eps),
                "queue_journal_depth": int(queue_depth(ctx.q_journal)),
                "db_path": str(db_path),
                "json_path": str(json_path),
                "db_ok": bool(conn is not None and cur is not None),
                "sqlite_errors_total": int(sqlite_errors_total),
                "json_errors_total": int(json_errors_total),
                "db_size_bytes": _fsize(str(db_path)),
                "json_size_bytes": _fsize(str(json_path)),
                "rotate_max_bytes": int(max(0, rotate_max_bytes or 0)),
                "rotate_keep": int(max(0, rotate_keep or 0)),
            }
            try_put(ctx.q_telemetry, TelemetryEvent(ts=now_dt, process="journal", data=data))
            last_telemetry = now

    # Best-effort shutdown flush.
    try:
        if db_buffer and conn is not None and cur is not None:
            cur.execute("BEGIN")
            cur.executemany(
                "INSERT INTO events (ts, ts_unix, event_type, payload_json) VALUES (?, ?, ?, ?)",
                db_buffer,
            )
            conn.commit()
            db_buffer.clear()
    except Exception:
        pass
    try:
        writer.flush()
    except Exception:
        pass
    try:
        writer.close()
    except Exception:
        pass
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass
