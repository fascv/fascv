from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _parse_ts(raw: Any) -> Optional[datetime]:
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
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat()


def _join_url(base: str, path: str) -> str:
    b = (base or "").rstrip("/")
    p = (path or "").lstrip("/")
    return f"{b}/{p}"


def _fetch_json(url: str, timeout_sec: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "md-gui/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8", errors="replace"))


def _event_time(event: Dict[str, Any]) -> Optional[datetime]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_ts = _parse_ts(payload.get("ts"))
        if payload_ts:
            return payload_ts
    return _parse_ts(event.get("ts"))


def _iter_md_events(items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for item in items:
        if not isinstance(item, dict):
            continue
        et = str(item.get("event_type", ""))
        if et in {"md_start", "md_stop", "md_error", "md_stale"}:
            yield item


def _iter_market_events(items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("event_type", "")) != "market":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        # Market events from md process should contain OHLCV fields.
        if "close" not in payload or "ts" not in payload:
            continue
        yield item


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def build_md_snapshot(status_doc: Any, journal_items: Any) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    status = status_doc if isinstance(status_doc, dict) else {}
    data = status.get("data", {}) if isinstance(status.get("data"), dict) else {}
    md = data.get("md", {}) if isinstance(data.get("md"), dict) else {}
    staleness = status.get("staleness_sec_by_process", {}) if isinstance(status.get("staleness_sec_by_process"), dict) else {}

    items = journal_items if isinstance(journal_items, list) else []
    md_events = list(_iter_md_events(items))
    market_events = list(_iter_market_events(items))

    md_events.sort(key=lambda e: (_iso(_parse_ts(e.get("ts"))) or ""), reverse=True)
    # Market events should be sorted by the market timestamp (payload.ts).
    market_events.sort(key=lambda e: (_iso(_event_time(e)) or ""), reverse=True)

    last_market_evt = market_events[0] if market_events else None
    # Bar timestamp (from payload.ts) can be a bucket start; keep it for reference.
    last_market_bar_ts = _parse_ts(md.get("last_market_ts")) or (_event_time(last_market_evt) if last_market_evt else None)
    # Arrival timestamp is more meaningful for "age/staleness" in the UI.
    last_market_seen_ts = _parse_ts(md.get("last_market_arrival_ts"))
    if last_market_seen_ts is None and isinstance(last_market_evt, dict):
        last_market_seen_ts = _parse_ts(last_market_evt.get("ts"))
    last_market_age_sec: Optional[float] = None
    if last_market_seen_ts is not None:
        try:
            last_market_age_sec = max(0.0, (now - last_market_seen_ts).total_seconds())
        except Exception:
            last_market_age_sec = None

    # Build a compact market table (last N).
    market_rows: List[Dict[str, Any]] = []
    regressions = 0
    prev_ts: Optional[datetime] = None
    sample = list(reversed(market_events[:250]))  # oldest->newest for checks; slice keeps payload small
    for evt in sample:
        payload = evt.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ts = _parse_ts(payload.get("ts"))
        if ts is None:
            continue
        if prev_ts is not None and ts < prev_ts:
            regressions += 1
        prev_ts = ts
        micro = payload.get("micro") if isinstance(payload.get("micro"), dict) else {}
        market_rows.append(
            {
                "ts": _iso(ts),
                "open": _safe_float(payload.get("open")),
                "high": _safe_float(payload.get("high")),
                "low": _safe_float(payload.get("low")),
                "close": _safe_float(payload.get("close")),
                "volume": _safe_float(payload.get("volume")),
                "spread_bps": micro.get("spread_bps"),
                "depth": micro.get("depth"),
                "imbalance": micro.get("imbalance"),
                "seq": payload.get("seq"),
            }
        )
    market_rows.sort(key=lambda r: r.get("ts") or "", reverse=True)

    issues: List[Dict[str, str]] = []
    md_staleness = staleness.get("md")
    stale_warn_sec = status.get("stale_warn_sec")
    try:
        stale_warn = float(stale_warn_sec) if stale_warn_sec is not None else None
    except Exception:
        stale_warn = None

    if not md:
        issues.append({"level": "warn", "code": "no_md_telemetry", "message": "No md telemetry found in /status."})
    if last_market_bar_ts is None:
        issues.append({"level": "warn", "code": "no_last_market_ts", "message": "No last market timestamp available."})
    if stale_warn is not None and isinstance(md_staleness, (int, float)) and md_staleness > stale_warn:
        issues.append(
            {"level": "warn", "code": "md_telemetry_stale", "message": f"md telemetry staleness {md_staleness:.2f}s > warn {stale_warn:.2f}s."}
        )
    if stale_warn is not None and isinstance(last_market_age_sec, (int, float)) and last_market_age_sec > stale_warn:
        issues.append(
            {"level": "warn", "code": "market_age_high", "message": f"last market age {last_market_age_sec:.2f}s > warn {stale_warn:.2f}s."}
        )
    if regressions > 0:
        issues.append(
            {"level": "warn", "code": "market_ts_regressions", "message": f"Detected {regressions} timestamp regressions in sampled market events."}
        )

    # Compact md event list.
    md_rows: List[Dict[str, Any]] = []
    for evt in md_events[:80]:
        et = str(evt.get("event_type", ""))
        ts = _parse_ts(evt.get("ts"))
        payload = evt.get("payload") if isinstance(evt.get("payload"), dict) else {}
        summary = ""
        if et == "md_error":
            summary = str(payload.get("type") or "") + (": " if payload.get("type") else "") + str(payload.get("error") or payload)
        elif et == "md_stale":
            summary = str(payload.get("reason") or payload)
        else:
            summary = json.dumps(payload, ensure_ascii=True) if payload else ""
        md_rows.append({"ts": _iso(ts), "event_type": et, "summary": summary})

    return {
        "generated_at": now.isoformat(),
        "md": md,
        "md_staleness_sec": md_staleness,
        "stale_warn_sec": stale_warn,
        "last_market_ts": _iso(last_market_bar_ts),
        "last_market_seen_ts": _iso(last_market_seen_ts),
        "last_market_age_sec": last_market_age_sec,
        "market": {
            "rows": market_rows[:200],
            "sampled_items": len(items),
            "sampled_market_events": len(market_events),
            "ts_regressions_in_sample": regressions,
        },
        "events": md_rows[:80],
        "issues": issues,
    }


def _make_handler(
    *,
    control_base_url: str,
    timeout_sec: float,
    default_journal_lines: int,
) -> type[BaseHTTPRequestHandler]:
    control_base_url = str(control_base_url).strip() or "http://127.0.0.1:8100"
    timeout_sec = float(timeout_sec)
    default_journal_lines = int(default_journal_lines)

    class MDGuiHandler(BaseHTTPRequestHandler):
        def _send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=True, indent=2, sort_keys=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
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
            if parsed.path == "/api/snapshot":
                query = urllib.parse.parse_qs(parsed.query)
                lines = default_journal_lines
                try:
                    lines = max(50, min(2000, int(query.get("lines", [str(default_journal_lines)])[0])))
                except Exception:
                    lines = default_journal_lines
                try:
                    status_doc = _fetch_json(_join_url(control_base_url, "/status"), timeout_sec=timeout_sec)
                    journal_doc = _fetch_json(
                        _join_url(control_base_url, f"/journal?lines={lines}"),
                        timeout_sec=timeout_sec,
                    )
                    journal_items = journal_doc.get("items", []) if isinstance(journal_doc, dict) else []
                    snapshot = build_md_snapshot(status_doc, journal_items)
                    self._send_json({"ok": True, "snapshot": snapshot})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=502)
                return
            self.send_response(404)

    return MDGuiHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="MD Module GUI (read-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8140)
    parser.add_argument("--control-url", default="http://127.0.0.1:8100")
    parser.add_argument("--timeout-sec", type=float, default=2.0)
    parser.add_argument("--journal-lines", type=int, default=1200)
    args = parser.parse_args()

    handler = _make_handler(
        control_base_url=str(args.control_url),
        timeout_sec=float(args.timeout_sec),
        default_journal_lines=int(args.journal_lines),
    )
    server = ThreadingHTTPServer((str(args.host), int(args.port)), handler)
    print(f"Starting MD GUI on http://{args.host}:{args.port} (control={args.control_url})")
    server.serve_forever()


