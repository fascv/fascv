from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from trading.types import MarketEvent
from trading.utils.binance import to_binance_symbol
from trading.utils.time import parse_ts, to_utc


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _to_dt(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return to_utc(raw)
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return to_utc(parse_ts(text))
    except Exception:
        return None


def _to_f(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except Exception:
        return None


def _alpha_warmup_bars(cfg: Dict[str, Any]) -> int:
    alpha_type = str(_cfg(cfg, "alpha.type", "momentum") or "momentum").strip().lower()
    if alpha_type in {"momentum", "trend"}:
        return max(2, int(_cfg(cfg, "alpha.lookback", 3)) + 1)
    if alpha_type in {"mean_reversion", "reversion"}:
        return max(2, int(_cfg(cfg, "alpha.mean_reversion.lookback", 6)) + 1)
    if alpha_type == "breakout":
        return max(2, int(_cfg(cfg, "alpha.breakout.lookback", 12)) + 1)
    if alpha_type == "swing":
        lb = int(_cfg(cfg, "alpha.swing.lookback", 20))
        mlb = int(_cfg(cfg, "alpha.swing.momentum_lookback", 7))
        return max(2, lb + 1, mlb + 2)
    if alpha_type == "auto":
        trend_lb = int(_cfg(cfg, "alpha.auto.trend.lookback", 3)) + 1
        mean_lb = int(_cfg(cfg, "alpha.auto.mean_reversion.lookback", 6)) + 1
        bo_lb = int(_cfg(cfg, "alpha.auto.breakout.lookback", 12)) + 1
        sw_lb = max(
            int(_cfg(cfg, "alpha.auto.swing.lookback", 20)) + 1,
            int(_cfg(cfg, "alpha.auto.swing.momentum_lookback", 7)) + 2,
        )
        regime_lb = int(_cfg(cfg, "alpha.auto.regime.lookback", 6)) + 1
        return max(2, trend_lb, mean_lb, bo_lb, sw_lb, regime_lb)
    return 2


def estimate_warmup_bars(cfg: Dict[str, Any]) -> int:
    rw = int(_cfg(cfg, "features.return_window", 1))
    aw = int(_cfg(cfg, "features.atr_window", 14))
    vw = int(_cfg(cfg, "features.volume_z_window", 30))
    tw = int(_cfg(cfg, "features.trend_window", 0))
    cw = int(_cfg(cfg, "features.context_window", 0))
    feature_need = max(2, rw + 1, aw, vw, tw + 1 if tw > 0 else 2, cw + 1 if cw > 0 else 2)
    alpha_need = _alpha_warmup_bars(cfg)
    need = max(feature_need, alpha_need)

    window_hours = float(_cfg(cfg, "core.warmup.window_hours", 0.0))
    if window_hours > 0.0:
        interval_sec = max(1, int(_cfg(cfg, "md.interval_seconds", 300)))
        # For very small custom bars (e.g. 30s), binance rest fallback can only preload at >=1m.
        effective_sec = max(60, interval_sec)
        need = max(need, int(math.ceil(window_hours * 3600.0 / float(effective_sec))))

    need = max(need, int(_cfg(cfg, "core.warmup.min_bars", 0)))
    max_bars = max(1, int(_cfg(cfg, "core.warmup.max_bars", 20000)))
    return max(0, min(max_bars, need))


def _warmup_journal_stale_max_sec(cfg: Dict[str, Any]) -> float:
    raw = _cfg(cfg, "core.warmup.journal_stale_max_sec", None)
    if raw is None:
        interval_sec = max(1, int(_cfg(cfg, "md.interval_seconds", 300)))
        # Default: if journal market data is older than ~15 bars (but at least 5min),
        # treat it as stale and rebuild from fresh exchange history.
        return float(max(300, interval_sec * 15))
    try:
        value = float(raw)
    except Exception:
        interval_sec = max(1, int(_cfg(cfg, "md.interval_seconds", 300)))
        return float(max(300, interval_sec * 15))
    return max(0.0, value)


def _event_from_payload(payload: Dict[str, Any], default_micro: Dict[str, float], ts_fallback: Any = None) -> Optional[MarketEvent]:
    ts = _to_dt(payload.get("ts")) or _to_dt(ts_fallback)
    if ts is None:
        return None
    o = _to_f(payload.get("open"))
    h = _to_f(payload.get("high"))
    l = _to_f(payload.get("low"))
    c = _to_f(payload.get("close"))
    v = _to_f(payload.get("volume"))
    if None in {o, h, l, c, v}:
        return None
    micro = dict(default_micro)
    maybe_micro = payload.get("micro")
    if isinstance(maybe_micro, dict):
        for k, val in maybe_micro.items():
            try:
                micro[str(k)] = float(val)
            except Exception:
                continue
    return MarketEvent(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v), micro=micro)


def _dedupe_and_trim(events: List[MarketEvent], limit: int) -> List[MarketEvent]:
    by_ts: Dict[str, MarketEvent] = {}
    for ev in events:
        by_ts[ev.ts.isoformat()] = ev
    out = sorted(by_ts.values(), key=lambda e: e.ts)
    if limit > 0 and len(out) > limit:
        out = out[-limit:]
    return out


def _load_from_journal_db(db_path: str, limit: int, default_micro: Dict[str, float]) -> List[MarketEvent]:
    if limit <= 0 or not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT ts, payload_json FROM events WHERE event_type = ? ORDER BY id DESC LIMIT ?",
            ("market", int(limit)),
        )
        rows = cur.fetchall()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out: List[MarketEvent] = []
    for ts_raw, payload_json in reversed(rows):
        try:
            payload = json.loads(str(payload_json))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        ev = _event_from_payload(payload, default_micro, ts_fallback=ts_raw)
        if ev is not None:
            out.append(ev)
    return out


