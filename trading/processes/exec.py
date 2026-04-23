from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import hashlib
from collections import deque
from datetime import datetime, timezone
from queue import Empty
from typing import Any, Callable, Dict, List, Optional, Tuple

from trading.execution.backtest import BacktestExecutionConfig, BacktestSimulator
from trading.execution.state_machine import OrderStateMachine
from trading.binance.rest import BinanceRestClient
from trading.binance.ws_auth import BinanceExecutionUpdate, BinanceUserDataWS
from trading.ipc.events import ControlCommand, ExecutionReport, Heartbeat, JournalEvent, OrderIntent, TelemetryEvent
from trading.ipc.queues import try_put, queue_depth
from trading.kraken.rest import KrakenAPIError, KrakenRestClient
from trading.kraken.ws_auth import OpenOrdersWS, OwnTradesWS, OwnTradeUpdate
from trading.processes.context import ProcessContext
from trading.types import Fill, MarketEvent, Order
from trading.utils.binance import normalize_pair as map_pair_binance
from trading.utils.binance import to_binance_symbol
from trading.utils.kraken import map_pair as map_pair_kraken
from trading.utils.kraken import to_kraken_rest_pair

_BINANCE_BAN_UNTIL_RE = re.compile(r"ip banned until\s*(\d+)", re.IGNORECASE)


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _apply_fill_to_sync_state(
    cash_eur: float,
    position_btc: float,
    avg_entry_price: float,
    side: str,
    qty_btc: float,
    price: float,
    fee_eur: float,
) -> Tuple[float, float, float]:
    cash = max(0.0, float(cash_eur or 0.0))
    position = max(0.0, float(position_btc or 0.0))
    avg_entry = max(0.0, float(avg_entry_price or 0.0))
    qty = max(0.0, float(qty_btc or 0.0))
    px = max(0.0, float(price or 0.0))
    fee = max(0.0, float(fee_eur or 0.0))
    side_norm = str(side or "").strip().lower()
    if qty <= 0.0 or px <= 0.0 or side_norm not in {"buy", "sell"}:
        return cash, position, avg_entry

    if side_norm == "buy":
        prior_cost = position * avg_entry
        position += qty
        avg_entry = ((prior_cost + (qty * px)) / position) if position > 0.0 else 0.0
        cash = max(0.0, cash - (qty * px) - fee)
        return cash, position, avg_entry

    executed_qty = min(qty, position) if position > 0.0 else qty
    cash = max(0.0, cash + (executed_qty * px) - fee)
    position = max(0.0, position - qty)
    if position <= 1e-12:
        avg_entry = 0.0
    return cash, position, avg_entry


def _infer_account_sync_delta_fill(
    previous_cash_eur: Optional[float],
    previous_position_btc: Optional[float],
    previous_avg_entry_price: Optional[float],
    cash_eur: float,
    position_btc: float,
    avg_entry_price: float,
    position_tolerance_btc: float,
    cash_tolerance_eur: float,
    min_notional_eur: float = 0.0,
) -> Optional[Dict[str, Any]]:
    if previous_cash_eur is None or previous_position_btc is None:
        return None

    prev_cash = max(0.0, float(previous_cash_eur or 0.0))
    prev_position = max(0.0, float(previous_position_btc or 0.0))
    prev_avg_entry = max(0.0, float(previous_avg_entry_price or 0.0))
    cash = max(0.0, float(cash_eur or 0.0))
    position = max(0.0, float(position_btc or 0.0))
    avg_entry = max(0.0, float(avg_entry_price or 0.0))
    qty_delta = position - prev_position
    qty = abs(qty_delta)
    if qty <= max(1e-12, float(position_tolerance_btc or 0.0)):
        return None

    side = "buy" if qty_delta > 0.0 else "sell"
    price = 0.0
    price_source = "unknown"
    cash_delta = (prev_cash - cash) if side == "buy" else (cash - prev_cash)
    cash_price = 0.0
    if qty > 0.0 and cash_delta > max(1e-12, float(cash_tolerance_eur or 0.0)):
        cash_price = cash_delta / qty

    # Prefer position-derived anchor prices when available. Shared quote-asset balances
    # can move for unrelated symbols and make cash_delta-derived prices unreliable.
    reference_price = 0.0
    if side == "buy" and avg_entry > 0.0:
        reference_price = avg_entry
    elif side == "sell" and prev_avg_entry > 0.0:
        reference_price = prev_avg_entry
    elif avg_entry > 0.0:
        reference_price = avg_entry

    if cash_price > 0.0 and reference_price > 0.0:
        max_rel_deviation = 0.5  # allow at most +/-50% vs reference before fallback
        rel_dev = abs(cash_price - reference_price) / max(1e-12, reference_price)
        if rel_dev <= max_rel_deviation:
            price = cash_price
            price_source = "cash_delta"
        else:
            price = reference_price
            if side == "sell" and prev_avg_entry > 0.0:
                price_source = "previous_avg_entry"
            else:
                price_source = "avg_entry"
    elif cash_price > 0.0:
        price = cash_price
        price_source = "cash_delta"
    elif reference_price > 0.0:
        price = reference_price
        if side == "sell" and prev_avg_entry > 0.0:
            price_source = "previous_avg_entry"
        else:
            price_source = "avg_entry"

    if price <= 0.0:
        return None

    min_notional = max(0.0, float(min_notional_eur or 0.0))
    if min_notional > 0.0:
        prev_notional = max(0.0, prev_position * max(prev_avg_entry, price))
        cur_notional = max(0.0, position * max(avg_entry, price))
        delta_notional = max(0.0, qty * price, cash_delta)
        if max(prev_notional, cur_notional, delta_notional) + 1e-9 < min_notional:
            return None

    return {
        "side": side,
        "qty_btc": qty,
        "price": float(price),
        "fee_eur": 0.0,
        "price_source": price_source,
        "cash_price": float(cash_price),
        "reference_price": float(reference_price),
        "cash_delta_eur": float(cash_delta),
        "previous_cash_eur": prev_cash,
        "cash_eur": cash,
        "previous_position_btc": prev_position,
        "position_btc": position,
        "previous_avg_entry_price": prev_avg_entry,
        "avg_entry_price": avg_entry,
    }


def _clone_intent_with_meta(intent: OrderIntent, **meta_updates: Any) -> OrderIntent:
    meta = dict(intent.meta) if isinstance(intent.meta, dict) else {}
    meta.update(meta_updates)
    return OrderIntent(
        ts=intent.ts,
        side=intent.side,
        qty_btc=float(intent.qty_btc),
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        post_only=bool(intent.post_only),
        client_id=intent.client_id,
        reason=intent.reason,
        meta=meta,
    )


def _should_retry_sell_after_balance_refresh(
    requested_qty: float,
    available_qty: Optional[float],
    reference_price: float,
    min_notional: float,
    min_trade_qty: float,
) -> bool:
    if available_qty is None:
        return False
    requested = max(0.0, float(requested_qty or 0.0))
    available = max(0.0, float(available_qty or 0.0))
    px = max(0.0, float(reference_price or 0.0))
    if requested <= 0.0 or available + 1e-12 >= requested:
        return False
    requested_notional = requested * px
    available_notional = available * px
    severe_shortfall = available <= max(1e-12, requested * 0.5)
    if min_notional > 0.0 and requested_notional + 1e-9 >= min_notional and available_notional + 1e-9 < min_notional:
        return True
    if min_trade_qty > 0.0 and requested + 1e-12 >= min_trade_qty and available + 1e-12 < min_trade_qty:
        return True
    return severe_shortfall


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self.last = time.time()

    def acquire(self) -> float:
        now = time.time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens < 1.0:
            sleep_for = (1.0 - self.tokens) / self.rate
            time.sleep(sleep_for)
            now = time.time()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.tokens -= 1.0
            self.last = now
            return sleep_for
        self.tokens -= 1.0
        self.last = now
        return 0.0


class RateBudget:
    def __init__(self, rate_per_sec: float, capacity: float):
        self._bucket = TokenBucket(rate_per_sec=rate_per_sec, capacity=capacity)
        self._lock = threading.Lock()

    def acquire(self) -> float:
        with self._lock:
            return self._bucket.acquire()


class DeadmanStub:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def cancel_all_orders_after(self, timeout: int) -> dict[str, int]:
        self.calls.append(int(timeout))
        return {"timeout": int(timeout)}

    def cancel_all(self) -> dict[str, int]:
        return {"count": 0}


class OwnTradesDeduper:
    def __init__(self, maxlen: int = 50000) -> None:
        self.maxlen = maxlen
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self._lock = threading.Lock()

    def first_seen(self, trade_id: str, event_id: Optional[str] = None) -> bool:
        keys: list[str] = []
        if trade_id:
            keys.append(f"trade:{trade_id}")
        if event_id:
            keys.append(f"event:{event_id}")
        if not keys:
            return True
        with self._lock:
            if any(key in self._seen for key in keys):
                return False
            for key in keys:
                self._seen.add(key)
                self._order.append(key)
            while len(self._order) > self.maxlen:
                old = self._order.popleft()
                self._seen.discard(old)
            return True


class OrderFillTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._filled_by_order: Dict[str, float] = {}
        self._target_by_order: Dict[str, float] = {}
        self._accounted_fee_by_order: Dict[str, float] = {}
        # qty synthesized from reconcile; later owntrades for same qty should not be double-counted.
        self._synthetic_cover_by_order: Dict[str, float] = {}

    def set_target(self, order_id: str, target_qty_btc: Optional[float]) -> None:
        if not order_id or target_qty_btc is None:
            return
        target = max(0.0, float(target_qty_btc))
        with self._lock:
            self._target_by_order[order_id] = target

    def _status_for(self, cumulative: float, target: Optional[float]) -> str:
        status = "PARTIAL"
        if target is not None and target > 0 and cumulative + 1e-12 >= target:
            status = "FILLED"
        return status

    def record_fill(self, order_id: str, fill_qty_btc: float) -> Tuple[float, Optional[float], str]:
        _, _, cumulative, target, status, _ = self.record_real_fill(order_id, fill_qty_btc, 0.0)
        return cumulative, target, status

    def record_real_fill(
        self,
        order_id: str,
        fill_qty_btc: float,
        fee_eur: float = 0.0,
    ) -> Tuple[float, float, float, Optional[float], str, float]:
        qty = max(0.0, float(fill_qty_btc))
        fee = max(0.0, float(fee_eur))
        with self._lock:
            synthetic_cover = self._synthetic_cover_by_order.get(order_id, 0.0)
            covered_qty = min(qty, synthetic_cover)
            effective_qty = max(0.0, qty - covered_qty)
            if covered_qty > 0.0:
                self._synthetic_cover_by_order[order_id] = max(0.0, synthetic_cover - covered_qty)

            effective_fee = fee
            if qty > 0.0 and effective_qty < qty:
                effective_fee = fee * (effective_qty / qty)

            cumulative = self._filled_by_order.get(order_id, 0.0) + effective_qty
            self._filled_by_order[order_id] = cumulative
            self._accounted_fee_by_order[order_id] = self._accounted_fee_by_order.get(order_id, 0.0) + effective_fee
            target = self._target_by_order.get(order_id)
            status = self._status_for(cumulative, target)
        return effective_qty, effective_fee, cumulative, target, status, covered_qty

    def record_synthetic_fill(
        self,
        order_id: str,
        fill_qty_btc: float,
        fee_eur: float = 0.0,
    ) -> Tuple[float, Optional[float], str]:
        qty = max(0.0, float(fill_qty_btc))
        fee = max(0.0, float(fee_eur))
        with self._lock:
            cumulative = self._filled_by_order.get(order_id, 0.0) + qty
            self._filled_by_order[order_id] = cumulative
            self._accounted_fee_by_order[order_id] = self._accounted_fee_by_order.get(order_id, 0.0) + fee
            self._synthetic_cover_by_order[order_id] = self._synthetic_cover_by_order.get(order_id, 0.0) + qty
            target = self._target_by_order.get(order_id)
            status = self._status_for(cumulative, target)
        return cumulative, target, status

    def filled_qty(self, order_id: str) -> float:
        with self._lock:
            return self._filled_by_order.get(order_id, 0.0)

    def has_fill(self, order_id: str) -> bool:
        return self.filled_qty(order_id) > 0.0

    def accounted_fee(self, order_id: str) -> float:
        with self._lock:
            return self._accounted_fee_by_order.get(order_id, 0.0)


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
    try_put(ctx.q_journal, evt)


def _send_telemetry(ctx: ProcessContext, data: Dict[str, Any]) -> None:
    evt = TelemetryEvent(ts=datetime.now(timezone.utc), process="exec", data=data)
    try_put(ctx.q_telemetry, evt)


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="exec", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _intent_reference_price(intent: OrderIntent, adapter: Any = None, symbol: str = "") -> float:
    px = _safe_float(intent.limit_price)
    if px is not None and px > 0.0:
        return float(px)
    meta = intent.meta if isinstance(getattr(intent, "meta", None), dict) else {}
    px = _safe_float(meta.get("reference_price"))
    if px is not None and px > 0.0:
        return float(px)
    if isinstance(adapter, BinanceRestClient):
        return float(adapter.ticker_price(symbol))
    return 0.0


