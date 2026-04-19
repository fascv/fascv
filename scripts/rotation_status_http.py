#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.rotation_scope import rotation_rows, rotation_scope_symbols, rotation_selected_symbols
from trading.rotation_universe import build_lanes


LANES = build_lanes()
SLUG_TO_SYMBOL = {str(cfg.get("slug", "")).lower(): symbol for symbol, cfg in LANES.items()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_status_text() -> str:
    try:
        out = subprocess.check_output(
            ["python3", "scripts/rotation_status.py"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        return out
    except Exception as exc:
        return f"rotation_status_error: {exc}\n"


def _default_port() -> int:
    try:
        return max(1, int(os.getenv("ROTATION_STATUS_HTTP_PORT", "8960")))
    except Exception:
        return 8960


def _load_active_state() -> dict:
    path = REPO_ROOT / "configs" / "rotation_active_lanes.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _gate_p1_sweet(row: dict) -> bool:
    reason = str(row.get("gate_reason", "") or "")
    if reason == "slope_profile_mismatch":
        return False
    return bool(row.get("slope_profile_match", True))


def _gate_p2_makro(row: dict) -> bool:
    reason = str(row.get("gate_reason", "") or "")
    if reason and reason not in {"-", "keep_open", "spread", "depth", "volume", "slope_profile_mismatch"}:
        return False
    return (
        not bool(row.get("macro_down_context"))
        and not bool(row.get("rebound_in_downtrend"))
        and not bool(row.get("countertrend_rebound"))
        and str(row.get("structure_phase", "")) != "downtrend"
    )


def _gate_p3_covol(row: dict) -> bool:
    reason = str(row.get("gate_reason", "") or "")
    if reason in {"spread", "depth", "volume"}:
        return False
    spread_ok = _safe_float(row.get("spread_bps"), 9999.0) <= 22.0
    depth_ok = _safe_float(row.get("top_depth_notional"), 0.0) >= 80.0
    q5 = _safe_float(row.get("quote_volume_5m"), 0.0)
    q60 = _safe_float(row.get("quote_volume_60m"), 0.0)
    volume_ok = not (q5 < 8.0 and q60 < 1500.0)
    return spread_ok and depth_ok and volume_ok


def _display_gate_reason(row: dict) -> str:
    base_reason = str(row.get("gate_reason", "") or "-")
    if bool(row.get("keep_open")):
        return "keep_open"
    if bool(row.get("selected_active")):
        selection_path = str(row.get("selection_path", "") or "")
        selection_strategy = str(
            row.get("selected_strategy", "") or row.get("strategy_primary", "") or ""
        )
        if selection_path == "fast_track" and selection_strategy:
            return f"fast_track_{selection_strategy}"
        if selection_strategy and base_reason in {"", "-"}:
            return f"selected_{selection_strategy}"
    return base_reason


def _fmt_num(value: object, digits: int = 2) -> str:
    return f"{_safe_float(value):.{digits}f}"


def _row_view(row: dict) -> dict:
    ret15_bps = _safe_float(row.get("ret15_bps"))
    rel15_bps = _safe_float(row.get("rel15_bps"))
    trend_bps = ret15_bps if abs(ret15_bps) > 1e-9 else rel15_bps
    if trend_bps > 0.0:
        trend_arrow = "↑"
        trend_dir = "up"
    elif trend_bps < 0.0:
        trend_arrow = "↓"
        trend_dir = "down"
    else:
        trend_arrow = "→"
        trend_dir = "flat"
    return {
        "symbol": str(row.get("symbol", "?")).upper(),
        "score": _safe_float(row.get("score")),
        "spread_bps": _safe_float(row.get("spread_bps")),
        "ret15_bps": ret15_bps,
        "rel15_bps": rel15_bps,
        "pos_pct": _safe_float(row.get("pos_pct"), 0.0),
        "open_notional": _safe_float(row.get("open_notional"), 0.0),
        "gate_reason": _display_gate_reason(row),
        "eligible": bool(row.get("eligible")),
        "keep_open": bool(row.get("keep_open")),
        "p1_sweet": _gate_p1_sweet(row),
        "p2_makro": _gate_p2_makro(row),
        "p3_covol": _gate_p3_covol(row),
        "lane": str(row.get("selected_strategy", "") or row.get("strategy_primary", "") or ""),
        "setup_type": str(row.get("setup_type", "") or ""),
        "trend_bps": trend_bps,
        "trend_arrow": trend_arrow,
        "trend_dir": trend_dir,
    }


def _build_view_model(state: dict) -> dict:
    selected = rotation_selected_symbols(state)
    selected_set = set(selected)
    selected_strategy_map = {
        str(key).upper(): str(value)
        for key, value in (state.get("selected_strategy_map") or {}).items()
        if str(key).strip()
    }
    rows = state.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    all_rows = list(rotation_rows(state))
    if not all_rows:
        all_rows = [row for row in rows if isinstance(row, dict)]

    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "list-units", "codex-rotation-*.service", "--state=running", "--no-pager"],
            cwd=REPO_ROOT,
            text=True,
            timeout=5,
        )
    except Exception:
        out = ""
    running_symbols: list[str] = []
    for line in out.splitlines():
        m = re.search(r"codex-rotation-([a-z0-9]+)\.service", line)
        if not m:
            continue
        slug = str(m.group(1)).lower().strip()
        if slug in {"selector", "status"}:
            continue
        symbol = SLUG_TO_SYMBOL.get(slug, slug.upper()).upper()
        if symbol and symbol not in running_symbols:
            running_symbols.append(symbol)

    existing_symbols = {
        str(row.get("symbol", "")).strip().upper()
        for row in all_rows
        if isinstance(row, dict)
    }
    for symbol in rotation_scope_symbols(
        state,
        running_symbols=running_symbols,
        include_watch=True,
        include_selected=True,
        include_rows=False,
    ):
        if symbol in existing_symbols:
            continue
        is_selected = symbol in selected_set
        gate_reason = "fallback_running_lane" if symbol in running_symbols else "fallback_scope_symbol"
        all_rows.append(
            {
                "symbol": symbol,
                "score": 0.0,
                "spread_bps": 0.0,
                "ret15_bps": 0.0,
                "rel15_bps": 0.0,
                "pos_pct": 0.0,
                "open_notional": 0.0,
                "gate_reason": gate_reason,
                "eligible": is_selected,
                "keep_open": False,
                "setup_type": gate_reason,
                "selected_active": is_selected,
                "selected_strategy": selected_strategy_map.get(symbol, ""),
            }
        )
        existing_symbols.add(symbol)

    active_rows = [row for row in all_rows if str(row.get("symbol", "")).upper() in selected_set]
    if not active_rows:
        active_rows = rows
    next_rows = [
        row
        for row in all_rows
        if bool(row.get("eligible")) and str(row.get("symbol", "")).upper() not in selected_set
    ]
    blocked_rows = [row for row in all_rows if not bool(row.get("eligible"))]

    active_views = [_row_view(row) for row in active_rows[:12]]
    in_trade_rows = [
        row for row in active_views if _safe_float(row.get("open_notional"), 0.0) > 0.0
    ]
    in_trade_rows.sort(
        key=lambda item: (
            -_safe_float(item.get("open_notional"), 0.0),
            str(item.get("symbol", "")),
        )
    )

    return {
        "generated_at": str(state.get("generated_at", "") or ""),
        "selected": selected,
        "fraction": _safe_float(state.get("fraction"), 0.0),
        "active_rows": active_views,
        "in_trade_rows": in_trade_rows,
        "next_rows": [_row_view(row) for row in next_rows[:20]],
        "blocked_rows": [_row_view(row) for row in blocked_rows[:40]],
    }


def _html_page() -> str:
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Rotation Overview</title>
  <style>
    :root {
      --bg: #0b0f14;
      --panel: #111823;
      --line: #253245;
      --txt: #e6edf3;
      --muted: #8b9ab0;
      --ok: #7ee787;
      --bad: #ff9b9b;
      --warn: #ffcc66;
      --accent: #80b9ff;
    }
    * { box-sizing: border-box; }
    body { margin: 16px; background: var(--bg); color: var(--txt); font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Helvetica, Arial; }
    .bar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
    .pill { border: 1px solid var(--line); background: var(--panel); border-radius: 999px; padding: 6px 10px; font-size: 12px; }
    a { color: var(--accent); text-decoration: none; }
    .section { margin-top: 14px; border: 1px solid var(--line); background: var(--panel); border-radius: 12px; overflow: hidden; }
    .section h2 { margin: 0; padding: 10px 12px; border-bottom: 1px solid var(--line); font-size: 14px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #1c2736; padding: 7px 8px; text-align: right; white-space: nowrap; }
    th:first-child, td:first-child { text-align: left; }
    tr:hover td { background: #121c2a; }
    .gate-ok { color: var(--ok); font-weight: 700; }
    .gate-bad { color: var(--bad); font-weight: 700; }
    .dir-up { color: var(--ok); font-weight: 700; }
    .dir-down { color: var(--bad); font-weight: 700; }
    .dir-flat { color: var(--muted); font-weight: 700; }
    .muted { color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .hidden { display: none; }
    .foot { margin-top: 10px; color: var(--muted); font-size: 12px; }
    @media (max-width: 1100px) {
      body { margin: 8px; }
      th, td { padding: 6px; font-size: 12px; }
    }
  </style>
</head>
<body>
  <div class="bar">
    <span class="pill"><b>Rotation Overview</b></span>
    <span id="updated" class="pill">updated: -</span>
    <span id="active" class="pill">active: -</span>
    <span id="share" class="pill">share: -</span>
    <a href="/api/status" target="_blank">/api/status</a>
    <a href="/raw" target="_blank">/raw</a>
  </div>
  <div class="section">
    <h2>Im Trade</h2>
    <table id="tbl_trades"></table>
  </div>
  <div class="foot mono">Live-Coins oben bleiben unveraendert. Diese Tabelle zeigt nur aktuell offene Trades.</div>
  <script>
    function esc(s) {
      return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',\"'\":'&#39;'}[c]));
    }
    function num(v, d=1) {
      const n = Number(v ?? 0);
      return Number.isFinite(n) ? n.toFixed(d) : '-';
    }
    function pct(v, d=1) { return num(v, d) + '%'; }
    function hdrTrades() {
      return `
      <thead><tr>
        <th>Symbol</th>
        <th>Lane</th>
        <th>Range</th>
        <th>Richtung</th>
        <th>Open</th>
        <th>r15</th>
      </tr></thead>`;
    }
    function rowTrade(r) {
      const dirClass = r.trend_dir === 'up' ? 'dir-up' : (r.trend_dir === 'down' ? 'dir-down' : 'dir-flat');
      return `<tr>
        <td class="mono">${esc(r.symbol)}</td>
        <td class="mono">${esc(r.lane || '-')}</td>
        <td>${pct(r.pos_pct,1)}</td>
        <td class="${dirClass}">${esc(r.trend_arrow || '→')}</td>
        <td>${num(r.open_notional,2)}</td>
        <td>${num(r.ret15_bps,1)}</td>
      </tr>`;
    }
    function renderTrades(id, rows) {
      const el = document.getElementById(id);
      if (!rows || !rows.length) {
        el.innerHTML = hdrTrades() + '<tbody><tr><td colspan="6" class="muted">keine offenen Trades</td></tr></tbody>';
        return;
      }
      el.innerHTML = hdrTrades() + '<tbody>' + rows.map(rowTrade).join('') + '</tbody>';
    }
    async function refresh() {
      try {
        const r = await fetch('/api/status', { cache: 'no-store' });
        const j = await r.json();
        const vm = j.view || {};
        document.getElementById('updated').textContent = 'updated: ' + (vm.generated_at || j.generated_at || '-');
        const sel = vm.selected || [];
        document.getElementById('active').textContent = 'active: ' + (sel.length ? sel.join(', ') : '(none)');
        document.getElementById('share').textContent = 'share: ' + ((Number(vm.fraction || 0) * 100).toFixed(1)) + '%';
        renderTrades('tbl_trades', vm.in_trade_rows || []);
      } catch (e) {
        const msg = [{ symbol: 'ERROR', lane: '-', pos_pct: 0, trend_arrow: '→', trend_dir: 'flat', open_notional: 0, ret15_bps: 0 }];
        renderTrades('tbl_trades', msg);
      }
    }
    refresh();
    setInterval(refresh, 10000);
  </script>
</body>
</html>
"""


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, obj: object, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=True, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, status: int = 200) -> None:
            body = text.encode("utf-8", errors="replace")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send_json({"status": "ok", "time": _now_iso()})
                return
            if parsed.path == "/raw":
                self._send_text(_render_status_text())
                return
            if parsed.path in {"/api/status", "/api/snapshot"}:
                state = _load_active_state()
                view = _build_view_model(state)
                self._send_json(
                    {
                        "ok": True,
                        "generated_at": _now_iso(),
                        "text": _render_status_text(),
                        "active": state,
                        "view": view,
                    }
                )
                return
            if parsed.path == "/":
                self._send_html(_html_page())
                return
            self._send_text("not found\n", status=404)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve rotation status overview via HTTP")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=_default_port())
    args = ap.parse_args()

    server = ThreadingHTTPServer((str(args.host), int(args.port)), _make_handler())
    print(f"rotation status http listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