def _load_from_journal_json(json_path: str, limit: int, default_micro: Dict[str, float]) -> List[MarketEvent]:
    if limit <= 0 or not json_path or not os.path.exists(json_path):
        return []
    tail: deque[MarketEvent] = deque(maxlen=max(1, int(limit)))
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("event_type", "")).strip().lower() != "market":
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                ev = _event_from_payload(payload, default_micro, ts_fallback=rec.get("ts"))
                if ev is not None:
                    tail.append(ev)
    except Exception:
        return []
    return list(tail)


def _binance_interval_for_seconds(seconds: int) -> Tuple[str, int]:
    supported: List[Tuple[str, int]] = [
        ("1m", 60),
        ("3m", 180),
        ("5m", 300),
        ("15m", 900),
        ("30m", 1800),
        ("1h", 3600),
        ("2h", 7200),
        ("4h", 14400),
        ("6h", 21600),
        ("8h", 28800),
        ("12h", 43200),
        ("1d", 86400),
    ]
    target = max(60, int(seconds))
    best = supported[0]
    for item in supported:
        if item[1] <= target:
            best = item
        else:
            break
    return best


def _load_from_binance_rest(
    cfg: Dict[str, Any],
    need: int,
    default_micro: Dict[str, float],
    end_before: Optional[datetime] = None,
) -> List[MarketEvent]:
    if need <= 0:
        return []
    pair = str(_cfg(cfg, "md.pair", _cfg(cfg, "live.symbol", "ETH/USDT")) or "ETH/USDT")
    symbol = to_binance_symbol(pair)
    base_url = str(_cfg(cfg, "core.warmup.binance_base_url", _cfg(cfg, "live.rest_url", "https://api.binance.com")))
    interval_seconds = int(_cfg(cfg, "md.interval_seconds", 300))
    interval_str, _ = _binance_interval_for_seconds(interval_seconds)
    timeout_sec = float(_cfg(cfg, "core.warmup.timeout_sec", 8.0))
    url = base_url.rstrip("/") + "/api/v3/klines"

    out: List[MarketEvent] = []
    end_ms: Optional[int] = None
    if end_before is not None:
        try:
            end_ms = int(to_utc(end_before).timestamp() * 1000.0) - 1
        except Exception:
            end_ms = None
    loops = 0
    max_loops = max(1, int(math.ceil(float(need) / 1000.0)) + 4)
    while len(out) < need and loops < max_loops:
        loops += 1
        limit = max(1, min(1000, need - len(out)))
        params: Dict[str, Any] = {"symbol": symbol, "interval": interval_str, "limit": limit}
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        try:
            resp = requests.get(url, params=params, timeout=timeout_sec)
            resp.raise_for_status()
            doc = resp.json()
        except Exception:
            break
        if not isinstance(doc, list) or not doc:
            break

        batch: List[MarketEvent] = []
        for item in doc:
            if not isinstance(item, list) or len(item) < 6:
                continue
            try:
                ts = datetime.fromtimestamp(int(item[0]) / 1000.0, tz=timezone.utc)
                o = float(item[1])
                h = float(item[2])
                l = float(item[3])
                c = float(item[4])
                v = float(item[5])
            except Exception:
                continue
            batch.append(MarketEvent(ts=ts, open=o, high=h, low=l, close=c, volume=v, micro=dict(default_micro)))
        if not batch:
            break

        out = batch + out
        try:
            first_open_ms = int(doc[0][0])
        except Exception:
            break
        end_ms = first_open_ms - 1
        if len(doc) < limit:
            break
        time.sleep(0.05)
    return out


