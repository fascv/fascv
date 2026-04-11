from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


STATUS_RANK = {
    "NEW": 0,
    "ACK": 1,
    "OPEN": 2,
    "VALIDATED": 2,
    "PARTIAL": 3,
    "FILLED": 4,
    "CANCELED": 4,
    "REJECTED": 4,
}

TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED"}
DEADMAN_DISABLE_WARN_SEC = 30.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


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


def _iter_exec_journal_events(journal_items: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for item in journal_items:
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type", ""))
        if event_type.startswith("exec") or event_type in {"fill", "deadman"}:
            yield item


def _journal_ts(event: Dict[str, Any]) -> Optional[datetime]:
    return _parse_ts(event.get("ts"))


def _find_budget_cutoff_ts(journal_items: Iterable[Dict[str, Any]]) -> Optional[datetime]:
    # When the user hits "Set Budget", core emits a core_budget_reset event with a cutoff_ts.
    # Without filtering, the GUI shows pre-reset trades and it looks like the new budget was ignored.
    items = journal_items if isinstance(journal_items, list) else list(journal_items)
    for evt in reversed(items):
        if not isinstance(evt, dict):
            continue
        if str(evt.get("event_type", "")) != "core_budget_reset":
            continue
        payload = evt.get("payload")
        if not isinstance(payload, dict):
            continue
        cutoff = _parse_ts(payload.get("cutoff_ts"))
        if cutoff is not None:
            return cutoff
    return None


def _event_time(event: Dict[str, Any]) -> Optional[datetime]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        payload_ts = _parse_ts(payload.get("ts"))
        if payload_ts:
            return payload_ts
    return _parse_ts(event.get("ts"))


def _build_order_view(events: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    orders: Dict[str, Dict[str, Any]] = {}
    global_issues: List[Dict[str, str]] = []

    for evt in events:
        event_type = str(evt.get("event_type", ""))
        payload = evt.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ts = _event_time(evt)

        if event_type == "exec_report":
            order_id = str(payload.get("order_id", "")).strip()
            if not order_id:
                continue
            order = orders.setdefault(
                order_id,
                {
                    "order_id": order_id,
                    "last_status": None,
                    "last_update_ts": None,
                    "total_fill_qty_btc": 0.0,
                    "total_fill_notional_eur": 0.0,
                    "total_fee_eur": 0.0,
                    "updates": 0,
                    "issues": [],
                    "history": [],
                },
            )
            status = str(payload.get("status", "")).upper()
            prev_status = order["last_status"]
            order["history"].append({"ts": _iso(ts), "status": status})
            order["updates"] += 1
            if ts and (order["last_update_ts"] is None or ts > order["last_update_ts"]):
                order["last_update_ts"] = ts

            if prev_status in TERMINAL_STATUSES and status != prev_status:
                order["issues"].append(f"terminal_status_changed:{prev_status}->{status}")
            prev_rank = STATUS_RANK.get(str(prev_status), -1)
            cur_rank = STATUS_RANK.get(status, -1)
            if prev_status is not None and cur_rank >= 0 and prev_rank >= 0 and cur_rank < prev_rank:
                order["issues"].append(f"status_regression:{prev_status}->{status}")
            order["last_status"] = status

        elif event_type == "fill":
            order_id = str(payload.get("order_id", "")).strip()
            qty = _safe_float(payload.get("qty_btc"))
            fee = _safe_float(payload.get("fee_eur"))
            if not order_id:
                global_issues.append(
                    {
                        "level": "warn",
                        "code": "fill_without_order_id",
                        "message": "Fill event without order_id found in journal.",
                    }
                )
                continue
            order = orders.setdefault(
                order_id,
                {
                    "order_id": order_id,
                    "last_status": None,
                    "last_update_ts": None,
                    "total_fill_qty_btc": 0.0,
                    "total_fill_notional_eur": 0.0,
                    "total_fee_eur": 0.0,
                    "updates": 0,
                    "issues": [],
                    "history": [],
                },
            )
            order["total_fill_qty_btc"] += qty
            price = _safe_float(payload.get("price"))
            # Best-effort: fill price is in quote currency (EUR for BTC/EUR). Sum notional so UI can show EUR.
            order["total_fill_notional_eur"] += qty * price
            order["total_fee_eur"] += fee
            if ts and (order["last_update_ts"] is None or ts > order["last_update_ts"]):
                order["last_update_ts"] = ts

    rows: List[Dict[str, Any]] = []
    for row in orders.values():
        qty_btc = float(row["total_fill_qty_btc"] or 0.0)
        notional_eur = float(row["total_fill_notional_eur"] or 0.0)
        avg_price = (notional_eur / qty_btc) if qty_btc > 0 else 0.0
        rows.append(
            {
                "order_id": row["order_id"],
                "last_status": row["last_status"],
                "last_update_ts": _iso(row["last_update_ts"]),
                "total_fill_qty_btc": round(qty_btc, 12),
                "total_fill_notional_eur": round(notional_eur, 2),
                "avg_fill_price_eur": round(avg_price, 2),
                "total_fee_eur": round(row["total_fee_eur"], 8),
                "updates": row["updates"],
                "issues": row["issues"],
            }
        )
    rows.sort(key=lambda item: (item["last_update_ts"] or "", item["order_id"]), reverse=True)
    return rows, global_issues


def _build_trade_view(events: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], Dict[str, Any]]:
    """
    Build a user-friendly "buy/sell rounds" view from fill events.

    The exec process itself does not track PnL; we derive it from fill cashflows:
    - buy consumes: qty*price + fee
    - sell produces: qty*price - fee

    We group fills into "rounds" (cycles) delimited by position going from ~0 to >0 and back to ~0.
    This matches the simulator's common enter-then-exit behavior and is easy to read.
    """
    issues: List[Dict[str, str]] = []
    fills: List[Dict[str, Any]] = []
    for evt in events:
        if not isinstance(evt, dict):
            continue
        if str(evt.get("event_type", "")) != "fill":
            continue
        payload = evt.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ts = _event_time(evt)
        side = str(payload.get("side", "")).strip().lower()
        if side not in {"buy", "sell"}:
            continue
        qty = _safe_float(payload.get("qty_btc"))
        price = _safe_float(payload.get("price"))
        fee = _safe_float(payload.get("fee_eur"))
        order_id = str(payload.get("order_id", "")).strip()
        if qty <= 0.0 or price <= 0.0:
            continue
        fills.append(
            {
                "ts": ts,
                "side": side,
                "qty_btc": qty,
                "price": price,
                "fee_eur": fee,
                "order_id": order_id,
            }
        )

    fills.sort(key=lambda f: (f["ts"] or datetime.min.replace(tzinfo=timezone.utc)))

    eps = 1e-12
    position_btc = 0.0
    current: Dict[str, Any] | None = None
    rounds: List[Dict[str, Any]] = []

    def _start_round(ts: Optional[datetime]) -> Dict[str, Any]:
        return {
            "entry_ts": ts,
            "exit_ts": None,
            "buy_qty_btc": 0.0,
            "sell_qty_btc": 0.0,
            "buy_notional_eur": 0.0,
            "sell_notional_eur": 0.0,
            "buy_fee_eur": 0.0,
            "sell_fee_eur": 0.0,
            "buy_px_qty_sum": 0.0,
            "sell_px_qty_sum": 0.0,
            "fills": 0,
        }

    for f in fills:
        ts = f["ts"]
        side = f["side"]
        qty = float(f["qty_btc"])
        px = float(f["price"])
        fee = float(f["fee_eur"])

        if current is None:
            if abs(position_btc) <= eps and side == "buy":
                current = _start_round(ts)
            elif abs(position_btc) <= eps and side == "sell":
                issues.append(
                    {
                        "level": "warn",
                        "code": "sell_while_flat",
                        "message": "Sell fill while position is flat (ignored for rounds).",
                    }
                )
                # Still update position to keep a consistent internal view.
            else:
                # If we start mid-stream, create a best-effort round so the UI still shows something.
                current = _start_round(ts)
                issues.append(
                    {
                        "level": "warn",
                        "code": "round_started_midstream",
                        "message": "Rounds started without a clean flat->buy boundary (likely due to limited journal window).",
                    }
                )

        current["fills"] += 1
        if side == "buy":
            current["buy_qty_btc"] += qty
            current["buy_notional_eur"] += qty * px
            current["buy_fee_eur"] += fee
            current["buy_px_qty_sum"] += qty * px
            position_btc += qty
        else:
            current["sell_qty_btc"] += qty
            current["sell_notional_eur"] += qty * px
            current["sell_fee_eur"] += fee
            current["sell_px_qty_sum"] += qty * px
            position_btc -= qty

        if abs(position_btc) <= eps and current is not None:
            current["exit_ts"] = ts
            invested = float(current["buy_notional_eur"] + current["buy_fee_eur"])
            received = float(current["sell_notional_eur"] - current["sell_fee_eur"])
            net = received - invested
            fees = float(current["buy_fee_eur"] + current["sell_fee_eur"])
            buy_avg = (current["buy_px_qty_sum"] / current["buy_qty_btc"]) if current["buy_qty_btc"] > 0 else 0.0
            sell_avg = (current["sell_px_qty_sum"] / current["sell_qty_btc"]) if current["sell_qty_btc"] > 0 else 0.0
            net_pct = (net / invested * 100.0) if invested > 0 else 0.0
            rounds.append(
                {
                    "entry_ts": _iso(current["entry_ts"]),
                    "exit_ts": _iso(current["exit_ts"]),
                    "qty_btc": round(float(current["buy_qty_btc"]), 12),
                    "invested_eur": round(invested, 2),
                    "received_eur": round(received, 2),
                    "fees_eur": round(fees, 4),
                    "net_eur": round(net, 2),
                    "net_pct": round(net_pct, 3),
                    "avg_buy_px_eur": round(float(buy_avg), 2),
                    "avg_sell_px_eur": round(float(sell_avg), 2),
                    "fills": int(current["fills"]),
                }
            )
            current = None

    # Open (not-yet-closed) position summary. PnL needs a mark price, which is injected later.
    open_pos = {
        "position_btc": round(float(position_btc), 12),
        "entry_ts": _iso(current["entry_ts"]) if current is not None else None,
        "invested_eur_so_far": round(float((current["buy_notional_eur"] + current["buy_fee_eur"]) if current else 0.0), 2),
        "received_eur_so_far": round(float((current["sell_notional_eur"] - current["sell_fee_eur"]) if current else 0.0), 2),
        "fees_eur_so_far": round(float((current["buy_fee_eur"] + current["sell_fee_eur"]) if current else 0.0), 4),
    }

    rounds.sort(key=lambda r: (r.get("exit_ts") or r.get("entry_ts") or ""), reverse=True)
    return rounds[:200], issues, open_pos


def build_exec_snapshot(
    status_doc: Dict[str, Any],
    journal_items: List[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    data = status_doc.get("data", {}) if isinstance(status_doc, dict) else {}
    exec_data = data.get("exec", {}) if isinstance(data, dict) else {}
    core_data = data.get("core", {}) if isinstance(data, dict) else {}
    core_data = dict(core_data) if isinstance(core_data, dict) else {}

    # Prefer a stable cutoff from core telemetry (survives short journal windows).
    cutoff_from_core = _parse_ts(core_data.get("budget_cutoff_ts"))
    cutoff_from_journal = _find_budget_cutoff_ts(journal_items)
    cutoff_ts: Optional[datetime] = None
    if cutoff_from_core and cutoff_from_journal:
        cutoff_ts = max(cutoff_from_core, cutoff_from_journal)
    else:
        cutoff_ts = cutoff_from_core or cutoff_from_journal
    exec_events = list(_iter_exec_journal_events(journal_items))
    if cutoff_ts is not None:
        exec_events = [e for e in exec_events if (_journal_ts(e) is None or _journal_ts(e) >= cutoff_ts)]
    orders, issues = _build_order_view(exec_events)
    trades, trade_issues, open_pos = _build_trade_view(exec_events)
    issues.extend(trade_issues)
    core_data["budget_cutoff_ts"] = _iso(cutoff_ts)

    # If we have a mark price from core telemetry, estimate current open PnL (cashflow + position value).
    mark = _safe_float(core_data.get("mark_price"), 0.0)
    if mark > 0.0 and abs(float(open_pos.get("position_btc") or 0.0)) > 0.0:
        pos_btc = float(open_pos.get("position_btc") or 0.0)
        position_value = pos_btc * mark
        # Net cashflow so far: -invested + received
        invested = _safe_float(open_pos.get("invested_eur_so_far"), 0.0)
        received = _safe_float(open_pos.get("received_eur_so_far"), 0.0)
        pnl = (-invested + received) + position_value
        open_pos["mark_price_eur"] = round(mark, 2)
        open_pos["position_value_eur"] = round(position_value, 2)
        open_pos["pnl_eur"] = round(pnl, 2)
    else:
        open_pos["mark_price_eur"] = None
        open_pos["position_value_eur"] = None
        open_pos["pnl_eur"] = None

    rate_pauses = 0
    rate_resumes = 0
    deadman_ticks = 0
    deadman_disables = 0
    deadman_last_event: Optional[Dict[str, Any]] = None
    deadman_last_disable_ts: Optional[datetime] = None
    deadman_last_tick_ts: Optional[datetime] = None
    recent_events: List[Dict[str, Any]] = []

    for evt in exec_events:
        event_type = str(evt.get("event_type", ""))
        payload = evt.get("payload", {})
        ts = _event_time(evt)
        ts_iso = _iso(ts)
        summary = ""
        if isinstance(payload, dict):
            if event_type == "exec_report":
                summary = f"{payload.get('order_id', '')} {payload.get('status', '')}"
            elif event_type == "fill":
                summary = f"{payload.get('order_id', '')} qty={payload.get('qty_btc', '')} fee={payload.get('fee_eur', '')}"
            elif event_type == "deadman":
                summary = f"event={payload.get('event', '')} timeout={payload.get('timeout', '')} reason={payload.get('reason', '')}"
            elif event_type.startswith("exec_rate_limit"):
                summary = json.dumps(payload, ensure_ascii=True)
            elif event_type.endswith("error"):
                summary = str(payload.get("error", payload))
        recent_events.append({"ts": ts_iso, "event_type": event_type, "summary": summary})

        if event_type == "exec_rate_limit_pause":
            rate_pauses += 1
        if event_type == "exec_rate_limit_resume":
            rate_resumes += 1
        if event_type == "deadman" and isinstance(payload, dict):
            deadman_last_event = payload
            if payload.get("event") == "tick":
                deadman_ticks += 1
                if ts and (deadman_last_tick_ts is None or ts > deadman_last_tick_ts):
                    deadman_last_tick_ts = ts
            if payload.get("event") == "disable":
                deadman_disables += 1
                if ts and (deadman_last_disable_ts is None or ts > deadman_last_disable_ts):
                    deadman_last_disable_ts = ts

    if rate_pauses > rate_resumes:
        issues.append(
            {
                "level": "warn",
                "code": "rate_limit_unbalanced",
                "message": f"rate_limit pauses ({rate_pauses}) exceed resumes ({rate_resumes}).",
            }
        )

    if deadman_last_disable_ts is not None:
        trading_enabled = bool(core_data.get("trading_enabled", False))
        canary_mode = bool(exec_data.get("canary_mode", False))
        mode_raw = str(exec_data.get("mode", core_data.get("mode", ""))).strip().lower()
        disable_is_latest = deadman_last_tick_ts is None or deadman_last_tick_ts < deadman_last_disable_ts
        disable_age_sec = (now - deadman_last_disable_ts).total_seconds()
        if (
            trading_enabled
            and not canary_mode
            and mode_raw != "paper"
            and disable_is_latest
            and 0.0 <= disable_age_sec <= DEADMAN_DISABLE_WARN_SEC
        ):
            issues.append(
                {
                    "level": "warn",
                    "code": "deadman_disabled_while_trading",
                    "message": "Recent deadman disable without newer tick while core.trading_enabled=true.",
                }
            )

    recent_events.sort(key=lambda item: item.get("ts") or "", reverse=True)

    return {
        "generated_at": now.isoformat(),
        "exec": exec_data,
        "core": core_data,
        "trades": trades,
        "open_position": open_pos,
        "orders": orders,
        "issues": issues,
        "stats": {
            "journal_exec_events": len(exec_events),
            "rate_limit_pauses": rate_pauses,
            "rate_limit_resumes": rate_resumes,
            "deadman_ticks": deadman_ticks,
            "deadman_disables": deadman_disables,
        },
        "events": recent_events[:200],
    }


def _join_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _fetch_json(url: str, timeout_sec: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "exec-gui/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _make_handler(control_base_url: str, timeout_sec: float, default_lines: int):
    class ExecGuiHandler(BaseHTTPRequestHandler):
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
            if parsed.path == "/api/snapshot":
                query = urllib.parse.parse_qs(parsed.query)
                lines = default_lines
                try:
                    lines = max(10, min(2000, int(query.get("lines", [str(default_lines)])[0])))
                except Exception:
                    lines = default_lines
                try:
                    status_doc = _fetch_json(_join_url(control_base_url, "/status"), timeout_sec=timeout_sec)
                    journal_doc = _fetch_json(
                        _join_url(control_base_url, f"/journal?lines={lines}"),
                        timeout_sec=timeout_sec,
                    )
                    journal_items = journal_doc.get("items", []) if isinstance(journal_doc, dict) else []
                    snapshot = build_exec_snapshot(status_doc, journal_items)
                    self._send_json({"ok": True, "snapshot": snapshot})
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=502)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return ExecGuiHandler


def run_exec_gui(
    host: str = "127.0.0.1",
    port: int = 8110,
    control_base_url: str = "http://127.0.0.1:8100",
    timeout_sec: float = 2.0,
    journal_lines: int = 400,
) -> None:
    handler_cls = _make_handler(
        control_base_url=control_base_url,
        timeout_sec=timeout_sec,
        default_lines=journal_lines,
    )
    server = ThreadingHTTPServer((host, int(port)), handler_cls)
    print(f"exec gui listening on http://{host}:{port} (control={control_base_url})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Exec-only GUI for trading/processes/exec observability")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8110)
    parser.add_argument("--control-url", default="http://127.0.0.1:8100")
    parser.add_argument("--timeout-sec", type=float, default=2.0)
    parser.add_argument("--journal-lines", type=int, default=400)
    args = parser.parse_args()
    run_exec_gui(
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
  <title>Exec Module GUI</title>
  <style>
    :root { --bg:#f4f3ee; --fg:#1d1d1d; --card:#fffdf6; --line:#d8d3c4; --accent:#2c6e49; --warn:#b23a48; }
    body { margin:0; font-family:"IBM Plex Sans", "Segoe UI", sans-serif; background:radial-gradient(circle at 10% 0%, #fffdf6, #efe8d8); color:var(--fg); }
    .wrap { max-width:1200px; margin:18px auto; padding:0 12px; }
    .grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }
    .card { border:1px solid var(--line); background:var(--card); border-radius:10px; padding:10px; }
    h1 { margin:0 0 10px; font-size:24px; letter-spacing:.02em; }
    h2 { margin:0 0 6px; font-size:12px; text-transform:uppercase; color:#5a5a5a; }
    .v { font-weight:700; font-size:18px; }
    .row { margin-top:10px; }
    table { width:100%; border-collapse:collapse; font-size:13px; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
    th, td { border-bottom:1px solid #ece7d8; padding:7px 8px; text-align:left; vertical-align:top; }
    th { background:#f7f2e7; }
    .ok { color:var(--accent); font-weight:700; }
    .warn { color:var(--warn); font-weight:700; }
    .pos { color:var(--accent); font-weight:800; }
    .neg { color:var(--warn); font-weight:800; }
    .mono { font-family:ui-monospace, Menlo, Consolas, monospace; }
    .small { font-size:12px; color:#5f5f5f; }
    .pill { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:2px 8px; margin-right:6px; background:#faf5ea; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Exec-Only Observability</h1>
    <div class="small">Quelle: Control-API <span class="mono">/status</span> + <span class="mono">/journal</span> (nur exec-relevante Events)</div>
    <div class="small">Balances updated_at: <span id="bal_ts" class="mono">-</span></div>
    <div class="small">Lifecycle filter budget_cutoff_ts: <span id="cutoff" class="mono">-</span></div>
    <div class="row grid">
      <div class="card"><h2>Mode</h2><div id="mode" class="v">-</div></div>
      <div class="card"><h2>Open Orders</h2><div id="open_orders" class="v">-</div></div>
      <div class="card"><h2>Balance EUR</h2><div id="bal_eur" class="v">-</div></div>
      <div class="card"><h2>Balance XBT</h2><div id="bal_xbt" class="v">-</div></div>
      <div class="card"><h2>Rate Hits</h2><div id="rate_hits" class="v">-</div></div>
      <div class="card"><h2>Rate Limited</h2><div id="rate_limited" class="v">-</div></div>
      <div class="card"><h2>Deadman Hits</h2><div id="deadman_hits" class="v">-</div></div>
      <div class="card"><h2>Deadman Wait ms</h2><div id="deadman_wait" class="v">-</div></div>
      <div class="card"><h2>Journal Exec Events</h2><div id="journal_events" class="v">-</div></div>
      <div class="card"><h2>Core Trading</h2><div id="core_trading" class="v">-</div></div>
    </div>

    <div class="row">
      <h2>Kauf/Verkauf Runden (Netto)</h2>
      <div class="small">Netto = (Verkaufserloes nach Gebuehr) - (Kaufkosten inkl. Gebuehr). Gruen=Plus, Rot=Minus.</div>
      <div class="card" style="margin:10px 0">
        <h2>Aktuell Offen</h2>
        <div class="small">Ein offener Stand ist normal: das ist eine Runde, die noch nicht wieder bei 0 ist.</div>
        <div class="row">
          <span class="pill">since</span> <span id="pos_since" class="mono">-</span>
          <span class="pill">position_btc</span> <span id="pos_btc" class="mono">-</span>
          <span class="pill">mark</span> <span id="pos_mark" class="mono">-</span>
          <span class="pill">value_eur</span> <span id="pos_value" class="mono">-</span>
          <span class="pill">pnl_eur</span> <span id="pos_pnl" class="mono">-</span>
        </div>
      </div>
      <table>
        <thead><tr><th>entry</th><th>exit</th><th>qty_btc</th><th>avg_buy</th><th>avg_sell</th><th>invested_eur</th><th>received_eur</th><th>fees_eur</th><th>net_eur</th><th>net_pct</th><th>fills</th></tr></thead>
        <tbody id="trades"></tbody>
      </table>
    </div>

    <div class="row card">
      <h2>Plausibility Checks</h2>
      <div id="issues"></div>
    </div>

    <div class="row">
      <h2>Order Lifecycle (Exec)</h2>
      <table>
        <thead><tr><th>order_id</th><th>last_status</th><th>fill_qty_btc</th><th>fill_notional_eur</th><th>avg_fill_px_eur</th><th>fee_eur</th><th>updates</th><th>issues</th><th>last_update</th></tr></thead>
        <tbody id="orders"></tbody>
      </table>
    </div>

    <div class="row">
      <h2>Recent Exec Events</h2>
      <table>
        <thead><tr><th>ts</th><th>event_type</th><th>summary</th></tr></thead>
        <tbody id="events"></tbody>
      </table>
    </div>
  </div>
  <script>
    function txt(v) { return (v===undefined || v===null || v==="") ? "-" : String(v); }
    function esc(v) { return txt(v).replace(/[&<>]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[s])); }
    function isIsoTs(v) { return (typeof v === "string") && /^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}/.test(v); }
    function fmtTs(raw) {
      if (!raw) return "-";
      let s = String(raw).trim();
      // trim fractional seconds to milliseconds to keep Date parsing robust
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
    function pickBal(obj, keys) {
      if (!obj) return null;
      for (const k of keys) {
        if (obj[k] !== undefined && obj[k] !== null && obj[k] !== "") return obj[k];
      }
      return null;
    }
    function issuePill(issue) {
      const cls = issue.level === "warn" ? "warn" : "ok";
      return `<span class="pill ${cls}">${esc(issue.code)}: ${esc(issue.message)}</span>`;
    }

    async function refresh() {
      const r = await fetch("/api/snapshot?lines=500");
      const payload = await r.json();
      if (!payload.ok) throw new Error(payload.error || "snapshot failed");
      const s = payload.snapshot;
      const ex = s.exec || {};
      const core = s.core || {};
      const openPos = s.open_position || {};

      document.getElementById("mode").textContent = txt(ex.mode);
      document.getElementById("open_orders").textContent = txt(ex.open_orders_count);
      const bal = ex.balances || {};
      document.getElementById("bal_ts").textContent = fmtTs(ex.balances_updated_at);
      document.getElementById("bal_eur").textContent = txt(pickBal(bal, ["ZEUR","EUR","ZEUR.HOLD","ZEUR.F"]));
      document.getElementById("bal_xbt").textContent = txt(pickBal(bal, ["XXBT","XBT","BTC","XXBT.HOLD","XXBT.F"]));
      document.getElementById("rate_hits").textContent = txt(ex.rate_limit_hits);
      document.getElementById("rate_limited").textContent = txt(ex.rate_limited);
      document.getElementById("deadman_hits").textContent = txt(ex.deadman_rate_limit_hits);
      document.getElementById("deadman_wait").textContent = txt(ex.deadman_budget_wait_ms);
      document.getElementById("journal_events").textContent = txt((s.stats||{}).journal_exec_events);
      document.getElementById("core_trading").textContent = txt(core.trading_enabled);
      document.getElementById("cutoff").textContent = fmtTs(core.budget_cutoff_ts);

      // Trades / rounds
      const trades = (s.trades || []).slice(0, 120);
      document.getElementById("trades").innerHTML = trades.map(t => {
        const net = Number(t.net_eur || 0);
        const cls = net >= 0 ? "pos" : "neg";
        return `
          <tr>
            <td class="mono">${esc(fmtTs(t.entry_ts))}</td>
            <td class="mono">${esc(fmtTs(t.exit_ts))}</td>
            <td>${esc(t.qty_btc)}</td>
            <td>${esc(t.avg_buy_px_eur)}</td>
            <td>${esc(t.avg_sell_px_eur)}</td>
            <td>${esc(t.invested_eur)}</td>
            <td>${esc(t.received_eur)}</td>
            <td>${esc(t.fees_eur)}</td>
            <td class="${cls}">${esc(t.net_eur)}</td>
            <td class="${cls}">${esc(t.net_pct)}%</td>
            <td>${esc(t.fills)}</td>
          </tr>
        `;
      }).join("");

      document.getElementById("pos_since").textContent = fmtTs(openPos.entry_ts);
      document.getElementById("pos_btc").textContent = txt(openPos.position_btc);
      document.getElementById("pos_mark").textContent = txt(openPos.mark_price_eur);
      document.getElementById("pos_value").textContent = txt(openPos.position_value_eur);
      const pnl = openPos.pnl_eur;
      const pnlCls = (pnl === undefined || pnl === null) ? "" : (Number(pnl) >= 0 ? "pos" : "neg");
      const pnlEl = document.getElementById("pos_pnl");
      pnlEl.classList.remove("pos"); pnlEl.classList.remove("neg");
      if (pnlCls) pnlEl.classList.add(pnlCls);
      pnlEl.textContent = txt(pnl);

      const issues = s.issues || [];
      document.getElementById("issues").innerHTML = issues.length
        ? issues.map(issuePill).join("")
        : `<span class="ok">Keine Auffälligkeiten in den aktuellen Heuristiken.</span>`;

      const orders = (s.orders || []).slice(0, 150);
      document.getElementById("orders").innerHTML = orders.map(o => `
        <tr>
          <td class="mono">${esc(o.order_id)}</td>
          <td>${esc(o.last_status)}</td>
          <td>${esc(o.total_fill_qty_btc)}</td>
          <td>${esc(o.total_fill_notional_eur)}</td>
          <td>${esc(o.avg_fill_price_eur)}</td>
          <td>${esc(o.total_fee_eur)}</td>
          <td>${esc(o.updates)}</td>
          <td>${esc((o.issues || []).join(", "))}</td>
          <td class="mono">${esc(fmtTs(o.last_update_ts))}</td>
        </tr>
      `).join("");

      const events = (s.events || []).slice(0, 150);
      document.getElementById("events").innerHTML = events.map(e => `
        <tr>
          <td class="mono">${esc(fmtTs(e.ts))}</td>
          <td>${esc(e.event_type)}</td>
          <td>${esc(e.summary)}</td>
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