_HTML = """<!doctype html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MD Module GUI</title>
  <style>
    :root { --bg:#f3f6fb; --fg:#0f172a; --card:#ffffff; --line:#cbd5e1; --accent:#0f766e; --warn:#b91c1c; --muted:#334155; }
    body { margin:0; font-family:"IBM Plex Sans", "Segoe UI", sans-serif; background:radial-gradient(circle at 15% 0%, #ffffff, #e7eef9); color:var(--fg); }
    .wrap { max-width:1200px; margin:18px auto; padding:0 12px; }
    .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }
    .card { border:1px solid var(--line); background:var(--card); border-radius:12px; padding:10px; }
    h1 { margin:0 0 6px; font-size:24px; letter-spacing:.02em; }
    h2 { margin:0 0 6px; font-size:12px; text-transform:uppercase; color:var(--muted); }
    .v { font-weight:700; font-size:18px; }
    .mono { font-family:ui-monospace, Menlo, Consolas, monospace; }
    .small { font-size:12px; color:var(--muted); }
    .row { margin-top:10px; }
    pre { margin:0; white-space:pre-wrap; word-break:break-word; }
    table { width:100%; border-collapse:collapse; font-size:13px; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
    th, td { border-bottom:1px solid #e5e7eb; padding:7px 8px; text-align:left; vertical-align:top; }
    th { background:#f1f5f9; }
    .ok { color:var(--accent); font-weight:700; }
    .warn { color:var(--warn); font-weight:700; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; margin-right:6px; background:#f8fafc; }
    canvas { width:100%; height:120px; display:block; }
    a { color:#0b4a6f; text-decoration:none; }
    a:hover { text-decoration:underline; }
    @media (max-width: 900px){ canvas { height:90px; } }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Market Data (MD) Observatory</h1>
    <div class="small">Quelle: Control-API <span class="mono">/status</span> + <span class="mono">/journal</span> (md telemetry + market/md_* journal events)</div>
    <div class="small">Updated: <span id="updated" class="mono">-</span></div>

    <div class="row grid">
      <div class="card"><h2>Mode</h2><div id="mode" class="v">-</div></div>
      <div class="card"><h2>MD Staleness (s)</h2><div id="md_stale" class="v">-</div><div class="small">Warn > <span id="warn_thr" class="mono">-</span></div></div>
      <div class="card"><h2>Last Market Age (s)</h2><div id="mkt_age" class="v">-</div><div id="mkt_ts" class="small mono">-</div></div>
      <div class="card"><h2>Queues</h2><div id="queues" class="v">-</div></div>
      <div class="card"><h2>Reconnects</h2><div id="reconnects" class="v">-</div></div>
      <div class="card"><h2>Stale Count</h2><div id="stale_count" class="v">-</div></div>
      <div class="card"><h2>Sampled Journal</h2><div id="sampled" class="v">-</div></div>
    </div>

    <div class="row card">
      <h2>Plausibility Checks</h2>
      <div id="issues"></div>
      <div class="row small">
        <span class="pill">API</span>
        <a href="/api/snapshot" target="_blank">/api/snapshot</a>
      </div>
    </div>

    <div class="row card">
      <h2>Close (Recent)</h2>
      <canvas id="chart" width="1200" height="180"></canvas>
      <div class="small mono" id="chart_meta">-</div>
    </div>

    <div class="row">
      <h2>Recent Market Events</h2>
      <table>
        <thead><tr><th>ts</th><th>close</th><th>volume</th><th>spread_bps</th><th>depth</th><th>imbalance</th><th>seq</th></tr></thead>
        <tbody id="market"></tbody>
      </table>
    </div>

    <div class="row">
      <h2>MD Events (stale/errors)</h2>
      <table>
        <thead><tr><th>ts</th><th>event_type</th><th>summary</th></tr></thead>
        <tbody id="events"></tbody>
      </table>
    </div>
  </div>
  <script>
    function txt(v) { return (v===undefined || v===null || v==="") ? "-" : String(v); }
    function esc(v) { return txt(v).replace(/[&<>]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[s])); }
    function num(v) { const n = Number(v); return Number.isFinite(n) ? n : null; }
    function fmt(v) { const n = num(v); return (n===null) ? txt(v) : String(Math.round(n*1000)/1000); }
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
    function pill(issue) {
      const cls = issue.level === "warn" ? "warn" : "ok";
      return `<span class="pill ${cls}">${esc(issue.code)}: ${esc(issue.message)}</span>`;
    }

    function drawCloseChart(rows) {
      const canvas = document.getElementById("chart");
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0,0,w,h);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0,0,w,h);

      const closes = rows.map(r => num(r.close)).filter(v => v!==null);
      if (!closes.length) return {min:null, max:null};
      const min = Math.min(...closes), max = Math.max(...closes);
      const pad = 14;
      const x0 = pad, y0 = pad, x1 = w - pad, y1 = h - pad;

      // grid
      ctx.strokeStyle = "#e2e8f0";
      ctx.lineWidth = 1;
      for (let i=0;i<=4;i++){
        const y = y0 + (y1-y0)*(i/4);
        ctx.beginPath(); ctx.moveTo(x0,y); ctx.lineTo(x1,y); ctx.stroke();
      }

      const scaleY = (v) => {
        if (max === min) return (y0 + y1) / 2;
        return y1 - ( (v - min) / (max - min) ) * (y1 - y0);
      };
      const scaleX = (i, n) => x0 + (n<=1 ? 0 : (i/(n-1))*(x1-x0));

      ctx.strokeStyle = "#0f766e";
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i=0;i<closes.length;i++){
        const x = scaleX(i, closes.length);
        const y = scaleY(closes[i]);
        if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      }
      ctx.stroke();

      // last point
      ctx.fillStyle = "#0f766e";
      ctx.beginPath();
      ctx.arc(scaleX(closes.length-1, closes.length), scaleY(closes[closes.length-1]), 3, 0, Math.PI*2);
      ctx.fill();
      return {min, max};
    }

    async function refresh() {
      const r = await fetch("/api/snapshot?lines=1200");
      const payload = await r.json();
      if (!payload.ok) throw new Error(payload.error || "snapshot failed");
      const s = payload.snapshot || {};
      const md = s.md || {};
      const market = (s.market || {}).rows || [];
      const events = s.events || [];

      document.getElementById("updated").textContent = fmtTs(s.generated_at);
      document.getElementById("mode").textContent = txt(md.mode);

      const st = num(s.md_staleness_sec);
      const thr = num(s.stale_warn_sec);
      document.getElementById("md_stale").textContent = (st===null) ? "-" : fmt(st);
      document.getElementById("md_stale").className = "v " + ((thr!==null && st!==null && st>thr) ? "warn" : "");
      document.getElementById("warn_thr").textContent = (thr===null) ? "-" : fmt(thr);

      const age = num(s.last_market_age_sec);
      document.getElementById("mkt_age").textContent = (age===null) ? "-" : fmt(age);
      document.getElementById("mkt_age").className = "v " + ((thr!==null && age!==null && age>thr) ? "warn" : "");
      const seenRaw = txt(s.last_market_seen_ts);
      const barRaw = txt(s.last_market_ts);
      const seen = (seenRaw === "-") ? "-" : fmtTs(seenRaw);
      const bar = (barRaw === "-") ? "-" : fmtTs(barRaw);
      document.getElementById("mkt_ts").textContent = (seen !== "-" && bar !== "-" && seen !== bar) ? `seen=${seen} bar=${bar}` : (seen !== "-" ? seen : bar);

      document.getElementById("queues").textContent = `core:${txt(md.queue_market_core)} exec:${txt(md.queue_market_exec)}`;
      document.getElementById("reconnects").textContent = txt(md.reconnects);
      document.getElementById("stale_count").textContent = txt(md.stale_count);
      document.getElementById("sampled").textContent = `${txt((s.market||{}).sampled_items)} lines`;

      const issues = s.issues || [];
      document.getElementById("issues").innerHTML = issues.length
        ? issues.map(pill).join("")
        : `<span class="ok">Keine Auffälligkeiten in den aktuellen Heuristiken.</span>`;

      // chart uses oldest->newest
      const forChart = market.slice().reverse().slice(0, 200);
      const meta = drawCloseChart(forChart);
      document.getElementById("chart_meta").textContent = `points=${forChart.length} close_min=${fmt(meta.min)} close_max=${fmt(meta.max)}`;

      document.getElementById("market").innerHTML = market.slice(0, 120).map(m => `
        <tr>
          <td class="mono">${esc(fmtTs(m.ts))}</td>
          <td>${esc(m.close)}</td>
          <td>${esc(m.volume)}</td>
          <td>${esc(m.spread_bps)}</td>
          <td>${esc(m.depth)}</td>
          <td>${esc(m.imbalance)}</td>
          <td>${esc(m.seq)}</td>
        </tr>
      `).join("");

      document.getElementById("events").innerHTML = events.slice(0, 80).map(e => `
        <tr>
          <td class="mono">${esc(fmtTs(e.ts))}</td>
          <td>${esc(e.event_type)}</td>
          <td class="mono">${esc(e.summary)}</td>
        </tr>
      `).join("");
    }

    async function loop() {
      try { await refresh(); } catch (e) { console.error(e); }
      setTimeout(loop, 1200);
    }
    loop();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
