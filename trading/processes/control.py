from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

from trading.ipc.events import ControlCommand, JournalEvent
from trading.ipc.queues import try_put
from trading.processes.context import ProcessContext

try:
    # FastAPI injects starlette Request; keep the type resolvable at module scope even with
    # `from __future__ import annotations` + nested route functions.
    from starlette.requests import Request as StarletteRequest
except Exception:  # pragma: no cover
    StarletteRequest = object  # type: ignore[misc,assignment]


def run_control(ctx: ProcessContext) -> None:
    try:
        from fastapi import Depends, FastAPI, HTTPException, WebSocket
        from fastapi.responses import HTMLResponse
        import uvicorn
    except Exception as exc:
        print(f"control process disabled: {exc}")
        return

    host = str(_cfg(ctx.config, "control.host", "127.0.0.1"))
    port = int(_cfg(ctx.config, "control.port", 8000))
    token = control_effective_token(ctx.config, os.environ)
    stale_warn_sec = float(_cfg(ctx.config, "control.stale_warn_sec", 5.0))
    md_interval_sec = max(0.0, float(_cfg(ctx.config, "md.interval_seconds", 0.0) or 0.0))
    default_market_age_warn_sec = stale_warn_sec
    if md_interval_sec > 0.0:
        # For bar-based feeds a healthy "latest market event" can naturally be older
        # than the generic process staleness threshold.
        default_market_age_warn_sec = max(stale_warn_sec, md_interval_sec * 2.5)
    control_started_at = datetime.now(timezone.utc)

    app = FastAPI()
    telemetry: Dict[str, Any] = {"updated_at": None, "updated_at_by_process": {}, "data": {}}
    telemetry_history: deque[Dict[str, Any]] = deque(maxlen=500)
    journal_path = _cfg(ctx.config, "journal.json_path", "logs/journal_events.jsonl")

    telemetry_lock = threading.Lock()
    last_telemetry_ts_by_process: dict[str, datetime] = {}

    # Cache journal-derived status to avoid parsing the journal file on every /status poll.
    last_journal_scan = {
        "ts": 0.0,
        "recent_exec_errors": [],
        "deadman_last_event": None,
        "deadman_last_event_ts": None,
        "deadman_last_disable_reason": None,
    }
    chart_scan_cache = {
        "ts": 0.0,
        "lines": 0,
        "max_points": 0,
        "max_fills": 0,
        "window_sec": 0,
        "data": {"points": [], "fills": []},
    }

    balance_refresh_sec = max(0.0, float(_cfg(ctx.config, "exec.balance_refresh_sec", 0.0) or 0.0))
    default_balances_warn_sec = 90.0
    if balance_refresh_sec > 0.0:
        default_balances_warn_sec = max(default_balances_warn_sec, balance_refresh_sec * 1.25)

    market_age_warn_sec = float(
        _cfg(ctx.config, "control.market_age_warn_sec", default_market_age_warn_sec)
    )
    balances_warn_sec = float(
        _cfg(ctx.config, "control.balances_warn_sec", default_balances_warn_sec)
    )
    journal_queue_warn = max(1, int(_cfg(ctx.config, "control.journal_queue_warn", 100)))
    deadman_tick_sec = max(
        1.0,
        float(
            _cfg(
                ctx.config,
                "exec.deadman_tick_sec",
                _cfg(ctx.config, "exec.deadman.tick_sec", 5.0),
            )
        ),
    )
    live_exchange = str(
        _cfg(ctx.config, "live.exchange", _cfg(ctx.config, "md.exchange", ""))
        or ""
    ).strip().lower()
    deadman_in_paper = bool(_cfg(ctx.config, "exec.deadman_in_paper", False))
    deadman_on_binance = bool(_cfg(ctx.config, "exec.enable_deadman_on_binance", False))

    external_probe_lock = threading.Lock()
    external_probe_cache: dict[str, dict[str, Any]] = {}
    external_probe_interval_sec = 2.0
    external_probe_timeout_sec = 0.35

    def _send_journal(event_type: str, payload: Dict[str, Any]) -> None:
        evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
        try_put(ctx.q_journal, evt)

    def telemetry_consumer() -> None:
        while not ctx.stop_event.is_set():
            try:
                evt = ctx.q_telemetry.get(timeout=0.5)
                with telemetry_lock:
                    telemetry["updated_at"] = evt.ts.isoformat()
                    telemetry["updated_at_by_process"][evt.process] = evt.ts.isoformat()
                    last_telemetry_ts_by_process[evt.process] = evt.ts
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

    def require_write_access(request: StarletteRequest) -> None:
        header_token = request.headers.get("X-Control-Token")
        allowed, status, detail = control_write_auth_decision(
            host=host,
            token=token,
            header_token=header_token,
        )
        if not allowed:
            raise HTTPException(status_code=status, detail=detail)

    def send_cmd(
        action: str,
        reason: str | None = None,
        *,
        request: StarletteRequest | None = None,
        targets: Optional[list[str]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        targets = targets or ["core", "exec"]
        cmd_payload = payload or {}
        cmd = ControlCommand(ts=datetime.now(timezone.utc), action=action, reason=reason, payload=cmd_payload)

        evt_payload: Dict[str, Any] = {
            "action": action,
            "reason": reason,
            "targets": list(targets),
            "payload": cmd_payload,
        }
        if request is not None:
            try:
                evt_payload["path"] = str(request.url.path)
                evt_payload["client_host"] = request.client.host if request.client else None
                evt_payload["user_agent"] = request.headers.get("user-agent")
            except Exception:
                pass
        _send_journal("control_cmd_sent", evt_payload)

        # Note: for now control always targets both core + exec.
        try_put(ctx.q_control_core, cmd)
        try_put(ctx.q_control_exec, cmd)

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/status")
    async def status() -> Dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = time.time()

        def _iso_age_sec(ts_iso: Any) -> float | None:
            if ts_iso is None:
                return None
            try:
                dt = datetime.fromisoformat(str(ts_iso))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (now_dt - dt).total_seconds())
            except Exception:
                return None

        def _as_float(v: Any) -> float | None:
            try:
                f = float(v)
            except Exception:
                return None
            if f != f:  # NaN
                return None
            return f

        with telemetry_lock:
            updated_at = telemetry.get("updated_at")
            updated_at_by_process = dict(telemetry.get("updated_at_by_process", {}) or {})
            data = {k: dict(v) for k, v in (telemetry.get("data", {}) or {}).items()}
            last_by_process = dict(last_telemetry_ts_by_process)

        staleness: Dict[str, Optional[float]] = {}
        for proc, ts in last_by_process.items():
            try:
                staleness[proc] = max(0.0, (now_dt - ts).total_seconds())
            except Exception:
                staleness[proc] = None

        core = data.get("core", {}) if isinstance(data.get("core"), dict) else {}
        exec_ = data.get("exec", {}) if isinstance(data.get("exec"), dict) else {}

        aggregate: Dict[str, Any] = {
            "mode": core.get("mode") or exec_.get("mode"),
            "trading_enabled": core.get("trading_enabled"),
            "trading_disable_reason": core.get("trading_disable_reason"),
            "open_orders_count": exec_.get("open_orders_count"),
            "rate_limited": exec_.get("rate_limited"),
            "balances_updated_at": exec_.get("balances_updated_at"),
            "balances_count": exec_.get("balances_count"),
        }

        # Scan journal-derived signals occasionally; /status is polled frequently by the dashboard.
        if journal_path and now - float(last_journal_scan["ts"]) >= 2.0:
            items = _tail_json_lines(str(journal_path), 1200)
            errs: list[dict] = []
            deadman_last_event: str | None = None
            deadman_last_event_ts: str | None = None
            deadman_last_disable_reason: str | None = None
            for item in items:
                et = str(item.get("event_type", ""))
                if et.startswith("exec_") and "error" in et:
                    errs.append(item)
                if et == "deadman":
                    payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
                    deadman_evt = str(payload.get("event", "")).strip().lower()
                    if deadman_evt:
                        deadman_last_event = deadman_evt
                        deadman_last_event_ts = str(item.get("ts", "")) or None
                        if deadman_evt == "disable":
                            deadman_last_disable_reason = str(payload.get("reason", "")).strip() or None

            last_journal_scan["recent_exec_errors"] = errs[-10:]
            last_journal_scan["deadman_last_event"] = deadman_last_event
            last_journal_scan["deadman_last_event_ts"] = deadman_last_event_ts
            last_journal_scan["deadman_last_disable_reason"] = deadman_last_disable_reason
            last_journal_scan["ts"] = now

        recent_exec_errors = list(last_journal_scan.get("recent_exec_errors") or [])
        deadman_last_event = str(last_journal_scan.get("deadman_last_event") or "")
        deadman_last_event_ts = last_journal_scan.get("deadman_last_event_ts")
        deadman_last_disable_reason = last_journal_scan.get("deadman_last_disable_reason")

        impact_url = (
            str(_cfg(ctx.config, "control.external_guis.impact", "") or "").strip()
            or str(_cfg(ctx.config, "control.external_guis.impact_console", "") or "").strip()
            or str(os.environ.get("IMPACT_URL", "") or "").strip()
            or str(os.environ.get("IMPACT_CONSOLE_URL", "") or "").strip()
            or _normalize_base_url(str(_cfg(ctx.config, "impact.api_url", "") or "").strip())
            or "http://127.0.0.1:8011/"
        )
        impact_url = _normalize_base_url(impact_url) or "http://127.0.0.1:8011/"

        exec_url = (
            str(_cfg(ctx.config, "control.external_guis.exec", "") or "").strip()
            or str(_cfg(ctx.config, "control.external_guis.exec_gui", "") or "").strip()
            or str(os.environ.get("EXEC_URL", "") or "").strip()
            or str(os.environ.get("EXEC_GUI_URL", "") or "").strip()
            or "http://127.0.0.1:8110/"
        )
        exec_url = _normalize_base_url(exec_url) or "http://127.0.0.1:8110/"

        journal_url = (
            str(_cfg(ctx.config, "control.external_guis.journal", "") or "").strip()
            or str(os.environ.get("JOURNAL_URL", "") or "").strip()
            or str(os.environ.get("JOURNAL_GUI_URL", "") or "").strip()
            or "http://127.0.0.1:8120/"
        )
        journal_url = _normalize_base_url(journal_url) or "http://127.0.0.1:8120/"

        core_url = (
            str(_cfg(ctx.config, "control.external_guis.core", "") or "").strip()
            or str(os.environ.get("CORE_URL", "") or "").strip()
            or str(os.environ.get("CORE_GUI_URL", "") or "").strip()
            or "http://127.0.0.1:8130/"
        )
        core_url = _normalize_base_url(core_url) or "http://127.0.0.1:8130/"

        md_url = (
            str(_cfg(ctx.config, "control.external_guis.md", "") or "").strip()
            or str(os.environ.get("MD_URL", "") or "").strip()
            or str(os.environ.get("MD_GUI_URL", "") or "").strip()
            or "http://127.0.0.1:8140/"
        )
        md_url = _normalize_base_url(md_url) or "http://127.0.0.1:8140/"

        # Canonical names used by the dashboard.
        external_guis = {
            "impact": impact_url,
            "exec": exec_url,
            "journal": journal_url,
            "core": core_url,
            "md": md_url,
        }

        # Legacy aliases (kept for older dashboards / scripts).
        external_guis_legacy = {
            "impact_console": impact_url,
            "exec_gui": exec_url,
            "core_gui": core_url,
            "md_gui": md_url,
        }

        external_guis_warnings: list[str] = []
        urls = [(k, v) for k, v in external_guis.items() if isinstance(v, str) and v]
        for i in range(len(urls)):
            for j in range(i + 1, len(urls)):
                a_name, a_url = urls[i]
                b_name, b_url = urls[j]
                if a_url == b_url:
                    external_guis_warnings.append(
                        f"port_conflict: {a_name} and {b_name} both point to {a_url}"
                    )

        external_guis_status: dict[str, dict[str, Any]] = {}
        for name, base_url in external_guis.items():
            if not isinstance(base_url, str) or not base_url:
                continue
            external_guis_status[name] = await asyncio.to_thread(
                _probe_loopback_http_cached,
                base_url=base_url,
                now=now,
                cache=external_probe_cache,
                lock=external_probe_lock,
                min_interval_sec=external_probe_interval_sec,
                timeout_sec=external_probe_timeout_sec,
            )

        mode = str(core.get("mode") or exec_.get("mode") or "").strip().lower()
        core_stale = _as_float(staleness.get("core"))
        exec_stale = _as_float(staleness.get("exec"))
        market_age = _as_float(core.get("last_market_event_age"))
        balances_age = _iso_age_sec(exec_.get("balances_updated_at"))
        queue_journal_depth = _as_float(core.get("queue_journal"))
        queue_journal_ok = queue_journal_depth is not None and queue_journal_depth <= float(journal_queue_warn)
        trading_enabled = bool(core.get("trading_enabled"))
        rate_limited = bool(exec_.get("rate_limited"))

        # Deadman is only mandatory in live mode when it is expected to be active.
        # Binance lanes often run with deadman intentionally disabled in config.
        deadman_event_age = _iso_age_sec(deadman_last_event_ts)
        deadman_ok = True
        deadman_detail = "not_live_mode"
        if mode == "live":
            deadman_required = bool(
                deadman_in_paper or live_exchange != "binance" or deadman_on_binance
            )
            if deadman_required:
                deadman_ok = (
                    deadman_last_event == "tick"
                    and deadman_event_age is not None
                    and deadman_event_age <= max(20.0, deadman_tick_sec * 3.0)
                )
                if deadman_ok:
                    deadman_detail = "armed"
                elif deadman_last_event == "disable":
                    deadman_detail = f"disabled:{deadman_last_disable_reason or 'unknown'}"
                elif deadman_last_event:
                    deadman_detail = f"last_event:{deadman_last_event}"
                else:
                    deadman_detail = "no_deadman_event_seen"
            else:
                deadman_ok = True
                deadman_detail = "not_required"

        balances_required = mode == "live"
        balances_ok = True
        balances_detail = "not_required"
        if balances_required:
            balances_ok = balances_age is not None and balances_age <= balances_warn_sec
            balances_detail = f"age_sec={balances_age if balances_age is not None else 'n/a'}"

        core_fresh_ok = core_stale is not None and core_stale <= stale_warn_sec
        exec_fresh_ok = exec_stale is not None and exec_stale <= stale_warn_sec
        market_fresh_ok = market_age is not None and market_age <= market_age_warn_sec
        journal_ok = bool(os.path.exists(str(journal_path))) and queue_journal_ok
        rate_limit_ok = not rate_limited

        overview_checks = {
            "trading_enabled": {
                "ok": trading_enabled,
                "detail": str(core.get("trading_disable_reason") or "enabled"),
                "value": core.get("trading_enabled"),
            },
            "core_fresh": {
                "ok": core_fresh_ok,
                "detail": f"stale_sec={core_stale}",
                "value": core_stale,
            },
            "exec_fresh": {
                "ok": exec_fresh_ok,
                "detail": f"stale_sec={exec_stale}",
                "value": exec_stale,
            },
            "market_fresh": {
                "ok": market_fresh_ok,
                "detail": f"age_sec={market_age}",
                "value": market_age,
            },
            "balances_fresh": {
                "ok": balances_ok,
                "detail": balances_detail,
                "value": balances_age,
            },
            "rate_limit_clear": {
                "ok": rate_limit_ok,
                "detail": f"rate_limited={rate_limited}",
                "value": exec_.get("rate_limited"),
            },
            "deadman_armed": {
                "ok": deadman_ok,
                "detail": deadman_detail,
                "value": deadman_last_event or None,
            },
            "journal_healthy": {
                "ok": journal_ok,
                "detail": f"queue_journal={queue_journal_depth}",
                "value": queue_journal_depth,
            },
        }
        overview_trade_ready = bool(all(bool(v.get("ok")) for v in overview_checks.values()))

        return {
            "time": now_dt.isoformat(),
            "updated_at": updated_at,
            "updated_at_by_process": updated_at_by_process,
            "data": data,
            "staleness_sec_by_process": staleness,
            "stale_warn_sec": stale_warn_sec,
            "aggregate": aggregate,
            "recent_exec_errors": recent_exec_errors,
            "external_guis": external_guis,
            "external_guis_legacy": external_guis_legacy,
            "external_guis_warnings": external_guis_warnings,
            "external_guis_status": external_guis_status,
            "deadman": {
                "last_event": deadman_last_event or None,
                "last_event_ts": deadman_last_event_ts,
                "last_event_age_sec": deadman_event_age,
                "last_disable_reason": deadman_last_disable_reason,
            },
            "overview_checks": overview_checks,
            "overview_trade_ready": overview_trade_ready,
            "overview_thresholds": {
                "stale_warn_sec": stale_warn_sec,
                "market_age_warn_sec": market_age_warn_sec,
                "md_interval_sec": md_interval_sec,
                "balances_warn_sec": balances_warn_sec,
                "journal_queue_warn": journal_queue_warn,
                "deadman_tick_sec": deadman_tick_sec,
            },
            "write_auth": {
                "token_required": bool(token),
                "loopback_only_without_token": True,
                "host": host,
            },
        }

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
        return {"items": _tail_json_lines(str(journal_path), lines)}

    @app.get("/overview_chart")
    async def overview_chart(
        lines: int = 3000,
        max_points: int = 1200,
        max_fills: int = 200,
        window_sec: int = 86400,
    ) -> Dict[str, Any]:
        lines = max(200, min(12000, int(lines)))
        max_points = max(200, min(3000, int(max_points)))
        max_fills = max(20, min(500, int(max_fills)))
        window_sec = max(60, min(86400, int(window_sec)))
        now = time.time()
        now_dt = datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(seconds=float(window_sec))

        def _as_float(v: Any) -> float | None:
            try:
                f = float(v)
            except Exception:
                return None
            if f != f:  # NaN
                return None
            return f

        def _downsample_keep_ends(items: list[dict], cap: int) -> list[dict]:
            if len(items) <= cap:
                return items
            # Preserve local highs/lows per bucket to avoid aliasing into an almost
            # linear line when replayed data is periodic.
            bucket_count = max(1, cap // 2)
            out: list[dict] = []
            n = len(items)
            for b in range(bucket_count):
                start = int((b * n) / bucket_count)
                end = int(((b + 1) * n) / bucket_count)
                if end <= start:
                    continue
                chunk = items[start:end]
                if not chunk:
                    continue
                min_idx = 0
                max_idx = 0
                min_price = float(chunk[0].get("price", 0.0))
                max_price = min_price
                for i in range(1, len(chunk)):
                    p = float(chunk[i].get("price", 0.0))
                    if p < min_price:
                        min_price = p
                        min_idx = i
                    if p > max_price:
                        max_price = p
                        max_idx = i
                if min_idx <= max_idx:
                    out.append(chunk[min_idx])
                    if max_idx != min_idx:
                        out.append(chunk[max_idx])
                else:
                    out.append(chunk[max_idx])
                    out.append(chunk[min_idx])
            if items and (not out or out[0] != items[0]):
                out.insert(0, items[0])
            if items and (not out or out[-1] != items[-1]):
                out.append(items[-1])
            if len(out) > cap:
                out = out[-cap:]
            return out

        def _parse_iso(ts: Any) -> datetime | None:
            if ts is None:
                return None
            try:
                dt = datetime.fromisoformat(str(ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                return None

        should_rescan = (
            now - float(chart_scan_cache.get("ts", 0.0)) >= 1.0
            or int(chart_scan_cache.get("lines", 0)) != lines
            or int(chart_scan_cache.get("max_points", 0)) != max_points
            or int(chart_scan_cache.get("max_fills", 0)) != max_fills
            or int(chart_scan_cache.get("window_sec", 0)) != window_sec
        )
        if should_rescan:
            items = _tail_json_lines(str(journal_path), lines)
            points: list[dict[str, Any]] = []
            fills: list[dict[str, Any]] = []
            for item in items:
                journal_ts = str(item.get("ts") or "").strip()
                if not journal_ts:
                    continue
                journal_dt = _parse_iso(journal_ts)
                if journal_dt is None or journal_dt < cutoff_dt:
                    continue
                et = str(item.get("event_type", "")).strip().lower()
                payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
                if et == "market":
                    price = _as_float(payload.get("close"))
                    if price is None:
                        continue
                    points.append({"ts": journal_ts, "price": price, "market_ts": str(payload.get("ts") or "")})
                elif et == "fill":
                    price = _as_float(payload.get("price"))
                    qty = _as_float(payload.get("qty_btc"))
                    if price is None:
                        continue
                    side = str(payload.get("side", "")).strip().lower()
                    fills.append(
                        {
                            "ts": journal_ts,
                            "price": price,
                            "side": side,
                            "qty_btc": qty,
                            "fill_ts": str(payload.get("ts") or ""),
                        }
                    )

            points.sort(key=lambda x: str(x.get("ts") or ""))
            fills.sort(key=lambda x: str(x.get("ts") or ""))
            points = _downsample_keep_ends(points, max_points)
            if len(fills) > max_fills:
                fills = fills[-max_fills:]

            chart_scan_cache["ts"] = now
            chart_scan_cache["lines"] = lines
            chart_scan_cache["max_points"] = max_points
            chart_scan_cache["max_fills"] = max_fills
            chart_scan_cache["window_sec"] = window_sec
            chart_scan_cache["data"] = {"points": points, "fills": fills}

        cached = chart_scan_cache.get("data", {}) if isinstance(chart_scan_cache.get("data"), dict) else {}
        points = list(cached.get("points") or [])
        fills = list(cached.get("fills") or [])

        with telemetry_lock:
            core = dict((telemetry.get("data", {}) or {}).get("core", {}) or {})
            exec_ = dict((telemetry.get("data", {}) or {}).get("exec", {}) or {})

        mode = str(core.get("mode") or exec_.get("mode") or "").strip().lower()
        pos_btc = _as_float(core.get("position_btc")) or 0.0
        avg_entry = _as_float(core.get("avg_entry_price")) or 0.0
        taker_fee_bps = max(0.0, float(_cfg(ctx.config, "cost.taker_fee_bps", 0.0)))
        min_exit_profit_bps = max(0.0, float(_cfg(ctx.config, "risk.min_exit_profit_bps", 0.0)))
        expected_exit_cost_bps = max(
            0.0,
            _as_float(core.get("expected_cost_bps")) or taker_fee_bps,
        )
        break_even_price = 0.0
        if pos_btc > 0.0 and avg_entry > 0.0:
            break_even_price = avg_entry * (
                1.0 + ((expected_exit_cost_bps + min_exit_profit_bps) / 10000.0)
            )

        break_even_lines: list[dict[str, Any]] = []
        if break_even_price > 0.0:
            break_even_lines.append({"label": "break_even", "price": break_even_price})
        if avg_entry > 0.0 and pos_btc > 0.0:
            break_even_lines.append({"label": "avg_entry", "price": avg_entry})

        return {
            "mode": mode,
            "points": points,
            "fills": fills,
            "break_even_lines": break_even_lines,
            "position_btc": pos_btc,
            "avg_entry_price": avg_entry if avg_entry > 0.0 else None,
            "mark_price": _as_float(core.get("mark_price")),
            "meta": {
                "lines_scanned": lines,
                "points": len(points),
                "fills": len(fills),
                "window_sec": window_sec,
                "cutoff_ts": cutoff_dt.isoformat(),
                "control_started_at": control_started_at.isoformat(),
                "taker_fee_bps": taker_fee_bps,
                "expected_exit_cost_bps": expected_exit_cost_bps,
                "min_exit_profit_bps": min_exit_profit_bps,
            },
        }

    @app.post("/start", dependencies=[Depends(require_write_access)])
    async def start(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("START", request=request)
        return {"ok": True}

    @app.post("/cancel_all", dependencies=[Depends(require_write_access)])
    async def cancel_all(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("CANCEL_ALL", request=request)
        return {"ok": True}

    @app.post("/stop", dependencies=[Depends(require_write_access)])
    async def stop(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("STOP", request=request)
        send_cmd("CANCEL_ALL", request=request)
        return {"ok": True}

    @app.post("/pause", dependencies=[Depends(require_write_access)])
    async def pause(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("PAUSE", request=request)
        return {"ok": True}

    @app.post("/resume", dependencies=[Depends(require_write_access)])
    async def resume(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("RESUME", request=request)
        return {"ok": True}

    @app.post("/reload_config", dependencies=[Depends(require_write_access)])
    async def reload_config(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("RELOAD", request=request)
        return {"ok": True}

    @app.post("/reload", dependencies=[Depends(require_write_access)])
    async def reload(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("RELOAD", request=request)
        return {"ok": True}

    @app.post("/flatten", dependencies=[Depends(require_write_access)])
    async def flatten(request: StarletteRequest) -> Dict[str, Any]:
        send_cmd("FLATTEN", "flatten", request=request)
        send_cmd("PAUSE", "flatten", request=request)
        send_cmd("CANCEL_ALL", "flatten", request=request)
        return {"ok": True}

    @app.post("/shutdown", dependencies=[Depends(require_write_access)])
    async def shutdown(request: StarletteRequest) -> Dict[str, Any]:
        # Send STOP/CANCEL_ALL first, then set stop_event.
        send_cmd("STOP", "shutdown", request=request)
        send_cmd("CANCEL_ALL", "shutdown", request=request)
        _send_journal("control_shutdown_requested", {"reason": "shutdown"})
        ctx.stop_event.set()
        return {"ok": True}

    @app.post("/set_budget", dependencies=[Depends(require_write_access)])
    async def set_budget(request: StarletteRequest) -> Dict[str, Any]:
        # Budget override is only meaningful when no real orders are possible.
        if ctx.mode not in {"paper", "sim"}:
            raise HTTPException(status_code=403, detail="budget_override_only_supported_in_paper_or_sim_mode")

        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid_json_body")

        def _as_pos_float(key: str) -> float:
            v = body.get(key)
            try:
                f = float(v)
            except Exception:
                raise HTTPException(status_code=400, detail=f"invalid_{key}")
            if not (f > 0.0):
                raise HTTPException(status_code=400, detail=f"invalid_{key}")
            return f

        # Backwards compatible payload:
        # - New: {"amount_eur": X} -> starting_cash_eur=max_exposure_eur=X (single-field sim UX)
        # - Old: {"starting_cash_eur": X, "max_exposure_eur": Y}
        if "amount_eur" in body:
            amount_eur = _as_pos_float("amount_eur")
            starting_cash_eur = float(amount_eur)
            max_exposure_eur = float(amount_eur)
        else:
            starting_cash_eur = _as_pos_float("starting_cash_eur")
            max_exposure_eur = _as_pos_float("max_exposure_eur")
        reset = bool(body.get("reset", True))

        # Stop/cancel first, then apply new budget. Order isn't guaranteed across processes, so
        # SET_BUDGET handlers are also defensive.
        send_cmd("STOP", "budget_reset", request=request)
        send_cmd("CANCEL_ALL", "budget_reset", request=request)
        send_cmd(
            "SET_BUDGET",
            "budget_reset",
            request=request,
            payload={
                "starting_cash_eur": starting_cash_eur,
                "max_exposure_eur": max_exposure_eur,
                "reset": reset,
            },
        )
        _send_journal(
            "control_budget_set",
            {
                "starting_cash_eur": starting_cash_eur,
                "max_exposure_eur": max_exposure_eur,
                "reset": reset,
            },
        )

        return {"ok": True, "starting_cash_eur": starting_cash_eur, "max_exposure_eur": max_exposure_eur, "reset": reset}

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

    @app.get("/app", response_class=HTMLResponse)
    async def dashboard_app() -> str:
        return _dashboard_html()

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))

    def _watch_stop_event() -> None:
        while not server.should_exit:
            if ctx.stop_event.is_set():
                server.should_exit = True
                break
            time.sleep(0.2)

    threading.Thread(target=_watch_stop_event, daemon=True).start()
    server.run()


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def is_loopback_host(host: str) -> bool:
    host = str(host or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def control_effective_token(cfg: Dict[str, Any], env: Mapping[str, str] | None = None) -> str | None:
    env = env or {}
    token = _cfg(cfg, "control.token", None)
    if token is None or str(token).strip() == "":
        token = env.get("CONTROL_TOKEN")
    if token is None:
        return None
    token = str(token).strip()
    return token or None


def control_write_auth_decision(*, host: str, token: str | None, header_token: str | None) -> Tuple[bool, int, str]:
    # If a token is configured (config or env), always enforce it.
    if token:
        if header_token == token:
            return True, 200, "ok"
        return False, 401, "missing_or_invalid_x_control_token"

    # Without a token, only allow writes if the server binds to loopback.
    if is_loopback_host(host):
        return True, 200, "ok_loopback"

    return False, 403, "write_endpoints_disabled_without_token_for_non_loopback_host"


def _tail_json_lines(path: str, lines: int) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    raw_lines = _tail_lines_from_end(path, lines)
    out: list[dict] = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception:
            continue
    return out


def _tail_lines_from_end(path: str, lines: int, *, chunk_size: int = 8192, max_bytes: int | None = None) -> list[str]:
    lines = max(0, int(lines))
    if lines <= 0:
        return []

    # Read from file end backwards until we have enough lines or hit the byte cap.
    collected: list[bytes] = []
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


def _normalize_base_url(url: str) -> str | None:
    """
    Convert e.g. http://127.0.0.1:8011/arrow?window=1h into http://127.0.0.1:8011/
    """
    url = str(url or "").strip()
    if not url:
        return None
    try:
        u = urlparse(url)
    except Exception:
        return None
    if u.scheme not in {"http", "https"}:
        return None
    if not u.netloc:
        return None
    return f"{u.scheme}://{u.netloc}/"


def _probe_loopback_http_cached(
    *,
    base_url: str,
    now: float,
    cache: dict[str, dict[str, Any]],
    lock: threading.Lock,
    min_interval_sec: float,
    timeout_sec: float,
) -> dict[str, Any]:
    """
    Best-effort reachability probe for loopback targets only (SSRF-safe default).
    Returns a dict suitable for JSON.
    """
    base_url = _normalize_base_url(base_url) or ""
    if not base_url:
        return {"ok": None, "skipped": True, "reason": "invalid_url"}

    try:
        u = urlparse(base_url)
        host = u.hostname or ""
    except Exception:
        host = ""

    if not is_loopback_host(host):
        return {"ok": None, "skipped": True, "reason": "non_loopback"}

    with lock:
        cached = cache.get(base_url)
        if cached and (now - float(cached.get("ts", 0.0))) < float(min_interval_sec):
            return dict(cached)

    result: dict[str, Any] = {"ts": now, "url": base_url}
    try:
        req = UrlRequest(base_url, headers={"User-Agent": "control-plane/1.0"})
        with urlopen(req, timeout=float(timeout_sec)) as resp:
            code = getattr(resp, "status", None)
        # If we got an HTTP response at all, the service is reachable.
        result.update({"ok": True, "http_status": code})
    except Exception as exc:
        result.update({"ok": False, "error": str(exc)[:200]})

    with lock:
        cache[base_url] = dict(result)
    return dict(result)


def _dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trading Control</title>
  <style>
    :root { --bg:#0f172a; --panel:#111827; --muted:#94a3b8; --text:#e2e8f0; --ok:#22c55e; --bad:#ef4444; --line:#60a5fa; }
    body { margin:0; font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:linear-gradient(140deg,#0b1220,#111827); color:var(--text); }
    .wrap { margin:18px 0; padding:0 16px; box-sizing:border-box; }
    h1 { margin:0 0 12px 0; font-size:22px; }
    h2 { margin:0 0 8px 0; font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
    .row { display:flex; gap:8px; flex-wrap:wrap; margin:10px 0; align-items:center; }
    .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }
    .card { background:rgba(17,24,39,.96); border:1px solid #334155; border-radius:10px; padding:12px; }
    .big { font-size:20px; font-weight:700; }
    .v { font-size:16px; font-weight:600; }
    .small { font-size:12px; color:var(--muted); }
    .mono { font-family:ui-monospace, Menlo, Consolas, monospace; }
    .hidden { display:none; }
    button { background:#0b1220; color:var(--text); border:1px solid #334155; border-radius:8px; padding:8px 12px; cursor:pointer; }
    button:hover { border-color:#64748b; }
    button.tab.active { border-color:#93c5fd; box-shadow:0 0 0 2px rgba(147,197,253,.55); }
    button.warn { border-color:#f59e0b; }
    button.bad { border-color:#ef4444; }
    input { background:#0b1220; border:1px solid #334155; border-radius:8px; padding:8px 10px; color:var(--text); min-width:260px; }
    a { color:#93c5fd; }
    iframe { width:100%; height:78vh; border:1px solid #334155; border-radius:10px; background:#0b1220; }
    .pill { font-size:12px; padding:4px 8px; border-radius:999px; border:1px solid #334155; color:var(--muted); }
    .pill.ok { color:#bbf7d0; border-color:rgba(34,197,94,.6); }
    .pill.bad { color:#fecaca; border-color:rgba(239,68,68,.6); }
    .banner { display:none; background:rgba(127,29,29,.3); border:1px solid rgba(239,68,68,.65); color:#fecaca; padding:10px 12px; border-radius:10px; margin:12px 0; }
    .ready { font-size:18px; font-weight:800; letter-spacing:.08em; }
    .ready.ok { color:#22c55e; }
    .ready.bad { color:#ef4444; }
    .lights { display:grid; gap:8px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
    .light { border:1px solid #334155; border-radius:9px; padding:8px 10px; background:#0b1220; }
    .light .top { display:flex; align-items:center; gap:8px; }
    .dot { width:11px; height:11px; border-radius:999px; background:#ef4444; box-shadow:0 0 0 1px rgba(255,255,255,.08); }
    .dot.ok { background:#22c55e; }
    .dot.bad { background:#ef4444; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Trading Control</h1>

    <div class="row">
      <span class="pill">X-Control-Token</span>
      <input id="token" type="password" placeholder="optional token for write endpoints" />
      <button onclick="saveToken()">Save</button>
      <button onclick="clearToken()">Clear</button>
      <span class="small">Write endpoints: token if configured, otherwise loopback-only.</span>
    </div>

    <div class="row" id="tabs">
      <button class="tab" data-tab="overview" onclick="setTab('overview')">Overview</button>
      <button class="tab" data-tab="impact" onclick="setTab('impact')">Impact</button>
    </div>

    <div class="row">
      <button data-cmd="1" onclick="cmd('/start', this)">Start</button>
      <button data-cmd="1" onclick="cmd('/pause', this)" class="warn">Pause</button>
      <button data-cmd="1" onclick="cmd('/resume', this)">Resume</button>
      <button data-cmd="1" onclick="cmd('/cancel_all', this)" class="warn">Cancel All</button>
      <button data-cmd="1" onclick="cmd('/stop', this)" class="bad">Stop + CancelAll</button>
      <button data-cmd="1" onclick="cmd('/flatten', this)" class="bad">Flatten</button>
      <button data-cmd="1" onclick="cmd('/reload_config', this)" class="warn">Reload Config</button>
      <button data-cmd="1" onclick="cmd('/shutdown', this)" class="bad">Shutdown</button>
      <span class="small mono" id="whereami"></span>
    </div>

    <div id="banner" class="banner"></div>

    <div id="tab_overview" class="tabpane">
      <div class="grid">
        <div class="card">
          <h2>Trade Ready</h2>
          <div id="trade_ready" class="ready bad">RED</div>
          <div id="trade_ready_detail" class="small mono">-</div>
        </div>
        <div class="card"><h2>Mode</h2><div id="mode" class="v">-</div></div>
        <div class="card"><h2>Trading Enabled</h2><div id="enabled" class="v">-</div><div id="disable_reason" class="small mono">-</div></div>
        <div class="card"><h2>Last Market Age (s)</h2><div id="age" class="v">-</div></div>
        <div class="card"><h2>Core Stale (s)</h2><div id="stale_core" class="v">-</div></div>
        <div class="card"><h2>Exec Stale (s)</h2><div id="stale_exec" class="v">-</div></div>
        <div class="card"><h2>Open Orders</h2><div id="open_orders" class="v">-</div></div>
        <div class="card"><h2>Balances Age (s)</h2><div id="balances_age" class="v">-</div></div>
        <div class="card"><h2>Rate Limited</h2><div id="rate_limited" class="v">-</div></div>
        <div class="card"><h2>Deadman</h2><div id="deadman" class="v">-</div></div>
        <div class="card"><h2>Equity (EUR)</h2><div id="equity" class="v">-</div></div>
        <div class="card"><h2>Position (BTC)</h2><div id="pos_btc" class="v">-</div></div>
        <div class="card"><h2>Avg Entry</h2><div id="avg_entry" class="v">-</div></div>
        <div class="card"><h2>Mark Price</h2><div id="mark_price" class="v">-</div></div>
      </div>

      <div class="card" style="margin-top:12px">
        <h2>Safety Lights</h2>
        <div id="lights" class="lights"></div>
      </div>

      <div class="card" style="margin-top:12px">
        <h2>Price Line + Trades + Break-Even</h2>
        <canvas id="price_chart" width="1280" height="260" style="width:100%; height:220px; display:block; background:#0b1220; border:1px solid #334155; border-radius:10px"></canvas>
        <div id="price_chart_meta" class="small mono">-</div>
      </div>

      <div class="card" style="margin-top:12px">
        <h2>Simulation Budget (Paper/Sim)</h2>
        <div class="row">
          <span class="pill" id="budget_start_label">Start EUR</span>
          <input id="budget_start" type="number" step="0.01" placeholder="e.g. 1000" style="min-width:160px" />
          <span class="pill" id="budget_exposure_label">Max Exposure EUR</span>
          <input id="budget_exposure" type="number" step="0.01" placeholder="e.g. 200" style="min-width:160px" />
          <button class="warn" onclick="applyBudget()">Apply Budget (Reset)</button>
          <span id="budget_result" class="small mono"></span>
        </div>
        <div class="small">Only for paper/sim. Resets cash/position/PnL and drops pending intents.</div>
      </div>
    </div>

    <div id="tab_impact" class="tabpane hidden">
      <div class="card">
        <h2>Impact Console</h2>
        <div class="row">
          <span class="pill">URL</span>
          <input id="impact_url" placeholder="http://127.0.0.1:8011/" />
          <button onclick="saveImpactUrl()">Save</button>
          <button onclick="clearImpactUrl()">Clear</button>
          <a id="impact_open" href="#" target="_blank">Open</a>
          <span id="impact_status" class="pill">-</span>
        </div>
        <iframe id="impact_iframe" src="about:blank"></iframe>
      </div>
    </div>

    <div class="row">
      <a href="/docs" target="_blank">API Docs</a>
      <a href="/status" target="_blank">/status</a>
      <a href="/health" target="_blank">/health</a>
      <a href="/overview_chart" target="_blank">/overview_chart</a>
    </div>
  </div>

  <script>
    function getToken(){ return localStorage.getItem('control_token') || ''; }
    function saveToken(){ const t = document.getElementById('token').value || ''; if (t) localStorage.setItem('control_token', t); else localStorage.removeItem('control_token'); }
    function clearToken(){ localStorage.removeItem('control_token'); document.getElementById('token').value = ''; }
    function getImpactUrl(){ return localStorage.getItem('impact_url') || localStorage.getItem('impact_console_url') || ''; }
    function saveImpactUrl(){
      const t = document.getElementById('impact_url').value || '';
      if (t) { localStorage.setItem('impact_url', t); localStorage.setItem('impact_console_url', t); }
      else { localStorage.removeItem('impact_url'); localStorage.removeItem('impact_console_url'); }
    }
    function clearImpactUrl(){ localStorage.removeItem('impact_url'); localStorage.removeItem('impact_console_url'); document.getElementById('impact_url').value = ''; }
    function fmt(v, digits=2){ if (v===undefined || v===null) return '-'; const n = Number(v); if (!Number.isFinite(n)) return String(v); return n.toFixed(digits); }
    function escapeHtml(s){ return String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); }
    function parseIsoMs(s){ if (!s) return null; const t = Date.parse(String(s)); return Number.isFinite(t) ? t : null; }

    function currentTab() {
      const h = (location.hash || '').replace('#','').trim().toLowerCase();
      if (!h || h === 'control_journal' || h === 'core' || h === 'exec' || h === 'md') return 'overview';
      if (h === 'impact_console') return 'impact';
      return h === 'impact' ? 'impact' : 'overview';
    }
    function setTab(tab){ location.hash = tab; renderTabs(); }
    function renderTabs(){
      const tab = currentTab();
      for (const btn of document.querySelectorAll('button.tab')) {
        const t = btn.getAttribute('data-tab');
        if (t === tab) btn.classList.add('active'); else btn.classList.remove('active');
      }
      for (const pane of document.querySelectorAll('.tabpane')) pane.classList.add('hidden');
      const pane = document.getElementById('tab_' + tab);
      if (pane) pane.classList.remove('hidden');
    }

    async function cmd(path, btn) {
      const headers = {};
      const tok = getToken();
      if (tok) headers['X-Control-Token'] = tok;
      try {
        if (btn && btn.classList) {
          for (const b of document.querySelectorAll('button[data-cmd="1"]')) { try { b.classList.remove('active'); } catch (e) {} }
          btn.classList.add('active');
        }
      } catch (e) {}
      try {
        const r = await fetch(path, { method:'POST', headers });
        if (!r.ok) console.error('cmd failed', path, r.status, await r.text());
      } catch (e) { console.error(e); }
    }

    async function applyBudget() {
      const out = document.getElementById('budget_result');
      try {
        const mode = String(window.__lastMode || '').toLowerCase();
        const starting_cash_eur = Number(document.getElementById('budget_start').value);
        const max_exposure_eur = Number(document.getElementById('budget_exposure')?.value);
        let body = null;
        if (mode === 'sim') {
          if (!(starting_cash_eur > 0)) { if (out) out.textContent = 'ERROR: positive amount required'; return; }
          body = { amount_eur: starting_cash_eur, reset: true };
        } else {
          if (!(starting_cash_eur > 0) || !(max_exposure_eur > 0)) { if (out) out.textContent = 'ERROR: positive numbers required'; return; }
          body = { starting_cash_eur, max_exposure_eur, reset: true };
        }
        const headers = { 'Content-Type': 'application/json' };
        const tok = getToken();
        if (tok) headers['X-Control-Token'] = tok;
        if (out) out.textContent = 'sending...';
        const r = await fetch('/set_budget', { method:'POST', headers, body: JSON.stringify(body) });
        if (!r.ok) { if (out) out.textContent = `ERROR ${r.status}`; return; }
        if (out) out.textContent = 'ok';
      } catch (e) {
        console.error(e);
        if (out) out.textContent = 'ERROR';
      }
    }

    function drawPriceChart(chart) {
      const canvas = document.getElementById('price_chart');
      const meta = document.getElementById('price_chart_meta');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0,0,w,h);
      ctx.fillStyle = '#0b1220';
      ctx.fillRect(0,0,w,h);

      const rawPoints = (chart && chart.points) ? chart.points : [];
      const rawFills = (chart && chart.fills) ? chart.fills : [];
      const breakLines = (chart && chart.break_even_lines) ? chart.break_even_lines : [];
      const points = rawPoints.filter(p => Number.isFinite(Number(p.price)));
      const fills = rawFills.filter(f => Number.isFinite(Number(f.price)));
      const prices = points.map(p => Number(p.price));
      for (const f of fills) prices.push(Number(f.price));
      for (const b of breakLines) prices.push(Number(b.price));

      const pad = 18;
      const x0 = pad, x1 = w - pad, y0 = pad, y1 = h - pad;
      ctx.strokeStyle = '#1f2937';
      ctx.lineWidth = 1;
      for (let i=0;i<=4;i++){
        const y = y0 + ((y1 - y0) * i / 4);
        ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
      }

      if (!points.length || !prices.length) {
        if (meta) meta.textContent = 'no market points yet';
        return;
      }

      let minP = Math.min(...prices);
      let maxP = Math.max(...prices);
      if (!(maxP > minP)) { maxP = minP + 1.0; }
      const padY = (maxP - minP) * 0.06;
      minP -= padY;
      maxP += padY;

      const n = points.length;
      const xScale = (i) => x0 + (n <= 1 ? 0 : (i / (n - 1)) * (x1 - x0));
      const yScale = (v) => y1 - ((v - minP) / (maxP - minP)) * (y1 - y0);
      const pointTs = points.map(p => parseIsoMs(p.ts));
      const series = points.map(p => Number(p.price));

      for (const line of breakLines) {
        const bp = Number(line.price);
        if (!Number.isFinite(bp)) continue;
        const y = yScale(bp);
        ctx.setLineDash([6,4]);
        ctx.strokeStyle = String(line.label||'') === 'avg_entry' ? '#a3a3a3' : '#f59e0b';
        ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
        ctx.setLineDash([]);
      }

      ctx.strokeStyle = '#60a5fa';
      ctx.lineWidth = 2;
      ctx.beginPath();
      for (let i=0;i<n;i++){
        const x = xScale(i);
        const y = yScale(Number(series[i]));
        if (i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
      }
      ctx.stroke();

      function nearestPointIndex(tsMs) {
        if (!Number.isFinite(tsMs)) return null;
        let bestIdx = null;
        let bestDiff = Infinity;
        for (let i=0;i<pointTs.length;i++) {
          const pms = pointTs[i];
          if (!Number.isFinite(pms)) continue;
          const d = Math.abs(pms - tsMs);
          if (d < bestDiff) { bestDiff = d; bestIdx = i; }
        }
        if (bestIdx === null) return null;
        if (!(bestDiff <= 15 * 60 * 1000)) return null; // only mark nearby points
        return bestIdx;
      }

      for (const f of fills) {
        const idx = nearestPointIndex(parseIsoMs(f.ts));
        if (idx === null) continue;
        const x = xScale(idx);
        const y = yScale(Number(f.price));
        const side = String(f.side || '').toLowerCase();
        ctx.fillStyle = side === 'buy' ? '#22c55e' : '#ef4444';
        ctx.strokeStyle = '#0b1220';
        ctx.lineWidth = 1.0;
        ctx.beginPath();
        ctx.arc(x, y, 3.8, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }

      const firstTs = String(points[0].ts || '-');
      const lastTs = String(points[points.length - 1].ts || '-');
      if (meta) meta.textContent = `points=${points.length} fills=${fills.length} price=${fmt(points[points.length-1].price,2)} range=[${firstTs} .. ${lastTs}]`;
    }

    function setImpactReachability(st) {
      const el = document.getElementById('impact_status');
      if (!el) return;
      el.classList.remove('ok');
      el.classList.remove('bad');
      if (!st) { el.textContent = '-'; return; }
      if (st.ok === true) { el.textContent = 'reachable'; el.classList.add('ok'); return; }
      if (st.ok === false) { el.textContent = 'down'; el.classList.add('bad'); return; }
      el.textContent = 'probe skipped';
    }

    function renderLights(s) {
      const host = document.getElementById('lights');
      if (!host) return;
      const checks = (s && s.overview_checks) ? s.overview_checks : {};
      const labels = [
        ['trading_enabled', 'Trading Enabled'],
        ['core_fresh', 'Core Fresh'],
        ['exec_fresh', 'Exec Fresh'],
        ['market_fresh', 'Market Fresh'],
        ['balances_fresh', 'Balances Fresh'],
        ['rate_limit_clear', 'Rate Limit Clear'],
        ['deadman_armed', 'Deadman Armed'],
        ['journal_healthy', 'Journal Healthy'],
      ];
      host.innerHTML = '';
      for (const [key, label] of labels) {
        const c = checks[key] || {};
        const ok = c.ok === true;
        const detail = c.detail === undefined ? '' : String(c.detail);
        const div = document.createElement('div');
        div.className = 'light';
        div.innerHTML = `<div class="top"><span class="dot ${ok ? 'ok':'bad'}"></span><strong>${escapeHtml(label)}</strong></div><div class="small mono">${escapeHtml(detail)}</div>`;
        host.appendChild(div);
      }
    }

    let lastStatus = null;
    async function refresh() {
      try {
        renderTabs();
        const tab = currentTab();
        document.getElementById('whereami').textContent = window.location.origin;
        const s = await (await fetch('/status')).json();
        lastStatus = s;

        const core = (s.data||{}).core||{};
        const exec = (s.data||{}).exec||{};
        const stale = s.staleness_sec_by_process || {};

        const mode = String(core.mode ?? exec.mode ?? '-');
        window.__lastMode = mode;
        document.getElementById('mode').textContent = mode;
        document.getElementById('enabled').textContent = String(core.trading_enabled);
        document.getElementById('disable_reason').textContent = String(core.trading_disable_reason || '-');
        document.getElementById('age').textContent = fmt(core.last_market_event_age);
        document.getElementById('stale_core').textContent = fmt(stale.core);
        document.getElementById('stale_exec').textContent = fmt(stale.exec);
        document.getElementById('open_orders').textContent = fmt(exec.open_orders_count, 0);
        document.getElementById('rate_limited').textContent = String(Boolean(exec.rate_limited));
        document.getElementById('deadman').textContent = String(((s.deadman||{}).last_event) || '-');
        document.getElementById('equity').textContent = fmt(core.equity_eur);
        document.getElementById('pos_btc').textContent = fmt(core.position_btc, 6);
        document.getElementById('avg_entry').textContent = fmt(core.avg_entry_price);
        document.getElementById('mark_price').textContent = fmt(core.mark_price);
        const balAge = (((s.overview_checks||{}).balances_fresh||{}).value);
        document.getElementById('balances_age').textContent = fmt(balAge);

        const ready = s.overview_trade_ready === true;
        const readyEl = document.getElementById('trade_ready');
        readyEl.textContent = ready ? 'GREEN' : 'RED';
        readyEl.classList.remove('ok'); readyEl.classList.remove('bad');
        readyEl.classList.add(ready ? 'ok' : 'bad');
        document.getElementById('trade_ready_detail').textContent = ready ? 'all critical checks green' : 'one or more critical checks red';
        renderLights(s);

        const staleWarn = Number((s.overview_thresholds||{}).stale_warn_sec);
        const sc = Number(stale.core), se = Number(stale.exec);
        const staleNow = (Number.isFinite(sc) && sc > staleWarn) || (Number.isFinite(se) && se > staleWarn);
        const banner = document.getElementById('banner');
        const warns = (s.external_guis_warnings || []);
        if (staleNow || (warns && warns.length)) {
          banner.style.display = 'block';
          const parts = [];
          if (staleNow) parts.push(`telemetry stale (core=${fmt(stale.core)}s exec=${fmt(stale.exec)}s)`);
          if (warns && warns.length) parts.push(warns.join(' | '));
          banner.textContent = parts.join(' | ');
        } else {
          banner.style.display = 'none';
          banner.textContent = '';
        }

        const statusUrl = ((((s||{}).external_guis||{}).impact) || '').trim();
        const localUrl = (getImpactUrl() || '').trim();
        const impactUrl = (localUrl || statusUrl || 'http://127.0.0.1:8011/').trim();
        const impactInput = document.getElementById('impact_url');
        if (impactInput && !impactInput.value) impactInput.value = localUrl || statusUrl || '';
        const impactOpen = document.getElementById('impact_open');
        if (impactOpen) impactOpen.href = impactUrl || '#';
        setImpactReachability((((s||{}).external_guis_status||{}).impact) || null);
        const iframe = document.getElementById('impact_iframe');
        if (iframe && tab === 'impact' && iframe.src !== impactUrl) iframe.src = impactUrl;

        try {
          const bsl = document.getElementById('budget_start_label');
          const bel = document.getElementById('budget_exposure_label');
          const bs = document.getElementById('budget_start');
          const be = document.getElementById('budget_exposure');
          if (String(mode).toLowerCase() === 'sim') {
            if (bsl) bsl.textContent = 'Amount EUR';
            if (bel) bel.style.display = 'none';
            if (be) be.style.display = 'none';
            if (bs && !bs.value && core.max_exposure_eur !== undefined && core.max_exposure_eur !== null) bs.value = String(core.max_exposure_eur);
          } else {
            if (bsl) bsl.textContent = 'Start EUR';
            if (bel) bel.style.display = '';
            if (be) be.style.display = '';
            if (bs && !bs.value && core.starting_cash_eur !== undefined && core.starting_cash_eur !== null) bs.value = String(core.starting_cash_eur);
            if (be && !be.value && core.max_exposure_eur !== undefined && core.max_exposure_eur !== null) be.value = String(core.max_exposure_eur);
          }
        } catch (e) {}

        if (tab === 'overview') {
          const chart = await (await fetch('/overview_chart?lines=8000&max_points=2400&max_fills=240&window_sec=86400')).json();
          drawPriceChart(chart);
        }
      } catch (e) {
        console.error(e);
      }
    }

    document.getElementById('token').value = getToken();
    try { document.getElementById('impact_url').value = getImpactUrl(); } catch (e) {}
    window.addEventListener('hashchange', renderTabs);
    setInterval(refresh, 10000);
    refresh();
  </script>
</body>
</html>"""