def _effective_min_entry_notional(
    *,
    configured_min_notional: float,
    exchange_min_notional: float,
) -> float:
    return max(0.0, float(configured_min_notional or 0.0), float(exchange_min_notional or 0.0))


def _adjust_sell_qty_for_balance(
    *,
    requested_qty: float,
    available_qty: float,
    buffer_qty: float,
    reference_price: float,
    min_notional: float,
) -> tuple[float, float]:
    sell_qty = min(max(0.0, float(requested_qty)), max(0.0, float(available_qty)))
    applied_buffer = max(0.0, float(buffer_qty))
    clamped_qty = min(sell_qty, max(0.0, float(available_qty) - applied_buffer))
    if (
        reference_price > 0.0
        and min_notional > 0.0
        and clamped_qty > 0.0
        and (clamped_qty * reference_price) + 1e-9 < min_notional
        and (sell_qty * reference_price) + 1e-9 >= min_notional
    ):
        return sell_qty, max(0.0, float(available_qty) - sell_qty)
    return clamped_qty, applied_buffer


def _map_status(status: str, vol: float | None, vol_exec: float | None) -> str:
    status = status.lower()
    if status in {"open", "pending"}:
        return "OPEN"
    if status in {"canceled", "expired"}:
        return "CANCELED"
    if status in {"closed"}:
        if vol is not None and vol_exec is not None and vol_exec < vol:
            return "PARTIAL"
        return "FILLED"
    return "OPEN"


def _map_binance_exec_status(status: str) -> str:
    s = str(status or "").upper().strip()
    if s in {"NEW", "PENDING_NEW"}:
        return "OPEN"
    if s in {"PARTIALLY_FILLED"}:
        return "PARTIAL"
    if s in {"FILLED"}:
        return "FILLED"
    if s in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return "CANCELED"
    if s in {"REJECTED"}:
        return "REJECTED"
    return "OPEN"


def _split_pair_assets(pair: str) -> tuple[str, str]:
    raw = (pair or "").strip().upper().replace("-", "/")
    if "/" not in raw:
        return "BTC", "EUR"
    base, quote = (part.strip() for part in raw.split("/", 1))
    if not base:
        base = "BTC"
    if not quote:
        quote = "EUR"
    return base, quote


def _asset_balance_aliases(asset: str) -> list[str]:
    token = str(asset or "").strip().upper()
    if not token:
        return []

    aliases: list[str] = [token]
    if token == "BTC":
        aliases.extend(["XBT", "XXBT", "XBTC"])
    elif token == "XBT":
        aliases.extend(["BTC", "XXBT", "XBTC"])

    if not token.startswith("X"):
        aliases.append(f"X{token}")
        aliases.append(f"XX{token}")
    if not token.startswith("Z"):
        aliases.append(f"Z{token}")

    if token.startswith(("X", "Z")) and len(token) > 1:
        aliases.append(token[1:])
    if token.startswith("XX") and len(token) > 2:
        aliases.append(token[2:])

    out: list[str] = []
    seen: set[str] = set()
    for item in aliases:
        key = item.strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _balance_for_asset(balances: Dict[str, Any], asset: str, field: str = "total") -> Optional[float]:
    if not isinstance(balances, dict):
        return None
    normalized: Dict[str, Any] = {str(k).strip().upper(): v for k, v in balances.items()}
    for key in _asset_balance_aliases(asset):
        if key not in normalized:
            continue
        entry = normalized.get(key)
        if isinstance(entry, dict):
            candidates = [field, "total", "free", "locked"]
            for candidate in candidates:
                value = _safe_float(entry.get(candidate))
                if value is not None:
                    return float(value)
            continue
        value = _safe_float(entry)
        if value is not None:
            return float(value)
    return None


def _journal_json_path(cfg: Dict[str, Any]) -> str:
    return str(_cfg(cfg, "journal.json_path", _cfg(cfg, "journal.path", "logs/journal_events.jsonl")))


def _explicit_journal_json_path(cfg: Dict[str, Any]) -> str:
    journal_cfg = cfg.get("journal")
    if not isinstance(journal_cfg, dict):
        return ""
    for key in ("json_path", "path"):
        path = str(journal_cfg.get(key) or "").strip()
        if path:
            return path
    return ""


def _event_timestamp_from_row(item: Dict[str, Any]) -> Optional[str]:
    payload = item.get("payload")
    if isinstance(payload, dict):
        ts = str(payload.get("ts") or "").strip()
        if ts:
            return ts
    data = item.get("data")
    if isinstance(data, dict):
        ts = str(data.get("ts") or "").strip()
        if ts:
            return ts
    ts = str(item.get("ts") or "").strip()
    return ts or None


def _latest_flat_sync_cutoff_from_tail(
    tail: deque[str],
    *,
    position_tolerance_btc: float,
) -> Optional[str]:
    cutoff_ts: Optional[str] = None
    flat_eps = max(1e-12, float(position_tolerance_btc or 0.0))
    for raw in tail:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("event_type") or item.get("event") or "").strip()
        if event_type not in {"core_account_synced", "exec_core_account_sync"}:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = item.get("data")
        if not isinstance(payload, dict):
            continue
        position_btc = _safe_float(payload.get("position_btc"))
        if position_btc is None or max(0.0, float(position_btc)) > flat_eps:
            continue
        ts = _event_timestamp_from_row(item)
        if ts:
            cutoff_ts = ts
    return cutoff_ts


def _load_seen_client_ids_from_journal(cfg: Dict[str, Any], max_events: int = 20000) -> set[str]:
    if max_events <= 0:
        return set()
    path = _explicit_journal_json_path(cfg)
    if not path or not os.path.exists(path):
        return set()
    tail: deque[str] = deque(maxlen=max_events)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    tail.append(line)
    except Exception:
        return set()
    out: set[str] = set()
    for raw in tail:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict) or item.get("event_type") != "exec_intent_seen":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        client_id = payload.get("client_id")
        if isinstance(client_id, str) and client_id:
            out.add(client_id)
    return out


def _load_entry_reference_from_journal(
    cfg: Dict[str, Any],
    *,
    expected_position_btc: float,
    position_tolerance_btc: float,
    max_events: int = 200000,
) -> Optional[Dict[str, Any]]:
    if expected_position_btc <= 0.0 or max_events <= 0:
        return None
    path = _explicit_journal_json_path(cfg)
    if not path or not os.path.exists(path):
        return None

    tail: deque[str] = deque(maxlen=max_events)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                row = line.strip()
                if row:
                    tail.append(row)
    except Exception:
        return None

    cutoff_ts = _latest_flat_sync_cutoff_from_tail(
        tail,
        position_tolerance_btc=position_tolerance_btc,
    )
    pos_qty = 0.0
    cost_basis = 0.0
    matched_fills = 0
    current_entry_ts: Optional[str] = None
    for raw in tail:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict) or item.get("event_type") != "fill":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        # Ignore synthetic reconciliation deltas when rebuilding entry reference.
        # They are bookkeeping artifacts and can distort average entry for open positions.
        source = str(payload.get("source") or "").strip().lower()
        if source == "account_sync_delta" or bool(payload.get("synthetic", False)):
            continue

        raw_fill_ts = payload.get("ts", item.get("ts"))
        fill_ts = str(raw_fill_ts or "").strip() or None
        if cutoff_ts and fill_ts and fill_ts <= cutoff_ts:
            continue
        side = str(payload.get("side", "")).strip().lower()
        qty = max(0.0, float(_safe_float(payload.get("qty_btc")) or 0.0))
        price = max(0.0, float(_safe_float(payload.get("price")) or 0.0))
        fee = max(0.0, float(_safe_float(payload.get("fee_eur")) or 0.0))
        if qty <= 0.0 or price <= 0.0:
            continue

        if side == "buy":
            if pos_qty <= 1e-12:
                current_entry_ts = fill_ts
            pos_qty += qty
            cost_basis += (qty * price) + fee
            matched_fills += 1
            continue

        if side == "sell":
            if pos_qty <= 0.0:
                pos_qty = 0.0
                cost_basis = 0.0
                continue
            sell_qty = min(qty, pos_qty)
            if sell_qty > 0.0 and cost_basis > 0.0 and pos_qty > 0.0:
                cost_basis -= cost_basis * (sell_qty / pos_qty)
                cost_basis = max(0.0, cost_basis)
            pos_qty -= sell_qty
            if pos_qty <= 1e-12:
                pos_qty = 0.0
                cost_basis = 0.0
                current_entry_ts = None
            matched_fills += 1

    if pos_qty <= 0.0 or cost_basis <= 0.0 or matched_fills <= 0:
        return None

    # If replayed position is materially smaller than exchange position,
    # we likely miss historical buys and the entry would be biased.
    # If replayed position is larger, missing sells do not bias average entry
    # under average-cost accounting, so we can still use the reference.
    if pos_qty + max(0.0, position_tolerance_btc) < expected_position_btc:
        return None

    avg_entry_price = cost_basis / pos_qty if pos_qty > 0.0 else 0.0
    if avg_entry_price <= 0.0:
        return None

    return {
        "avg_entry_price": float(avg_entry_price),
        "position_btc": float(pos_qty),
        "fills_replayed": float(matched_fills),
        "entry_ts": current_entry_ts,
    }


def _load_entry_reference_from_exchange_history(
    adapter: Any,
    *,
    symbol: str,
    expected_position_btc: float,
    position_tolerance_btc: float,
    max_trades: int = 1000,
) -> Optional[Dict[str, Any]]:
    if expected_position_btc <= 0.0 or max_trades <= 0:
        return None
    if not isinstance(adapter, BinanceRestClient):
        return None

    try:
        trades = adapter.my_trades(symbol=symbol, limit=max_trades)
    except Exception:
        return None
    if not trades:
        return None

    try:
        trades = sorted(
            trades,
            key=lambda item: (
                int(item.get("time", 0) or 0),
                int(item.get("id", 0) or 0),
            ),
        )
    except Exception:
        pass

    flat_eps = max(1e-12, float(position_tolerance_btc or 0.0))
    replay_start_idx = 0
    replay_pos_qty = 0.0
    for idx, tr in enumerate(trades):
        try:
            qty = max(0.0, float(tr.get("qty", "0") or 0.0))
        except Exception:
            qty = 0.0
        try:
            price = max(0.0, float(tr.get("price", "0") or 0.0))
        except Exception:
            price = 0.0
        if qty <= 0.0 or price <= 0.0:
            continue
        is_buy = bool(tr.get("isBuyer", False))
        if is_buy:
            replay_pos_qty += qty
            continue
        if replay_pos_qty <= 0.0:
            replay_pos_qty = 0.0
            replay_start_idx = idx + 1
            continue
        replay_pos_qty -= min(qty, replay_pos_qty)
        if replay_pos_qty <= flat_eps:
            replay_pos_qty = 0.0
            replay_start_idx = idx + 1

    pos_qty = 0.0
    cost_basis = 0.0
    matched_trades = 0
    current_entry_ts: Optional[str] = None
    for tr in trades[replay_start_idx:]:
        try:
            qty = max(0.0, float(tr.get("qty", "0") or 0.0))
        except Exception:
            qty = 0.0
        try:
            price = max(0.0, float(tr.get("price", "0") or 0.0))
        except Exception:
            price = 0.0
        try:
            quote_qty = max(0.0, float(tr.get("quoteQty", "0") or 0.0))
        except Exception:
            quote_qty = 0.0
        if quote_qty <= 0.0 and qty > 0.0 and price > 0.0:
            quote_qty = qty * price
        if qty <= 0.0 or price <= 0.0:
            continue

        trade_time_ms = int(tr.get("time", 0) or 0)
        trade_ts: Optional[str] = None
        if trade_time_ms > 0:
            trade_ts = datetime.fromtimestamp(trade_time_ms / 1000.0, tz=timezone.utc).isoformat()
        is_buy = bool(tr.get("isBuyer", False))
        if is_buy:
            if pos_qty <= 1e-12:
                current_entry_ts = trade_ts
            try:
                commission = max(0.0, float(tr.get("commission", "0") or 0.0))
            except Exception:
                commission = 0.0
            commission_asset = str(tr.get("commissionAsset", "") or "").upper()
            fee_quote = adapter.commission_to_quote(
                symbol=symbol,
                commission=commission,
                commission_asset=commission_asset,
                trade_price=price,
            )
            pos_qty += qty
            cost_basis += quote_qty + max(0.0, float(fee_quote))
            matched_trades += 1
            continue

        if pos_qty <= 0.0:
            pos_qty = 0.0
            cost_basis = 0.0
            continue
        sell_qty = min(qty, pos_qty)
        if sell_qty > 0.0 and cost_basis > 0.0 and pos_qty > 0.0:
            cost_basis -= cost_basis * (sell_qty / pos_qty)
            cost_basis = max(0.0, cost_basis)
        pos_qty -= sell_qty
        if pos_qty <= 1e-12:
            pos_qty = 0.0
            cost_basis = 0.0
            current_entry_ts = None
        matched_trades += 1

    if pos_qty <= 0.0 or cost_basis <= 0.0 or matched_trades <= 0:
        return None
    if pos_qty + max(0.0, position_tolerance_btc) < expected_position_btc:
        return None

    avg_entry_price = cost_basis / pos_qty if pos_qty > 0.0 else 0.0
    if avg_entry_price <= 0.0:
        return None

    return {
        "avg_entry_price": float(avg_entry_price),
        "position_btc": float(pos_qty),
        "fills_replayed": float(matched_trades),
        "entry_ts": current_entry_ts,
    }


