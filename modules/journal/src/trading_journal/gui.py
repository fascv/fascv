from __future__ import annotations

import argparse
import json
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_iso_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _jsonl_ends_with_newline(path: str) -> Optional[bool]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            n = fh.tell()
            if n <= 0:
                return True
            fh.seek(n - 1)
            return fh.read(1) == b"\n"
    except Exception:
        return None


def _tail_lines_from_end(
    path: str,
    lines: int,
    *,
    chunk_size: int = 8192,
    max_bytes: int | None = None,
) -> List[str]:
    lines = max(0, int(lines))
    if lines <= 0:
        return []
    if not path or not os.path.exists(path):
        return []

    collected: List[bytes] = []
    pending = b""
    total_read = 0

    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()

        while pos > 0 and len(collected) < lines and (max_bytes is None or total_read < max_bytes):
            to_read = min(chunk_size, pos)
            pos -= to_read
            fh.seek(pos)
            chunk = fh.read(to_read)
            total_read += len(chunk)
            data = chunk + pending

            parts = data.split(b"\n")
            pending = parts[0]
            complete = parts[1:]

            for part in reversed(complete):
                if part.strip():
                    collected.append(part)
                if len(collected) >= lines:
                    break

        if len(collected) < lines and pending.strip():
            collected.append(pending)

    collected = list(reversed(collected[:lines]))
    return [b.decode("utf-8", errors="replace") for b in collected]


def tail_jsonl(
    path: str,
    lines: int,
    *,
    max_bytes: int | None = None,
    event_type_contains: str | None = None,
) -> Dict[str, Any]:
    """
    Fast tail for JSONL where each line should be a JSON object.
    Returns a JSON-serializable dict.
    """
    raw = _tail_lines_from_end(path, lines, max_bytes=max_bytes)
    items: List[Dict[str, Any]] = []
    parse_errors = 0
    f = str(event_type_contains or "").strip()

    for ln in raw:
        text = ln.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except Exception:
            parse_errors += 1
            continue
        if not isinstance(obj, dict):
            continue
        if f:
            et = str(obj.get("event_type", ""))
            if f not in et:
                continue
        items.append(obj)

    return {
        "path": path,
        "lines_requested": int(lines),
        "lines_sampled": int(len(raw)),
        "parse_errors_in_sample": int(parse_errors),
        "items": items,
    }


def list_rotated_jsonl_files(json_path: str) -> List[Dict[str, Any]]:
    if not json_path:
        return []
    dir_ = os.path.dirname(json_path) or "."
    base = os.path.basename(json_path)
    root, ext = os.path.splitext(base)
    ext = ext or ".jsonl"
    prefix = root + "."

    out: List[Dict[str, Any]] = []
    try:
        for name in os.listdir(dir_):
            if name == base:
                continue
            if not name.startswith(prefix):
                continue
            if not name.endswith(ext):
                continue
            path = os.path.join(dir_, name)
            try:
                st = os.stat(path)
                out.append(
                    {
                        "name": name,
                        "path": path,
                        "size_bytes": int(st.st_size),
                        "mtime": float(st.st_mtime),
                    }
                )
            except Exception:
                out.append({"name": name, "path": path, "size_bytes": -1, "mtime": None})
    except Exception:
        return []

    out.sort(key=lambda it: (it.get("mtime") or 0.0), reverse=True)
    return out


def _sqlite_connect_ro(db_path: str) -> sqlite3.Connection:
    # Open read-only to avoid interacting with the writer. (Busy locks can still happen.)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("PRAGMA query_only=1;")
    cur.execute("PRAGMA busy_timeout=250;")
    return conn


