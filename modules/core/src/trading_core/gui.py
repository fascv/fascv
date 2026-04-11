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
    text = raw
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
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8", errors="replace"))


def _iter_core_journal_events(items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for item in items:
        if not isinstance(item, dict):
            continue
        et = str(item.get("event_type", ""))
        if et == "core_decision" or et.startswith("core_"):
            yield item


def _event_time(event: Dict[str, Any]) -> Optional[datetime]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_ts = _parse_ts(payload.get("ts"))
        if payload_ts:
            return payload_ts
    return _parse_ts(event.get("ts"))


def _last_of(events: List[Dict[str, Any]], event_type: str) -> Optional[Dict[str, Any]]:
    for e in reversed(events):
        if str(e.get("event_type", "")) == event_type:
            return e
    return None


def build_core_snapshot(status_doc: Any, journal_items: Any) -> Dict[str, Any]:
    status = status_doc if isinstance(status_doc, dict) else {}
    data = status.get("data", {}) if isinstance(status.get("data"), dict) else {}
    core = data.get("core", {}) if isinstance(data.get("core"), dict) else {}

    items = journal_items if isinstance(journal_items, list) else []
    core_events = list(_iter_core_journal_events(items))

    last_decision = _last_of(core_events, "core_decision")
    last_disabled = _last_of(core_events, "core_trading_disabled")
    last_enabled = _last_of(core_events, "core_trading_enabled")
    last_error = _last_of(core_events, "core_error")

    # Aggregate a few counts over the sampled journal window.
    gate_reasons: Dict[str, int] = {}
    risk_reasons: Dict[str, int] = {}
    errors = 0
    decisions = 0
    intents = 0
    dropped = 0

    for e in core_events:
        if str(e.get("event_type", "")) == "core_error":
            errors += 1
            continue
        if str(e.get("event_type", "")) != "core_decision":
            continue
        decisions += 1
        payload = e.get("payload")
        if not isinstance(payload, dict):
            continue

        gate = payload.get("gate")
        if isinstance(gate, dict):
            r = gate.get("reason")
            if r:
                gate_reasons[str(r)] = gate_reasons.get(str(r), 0) + 1

        risk = payload.get("risk")
        if isinstance(risk, dict):
            rr = risk.get("reason")
            if rr:
                risk_reasons[str(rr)] = risk_reasons.get(str(rr), 0) + 1

        il = payload.get("intents")
        if isinstance(il, list):
            intents += len(il)
        dropped += int(payload.get("dropped_market_events") or 0)

    # Pull a small "current decision" view.
    last_decision_payload = last_decision.get("payload") if isinstance(last_decision, dict) else None
    if not isinstance(last_decision_payload, dict):
        last_decision_payload = {}

    # Some keys in core_decision payload are stored as strings already.
    decision_ts = _parse_ts(last_decision_payload.get("ts"))
    age_sec: Optional[float] = None
    if decision_ts is not None:
        try:
            age_sec = max(0.0, (datetime.now(timezone.utc) - decision_ts).total_seconds())
        except Exception:
            age_sec = None

    gate = last_decision_payload.get("gate") if isinstance(last_decision_payload.get("gate"), dict) else {}
    risk = last_decision_payload.get("risk") if isinstance(last_decision_payload.get("risk"), dict) else {}
    news = last_decision_payload.get("news") if isinstance(last_decision_payload.get("news"), dict) else {}

    return {
        "core": core,
        "journal_window": {
            "sampled_items": len(items),
            "core_events": len(core_events),
            "core_decisions": decisions,
            "core_errors": errors,
            "intents_in_window": intents,
            "dropped_market_events_in_window": dropped,
            "gate_block_reasons": gate_reasons,
            "risk_reasons": risk_reasons,
        },
        "last": {
            "decision_ts": _iso(decision_ts),
            "decision_age_sec": age_sec,
            "gate": gate,
            "risk": risk,
            "news": news,
            "trading_disabled": (last_disabled or {}).get("payload") if isinstance(last_disabled, dict) else None,
            "trading_enabled": (last_enabled or {}).get("payload") if isinstance(last_enabled, dict) else None,
            "error": (last_error or {}).get("payload") if isinstance(last_error, dict) else None,
        },
        "last_decision_payload": last_decision_payload,
        "updated_at": _iso(datetime.now(timezone.utc)),
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

    class CoreGuiHandler(BaseHTTPRequestHandler):
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
                    snapshot = build_core_snapshot(status_doc, journal_items)
                    self._send_json({"ok": True, "snapshot": snapshot})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=502)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return CoreGuiHandler


def run_core_gui(
    host: str = "127.0.0.1",
    port: int = 8130,
    control_base_url: str = "http://127.0.0.1:8100",
    timeout_sec: float = 2.0,
    journal_lines: int = 600,
) -> None:
    handler_cls = _make_handler(
        control_base_url=control_base_url,
        timeout_sec=timeout_sec,
        default_journal_lines=journal_lines,
    )
    server = ThreadingHTTPServer((host, int(port)), handler_cls)
    print(f"core gui listening on http://{host}:{port} (control={control_base_url})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Core-only GUI for core decision observability")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8130)
    parser.add_argument("--control-url", default="http://127.0.0.1:8100")
    parser.add_argument("--timeout-sec", type=float, default=2.0)
    parser.add_argument("--journal-lines", type=int, default=600)
    args = parser.parse_args()
    run_core_gui(
        host=args.host,
        port=args.port,
        control_base_url=args.control_url,
        timeout_sec=args.timeout_sec,
        journal_lines=args.journal_lines,
    )


