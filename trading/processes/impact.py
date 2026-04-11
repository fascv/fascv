from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from trading.ipc.events import Heartbeat, JournalEvent, NewsEvent, TelemetryEvent
from trading.ipc.queues import put_latest, queue_depth, try_put
from trading.processes.context import ProcessContext


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
    try_put(ctx.q_journal, evt)


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="impact", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def _send_telemetry(ctx: ProcessContext, data: Dict[str, Any]) -> None:
    evt = TelemetryEvent(ts=datetime.now(timezone.utc), process="impact", data=data)
    try_put(ctx.q_telemetry, evt)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _fetch_json(url: str, timeout_seconds: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read().decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("impact api response must be a JSON object")
        return payload


def _load_json_file(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("impact file payload must be a JSON object")
    return payload


def _pick_multi_asset_horizon(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    horizons = payload.get("horizons")
    if not isinstance(horizons, list):
        return None

    best: Optional[Dict[str, Any]] = None
    best_minutes = 10**9
    for row in horizons:
        if not isinstance(row, dict):
            continue
        if str(row.get("status", "")).strip().lower() != "ok":
            continue
        minutes = _as_int(row.get("horizon_minutes"), 0)
        if minutes <= 0:
            minutes = 10**8
        if best is None or minutes < best_minutes:
            best = row
            best_minutes = minutes

    if best is not None:
        return best

    # Fallback: first dict row, even if status is not "ok".
    for row in horizons:
        if isinstance(row, dict):
            return row
    return None


def _to_news_event(payload: Dict[str, Any], symbol_default: str, preferred_group: str = "") -> NewsEvent:
    # New impact format: /signal/multi-asset-probability
    multi_h = _pick_multi_asset_horizon(payload)
    if multi_h is not None:
        selected_payload = multi_h
        selected_scope = "overall"
        group_key = str(preferred_group or "").strip().lower()
        if group_key:
            group_probabilities = multi_h.get("group_probabilities")
            if isinstance(group_probabilities, dict):
                group_row = group_probabilities.get(group_key)
                if isinstance(group_row, dict) and str(group_row.get("status", "")).strip().lower() == "ok":
                    selected_payload = group_row
                    selected_scope = group_key

        prob_up = _clamp(_as_float(selected_payload.get("prob_up", 0.5), 0.5), 0.0, 1.0)
        prob_down = _clamp(_as_float(selected_payload.get("prob_down", 1.0 - prob_up), 1.0 - prob_up), 0.0, 1.0)
        confidence = _clamp(_as_float(selected_payload.get("confidence", 0.0), 0.0), 0.0, 1.0)
        sentiment = _clamp((2.0 * prob_up) - 1.0, -1.0, 1.0)
        impact = confidence
        horizon_minutes = max(0, _as_int(multi_h.get("horizon_minutes"), 0))
        score_now = _as_float(selected_payload.get("score_now", multi_h.get("score_now", 0.0)), 0.0)
        source_count = max(
            0,
            _as_int(
                selected_payload.get(
                    "active_sources_now",
                    selected_payload.get(
                        "relations",
                        multi_h.get("active_sources_now", multi_h.get("relations", payload.get("source_assets_considered", 0))),
                    ),
                ),
                0,
            ),
        )
        target_symbol = str(payload.get("target_symbol", symbol_default))
        event_id = f"multi_asset_prob:{target_symbol}:{horizon_minutes}m"
        signal_state = "neutral"
        if prob_up >= 0.55:
            signal_state = "risk_on"
        elif prob_up <= 0.45:
            signal_state = "risk_off"

        return NewsEvent(
            ts=datetime.now(timezone.utc),
            symbol=target_symbol,
            sentiment_score=sentiment,
            impact_score=impact,
            source_count=source_count,
            event_id=event_id,
            meta={
                "signal_format": "multi_asset_probability",
                "signal_scope": selected_scope,
                "preferred_group": group_key or None,
                "signal_state": signal_state,
                "target_market_symbol": payload.get("target_market_symbol"),
                "horizon_minutes": horizon_minutes,
                "prob_up": prob_up,
                "prob_down": prob_down,
                "confidence": confidence,
                "score_now": score_now,
                "expected_return_bps": _as_float(selected_payload.get("expected_return_bps", multi_h.get("expected_return_bps", 0.0)), 0.0),
                "relations": _as_int(selected_payload.get("relations", multi_h.get("relations")), 0),
                "active_sources_now": _as_int(selected_payload.get("active_sources_now", multi_h.get("active_sources_now")), 0),
                "samples": _as_int(selected_payload.get("samples", multi_h.get("samples")), 0),
                "provider": payload.get("provider"),
                "lookback": payload.get("lookback"),
                "raw_confidence": confidence,
                "raw_score": score_now,
            },
        )

    # Legacy impact format: /signal/trading
    score = _as_float(payload.get("final_score", payload.get("score", 0.0)), 0.0)
    confidence = _as_float(payload.get("news_driven_probability", payload.get("confidence", 0.0)), 0.0)

    sentiment = _clamp(score, -1.0, 1.0)
    impact = _clamp(max(abs(sentiment), abs(confidence)), 0.0, 1.0)

    source_count = int(
        payload.get(
            "contributing_items",
            len(payload.get("reasons", [])) if isinstance(payload.get("reasons"), list) else 1,
        )
    )
    source_count = max(0, source_count)

    raw_event = payload.get("attribution_event")
    event_id: Optional[str]
    if isinstance(raw_event, dict):
        event_id = str(raw_event.get("id", raw_event.get("event_id", ""))) or None
    elif raw_event is None:
        event_id = None
    else:
        event_id = str(raw_event)

    return NewsEvent(
        ts=datetime.now(timezone.utc),
        symbol=str(payload.get("symbol", symbol_default)),
        sentiment_score=sentiment,
        impact_score=impact,
        source_count=source_count,
        event_id=event_id,
        meta={
            "signal_state": payload.get("signal_state"),
            "attribution_state": payload.get("attribution_state"),
            "mode": payload.get("mode"),
            "mode_effective": payload.get("mode_effective"),
            "raw_score": score,
            "raw_confidence": confidence,
        },
    )


def run_impact(ctx: ProcessContext) -> None:
    cfg = ctx.config
    enabled = bool(_cfg(cfg, "impact.enabled", False))
    if not enabled:
        return

    symbol = str(_cfg(cfg, "impact.symbol", _cfg(cfg, "md.pair", "BTC/EUR")))
    source = str(_cfg(cfg, "impact.source", "api")).lower()
    api_url = str(_cfg(cfg, "impact.api_url", "http://127.0.0.1:8011/arrow?window=1h&mode=auto"))
    file_path = str(_cfg(cfg, "impact.file_path", "modules/impact_console/diagnostics/arrow_1h.json"))
    preferred_group = str(_cfg(cfg, "impact.preferred_group", "") or "").strip().lower()
    timeout_seconds = float(_cfg(cfg, "impact.timeout_seconds", 3.0))
    interval_seconds = float(_cfg(cfg, "impact.interval_seconds", 15.0))
    fallback_to_file = bool(_cfg(cfg, "impact.fallback_to_file", True))

    hb_seq = 0
    last_heartbeat = 0.0
    heartbeat_interval = float(_cfg(cfg, "impact.heartbeat_interval", 2.0))

    _send_journal(ctx, "impact_start", {"source": source, "symbol": symbol, "api_url": api_url, "file_path": file_path})

    while not ctx.stop_event.is_set():
        payload: Optional[Dict[str, Any]] = None
        source_used = source
        error_msg: Optional[str] = None

        try:
            if source == "file":
                payload = _load_json_file(file_path)
            else:
                payload = _fetch_json(api_url, timeout_seconds)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, FileNotFoundError) as exc:
            error_msg = str(exc)
            if source != "file" and fallback_to_file:
                try:
                    payload = _load_json_file(file_path)
                    source_used = "file_fallback"
                except Exception as file_exc:
                    error_msg = f"{error_msg}; file_fallback={file_exc}"

        if payload is None:
            _send_journal(ctx, "impact_error", {"source": source_used, "error": error_msg or "unknown"})
            time.sleep(max(0.1, interval_seconds))
            continue

        event = _to_news_event(payload, symbol, preferred_group=preferred_group)
        if ctx.q_impact_core is not None:
            put_latest(ctx.q_impact_core, event)

        _send_journal(
            ctx,
            "impact_event",
            {
                "source": source_used,
                "ts": event.ts.isoformat(),
                "symbol": event.symbol,
                "sentiment_score": event.sentiment_score,
                "impact_score": event.impact_score,
                "source_count": event.source_count,
                "event_id": event.event_id,
                "meta": event.meta,
            },
        )

        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            _send_telemetry(
                ctx,
                {
                    "mode": ctx.mode,
                    "impact_source": source_used,
                    "impact_queue_depth": queue_depth(ctx.q_impact_core) if ctx.q_impact_core is not None else 0,
                    "last_impact_ts": event.ts.isoformat(),
                },
            )
            last_heartbeat = now

        time.sleep(max(0.1, interval_seconds))

    _send_journal(ctx, "impact_stop", {})