def _handle_own_trade(
    ctx: ProcessContext,
    update: OwnTradeUpdate,
    order_sm: OrderStateMachine,
    sm_lock: threading.Lock,
    deduper: OwnTradesDeduper,
    fill_tracker: OrderFillTracker,
    resolve_order_aliases: Optional[Callable[[str], List[str]]] = None,
    on_fill_observed: Optional[Callable[[Fill], None]] = None,
) -> None:
    if not deduper.first_seen(update.trade_id, update.event_id):
        return
    aliases = resolve_order_aliases(update.order_id) if resolve_order_aliases is not None else []
    effective_qty, effective_fee, cumulative_filled, target_qty, status, covered_qty = fill_tracker.record_real_fill(
        update.order_id,
        update.vol,
        update.fee,
    )
    if effective_qty <= 0.0:
        _send_journal(
            ctx,
            "exec_owntrade_covered_by_reconcile",
            {
                "order_id": update.order_id,
                "trade_id": update.trade_id,
                "event_id": update.event_id,
                "reported_qty_btc": update.vol,
                "covered_qty_btc": covered_qty,
                "source": "owntrades",
            },
        )
        return
    fill = Fill(
        ts=update.ts,
        side=update.side or "",
        qty_btc=effective_qty,
        price=update.price,
        fee_eur=effective_fee,
        order_id=update.order_id,
        slippage_bps=0.0,
        meta={"order_aliases": aliases} if aliases else {},
    )
    with sm_lock:
        order_sm.transition(update.order_id, status, update.ts, allow_recovery=True)
        for alias in aliases:
            order_sm.transition(alias, status, update.ts, allow_recovery=True)
    report = ExecutionReport(
        ts=update.ts,
        order_id=update.order_id,
        status=status,
        filled_qty_btc=cumulative_filled,
        avg_price=update.price,
        fee_eur=effective_fee,
        latency_ms=0.0,
        meta={
            "source": "owntrades",
            "trade_id": update.trade_id,
            "event_id": update.event_id,
            **({"order_aliases": aliases} if aliases else {}),
        },
    )
    try_put(ctx.q_exec_report, report)
    try_put(ctx.q_exec_report, fill)
    _send_journal(ctx, "exec_report", _exec_report_payload(report))
    _send_journal(
        ctx,
        "fill",
        {
            "ts": fill.ts.isoformat(),
            "order_id": fill.order_id,
            "side": fill.side,
            "qty_btc": fill.qty_btc,
            "price": fill.price,
            "fee_eur": fill.fee_eur,
            "slippage_bps": fill.slippage_bps,
            "trade_id": update.trade_id,
            "event_id": update.event_id,
            "pair": update.pair,
            "reported_qty_btc": update.vol,
            "fill_status": status,
            "cum_filled_qty_btc": cumulative_filled,
            "target_qty_btc": target_qty,
            "source": "owntrades",
        },
    )
    if on_fill_observed is not None:
        try:
            on_fill_observed(fill)
        except Exception:
            pass


def _exec_report_payload(report: ExecutionReport) -> Dict[str, Any]:
    # Keep journal JSONL append-only and robust: datetimes must be serialized explicitly.
    payload: Dict[str, Any] = dict(report.__dict__)
    ts = payload.get("ts")
    if isinstance(ts, datetime):
        payload["ts"] = ts.isoformat()
    return payload


def _kraken_cl_ord_id(client_id: str) -> str:
    # Kraken is strict about allowed chars/length. Use a stable compact id for REST.
    # Deterministic across restarts for idempotency.
    digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()
    return "c" + digest[:31]