_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Core Module GUI</title>
  <style>
    :root { --bg:#f4f3ee; --fg:#1d1d1d; --card:#fffdf6; --line:#d8d3c4; --accent:#2c6e49; --warn:#b23a48; --muted:#5f5f5f; }
    body { margin:0; font-family:"IBM Plex Sans", "Segoe UI", sans-serif; background:radial-gradient(circle at 10% 0%, #fffdf6, #efe8d8); color:var(--fg); }
    .wrap { max-width:1200px; margin:18px auto; padding:0 12px; }
    .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }
    .card { border:1px solid var(--line); background:var(--card); border-radius:10px; padding:10px; }
    h1 { margin:0 0 10px; font-size:24px; letter-spacing:.02em; }
    h2 { margin:0 0 6px; font-size:12px; text-transform:uppercase; color:#5a5a5a; }
    .v { font-weight:700; font-size:18px; }
    .mono { font-family:ui-monospace, Menlo, Consolas, monospace; }
    .small { font-size:12px; color:var(--muted); }
    .row { margin-top:10px; }
    pre { margin:0; white-space:pre-wrap; word-break:break-word; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; margin-right:6px; background:#faf5ea; }
    .ok { color:var(--accent); font-weight:700; }
    .bad { color:var(--warn); font-weight:700; }
    .split { display:grid; gap:10px; grid-template-columns: 1fr 1fr; }
    @media (max-width: 900px){ .split { grid-template-columns: 1fr; } }
    a { color:#1a4a8a; text-decoration:none; }
    a:hover { text-decoration:underline; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Core Decision Observatory</h1>
    <div class="small">Quelle: Control-API <span class="mono">/status</span> + <span class="mono">/journal</span> (core-relevante Events + letztes <span class="mono">core_decision</span>)</div>
    <div class="small">Updated: <span id="updated" class="mono">-</span></div>

    <div class="row grid">
      <div class="card"><h2>Trading Enabled</h2><div id="enabled" class="v">-</div><div id="disable_reason" class="small mono">-</div></div>
      <div class="card"><h2>Last Market Age (s)</h2><div id="age" class="v">-</div></div>
      <div class="card"><h2>Orders Last Min</h2><div id="orders_last_min" class="v">-</div></div>
      <div class="card"><h2>Last Decision Age (s)</h2><div id="decision_age" class="v">-</div></div>
      <div class="card"><h2>Gate</h2><div id="gate" class="v">-</div><div id="gate_reason" class="small mono">-</div></div>
      <div class="card"><h2>Risk</h2><div id="risk" class="v">-</div><div id="risk_reason" class="small mono">-</div></div>
      <div class="card"><h2>News</h2><div id="news" class="v">-</div><div id="news_meta" class="small mono">-</div></div>
      <div class="card"><h2>Queues</h2><div id="queues" class="v">-</div></div>
    </div>

    <div class="row split">
      <div class="card">
        <h2>Last core_decision (payload)</h2>
        <pre id="decision"></pre>
      </div>
      <div class="card">
        <h2>Window Summary</h2>
        <pre id="summary"></pre>
        <div class="row small">
          <span class="pill">API</span>
          <a href="/api/snapshot" target="_blank">/api/snapshot</a>
        </div>
      </div>
    </div>
  </div>

  <script>
    function fmt(x){
      if (x === null || x === undefined) return '-';
      const n = Number(x);
      if (Number.isFinite(n)) return String(Math.round(n * 1000) / 1000);
      return String(x);
    }
    function fmtTs(raw) {
      if (!raw) return '-';
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
    function isIsoTs(v) { return (typeof v === "string") && /^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}/.test(v); }
    function stringifyDe(obj) {
      return JSON.stringify(
        obj || {},
        (k, v) => (isIsoTs(v) ? fmtTs(v) : v),
        2,
      );
    }
    function yesno(v){
      if (v === true) return 'true';
      if (v === false) return 'false';
      return String(v);
    }

    async function refresh(){
      try {
        const res = await fetch('/api/snapshot?lines=800');
        const doc = await res.json();
        if (!doc.ok) throw new Error(doc.error || 'snapshot error');
        const snap = doc.snapshot || {};
        const core = snap.core || {};
        const last = snap.last || {};
        const gate = last.gate || {};
        const risk = last.risk || {};
        const news = last.news || {};
        const w = snap.journal_window || {};

        document.getElementById('updated').textContent = fmtTs(snap.updated_at || '-');

        document.getElementById('enabled').textContent = yesno(core.trading_enabled);
        document.getElementById('disable_reason').textContent = String(core.trading_disable_reason || '-');
        document.getElementById('age').textContent = fmt(core.last_market_event_age);
        document.getElementById('orders_last_min').textContent = fmt(core.orders_last_min);
        document.getElementById('decision_age').textContent = fmt(last.decision_age_sec);

        const gateAllow = (gate.allow === true);
        document.getElementById('gate').textContent = gateAllow ? 'ALLOW' : 'BLOCK';
        document.getElementById('gate').className = 'v ' + (gateAllow ? 'ok' : 'bad');
        document.getElementById('gate_reason').textContent = String(gate.reason || '-');

        const riskAllow = (risk.allow === true);
        document.getElementById('risk').textContent = riskAllow ? 'ALLOW' : 'BLOCK';
        document.getElementById('risk').className = 'v ' + (riskAllow ? 'ok' : 'bad');
        document.getElementById('risk_reason').textContent = String(risk.reason || '-');

        const present = (news.present === true);
        document.getElementById('news').textContent = present ? 'present' : 'none';
        document.getElementById('news').className = 'v ' + (present ? 'ok' : '');
        const impact = (news.impact !== undefined) ? news.impact : news.impact_score;
        const age = (news.age !== undefined) ? news.age : news.age_sec;
        document.getElementById('news_meta').textContent = `impact=${fmt(impact)} age=${fmt(age)} id=${String(news.event_id || '-')}`;

        document.getElementById('queues').textContent = `mc:${fmt(core.queue_market_core)} oi:${fmt(core.queue_order_intent)} er:${fmt(core.queue_exec_report)} cc:${fmt(core.queue_control_core)} ce:${fmt(core.queue_control_exec)}`;

        document.getElementById('decision').textContent = stringifyDe(snap.last_decision_payload || {});
        document.getElementById('summary').textContent = stringifyDe(w);
      } catch (e) {
        console.error(e);
      }
    }

    setInterval(refresh, 1000);
    refresh();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