def sqlite_schema_snapshot(db_path: str) -> Dict[str, Any]:
    if not db_path or not os.path.exists(db_path):
        return {"db_path": db_path, "db_ok": False, "error": "db_missing", "columns": [], "indexes": []}
    try:
        conn = _sqlite_connect_ro(db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(events)")
        cols = [{"name": r[1], "type": r[2], "pk": int(r[5] or 0)} for r in cur.fetchall()]
        cur.execute("PRAGMA index_list(events)")
        idxs = [{"name": r[1], "unique": int(r[2] or 0)} for r in cur.fetchall()]
        conn.close()
        return {"db_path": db_path, "db_ok": True, "columns": cols, "indexes": idxs}
    except Exception as exc:
        return {
            "db_path": db_path,
            "db_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "columns": [],
            "indexes": [],
        }


def sqlite_recent_rows_snapshot(db_path: str, limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(2000, int(limit)))
    if not db_path or not os.path.exists(db_path):
        return {"db_path": db_path, "db_ok": False, "error": "db_missing", "rows": [], "max_id": None}
    try:
        conn = _sqlite_connect_ro(db_path)
        cur = conn.cursor()
        cur.execute("SELECT MAX(id) FROM events")
        (max_id,) = cur.fetchone() or (None,)
        cur.execute(
            "SELECT id, ts, ts_unix, event_type, payload_json FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows_raw = cur.fetchall()
        conn.close()

        rows: List[Dict[str, Any]] = []
        for rid, ts, ts_unix, et, payload_json in rows_raw:
            payload_prev = payload_json if isinstance(payload_json, str) else ""
            if len(payload_prev) > 240:
                payload_prev = payload_prev[:240] + "..."
            rows.append(
                {
                    "id": int(rid),
                    "ts": ts,
                    "ts_unix": ts_unix,
                    "event_type": et,
                    "payload_json_preview": payload_prev,
                }
            )
        return {"db_path": db_path, "db_ok": True, "rows": rows, "max_id": max_id}
    except Exception as exc:
        return {
            "db_path": db_path,
            "db_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "rows": [],
            "max_id": None,
        }


def build_summary(
    *,
    json_path: str,
    db_path: str,
    lines: int,
    max_bytes: int | None,
    event_type_contains: str | None = None,
) -> Dict[str, Any]:
    now = _now()

    def _stat(path: str) -> Dict[str, Any]:
        if not path:
            return {"path": path, "exists": False}
        if not os.path.exists(path):
            return {"path": path, "exists": False}
        try:
            st = os.stat(path)
            return {"path": path, "exists": True, "size_bytes": int(st.st_size), "mtime": float(st.st_mtime)}
        except Exception:
            return {"path": path, "exists": True, "size_bytes": -1, "mtime": None}

    json_tail = tail_jsonl(
        json_path,
        lines,
        max_bytes=max_bytes,
        event_type_contains=event_type_contains,
    )
    last_event = (json_tail.get("items") or [])[-1] if (json_tail.get("items") or []) else None
    last_ts = _parse_iso_ts(last_event.get("ts")) if isinstance(last_event, dict) else None
    tail_age_sec = (now - last_ts).total_seconds() if last_ts else None

    warnings: List[Dict[str, str]] = []
    ends_nl = _jsonl_ends_with_newline(json_path)
    if ends_nl is False:
        warnings.append({"level": "warn", "code": "jsonl_not_newline_terminated", "message": "JSONL does not end with newline."})

    schema = sqlite_schema_snapshot(db_path)
    expected_cols = {"id", "ts", "ts_unix", "event_type", "payload_json"}
    if schema.get("db_ok"):
        cols = {c.get("name") for c in (schema.get("columns") or []) if isinstance(c, dict)}
        missing = sorted([c for c in expected_cols if c not in cols])
        if missing:
            warnings.append(
                {
                    "level": "warn",
                    "code": "sqlite_schema_missing_columns",
                    "message": "Missing columns in events: " + ",".join(missing),
                }
            )
    else:
        if schema.get("error") and schema.get("error") != "db_missing":
            warnings.append({"level": "warn", "code": "sqlite_unreadable", "message": str(schema.get("error"))})

    return {
        "generated_at": now.isoformat(),
        "jsonl": {
            **_stat(json_path),
            "ends_with_newline": ends_nl,
            "tail_age_sec": tail_age_sec,
            "parse_errors_in_sample": int(json_tail.get("parse_errors_in_sample") or 0),
        },
        "sqlite": {
            **_stat(db_path),
            "db_ok": bool(schema.get("db_ok")),
        },
        "rotation": {"files": list_rotated_jsonl_files(json_path)[:20]},
        "warnings": warnings,
    }


@dataclass(frozen=True)
class JournalGuiConfig:
    json_path: str
    db_path: str
    default_lines: int
    max_bytes: int | None


def _make_handler(cfg: JournalGuiConfig):
    class JournalGuiHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, payload: str) -> None:
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_HTML)
                return

            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/summary":
                lines = max(10, min(2000, _safe_int(query.get("lines", [str(cfg.default_lines)])[0], cfg.default_lines)))
                et = str(query.get("event_type_contains", [""])[0] or "").strip() or None
                payload = build_summary(
                    json_path=cfg.json_path,
                    db_path=cfg.db_path,
                    lines=lines,
                    max_bytes=cfg.max_bytes,
                    event_type_contains=et,
                )
                self._send_json({"ok": True, "summary": payload})
                return

            if parsed.path == "/api/tail":
                lines = max(10, min(2000, _safe_int(query.get("lines", [str(cfg.default_lines)])[0], cfg.default_lines)))
                et = str(query.get("event_type_contains", [""])[0] or "").strip() or None
                tail = tail_jsonl(cfg.json_path, lines, max_bytes=cfg.max_bytes, event_type_contains=et)
                self._send_json({"ok": True, "tail": tail})
                return

            if parsed.path == "/api/sql/schema":
                snap = sqlite_schema_snapshot(cfg.db_path)
                self._send_json({"ok": True, "schema": snap})
                return

            if parsed.path == "/api/sql/recent":
                limit = max(1, min(2000, _safe_int(query.get("limit", ["100"])[0], 100)))
                snap = sqlite_recent_rows_snapshot(cfg.db_path, limit=limit)
                self._send_json({"ok": True, "recent": snap})
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return JournalGuiHandler