def run_exec(ctx: ProcessContext) -> None:
    cfg = ctx.config
    mode = ctx.mode
    exchange = str(_cfg(cfg, "exec.exchange", _cfg(cfg, "live.exchange", "kraken"))).strip().lower()
    accept_new = True
    run_state_lock = threading.Lock()

    rate_per_sec = float(_cfg(cfg, "exec.rate_limit_per_sec", _cfg(cfg, "live.rate_limit_per_sec", 1.0)))
    rate_budget = RateBudget(rate_per_sec=rate_per_sec, capacity=max(1.0, rate_per_sec))

    simulator = None
    adapter = None
    deadman_adapter = None
    if mode in {"paper", "sim"}:
        simulator = BacktestSimulator(
            BacktestExecutionConfig(
                latency_bars=int(_cfg(cfg, "execution.latency_bars", 0)),
                partial_fill_ratio=float(_cfg(cfg, "execution.partial_fill_ratio", 1.0)),
                slippage_bps=float(_cfg(cfg, "execution.slippage_bps", 1.0)),
            ),
            maker_fee_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
            taker_fee_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
        )
        if bool(_cfg(cfg, "exec.deadman_in_paper", False)):
            deadman_adapter = DeadmanStub()
    else:
        if exchange == "binance":
            live_pair = str(_cfg(cfg, "exec.pair", _cfg(cfg, "md.pair", _cfg(cfg, "live.symbol", "ETH/USDT"))))
            adapter = BinanceRestClient(
                api_key=str(_cfg(cfg, "live.api_key", os.getenv("BINANCE_API_KEY", ""))),
                api_secret=str(_cfg(cfg, "live.api_secret", os.getenv("BINANCE_API_SECRET", ""))),
                base_url=str(_cfg(cfg, "live.rest_url", _cfg(cfg, "live.base_url", "https://api.binance.com"))),
                symbol=to_binance_symbol(live_pair),
                timeout=float(_cfg(cfg, "live.timeout_sec", 10.0)),
            )
            if bool(_cfg(cfg, "exec.enable_deadman_on_binance", False)):
                deadman_adapter = adapter
            else:
                deadman_adapter = None
        else:
            adapter = KrakenRestClient(
                api_key=_cfg(cfg, "live.api_key", ""),
                api_secret=_cfg(cfg, "live.api_secret", ""),
                base_url=_cfg(cfg, "live.rest_url", "https://api.kraken.com"),
            )
            deadman_adapter = adapter

    order_sm = OrderStateMachine()
    sm_lock = threading.Lock()
    own_trades_deduper = OwnTradesDeduper(maxlen=int(_cfg(cfg, "exec.owntrades_dedupe_size", 50000)))
    fill_tracker = OrderFillTracker()
    seen_client_ids = _load_seen_client_ids_from_journal(
        cfg,
        max_events=int(_cfg(cfg, "exec.restart_seen_client_ids_max_events", 20000)),
    )
    order_recv_ts: Dict[str, float] = {}
    last_latency_ms = 0.0
    if exchange == "binance":
        pair_ws = map_pair_binance(_cfg(cfg, "exec.pair", _cfg(cfg, "md.pair", _cfg(cfg, "live.symbol", "ETH/USDT"))))
        pair_rest = to_binance_symbol(pair_ws)
    else:
        pair_ws = map_pair_kraken(_cfg(cfg, "exec.pair", _cfg(cfg, "md.pair", _cfg(cfg, "live.kraken_pair", "BTC/EUR"))))
        pair_rest = to_kraken_rest_pair(pair_ws)
    pair_base_asset, pair_quote_asset = _split_pair_assets(pair_ws)
    known_txids: set[str] = set()
    txid_aliases: Dict[str, set[str]] = {}
    known_txids_lock = threading.Lock()
    canary = bool(_cfg(cfg, "exec.canary_mode", False))
    rate_limit_hits = 0
    last_rate_signal = 0.0
    deadman_timeout = int(_cfg(cfg, "exec.deadman_timeout_sec", 60))
    deadman_tick = float(_cfg(cfg, "exec.deadman_tick_sec", 20.0))
    deadman_state = {"enabled": False, "disable_reason": "startup", "disable_pending": False}
    deadman_lock = threading.Lock()
    rate_limit_pause_sec = float(_cfg(cfg, "exec.rate_limit_pause_sec", 2.0))
    resume_after: float | None = None
    deadman_budget_wait_ms = 0.0
    deadman_rate_limit_hits = 0
    fill_truth_grace_sec = float(_cfg(cfg, "exec.fill_truth_grace_sec", 10.0))
    fill_truth_pending_ttl_sec = float(_cfg(cfg, "exec.fill_truth_pending_ttl_sec", 900.0))
    fill_truth_pending_max_entries = int(_cfg(cfg, "exec.fill_truth_pending_max_entries", 10000))
    pending_fill_truth: Dict[str, Dict[str, Any]] = {}
    pending_fill_truth_lock = threading.Lock()

    hb_seq = 0
    last_heartbeat = 0.0
    last_telemetry = 0.0
    heartbeat_interval = float(_cfg(cfg, "exec.heartbeat_interval", 1.0))
    telemetry_interval = float(_cfg(cfg, "exec.telemetry_interval", 1.0))

    _send_journal(ctx, "exec_start", {"mode": mode})
    if seen_client_ids:
        _send_journal(
            ctx,
            "exec_recovery_seen_client_ids",
            {"count": len(seen_client_ids), "source": _journal_json_path(cfg)},
        )

    def _set_deadman_enabled(enabled: bool, reason: str) -> None:
        if deadman_adapter is None:
            return
        with deadman_lock:
            live_enabled = bool(enabled and not canary)
            deadman_state["enabled"] = live_enabled
            if not live_enabled:
                deadman_state["disable_reason"] = reason
                deadman_state["disable_pending"] = True
            else:
                deadman_state["disable_pending"] = False

    def _set_accept_new(enabled: bool, reason: str, clear_resume: bool = True) -> None:
        nonlocal accept_new, resume_after
        with run_state_lock:
            accept_new = enabled
            if clear_resume:
                resume_after = None
        _set_deadman_enabled(enabled, reason)

    def _track_known_txid(order_id: str, status: Optional[str] = None) -> None:
        if not order_id:
            return
        with known_txids_lock:
            if status in {"CANCELED", "FILLED", "REJECTED"}:
                known_txids.discard(order_id)
                txid_aliases.pop(order_id, None)
            else:
                known_txids.add(order_id)

    def _known_snapshot() -> set[str]:
        with known_txids_lock:
            return set(known_txids)

    def _register_txid_alias(txid: str, alias: Optional[str]) -> None:
        if not txid or not alias:
            return
        alias_id = str(alias).strip()
        if not alias_id or alias_id == txid:
            return
        with known_txids_lock:
            aliases = txid_aliases.setdefault(txid, set())
            aliases.add(alias_id)

    def _aliases_for_order(order_id: str) -> list[str]:
        if not order_id:
            return []
        with known_txids_lock:
            aliases = txid_aliases.get(order_id, set())
            return sorted(a for a in aliases if a and a != order_id)

    def _transition_order_and_aliases(order_id: str, state: str, ts: datetime) -> list[str]:
        aliases = _aliases_for_order(order_id)
        with sm_lock:
            order_sm.transition(order_id, state, ts, allow_recovery=True)
            for alias in aliases:
                order_sm.transition(alias, state, ts, allow_recovery=True)
        return aliases

    def _mark_seen_client_id(client_id: Optional[str], reason: str, order_id: str = "") -> None:
        if not client_id:
            return
        if client_id in seen_client_ids:
            return
        seen_client_ids.add(client_id)
        _send_journal(
            ctx,
            "exec_intent_seen",
            {"client_id": client_id, "reason": reason, "order_id": order_id},
        )

    def _track_fill_truth_pending(order_id: str, source: str, ts: datetime) -> None:
        if not order_id or fill_tracker.has_fill(order_id):
            return
        should_emit = False
        with pending_fill_truth_lock:
            if order_id not in pending_fill_truth:
                pending_fill_truth[order_id] = {
                    "source": source,
                    "created_at": time.time(),
                    "status_ts": ts.isoformat(),
                    "warned": False,
                }
                should_emit = True
        if should_emit:
            _send_journal(
                ctx,
                "exec_fill_truth_pending",
                {
                    "order_id": order_id,
                    "source": source,
                    "status": "FILLED",
                    "grace_sec": fill_truth_grace_sec,
                },
            )

    def _resolve_fill_truth_if_seen(order_id: str, resolved_by: str) -> None:
        if not order_id or not fill_tracker.has_fill(order_id):
            return
        item: Optional[Dict[str, Any]] = None
        with pending_fill_truth_lock:
            item = pending_fill_truth.pop(order_id, None)
        if item is None:
            return
        created_at = float(item.get("created_at", time.time()))
        age_sec = max(0.0, time.time() - created_at)
        _send_journal(
            ctx,
            "exec_fill_truth_reconciled",
            {
                "order_id": order_id,
                "source": item.get("source"),
                "resolved_by": resolved_by,
                "age_sec": round(age_sec, 3),
                "filled_qty_btc": fill_tracker.filled_qty(order_id),
            },
        )

    def _check_fill_truth_gaps() -> None:
        now = time.time()
        warn_items: list[Dict[str, Any]] = []
        resolved: list[str] = []
        expired: list[Dict[str, Any]] = []
        evicted: list[Dict[str, Any]] = []
        with pending_fill_truth_lock:
            for order_id, item in pending_fill_truth.items():
                if fill_tracker.has_fill(order_id):
                    resolved.append(order_id)
                    continue
                created_at = float(item.get("created_at", now))
                age_sec = max(0.0, now - created_at)
                if age_sec >= fill_truth_grace_sec and not bool(item.get("warned", False)):
                    item["warned"] = True
                    warn_items.append(
                        {
                            "order_id": order_id,
                            "source": item.get("source"),
                            "age_sec": round(age_sec, 3),
                            "grace_sec": fill_truth_grace_sec,
                        }
                    )
            for order_id in resolved:
                pending_fill_truth.pop(order_id, None)
            if fill_truth_pending_ttl_sec > 0:
                for order_id, item in list(pending_fill_truth.items()):
                    created_at = float(item.get("created_at", now))
                    age_sec = max(0.0, now - created_at)
                    if age_sec >= fill_truth_pending_ttl_sec:
                        pending_fill_truth.pop(order_id, None)
                        expired.append(
                            {
                                "order_id": order_id,
                                "source": item.get("source"),
                                "age_sec": round(age_sec, 3),
                                "ttl_sec": fill_truth_pending_ttl_sec,
                            }
                        )
            if fill_truth_pending_max_entries > 0 and len(pending_fill_truth) > fill_truth_pending_max_entries:
                overflow = len(pending_fill_truth) - fill_truth_pending_max_entries
                oldest = sorted(
                    pending_fill_truth.items(),
                    key=lambda kv: float(kv[1].get("created_at", now)),
                )[:overflow]
                for order_id, item in oldest:
                    pending_fill_truth.pop(order_id, None)
                    evicted.append(
                        {
                            "order_id": order_id,
                            "source": item.get("source"),
                            "reason": "max_entries",
                            "max_entries": fill_truth_pending_max_entries,
                        }
                    )
        for order_id in resolved:
            _send_journal(
                ctx,
                "exec_fill_truth_reconciled",
                {
                    "order_id": order_id,
                    "source": "scanner",
                    "resolved_by": "scanner",
                    "filled_qty_btc": fill_tracker.filled_qty(order_id),
                },
            )
        for payload in expired:
            _send_journal(ctx, "exec_fill_truth_expired", payload)
        for payload in evicted:
            _send_journal(ctx, "exec_fill_truth_evicted", payload)
        for payload in warn_items:
            _send_journal(ctx, "exec_fill_truth_gap", payload)

    buy_order_ids: set[str] = set()

    def _emit_exec_report(report: ExecutionReport) -> None:
        nonlocal buy_settlement_confirm_pending
        meta: Dict[str, Any] = dict(report.meta) if isinstance(report.meta, dict) else {}
        aliases = _aliases_for_order(report.order_id)
        if aliases:
            merged: set[str] = set(aliases)
            existing = meta.get("order_aliases")
            if isinstance(existing, str):
                merged.add(existing)
            elif isinstance(existing, (list, tuple, set)):
                for item in existing:
                    if item is not None:
                        merged.add(str(item))
            merged.discard(report.order_id)
            if merged:
                meta["order_aliases"] = sorted(merged)
        report.meta = meta
        report_ids = {str(report.order_id or "").strip()}
        report_ids.update(str(alias).strip() for alias in aliases if str(alias).strip())
        report_ids.discard("")
        is_buy_report = any(order_id in buy_order_ids for order_id in report_ids)
        terminal_status = str(report.status or "").upper().strip() in {"FILLED", "CANCELED", "REJECTED"}
        if terminal_status:
            for order_id in report_ids:
                buy_order_ids.discard(order_id)
        if is_buy_report and str(report.status or "").upper().strip() in {"CANCELED", "REJECTED"}:
            buy_settlement_confirm_pending = True
            _request_balance_refresh("buy_settlement_check", immediate=True)
            _send_journal(
                ctx,
                "exec_buy_settlement_check_requested",
                {"order_ids": sorted(report_ids), "status": str(report.status or "").upper().strip()},
            )
        try_put(ctx.q_exec_report, report)
        _send_journal(ctx, "exec_report", _exec_report_payload(report))
        source = str(meta.get("source", ""))
        if report.status == "FILLED" and source in {"openorders_ws", "reconcile"}:
            _track_fill_truth_pending(report.order_id, source, report.ts)

    def _resolve_order_side(payload: Dict[str, Any]) -> Optional[str]:
        side = str(payload.get("type", "")).strip().lower()
        if side in {"buy", "sell"}:
            return side
        descr = payload.get("descr")
        if isinstance(descr, dict):
            side = str(descr.get("type", "")).strip().lower()
            if side in {"buy", "sell"}:
                return side
        return None

    def _resolve_order_avg_price(payload: Dict[str, Any], vol_exec: float) -> float:
        price = _safe_float(payload.get("price"))
        if price is not None and price > 0.0:
            return float(price)
        cost = _safe_float(payload.get("cost"))
        if cost is not None and cost > 0.0 and vol_exec > 0.0:
            return float(cost / vol_exec)
        descr = payload.get("descr")
        if isinstance(descr, dict):
            limit_price = _safe_float(descr.get("price"))
            if limit_price is not None and limit_price > 0.0:
                return float(limit_price)
        return 0.0

    def _resolve_order_ts(payload: Dict[str, Any]) -> datetime:
        for field in ("closetm", "lastupdated", "opentm", "starttm"):
            ts = _safe_float(payload.get(field))
            if ts is None or ts <= 0.0:
                continue
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                continue
        return datetime.now(timezone.utc)

    def _emit_reconcile_fill_if_needed(order_id: str, payload: Dict[str, Any], vol_exec: Optional[float]) -> bool:
        exec_qty = max(0.0, float(vol_exec or 0.0))
        if not order_id or exec_qty <= 0.0:
            return False
        already_accounted = fill_tracker.filled_qty(order_id)
        missing_qty = max(0.0, exec_qty - already_accounted)
        if missing_qty <= 1e-12:
            return False
        side = _resolve_order_side(payload)
        if side is None:
            _send_journal(
                ctx,
                "exec_reconcile_fill_skipped",
                {
                    "order_id": order_id,
                    "reason": "missing_side",
                    "reported_vol_exec": exec_qty,
                    "accounted_qty_btc": already_accounted,
                },
            )
            return False
        avg_price = _resolve_order_avg_price(payload, exec_qty)
        if avg_price <= 0.0:
            _send_journal(
                ctx,
                "exec_reconcile_fill_skipped",
                {
                    "order_id": order_id,
                    "reason": "missing_price",
                    "reported_vol_exec": exec_qty,
                    "accounted_qty_btc": already_accounted,
                },
            )
            return False

        fee_total = max(0.0, float(_safe_float(payload.get("fee")) or 0.0))
        fee_accounted = fill_tracker.accounted_fee(order_id)
        fee_missing = max(0.0, fee_total - fee_accounted)
        if fee_total > 0.0 and exec_qty > 0.0:
            proportional_fee = fee_total * (missing_qty / exec_qty)
            if fee_missing > 0.0:
                fee_missing = min(fee_missing, proportional_fee)
            else:
                fee_missing = proportional_fee

        cumulative_filled, target_qty, fill_status = fill_tracker.record_synthetic_fill(order_id, missing_qty, fee_missing)
        fill_ts = _resolve_order_ts(payload)
        fill = Fill(
            ts=fill_ts,
            side=side,
            qty_btc=missing_qty,
            price=avg_price,
            fee_eur=fee_missing,
            order_id=order_id,
            slippage_bps=0.0,
            meta={"order_aliases": _aliases_for_order(order_id)},
        )
        try_put(ctx.q_exec_report, fill)
        _send_journal(
            ctx,
            "fill",
            {
                "ts": fill.ts.isoformat(),
                "order_id": fill.order_id,
                "side": fill.side,
                "qty_btc": fill.qty_btc,
                "price": fill.price,
                "fee_eur": fill.fee_eur,
                "slippage_bps": fill.slippage_bps,
                "fill_status": fill_status,
                "cum_filled_qty_btc": cumulative_filled,
                "target_qty_btc": target_qty,
                "source": "reconcile",
                "synthetic": True,
                "reported_vol_exec": exec_qty,
                "previous_accounted_qty_btc": already_accounted,
            },
        )
        _note_fill_for_account_sync(fill)
        _resolve_fill_truth_if_seen(order_id, "reconcile_fill")
        return True

    def _rate_limit_pause_from_error(exc: KrakenAPIError) -> float:
        base_pause = max(0.0, float(rate_limit_pause_sec))
        text = str(exc or "")
        now = time.time()

        m = _BINANCE_BAN_UNTIL_RE.search(text)
        if m:
            try:
                ban_until_ms = int(m.group(1))
                ban_delay = (ban_until_ms / 1000.0) - now
                if ban_delay > 0.0:
                    return max(base_pause, ban_delay + 1.0)
            except Exception:
                pass

        payload = getattr(exc, "payload", None)
        if isinstance(payload, dict):
            retry_after = payload.get("retryAfter")
            if retry_after is not None:
                try:
                    retry_raw = float(retry_after)
                    # Binance variants: absolute ms timestamp or small relative seconds.
                    if retry_raw > 1_000_000_000_000:
                        retry_delay = (retry_raw / 1000.0) - now
                    elif retry_raw > 1_000_000_000:
                        retry_delay = retry_raw - now
                    else:
                        retry_delay = retry_raw
                    if retry_delay > 0.0:
                        return max(base_pause, retry_delay + 1.0)
                except Exception:
                    pass

        low = text.lower()
        if "request weight per 1 minute" in low or "too much request weight" in low:
            return max(base_pause, 60.0)
        return base_pause

    def _pause_for_rate_limit(source: str, wait_s: float, forced_pause_s: float = 0.0) -> None:
        nonlocal accept_new, resume_after, last_rate_signal
        now = time.time()
        should_signal = False
        pause_seconds = max(0.0, float(wait_s), float(forced_pause_s), float(rate_limit_pause_sec))
        with run_state_lock:
            accept_new = False
            pause_until = now + pause_seconds
            resume_after = pause_until if resume_after is None else max(resume_after, pause_until)
            if (now - last_rate_signal) > 1.0:
                last_rate_signal = now
                should_signal = True
        _set_deadman_enabled(False, f"rate_limit:{source}")
        if should_signal:
            try_put(
                ctx.q_control_core,
                ControlCommand(
                    ts=datetime.now(timezone.utc),
                    action="PAUSE",
                    reason="rate_limit",
                    payload={
                        "resume_after_sec": pause_seconds,
                        "source": source,
                    },
                ),
            )
            _send_journal(
                ctx,
                "exec_rate_limit_pause",
                {
                    "source": source,
                    "wait_ms": round(wait_s * 1000.0, 3),
                    "resume_after_sec": pause_seconds,
                },
            )

    def _maybe_resume_from_rate_limit() -> None:
        nonlocal accept_new, resume_after
        should_resume = False
        with run_state_lock:
            if resume_after is not None and time.time() >= resume_after:
                accept_new = True
                resume_after = None
                should_resume = True
        if should_resume:
            _set_deadman_enabled(True, "rate_limit_resume")
            try_put(ctx.q_control_core, ControlCommand(ts=datetime.now(timezone.utc), action="RESUME", reason="rate_limit"))
            _send_journal(ctx, "exec_rate_limit_resume", {"reason": "budget_recovered"})

    def _rate_limit_remaining_sec() -> float:
        with run_state_lock:
            if resume_after is None:
                return 0.0
            return max(0.0, float(resume_after) - time.time())

    def _acquire_budget(source: str) -> float:
        nonlocal deadman_budget_wait_ms, deadman_rate_limit_hits, rate_limit_hits
        wait_s = rate_budget.acquire()
        if wait_s > 0:
            if source.startswith("deadman"):
                deadman_budget_wait_ms += wait_s * 1000.0
                deadman_rate_limit_hits += 1
            else:
                rate_limit_hits += 1
            _pause_for_rate_limit(source, wait_s)
        return wait_s

    def _load_open_orders_snapshot(snapshot_source: str) -> Dict[str, Any]:
        if adapter is None:
            return {}
        _acquire_budget(f"{snapshot_source}_open_orders")
        open_orders = adapter.open_orders()
        open_payload = open_orders.get("open", {})
        if isinstance(open_payload, dict):
            for txid, payload in open_payload.items():
                vol = _safe_float(payload.get("vol")) if isinstance(payload, dict) else None
                fill_tracker.set_target(str(txid), vol)
                with sm_lock:
                    order_sm.transition(str(txid), "OPEN", datetime.now(timezone.utc), allow_recovery=True)
                _track_known_txid(str(txid), "OPEN")
            _send_journal(
                ctx,
                "exec_recovery_snapshot",
                {"source": snapshot_source, "open_orders": len(open_payload)},
            )
        return open_orders

    last_balances: Dict[str, Any] = {}
    last_balances_updated_at: Optional[str] = None
    balances_refresh_sec = float(_cfg(cfg, "exec.balance_refresh_sec", 60.0))
    balances_nonzero_only = bool(_cfg(cfg, "exec.balance_nonzero_only", True))
    last_balances_refresh = 0.0
    balance_refresh_on_owntrades = bool(_cfg(cfg, "exec.balance_refresh_on_owntrades", True))
    balance_refresh_debounce_sec = float(_cfg(cfg, "exec.balance_refresh_debounce_sec", 30.0))
    sync_core_account_on_start = bool(_cfg(cfg, "exec.sync_core_account_on_start", True))
    # Keep account sync threshold independent from order sizing.
    # `order.min_trade_btc` is used by order placement and may be configured as a
    # strategy-level sizing guard (or even quote-like threshold in legacy configs).
    # Using it here can suppress valid external/manual positions on expensive pairs.
    sync_min_position_btc = max(
        0.0,
        float(_cfg(cfg, "exec.sync_min_position_btc", 0.0)),
    )
    sync_min_position_eur = max(0.0, float(_cfg(cfg, "exec.sync_min_position_eur", 0.0)))
    min_trade_btc = max(0.0, float(_cfg(cfg, "order.min_trade_btc", 0.0)))
    min_entry_notional_eur = max(0.0, float(_cfg(cfg, "exec.min_entry_notional_eur", 0.0)))
    sell_balance_buffer_btc = max(0.0, float(_cfg(cfg, "exec.sell_balance_buffer_btc", 0.0)))
    core_account_synced = False
    last_core_sync_cash_eur: Optional[float] = None
    last_core_sync_position_btc: Optional[float] = None
    last_core_sync_avg_entry_price: Optional[float] = None
    startup_flat_balance_needs_confirm = False
    buy_settlement_confirm_pending = False
    balance_req_lock = threading.Lock()
    balance_pending = False
    balance_pending_reason: Optional[str] = None
    balance_next_allowed_ts = 0.0
    sync_min_notional_eur_cache: Optional[float] = None

    def _core_account_sync_changed(cash_eur: float, position_btc: float, avg_entry_price: float) -> bool:
        if not core_account_synced:
            return True
        cash_prev = 0.0 if last_core_sync_cash_eur is None else float(last_core_sync_cash_eur)
        pos_prev = 0.0 if last_core_sync_position_btc is None else float(last_core_sync_position_btc)
        entry_prev = 0.0 if last_core_sync_avg_entry_price is None else float(last_core_sync_avg_entry_price)
        cash_tol = max(0.25, float(sync_min_position_eur), abs(cash_eur) * 0.005)
        pos_ref = max(abs(position_btc), abs(pos_prev), float(sync_min_position_btc), float(min_trade_btc), 1e-9)
        pos_tol = max(float(sync_min_position_btc), pos_ref * 0.01)
        entry_ref = max(abs(avg_entry_price), abs(entry_prev), 1e-9)
        entry_tol = max(1e-8, entry_ref * 0.0002)
        if abs(float(cash_eur) - cash_prev) > cash_tol:
            return True
        if abs(float(position_btc) - pos_prev) > pos_tol:
            return True
        if max(abs(position_btc), abs(pos_prev)) > max(1e-9, float(sync_min_position_btc)):
            if abs(float(avg_entry_price) - entry_prev) > entry_tol:
                return True
        return False

    def _note_fill_for_account_sync(fill: Fill) -> None:
        nonlocal last_core_sync_cash_eur
        nonlocal last_core_sync_position_btc
        nonlocal last_core_sync_avg_entry_price
        if mode != "live" or not core_account_synced:
            return
        cash_prev = 0.0 if last_core_sync_cash_eur is None else float(last_core_sync_cash_eur)
        pos_prev = 0.0 if last_core_sync_position_btc is None else float(last_core_sync_position_btc)
        entry_prev = 0.0 if last_core_sync_avg_entry_price is None else float(last_core_sync_avg_entry_price)
        cash_new, pos_new, entry_new = _apply_fill_to_sync_state(
            cash_prev,
            pos_prev,
            entry_prev,
            fill.side,
            fill.qty_btc,
            fill.price,
            fill.fee_eur,
        )
        last_core_sync_cash_eur = cash_new
        last_core_sync_position_btc = pos_new
        last_core_sync_avg_entry_price = entry_new

    def _emit_account_sync_delta_fill(source: str, delta: Dict[str, Any]) -> None:
        order_id = f"account_sync_delta_{int(time.time() * 1000)}"
        ts_iso = datetime.now(timezone.utc).isoformat()
        _send_journal(
            ctx,
            "exec_report",
            {
                "ts": ts_iso,
                "order_id": order_id,
                "status": "FILLED",
                "filled_qty_btc": float(delta["qty_btc"]),
                "avg_price": float(delta["price"]),
                "fee_eur": float(delta.get("fee_eur", 0.0) or 0.0),
                "latency_ms": 0.0,
                "reason": "account_sync_delta",
                "meta": {
                    "source": "account_sync_delta",
                    "synthetic": True,
                    "balance_sync_source": source,
                    "price_source": delta.get("price_source"),
                },
            },
        )
        _send_journal(
            ctx,
            "fill",
            {
                "ts": ts_iso,
                "order_id": order_id,
                "side": delta["side"],
                "qty_btc": float(delta["qty_btc"]),
                "price": float(delta["price"]),
                "fee_eur": float(delta.get("fee_eur", 0.0) or 0.0),
                "slippage_bps": 0.0,
                "fill_status": "FILLED",
                "cum_filled_qty_btc": float(delta["qty_btc"]),
                "target_qty_btc": float(delta["qty_btc"]),
                "source": "account_sync_delta",
                "synthetic": True,
                "balance_sync_source": source,
                "price_source": delta.get("price_source"),
                "cash_delta_eur": float(delta.get("cash_delta_eur", 0.0) or 0.0),
                "previous_cash_eur": float(delta.get("previous_cash_eur", 0.0) or 0.0),
                "cash_eur": float(delta.get("cash_eur", 0.0) or 0.0),
                "previous_position_btc": float(delta.get("previous_position_btc", 0.0) or 0.0),
                "position_btc": float(delta.get("position_btc", 0.0) or 0.0),
                "previous_avg_entry_price": float(delta.get("previous_avg_entry_price", 0.0) or 0.0),
                "avg_entry_price": float(delta.get("avg_entry_price", 0.0) or 0.0),
            },
        )

    def _maybe_sync_core_account(balances: Dict[str, Any], source: str) -> None:
        nonlocal core_account_synced
        nonlocal last_core_sync_cash_eur
        nonlocal last_core_sync_position_btc
        nonlocal last_core_sync_avg_entry_price
        nonlocal startup_flat_balance_needs_confirm
        nonlocal sync_min_notional_eur_cache
        if mode != "live":
            return
        if not sync_core_account_on_start:
            return
        quote_cash = _balance_for_asset(balances, pair_quote_asset, field="free")
        base_position = _balance_for_asset(balances, pair_base_asset, field="total")
        if quote_cash is None and base_position is None:
            _send_journal(
                ctx,
                "exec_core_account_sync_skipped",
                {
                    "reason": "missing_pair_balances",
                    "pair": pair_ws,
                    "base_asset": pair_base_asset,
                    "quote_asset": pair_quote_asset,
                    "source": source,
                },
            )
            return
        cash_eur = max(0.0, float(quote_cash or 0.0))
        position_btc = max(0.0, float(base_position or 0.0))
        if sync_min_position_btc > 0.0 and position_btc < sync_min_position_btc:
            position_btc = 0.0
        startup_flat_balance_needs_confirm = bool(source.startswith("startup") and position_btc <= 0.0)
        avg_entry_price = 0.0
        entry_reference_source: Optional[str] = None
        entry_reference_ts: Optional[str] = None
        if position_btc > 0.0:
            tol = max(
                1e-9,
                float(sync_min_position_btc),
                float(position_btc) * 0.01,
            )
            cached_avg_entry = max(0.0, float(last_core_sync_avg_entry_price or 0.0))
            prev_position = max(0.0, float(last_core_sync_position_btc or 0.0))
            source_norm = str(source or "").strip().lower()
            source_is_startup = source_norm.startswith("startup")
            source_is_owntrades_event = source_norm.startswith("event:owntrades")
            position_changed_vs_sync = (
                not core_account_synced
                or abs(position_btc - prev_position) > tol
            )
            # Avoid expensive history lookups on periodic refreshes when nothing changed.
            should_refresh_entry_reference = (
                source_is_startup
                or (position_changed_vs_sync and not source_is_owntrades_event)
                or cached_avg_entry <= 0.0
            )
            if not should_refresh_entry_reference and cached_avg_entry > 0.0:
                avg_entry_price = cached_avg_entry
                entry_reference_source = "cached_sync"
            else:
                replay = _load_entry_reference_from_journal(
                    cfg,
                    expected_position_btc=position_btc,
                    position_tolerance_btc=tol,
                    max_events=int(_cfg(cfg, "exec.restart_entry_ref_max_events", 200000)),
                )
                if replay is not None:
                    avg_entry_price = max(0.0, float(replay.get("avg_entry_price") or 0.0))
                    if avg_entry_price > 0.0:
                        entry_reference_source = "journal_fill_replay"
                        entry_reference_ts = str(replay.get("entry_ts") or "").strip() or None
                        _send_journal(
                            ctx,
                            "exec_entry_reference_recovered",
                            {
                                "source": source,
                                "entry_source": entry_reference_source,
                                "avg_entry_price": avg_entry_price,
                                "position_btc": position_btc,
                                "replayed_position_btc": float(replay.get("position_btc") or 0.0),
                                "fills_replayed": int(float(replay.get("fills_replayed") or 0.0)),
                                "entry_ts": entry_reference_ts,
                                "position_tolerance_btc": tol,
                            },
                        )
                if avg_entry_price <= 0.0:
                    replay = _load_entry_reference_from_exchange_history(
                        adapter,
                        symbol=to_binance_symbol(pair_ws),
                        expected_position_btc=position_btc,
                        position_tolerance_btc=tol,
                        max_trades=int(_cfg(cfg, "exec.restart_entry_ref_max_exchange_trades", 1000)),
                    )
                    if replay is not None:
                        avg_entry_price = max(0.0, float(replay.get("avg_entry_price") or 0.0))
                        if avg_entry_price > 0.0:
                            entry_reference_source = "exchange_trade_history"
                            entry_reference_ts = str(replay.get("entry_ts") or "").strip() or None
                            _send_journal(
                                ctx,
                                "exec_entry_reference_recovered",
                                {
                                    "source": source,
                                    "entry_source": entry_reference_source,
                                    "avg_entry_price": avg_entry_price,
                                    "position_btc": position_btc,
                                    "replayed_position_btc": float(replay.get("position_btc") or 0.0),
                                    "fills_replayed": int(float(replay.get("fills_replayed") or 0.0)),
                                    "entry_ts": entry_reference_ts,
                                    "position_tolerance_btc": tol,
                                },
                            )
        if (
            sync_min_position_eur > 0.0
            and avg_entry_price > 0.0
            and (position_btc * avg_entry_price) < sync_min_position_eur
            ):
                _send_journal(
                    ctx,
                    "exec_core_account_sync_dust_notional",
                    {
                        "pair": pair_ws,
                        "base_asset": pair_base_asset,
                        "position_btc": position_btc,
                        "avg_entry_price": avg_entry_price,
                        "position_notional_eur": float(position_btc * avg_entry_price),
                        "sync_min_position_eur": float(sync_min_position_eur),
                        "source": source,
                    },
                )
                position_btc = 0.0
                avg_entry_price = 0.0
                entry_reference_source = None
        if core_account_synced:
            open_orders_count = 0
            try:
                open_orders_count = int(order_sm.open_orders_count())
            except Exception:
                open_orders_count = 0
            cash_prev = 0.0 if last_core_sync_cash_eur is None else float(last_core_sync_cash_eur)
            pos_prev = 0.0 if last_core_sync_position_btc is None else float(last_core_sync_position_btc)
            pos_ref = max(abs(position_btc), abs(pos_prev), float(sync_min_position_btc), float(min_trade_btc), 1e-9)
            pos_tol = max(float(sync_min_position_btc), pos_ref * 0.01)
            cash_tol = max(0.25, float(sync_min_position_eur), abs(cash_eur) * 0.005)
            if open_orders_count <= 0:
                if sync_min_notional_eur_cache is None:
                    sync_min_notional_eur_cache = max(0.0, float(sync_min_position_eur))
                    try:
                        if adapter is not None:
                            sync_min_notional_eur_cache = max(
                                float(sync_min_notional_eur_cache),
                                float(adapter.min_notional(pair_rest) or 0.0),
                            )
                    except Exception:
                        pass
                delta_fill = _infer_account_sync_delta_fill(
                    previous_cash_eur=last_core_sync_cash_eur,
                    previous_position_btc=last_core_sync_position_btc,
                    previous_avg_entry_price=last_core_sync_avg_entry_price,
                    cash_eur=cash_eur,
                    position_btc=position_btc,
                    avg_entry_price=avg_entry_price,
                    position_tolerance_btc=pos_tol,
                    cash_tolerance_eur=cash_tol,
                    min_notional_eur=float(sync_min_notional_eur_cache or 0.0),
                )
                if delta_fill is not None:
                    _emit_account_sync_delta_fill(source, delta_fill)
            else:
                _send_journal(
                    ctx,
                    "exec_core_account_sync_delta_skipped",
                    {
                        "reason": "open_orders_present",
                        "open_orders_count": open_orders_count,
                        "source": source,
                    },
                )
        if not _core_account_sync_changed(cash_eur, position_btc, avg_entry_price):
            return
        cmd = ControlCommand(
            ts=datetime.now(timezone.utc),
            action="SYNC_ACCOUNT",
            reason="exec_balance_sync",
            payload={
                "pair": pair_ws,
                "base_asset": pair_base_asset,
                "quote_asset": pair_quote_asset,
                "cash_eur": cash_eur,
                "position_btc": position_btc,
                "avg_entry_price": avg_entry_price,
                "entry_ts": entry_reference_ts,
                "reset": True,
                "source": source,
            },
        )
        try_put(ctx.q_control_core, cmd)
        core_account_synced = True
        last_core_sync_cash_eur = float(cash_eur)
        last_core_sync_position_btc = float(position_btc)
        last_core_sync_avg_entry_price = float(avg_entry_price)
        _send_journal(
            ctx,
            "exec_core_account_sync",
            {
                "pair": pair_ws,
                "base_asset": pair_base_asset,
                "quote_asset": pair_quote_asset,
                "cash_eur": cash_eur,
                "position_btc": position_btc,
                "avg_entry_price": avg_entry_price,
                "entry_ts": entry_reference_ts,
                "entry_reference_source": entry_reference_source,
                "source": source,
            },
        )

    def _refresh_balances(source: str) -> None:
        nonlocal last_balances, last_balances_updated_at, last_balances_refresh
        if adapter is None:
            return
        # Tests and alternative adapters may not implement Balance.
        if not hasattr(adapter, "balance"):
            return
        if _rate_limit_remaining_sec() > 0.0:
            return
        _acquire_budget(f"{source}_balance")
        try:
            bal = adapter.balance()  # type: ignore[attr-defined]
            if not isinstance(bal, dict):
                bal = {}
            if balances_nonzero_only:
                filtered: Dict[str, Any] = {}
                for k, v in bal.items():
                    if isinstance(v, dict):
                        non_zero = False
                        for candidate in ("free", "locked", "total"):
                            numeric = _safe_float(v.get(candidate))
                            if numeric is not None and numeric != 0.0:
                                non_zero = True
                                break
                        if non_zero:
                            filtered[str(k)] = v
                        continue
                    try:
                        if float(v) != 0.0:
                            filtered[str(k)] = v
                    except Exception:
                        filtered[str(k)] = v
                bal = filtered

            last_balances = bal
            last_balances_updated_at = datetime.now(timezone.utc).isoformat()
            last_balances_refresh = time.time()
            _maybe_sync_core_account(bal, source)

            _send_journal(
                ctx,
                "exec_balance_snapshot",
                {
                    "source": source,
                    "count": len(bal),
                    "balances": bal,
                    "updated_at": last_balances_updated_at,
                },
            )
            # Control-plane retains the latest exec telemetry dict, so sending the full balance
            # snapshot is ok (and avoids creating a separate endpoint).
            _send_telemetry(
                ctx,
                {
                    "balances": bal,
                    "balances_updated_at": last_balances_updated_at,
                    "balances_count": len(bal),
                },
            )
        except KrakenAPIError as exc:
            last_balances_refresh = time.time()
            if exc.is_rate_limit():
                _pause_for_rate_limit(
                    f"{source}_balance_api",
                    0.0,
                    _rate_limit_pause_from_error(exc),
                )
            _send_journal(
                ctx,
                "exec_balance_error",
                {
                    "source": source,
                    "error": str(exc),
                    "is_rate_limit": exc.is_rate_limit(),
                    "is_auth": exc.is_auth(),
                    "is_transient": exc.is_transient(),
                },
            )
        except Exception as exc:
            last_balances_refresh = time.time()
            _send_journal(ctx, "exec_balance_error", {"source": source, "error": str(exc)})

    def _request_balance_refresh(reason: str, *, immediate: bool = False) -> None:
        nonlocal balance_pending, balance_pending_reason, balance_next_allowed_ts
        if adapter is None:
            return
        if not hasattr(adapter, "balance"):
            return
        with balance_req_lock:
            balance_pending = True
            balance_pending_reason = str(reason or "event")
            if immediate:
                balance_next_allowed_ts = 0.0

    if deadman_adapter is not None:
        if canary:
            try:
                _acquire_budget("deadman_canary_disable")
                resp = deadman_adapter.cancel_all_orders_after(0)
                _send_journal(
                    ctx,
                    "deadman",
                    {"event": "disable", "timeout": 0, "reason": "canary", "response": resp},
                )
            except Exception as exc:
                _send_journal(ctx, "deadman_error", {"error": str(exc)})

        def _deadman_worker() -> None:
            last_enabled = False
            last_tick = 0.0
            while not ctx.stop_event.is_set():
                with deadman_lock:
                    enabled = deadman_state["enabled"]
                    disable_reason = str(deadman_state.get("disable_reason", "disabled"))
                    disable_pending = bool(deadman_state.get("disable_pending", False))
                now = time.time()
                if enabled:
                    if now - last_tick >= deadman_tick:
                        try:
                            _acquire_budget("deadman_tick")
                            resp = deadman_adapter.cancel_all_orders_after(deadman_timeout)
                            _send_journal(
                                ctx,
                                "deadman",
                                {"event": "tick", "timeout": deadman_timeout, "response": resp},
                            )
                        except KrakenAPIError as exc:
                            if exc.is_rate_limit():
                                _pause_for_rate_limit("deadman_api", 0.0, _rate_limit_pause_from_error(exc))
                            _send_journal(
                                ctx,
                                "deadman_error",
                                {
                                    "error": str(exc),
                                    "is_rate_limit": exc.is_rate_limit(),
                                    "is_auth": exc.is_auth(),
                                    "is_transient": exc.is_transient(),
                                },
                            )
                        except Exception as exc:
                            _send_journal(ctx, "deadman_error", {"error": str(exc)})
                        last_tick = now
                    else:
                        time.sleep(min(0.1, max(deadman_tick / 4.0, 0.01)))
                else:
                    if last_enabled or disable_pending:
                        try:
                            _acquire_budget("deadman_disable")
                            resp = deadman_adapter.cancel_all_orders_after(0)
                            with deadman_lock:
                                deadman_state["disable_pending"] = False
                            _send_journal(
                                ctx,
                                "deadman",
                                {"event": "disable", "timeout": 0, "reason": disable_reason, "response": resp},
                            )
                        except KrakenAPIError as exc:
                            if exc.is_rate_limit():
                                _pause_for_rate_limit("deadman_disable_api", 0.0, _rate_limit_pause_from_error(exc))
                            _send_journal(
                                ctx,
                                "deadman_error",
                                {
                                    "error": str(exc),
                                    "is_rate_limit": exc.is_rate_limit(),
                                    "is_auth": exc.is_auth(),
                                    "is_transient": exc.is_transient(),
                                },
                            )
                        except Exception as exc:
                            _send_journal(ctx, "deadman_error", {"error": str(exc)})
                        last_tick = now
                    time.sleep(0.1)
                last_enabled = enabled

        dm = threading.Thread(target=_deadman_worker, daemon=True)
        dm.start()
    elif mode == "live":
        _send_journal(
            ctx,
            "deadman_unavailable",
            {"exchange": exchange, "reason": "adapter_without_deadman"},
        )

    if adapter is not None:
        try:
            _load_open_orders_snapshot("startup")
        except KrakenAPIError as exc:
            if exc.is_rate_limit():
                _pause_for_rate_limit("startup_open_orders_api", 0.0, _rate_limit_pause_from_error(exc))
            _send_journal(
                ctx,
                "exec_error",
                {
                    "error": str(exc),
                    "is_rate_limit": exc.is_rate_limit(),
                    "is_auth": exc.is_auth(),
                    "is_transient": exc.is_transient(),
                },
            )
        except Exception as exc:
            _send_journal(ctx, "exec_error", {"error": str(exc), "kind": "startup_snapshot"})

        try:
            _refresh_balances("startup")
        except Exception:
            pass

        use_private_ws = bool(_cfg(cfg, "exec.private_ws_enabled", True))
        if use_private_ws:
            if exchange == "kraken":
                def _open_orders_worker() -> None:
                    backoff = 1.0
                    ws_url = _cfg(cfg, "exec.ws_auth_url", _cfg(cfg, "live.ws_auth_url", "wss://ws-auth.kraken.com"))
                    while not ctx.stop_event.is_set():
                        try:
                            ws = OpenOrdersWS(adapter, url=ws_url)

                            async def _run() -> None:
                                async for update in ws.stream():
                                    mapped = _map_status(update.status, update.vol, update.vol_exec)
                                    fill_tracker.set_target(update.order_id, update.vol)
                                    with sm_lock:
                                        order_sm.transition(update.order_id, mapped, update.ts, allow_recovery=True)
                                    _track_known_txid(update.order_id, mapped)
                                    report = ExecutionReport(
                                        ts=update.ts,
                                        order_id=update.order_id,
                                        status=mapped,
                                        filled_qty_btc=float(update.vol_exec or 0.0),
                                        avg_price=0.0,
                                        fee_eur=0.0,
                                        latency_ms=0.0,
                                        meta={"source": "openorders_ws"},
                                    )
                                    _emit_exec_report(report)

                            asyncio.run(_run())
                            backoff = 1.0
                        except Exception as exc:
                            _send_journal(ctx, "exec_ws_error", {"error": str(exc), "worker": "openorders"})
                            time.sleep(backoff)
                            backoff = min(30.0, backoff * 2.0)

                t = threading.Thread(target=_open_orders_worker, daemon=True)
                t.start()

                def _own_trades_worker() -> None:
                    backoff = 1.0
                    ws_url = _cfg(cfg, "exec.ws_auth_url", _cfg(cfg, "live.ws_auth_url", "wss://ws-auth.kraken.com"))
                    while not ctx.stop_event.is_set():
                        try:
                            ws = OwnTradesWS(adapter, url=ws_url)

                            async def _run() -> None:
                                async for update in ws.stream():
                                    _handle_own_trade(
                                        ctx,
                                        update,
                                        order_sm,
                                        sm_lock,
                                        own_trades_deduper,
                                        fill_tracker,
                                        resolve_order_aliases=_aliases_for_order,
                                        on_fill_observed=_note_fill_for_account_sync,
                                    )
                                    _resolve_fill_truth_if_seen(update.order_id, "owntrades")
                                    if balance_refresh_on_owntrades:
                                        _request_balance_refresh("owntrades")

                            asyncio.run(_run())
                            backoff = 1.0
                        except Exception as exc:
                            _send_journal(ctx, "exec_owntrades_error", {"error": str(exc), "worker": "owntrades"})
                            time.sleep(backoff)
                            backoff = min(30.0, backoff * 2.0)

                ot = threading.Thread(target=_own_trades_worker, daemon=True)
                ot.start()
            elif exchange == "binance" and isinstance(adapter, BinanceRestClient):
                def _binance_user_stream_worker() -> None:
                    backoff = 1.0
                    ws_url = _cfg(
                        cfg,
                        "exec.ws_auth_url",
                        _cfg(cfg, "live.ws_auth_url", "wss://stream.binance.com:9443"),
                    )
                    keepalive_sec = float(_cfg(cfg, "exec.private_ws_keepalive_sec", 30 * 60))
                    while not ctx.stop_event.is_set():
                        try:
                            ws = BinanceUserDataWS(adapter, url=ws_url, keepalive_sec=keepalive_sec)

                            async def _run() -> None:
                                async for update in ws.stream():
                                    if not isinstance(update, BinanceExecutionUpdate):
                                        continue
                                    if not update.order_id:
                                        continue
                                    mapped = _map_binance_exec_status(update.status)
                                    if update.orig_qty > 0.0:
                                        fill_tracker.set_target(update.order_id, update.orig_qty)
                                    if update.client_order_id:
                                        _register_txid_alias(update.order_id, update.client_order_id)
                                    aliases = _transition_order_and_aliases(update.order_id, mapped, update.ts)
                                    _track_known_txid(update.order_id, mapped)
                                    avg_price = 0.0
                                    if update.cum_qty > 0.0 and update.cum_quote > 0.0:
                                        avg_price = float(update.cum_quote / update.cum_qty)
                                    report_meta: Dict[str, Any] = {
                                        "source": "openorders_ws",
                                        "exchange": "binance",
                                        "exec_type": update.exec_type,
                                    }
                                    if update.trade_id:
                                        report_meta["trade_id"] = update.trade_id
                                    if update.event_id:
                                        report_meta["event_id"] = update.event_id
                                    if aliases:
                                        report_meta["order_aliases"] = aliases
                                    report = ExecutionReport(
                                        ts=update.ts,
                                        order_id=update.order_id,
                                        status=mapped,
                                        filled_qty_btc=float(update.cum_qty or 0.0),
                                        avg_price=avg_price,
                                        fee_eur=float(update.fee_quote if update.exec_type == "TRADE" else 0.0),
                                        latency_ms=0.0,
                                        meta=report_meta,
                                    )
                                    _emit_exec_report(report)

                                    if update.exec_type == "TRADE" and update.last_qty > 0.0:
                                        fill_price = update.last_price
                                        if fill_price <= 0.0:
                                            fill_price = avg_price
                                        trade_id = update.trade_id or f"{update.order_id}:{update.event_id or ''}"
                                        own_trade = OwnTradeUpdate(
                                            ts=update.ts,
                                            trade_id=str(trade_id),
                                            order_id=update.order_id,
                                            side=update.side,
                                            price=float(fill_price or 0.0),
                                            vol=float(update.last_qty),
                                            fee=float(update.fee_quote),
                                            pair=update.pair,
                                            event_id=update.event_id,
                                        )
                                        _handle_own_trade(
                                            ctx,
                                            own_trade,
                                            order_sm,
                                            sm_lock,
                                            own_trades_deduper,
                                            fill_tracker,
                                            resolve_order_aliases=_aliases_for_order,
                                            on_fill_observed=_note_fill_for_account_sync,
                                        )
                                        _resolve_fill_truth_if_seen(update.order_id, "owntrades")
                                        if balance_refresh_on_owntrades:
                                            _request_balance_refresh("owntrades")

                            asyncio.run(_run())
                            backoff = 1.0
                        except Exception as exc:
                            _send_journal(ctx, "exec_owntrades_error", {"error": str(exc), "worker": "binance_user_ws"})
                            time.sleep(backoff)
                            backoff = min(30.0, backoff * 2.0)

                bu = threading.Thread(target=_binance_user_stream_worker, daemon=True)
                bu.start()
            else:
                _send_journal(
                    ctx,
                    "exec_private_ws_disabled",
                    {"exchange": exchange, "reason": "unsupported_exchange"},
                )
        else:
            _send_journal(
                ctx,
                "exec_private_ws_disabled",
                {"exchange": exchange, "reason": "polling_reconcile_only"},
            )

        def _reconcile_worker() -> None:
            interval = float(_cfg(cfg, "exec.reconcile_interval_sec", 30.0))
            while not ctx.stop_event.is_set():
                remaining = _rate_limit_remaining_sec()
                if remaining > 0.0:
                    time.sleep(min(interval, max(0.25, min(2.0, remaining))))
                    continue
                try:
                    _acquire_budget("reconcile_open_orders")
                    open_orders = adapter.open_orders()
                    open_payload = open_orders.get("open", {})
                    open_txids = set(open_payload.keys()) if isinstance(open_payload, dict) else set()
                    if isinstance(open_payload, dict):
                        for txid, payload in open_payload.items():
                            vol = _safe_float(payload.get("vol")) if isinstance(payload, dict) else None
                            fill_tracker.set_target(str(txid), vol)
                            _track_known_txid(str(txid), "OPEN")
                            with sm_lock:
                                order_sm.transition(str(txid), "OPEN", datetime.now(timezone.utc), allow_recovery=True)

                    closed = list(_known_snapshot() - open_txids)
                    if closed:
                        _acquire_budget("reconcile_query_orders")
                        result = adapter.query_orders(",".join(sorted(closed)))
                        for txid, payload in result.items():
                            status = str(payload.get("status", "")).lower()
                            vol = _safe_float(payload.get("vol"))
                            vol_exec = _safe_float(payload.get("vol_exec"))
                            fill_tracker.set_target(txid, vol)
                            mapped = _map_status(status, vol, vol_exec)
                            now_ts = datetime.now(timezone.utc)
                            aliases = _transition_order_and_aliases(txid, mapped, now_ts)
                            _emit_reconcile_fill_if_needed(txid, payload, vol_exec)
                            reconcile_avg_price = _resolve_order_avg_price(payload, float(vol_exec or 0.0))
                            reconcile_fee = max(0.0, float(_safe_float(payload.get("fee")) or 0.0))
                            report_meta: Dict[str, Any] = {"source": "reconcile"}
                            if aliases:
                                report_meta["order_aliases"] = aliases
                            report = ExecutionReport(
                                ts=now_ts,
                                order_id=txid,
                                status=mapped,
                                filled_qty_btc=float(vol_exec or 0.0),
                                avg_price=float(reconcile_avg_price),
                                fee_eur=float(reconcile_fee),
                                latency_ms=0.0,
                                meta=report_meta,
                            )
                            _emit_exec_report(report)
                            _track_known_txid(txid, mapped)
                            _resolve_fill_truth_if_seen(txid, "reconcile")
                except KrakenAPIError as exc:
                    if exc.is_rate_limit():
                        _pause_for_rate_limit("reconcile_api", 0.0, _rate_limit_pause_from_error(exc))
                    _send_journal(
                        ctx,
                        "exec_reconcile_error",
                        {
                            "error": str(exc),
                            "is_rate_limit": exc.is_rate_limit(),
                            "is_auth": exc.is_auth(),
                            "is_transient": exc.is_transient(),
                        },
                    )
                except Exception as exc:
                    _send_journal(ctx, "exec_reconcile_error", {"error": str(exc), "kind": "unexpected"})
                time.sleep(interval)

        r = threading.Thread(target=_reconcile_worker, daemon=True)
        r.start()

    _set_deadman_enabled(accept_new, "startup")

    pending_intent: OrderIntent | None = None

    while not ctx.stop_event.is_set():
        did_work = False
        # control commands
        try:
            cmd: ControlCommand = ctx.q_control_exec.get_nowait()
            did_work = True
            if cmd.action in {"STOP", "PAUSE"}:
                _set_accept_new(False, cmd.reason or cmd.action.lower())
            elif cmd.action in {"START", "RESUME"}:
                _set_accept_new(True, cmd.reason or cmd.action.lower())
            elif cmd.action == "CANCEL_ALL":
                canceled = order_sm.cancel_all(datetime.now(timezone.utc))
                canceled_buy_ids = sorted(order_id for order_id in canceled if order_id in buy_order_ids)
                if canceled_buy_ids:
                    buy_settlement_confirm_pending = True
                    _request_balance_refresh("cancel_all_buy_settlement_check", immediate=True)
                    _send_journal(
                        ctx,
                        "exec_buy_settlement_check_requested",
                        {"order_ids": canceled_buy_ids, "status": "CANCEL_ALL"},
                    )
                if simulator is not None:
                    simulator.cancel_all()
                if adapter is not None:
                    try:
                        _acquire_budget("cancel_all")
                        adapter.cancel_all()
                    except KrakenAPIError as exc:
                        if exc.is_rate_limit():
                            _pause_for_rate_limit("cancel_all_api", 0.0, _rate_limit_pause_from_error(exc))
                        _send_journal(
                            ctx,
                            "exec_error",
                            {
                                "error": str(exc),
                                "is_rate_limit": exc.is_rate_limit(),
                                "is_auth": exc.is_auth(),
                                "is_transient": exc.is_transient(),
                            },
                        )
                    except Exception as exc:
                        _send_journal(ctx, "exec_error", {"error": str(exc), "kind": "cancel_all"})
                for order_id in canceled:
                    _track_known_txid(order_id, "CANCELED")
                    report = ExecutionReport(
                        ts=datetime.now(timezone.utc),
                        order_id=order_id,
                        status="CANCELED",
                        filled_qty_btc=0.0,
                        avg_price=0.0,
                        fee_eur=0.0,
                        latency_ms=0.0,
                        reason=cmd.reason,
                    )
                    _emit_exec_report(report)
            elif cmd.action == "SET_BUDGET":
                # Budget resets are intended for sim/paper: stop accepting new intents, cancel everything,
                # and drop any queued intents so the next START begins from a clean slate.
                _set_accept_new(False, cmd.reason or "budget_reset")
                canceled = order_sm.cancel_all(datetime.now(timezone.utc))
                canceled_buy_ids = sorted(order_id for order_id in canceled if order_id in buy_order_ids)
                if canceled_buy_ids:
                    buy_settlement_confirm_pending = True
                    _request_balance_refresh("budget_reset_buy_settlement_check", immediate=True)
                    _send_journal(
                        ctx,
                        "exec_buy_settlement_check_requested",
                        {"order_ids": canceled_buy_ids, "status": "BUDGET_RESET"},
                    )
                if simulator is not None:
                    simulator.cancel_all()
                if adapter is not None:
                    try:
                        _acquire_budget("cancel_all")
                        adapter.cancel_all()
                    except Exception:
                        # Best-effort; errors are already handled in normal CANCEL_ALL path.
                        pass

                pending_intent = None
                dropped = 0
                while True:
                    try:
                        _ = ctx.q_order_intent.get_nowait()
                        dropped += 1
                    except Empty:
                        break
                _send_journal(
                    ctx,
                    "exec_intents_dropped",
                    {"count": dropped, "reason": cmd.reason or "budget_reset", "canceled_orders": len(canceled)},
                )
        except Empty:
            pass

        _maybe_resume_from_rate_limit()
        _check_fill_truth_gaps()

        # order intents
        try:
            if pending_intent is not None:
                intent = pending_intent
                pending_intent = None
            else:
                intent = ctx.q_order_intent.get_nowait()
            did_work = True
            if intent.client_id and intent.client_id in seen_client_ids:
                _send_journal(ctx, "exec_intent_dedup_skip", {"client_id": intent.client_id})
                continue
            with run_state_lock:
                can_accept = accept_new
            is_emergency_exit = False
            try:
                if isinstance(getattr(intent, "meta", None), dict):
                    is_emergency_exit = bool(intent.meta.get("emergency_exit", False))
            except Exception:
                is_emergency_exit = False
            if not can_accept:
                # Always allow explicit emergency exits while stopped/paused so hard-stop flatten can execute.
                if not is_emergency_exit:
                    # Do not silently drop intents while paused (e.g. rate limit). Keep it and retry.
                    pending_intent = intent
                    time.sleep(0.05)
                    continue
            order_id = intent.client_id or f"order_{int(time.time()*1000)}"
            order_recv_ts[order_id] = time.time()

            _acquire_budget("order_intent")

            if adapter is not None and str(intent.side or "").lower() == "buy":
                if startup_flat_balance_needs_confirm:
                    _refresh_balances("pre_buy_confirm")
                    startup_flat_balance_needs_confirm = False
                if buy_settlement_confirm_pending:
                    _refresh_balances("pre_buy_settlement_confirm")
                    buy_settlement_confirm_pending = False
                exchange_base_position = _balance_for_asset(last_balances, pair_base_asset, field="total")
                exchange_base_position = max(0.0, float(exchange_base_position or 0.0))
                existing_position_threshold = max(1e-9, float(sync_min_position_btc), float(min_trade_btc))
                if exchange_base_position >= existing_position_threshold:
                    _maybe_sync_core_account(last_balances, "buy_existing_balance_guard")
                    _send_journal(
                        ctx,
                        "exec_buy_skipped_existing_balance",
                        {
                            "order_id": order_id,
                            "pair": pair_ws,
                            "base_asset": pair_base_asset,
                            "exchange_position_btc": exchange_base_position,
                            "threshold_btc": existing_position_threshold,
                            "requested_qty_btc": float(intent.qty_btc),
                            "balances_updated_at": last_balances_updated_at,
                        },
                    )
                    continue

            order = Order(
                ts=intent.ts,
                side=intent.side,
                qty_btc=intent.qty_btc,
                order_type=intent.order_type,
                price=intent.limit_price,
                post_only=intent.post_only,
                id=order_id,
            )
            is_buy_order = str(order.side or "").lower() == "buy"
            if is_buy_order:
                buy_order_ids.add(order_id)
            order_reference_price = _intent_reference_price(intent, adapter=adapter, symbol=pair_rest)
            exchange_min_notional = 0.0
            if isinstance(adapter, BinanceRestClient):
                exchange_min_notional = max(0.0, float(adapter.min_notional(pair_rest) or 0.0))
            if adapter is not None and str(order.side or "").lower() == "buy":
                effective_min_entry_notional = _effective_min_entry_notional(
                    configured_min_notional=min_entry_notional_eur,
                    exchange_min_notional=exchange_min_notional,
                )
                order_notional = max(0.0, float(order.qty_btc) * float(order_reference_price))
                if (
                    effective_min_entry_notional > 0.0
                    and order_reference_price > 0.0
                    and order_notional + 1e-9 < effective_min_entry_notional
                ):
                    _send_journal(
                        ctx,
                        "exec_buy_skipped_below_min_notional",
                        {
                            "order_id": order_id,
                            "pair": pair_ws,
                            "requested_qty_btc": float(order.qty_btc),
                            "reference_price": float(order_reference_price),
                            "order_notional_eur": float(order_notional),
                            "configured_min_entry_notional_eur": float(min_entry_notional_eur),
                            "exchange_min_notional_eur": float(exchange_min_notional),
                            "effective_min_entry_notional_eur": float(effective_min_entry_notional),
                        },
                    )
                    continue
            if adapter is not None and str(order.side or "").lower() == "sell":
                available_base = _balance_for_asset(last_balances, pair_base_asset, field="free")
                if available_base is not None:
                    sell_balance_refresh_attempts = 0
                    try:
                        if isinstance(getattr(intent, "meta", None), dict):
                            sell_balance_refresh_attempts = int(intent.meta.get("sell_balance_refresh_attempts", 0) or 0)
                    except Exception:
                        sell_balance_refresh_attempts = 0
                    if (
                        _should_retry_sell_after_balance_refresh(
                            requested_qty=float(order.qty_btc),
                            available_qty=float(available_base),
                            reference_price=float(order_reference_price),
                            min_notional=float(exchange_min_notional),
                            min_trade_qty=float(min_trade_btc),
                        )
                        and sell_balance_refresh_attempts < 1
                    ):
                        pending_intent = _clone_intent_with_meta(
                            intent,
                            sell_balance_refresh_attempts=sell_balance_refresh_attempts + 1,
                        )
                        _send_journal(
                            ctx,
                            "exec_sell_balance_refresh_retry",
                            {
                                "order_id": order_id,
                                "pair": pair_ws,
                                "base_asset": pair_base_asset,
                                "requested_qty_btc": float(order.qty_btc),
                                "available_qty_btc": float(available_base),
                                "reference_price": float(order_reference_price),
                                "exchange_min_notional_eur": float(exchange_min_notional),
                                "retry_attempt": int(sell_balance_refresh_attempts + 1),
                                "reason": "stale_sell_balance_guard",
                            },
                        )
                        _request_balance_refresh("sell_stale_balance_guard")
                        _refresh_balances("sell_stale_balance_guard")
                        time.sleep(0.05)
                        continue
                    original_qty = float(order.qty_btc)
                    adjusted_qty, applied_buffer = _adjust_sell_qty_for_balance(
                        requested_qty=original_qty,
                        available_qty=float(available_base),
                        buffer_qty=sell_balance_buffer_btc,
                        reference_price=order_reference_price,
                        min_notional=exchange_min_notional,
                    )
                    if original_qty > adjusted_qty + 1e-12:
                        original_qty = float(order.qty_btc)
                        order.qty_btc = adjusted_qty
                        _send_journal(
                            ctx,
                            "exec_sell_qty_clamped_to_balance",
                            {
                                "order_id": order_id,
                                "pair": pair_ws,
                                "base_asset": pair_base_asset,
                                "requested_qty_btc": original_qty,
                                "available_qty_btc": float(available_base),
                                "buffer_btc": float(applied_buffer),
                                "clamped_qty_btc": float(order.qty_btc),
                                "reference_price": float(order_reference_price),
                                "exchange_min_notional_eur": float(exchange_min_notional),
                            },
                        )
                    sell_notional = max(0.0, float(order.qty_btc) * float(order_reference_price))
                    available_notional = max(0.0, float(available_base) * float(order_reference_price))
                    if (
                        exchange_min_notional > 0.0
                        and order_reference_price > 0.0
                        and sell_notional + 1e-9 < exchange_min_notional
                        and available_notional + 1e-9 < exchange_min_notional
                    ):
                        _send_journal(
                            ctx,
                            "exec_sell_skipped_below_min_notional",
                            {
                                "order_id": order_id,
                                "pair": pair_ws,
                                "base_asset": pair_base_asset,
                                "requested_qty_btc": float(intent.qty_btc),
                                "available_qty_btc": float(available_base),
                                "reference_price": float(order_reference_price),
                                "requested_notional_eur": float(sell_notional),
                                "available_notional_eur": float(available_notional),
                                "exchange_min_notional_eur": float(exchange_min_notional),
                            },
                        )
                        _request_balance_refresh("sell_below_min_notional")
                        continue
                    if float(order.qty_btc) < max(1e-12, min_trade_btc):
                        _send_journal(
                            ctx,
                            "exec_sell_skipped_insufficient_available",
                            {
                                "order_id": order_id,
                                "pair": pair_ws,
                                "base_asset": pair_base_asset,
                                "requested_qty_btc": float(intent.qty_btc),
                                "available_qty_btc": float(available_base),
                                "buffer_btc": float(sell_balance_buffer_btc),
                                "min_trade_btc": float(min_trade_btc),
                            },
                        )
                        _request_balance_refresh("sell_qty_below_min_after_clamp")
                        continue

            with sm_lock:
                order_sm.transition(order_id, "NEW", datetime.now(timezone.utc))
                order_sm.transition(order_id, "ACK", datetime.now(timezone.utc))
            fill_tracker.set_target(order_id, order.qty_btc)
            exchange_order_id: Optional[str] = None

            if simulator is not None:
                simulator.submit([order])
                with sm_lock:
                    order_sm.transition(order_id, "OPEN", datetime.now(timezone.utc))
            elif adapter is not None:
                try:
                    result = adapter.add_order(
                        pair=pair_rest,
                        side=order.side,
                        order_type=order.order_type,
                        volume=str(order.qty_btc),
                        price=str(order.price) if order.price is not None else None,
                        cl_ord_id=_kraken_cl_ord_id(order.id or order_id),
                        post_only=order.post_only,
                        validate=canary,
                    )
                    if not canary:
                        with sm_lock:
                            order_sm.transition(order_id, "OPEN", datetime.now(timezone.utc))
                        txids = result.get("txid", [])
                        if txids:
                            txid = str(txids[0])
                            exchange_order_id = txid
                            _register_txid_alias(txid, order_id)
                            _track_known_txid(txid, "OPEN")
                            if is_buy_order:
                                buy_order_ids.add(txid)
                            fill_tracker.set_target(txid, order.qty_btc)
                            with sm_lock:
                                order_sm.transition(txid, "OPEN", datetime.now(timezone.utc), allow_recovery=True)
                except KrakenAPIError as exc:
                    if exc.is_rate_limit():
                        rate_limit_hits += 1
                        _pause_for_rate_limit("add_order_api", 0.0, _rate_limit_pause_from_error(exc))
                        time.sleep(1.0)
                        try_put(ctx.q_order_intent, intent)
                        continue
                    if "insufficient balance" in str(exc).lower():
                        _request_balance_refresh("add_order_insufficient_balance")
                    if exc.is_transient():
                        _send_journal(
                            ctx,
                            "exec_retry",
                            {"reason": "transient_add_order", "order_id": order.id or "", "error": str(exc)},
                        )
                        try_put(ctx.q_order_intent, intent)
                        time.sleep(0.2)
                        continue
                    if exc.is_auth():
                        _set_accept_new(False, "auth_error")
                        try_put(ctx.q_control_core, ControlCommand(ts=datetime.now(timezone.utc), action="STOP", reason="auth_error"))
                        _send_journal(ctx, "exec_error", {"error": str(exc), "kind": "auth_error"})
                        continue
                    else:
                        now_ts = datetime.now(timezone.utc)
                        with sm_lock:
                            order_sm.transition(order.id or "", "REJECTED", now_ts, allow_recovery=True)
                        report = ExecutionReport(
                            ts=now_ts,
                            order_id=order.id or "",
                            status="REJECTED",
                            filled_qty_btc=0.0,
                            avg_price=0.0,
                            fee_eur=0.0,
                            latency_ms=0.0,
                            reason=str(exc),
                        )
                        _mark_seen_client_id(intent.client_id, "rejected", order.id or "")
                        _emit_exec_report(report)
                        continue

            _mark_seen_client_id(intent.client_id, "accepted", order_id)

            latency_ms = (time.time() - order_recv_ts[order_id]) * 1000.0
            last_latency_ms = latency_ms
            status = "VALIDATED" if canary else "OPEN"
            report_order_id = exchange_order_id or order_id
            report_meta: Dict[str, Any] = {}
            if exchange_order_id and exchange_order_id != order_id:
                report_meta["source"] = "add_order_ack"
                report_meta["client_id"] = order_id
                report_meta["order_aliases"] = [order_id]
            report = ExecutionReport(
                ts=datetime.now(timezone.utc),
                order_id=report_order_id,
                status=status,
                filled_qty_btc=0.0,
                avg_price=0.0,
                fee_eur=0.0,
                latency_ms=latency_ms,
                meta=report_meta,
            )
            _emit_exec_report(report)
        except Empty:
            pass

        # market events for paper fills
        if simulator is not None:
            try:
                event: MarketEvent = ctx.q_market_exec.get(timeout=0.1)
                did_work = True
                fills: List[Fill] = simulator.process(event, spread_bps=event.micro.get("spread_bps", 0.0))
                for fill in fills:
                    status = "FILLED" if simulator.config.partial_fill_ratio >= 1.0 else "PARTIAL"
                    with sm_lock:
                        order_sm.transition(fill.order_id or "", status, datetime.now(timezone.utc))
                    report = ExecutionReport(
                        ts=fill.ts,
                        order_id=fill.order_id or "",
                        status=status,
                        filled_qty_btc=fill.qty_btc,
                        avg_price=fill.price,
                        fee_eur=fill.fee_eur,
                        latency_ms=0.0,
                    )
                    _emit_exec_report(report)
                    try_put(ctx.q_exec_report, fill)
                    _send_journal(ctx, "fill", {
                        "ts": fill.ts.isoformat(),
                        "order_id": fill.order_id,
                        "side": fill.side,
                        "qty_btc": fill.qty_btc,
                        "price": fill.price,
                        "fee_eur": fill.fee_eur,
                        "slippage_bps": fill.slippage_bps,
                    })
            except Empty:
                pass

        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            last_heartbeat = now

        # Balance refresh: primarily event-driven (debounced), plus optional periodic safety-net.
        if adapter is not None:
            do_refresh = False
            refresh_source = None
            with balance_req_lock:
                if balance_pending and now >= balance_next_allowed_ts:
                    do_refresh = True
                    refresh_source = f"event:{balance_pending_reason or 'event'}"
                    balance_pending = False
                    balance_pending_reason = None
                    balance_next_allowed_ts = now + max(0.0, balance_refresh_debounce_sec)
            if do_refresh and refresh_source:
                _refresh_balances(refresh_source)

            if balances_refresh_sec > 0 and (now - last_balances_refresh) >= balances_refresh_sec:
                _refresh_balances("periodic")
        if now - last_telemetry >= telemetry_interval:
            last_telemetry = now
            with sm_lock:
                open_count = order_sm.open_orders_count()
            with run_state_lock:
                rate_limited = resume_after is not None
            _send_telemetry(
                ctx,
                {
                    "mode": ctx.mode,
                    "open_orders_count": open_count,
                    "queue_order_intent": queue_depth(ctx.q_order_intent),
                    "queue_exec_report": queue_depth(ctx.q_exec_report),
                    "order_latency_ms": last_latency_ms,
                    "rate_limit_hits": rate_limit_hits,
                    "rate_limited": rate_limited,
                    "canary_mode": canary,
                    "deadman_rate_limit_hits": deadman_rate_limit_hits,
                    "deadman_budget_wait_ms": deadman_budget_wait_ms,
                    "fill_truth_pending_count": len(pending_fill_truth),
                    "balances_updated_at": last_balances_updated_at,
                    "balances_count": len(last_balances),
                },
            )

        if not did_work:
            # Prevent a hot idle loop when there are no control/orders/market events.
            time.sleep(0.01)

    if deadman_adapter is not None:
        _set_deadman_enabled(False, "shutdown")
        try:
            _acquire_budget("deadman_shutdown_disable")
            resp = deadman_adapter.cancel_all_orders_after(0)
            _send_journal(
                ctx,
                "deadman",
                {"event": "disable", "timeout": 0, "reason": "shutdown", "response": resp},
            )
        except Exception as exc:
            _send_journal(ctx, "deadman_error", {"error": str(exc), "kind": "shutdown_disable"})

    _send_journal(ctx, "exec_stop", {})
