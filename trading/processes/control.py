from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict

from trading.ipc.events import ControlCommand
from trading.ipc.queues import try_put
from trading.processes.context import ProcessContext


def run_control(ctx: ProcessContext) -> None:
    try:
        from fastapi import FastAPI, WebSocket
        from fastapi.responses import HTMLResponse
        import uvicorn
    except Exception as exc:
        print(f"control process disabled: {exc}")
        return

    app = FastAPI()
    telemetry: Dict[str, Any] = {"updated_at": None, "data": {}}
    telemetry_history: deque[Dict[str, Any]] = deque(maxlen=500)
    journal_path = _cfg(ctx.config, "journal.json_path", "logs/journal_events.jsonl")

    def telemetry_consumer() -> None:
        while not ctx.stop_event.is_set():
            try:
                evt = ctx.q_telemetry.get(timeout=0.5)
                telemetry["updated_at"] = evt.ts.isoformat()
                telemetry["data"].setdefault(evt.process, {}).update(evt.data)
                telemetry_history.append(
                    {
                        "ts": evt.ts.isoformat(),
                        "process": evt.process,
                        "data": evt.data,
                    }
                )
            except Exception:
                continue

    t = threading.Thread(target=telemetry_consumer, daemon=True)
    t.start()

    def send_cmd(action: str, reason: str | None = None) -> None:
        cmd = ControlCommand(ts=datetime.now(timezone.utc), action=action, reason=reason)
        try_put(ctx.q_control_core, cmd)
        try_put(ctx.q_control_exec, cmd)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/status")
    async def status() -> Dict[str, Any]:
        return telemetry

    @app.get("/telemetry")
    async def telemetry_tail(limit: int = 100) -> Dict[str, Any]:
        limit = max(1, min(1000, int(limit)))
        return {
            "items": list(telemetry_history)[-limit:],
            "count": len(telemetry_history),
        }

    @app.get("/journal")
    async def journal_tail(lines: int = 200) -> Dict[str, Any]:
        lines = max(1, min(2000, int(lines)))
        return {"items": _tail_json_lines(journal_path, lines)}

    @app.post("/start")
    async def start() -> Dict[str, Any]:
        send_cmd("START")
        return {"ok": True}

    @app.post("/stop")
    async def stop() -> Dict[str, Any]:
        send_cmd("STOP")
        send_cmd("CANCEL_ALL")
        return {"ok": True}

    @app.post("/pause")
    async def pause() -> Dict[str, Any]:
        send_cmd("PAUSE")
        return {"ok": True}

    @app.post("/resume")
    async def resume() -> Dict[str, Any]:
        send_cmd("RESUME")
        return {"ok": True}

    @app.post("/reload_config")
    async def reload_config() -> Dict[str, Any]:
        send_cmd("RELOAD")
        return {"ok": True}

    @app.post("/flatten")
    async def flatten() -> Dict[str, Any]:
        send_cmd("STOP", "flatten")
        send_cmd("CANCEL_ALL", "flatten")
        return {"ok": True}

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                await ws.send_json(telemetry)
                await asyncio.sleep(1.0)
        except Exception:
            pass

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _dashboard_html()

    host = _cfg(ctx.config, "control.host", "127.0.0.1")
    port = int(_cfg(ctx.config, "control.port", 8000))
    uvicorn.run(app, host=host, port=port, log_level="info")


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _tail_json_lines(path: str, lines: int) -> list[dict]:
    if not os.path.exists(path):
        return []
    buf: deque[str] = deque(maxlen=lines)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                buf.append(line)
    out: list[dict] = []
    for raw in buf:
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trader Control</title>
  <style>
    :root { --bg:#0f172a; --panel:#111827; --muted:#9ca3af; --text:#e5e7eb; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; }
    body { margin:0; font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:linear-gradient(135deg,#0f172a,#1f2937); color:var(--text); }
    .wrap { max-width:1100px; margin:24px auto; padding:0 12px; }
    .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
    .card { background:rgba(17,24,39,.95); border:1px solid #334155; border-radius:10px; padding:12px; }
    h1 { margin:0 0 12px 0; font-size:22px; }
    h2 { margin:0 0 8px 0; font-size:14px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
    .v { font-size:18px; font-weight:600; }
    .row { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }
    button { background:#1d4ed8; color:white; border:0; border-radius:8px; padding:8px 12px; cursor:pointer; }
    button.warn { background:#b45309; }
    button.bad { background:#b91c1c; }
    pre { background:#0b1220; border:1px solid #334155; padding:10px; border-radius:8px; overflow:auto; max-height:280px; }
    a { color:#93c5fd; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Trading Control Plane</h1>
    <div class="row">
      <button onclick="cmd('/start')">Start</button>
      <button onclick="cmd('/pause')" class="warn">Pause</button>
      <button onclick="cmd('/resume')">Resume</button>
      <button onclick="cmd('/stop')" class="bad">Stop + CancelAll</button>
      <button onclick="cmd('/flatten')" class="bad">Flatten</button>
      <button onclick="cmd('/reload_config')" class="warn">Reload Config</button>
    </div>
    <div class="grid">
      <div class="card"><h2>Mode</h2><div id="mode" class="v">-</div></div>
      <div class="card"><h2>Trading Enabled</h2><div id="enabled" class="v">-</div></div>
      <div class="card"><h2>Last Market Age (s)</h2><div id="age" class="v">-</div></div>
      <div class="card"><h2>Open Orders</h2><div id="open_orders" class="v">-</div></div>
      <div class="card"><h2>Order Latency (ms)</h2><div id="latency" class="v">-</div></div>
      <div class="card"><h2>Rate Hits</h2><div id="rate_hits" class="v">-</div></div>
      <div class="card"><h2>Deadman Rate Hits</h2><div id="deadman_hits" class="v">-</div></div>
      <div class="card"><h2>Queues</h2><div id="queues" class="v">-</div></div>
    </div>
    <div class="card" style="margin-top:12px">
      <h2>Journal Tail</h2>
      <pre id="journal"></pre>
    </div>
    <div class="row">
      <a href="/docs" target="_blank">API Docs</a>
      <a href="/status" target="_blank">/status</a>
      <a href="/health" target="_blank">/health</a>
      <a href="/telemetry" target="_blank">/telemetry</a>
      <a href="/journal" target="_blank">/journal</a>
    </div>
  </div>
  <script>
    async function cmd(path) {
      try { await fetch(path, { method:'POST' }); } catch (e) { console.error(e); }
    }
    async function refresh() {
      try {
        const s = await (await fetch('/status')).json();
        const core = (s.data||{}).core||{};
        const exec = (s.data||{}).exec||{};
        document.getElementById('mode').textContent = core.mode ?? exec.mode ?? '-';
        document.getElementById('enabled').textContent = String(core.trading_enabled);
        document.getElementById('age').textContent = fmt(core.last_market_event_age);
        document.getElementById('open_orders').textContent = fmt(exec.open_orders_count);
        document.getElementById('latency').textContent = fmt(exec.order_latency_ms);
        document.getElementById('rate_hits').textContent = fmt(exec.rate_limit_hits);
        document.getElementById('deadman_hits').textContent = fmt(exec.deadman_rate_limit_hits);
        document.getElementById('queues').textContent =
          `mc:${fmt(core.queue_market_core)} oi:${fmt(core.queue_order_intent)} er:${fmt(exec.queue_exec_report)}`;
        const j = await (await fetch('/journal?lines=30')).json();
        document.getElementById('journal').textContent = JSON.stringify(j.items||[], null, 2);
      } catch (e) {
        console.error(e);
      }
    }
    function fmt(v){ return (v===undefined||v===null)?'-':String(v); }
    setInterval(refresh, 1000);
    refresh();
  </script>
</body>
</html>"""