def warmup_feature_and_alpha(cfg: Dict[str, Any], feature_engine: Any, alpha_model: Any) -> Dict[str, Any]:
    needed = estimate_warmup_bars(cfg)
    if needed <= 0:
        return {"enabled": False, "required_bars": 0, "hydrated_bars": 0}

    default_micro = _cfg(cfg, "data.default_micro", {}) or {}
    default_micro = dict(default_micro) if isinstance(default_micro, dict) else {}

    collected: List[MarketEvent] = []
    source_counts: Dict[str, int] = {}

    use_journal_db = bool(_cfg(cfg, "core.warmup.use_journal_db", True))
    if use_journal_db:
        db_path = str(_cfg(cfg, "core.warmup.journal_db_path", _cfg(cfg, "journal.db_path", "")))
        from_db = _load_from_journal_db(db_path, needed, default_micro)
        source_counts["journal_db"] = len(from_db)
        collected.extend(from_db)
        collected = _dedupe_and_trim(collected, needed)

    use_journal_json = bool(_cfg(cfg, "core.warmup.use_journal_json", True))
    if use_journal_json and len(collected) < needed:
        json_path = str(_cfg(cfg, "core.warmup.journal_json_path", _cfg(cfg, "journal.json_path", "")))
        from_json = _load_from_journal_json(json_path, needed, default_micro)
        source_counts["journal_json"] = len(from_json)
        collected.extend(from_json)
        collected = _dedupe_and_trim(collected, needed)

    exchange = str(_cfg(cfg, "md.exchange", _cfg(cfg, "live.exchange", "kraken"))).strip().lower()
    rest_backfill = bool(_cfg(cfg, "core.warmup.rest_backfill", True))
    journal_stale_max_sec = _warmup_journal_stale_max_sec(cfg)
    journal_latest_age_sec: Optional[float] = None
    journal_stale = False
    if collected:
        latest_ts = collected[-1].ts
        journal_latest_age_sec = max(0.0, (datetime.now(timezone.utc) - latest_ts).total_seconds())
        if journal_stale_max_sec > 0.0 and journal_latest_age_sec > journal_stale_max_sec:
            journal_stale = True

    stale_journal_replaced = False
    if exchange == "binance" and rest_backfill:
        if journal_stale:
            from_rest = _load_from_binance_rest(cfg, needed, default_micro, end_before=None)
            source_counts["binance_rest"] = len(from_rest)
            if from_rest:
                stale_journal_replaced = True
                source_counts["stale_journal_replaced_bars"] = len(collected)
                collected = _dedupe_and_trim(from_rest, needed)
        elif len(collected) < needed:
            missing = needed - len(collected)
            oldest_ts = collected[0].ts if collected else None
            from_rest = _load_from_binance_rest(cfg, missing, default_micro, end_before=oldest_ts)
            source_counts["binance_rest"] = len(from_rest)
            collected.extend(from_rest)
            collected = _dedupe_and_trim(collected, needed)

    hydrated = 0
    for ev in collected:
        try:
            features = feature_engine.compute(ev)
            alpha_model.predict(features)
            hydrated += 1
        except Exception:
            continue

    return {
        "enabled": True,
        "required_bars": int(needed),
        "hydrated_bars": int(hydrated),
        "source_counts": source_counts,
        "journal_stale_replaced": bool(stale_journal_replaced),
        "journal_stale_max_sec": float(journal_stale_max_sec),
        "journal_latest_age_sec": journal_latest_age_sec,
        "start_ts": collected[0].ts.isoformat() if collected else None,
        "end_ts": collected[-1].ts.isoformat() if collected else None,
        "_close_prices": [float(ev.close) for ev in collected],
    }
