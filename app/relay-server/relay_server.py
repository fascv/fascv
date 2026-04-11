from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import yaml

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://api.binance.com").strip().rstrip("/")
RELAY_TOKEN = os.getenv("RELAY_TOKEN", "").strip()
CORS_ALLOW_ORIGIN = os.getenv("RELAY_CORS_ALLOW_ORIGIN", "*").strip() or "*"

HOST = os.getenv("RELAY_HOST", "0.0.0.0")
PORT = int(os.getenv("RELAY_PORT", "8000"))
REQUEST_TIMEOUT = float(os.getenv("RELAY_TIMEOUT_SEC", "25"))
MAX_WORKERS = max(1, min(int(os.getenv("RELAY_WORKERS", "8")), 24))
REPORT_CACHE_TTL_SEC = float(os.getenv("REPORT_CACHE_TTL_SEC", "900"))
MY_TRADES_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("MY_TRADES_MIN_INTERVAL_SEC", "0.35")))
MY_TRADES_MAX_RETRIES = max(1, int(os.getenv("MY_TRADES_MAX_RETRIES", "3")))
MY_TRADES_RETRY_BASE_SEC = max(0.1, float(os.getenv("MY_TRADES_RETRY_BASE_SEC", "2.0")))
BINANCE_REPORT_MIN_INTERVAL_SEC = max(0.0, float(os.getenv("BINANCE_REPORT_MIN_INTERVAL_SEC", "45")))
BINANCE_REPORT_MAX_SYMBOLS = max(1, int(os.getenv("BINANCE_REPORT_MAX_SYMBOLS", "4")))
BINANCE_REPORT_MAX_WINDOW_HOURS = max(1.0, float(os.getenv("BINANCE_REPORT_MAX_WINDOW_HOURS", "24")))
BINANCE_REPORT_MAX_WORKERS = max(1, min(int(os.getenv("BINANCE_REPORT_MAX_WORKERS", "1")), 4))
BINANCE_REPORT_REQUIRE_SYMBOLS = str(os.getenv("BINANCE_REPORT_REQUIRE_SYMBOLS", "1")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BINANCE_REPORT_AUTO_FALLBACK_LOCAL = str(
    os.getenv("BINANCE_REPORT_AUTO_FALLBACK_LOCAL", "1")
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from trading.binance import trade_mirror as binance_trade_mirror

LOGS_DIR = os.path.join(REPO_ROOT, "logs")
DEFAULT_ROTATION_ACTIVE_FILE = os.path.abspath(
    os.path.join(BASE_DIR, "..", "..", "bitcoin", "configs", "rotation_active_lanes.json")
)
ROTATION_ACTIVE_FILE = os.getenv("ROTATION_ACTIVE_FILE", DEFAULT_ROTATION_ACTIVE_FILE).strip()
DEFAULT_ROTATION_META_SHADOW_REPORT_FILE = os.path.join(
    REPO_ROOT, "logs", "rotation_meta_shadow_report.json"
)
ROTATION_META_SHADOW_REPORT_FILE = os.getenv(
    "ROTATION_META_SHADOW_REPORT_FILE", DEFAULT_ROTATION_META_SHADOW_REPORT_FILE
).strip()
ROTATION_RUNTIME_CONFIG_DIR = os.path.join(REPO_ROOT, "logs", "rotation_runtime_configs")
CORE_ROTATION_STRATEGIES = {"staircase", "continuation", "breakout", "rebound"}

_SYMBOL_CACHE: list[str] = []
_SYMBOL_CACHE_AT: float = 0.0
_SYMBOL_CACHE_TTL_SEC = 300.0
_REPORT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MY_TRADES_LOCK = threading.Lock()
_MY_TRADES_NEXT_TS = 0.0
_BINANCE_REPORT_LOCK = threading.Lock()
_BINANCE_REPORT_NEXT_TS = 0.0

EPS = 1e-12
POINT3_GATE_REASONS = {"spread", "depth", "volume"}
POINT1_GATE_REASONS = {"slope_profile_mismatch"}
POINT2_GATE_REASONS = {"rebound_in_downtrend", "structure_downtrend"}
ROTATION_UNIT_RE = re.compile(r"codex-rotation-([a-z0-9]+)\.service")
ROTATION_STATUS_TIMEOUT_SEC = max(0.25, float(os.getenv("ROTATION_STATUS_TIMEOUT_SEC", "1.5")))
ROTATION_STATUS_RETRIES = max(1, int(os.getenv("ROTATION_STATUS_RETRIES", "3")))
ROTATION_STATUS_RETRY_DELAY_SEC = max(
    0.0, float(os.getenv("ROTATION_STATUS_RETRY_DELAY_SEC", "0.25"))
)
ROTATION_LIVE_CACHE_TTL_SEC = max(0.0, float(os.getenv("ROTATION_LIVE_CACHE_TTL_SEC", "12.0")))
ROTATION_LIVE_STALE_FALLBACK_SEC = max(
    ROTATION_LIVE_CACHE_TTL_SEC,
    float(os.getenv("ROTATION_LIVE_STALE_FALLBACK_SEC", "30.0")),
)
ROTATION_LIVE_JOURNAL_LINES_SELECTED = max(
    500,
    int(os.getenv("ROTATION_LIVE_JOURNAL_LINES_SELECTED", "4000")),
)
ROTATION_LIVE_JOURNAL_LINES_ACTIVE = max(
    500,
    int(os.getenv("ROTATION_LIVE_JOURNAL_LINES_ACTIVE", "2500")),
)
ROTATION_LIVE_DUST_POSITION_USDC = max(
    0.0, float(os.getenv("ROTATION_LIVE_DUST_POSITION_USDC", "1.0"))
)
STRATEGY_ORDER = (
    "staircase",
    "continuation",
    "breakout",
    "rebound",
)

_ROTATION_LIVE_CACHE: tuple[float, dict[str, Any]] | None = None
_ROTATION_LIVE_CACHE_LOCK = threading.Lock()
_ROTATION_LIVE_REFRESH_LOCK = threading.Lock()
_RUNTIME_CONFIG_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_RUNTIME_CONFIG_CACHE_LOCK = threading.Lock()


def add_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Access-Control-Allow-Origin", CORS_ALLOW_ORIGIN)
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Max-Age", "600")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    try:
        handler.send_response(status)
        add_cors_headers(handler)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except OSError:
        # Client disconnected before response was fully written.
        return


def serve_file(handler: BaseHTTPRequestHandler, absolute_path: str) -> bool:
    if not os.path.isfile(absolute_path):
        return False

    with open(absolute_path, "rb") as f:
        data = f.read()

    content_type = mimetypes.guess_type(absolute_path)[0] or "application/octet-stream"
    handler.send_response(200)
    add_cors_headers(handler)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)
    return True


def parse_iso_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_day_utc(day_utc: str) -> tuple[str, str]:
    day_text = day_utc.strip()
    try:
        day = datetime.strptime(day_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Ungueltiger UTC-Tag. Format: YYYY-MM-DD") from exc

    next_day = day + timedelta(days=1)
    return (
        day.isoformat().replace("+00:00", "Z"),
        next_day.isoformat().replace("+00:00", "Z"),
    )


def normalize_usdc_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("/", "").replace("_", "").replace("-", "")
    text = "".join(ch for ch in text if ch.isalnum())
    if not text:
        return ""
    if not text.endswith("USDC"):
        text = f"{text}USDC"
    return text


def symbol_from_journal_path(path: Path) -> str:
    stem = path.stem
    prefix = "journal_live_binance_"
    if not stem.startswith(prefix):
        return ""

    core = stem[len(prefix):]
    if core.endswith("_rotation"):
        core = core[: -len("_rotation")]
    if not core.endswith("_usdc"):
        return ""

    base = core[: -len("_usdc")].strip("_")
    if not base:
        return ""
    return f"{base.upper()}USDC"


def iter_local_journal_files(symbols_filter: list[str] | None = None) -> list[Path]:
    logs_dir = Path(LOGS_DIR)
    if not logs_dir.is_dir():
        return []

    patterns = (
        "journal_live_binance_*_usdc_rotation.jsonl",
        "journal_live_binance_*_usdc.jsonl",
    )
    files: dict[str, Path] = {}
    for pattern in patterns:
        for path in logs_dir.glob(pattern):
            if not path.is_file():
                continue
            symbol = symbol_from_journal_path(path)
            if not symbol:
                continue
            files[symbol] = path

    if symbols_filter:
        wanted = set(symbols_filter)
        return sorted(
            [path for symbol, path in files.items() if symbol in wanted],
            key=lambda p: p.name,
        )
    return sorted(files.values(), key=lambda p: p.name)


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def tail_json_rows(path: Path, max_lines: int = 1200) -> list[dict[str, Any]]:
    if max_lines <= 0 or not path.is_file():
        return []
    tail: list[dict[str, Any]] = []
    buf: list[str] = []
    try:
        from collections import deque

        raw_tail: deque[str] = deque(maxlen=max_lines)
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                row = line.strip()
                if row:
                    raw_tail.append(row)
        buf = list(raw_tail)
    except Exception:
        return []
    for raw in buf:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            tail.append(item)
    return tail


def rotation_journal_path(symbol: str) -> Path | None:
    wanted = f"{str(symbol).strip().upper()}USDC"
    files = iter_local_journal_files([wanted])
    return files[0] if files else None


def reconstruct_live_campaign_metrics(
    journal_path: Path,
    *,
    selected_since_iso: str,
    current_position_qty: float,
    mark_price: float,
    max_lines: int = 4000,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    selected_since = parse_iso_datetime(selected_since_iso)
    if selected_since is None or current_position_qty <= EPS or mark_price <= EPS:
        return None
    if rows is None:
        rows = tail_json_rows(journal_path, max_lines=max_lines)
    if not rows:
        return None

    campaign_qty = 0.0
    campaign_cost = 0.0
    campaign_realized = 0.0
    matched_fills = 0
    started = False
    qty_tolerance = max(1e-9, current_position_qty * 0.01)
    campaign_started_at: datetime | None = None

    for item in rows:
        if item.get("event_type") != "fill":
            continue
        event_ts = parse_iso_datetime(item.get("ts"))
        if event_ts is None or event_ts < selected_since:
            continue
        payload = item.get("payload")
        if not _is_reportable_fill_payload(payload):
            continue
        side = str(payload.get("side") or "").strip().lower()
        qty = max(0.0, to_float(payload.get("qty_btc"), 0.0))
        price = max(0.0, to_float(payload.get("price"), 0.0))
        fee = max(0.0, to_float(payload.get("fee_eur"), 0.0))
        if qty <= 0.0 or price <= 0.0:
            continue

        if side == "buy":
            if campaign_qty <= EPS:
                campaign_started_at = event_ts
            campaign_qty += qty
            campaign_cost += (qty * price) + fee
            matched_fills += 1
            started = True
            continue

        if side != "sell":
            continue
        if not started:
            return None
        if campaign_qty <= 0.0:
            return None
        if qty > (campaign_qty + qty_tolerance):
            return None
        sell_qty = min(qty, campaign_qty)
        avg_cost = (campaign_cost / campaign_qty) if campaign_qty > EPS else 0.0
        alloc_cost = avg_cost * sell_qty
        campaign_realized += (sell_qty * price) - fee - alloc_cost
        campaign_qty = max(0.0, campaign_qty - sell_qty)
        campaign_cost = max(0.0, campaign_cost - alloc_cost)
        matched_fills += 1
        if campaign_qty <= EPS:
            campaign_qty = 0.0
            campaign_cost = 0.0
            campaign_started_at = None

    if matched_fills <= 0 or campaign_qty <= EPS or campaign_cost <= EPS:
        return None
    if abs(campaign_qty - current_position_qty) > qty_tolerance:
        return None

    avg_entry_price = campaign_cost / campaign_qty if campaign_qty > EPS else 0.0
    if avg_entry_price <= EPS:
        return None

    unrealized = (mark_price - avg_entry_price) * current_position_qty
    entry_value = current_position_qty * avg_entry_price
    exit_value = current_position_qty * mark_price
    return {
        "positionQty": float(current_position_qty),
        "entryPrice": float(avg_entry_price),
        "entryValueUsdc": float(entry_value),
        "exitValueUsdc": float(exit_value),
        "realizedPnlUsdc": float(campaign_realized),
        "unrealizedPnlUsdc": float(unrealized),
        "totalPnlUsdc": float(campaign_realized + unrealized),
        "entryOpenedAt": campaign_started_at.isoformat().replace("+00:00", "Z")
        if campaign_started_at is not None
        else "",
    }


def infer_live_entry_opened_at(
    journal_path: Path,
    *,
    selected_since_iso: str,
    current_position_qty: float,
    max_lines: int = 2500,
    rows: list[dict[str, Any]] | None = None,
) -> str:
    selected_since = parse_iso_datetime(selected_since_iso)
    if current_position_qty <= EPS:
        return ""
    if rows is None:
        rows = tail_json_rows(journal_path, max_lines=max_lines)
    if not rows:
        return ""

    qty_tolerance = max(1e-9, current_position_qty * 0.02)

    def _resolve(min_ts: datetime | None) -> str:
        # Prefer explicitly recovered exchange entry references when available.
        for item in reversed(rows):
            if item.get("event_type") != "exec_entry_reference_recovered":
                continue
            event_ts = parse_iso_datetime(item.get("ts"))
            if event_ts is None or (min_ts is not None and event_ts < min_ts):
                continue
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            entry_ts = parse_iso_datetime(payload.get("entry_ts"))
            if entry_ts is None:
                continue
            pos = abs(to_float(payload.get("position_btc"), 0.0))
            replayed_pos = abs(to_float(payload.get("replayed_position_btc"), 0.0))
            if pos > EPS and abs(pos - current_position_qty) <= max(qty_tolerance, pos * 0.02):
                return entry_ts.isoformat().replace("+00:00", "Z")
            if replayed_pos > EPS and abs(replayed_pos - current_position_qty) <= max(
                qty_tolerance, replayed_pos * 0.02
            ):
                return entry_ts.isoformat().replace("+00:00", "Z")
            if pos <= EPS and replayed_pos <= EPS:
                return entry_ts.isoformat().replace("+00:00", "Z")

        # Fallback: walk fills backwards and find the earliest buy that composes current size.
        remaining_qty = float(current_position_qty)
        for item in reversed(rows):
            if item.get("event_type") != "fill":
                continue
            event_ts = parse_iso_datetime(item.get("ts"))
            if event_ts is None or (min_ts is not None and event_ts < min_ts):
                continue
            payload = item.get("payload")
            if not _is_reportable_fill_payload(payload):
                continue
            side = str(payload.get("side") or "").strip().lower()
            qty = max(0.0, to_float(payload.get("qty_btc"), 0.0))
            if qty <= 0.0:
                continue
            if side == "sell":
                remaining_qty += qty
                continue
            if side != "buy":
                continue
            remaining_qty -= qty
            if remaining_qty <= qty_tolerance:
                return event_ts.isoformat().replace("+00:00", "Z")
        return ""

    opened_at = _resolve(selected_since)
    if opened_at:
        return opened_at
    # Selection timestamps can jump when the selector rotates state even while a
    # position is still open. Keep the hold timer stable by falling back to the
    # latest matching entry reference in recent journal history.
    return _resolve(None)


def extract_recent_corridor_state(
    journal_path: Path,
    *,
    max_lines: int = 2500,
    entry_price: float | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if rows is None:
        rows = tail_json_rows(journal_path, max_lines=max_lines)
    if not rows:
        return None

    trend_window_minutes = 10
    trend_window_hour_minutes = 60
    trend_window_5h_minutes = 300
    trend_window_10h_minutes = 600
    trend_window_10h_points = 120
    trend_window_day_minutes = 1440
    needed_samples = (
        max(
            trend_window_minutes,
            trend_window_hour_minutes,
            trend_window_5h_minutes,
            trend_window_10h_minutes,
            trend_window_day_minutes,
        )
        + 1
    )
    samples: list[dict[str, Any]] = []
    for item in reversed(rows):
        if item.get("event_type") != "core_decision":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        features = payload.get("features")
        if not isinstance(features, dict):
            continue
        risk_payload = payload.get("risk")
        if not isinstance(risk_payload, dict):
            risk_payload = {}
        price = to_float(features.get("price"), 0.0)
        pos_pct: float | None = None

        corridor_low = to_float(features.get("corridor_low_price"), 0.0)
        corridor_high = to_float(features.get("corridor_high_price"), 0.0)
        if corridor_high > corridor_low > 0.0 and price > 0.0:
            # Use the real percentage within/below/above corridor.
            pos_pct = ((price - corridor_low) / (corridor_high - corridor_low)) * 100.0
        else:
            try:
                corridor_pct = float(features.get("corridor_position_pct"))
            except (TypeError, ValueError):
                corridor_pct = float("nan")
            if math.isfinite(corridor_pct):
                pos_pct = corridor_pct
            else:
                context_pos = features.get("context_range_pos")
                try:
                    context_pos_f = float(context_pos)
                except (TypeError, ValueError):
                    context_pos_f = float("nan")
                if math.isfinite(context_pos_f):
                    pos_pct = context_pos_f * 100.0
        if pos_pct is None or not math.isfinite(pos_pct):
            continue
        signal_ts = str(payload.get("ts") or item.get("ts") or "").strip()
        samples.append(
            {
                "posPct": float(pos_pct),
                "price": float(price),
                "signalTs": signal_ts,
                "corridorLowPrice": to_float(features.get("corridor_low_price"), 0.0),
                "corridorHighPrice": to_float(features.get("corridor_high_price"), 0.0),
                "corridorStagedExitStepPct": to_float(
                    features.get("corridor_staged_exit_step_pct"),
                    10.0,
                ),
                "corridorStagedHysteresisPct": to_float(
                    features.get("corridor_staged_hysteresis_pct"),
                    0.75,
                ),
                "corridorStagedNoBuyAbovePct": to_float(
                    features.get("corridor_staged_no_buy_above_pct"),
                    50.0,
                ),
                "corridorStagedProfitTargetEnabled": to_float(
                    features.get("corridor_staged_profit_target_enabled"),
                    0.0,
                )
                >= 0.5,
                "corridorStagedProfitTargetBasePct": to_float(
                    features.get("corridor_staged_profit_target_base_pct"),
                    0.0,
                ),
                "corridorStagedProfitTargetMinPct": to_float(
                    features.get("corridor_staged_profit_target_min_pct"),
                    0.0,
                ),
                "corridorStagedProfitTargetMaxPct": to_float(
                    features.get("corridor_staged_profit_target_max_pct"),
                    100.0,
                ),
                "corridorStagedProfitTargetMult10": to_float(
                    features.get("corridor_staged_profit_target_mult_10"),
                    1.25,
                ),
                "corridorStagedProfitTargetMult20": to_float(
                    features.get("corridor_staged_profit_target_mult_20"),
                    1.10,
                ),
                "corridorStagedProfitTargetMult30": to_float(
                    features.get("corridor_staged_profit_target_mult_30"),
                    1.00,
                ),
                "corridorStagedProfitTargetMult40": to_float(
                    features.get("corridor_staged_profit_target_mult_40"),
                    0.90,
                ),
                "corridorStagedProfitTargetMult50": to_float(
                    features.get("corridor_staged_profit_target_mult_50"),
                    0.80,
                ),
                "corridorExitArmed": to_bool(
                    risk_payload.get("corridor_exit_armed"),
                    False,
                ),
                "corridorExitPeakPct": to_float(
                    risk_payload.get("corridor_exit_peak_pct"),
                    0.0,
                ),
                "corridorStagedModeEnabled": to_float(
                    features.get("corridor_staged_mode_enabled"),
                    0.0,
                )
                >= 0.5,
            }
        )
        if len(samples) >= needed_samples:
            break

    if not samples:
        return None

    latest = samples[0]
    latest_price = float(latest.get("price") or 0.0)
    latest_ts = str(latest.get("signalTs") or "")

    def _avg_window(start: int, size: int) -> float | None:
        window = [float(item.get("posPct") or 0.0) for item in samples[start : start + size]]
        if not window:
            return None
        return float(sum(window) / max(1, len(window)))

    def _avg_price_window(start: int, size: int) -> float | None:
        window = [float(item.get("price") or 0.0) for item in samples[start : start + size]]
        if not window:
            return None
        return float(sum(window) / max(1, len(window)))

    latest_avg_pos = _avg_window(0, trend_window_minutes)
    prev_pos = latest_avg_pos if latest_avg_pos is not None else 0.0
    prev_price = latest_price
    if len(samples) >= 2:
        prev_avg = _avg_window(1, trend_window_minutes)
        if prev_avg is not None:
            prev_pos = prev_avg
        prev = samples[1]
        prev_price = float(prev.get("price") or 0.0)

    latest_avg_pos = latest_avg_pos if latest_avg_pos is not None else prev_pos
    delta_pct = latest_avg_pos - prev_pos
    price_delta = latest_price - prev_price
    direction = "flat"
    if delta_pct > 0.02:
        direction = "up"
    elif delta_pct < -0.02:
        direction = "down"
    elif price_delta > 0.0:
        direction = "up"
    elif price_delta < 0.0:
        direction = "down"

    # Compare each long trend window against a shifted reference window,
    # not just against the previous minute, to avoid twitchy direction flips.
    hour_shift = max(1, min(30, trend_window_hour_minutes // 6))
    day_shift = max(1, min(180, trend_window_day_minutes // 24))

    hour_avg = _avg_window(0, trend_window_hour_minutes)
    hour_prev = _avg_window(hour_shift, trend_window_hour_minutes)
    hour_delta = None
    hour_direction = "flat"
    if hour_avg is not None:
        if hour_prev is None:
            hour_prev = hour_avg
        hour_delta = float(hour_avg - hour_prev)
        if hour_delta > 0.02:
            hour_direction = "up"
        elif hour_delta < -0.02:
            hour_direction = "down"

    day_avg = _avg_window(0, trend_window_day_minutes)
    day_prev = _avg_window(day_shift, trend_window_day_minutes)
    day_delta = None
    day_direction = "flat"
    if day_avg is not None:
        if day_prev is None:
            day_prev = day_avg
        day_delta = float(day_avg - day_prev)
        if day_delta > 0.02:
            day_direction = "up"
        elif day_delta < -0.02:
            day_direction = "down"

    entry = max(0.0, to_float(entry_price, 0.0))
    entry_trend_pct_10m = None
    entry_trend_delta_10m = None
    entry_trend_direction_10m = "flat"
    entry_trend_pct_1h = None
    entry_trend_delta_1h = None
    entry_trend_direction_1h = "flat"
    entry_trend_pct_5h = None
    entry_trend_series_10h: list[float] = []
    entry_trend_pct_24h = None
    entry_trend_delta_24h = None
    entry_trend_direction_24h = "flat"
    if entry > EPS:
        # 10m trend should represent the recent 10-minute move, not distance to entry.
        price_avg_10m = _avg_price_window(0, trend_window_minutes)
        price_ref_10m = _avg_price_window(trend_window_minutes, trend_window_minutes)
        if price_ref_10m is None and samples:
            lookback_idx = min(len(samples) - 1, trend_window_minutes)
            price_ref_10m = to_float(samples[lookback_idx].get("price"), 0.0)
        if (
            price_avg_10m is not None
            and price_ref_10m is not None
            and price_avg_10m > EPS
            and price_ref_10m > EPS
        ):
            entry_trend_pct_10m = ((price_avg_10m / price_ref_10m) - 1.0) * 100.0

            price_avg_10m_prev = _avg_price_window(1, trend_window_minutes)
            price_ref_10m_prev = _avg_price_window(
                trend_window_minutes + 1,
                trend_window_minutes,
            )
            if price_ref_10m_prev is None and samples:
                prev_lookback_idx = min(len(samples) - 1, trend_window_minutes + 1)
                price_ref_10m_prev = to_float(samples[prev_lookback_idx].get("price"), 0.0)
            if (
                price_avg_10m_prev is not None
                and price_ref_10m_prev is not None
                and price_avg_10m_prev > EPS
                and price_ref_10m_prev > EPS
            ):
                prev_entry_pct_10m = ((price_avg_10m_prev / price_ref_10m_prev) - 1.0) * 100.0
                entry_trend_delta_10m = entry_trend_pct_10m - prev_entry_pct_10m

            if entry_trend_pct_10m > 0.01:
                entry_trend_direction_10m = "up"
            elif entry_trend_pct_10m < -0.01:
                entry_trend_direction_10m = "down"

        price_avg_1h = _avg_price_window(0, trend_window_hour_minutes)
        price_prev_1h = _avg_price_window(hour_shift, trend_window_hour_minutes)
        if price_avg_1h is not None:
            if price_prev_1h is None:
                price_prev_1h = price_avg_1h
            entry_trend_pct_1h = ((price_avg_1h / entry) - 1.0) * 100.0
            prev_entry_pct_1h = ((price_prev_1h / entry) - 1.0) * 100.0
            entry_trend_delta_1h = entry_trend_pct_1h - prev_entry_pct_1h
            if entry_trend_delta_1h > 0.01:
                entry_trend_direction_1h = "up"
            elif entry_trend_delta_1h < -0.01:
                entry_trend_direction_1h = "down"

        price_avg_5h = _avg_price_window(0, trend_window_5h_minutes)
        if price_avg_5h is not None:
            entry_trend_pct_5h = ((price_avg_5h / entry) - 1.0) * 100.0

        # 10h sparkline relative to entry.
        minute_series: list[float] = []
        sample_count = len(samples)
        oldest_idx = max(0, sample_count - 1)
        for minute_ago in range(trend_window_10h_minutes - 1, -1, -1):
            sample_idx = minute_ago if minute_ago < sample_count else oldest_idx
            sample_price = to_float(samples[sample_idx].get("price"), 0.0)
            if sample_price > EPS:
                minute_series.append(((sample_price / entry) - 1.0) * 100.0)
            elif minute_series:
                minute_series.append(minute_series[-1])
            else:
                minute_series.append(0.0)
        if minute_series:
            points_target = max(16, int(trend_window_10h_points))
            if len(minute_series) <= points_target:
                entry_trend_series_10h = minute_series
            else:
                # Evenly resample to a fixed-size series for compact payload + smooth UI.
                resampled: list[float] = []
                span = len(minute_series) - 1
                for i in range(points_target):
                    pos = (i * span) / max(1, points_target - 1)
                    lo = int(math.floor(pos))
                    hi = int(math.ceil(pos))
                    frac = pos - lo
                    lo_v = minute_series[lo]
                    hi_v = minute_series[hi]
                    resampled.append((lo_v * (1.0 - frac)) + (hi_v * frac))
                entry_trend_series_10h = resampled

        price_avg_24h = _avg_price_window(0, trend_window_day_minutes)
        price_prev_24h = _avg_price_window(day_shift, trend_window_day_minutes)
        if price_avg_24h is not None:
            if price_prev_24h is None:
                price_prev_24h = price_avg_24h
            entry_trend_pct_24h = ((price_avg_24h / entry) - 1.0) * 100.0
            prev_entry_pct_24h = ((price_prev_24h / entry) - 1.0) * 100.0
            entry_trend_delta_24h = entry_trend_pct_24h - prev_entry_pct_24h
            if entry_trend_delta_24h > 0.01:
                entry_trend_direction_24h = "up"
            elif entry_trend_delta_24h < -0.01:
                entry_trend_direction_24h = "down"

    return {
        "rangePositionPct": float(latest_avg_pos),
        "rangePositionDeltaPct": float(delta_pct),
        "rangeDirection": direction,
        "rangeSignalTs": latest_ts,
        "rangePrice": float(latest_price),
        "rangeSmoothingWindowMin": int(trend_window_minutes),
        "rangePositionPctHour": float(hour_avg) if hour_avg is not None else None,
        "rangePositionDeltaPctHour": float(hour_delta) if hour_delta is not None else None,
        "rangeDirectionHour": hour_direction,
        "rangeSmoothingWindowHourMin": int(trend_window_hour_minutes),
        "rangePositionPctDay": float(day_avg) if day_avg is not None else None,
        "rangePositionDeltaPctDay": float(day_delta) if day_delta is not None else None,
        "rangeDirectionDay": day_direction,
        "rangeSmoothingWindowDayMin": int(trend_window_day_minutes),
        "entryTrendPct10m": float(entry_trend_pct_10m) if entry_trend_pct_10m is not None else None,
        "entryTrendDeltaPct10m": float(entry_trend_delta_10m) if entry_trend_delta_10m is not None else None,
        "entryTrendDirection10m": entry_trend_direction_10m,
        "entryTrendPct1h": float(entry_trend_pct_1h) if entry_trend_pct_1h is not None else None,
        "entryTrendDeltaPct1h": float(entry_trend_delta_1h) if entry_trend_delta_1h is not None else None,
        "entryTrendDirection1h": entry_trend_direction_1h,
        "entryTrendPct5h": float(entry_trend_pct_5h) if entry_trend_pct_5h is not None else None,
        "entryTrendSeries10h": [float(round(v, 6)) for v in entry_trend_series_10h],
        "entryTrendWindow10hMin": int(trend_window_10h_minutes),
        "entryTrendSeriesPoints10h": int(len(entry_trend_series_10h)),
        "entryTrendPct24h": float(entry_trend_pct_24h) if entry_trend_pct_24h is not None else None,
        "entryTrendDeltaPct24h": float(entry_trend_delta_24h) if entry_trend_delta_24h is not None else None,
        "entryTrendDirection24h": entry_trend_direction_24h,
        "corridorLowPrice": float(latest.get("corridorLowPrice") or 0.0),
        "corridorHighPrice": float(latest.get("corridorHighPrice") or 0.0),
        "corridorStagedExitStepPct": float(latest.get("corridorStagedExitStepPct") or 10.0),
        "corridorStagedHysteresisPct": float(latest.get("corridorStagedHysteresisPct") or 0.75),
        "corridorStagedNoBuyAbovePct": float(latest.get("corridorStagedNoBuyAbovePct") or 50.0),
        "corridorStagedProfitTargetEnabled": bool(latest.get("corridorStagedProfitTargetEnabled")),
        "corridorStagedProfitTargetBasePct": float(latest.get("corridorStagedProfitTargetBasePct") or 0.0),
        "corridorStagedProfitTargetMinPct": float(latest.get("corridorStagedProfitTargetMinPct") or 0.0),
        "corridorStagedProfitTargetMaxPct": float(latest.get("corridorStagedProfitTargetMaxPct") or 100.0),
        "corridorStagedProfitTargetMult10": float(latest.get("corridorStagedProfitTargetMult10") or 1.25),
        "corridorStagedProfitTargetMult20": float(latest.get("corridorStagedProfitTargetMult20") or 1.10),
        "corridorStagedProfitTargetMult30": float(latest.get("corridorStagedProfitTargetMult30") or 1.00),
        "corridorStagedProfitTargetMult40": float(latest.get("corridorStagedProfitTargetMult40") or 0.90),
        "corridorStagedProfitTargetMult50": float(latest.get("corridorStagedProfitTargetMult50") or 0.80),
        "corridorExitArmed": to_bool(latest.get("corridorExitArmed"), False),
        "corridorExitPeakPct": float(latest.get("corridorExitPeakPct") or 0.0),
        "corridorStagedModeEnabled": bool(latest.get("corridorStagedModeEnabled")),
    }


def compute_live_exit_tracker(row: dict[str, Any]) -> dict[str, Any]:
    qty = max(0.0, to_float(row.get("positionQty"), 0.0))
    mark = max(0.0, to_float(row.get("exitPrice"), 0.0))
    entry = max(0.0, to_float(row.get("entryPrice"), 0.0))
    low = max(0.0, to_float(row.get("corridorLowPrice"), 0.0))
    high = max(0.0, to_float(row.get("corridorHighPrice"), 0.0))
    span = high - low
    if qty <= EPS or mark <= EPS or entry <= EPS:
        return {
            "exitNeedUsdc": None,
            "exitBonusUsdc": None,
            "exitRollArmed": False,
            "exitEntryStagePct": None,
            "exitTargetPct": None,
            "exitTargetPrice": None,
            "exitArmUsdc": None,
            "exitRetraceUsdc": None,
            "exitMode": "unavailable",
        }

    no_buy_above = max(
        1.0,
        min(100.0, to_float(row.get("corridorStagedNoBuyAbovePct"), 50.0)),
    )
    exit_step = max(0.1, to_float(row.get("corridorStagedExitStepPct"), 10.0))
    hysteresis = max(0.0, to_float(row.get("corridorStagedHysteresisPct"), 0.75))
    current_pos_pct = to_float(row.get("rangePositionPct"), float("nan"))
    if not math.isfinite(current_pos_pct) and low > EPS and high > low and span > EPS:
        current_pos_pct = ((mark - low) / span) * 100.0
    entry_pos_pct = ((entry - low) / span) * 100.0 if low > EPS and high > low and span > EPS else float("nan")

    entry_levels = sorted({10.0, 20.0, 30.0, 40.0})
    entry_levels = [level for level in entry_levels if level < max(1.0, no_buy_above)]
    if not entry_levels:
        entry_levels = [min(40.0, max(1.0, no_buy_above - 1.0))]
    if math.isfinite(entry_pos_pct):
        entry_stage = min((level for level in entry_levels if entry_pos_pct <= level), default=entry_levels[-1])
    else:
        entry_stage = 30.0

    roll_state_raw = row.get("corridorExitArmed")
    roll_state_explicit = None if roll_state_raw is None else to_bool(roll_state_raw, False)

    abs_roll_enabled = to_bool(row.get("profitRollExitEnabled"), False)
    abs_roll_arm_usdc = max(0.0, to_float(row.get("profitRollArmUsdc"), 0.0))
    abs_roll_retrace_usdc = max(0.0, to_float(row.get("profitRollRetraceUsdc"), 0.0))
    use_abs_roll = abs_roll_enabled and abs_roll_arm_usdc > 0.0 and abs_roll_retrace_usdc > 0.0
    if use_abs_roll:
        open_pnl_usdc = (mark - entry) * qty
        target_price = entry + (abs_roll_arm_usdc / qty) if qty > EPS else 0.0
        entry_notional = max(EPS, entry * qty)
        target_pct = (abs_roll_arm_usdc / entry_notional) * 100.0
        price_target_armed = open_pnl_usdc >= abs_roll_arm_usdc
        roll_armed = (
            roll_state_explicit
            if roll_state_explicit is not None
            else price_target_armed
        )
        need_usdc = max(0.0, abs_roll_arm_usdc - open_pnl_usdc)
        bonus_usdc = max(0.0, open_pnl_usdc - abs_roll_arm_usdc) if roll_armed else 0.0
        return {
            "exitNeedUsdc": float(need_usdc),
            "exitBonusUsdc": float(bonus_usdc),
            "exitRollArmed": bool(roll_armed),
            "exitEntryStagePct": float(entry_stage),
            "exitTargetPct": float(target_pct),
            "exitTargetPrice": (None if target_price <= 0.0 else float(target_price)),
            "exitArmUsdc": float(abs_roll_arm_usdc),
            "exitRetraceUsdc": float(abs_roll_retrace_usdc),
            "exitMode": "profit_roll_abs",
        }

    profit_target_enabled = bool(row.get("corridorStagedProfitTargetEnabled"))
    if profit_target_enabled:
        target_base_pct = max(0.0, to_float(row.get("corridorStagedProfitTargetBasePct"), 0.0))
        target_min_pct = max(0.0, to_float(row.get("corridorStagedProfitTargetMinPct"), 0.0))
        target_max_pct = max(target_min_pct, to_float(row.get("corridorStagedProfitTargetMaxPct"), 100.0))
        mult_10 = max(0.1, to_float(row.get("corridorStagedProfitTargetMult10"), 1.25))
        mult_20 = max(0.1, to_float(row.get("corridorStagedProfitTargetMult20"), 1.10))
        mult_30 = max(0.1, to_float(row.get("corridorStagedProfitTargetMult30"), 1.00))
        mult_40 = max(0.1, to_float(row.get("corridorStagedProfitTargetMult40"), 0.90))
        mult_50 = max(0.1, to_float(row.get("corridorStagedProfitTargetMult50"), 0.80))

        if entry_stage <= 10.0:
            stage_mult = mult_10
        elif entry_stage <= 20.0:
            stage_mult = mult_20
        elif entry_stage <= 30.0:
            stage_mult = mult_30
        elif entry_stage <= 40.0:
            stage_mult = mult_40
        else:
            stage_mult = mult_50

        target_pct = target_base_pct * stage_mult
        target_pct = max(target_min_pct, min(target_max_pct, target_pct))
        target_price = entry * (1.0 + (target_pct / 100.0))
        need_usdc = max(0.0, (target_price - mark) * qty)
        price_target_armed = mark >= target_price
        roll_armed = (
            roll_state_explicit
            if roll_state_explicit is not None
            else price_target_armed
        )
        bonus_usdc = max(0.0, (mark - target_price) * qty) if roll_armed else 0.0
        exit_mode = "profit_target"
    else:
        target_pct = min(no_buy_above, entry_stage + exit_step)
        if low > EPS and high > low and span > EPS:
            target_price = low + (span * (target_pct / 100.0))
        else:
            target_price = 0.0
        need_usdc = max(0.0, (target_price - mark) * qty) if target_price > 0.0 else None
        price_target_armed = (
            math.isfinite(current_pos_pct)
            and current_pos_pct >= max(0.0, target_pct - hysteresis)
        )
        roll_armed = (
            roll_state_explicit
            if roll_state_explicit is not None
            else price_target_armed
        )
        bonus_usdc = (
            max(0.0, (mark - target_price) * qty)
            if roll_armed and target_price > 0.0
            else 0.0
        )
        exit_mode = "stage_roll"

    return {
        "exitNeedUsdc": (None if need_usdc is None else float(need_usdc)),
        "exitBonusUsdc": float(bonus_usdc),
        "exitRollArmed": bool(roll_armed),
        "exitEntryStagePct": float(entry_stage),
        "exitTargetPct": float(target_pct),
        "exitTargetPrice": (None if target_price <= 0.0 else float(target_price)),
        "exitArmUsdc": None,
        "exitRetraceUsdc": None,
        "exitMode": str(exit_mode),
    }


def iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _is_reportable_fill_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    source = str(payload.get("source") or "").strip().lower()
    if source == "account_sync_delta":
        return False
    order_id = str(payload.get("order_id") or "").strip().lower()
    if order_id.startswith("account_sync_delta_"):
        return False
    return True


def extract_error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "detail", "error"):
            value = payload.get(key)
            if value:
                return str(value)
    return str(payload)


def binance_get(path: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    query = urlencode(params or {})
    url = f"{BINANCE_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"

    req = Request(url=url, method="GET")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body
        raise RuntimeError(f"Binance Fehler {exc.code}: {extract_error_text(payload)}") from exc
    except URLError as exc:
        raise RuntimeError(f"Binance Netzwerkfehler: {exc}") from exc


def fetch_server_time_offset_ms() -> int:
    data = binance_get("/api/v3/time")
    server_time = int(data.get("serverTime", 0))
    if server_time <= 0:
        raise RuntimeError("Binance serverTime konnte nicht gelesen werden.")
    return server_time - int(time.time() * 1000)


def fetch_usdc_symbols() -> list[str]:
    data = binance_get("/api/v3/exchangeInfo")
    symbols_data = data.get("symbols", [])

    symbols: list[str] = []
    for item in symbols_data:
        symbol = str(item.get("symbol", "")).upper()
        quote = str(item.get("quoteAsset", "")).upper()
        status = str(item.get("status", "")).upper()
        if quote == "USDC" and status == "TRADING" and symbol.endswith("USDC"):
            symbols.append(symbol)

    return sorted(set(symbols))


def get_usdc_symbols_cached() -> list[str]:
    global _SYMBOL_CACHE, _SYMBOL_CACHE_AT

    now = time.time()
    if _SYMBOL_CACHE and (now - _SYMBOL_CACHE_AT) < _SYMBOL_CACHE_TTL_SEC:
        return _SYMBOL_CACHE

    _SYMBOL_CACHE = fetch_usdc_symbols()
    _SYMBOL_CACHE_AT = now
    return _SYMBOL_CACHE


def fetch_my_trades_for_symbol(symbol: str, start_ms: int, end_ms: int, offset_ms: int) -> list[dict[str, Any]]:
    global _MY_TRADES_NEXT_TS

    def is_rate_limit_error(exc: Exception) -> bool:
        text = str(exc)
        return "Binance Fehler 429" in text or "Too much request weight" in text

    last_exc: Exception | None = None
    for attempt in range(MY_TRADES_MAX_RETRIES):
        timestamp = int(time.time() * 1000) + offset_ms
        params: dict[str, Any] = {
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1000,
            "recvWindow": 60000,
            "timestamp": timestamp,
        }

        query = urlencode(params)
        signature = hmac.new(
            BINANCE_API_SECRET.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        signed = dict(params)
        signed["signature"] = signature

        wait_for = 0.0
        with _MY_TRADES_LOCK:
            now = time.monotonic()
            if now < _MY_TRADES_NEXT_TS:
                wait_for = _MY_TRADES_NEXT_TS - now
            _MY_TRADES_NEXT_TS = max(_MY_TRADES_NEXT_TS, now) + MY_TRADES_MIN_INTERVAL_SEC
        if wait_for > 0.0:
            time.sleep(wait_for)

        try:
            data = binance_get(
                "/api/v3/myTrades",
                params=signed,
                headers={"X-MBX-APIKEY": BINANCE_API_KEY},
            )
            if not isinstance(data, list):
                return []
            return [item for item in data if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, Exception):
                raise
            last_exc = exc
            if not is_rate_limit_error(exc) or attempt >= (MY_TRADES_MAX_RETRIES - 1):
                raise
            backoff = MY_TRADES_RETRY_BASE_SEC * (2**attempt)
            time.sleep(backoff)

    if last_exc is not None:
        raise last_exc
    return []


def is_binance_rate_limit_error_text(text: str) -> bool:
    msg = str(text or "").lower()
    return any(
        token in msg
        for token in (
            "binance fehler 429",
            "binance fehler 418",
            "too much request weight",
            "ip banned until",
            "request weight",
        )
    )


def _acquire_binance_report_budget(symbol_count: int) -> None:
    global _BINANCE_REPORT_NEXT_TS
    wait_for = 0.0
    with _BINANCE_REPORT_LOCK:
        now = time.monotonic()
        if now < _BINANCE_REPORT_NEXT_TS:
            wait_for = _BINANCE_REPORT_NEXT_TS - now
        # Larger scans consume proportionally more budget and should wait longer.
        slot = BINANCE_REPORT_MIN_INTERVAL_SEC * max(1, int(symbol_count))
        _BINANCE_REPORT_NEXT_TS = max(_BINANCE_REPORT_NEXT_TS, now) + slot
    if wait_for > 0.0:
        time.sleep(wait_for)


def require_config() -> str | None:
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return "Relay ist nicht konfiguriert (Binance API Key/Secret fehlen)."
    if not RELAY_TOKEN:
        return "Relay ist nicht konfiguriert (RELAY_TOKEN fehlt)."
    return None


def check_auth(handler: BaseHTTPRequestHandler) -> bool:
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        json_response(handler, 401, {"detail": "Authorization Bearer Token fehlt."})
        return False

    token = auth.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, RELAY_TOKEN):
        json_response(handler, 401, {"detail": "Ungueltiger Relay Token."})
        return False

    return True


def load_rotation_payload() -> tuple[str, dict[str, Any]]:
    rotation_file = ROTATION_ACTIVE_FILE or DEFAULT_ROTATION_ACTIVE_FILE
    if not os.path.isfile(rotation_file):
        raise FileNotFoundError(f"Rotation-Datei nicht gefunden: {rotation_file}")

    with open(rotation_file, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError("Rotation-Datei ist ungueltig (kein JSON-Objekt).")
    return rotation_file, payload


def load_rotation_meta_summary() -> dict[str, Any]:
    configured_lookback_hours = to_float(os.getenv("ROTATION_META_TRADE_LOOKBACK_HOURS"), 0.0)
    report_path = Path(ROTATION_META_SHADOW_REPORT_FILE)
    empty: dict[str, Any] = {
        "generatedAt": "",
        "currentProfile": "",
        "riskMode": "",
        "metaMode": "",
        "confidence": 0.0,
        "lookbackHours": 0.0,
        "notes": "",
        "rows": [],
        "available": False,
    }
    if not report_path.is_file():
        return empty

    try:
        with report_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return empty

    if not isinstance(payload, dict):
        return empty

    recommendation = payload.get("recommendation")
    if not isinstance(recommendation, dict):
        recommendation = {}
    trade_summary = payload.get("trade_summary")
    if not isinstance(trade_summary, dict):
        trade_summary = {}
    strategy_breakdown = trade_summary.get("strategy_breakdown")
    if not isinstance(strategy_breakdown, dict):
        strategy_breakdown = {}
    watch_summary = payload.get("watch_pool_strategy_summary")
    if not isinstance(watch_summary, dict):
        watch_summary = {}
    universe_rankings = payload.get("universe_strategy_rankings")
    if not isinstance(universe_rankings, dict):
        universe_rankings = {}

    strategy_weights = recommendation.get("strategy_weights")
    if not isinstance(strategy_weights, dict):
        strategy_weights = {}
    strategy_actions = recommendation.get("strategy_actions")
    if not isinstance(strategy_actions, dict):
        strategy_actions = {}

    strategy_names = list(STRATEGY_ORDER)

    rows: list[dict[str, Any]] = []
    for strategy in strategy_names:
        stats = strategy_breakdown.get(strategy)
        if not isinstance(stats, dict):
            stats = {}
        action = strategy_actions.get(strategy)
        if not isinstance(action, dict):
            action = {}
        watch = watch_summary.get(strategy)
        if not isinstance(watch, dict):
            watch = {}
        universe = universe_rankings.get(strategy)
        if not isinstance(universe, list):
            universe = []

        exit_reasons = stats.get("exit_reasons")
        if not isinstance(exit_reasons, dict):
            exit_reasons = {}
        top_symbols = stats.get("top_symbols")
        if not isinstance(top_symbols, list):
            top_symbols = []

        watch_candidates = watch.get("top_candidates")
        if not isinstance(watch_candidates, list):
            watch_candidates = []
        watch_top_symbols = [
            str(item.get("symbol", "")).strip().upper()
            for item in watch_candidates
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        ][:3]

        universe_top_symbols = [
            str(item.get("symbol", "")).strip().upper()
            for item in universe
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        ][:3]

        failed_start_count = sum(
            to_int(value, 0)
            for key, value in exit_reasons.items()
            if "failed_start" in str(key).strip().lower()
        )

        rows.append(
            {
                "strategy": strategy,
                "weight": to_float(strategy_weights.get(strategy), 0.0),
                "action": {
                    "mode": str(action.get("mode", "")).strip().lower(),
                    "slotTarget": to_int(action.get("slot_target"), 0),
                    "topSymbols": [
                        str(item).strip().upper()
                        for item in action.get("top_symbols", [])
                        if str(item).strip()
                    ][:4],
                },
                "tradeCount": to_int(stats.get("trade_count"), 0),
                "netPnlUsdc": to_float(stats.get("net_pnl"), 0.0),
                "winRate": to_float(stats.get("win_rate"), 0.0),
                "avgHoldSec": to_float(stats.get("avg_hold_sec"), 0.0),
                "avgWinUsdc": to_float(stats.get("avg_win"), 0.0),
                "avgLossUsdc": to_float(stats.get("avg_loss"), 0.0),
                "lastExitAt": str(stats.get("last_exit_ts") or "").strip(),
                "failedStartExitCount": failed_start_count,
                "exitReasons": {
                    str(key): to_int(value, 0)
                    for key, value in exit_reasons.items()
                    if str(key).strip()
                },
                "topSymbols": [str(item).strip().upper() for item in top_symbols if str(item).strip()][:4],
                "watchCandidateCount": to_int(watch.get("candidate_count"), 0),
                "buyReadyCount": to_int(watch.get("buy_ready_count"), 0),
                "mlPositiveCount": to_int(watch.get("ml_positive_count"), 0),
                "avgTopPProfit": to_float(watch.get("avg_top_p_profit"), 0.0),
                "avgTopStrategyScore": to_float(watch.get("avg_top_strategy_score"), 0.0),
                "dominantGateReasons": {
                    str(key): to_int(value, 0)
                    for key, value in (watch.get("dominant_gate_reasons") or {}).items()
                    if str(key).strip()
                },
                "watchTopSymbols": watch_top_symbols,
                "universeTopSymbols": universe_top_symbols,
            }
        )

    return {
        "generatedAt": str(payload.get("generated_at", "")).strip(),
        "currentProfile": str(
            payload.get("current_profile")
            or recommendation.get("profile")
            or ""
        ).strip(),
        "riskMode": str(recommendation.get("risk_mode", "")).strip(),
        "metaMode": str(payload.get("meta_mode", "")).strip(),
        "confidence": to_float(recommendation.get("confidence"), 0.0),
        "lookbackHours": to_float(payload.get("trade_lookback_hours"), configured_lookback_hours),
        "notes": str(recommendation.get("notes", "")).strip(),
        "rows": rows,
        "available": True,
    }


def list_running_rotation_symbols() -> list[str]:
    try:
        out = subprocess.check_output(
            [
                "systemctl",
                "--user",
                "list-units",
                "codex-rotation-*.service",
                "--state=running",
                "--no-pager",
            ],
            cwd=REPO_ROOT,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    lane_symbols: list[str] = []
    for line in out.splitlines():
        match = ROTATION_UNIT_RE.search(line)
        if not match:
            continue
        slug = str(match.group(1)).strip().lower()
        if slug in {"selector", "status", "http"}:
            continue
        symbol = slug.upper()
        if symbol and symbol not in lane_symbols:
            lane_symbols.append(symbol)
    return lane_symbols


def load_rotation_lane_ports() -> dict[str, int]:
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    try:
        from trading.rotation_universe import build_lanes
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Rotation-Lanes konnten nicht geladen werden: {exc}") from exc

    lane_ports: dict[str, int] = {}
    for symbol, lane in build_lanes().items():
        ports = lane.get("ports")
        if not isinstance(ports, (list, tuple)) or not ports:
            continue
        try:
            lane_ports[str(symbol).upper()] = int(ports[0])
        except (TypeError, ValueError):
            continue
    return lane_ports


def fetch_local_json(url: str, timeout: float) -> Any:
    req = Request(url=url, method="GET")
    req.add_header("Accept", "application/json")
    last_exc: Exception | None = None
    parsed = urlparse(url)
    is_local = parsed.hostname in {"127.0.0.1", "localhost"}

    for attempt in range(max(1, ROTATION_STATUS_RETRIES if is_local else 1)):
        try:
            with urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except URLError as exc:
            last_exc = exc
            reason = getattr(exc, "reason", None)
            retryable = isinstance(reason, ConnectionRefusedError | TimeoutError | socket.timeout)
            if not retryable or attempt + 1 >= max(1, ROTATION_STATUS_RETRIES if is_local else 1):
                break
            if ROTATION_STATUS_RETRY_DELAY_SEC > 0.0:
                time.sleep(ROTATION_STATUS_RETRY_DELAY_SEC)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            break

    if isinstance(last_exc, URLError):
        reason = getattr(last_exc, "reason", None)
        if isinstance(reason, ConnectionRefusedError):
            raise RuntimeError("Lane startet gerade neu oder antwortet noch nicht.") from last_exc
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RuntimeError("Lane antwortet gerade zu langsam.") from last_exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Lokaler Status konnte nicht geladen werden.")


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    if value is None:
        return default
    return bool(value)


def _normalize_strategy_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _runtime_config_path(symbol: str) -> Path | None:
    name = str(symbol or "").strip().lower()
    if not name:
        return None
    return Path(ROTATION_RUNTIME_CONFIG_DIR) / f"{name}_runtime.yaml"


def _load_runtime_config_payload(symbol: str) -> dict[str, Any]:
    path = _runtime_config_path(symbol)
    if path is None:
        return {}

    try:
        stat = path.stat()
    except Exception:
        with _RUNTIME_CONFIG_CACHE_LOCK:
            _RUNTIME_CONFIG_CACHE.pop(str(symbol or "").strip().lower(), None)
        return {}

    mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    size = int(stat.st_size)
    key = str(symbol or "").strip().lower()

    with _RUNTIME_CONFIG_CACHE_LOCK:
        cached = _RUNTIME_CONFIG_CACHE.get(key)
        if cached is not None and cached[0] == mtime_ns and cached[1] == size:
            return cached[2]

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    with _RUNTIME_CONFIG_CACHE_LOCK:
        _RUNTIME_CONFIG_CACHE[key] = (mtime_ns, size, payload)

    return payload


def _runtime_metadata(symbol: str) -> dict[str, Any]:
    payload = _load_runtime_config_payload(symbol)
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    alpha = payload.get("alpha")
    if not isinstance(alpha, dict):
        alpha = {}
    risk = payload.get("risk")
    if not isinstance(risk, dict):
        risk = {}

    return {
        "strategy": _normalize_strategy_name(runtime.get("rotation_strategy_name")),
        "alphaType": _normalize_strategy_name(runtime.get("rotation_alpha_type"))
        or _normalize_strategy_name(alpha.get("type")),
        "profitRollExitEnabled": to_bool(risk.get("profit_roll_exit_enabled"), False),
        "profitRollArmUsdc": max(0.0, to_float(risk.get("profit_roll_arm_eur"), 0.0)),
        "profitRollRetraceUsdc": max(0.0, to_float(risk.get("profit_roll_retrace_eur"), 0.0)),
        "manualEntryExitOnly": to_bool(risk.get("manual_entry_exit_only"), False),
    }


def _runtime_strategy_metadata(symbol: str) -> tuple[str, str]:
    metadata = _runtime_metadata(symbol)
    return str(metadata.get("strategy", "")), str(metadata.get("alphaType", ""))


def _runtime_profit_roll_metadata(symbol: str) -> tuple[bool, float, float]:
    metadata = _runtime_metadata(symbol)
    return (
        bool(metadata.get("profitRollExitEnabled", False)),
        float(metadata.get("profitRollArmUsdc", 0.0)),
        float(metadata.get("profitRollRetraceUsdc", 0.0)),
    )


def _runtime_manual_entry_exit_only(symbol: str) -> bool:
    metadata = _runtime_metadata(symbol)
    return bool(metadata.get("manualEntryExitOnly", False))


def _display_rotation_strategy(snapshot_strategy: Any, runtime_strategy: Any) -> str:
    runtime_name = _normalize_strategy_name(runtime_strategy)
    if runtime_name in CORE_ROTATION_STRATEGIES:
        return runtime_name
    snapshot_name = _normalize_strategy_name(snapshot_strategy)
    if snapshot_name in CORE_ROTATION_STRATEGIES:
        return snapshot_name
    return ""


def load_rotation_points() -> dict[str, Any]:
    rotation_file, payload = load_rotation_payload()

    selected_symbols = {
        str(item).strip().upper()
        for item in payload.get("selected", [])
        if str(item).strip()
    }

    rows_source = payload.get("all_rows")
    if not isinstance(rows_source, list):
        rows_source = payload.get("rows")
    if not isinstance(rows_source, list):
        rows_source = []

    # Fallback for stale selector snapshots:
    # when the JSON only contains one/no row, derive symbols from running lanes
    # so the overview remains useful.
    if len(rows_source) <= 1:
        lane_symbols = list_running_rotation_symbols()
        if lane_symbols:
            existing_symbols = {
                str(item.get("symbol", "")).strip().upper()
                for item in rows_source
                if isinstance(item, dict)
            }
            synthetic_rows: list[dict[str, Any]] = []
            for symbol in lane_symbols:
                if symbol in existing_symbols:
                    continue
                synthetic_rows.append(
                    {
                        "symbol": symbol,
                        "market": f"{symbol}USDC",
                        "eligible": True,
                        "score": 0.0,
                        "gate_reason": "fallback_running_lane",
                        "slope_profile_match": True,
                        "macro_up_context": True,
                        "long_term_uptrend_context": True,
                        "structure_phase": "uptrend",
                    }
                )
            if synthetic_rows:
                rows_source.extend(synthetic_rows)

    rows: list[dict[str, Any]] = []
    for item in rows_source:
        if not isinstance(item, dict):
            continue

        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue

        gate_reason = str(item.get("gate_reason", "")).strip()
        macro_up_context = to_bool(item.get("macro_up_context", False))
        long_term_uptrend_context = to_bool(item.get("long_term_uptrend_context", False))
        structure_phase = str(item.get("structure_phase", "")).strip().lower()

        point1_ok = to_bool(item.get("slope_profile_match", False))
        # Punkt 2: Coin geht im laengeren Zeitraum grundsaetzlich bergauf.
        point2_ok = macro_up_context or long_term_uptrend_context
        point3_ok = gate_reason not in POINT3_GATE_REASONS
        if gate_reason == "fallback_running_lane":
            point1_ok = False
            point2_ok = False
            point3_ok = False

        rows.append(
            {
                "symbol": symbol,
                "market": str(item.get("market", "")).strip().upper(),
                "selected": symbol in selected_symbols,
                "eligible": to_bool(item.get("eligible", False)),
                "score": to_float(item.get("score"), 0.0),
                "gateReason": gate_reason,
                "point1Ok": point1_ok,
                "point2Ok": point2_ok,
                "point3Ok": point3_ok,
                "postDumpRecoveryPending": gate_reason == "post_dump_recovery_pending"
                or to_bool(item.get("post_dump_blocked", False)),
                "spreadBps": to_float(item.get("spread_bps"), 0.0),
                "topDepthNotional": to_float(item.get("top_depth_notional"), 0.0),
                "quoteVolume5m": to_float(item.get("quote_volume_5m"), 0.0),
                "quoteVolume60m": to_float(item.get("quote_volume_60m"), 0.0),
                "slopeProfileMatch": point1_ok,
                "macroUpContext": macro_up_context,
                "longTermUptrendContext": long_term_uptrend_context,
                "structurePhase": structure_phase or "-",
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[int, int, int, int, int, float, str]:
        lane_rank = 2 if bool(row["selected"]) else (1 if bool(row["eligible"]) else 0)
        ok_count = (
            int(bool(row["point1Ok"]))
            + int(bool(row["point2Ok"]))
            + int(bool(row["point3Ok"]))
        )
        missing_count = 3 - ok_count
        return (
            # Primary ordering: rows with all 3 OK first, then 2/1/0 OK.
            missing_count,
            0 if bool(row["point1Ok"]) else 1,
            0 if bool(row["point2Ok"]) else 1,
            0 if bool(row["point3Ok"]) else 1,
            -lane_rank,
            -float(row["score"]),
            str(row["symbol"]),
        )

    rows.sort(key=sort_key)

    summary = {
        "total": len(rows),
        "selected": sum(1 for row in rows if bool(row["selected"])),
        "eligible": sum(1 for row in rows if bool(row["eligible"])),
        "point1Ok": sum(1 for row in rows if bool(row["point1Ok"])),
        "point2Ok": sum(1 for row in rows if bool(row["point2Ok"])),
        "point3Ok": sum(1 for row in rows if bool(row["point3Ok"])),
        "postDumpRecoveryPending": sum(1 for row in rows if bool(row["postDumpRecoveryPending"])),
    }

    point1_symbols = sorted(row["symbol"] for row in rows if bool(row["point1Ok"]))
    point2_symbols = sorted(row["symbol"] for row in rows if bool(row["point2Ok"]))
    point3_symbols = sorted(row["symbol"] for row in rows if bool(row["point3Ok"]))
    points = {
        "point1": {
            "name": "1 Sweet",
            "label": "Sweet-Schraege",
            "hits": len(point1_symbols),
            "total": len(rows),
            "symbols": point1_symbols,
            "blockedCount": sum(1 for row in rows if str(row["gateReason"]) in POINT1_GATE_REASONS),
        },
        "point2": {
            "name": "2 Makro",
            "label": "Makrostruktur aufwaerts",
            "hits": len(point2_symbols),
            "total": len(rows),
            "symbols": point2_symbols,
            "blockedCount": sum(1 for row in rows if str(row["gateReason"]) in POINT2_GATE_REASONS),
        },
        "point3": {
            "name": "3 Co/Vol",
            "label": "Kostengate / Liquiditaet",
            "hits": len(point3_symbols),
            "total": len(rows),
            "symbols": point3_symbols,
            "blockedCount": sum(1 for row in rows if str(row["gateReason"]) in POINT3_GATE_REASONS),
        },
    }

    return {
        "ok": to_bool(payload.get("ok", False)),
        "generatedAt": str(payload.get("generated_at", "")).strip(),
        "sourceFile": rotation_file,
        "selected": sorted(selected_symbols),
        "summary": summary,
        "points": points,
        "rows": rows,
    }


def load_rotation_live() -> dict[str, Any]:
    global _ROTATION_LIVE_CACHE

    now = time.time()
    with _ROTATION_LIVE_CACHE_LOCK:
        cached = _ROTATION_LIVE_CACHE
    if cached is not None:
        ts, payload = cached
        if (now - ts) < ROTATION_LIVE_CACHE_TTL_SEC:
            return payload

    refresh_lock_owned = _ROTATION_LIVE_REFRESH_LOCK.acquire(blocking=False)
    if not refresh_lock_owned:
        # Another thread is rebuilding live payload. Serve a recent stale copy
        # immediately to avoid a thundering herd under slow rebuilds.
        with _ROTATION_LIVE_CACHE_LOCK:
            stale_cached = _ROTATION_LIVE_CACHE
        if stale_cached is not None:
            stale_ts, stale_payload = stale_cached
            if (now - stale_ts) < ROTATION_LIVE_STALE_FALLBACK_SEC:
                return stale_payload

        # No usable stale copy: wait for the in-flight rebuild once.
        with _ROTATION_LIVE_REFRESH_LOCK:
            pass
        with _ROTATION_LIVE_CACHE_LOCK:
            waited_cached = _ROTATION_LIVE_CACHE
        if waited_cached is not None:
            return waited_cached[1]

        # Rebuild path after a failed in-flight attempt that produced no cache.
        _ROTATION_LIVE_REFRESH_LOCK.acquire()
        refresh_lock_owned = True

    try:
        build_started_at = time.time()
        rotation_file, raw_payload = load_rotation_payload()
        strategy_summary = load_rotation_meta_summary()

        selected_symbols = {
            str(item).strip().upper()
            for item in raw_payload.get("selected", [])
            if str(item).strip()
        }
        selected_strategy_map_raw = raw_payload.get("selected_strategy_map")
        selected_strategy_map: dict[str, str] = {}
        if isinstance(selected_strategy_map_raw, dict):
            for key, value in selected_strategy_map_raw.items():
                symbol = str(key).strip().upper()
                strategy = str(value).strip().lower()
                if symbol and strategy:
                    selected_strategy_map[symbol] = strategy
        selected_since_raw = raw_payload.get("selected_since")
        selected_since: dict[str, str] = {}
        if isinstance(selected_since_raw, dict):
            for key, value in selected_since_raw.items():
                symbol = str(key).strip().upper()
                if symbol:
                    selected_since[symbol] = str(value).strip()

        rows_source = raw_payload.get("all_rows")
        if not isinstance(rows_source, list):
            rows_source = raw_payload.get("rows")
        if not isinstance(rows_source, list):
            rows_source = []

        row_meta_by_symbol: dict[str, dict[str, Any]] = {}
        running_symbols: list[str] = []
        for symbol in list_running_rotation_symbols():
            if symbol not in running_symbols:
                running_symbols.append(symbol)

        running_symbol_set = set(running_symbols)
        symbols: list[str] = []
        watch_symbols_raw = raw_payload.get("watch_symbols")
        watch_symbols_present = isinstance(watch_symbols_raw, list)
        if running_symbols:
            symbols.extend(running_symbols)
        elif watch_symbols_present:
            for item in watch_symbols_raw:
                symbol = str(item).strip().upper()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)

        for symbol in sorted(selected_symbols):
            if symbol not in symbols:
                symbols.append(symbol)

        for item in rows_source:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            row_meta_by_symbol[symbol] = item
            # When running lanes are available, the live view should stay scoped to
            # those processes instead of probing stale selector/watch candidates.
            if not running_symbols and not watch_symbols_present and symbol not in symbols:
                symbols.append(symbol)

        lane_ports = load_rotation_lane_ports()
        workers = max(1, min(MAX_WORKERS, len(symbols) or 1))

        def init_row(
            *,
            symbol: str,
            market: str,
            selected: bool,
            running: bool,
            eligible: bool,
            score: float,
            gate_reason: str,
            selected_since_iso: str,
            selector_pos_pct: float,
            selector_pos_7d_pct: float,
            selector_pos_pct_raw72: float,
            selector_near_pos_pct: float,
            selector_mid_pos_pct: float,
            selector_long_pos_pct: float,
            runtime_meta: dict[str, Any],
        ) -> dict[str, Any]:
            display_strategy = _display_rotation_strategy(
                selected_strategy_map.get(symbol, ""),
                runtime_meta.get("strategy", ""),
            )
            return {
                "symbol": symbol,
                "market": market,
                "selected": selected,
                "running": running,
                "strategy": display_strategy,
                "alphaType": str(runtime_meta.get("alphaType", "")),
                "profitRollExitEnabled": bool(runtime_meta.get("profitRollExitEnabled", False)),
                "profitRollArmUsdc": float(runtime_meta.get("profitRollArmUsdc", 0.0)),
                "profitRollRetraceUsdc": float(runtime_meta.get("profitRollRetraceUsdc", 0.0)),
                "manualEntryExitOnly": bool(runtime_meta.get("manualEntryExitOnly", False)),
                "eligible": bool(eligible),
                "score": float(score),
                "gateReason": gate_reason,
                "selectedSince": selected_since_iso,
                "entryOpenedAt": "",
                "statusOk": False,
                "stale": False,
                "currentlyTrading": False,
                "positionOpen": False,
                "tradeReady": False,
                "tradingEnabled": False,
                "positionQty": 0.0,
                "entryPrice": 0.0,
                "exitPrice": 0.0,
                "entryValueUsdc": 0.0,
                "exitValueUsdc": 0.0,
                "realizedPnlUsdc": 0.0,
                "unrealizedPnlUsdc": 0.0,
                "totalPnlUsdc": 0.0,
                "rangePositionPct": 0.0,
                "rangePositionDeltaPct": 0.0,
                "rangeDirection": "flat",
                "rangePositionPctHour": None,
                "rangePositionDeltaPctHour": None,
                "rangeDirectionHour": "flat",
                "rangePositionPctDay": None,
                "rangePositionDeltaPctDay": None,
                "rangeDirectionDay": "flat",
                "selectorPosPct": selector_pos_pct if math.isfinite(selector_pos_pct) else None,
                "selectorPos7dPct": (
                    selector_pos_7d_pct if math.isfinite(selector_pos_7d_pct) else None
                ),
                "selectorPosPctRaw72": (
                    selector_pos_pct_raw72 if math.isfinite(selector_pos_pct_raw72) else None
                ),
                "selectorNearPosPct": selector_near_pos_pct if math.isfinite(selector_near_pos_pct) else None,
                "selectorMidPosPct": selector_mid_pos_pct if math.isfinite(selector_mid_pos_pct) else None,
                "selectorLongPosPct": (
                    selector_long_pos_pct if math.isfinite(selector_long_pos_pct) else None
                ),
                "entryTrendPct10m": None,
                "entryTrendDeltaPct10m": None,
                "entryTrendDirection10m": "flat",
                "entryTrendPct1h": None,
                "entryTrendDeltaPct1h": None,
                "entryTrendDirection1h": "flat",
                "entryTrendPct5h": None,
                "entryTrendSeries10h": [],
                "entryTrendWindow10hMin": 600,
                "entryTrendSeriesPoints10h": 0,
                "entryTrendPct24h": None,
                "entryTrendDeltaPct24h": None,
                "entryTrendDirection24h": "flat",
                "rangeSignalTs": "",
                "rangePrice": 0.0,
                "rangeSmoothingWindowMin": 10,
                "rangeSmoothingWindowHourMin": 60,
                "rangeSmoothingWindowDayMin": 1440,
                "corridorLowPrice": 0.0,
                "corridorHighPrice": 0.0,
                "corridorStagedExitStepPct": 10.0,
                "corridorStagedHysteresisPct": 0.75,
                "corridorStagedNoBuyAbovePct": 50.0,
                "corridorStagedProfitTargetEnabled": False,
                "corridorStagedProfitTargetBasePct": 0.0,
                "corridorStagedProfitTargetMinPct": 0.0,
                "corridorStagedProfitTargetMaxPct": 100.0,
                "corridorStagedProfitTargetMult10": 1.25,
                "corridorStagedProfitTargetMult20": 1.10,
                "corridorStagedProfitTargetMult30": 1.00,
                "corridorStagedProfitTargetMult40": 0.90,
                "corridorStagedProfitTargetMult50": 0.80,
                "corridorExitArmed": None,
                "corridorExitPeakPct": None,
                "corridorStagedModeEnabled": False,
                "exitNeedUsdc": None,
                "exitBonusUsdc": None,
                "exitRollArmed": False,
                "exitEntryStagePct": None,
                "exitTargetPct": None,
                "exitTargetPrice": None,
                "exitMode": "unavailable",
                "openOrdersCount": 0,
                "freshnessSec": 0.0,
                "updatedAt": "",
                "statusError": "",
                "controlPort": lane_ports.get(symbol),
            }

        def collect(symbol: str) -> dict[str, Any]:
            meta = row_meta_by_symbol.get(symbol, {})
            market = str(meta.get("market", "")).strip().upper() or f"{symbol}USDC"
            selected = symbol in selected_symbols
            running = symbol in running_symbol_set
            eligible = to_bool(meta.get("eligible", False))
            score = to_float(meta.get("score"), 0.0)
            gate_reason = str(meta.get("gate_reason", "")).strip()
            selector_pos_pct = to_float(meta.get("pos_pct"), float("nan"))
            selector_pos_7d_pct = to_float(
                meta.get(
                    "pos_7d_nocrash_pct",
                    meta.get("pos_7d_pct", meta.get("pos_48h_pct", meta.get("pos_36h_pct"))),
                ),
                float("nan"),
            )
            selector_pos_pct_raw72 = to_float(meta.get("pos_pct_raw72"), float("nan"))
            selector_near_pos_pct = to_float(meta.get("corridor_near_pos_pct"), float("nan"))
            selector_mid_pos_pct = to_float(meta.get("corridor_mid_pos_pct"), float("nan"))
            selector_long_pos_pct = to_float(meta.get("corridor_long_pos_pct"), float("nan"))
            selected_since_iso = selected_since.get(symbol, "")
            runtime_meta = _runtime_metadata(symbol)

            row = init_row(
                symbol=symbol,
                market=market,
                selected=selected,
                running=running,
                eligible=eligible,
                score=score,
                gate_reason=gate_reason,
                selected_since_iso=selected_since_iso,
                selector_pos_pct=selector_pos_pct,
                selector_pos_7d_pct=selector_pos_7d_pct,
                selector_pos_pct_raw72=selector_pos_pct_raw72,
                selector_near_pos_pct=selector_near_pos_pct,
                selector_mid_pos_pct=selector_mid_pos_pct,
                selector_long_pos_pct=selector_long_pos_pct,
                runtime_meta=runtime_meta,
            )

            port = lane_ports.get(symbol)
            if port is None:
                row["statusError"] = "Keine Lane-Konfiguration gefunden."
                row["state"] = "missing"
                return row

            try:
                payload = fetch_local_json(
                    f"http://127.0.0.1:{port}/status",
                    timeout=ROTATION_STATUS_TIMEOUT_SEC,
                )
            except Exception as exc:  # noqa: BLE001
                row["statusError"] = str(exc)
                row["state"] = "down"
                return row

            data = payload.get("data") if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                data = {}
            core = data.get("core") if isinstance(data.get("core"), dict) else {}
            exec_data = data.get("exec") if isinstance(data.get("exec"), dict) else {}
            aggregate = payload.get("aggregate") if isinstance(payload, dict) else {}
            if not isinstance(aggregate, dict):
                aggregate = {}
            staleness = payload.get("staleness_sec_by_process") if isinstance(payload, dict) else {}
            if not isinstance(staleness, dict):
                staleness = {}

            stale_warn_sec = to_float(payload.get("stale_warn_sec"), 12.0)
            freshness_values = [
                to_float(staleness.get("core"), 0.0),
                to_float(staleness.get("exec"), 0.0),
                to_float(staleness.get("md"), 0.0),
            ]
            freshness_sec = max(freshness_values) if freshness_values else 0.0
            position_qty = abs(to_float(core.get("position_btc"), 0.0))
            entry_price = to_float(core.get("avg_entry_price"), 0.0)
            exit_price = to_float(core.get("mark_price"), 0.0)
            entry_value = position_qty * entry_price if position_qty > EPS and entry_price > EPS else 0.0
            exit_value = to_float(core.get("position_value_eur"), 0.0)
            if exit_value <= EPS and position_qty > EPS and exit_price > EPS:
                exit_value = position_qty * exit_price
            open_orders_count = to_int(
                aggregate.get("open_orders_count", exec_data.get("open_orders_count")),
                0,
            )
            realized_pnl = to_float(core.get("realized_pnl_eur"), 0.0)
            unrealized_pnl = to_float(core.get("unrealized_pnl_eur"), 0.0)
            campaign_metrics = None
            entry_opened_at = ""
            journal_path: Path | None = None
            journal_rows: list[dict[str, Any]] | None = None
            journal_max_lines = 0

            if selected and selected_since_iso:
                journal_path = rotation_journal_path(symbol)
                if journal_path is not None:
                    journal_max_lines = max(journal_max_lines, ROTATION_LIVE_JOURNAL_LINES_SELECTED)
                    journal_rows = tail_json_rows(journal_path, max_lines=journal_max_lines)
                    campaign_metrics = reconstruct_live_campaign_metrics(
                        journal_path,
                        selected_since_iso=selected_since_iso,
                        current_position_qty=position_qty,
                        mark_price=exit_price,
                        rows=journal_rows,
                    )
            if campaign_metrics is not None:
                position_qty = to_float(campaign_metrics.get("positionQty"), position_qty)
                entry_price = to_float(campaign_metrics.get("entryPrice"), entry_price)
                entry_value = to_float(campaign_metrics.get("entryValueUsdc"), entry_value)
                exit_value = to_float(campaign_metrics.get("exitValueUsdc"), exit_value)
                realized_pnl = to_float(campaign_metrics.get("realizedPnlUsdc"), realized_pnl)
                unrealized_pnl = to_float(campaign_metrics.get("unrealizedPnlUsdc"), unrealized_pnl)
                entry_opened_at = str(campaign_metrics.get("entryOpenedAt") or "").strip()

            dust_position = (
                open_orders_count <= 0
                and max(entry_value, exit_value) < ROTATION_LIVE_DUST_POSITION_USDC
            )
            if dust_position:
                position_qty = 0.0
                entry_price = 0.0
                exit_price = 0.0
                entry_value = 0.0
                exit_value = 0.0
                unrealized_pnl = 0.0

            position_open = position_qty > EPS or entry_value > EPS or exit_value > EPS
            currently_trading = position_open or open_orders_count > 0

            if currently_trading and journal_path is None:
                journal_path = rotation_journal_path(symbol)
            if currently_trading and journal_path is not None and journal_rows is None:
                journal_max_lines = max(journal_max_lines, ROTATION_LIVE_JOURNAL_LINES_ACTIVE)
                journal_rows = tail_json_rows(journal_path, max_lines=journal_max_lines)

            if not entry_opened_at and currently_trading and journal_path is not None:
                entry_opened_at = infer_live_entry_opened_at(
                    journal_path,
                    selected_since_iso=selected_since_iso,
                    current_position_qty=position_qty,
                    rows=journal_rows,
                )
            if not entry_opened_at and currently_trading:
                selected_since_dt = parse_iso_datetime(selected_since_iso)
                if selected_since_dt is not None:
                    entry_opened_at = selected_since_dt.isoformat().replace("+00:00", "Z")

            trading_enabled = to_bool(
                aggregate.get("trading_enabled", core.get("trading_enabled", False)),
                False,
            )
            corridor_state = None
            if currently_trading and journal_path is not None:
                corridor_state = extract_recent_corridor_state(
                    journal_path,
                    entry_price=entry_price,
                    rows=journal_rows,
                )

            row.update(
                {
                    "statusOk": True,
                    "stale": freshness_sec >= stale_warn_sec,
                    "currentlyTrading": currently_trading,
                    "positionOpen": position_open,
                    "tradeReady": to_bool(payload.get("overview_trade_ready", False)),
                    "tradingEnabled": trading_enabled,
                    "positionQty": position_qty,
                    "entryPrice": entry_price,
                    "exitPrice": exit_price,
                    "entryValueUsdc": entry_value,
                    "exitValueUsdc": exit_value,
                    "dustPositionHidden": dust_position,
                    "realizedPnlUsdc": realized_pnl,
                    "unrealizedPnlUsdc": unrealized_pnl,
                    "totalPnlUsdc": realized_pnl + unrealized_pnl,
                    "openOrdersCount": open_orders_count,
                    "freshnessSec": freshness_sec,
                    "updatedAt": str(payload.get("updated_at", "")).strip(),
                    "pnlSource": "campaign" if campaign_metrics is not None else "status",
                    "entryOpenedAt": entry_opened_at,
                }
            )
            if isinstance(corridor_state, dict):
                row.update(corridor_state)
            row.update(compute_live_exit_tracker(row))

            if currently_trading:
                row["state"] = "open"
            elif row["stale"]:
                row["state"] = "stale"
            elif selected:
                row["state"] = "selected"
            else:
                row["state"] = "watch"
            return row

        rows: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(collect, symbol): symbol for symbol in symbols}
            for future in concurrent.futures.as_completed(future_map):
                symbol = future_map[future]
                try:
                    rows.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    runtime_meta = _runtime_metadata(symbol)
                    row = init_row(
                        symbol=symbol,
                        market=f"{symbol}USDC",
                        selected=symbol in selected_symbols,
                        running=symbol in running_symbol_set,
                        eligible=False,
                        score=0.0,
                        gate_reason="",
                        selected_since_iso=selected_since.get(symbol, ""),
                        selector_pos_pct=float("nan"),
                        selector_pos_7d_pct=float("nan"),
                        selector_pos_pct_raw72=float("nan"),
                        selector_near_pos_pct=float("nan"),
                        selector_mid_pos_pct=float("nan"),
                        selector_long_pos_pct=float("nan"),
                        runtime_meta=runtime_meta,
                    )
                    row.update(
                        {
                            "tradeReady": False,
                            "statusError": str(exc),
                            "controlPort": lane_ports.get(symbol),
                            "state": "down",
                        }
                    )
                    rows.append(row)

        def sort_key(row: dict[str, Any]) -> tuple[int, str, int, int, int, int, int, str]:
            symbol = str(row.get("symbol", ""))
            in_trade = bool(row.get("currentlyTrading")) or bool(row.get("positionOpen")) or int(
                row.get("openOrdersCount", 0)
            ) > 0
            return (
                0 if in_trade else 1,
                symbol if in_trade else "",
                0 if bool(row.get("selected")) else 1,
                0 if bool(row.get("running")) else 1,
                0 if bool(row.get("statusOk")) else 1,
                0 if not bool(row.get("stale")) else 1,
                -int(bool(row.get("eligible"))),
                symbol,
            )

        rows.sort(key=sort_key)

        summary = {
            "total": len(rows),
            "selected": sum(1 for row in rows if bool(row.get("selected"))),
            "running": sum(1 for row in rows if bool(row.get("running"))),
            "eligible": sum(1 for row in rows if bool(row.get("eligible"))),
            "open": sum(1 for row in rows if bool(row.get("currentlyTrading"))),
            "withPosition": sum(1 for row in rows if bool(row.get("positionOpen"))),
            "withOpenOrders": sum(1 for row in rows if int(row.get("openOrdersCount", 0)) > 0),
            "tradeReady": sum(1 for row in rows if bool(row.get("tradeReady"))),
            "stale": sum(1 for row in rows if bool(row.get("stale"))),
            "down": sum(1 for row in rows if not bool(row.get("statusOk"))),
        }

        live_payload = {
            "ok": True,
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sourceFile": rotation_file,
            "selected": sorted(selected_symbols),
            "watchSymbols": symbols,
            "summary": summary,
            "strategySummary": strategy_summary,
            "rows": rows,
        }
        with _ROTATION_LIVE_CACHE_LOCK:
            # Cache age should reflect when rebuild started, not when it finished.
            # Otherwise long rebuilds effectively extend staleness by build duration.
            _ROTATION_LIVE_CACHE = (build_started_at, live_payload)
        return live_payload
    finally:
        if refresh_lock_owned:
            _ROTATION_LIVE_REFRESH_LOCK.release()


def normalize_trade(symbol: str, item: dict[str, Any]) -> dict[str, Any] | None:
    base_coin = symbol.removesuffix("USDC")
    quote_coin = symbol[len(base_coin) :] if symbol.startswith(base_coin) else ""
    side = "BUY" if bool(item.get("isBuyer")) else "SELL"

    qty = to_float(item.get("qty"), 0.0)
    price = to_float(item.get("price"), 0.0)
    quote_qty = to_float(item.get("quoteQty"), qty * price)
    time_ms = to_int(item.get("time"), 0)
    order_id_raw = item.get("orderId")
    trade_id_raw = item.get("id")
    order_id = str(order_id_raw).strip() if order_id_raw is not None else ""
    trade_id = str(trade_id_raw).strip() if trade_id_raw is not None else ""

    commission = to_float(item.get("commission"), 0.0)
    commission_asset = str(item.get("commissionAsset", "")).upper()

    if qty <= EPS or time_ms <= 0:
        return None

    fee_usdc = 0.0
    if commission > EPS:
        if commission_asset == quote_coin:
            fee_usdc = commission
        elif commission_asset == base_coin:
            fee_usdc = commission * price
    fee_in_quote = commission_asset == quote_coin

    if side == "BUY":
        effective_qty = qty
        if commission_asset == base_coin:
            effective_qty = max(qty - commission, 0.0)
        # If fee is charged in base asset, quantity adjustment already captures cost.
        gross_usdc = quote_qty + (fee_usdc if fee_in_quote else 0.0)
    else:
        effective_qty = qty
        if commission_asset == base_coin:
            effective_qty = qty + commission
        # If fee is charged in base asset, quantity adjustment already captures cost.
        gross_usdc = quote_qty - (fee_usdc if fee_in_quote else 0.0)

    if effective_qty <= EPS:
        return None

    return {
        "symbol": symbol,
        "coin": base_coin,
        "side": side,
        "orderId": order_id,
        "tradeId": trade_id,
        "timeMs": time_ms,
        "timeIso": iso_from_ms(time_ms),
        "quantity": effective_qty,
        "grossUsdc": gross_usdc,
        "price": price,
        "quoteQty": quote_qty,
        "feeUsdc": fee_usdc,
        "commissionAsset": commission_asset,
    }


def aggregate_order_fills(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    fallback_counter = 0

    for trade in trades:
        symbol = str(trade["symbol"])
        side = str(trade["side"])
        order_id = str(trade.get("orderId", "")).strip()
        trade_id = str(trade.get("tradeId", "")).strip()

        group_id = order_id
        if not group_id:
            if trade_id:
                group_id = f"trade:{trade_id}"
            else:
                group_id = f"fallback:{symbol}:{side}:{fallback_counter}"
                fallback_counter += 1

        key = (symbol, side, group_id)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "symbol": symbol,
                "coin": str(trade["coin"]),
                "side": side,
                "orderId": order_id,
                "timeMinMs": int(trade["timeMs"]),
                "timeMaxMs": int(trade["timeMs"]),
                "quantity": float(trade["quantity"]),
                "grossUsdc": float(trade["grossUsdc"]),
            }
            continue

        t_ms = int(trade["timeMs"])
        existing["timeMinMs"] = min(int(existing["timeMinMs"]), t_ms)
        existing["timeMaxMs"] = max(int(existing["timeMaxMs"]), t_ms)
        existing["quantity"] = float(existing["quantity"]) + float(trade["quantity"])
        existing["grossUsdc"] = float(existing["grossUsdc"]) + float(trade["grossUsdc"])

    aggregated: list[dict[str, Any]] = []
    for item in grouped.values():
        side = str(item["side"])
        chosen_ms = int(item["timeMinMs"]) if side == "BUY" else int(item["timeMaxMs"])
        aggregated.append(
            {
                "symbol": str(item["symbol"]),
                "coin": str(item["coin"]),
                "side": side,
                "orderId": str(item.get("orderId", "")),
                "timeMs": chosen_ms,
                "timeIso": iso_from_ms(chosen_ms),
                "quantity": float(item["quantity"]),
                "grossUsdc": float(item["grossUsdc"]),
            }
        )

    aggregated.sort(key=lambda t: (int(t["timeMs"]), str(t["symbol"]), str(t["side"])))
    return aggregated


def build_report(
    from_iso: str,
    to_iso: str,
    normalized_trades: list[dict[str, Any]],
    *,
    settle_by_sell_time: bool = True,
) -> dict[str, Any]:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    total_buy_all = 0.0
    total_sell_all = 0.0
    trade_count = 0
    for trade in normalized_trades:
        symbol = str(trade["symbol"])
        by_symbol.setdefault(symbol, []).append(trade)
        side = str(trade.get("side", "")).upper()
        gross = float(trade.get("grossUsdc", 0.0) or 0.0)
        if gross > EPS:
            if side == "BUY":
                total_buy_all += gross
            elif side == "SELL":
                total_sell_all += gross
        trade_count += 1

    bundles: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []

    for symbol, trades in by_symbol.items():
        trades.sort(key=lambda t: int(t["timeMs"]))
        buy_lots: list[dict[str, Any]] = []

        for trade in trades:
            qty = float(trade["quantity"])
            gross = float(trade["grossUsdc"])
            side = str(trade["side"])

            if qty <= EPS:
                continue

            if side == "BUY":
                unit_cost = gross / qty
                buy_lots.append(
                    {
                        "remainingQty": qty,
                        "unitCost": unit_cost,
                        "buyTime": str(trade["timeIso"]),
                    }
                )
                continue

            remaining = qty
            sell_unit = gross / qty
            sell_time = str(trade["timeIso"])

            while remaining > EPS and buy_lots:
                lot = buy_lots[0]
                lot_qty = float(lot["remainingQty"])
                matched_qty = lot_qty if lot_qty < remaining else remaining

                buy_gross = matched_qty * float(lot["unitCost"])
                sell_gross = matched_qty * sell_unit
                proceeds = sell_gross - buy_gross

                bundles.append(
                    {
                        "symbol": symbol,
                        "quantity": round(matched_qty, 8),
                        "buyTime": str(lot["buyTime"]),
                        "sellTime": sell_time,
                        "buyGrossUsdc": round(buy_gross, 8),
                        "sellGrossUsdc": round(sell_gross, 8),
                        "proceedsUsdc": round(proceeds, 8),
                    }
                )
                trade_rows.append(
                    {
                        "symbol": symbol,
                        "quantity": round(matched_qty, 8),
                        "buyTime": str(lot["buyTime"]),
                        "sellTime": sell_time,
                        "buyPrice": round(float(lot["unitCost"]), 12),
                        "sellPrice": round(float(sell_unit), 12),
                        "buyGrossUsdc": round(buy_gross, 8),
                        "sellGrossUsdc": round(sell_gross, 8),
                        "proceedsUsdc": round(proceeds, 8),
                        "closed": True,
                    }
                )

                remaining -= matched_qty
                lot["remainingQty"] = lot_qty - matched_qty
                if float(lot["remainingQty"]) <= EPS:
                    buy_lots.pop(0)

        for lot in buy_lots:
            remaining_qty = float(lot.get("remainingQty", 0.0) or 0.0)
            unit_cost = float(lot.get("unitCost", 0.0) or 0.0)
            if remaining_qty <= EPS or unit_cost <= EPS:
                continue
            trade_rows.append(
                {
                    "symbol": symbol,
                    "quantity": round(remaining_qty, 8),
                    "buyTime": str(lot.get("buyTime") or ""),
                    "sellTime": "",
                    "buyPrice": round(unit_cost, 12),
                    "sellPrice": 0.0,
                    "buyGrossUsdc": round(remaining_qty * unit_cost, 8),
                    "sellGrossUsdc": 0.0,
                    "proceedsUsdc": None,
                    "closed": False,
                }
            )

    bundles.sort(key=lambda b: (b["sellTime"], b["symbol"]))
    trade_rows.sort(key=lambda row: (str(row.get("buyTime") or ""), str(row.get("symbol") or "")))

    symbol_map: dict[str, dict[str, Any]] = {}
    for bundle in bundles:
        symbol = str(bundle["symbol"])
        entry = symbol_map.setdefault(
            symbol,
            {
                "symbol": symbol,
                "bundleCount": 0,
                "buyGrossUsdc": 0.0,
                "sellGrossUsdc": 0.0,
                "proceedsUsdc": 0.0,
            },
        )

        entry["bundleCount"] += 1
        entry["buyGrossUsdc"] += float(bundle["buyGrossUsdc"])
        entry["sellGrossUsdc"] += float(bundle["sellGrossUsdc"])
        entry["proceedsUsdc"] += float(bundle["proceedsUsdc"])

    symbol_summaries = [
        {
            "symbol": item["symbol"],
            "bundleCount": int(item["bundleCount"]),
            "buyGrossUsdc": round(float(item["buyGrossUsdc"]), 8),
            "sellGrossUsdc": round(float(item["sellGrossUsdc"]), 8),
            "proceedsUsdc": round(float(item["proceedsUsdc"]), 8),
        }
        for item in sorted(symbol_map.values(), key=lambda x: str(x["symbol"]))
    ]

    report_rows = trade_rows
    if settle_by_sell_time:
        from_dt = parse_iso_utc(from_iso)
        to_dt = parse_iso_utc(to_iso)
        filtered_rows: list[dict[str, Any]] = []
        for row in trade_rows:
            if not bool(row.get("closed")):
                continue
            sell_time = str(row.get("sellTime") or "").strip()
            if not sell_time:
                continue
            try:
                sell_dt = parse_iso_utc(sell_time)
            except Exception:
                continue
            if from_dt <= sell_dt < to_dt:
                filtered_rows.append(row)
        report_rows = filtered_rows

    if settle_by_sell_time:
        bundles = [
            {
                "symbol": str(row.get("symbol") or ""),
                "quantity": round(float(row.get("quantity") or 0.0), 8),
                "buyTime": str(row.get("buyTime") or ""),
                "sellTime": str(row.get("sellTime") or ""),
                "buyGrossUsdc": round(float(row.get("buyGrossUsdc") or 0.0), 8),
                "sellGrossUsdc": round(float(row.get("sellGrossUsdc") or 0.0), 8),
                "proceedsUsdc": round(float(row.get("proceedsUsdc") or 0.0), 8),
            }
            for row in report_rows
        ]
        bundles.sort(key=lambda b: (b["sellTime"], b["symbol"]))

        symbol_map = {}
        for row in report_rows:
            symbol = str(row.get("symbol") or "")
            entry = symbol_map.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "bundleCount": 0,
                    "buyGrossUsdc": 0.0,
                    "sellGrossUsdc": 0.0,
                    "proceedsUsdc": 0.0,
                },
            )
            entry["bundleCount"] += 1
            entry["buyGrossUsdc"] += float(row.get("buyGrossUsdc") or 0.0)
            entry["sellGrossUsdc"] += float(row.get("sellGrossUsdc") or 0.0)
            entry["proceedsUsdc"] += float(row.get("proceedsUsdc") or 0.0)

        symbol_summaries = [
            {
                "symbol": item["symbol"],
                "bundleCount": int(item["bundleCount"]),
                "buyGrossUsdc": round(float(item["buyGrossUsdc"]), 8),
                "sellGrossUsdc": round(float(item["sellGrossUsdc"]), 8),
                "proceedsUsdc": round(float(item["proceedsUsdc"]), 8),
            }
            for item in sorted(symbol_map.values(), key=lambda x: str(x["symbol"]))
        ]

    total_buy = sum(float(x["buyGrossUsdc"]) for x in symbol_summaries)
    total_sell = sum(float(x["sellGrossUsdc"]) for x in symbol_summaries)
    total_proceeds = sum(float(x["proceedsUsdc"]) for x in symbol_summaries)

    if settle_by_sell_time:
        total_buy_all = round(total_buy, 8)
        total_sell_all = round(total_sell, 8)
    else:
        total_buy_all = round(total_buy_all, 8)
        total_sell_all = round(total_sell_all, 8)
    matched_buy = round(total_buy, 8)
    matched_sell = round(total_sell, 8)
    matched_proceeds = round(total_proceeds, 8)
    net_cashflow = round(total_sell_all - total_buy_all, 8)

    day_summary = {
        "bundleCount": len(bundles),
        "tradeRowCount": len(report_rows),
        "closedTradeRowCount": len(report_rows) if settle_by_sell_time else sum(
            1 for row in report_rows if bool(row.get("closed"))
        ),
        "openTradeRowCount": 0 if settle_by_sell_time else sum(
            1 for row in report_rows if not bool(row.get("closed"))
        ),
        "symbolCount": len(symbol_summaries),
        "tradeCount": (len(report_rows) * 2) if settle_by_sell_time else trade_count,
        # Top-level totals should reflect all fills in the requested window.
        "buyGrossUsdc": total_buy_all,
        "sellGrossUsdc": total_sell_all,
        # Keep realized PnL semantics based on matched buy/sell bundles.
        "proceedsUsdc": matched_proceeds,
        "matchedBuyGrossUsdc": matched_buy,
        "matchedSellGrossUsdc": matched_sell,
        "matchedProceedsUsdc": matched_proceeds,
        "netCashflowUsdc": net_cashflow,
    }

    return {
        "fromIso": from_iso,
        "toIso": to_iso,
        "tradeRows": report_rows,
        "bundles": bundles,
        "symbolSummaries": symbol_summaries,
        "daySummary": day_summary,
    }


def collect_trades_local(from_iso: str, to_iso: str, symbols_filter: list[str] | None = None) -> dict[str, Any]:
    from_dt = parse_iso_utc(from_iso)
    to_dt = parse_iso_utc(to_iso)

    if to_dt <= from_dt:
        raise ValueError("Ungueltiges Zeitfenster: toIso muss groesser als fromIso sein.")
    if (to_dt - from_dt).days > 31:
        raise ValueError("Zeitraum zu gross. Bitte maximal 31 Tage pro Anfrage.")

    cache_key = f"local|{from_dt.isoformat()}|{to_dt.isoformat()}"
    if symbols_filter:
        cache_key = f"{cache_key}|{'-'.join(sorted(symbols_filter))}"
    now = time.time()
    cached = _REPORT_CACHE.get(cache_key)
    if cached is not None:
        ts, payload = cached
        if (now - ts) < REPORT_CACHE_TTL_SEC:
            return payload

    files = iter_local_journal_files(symbols_filter)
    if not files:
        if symbols_filter:
            raise ValueError("Keine passende lokale Journal-Datei fuer das angeforderte Symbol gefunden.")
        raise ValueError("Keine lokalen Journal-Dateien gefunden.")

    normalized_trades: list[dict[str, Any]] = []
    dedup: set[tuple[str, str, str, float, float, str]] = set()
    for path in files:
        symbol = symbol_from_journal_path(path)
        if not symbol:
            continue
        coin = symbol.removesuffix("USDC")
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if '"event_type":"fill"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if str(event.get("event_type", "")) != "fill":
                        continue

                    payload = event.get("payload")
                    if not _is_reportable_fill_payload(payload):
                        continue

                    side_raw = str(payload.get("side", "")).strip().lower()
                    if side_raw not in {"buy", "sell"}:
                        continue
                    side = "BUY" if side_raw == "buy" else "SELL"

                    ts_raw = str(payload.get("ts", "")).strip() or str(event.get("ts", "")).strip()
                    if not ts_raw:
                        continue
                    try:
                        ts = parse_iso_utc(ts_raw)
                    except ValueError:
                        continue
                    if not (ts < to_dt):
                        continue

                    qty_raw = to_float(payload.get("qty_btc"), 0.0)
                    price = to_float(payload.get("price"), 0.0)
                    fee_usdc = max(0.0, to_float(payload.get("fee_eur"), 0.0))
                    if qty_raw <= EPS or price <= EPS:
                        continue

                    order_id = str(payload.get("order_id", "")).strip()
                    dedup_key = (symbol, order_id, ts.isoformat(), qty_raw, price, side)
                    if dedup_key in dedup:
                        continue
                    dedup.add(dedup_key)

                    quote_qty = qty_raw * price
                    qty_effective = qty_raw
                    if side == "BUY":
                        # Journal fills do not carry commission asset metadata.
                        # For realistic account-level post-trade pricing we model
                        # the common Binance spot case: buy fee deducted in base.
                        fee_qty = (fee_usdc / price) if fee_usdc > EPS else 0.0
                        qty_effective = max(0.0, qty_raw - fee_qty)
                        gross_usdc = quote_qty
                    else:
                        # For sells we model quote-side fee, so gross represents
                        # the net quote amount credited to the account.
                        gross_usdc = max(0.0, quote_qty - fee_usdc)
                    if qty_effective <= EPS or gross_usdc <= EPS:
                        continue

                    time_ms = int(ts.timestamp() * 1000)
                    normalized_trades.append(
                        {
                            "symbol": symbol,
                            "coin": coin,
                            "side": side,
                            "orderId": order_id,
                            "tradeId": f"{order_id}:{time_ms}:{qty_raw}",
                            "timeMs": time_ms,
                            "timeIso": ts.isoformat().replace("+00:00", "Z"),
                            "quantity": qty_effective,
                            "grossUsdc": gross_usdc,
                        }
                    )
        except OSError:
            continue

    merged_trades = aggregate_order_fills(normalized_trades)
    report = build_report(
        from_iso=from_dt.isoformat().replace("+00:00", "Z"),
        to_iso=to_dt.isoformat().replace("+00:00", "Z"),
        normalized_trades=merged_trades,
    )
    report["source"] = "local_journal"
    report["scannedJournalFiles"] = len(files)
    report["fillEvents"] = len(normalized_trades)
    _REPORT_CACHE[cache_key] = (time.time(), report)
    return report


def collect_trades_mirror(from_iso: str, to_iso: str, symbols_filter: list[str] | None = None) -> dict[str, Any]:
    from_dt = parse_iso_utc(from_iso)
    to_dt = parse_iso_utc(to_iso)
    cache_key = f"mirror|{from_dt.isoformat()}|{to_dt.isoformat()}"
    if symbols_filter:
        cache_key = f"{cache_key}|{'-'.join(sorted(symbols_filter))}"
    now = time.time()
    cached = _REPORT_CACHE.get(cache_key)
    if cached is not None:
        ts, payload = cached
        if (now - ts) < REPORT_CACHE_TTL_SEC:
            return payload
    report = binance_trade_mirror.collect_trades_mirror(from_iso, to_iso, symbols_filter)
    _REPORT_CACHE[cache_key] = (time.time(), report)
    return report


def collect_trades(from_iso: str, to_iso: str, symbols_filter: list[str] | None = None) -> dict[str, Any]:
    from_dt = parse_iso_utc(from_iso)
    to_dt = parse_iso_utc(to_iso)

    if to_dt <= from_dt:
        raise ValueError("Ungueltiges Zeitfenster: toIso muss groesser als fromIso sein.")

    # Keep query sizes bounded to avoid very long scans.
    if (to_dt - from_dt).days > 31:
        raise ValueError("Zeitraum zu gross. Bitte maximal 31 Tage pro Anfrage.")
    window_hours = (to_dt - from_dt).total_seconds() / 3600.0
    if window_hours > BINANCE_REPORT_MAX_WINDOW_HOURS:
        raise ValueError(
            f"Binance-Report auf max. {BINANCE_REPORT_MAX_WINDOW_HOURS:.0f}h begrenzt."
        )
    if BINANCE_REPORT_REQUIRE_SYMBOLS and not symbols_filter:
        raise ValueError(
            "Binance-Report nur mit expliziten Symbolen erlaubt (z.B. ['BTCUSDC']). "
            "Nutze sonst source=local."
        )
    if symbols_filter and len(symbols_filter) > BINANCE_REPORT_MAX_SYMBOLS:
        raise ValueError(
            f"Zu viele Symbole fuer Binance-Report ({len(symbols_filter)} > {BINANCE_REPORT_MAX_SYMBOLS})."
        )

    cache_key = f"{from_dt.isoformat()}|{to_dt.isoformat()}"
    if symbols_filter:
        cache_key = f"{cache_key}|{'-'.join(sorted(symbols_filter))}"
    now = time.time()
    cached = _REPORT_CACHE.get(cache_key)
    if cached is not None:
        ts, payload = cached
        if (now - ts) < REPORT_CACHE_TTL_SEC:
            return payload

    start_ms = int(from_dt.timestamp() * 1000)
    end_ms = int(to_dt.timestamp() * 1000)

    symbols = get_usdc_symbols_cached()
    if symbols_filter:
        allow = set(symbols)
        symbols = [symbol for symbol in symbols_filter if symbol in allow]
        if not symbols:
            raise ValueError("Keine gueltigen USDC-Symbole angefordert.")
    if not symbols:
        raise ValueError("Keine Symbole fuer Binance-Report vorhanden.")
    _acquire_binance_report_budget(len(symbols))
    offset_ms = fetch_server_time_offset_ms()

    def collect_for_symbol(symbol: str) -> list[dict[str, Any]]:
        raw_trades = fetch_my_trades_for_symbol(symbol, start_ms, end_ms, offset_ms)
        result: list[dict[str, Any]] = []
        for item in raw_trades:
            normalized = normalize_trade(symbol, item)
            if normalized is None:
                continue

            time_ms = int(normalized["timeMs"])
            if start_ms <= time_ms < end_ms:
                result.append(normalized)
        return result

    normalized_trades: list[dict[str, Any]] = []
    skipped_rate_limit: list[str] = []
    workers = max(1, min(MAX_WORKERS, BINANCE_REPORT_MAX_WORKERS, len(symbols)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(collect_for_symbol, symbol): symbol for symbol in symbols}
        for future in concurrent.futures.as_completed(future_map):
            symbol = future_map[future]
            try:
                normalized_trades.extend(future.result())
            except Exception as exc:
                if "Binance Fehler 429" in str(exc) or "Too much request weight" in str(exc):
                    skipped_rate_limit.append(symbol)
                    continue
                raise RuntimeError(f"Fehler bei Symbol {symbol}: {exc}") from exc

    merged_trades = aggregate_order_fills(normalized_trades)

    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in normalized_trades:
        rows_by_symbol.setdefault(str(row["symbol"]), []).append(row)
    for symbol, rows in rows_by_symbol.items():
        existing = binance_trade_mirror.load_mirror_rows(symbol)
        merged = binance_trade_mirror.merge_mirror_rows(existing, rows)
        binance_trade_mirror.write_mirror_rows(symbol, merged)

    report = build_report(
        from_iso=from_dt.isoformat().replace("+00:00", "Z"),
        to_iso=to_dt.isoformat().replace("+00:00", "Z"),
        normalized_trades=merged_trades,
    )
    report["source"] = "binance_api"
    report["mirrorUpdated"] = True
    if skipped_rate_limit:
        report["partial"] = True
        report["skippedSymbols"] = sorted(set(skipped_rate_limit))
        report["warning"] = (
            "Einige Symbole wurden wegen Binance 429 (Request-Weight-Limit) uebersprungen."
        )
    _REPORT_CACHE[cache_key] = (time.time(), report)
    return report


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "BinanceRelay/2.0"

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        add_cors_headers(self)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        if parsed.path == "/":
            if not serve_file(self, os.path.join(WEB_DIR, "index.html")):
                json_response(self, 404, {"detail": "Dashboard-Datei fehlt."})
            return

        if parsed.path.startswith("/static/"):
            rel = parsed.path.removeprefix("/static/")
            safe_rel = os.path.normpath(rel)
            if safe_rel.startswith("..") or os.path.isabs(safe_rel):
                json_response(self, 403, {"detail": "Forbidden"})
                return

            if not serve_file(self, os.path.join(WEB_DIR, safe_rel)):
                json_response(self, 404, {"detail": "Not found"})
            return

        if parsed.path == "/health":
            json_response(self, 200, {"status": "ok"})
            return

        if parsed.path == "/rotation/points":
            if not RELAY_TOKEN:
                json_response(self, 500, {"detail": "Relay ist nicht konfiguriert (RELAY_TOKEN fehlt)."})
                return
            if not check_auth(self):
                return
            try:
                json_response(self, 200, load_rotation_points())
            except FileNotFoundError as exc:
                json_response(self, 404, {"detail": str(exc)})
            except ValueError as exc:
                json_response(self, 400, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 502, {"detail": str(exc)})
            return

        if parsed.path == "/rotation/live":
            if not RELAY_TOKEN:
                json_response(self, 500, {"detail": "Relay ist nicht konfiguriert (RELAY_TOKEN fehlt)."})
                return
            if not check_auth(self):
                return
            try:
                json_response(self, 200, load_rotation_live())
            except FileNotFoundError as exc:
                json_response(self, 404, {"detail": str(exc)})
            except ValueError as exc:
                json_response(self, 400, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                json_response(self, 502, {"detail": str(exc)})
            return

        config_error = require_config()
        if config_error:
            json_response(self, 500, {"detail": config_error})
            return

        if parsed.path == "/symbols/usdc":
            if not check_auth(self):
                return
            try:
                symbols = get_usdc_symbols_cached()
            except Exception as exc:  # noqa: BLE001
                json_response(self, 502, {"detail": str(exc)})
                return
            json_response(self, 200, {"symbols": symbols})
            return

        json_response(self, 404, {"detail": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/trades/report":
            json_response(self, 404, {"detail": "Not found"})
            return

        if not check_auth(self):
            return

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_len = 0

        raw = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            json_response(self, 400, {"detail": "Ungueltiger JSON-Body."})
            return

        from_iso = str(body.get("fromIso", "")).strip()
        to_iso = str(body.get("toIso", "")).strip()
        day_utc = str(body.get("dayUtc", "")).strip()
        source = str(body.get("source", "local")).strip().lower()
        one_symbol = normalize_usdc_symbol(body.get("symbol"))
        raw_symbols = body.get("symbols")
        requested_symbols: list[str] = []
        if one_symbol:
            requested_symbols.append(one_symbol)
        if isinstance(raw_symbols, list):
            for item in raw_symbols:
                symbol = normalize_usdc_symbol(item)
                if symbol and symbol not in requested_symbols:
                    requested_symbols.append(symbol)

        if day_utc:
            try:
                from_iso, to_iso = parse_day_utc(day_utc)
            except ValueError as exc:
                json_response(self, 400, {"detail": str(exc)})
                return
        elif not from_iso or not to_iso:
            json_response(self, 400, {"detail": "Bitte entweder 'dayUtc' oder 'fromIso'+'toIso' senden."})
            return

        try:
            if source in {"auto", "default"}:
                try:
                    report = collect_trades_mirror(from_iso, to_iso, requested_symbols or None)
                    report["sourceRequested"] = "auto"
                except Exception:
                    report = collect_trades_local(from_iso, to_iso, requested_symbols or None)
                    report["source"] = "local_journal_fallback"
                    report["sourceRequested"] = "auto"
            elif source in {"mirror", "binance_mirror"}:
                report = collect_trades_mirror(from_iso, to_iso, requested_symbols or None)
            elif source in {"local", "journal", "journals"}:
                report = collect_trades_local(from_iso, to_iso, requested_symbols or None)
            elif source in {"binance", "api"}:
                config_error = require_config()
                if config_error:
                    json_response(self, 500, {"detail": config_error})
                    return
                try:
                    report = collect_trades(from_iso, to_iso, requested_symbols or None)
                except Exception as exc:  # noqa: BLE001
                    # Protect the dashboard from outages when Binance temporarily rate-limits.
                    if BINANCE_REPORT_AUTO_FALLBACK_LOCAL and is_binance_rate_limit_error_text(str(exc)):
                        report = collect_trades_local(from_iso, to_iso, requested_symbols or None)
                        report["source"] = "local_journal_fallback"
                        report["sourceRequested"] = "binance"
                        report["partial"] = True
                        report["warning"] = (
                            "Binance-API ist rate-limited/banned. Automatisch auf lokale Journale gewechselt."
                        )
                        report["fallbackReason"] = str(exc)
                    else:
                        raise
            else:
                json_response(
                    self,
                    400,
                    {"detail": "Ungueltige Quelle. Erlaubt: auto, mirror, local oder binance."},
                )
                return
            json_response(self, 200, report)
        except ValueError as exc:
            json_response(self, 400, {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            json_response(self, 502, {"detail": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        if self.path.startswith("/rotation/live"):
            return
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), RelayHandler)
    print(f"Relay listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