def run_journal_gui(
    *,
    host: str = "127.0.0.1",
    port: int = 8120,
    json_path: str = "logs/journal_events.jsonl",
    db_path: str = "logs/journal.db",
    lines: int = 400,
    max_bytes: int | None = 2_000_000,
) -> None:
    handler_cls = _make_handler(
        JournalGuiConfig(
            json_path=str(json_path),
            db_path=str(db_path),
            default_lines=int(lines),
            max_bytes=max_bytes,
        )
    )
    server = ThreadingHTTPServer((host, int(port)), handler_cls)
    print(f"journal gui listening on http://{host}:{port} (jsonl={json_path} db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Journal-only GUI for persistence/integrity inspection")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8120)
    parser.add_argument("--json-path", default="logs/journal_events.jsonl")
    parser.add_argument("--db-path", default="logs/journal.db")
    parser.add_argument("--lines", type=int, default=400)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    args = parser.parse_args()
    run_journal_gui(
        host=args.host,
        port=args.port,
        json_path=args.json_path,
        db_path=args.db_path,
        lines=args.lines,
        max_bytes=args.max_bytes,
    )


_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Journal Module GUI</title>
  <style>
    :root {
      --bg:#f5f2ea;
      --fg:#1c1c1c;
      --card:#fffdf8;
      --line:#d7d0c0;
      --accent:#215a8a;
      --warn:#b23a48;
      --ok:#2c6e49;
    }
    body { margin:0; font-family:"IBM Plex Sans", "Segoe UI", sans-serif; background:radial-gradient(circle at 15% 0%, #fffdf8, #eee6d5); color:var(--fg); }
    .wrap { max-width:1200px; margin:18px auto; padding:0 12px; }
    h1 { margin:0 0 8px; font-size:24px; letter-spacing:.02em; }
    .small { font-size:12px; color:#5f5f5f; }
    .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); margin-top:10px; }
    .card { border:1px solid var(--line); background:var(--card); border-radius:10px; padding:10px; }
    h2 { margin:0 0 6px; font-size:12px; text-transform:uppercase; color:#5a5a5a; }
    .v { font-weight:700; font-size:16px; }
    .mono { font-family:ui-monospace, Menlo, Consolas, monospace; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; margin:4px 6px 0 0; background:#faf5ea; }
    .warn { color:var(--warn); font-weight:700; }
    .ok { color:var(--ok); font-weight:700; }
    input { border:1px solid var(--line); border-radius:8px; padding:6px 8px; background:#fff; }
    button { border:1px solid var(--line); border-radius:8px; padding:6px 10px; background:#f7f2e7; cursor:pointer; }
    button:hover { background:#f1eadb; }
    pre { margin:0; white-space:pre-wrap; word-break:break-word; }
    table { width:100%; border-collapse:collapse; font-size:13px; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
    th, td { border-bottom:1px solid #ece7d8; padding:7px 8px; text-align:left; vertical-align:top; }
    th { background:#f7f2e7; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Journal Integrity Inspector</h1>
    <div class="small">Liest direkt von Disk: <span class="mono">journal_events.jsonl</span> + <span class="mono">journal.db</span></div>
    <div class="small">Refresh: Polling alle ~1.2s</div>

    <div class="grid">
      <div class="card"><h2>JSONL Path</h2><div id="json_path" class="v mono">-</div><div id="json_meta" class="small">-</div></div>
      <div class="card"><h2>JSONL Tail Age</h2><div id="tail_age" class="v">-</div><div id="json_parse" class="small">-</div></div>
      <div class="card"><h2>SQLite Path</h2><div id="db_path" class="v mono">-</div><div id="db_meta" class="small">-</div></div>
      <div class="card"><h2>Rotation Files</h2><div id="rot_count" class="v">-</div><div id="rot_hint" class="small">-</div></div>
    </div>

    <div class="card" style="margin-top:10px;">
      <h2>Warnings</h2>
      <div id="warnings"></div>
    </div>

    <div class="card" style="margin-top:10px;">
      <h2>Tail Controls</h2>
      <div class="small">
        <label>lines <input id="lines" type="number" min="10" max="2000" value="400" /></label>
        <label style="margin-left:10px;">event_type contains <input id="filter" type="text" placeholder="exec_" /></label>
        <button style="margin-left:10px;" onclick="refresh(true)">Refresh now</button>
      </div>
    </div>

    <div class="grid" style="margin-top:10px;">
      <div class="card" style="grid-column:1/-1;">
        <h2>JSONL Tail (parseable items)</h2>
        <pre id="tail" class="mono"></pre>
      </div>
    </div>

    <div class="grid" style="margin-top:10px;">
      <div class="card" style="grid-column:1/-1;">
        <h2>SQLite Recent Rows</h2>
        <table>
          <thead><tr><th>id</th><th>ts</th><th>event_type</th><th>payload_json (preview)</th></tr></thead>
          <tbody id="db_rows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    function txt(v) { return (v===undefined || v===null || v==="") ? "-" : String(v); }
    function esc(v) { return txt(v).replace(/[&<>]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[s])); }
    function isIsoTs(v) { return (typeof v === "string") && /^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}/.test(v); }
    function fmtTs(raw) {
      if (!raw) return "-";
      let s = String(raw).trim();
      s = s.replace(/\\.(\\d{3})\\d+(Z|[+-]\\d\\d:\\d\\d)$/, ".$1$2");
      if (s.endsWith("Z")) s = s.slice(0, -1) + "+00:00";
      const d = new Date(s);
      if (Number.isNaN(d.getTime())) return String(raw);
      const dd = String(d.getUTCDate()).padStart(2, "0");
      const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
      const yy = String(d.getUTCFullYear()).slice(-2);
      const hh = String(d.getUTCHours()).padStart(2, "0");
      const mi = String(d.getUTCMinutes()).padStart(2, "0");
      const ss = String(d.getUTCSeconds()).padStart(2, "0");
      return `${dd}-${mm}-${yy} T ${hh}:${mi}:${ss}`;
    }
    function stringifyDe(obj) {
      return JSON.stringify(
        obj || {},
        (k, v) => (isIsoTs(v) ? fmtTs(v) : v),
        2,
      );
    }
    function fmtBytes(n) {
      n = Number(n);
      if (!isFinite(n) || n < 0) return "-";
      const u = ["B","KB","MB","GB"];
      let i=0; while (n>=1024 && i<u.length-1) { n/=1024; i++; }
      return n.toFixed(i?1:0) + " " + u[i];
    }
    function fmtAge(sec) {
      sec = Number(sec);
      if (!isFinite(sec) || sec < 0) return "-";
      if (sec < 2) return sec.toFixed(2) + "s";
      if (sec < 90) return sec.toFixed(1) + "s";
      return Math.round(sec) + "s";
    }
    function pill(w) {
      const cls = (w.level === "warn") ? "warn" : "ok";
      return `<span class="pill ${cls}">${esc(w.code)}: ${esc(w.message)}</span>`;
    }

    async function refresh(force) {
      const lines = Math.max(10, Math.min(2000, parseInt(document.getElementById("lines").value || "400")));
      const f = (document.getElementById("filter").value || "").trim();
      const qs = new URLSearchParams({lines: String(lines), event_type_contains: f});

      const sum = await (await fetch("/api/summary?" + qs.toString())).json();
      if (!sum.ok) throw new Error("summary failed");
      const s = sum.summary;

      document.getElementById("json_path").textContent = txt((s.jsonl||{}).path);
      document.getElementById("db_path").textContent = txt((s.sqlite||{}).path);

      const jm = s.jsonl || {};
      const ends = jm.ends_with_newline;
      const endsTxt = (ends === true) ? "ends_with_newline=true" : (ends === false ? "ends_with_newline=false" : "ends_with_newline=?");
      document.getElementById("json_meta").textContent = `exists=${txt(jm.exists)} size=${fmtBytes(jm.size_bytes)} ${endsTxt}`;
      document.getElementById("tail_age").textContent = fmtAge(jm.tail_age_sec);
      document.getElementById("json_parse").textContent = `parse_errors_in_sample=${txt(jm.parse_errors_in_sample)}`;

      const dm = s.sqlite || {};
      document.getElementById("db_meta").textContent = `exists=${txt(dm.exists)} size=${fmtBytes(dm.size_bytes)} db_ok=${txt(dm.db_ok)}`;

      const rot = (s.rotation||{}).files || [];
      document.getElementById("rot_count").textContent = String(rot.length);
      document.getElementById("rot_hint").textContent = rot.length ? `latest=${rot[0].name} (${fmtBytes(rot[0].size_bytes)})` : "no rotated files found";

      const warnings = s.warnings || [];
      document.getElementById("warnings").innerHTML = warnings.length
        ? warnings.map(pill).join("")
        : `<span class="ok">Keine Warnings aus den aktuellen Checks.</span>`;

      const tail = await (await fetch("/api/tail?" + qs.toString())).json();
      if (!tail.ok) throw new Error("tail failed");
      document.getElementById("tail").textContent = stringifyDe((tail.tail||{}).items || []);

      const recent = await (await fetch("/api/sql/recent?limit=80")).json();
      if (!recent.ok) throw new Error("sql recent failed");
      const rows = ((recent.recent||{}).rows || []);
      document.getElementById("db_rows").innerHTML = rows.map(r => `
        <tr>
          <td class="mono">${esc(r.id)}</td>
          <td class="mono">${esc(fmtTs(r.ts))}</td>
          <td>${esc(r.event_type)}</td>
          <td class="mono">${esc(r.payload_json_preview)}</td>
        </tr>
      `).join("");
    }

    async function loop() {
      try { await refresh(false); } catch (e) { console.error(e); }
      setTimeout(loop, 1200);
    }
    loop();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
