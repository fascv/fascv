from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from queue import Empty
from typing import Any, Deque, Dict, List, Optional

from trading.alpha.base import AlphaModel
from trading.alpha.factory import build_alpha_model
from trading.config import load_config
from trading.config_overlay import apply_yaml_overlay
from trading.cost.model import CostModel, FeeConfig, SlippageConfig
from trading.execution.state import StateManager
from trading.features.engine import FeatureEngine
from trading.gate.gate import GateConfig, TradeabilityGate
from trading.ipc.events import ControlCommand, Heartbeat, JournalEvent, NewsEvent, OrderIntent, TelemetryEvent
from trading.ipc.queues import try_put, queue_depth
from trading.order.builder import OrderBuilder, OrderConfig
from trading.processes.context import ProcessContext
from trading.risk.sizing import RiskConfig, RiskManager
from trading.types import Fill, MarketEvent, RiskDecision
from trading.warmup import warmup_feature_and_alpha

# "Hard" risk reasons that should globally disable trading and propagate STOP/CANCEL.
# Keep this list strict: normal risk throttles like cooldown/max_exposure must not permanently
# switch the engine off, otherwise the system appears "stuck" after small losses or price drift.
HARD_RISK_REASONS = {"daily_loss_limit", "max_drawdown"}
RECOVERABLE_EXIT_REASONS = {
    "hard_take_profit",
    "green_candle_take_exit",
    "time_break_even_floor",
    "red_candle_exit",
    "chop_break_even_reclaim",
    "failed_start_exit",
    "hard_stop_loss",
    "trailing_stop",
    "peak_profit_retrace",
    "edge_exit",
    "exit_bypass_gate",
    "reversal_exit_after_break_even",
}


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _quantile_from_sorted(sorted_values: List[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    q_clamped = max(0.0, min(1.0, float(q)))
    pos = q_clamped * float(len(sorted_values) - 1)
    lo_idx = int(pos)
    hi_idx = min(len(sorted_values) - 1, lo_idx + 1)
    if hi_idx <= lo_idx:
        return float(sorted_values[lo_idx])
    frac = pos - float(lo_idx)
    lo_val = float(sorted_values[lo_idx])
    hi_val = float(sorted_values[hi_idx])
    return lo_val + ((hi_val - lo_val) * frac)


def _runtime_config_path(cfg: Dict[str, Any]) -> str:
    return str(_cfg(cfg, "runtime.config_path", "") or "").strip()


def _load_runtime_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    config_path = _runtime_config_path(cfg)
    if not config_path:
        return copy.deepcopy(cfg)
    loaded = load_config(config_path).raw
    loaded.setdefault("runtime", {})
    loaded["runtime"]["config_path"] = config_path
    return loaded


def _copy_risk_runtime_state(src: RiskManager, dst: RiskManager) -> None:
    stateful_attrs = (
        "_cooldown_remaining",
        "_current_day",
        "_day_start_equity",
        "_last_realized_pnl",
        "_last_position_sign",
        "_bars_in_position",
        "_trail_position_sign",
        "_trail_peak_price",
        "_trailing_armed",
        "_position_trough_price",
        "_last_long_exit_price",
        "_reentry_cooldown_remaining",
        "_short_loss_cluster_count",
        "_short_loss_cluster_age_bars",
        "_dynamic_profit_target_bps_active",
        "_last_long_entry_price",
        "_last_effective_position_btc",
        # Preserve corridor staged-exit state across hot reloads; otherwise a reload
        # clears armed/peak tracking and can suppress an otherwise valid roll exit.
        "_corridor_smooth_pos_pct",
        "_corridor_prev_smooth_pos_pct",
        "_corridor_armed_stage_pct",
        "_corridor_armed_stage_age_bars",
        "_corridor_lowest_pos_pct",
        "_corridor_pending_entry_stage_pct",
        "_corridor_entry_stage_pct",
        "_corridor_exit_armed",
        "_corridor_exit_peak_pct",
    )
    for attr in stateful_attrs:
        if hasattr(src, attr):
            setattr(dst, attr, copy.deepcopy(getattr(src, attr)))

    if hasattr(src, "_recent_prices") and hasattr(dst, "_recent_prices"):
        recent_prices = getattr(src, "_recent_prices")
        maxlen = getattr(getattr(dst, "_recent_prices"), "maxlen", None)
        setattr(dst, "_recent_prices", deque(recent_prices, maxlen=maxlen))


def _parse_iso_datetime(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _estimate_position_bars_from_entry_ts(entry_ts_raw: Any, sync_ts: datetime, bar_seconds: float) -> int:
    entry_ts = _parse_iso_datetime(entry_ts_raw)
    if entry_ts is None:
        return 0
    elapsed_sec = max(0.0, (sync_ts - entry_ts).total_seconds())
    return max(1, int(elapsed_sec // max(1.0, float(bar_seconds or 60.0))) + 1)


def _restore_risk_position_state_from_sync(
    risk_manager: RiskManager,
    *,
    position_btc: float,
    reference_price: float,
    entry_ts_raw: Any,
    sync_ts: datetime,
    bar_seconds: float,
) -> int:
    qty_eps = max(1e-12, float(getattr(risk_manager.config, "position_epsilon_btc", 1e-12) or 1e-12))
    sign = 0 if abs(float(position_btc or 0.0)) <= qty_eps else (1 if float(position_btc or 0.0) > 0.0 else -1)
    prev_sign = int(getattr(risk_manager, "_last_position_sign", 0) or 0)
    prev_bars = int(getattr(risk_manager, "_bars_in_position", 0) or 0)
    prev_qty = abs(float(getattr(risk_manager, "_last_effective_position_btc", 0.0) or 0.0))

    if sign == 0:
        setattr(risk_manager, "_last_position_sign", 0)
        setattr(risk_manager, "_last_effective_position_btc", 0.0)
        setattr(risk_manager, "_bars_in_position", 0)
        setattr(risk_manager, "_trail_position_sign", 0)
        setattr(risk_manager, "_trail_peak_price", 0.0)
        setattr(risk_manager, "_trailing_armed", False)
        setattr(risk_manager, "_position_trough_price", 0.0)
        setattr(risk_manager, "_dynamic_profit_target_bps_active", 0.0)
        recent_prices = getattr(risk_manager, "_recent_prices", None)
        if hasattr(recent_prices, "clear"):
            recent_prices.clear()
        return 0

    material_increase = (
        sign == prev_sign
        and bool(getattr(risk_manager, "_material_position_increase", None))
        and risk_manager._material_position_increase(abs(float(position_btc or 0.0)), previous_position_btc=prev_qty)
    )
    restored_bars = _estimate_position_bars_from_entry_ts(entry_ts_raw, sync_ts, bar_seconds)
    if material_increase:
        restored_bars = 1
    elif restored_bars <= 0:
        restored_bars = max(1, prev_bars if prev_sign == sign else 1)
    elif prev_sign == sign:
        restored_bars = max(prev_bars, restored_bars)

    setattr(risk_manager, "_last_position_sign", sign)
    setattr(risk_manager, "_last_effective_position_btc", abs(float(position_btc or 0.0)))
    setattr(risk_manager, "_trail_position_sign", sign)
    setattr(risk_manager, "_bars_in_position", max(1, restored_bars))

    ref = max(0.0, float(reference_price or 0.0))
    if ref > 0.0:
        prev_peak = max(0.0, float(getattr(risk_manager, "_trail_peak_price", 0.0) or 0.0))
        prev_trough = max(0.0, float(getattr(risk_manager, "_position_trough_price", 0.0) or 0.0))
        reset_trackers = prev_sign != sign or material_increase
        setattr(risk_manager, "_trail_peak_price", ref if reset_trackers or prev_peak <= 0.0 else max(prev_peak, ref))
        setattr(
            risk_manager,
            "_position_trough_price",
            ref if reset_trackers or prev_trough <= 0.0 else min(prev_trough, ref),
        )
    if prev_sign != sign or material_increase:
        setattr(risk_manager, "_trailing_armed", False)
        setattr(risk_manager, "_dynamic_profit_target_bps_active", 0.0)
        recent_prices = getattr(risk_manager, "_recent_prices", None)
        if hasattr(recent_prices, "clear"):
            recent_prices.clear()
    return max(1, restored_bars)


def _reset_risk_account_state_from_sync(
    risk_manager: RiskManager,
    *,
    sync_ts: datetime,
    equity_ref: float,
    realized_pnl_eur: float,
) -> None:
    setattr(risk_manager, "_current_day", sync_ts.date())
    setattr(risk_manager, "_day_start_equity", max(0.0, float(equity_ref or 0.0)))
    setattr(risk_manager, "_last_realized_pnl", float(realized_pnl_eur or 0.0))


def _journal_json_path(cfg: Dict[str, Any]) -> str:
    return str(_cfg(cfg, "journal.json_path", _cfg(cfg, "journal.path", "logs/journal_events.jsonl")))


def _event_payload(item: Dict[str, Any]) -> Dict[str, Any]:
    payload = item.get("payload")
    if isinstance(payload, dict):
        return payload
    data = item.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _event_timestamp(item: Dict[str, Any]) -> datetime | None:
    payload = _event_payload(item)
    payload_ts = _parse_iso_datetime(payload.get("ts"))
    if payload_ts is not None:
        return payload_ts
    return _parse_iso_datetime(item.get("ts"))


def _safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return float(default)


def _bars_elapsed_since(ts: datetime | None, *, now_ts: datetime, bar_seconds: float) -> int:
    if ts is None:
        return 0
    return max(0, int(max(0.0, (now_ts - ts).total_seconds()) // max(1.0, float(bar_seconds or 60.0))))


def _recover_flat_reentry_state_from_journal(
    cfg: Dict[str, Any],
    *,
    sync_ts: datetime,
    bar_seconds: float,
    max_events: int = 200000,
) -> Dict[str, Any] | None:
    path = _journal_json_path(cfg)
    if not path or not os.path.exists(path) or max_events <= 0:
        return None
    journal_cfg = cfg.get("journal")
    journal_path_explicit = False
    if isinstance(journal_cfg, dict):
        journal_path_explicit = bool(str(journal_cfg.get("json_path") or "").strip())

    qty_eps = max(
        1e-12,
        float(
            _cfg(
                cfg,
                "risk.position_epsilon_btc",
                _cfg(cfg, "exec.sync_min_position_btc", 1e-12),
            )
            or 1e-12
        ),
    )
    tail: deque[str] = deque(maxlen=max_events)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                row = line.strip()
                if row:
                    tail.append(row)
    except Exception:
        return None

    decision_rows: List[Dict[str, Any]] = []
    external_flat_rows: List[Dict[str, Any]] = []
    flat_reentry_snapshots: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for raw in tail:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        journal_ts = _parse_iso_datetime(item.get("ts")) or _event_timestamp(item)
        if journal_ts is None or journal_ts > sync_ts:
            continue
        events.append(item)
        event_type = str(item.get("event_type") or item.get("event") or "").strip()
        if event_type == "fill":
            payload = _event_payload(item)
            fill_ts = _event_timestamp(item) or journal_ts
            source = str(payload.get("source") or "").strip().lower()
            side = str(payload.get("side") or "").strip().lower()
            qty = max(0.0, _safe_float(payload.get("qty_btc"), 0.0))
            remaining_position = max(0.0, _safe_float(payload.get("position_btc"), 0.0))
            if source == "account_sync_delta" and side == "sell" and qty > 0.0 and remaining_position <= qty_eps:
                external_flat_rows.append(
                    {
                        "ts": fill_ts,
                        "reason": "account_sync_delta",
                        "price": max(0.0, _safe_float(payload.get("price"), 0.0)),
                    }
                )
        if event_type != "core_decision":
            continue
        payload = _event_payload(item)
        risk_payload = payload.get("risk")
        if not isinstance(risk_payload, dict):
            continue
        target_btc = _safe_float(risk_payload.get("target_btc"), 0.0)
        snapshot = {
            "ts": journal_ts,
            "reason": str(risk_payload.get("reason") or "").strip().lower(),
            "target_btc": target_btc,
            "reentry_cooldown_remaining": max(
                0, int(risk_payload.get("reentry_cooldown_remaining") or 0)
            ),
            "short_loss_cluster_count": max(
                0, int(risk_payload.get("short_loss_cluster_count") or 0)
            ),
            "short_loss_cluster_age_bars": max(
                0, int(risk_payload.get("short_loss_cluster_age_bars") or 0)
            ),
            "last_long_exit_price": max(
                0.0, _safe_float(risk_payload.get("last_long_exit_price"), 0.0)
            ),
        }
        if abs(target_btc) <= 1e-9 and (
            snapshot["reentry_cooldown_remaining"] > 0
            or snapshot["short_loss_cluster_count"] > 0
            or snapshot["last_long_exit_price"] > 0.0
        ):
            flat_reentry_snapshots.append(snapshot)
        reason = str(risk_payload.get("reason") or "").strip().lower()
        if reason not in RECOVERABLE_EXIT_REASONS:
            continue
        if not bool(risk_payload.get("allow", False)):
            continue
        if abs(target_btc) > 1e-9:
            continue
        decision_rows.append(
            {
                "ts": journal_ts,
                "reason": reason,
                "price": max(0.0, _safe_float((payload.get("features") or {}).get("price"), 0.0)),
            }
        )

    if not decision_rows and not (journal_path_explicit and external_flat_rows):
        return None

    tolerance_sec = max(5.0, float(bar_seconds or 60.0) * 2.0)
    position_btc = 0.0
    position_cost = 0.0
    current_entry_price = 0.0
    current_entry_ts: datetime | None = None
    completed_campaigns: List[Dict[str, Any]] = []

    def _match_exit_reason(entry_ts: datetime | None, exit_ts: datetime) -> Dict[str, Any] | None:
        matched: Dict[str, Any] | None = None
        for row in decision_rows:
            row_ts = row["ts"]
            if entry_ts is not None and row_ts < entry_ts:
                continue
            if row_ts > exit_ts:
                break
            if (exit_ts - row_ts).total_seconds() > tolerance_sec:
                continue
            matched = row
        return matched

    def _match_external_flat_exit(exit_ts: datetime) -> Dict[str, Any] | None:
        matched: Dict[str, Any] | None = None
        for row in external_flat_rows:
            row_ts = row["ts"]
            if row_ts > exit_ts:
                break
            if (exit_ts - row_ts).total_seconds() > tolerance_sec:
                continue
            matched = row
        return matched

    def _record_exit(exit_ts: datetime, exit_price: float, entry_price: float = 0.0) -> None:
        nonlocal current_entry_ts
        matched = _match_exit_reason(current_entry_ts, exit_ts)
        external_match = _match_external_flat_exit(exit_ts)
        resolved_price = max(
            0.0,
            float(exit_price or 0.0),
            max(0.0, float((external_match or {}).get("price") or 0.0)),
        )
        resolved_reason = str((matched or {}).get("reason") or "").strip().lower()
        if not resolved_reason:
            resolved_reason = str((external_match or {}).get("reason") or "").strip().lower()
        completed_campaigns.append(
            {
                "entry_ts": current_entry_ts,
                "exit_ts": exit_ts,
                "exit_price": resolved_price,
                "entry_price": max(0.0, float(entry_price or 0.0)),
                "exit_reason": resolved_reason or None,
                "bars_in_position": _bars_elapsed_since(current_entry_ts, now_ts=exit_ts, bar_seconds=bar_seconds),
            }
        )
        current_entry_ts = None

    for item in events:
        event_type = str(item.get("event_type") or item.get("event") or "").strip()
        payload = _event_payload(item)
        event_ts = _event_timestamp(item)
        if event_ts is None:
            continue
        if event_type == "fill":
            side = str(payload.get("side") or "").strip().lower()
            qty = max(0.0, _safe_float(payload.get("qty_btc"), 0.0))
            price = max(0.0, _safe_float(payload.get("price"), 0.0))
            fee = max(0.0, _safe_float(payload.get("fee_eur"), 0.0))
            if qty <= 0.0:
                continue
            if side == "buy":
                if position_btc <= qty_eps:
                    current_entry_ts = event_ts
                    position_cost = 0.0
                    current_entry_price = 0.0
                position_btc += qty
                position_cost += (qty * price) + fee
                if position_btc > qty_eps:
                    current_entry_price = position_cost / position_btc
                continue
            if side == "sell":
                if position_btc <= qty_eps:
                    position_btc = 0.0
                    position_cost = 0.0
                    current_entry_price = 0.0
                    current_entry_ts = None
                    continue
                sell_qty = min(qty, position_btc)
                entry_price_before_exit = (
                    current_entry_price
                    if current_entry_price > 0.0
                    else (position_cost / position_btc if position_cost > 0.0 else 0.0)
                )
                alloc_cost = 0.0
                if position_btc > qty_eps and position_cost > 0.0 and sell_qty > 0.0:
                    avg_cost = position_cost / position_btc
                    alloc_cost = avg_cost * sell_qty
                position_btc = max(0.0, position_btc - sell_qty)
                position_cost = max(0.0, position_cost - alloc_cost)
                if position_btc <= qty_eps:
                    position_btc = 0.0
                    position_cost = 0.0
                    current_entry_price = 0.0
                    _record_exit(event_ts, price, entry_price_before_exit)
                elif position_cost > 0.0:
                    current_entry_price = position_cost / position_btc
                continue
        if event_type not in {"core_account_synced", "exec_core_account_sync"}:
            continue
        synced_position = max(0.0, _safe_float(payload.get("position_btc"), 0.0))
        if synced_position > qty_eps:
            position_btc = synced_position
            if current_entry_ts is None:
                current_entry_ts = _parse_iso_datetime(payload.get("entry_ts")) or event_ts
            synced_entry_price = max(0.0, _safe_float(payload.get("avg_entry_price"), 0.0))
            if synced_entry_price > 0.0:
                current_entry_price = synced_entry_price
                position_cost = synced_entry_price * synced_position
            elif position_cost <= qty_eps:
                current_entry_price = 0.0
            continue
        if position_btc > qty_eps:
            ref_price = max(
                0.0,
                _safe_float(payload.get("reference_price"), 0.0),
                _safe_float(payload.get("avg_entry_price"), 0.0),
                _safe_float(payload.get("mark_price"), 0.0),
            )
            entry_price_before_exit = (
                current_entry_price
                if current_entry_price > 0.0
                else (position_cost / position_btc if position_cost > 0.0 else 0.0)
            )
            _record_exit(event_ts, ref_price, entry_price_before_exit)
        position_btc = 0.0
        position_cost = 0.0
        current_entry_price = 0.0
        current_entry_ts = None

    if position_btc > qty_eps or not completed_campaigns:
        return None

    latest = completed_campaigns[-1]
    latest_exit_ts = latest.get("exit_ts")
    if not isinstance(latest_exit_ts, datetime):
        return None
    latest_reason = str(latest.get("exit_reason") or "").strip().lower()
    latest_price = max(0.0, float(latest.get("exit_price") or 0.0))
    latest_entry_price = max(0.0, float(latest.get("entry_price") or 0.0))
    if latest_price <= 0.0:
        matched = _match_exit_reason(latest.get("entry_ts"), latest_exit_ts)
        latest_price = max(0.0, float((matched or {}).get("price") or 0.0))
    if latest_price <= 0.0:
        return None

    latest_snapshot = flat_reentry_snapshots[-1] if flat_reentry_snapshots else None
    snapshot_ts = latest_snapshot.get("ts") if isinstance(latest_snapshot, dict) else None
    if isinstance(snapshot_ts, datetime) and snapshot_ts >= latest_exit_ts:
        snapshot_exit_price = max(0.0, float(latest_snapshot.get("last_long_exit_price") or 0.0))
        if snapshot_exit_price <= 0.0:
            snapshot_exit_price = latest_price
        snapshot_entry_price = max(0.0, float(latest_snapshot.get("last_long_entry_price") or 0.0))
        if snapshot_entry_price <= 0.0:
            snapshot_entry_price = latest_entry_price
        return {
            "last_long_exit_price": snapshot_exit_price,
            "last_long_entry_price": snapshot_entry_price,
            "reentry_cooldown_remaining": max(
                0, int(latest_snapshot.get("reentry_cooldown_remaining") or 0)
            ),
            "short_loss_cluster_count": max(
                0, int(latest_snapshot.get("short_loss_cluster_count") or 0)
            ),
            "short_loss_cluster_age_bars": max(
                0, int(latest_snapshot.get("short_loss_cluster_age_bars") or 0)
            ),
            "exit_reason": latest_reason or None,
            "exit_ts": latest_exit_ts.isoformat(),
        }

    whipsaw_max_bars = max(0, int(_cfg(cfg, "risk.reentry_whipsaw_hard_stop_max_bars", 0) or 0))
    trailing_cooldown = max(0, int(_cfg(cfg, "risk.reentry_cooldown_bars_after_trailing_stop", 0) or 0))
    whipsaw_cooldown = max(0, int(_cfg(cfg, "risk.reentry_cooldown_bars_after_whipsaw_stop_loss", 0) or 0))
    cluster_window_bars = max(0, int(_cfg(cfg, "risk.reentry_loss_cluster_window_bars", 0) or 0))
    cluster_cooldown = max(0, int(_cfg(cfg, "risk.reentry_cooldown_bars_after_loss_cluster", 0) or 0))
    weak_exit_cooldown = max(0, int(_cfg(cfg, "risk.reentry_cooldown_bars_after_weak_exit", 0) or 0))
    external_flat_cooldown = max(
        0,
        int(
            _cfg(
                cfg,
                "risk.reentry_cooldown_bars_after_external_sync_flat",
                weak_exit_cooldown,
            )
            or weak_exit_cooldown
            or 0
        ),
    )
    bars_since_latest_exit = _bars_elapsed_since(latest_exit_ts, now_ts=sync_ts, bar_seconds=bar_seconds)

    cluster_count = 0
    cluster_age_bars = 0
    if latest_reason == "hard_stop_loss" and whipsaw_max_bars > 0:
        latest_bars = max(0, int(latest.get("bars_in_position") or 0))
        if latest_bars <= whipsaw_max_bars:
            cluster_count = 1
            cluster_age_bars = bars_since_latest_exit
            previous_exit_ts = latest_exit_ts
            for campaign in reversed(completed_campaigns[:-1]):
                reason = str(campaign.get("exit_reason") or "").strip().lower()
                bars_in_position = max(0, int(campaign.get("bars_in_position") or 0))
                exit_ts = campaign.get("exit_ts")
                if reason != "hard_stop_loss" or bars_in_position > whipsaw_max_bars or not isinstance(exit_ts, datetime):
                    break
                gap_bars = _bars_elapsed_since(exit_ts, now_ts=previous_exit_ts, bar_seconds=bar_seconds)
                if cluster_window_bars > 0 and gap_bars > cluster_window_bars:
                    break
                cluster_count += 1
                previous_exit_ts = exit_ts

    base_reentry_cooldown = 0
    if latest_reason == "trailing_stop":
        base_reentry_cooldown = trailing_cooldown
        cluster_count = 0
        cluster_age_bars = 0
    elif latest_reason == "hard_stop_loss" and whipsaw_max_bars > 0:
        latest_bars = max(0, int(latest.get("bars_in_position") or 0))
        if latest_bars <= whipsaw_max_bars:
            base_reentry_cooldown = whipsaw_cooldown
            if cluster_count >= 2 and cluster_cooldown > 0:
                base_reentry_cooldown = max(base_reentry_cooldown, cluster_cooldown)
        else:
            cluster_count = 0
            cluster_age_bars = 0
    elif latest_reason in {"time_break_even_floor", "failed_start_exit", "chop_break_even_reclaim"}:
        base_reentry_cooldown = weak_exit_cooldown
        cluster_count = 0
        cluster_age_bars = 0
    elif latest_reason in {"account_sync_delta", "external_sync_flat"}:
        base_reentry_cooldown = external_flat_cooldown
        cluster_count = 0
        cluster_age_bars = 0
    else:
        cluster_count = 0
        cluster_age_bars = 0

    remaining_cooldown = max(0, int(base_reentry_cooldown) - bars_since_latest_exit)
    return {
        "last_long_exit_price": latest_price,
        "last_long_entry_price": latest_entry_price,
        "reentry_cooldown_remaining": remaining_cooldown,
        "short_loss_cluster_count": cluster_count,
        "short_loss_cluster_age_bars": cluster_age_bars,
        "exit_reason": latest_reason or None,
        "exit_ts": latest_exit_ts.isoformat(),
    }


def _restore_risk_flat_reentry_state_from_journal(
    risk_manager: RiskManager,
    cfg: Dict[str, Any],
    *,
    sync_ts: datetime,
    bar_seconds: float,
) -> Dict[str, Any] | None:
    recovered = _recover_flat_reentry_state_from_journal(
        cfg,
        sync_ts=sync_ts,
        bar_seconds=bar_seconds,
    )
    if not recovered:
        return None
    recovered_exit_price = max(0.0, float(recovered.get("last_long_exit_price") or 0.0))
    recovered_entry_price = max(0.0, float(recovered.get("last_long_entry_price") or 0.0))
    recovered_reentry = max(0, int(recovered.get("reentry_cooldown_remaining") or 0))
    recovered_cluster_count = max(0, int(recovered.get("short_loss_cluster_count") or 0))
    recovered_cluster_age = max(0, int(recovered.get("short_loss_cluster_age_bars") or 0))
    applied = False

    if recovered_exit_price > 0.0:
        current_exit_price = max(0.0, float(getattr(risk_manager, "_last_long_exit_price", 0.0) or 0.0))
        if current_exit_price <= 0.0:
            setattr(risk_manager, "_last_long_exit_price", recovered_exit_price)
            applied = True
    if recovered_entry_price > 0.0:
        current_entry_price = max(0.0, float(getattr(risk_manager, "_last_long_entry_price", 0.0) or 0.0))
        if current_entry_price <= 0.0:
            setattr(risk_manager, "_last_long_entry_price", recovered_entry_price)
            applied = True
    current_reentry = max(0, int(getattr(risk_manager, "_reentry_cooldown_remaining", 0) or 0))
    if recovered_reentry > current_reentry:
        setattr(risk_manager, "_reentry_cooldown_remaining", recovered_reentry)
        applied = True
    current_cluster_count = max(0, int(getattr(risk_manager, "_short_loss_cluster_count", 0) or 0))
    current_cluster_age = max(0, int(getattr(risk_manager, "_short_loss_cluster_age_bars", 0) or 0))
    if (
        recovered_cluster_count > current_cluster_count
        or (
            recovered_cluster_count == current_cluster_count
            and recovered_cluster_count > 0
            and (current_cluster_age <= 0 or recovered_cluster_age < current_cluster_age)
        )
    ):
        setattr(risk_manager, "_short_loss_cluster_count", recovered_cluster_count)
        setattr(risk_manager, "_short_loss_cluster_age_bars", recovered_cluster_age)
        applied = True
    return recovered if applied else None


def _queue_propagated_disable_commands(ctx: ProcessContext, ts: datetime, reason: str) -> None:
    for q in (ctx.q_control_core, ctx.q_control_exec):
        # Queue CANCEL_ALL ahead of STOP. The exec loop processes at most one control
        # command before it starts draining intents, so STOP->emergency_exit->CANCEL_ALL
        # can cancel the just-submitted emergency exit on the next iteration.
        try_put(q, ControlCommand(ts=ts, action="CANCEL_ALL", reason=reason))
        try_put(
            q,
            ControlCommand(
                ts=ts,
                action="STOP",
                reason=reason,
                payload={"skip_emergency_exit": True},
            ),
        )


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
    try_put(ctx.q_journal, evt)


def _send_telemetry(ctx: ProcessContext, data: Dict[str, Any]) -> None:
    evt = TelemetryEvent(ts=datetime.now(timezone.utc), process="core", data=data)
    try_put(ctx.q_telemetry, evt)


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="core", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def _inject_alpha_features(features: Any, alpha_meta: Dict[str, Any]) -> None:
    structure = alpha_meta.get("structure") if isinstance(alpha_meta.get("structure"), dict) else {}
    active_leg = str(structure.get("active_leg") or "").strip().lower()
    swing_state = str(alpha_meta.get("swing_state") or "").strip().lower()
    continuation_state = str(alpha_meta.get("continuation_state") or "").strip().lower()
    breakout_state = str(alpha_meta.get("breakout_state") or "").strip().lower()
    features.values["alpha_up_structure"] = 1.0 if bool(structure.get("up_structure")) else 0.0
    features.values["alpha_down_structure"] = 1.0 if bool(structure.get("down_structure")) else 0.0
    features.values["alpha_active_leg_rise"] = 1.0 if active_leg == "rise" else 0.0
    features.values["alpha_swing_micro_valley_rebound"] = 1.0 if swing_state == "micro_valley_rebound" else 0.0
    features.values["alpha_swing_valley_rebound"] = 1.0 if swing_state == "valley_rebound" else 0.0
    features.values["alpha_staircase_override"] = 1.0 if continuation_state == "staircase_override" else 0.0
    features.values["alpha_impulse_override"] = 1.0 if continuation_state == "impulse_override" else 0.0
    features.values["alpha_continuation_early_liftoff"] = (
        1.0 if continuation_state == "early_liftoff_override" else 0.0
    )
    features.values["alpha_structure_range_pos"] = float(structure.get("range_pos") or 0.0)
    features.values["alpha_structure_slope_short_bps"] = float(structure.get("slope_short_bps") or 0.0)
    features.values["alpha_structure_drawdown_from_peak_bps"] = float(
        structure.get("drawdown_from_peak_bps") or 0.0
    )
    features.values["alpha_continuation_await_liftoff"] = 1.0 if continuation_state == "await_liftoff" else 0.0
    features.values["alpha_continuation_armed"] = 1.0 if continuation_state == "armed" else 0.0
    features.values["alpha_recent_bias_bps"] = float(alpha_meta.get("recent_bias_bps") or 0.0)
    features.values["alpha_campaign_hold_bias"] = 1.0 if continuation_state in {
        "staircase_override",
        "impulse_override",
    } else 0.0
    features.values["alpha_breakout_state_up"] = 1.0 if breakout_state == "up_breakout" else 0.0
    features.values["alpha_breakout_state_down"] = 1.0 if breakout_state == "down_breakout" else 0.0
    features.values["alpha_breakout_up_bps"] = float(alpha_meta.get("breakout_up_bps") or 0.0)
    features.values["alpha_breakout_down_bps"] = float(alpha_meta.get("breakout_down_bps") or 0.0)


def _build_components(cfg: Dict[str, Any]) -> tuple[FeatureEngine, AlphaModel, CostModel, TradeabilityGate, RiskManager, OrderBuilder, StateManager]:
    default_micro = _cfg(cfg, "data.default_micro", {})
    min_trade_btc = float(_cfg(cfg, "order.min_trade_btc", 0.0001))
    sync_min_position_btc = float(_cfg(cfg, "exec.sync_min_position_btc", 0.0))
    position_epsilon_btc = max(1e-12, sync_min_position_btc)
    feature_engine = FeatureEngine(
        return_window=int(_cfg(cfg, "features.return_window", 1)),
        atr_window=int(_cfg(cfg, "features.atr_window", 14)),
        volume_z_window=int(_cfg(cfg, "features.volume_z_window", 30)),
        trend_window=int(_cfg(cfg, "features.trend_window", 0)),
        context_window=int(_cfg(cfg, "features.context_window", 0)),
        default_micro=default_micro,
    )

    alpha_model = build_alpha_model(cfg)

    cost_model = CostModel(
        fee_config=FeeConfig(
            maker_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
            taker_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
        ),
        slippage_config=SlippageConfig(
            base_bps=float(_cfg(cfg, "cost.slippage_base_bps", 1.0)),
            vol_mult=float(_cfg(cfg, "cost.slippage_vol_mult", 0.05)),
        ),
        spread_component_factor=float(_cfg(cfg, "cost.spread_component_factor", 0.5)),
    )

    gate = TradeabilityGate(
        GateConfig(
            safety_margin_bps=float(_cfg(cfg, "gate.safety_margin_bps", 1.0)),
            cost_coverage_ratio=float(_cfg(cfg, "gate.cost_coverage_ratio", 1.0)),
            cost_roundtrip_multiplier=float(_cfg(cfg, "gate.cost_roundtrip_multiplier", 1.0)),
            max_spread_bps=float(_cfg(cfg, "gate.max_spread_bps", 15.0)),
            min_atr_bps=float(_cfg(cfg, "gate.min_atr_bps", 0.0)),
            max_atr_bps=float(_cfg(cfg, "gate.max_atr_bps", 200.0)),
            session_start_utc=int(_cfg(cfg, "gate.session_start_utc", 0)),
            session_end_utc=int(_cfg(cfg, "gate.session_end_utc", 24)),
            stale_seconds=int(_cfg(cfg, "gate.stale_seconds", 3600)),
            block_on_high_news_impact=bool(_cfg(cfg, "gate.block_on_high_news_impact", False)),
            max_news_impact=float(_cfg(cfg, "gate.max_news_impact", 1.0)),
            max_news_age_sec=int(_cfg(cfg, "gate.max_news_age_sec", 3600)),
            news_safety_margin_bps=float(_cfg(cfg, "gate.news_safety_margin_bps", 0.0)),
        )
    )

    risk = RiskManager(
        RiskConfig(
            max_exposure_eur=float(_cfg(cfg, "risk.max_exposure_eur", 50.0)),
            vol_target_bps=float(_cfg(cfg, "risk.vol_target_bps", 80.0)),
            daily_loss_limit_eur=float(_cfg(cfg, "risk.daily_loss_limit_eur", 10.0)),
            max_drawdown_pct=float(_cfg(cfg, "risk.max_drawdown_pct", 10.0)),
            cooldown_bars=int(_cfg(cfg, "risk.cooldown_bars", 5)),
            allow_short=bool(_cfg(cfg, "risk.allow_short", False)),
            use_vol_scaling=bool(_cfg(cfg, "risk.use_vol_scaling", True)),
            use_gate_size_factor=bool(_cfg(cfg, "risk.use_gate_size_factor", True)),
            entry_edge_bps=float(_cfg(cfg, "risk.entry_edge_bps", 0.0)),
            entry_cost_buffer_bps=float(_cfg(cfg, "risk.entry_cost_buffer_bps", 0.0)),
            entry_cost_coverage_ratio=float(_cfg(cfg, "risk.entry_cost_coverage_ratio", 1.0)),
            entry_cost_roundtrip_multiplier=float(
                _cfg(cfg, "risk.entry_cost_roundtrip_multiplier", 1.0)
            ),
            entry_min_atr_to_cost_ratio=float(_cfg(cfg, "risk.entry_min_atr_to_cost_ratio", 0.0)),
            disable_entry_edge_gate=bool(_cfg(cfg, "risk.disable_entry_edge_gate", False)),
            override_max_structure_range_pos=float(
                _cfg(cfg, "risk.override_max_structure_range_pos", 1.0)
            ),
            override_min_drawdown_from_peak_bps=float(
                _cfg(cfg, "risk.override_min_drawdown_from_peak_bps", 0.0)
            ),
            override_min_drawdown_to_cost_ratio=float(
                _cfg(cfg, "risk.override_min_drawdown_to_cost_ratio", 0.0)
            ),
            override_min_slope_short_bps=float(
                _cfg(cfg, "risk.override_min_slope_short_bps", -999.0)
            ),
            override_max_trend_return_bps=float(
                _cfg(cfg, "risk.override_max_trend_return_bps", 0.0)
            ),
            override_max_context_range_pos=float(
                _cfg(cfg, "risk.override_max_context_range_pos", 1.0)
            ),
            late_entry_block_context_range_pos=float(
                _cfg(cfg, "risk.late_entry_block_context_range_pos", 1.0)
            ),
            late_entry_block_structure_range_pos=float(
                _cfg(cfg, "risk.late_entry_block_structure_range_pos", 1.0)
            ),
            late_entry_block_max_context_drawdown_bps=float(
                _cfg(cfg, "risk.late_entry_block_max_context_drawdown_bps", 0.0)
            ),
            late_entry_block_min_trend_return_bps=float(
                _cfg(cfg, "risk.late_entry_block_min_trend_return_bps", 0.0)
            ),
            late_entry_block_min_return_bps=float(
                _cfg(cfg, "risk.late_entry_block_min_return_bps", 0.0)
            ),
            exit_edge_bps=float(_cfg(cfg, "risk.exit_edge_bps", 0.0)),
            exit_bypass_gate_edge_bps=float(_cfg(cfg, "risk.exit_bypass_gate_edge_bps", 0.0)),
            min_hold_bars=int(_cfg(cfg, "risk.min_hold_bars", 0)),
            failed_start_exit_enabled=bool(_cfg(cfg, "risk.failed_start_exit_enabled", False)),
            failed_start_min_bars=int(_cfg(cfg, "risk.failed_start_min_bars", 0)),
            failed_start_max_bars=int(_cfg(cfg, "risk.failed_start_max_bars", 0)),
            failed_start_min_rebound_bps=float(_cfg(cfg, "risk.failed_start_min_rebound_bps", 0.0)),
            failed_start_loss_bps=float(_cfg(cfg, "risk.failed_start_loss_bps", 0.0)),
            chop_break_even_reclaim_enabled=bool(
                _cfg(cfg, "risk.chop_break_even_reclaim_enabled", False)
            ),
            chop_break_even_reclaim_min_bars=int(
                _cfg(cfg, "risk.chop_break_even_reclaim_min_bars", 0)
            ),
            chop_break_even_reclaim_min_drawdown_bps=float(
                _cfg(cfg, "risk.chop_break_even_reclaim_min_drawdown_bps", 0.0)
            ),
            chop_break_even_reclaim_max_edge_bps=float(
                _cfg(cfg, "risk.chop_break_even_reclaim_max_edge_bps", 0.0)
            ),
            chop_break_even_reclaim_cross_window_bars=int(
                _cfg(cfg, "risk.chop_break_even_reclaim_cross_window_bars", 0)
            ),
            chop_break_even_reclaim_min_crosses=int(
                _cfg(cfg, "risk.chop_break_even_reclaim_min_crosses", 0)
            ),
            peak_profit_retrace_enabled=bool(_cfg(cfg, "risk.peak_profit_retrace_enabled", False)),
            peak_profit_retrace_arm_bps=float(_cfg(cfg, "risk.peak_profit_retrace_arm_bps", 0.0)),
            peak_profit_retrace_pct=float(_cfg(cfg, "risk.peak_profit_retrace_pct", 0.0)),
            require_break_even_for_exit=bool(_cfg(cfg, "risk.require_break_even_for_exit", False)),
            allow_reversal_exit_after_break_even=bool(
                _cfg(cfg, "risk.allow_reversal_exit_after_break_even", False)
            ),
            time_break_even_floor_enabled=bool(_cfg(cfg, "risk.time_break_even_floor_enabled", False)),
            time_break_even_floor_bars=int(_cfg(cfg, "risk.time_break_even_floor_bars", 0)),
            red_candle_exit_enabled=bool(_cfg(cfg, "risk.red_candle_exit_enabled", False)),
            red_candle_window_bars=int(_cfg(cfg, "risk.red_candle_window_bars", 0)),
            green_candle_take_exit_enabled=bool(
                _cfg(cfg, "risk.green_candle_take_exit_enabled", False)
            ),
            green_candle_take_min_bars=int(_cfg(cfg, "risk.green_candle_take_min_bars", 0)),
            green_candle_take_max_bars=int(_cfg(cfg, "risk.green_candle_take_max_bars", 0)),
            green_candle_take_required_green_bars=int(
                _cfg(cfg, "risk.green_candle_take_required_green_bars", 0)
            ),
            green_candle_take_min_profit_bps=float(
                _cfg(cfg, "risk.green_candle_take_min_profit_bps", 0.0)
            ),
            min_exit_profit_bps=float(_cfg(cfg, "risk.min_exit_profit_bps", 0.0)),
            hard_take_profit_bps=float(_cfg(cfg, "risk.hard_take_profit_bps", 0.0)),
            dynamic_profit_target_enabled=bool(_cfg(cfg, "risk.dynamic_profit_target_enabled", False)),
            dynamic_profit_target_bps_at_low=float(_cfg(cfg, "risk.dynamic_profit_target_bps_at_low", 0.0)),
            dynamic_profit_break_even_from_high_pct=float(
                _cfg(cfg, "risk.dynamic_profit_break_even_from_high_pct", 0.0)
            ),
            hard_stop_loss_bps=float(_cfg(cfg, "risk.hard_stop_loss_bps", 0.0)),
            hard_take_profit_only_in_range=bool(_cfg(cfg, "risk.hard_take_profit_only_in_range", False)),
            trailing_stop_enabled=bool(_cfg(cfg, "risk.trailing_stop_enabled", False)),
            trailing_activation_bps=float(_cfg(cfg, "risk.trailing_activation_bps", 0.0)),
            trailing_stop_bps=float(_cfg(cfg, "risk.trailing_stop_bps", 0.0)),
            trailing_stop_atr_mult=float(_cfg(cfg, "risk.trailing_stop_atr_mult", 0.0)),
            campaign_hold_enabled=bool(_cfg(cfg, "risk.campaign_hold_enabled", False)),
            campaign_hold_min_bars=int(_cfg(cfg, "risk.campaign_hold_min_bars", 0)),
            campaign_hold_min_profit_bps=float(_cfg(cfg, "risk.campaign_hold_min_profit_bps", 0.0)),
            campaign_hold_min_trend_bps=float(_cfg(cfg, "risk.campaign_hold_min_trend_bps", 0.0)),
            campaign_hold_max_range_pos=float(_cfg(cfg, "risk.campaign_hold_max_range_pos", 1.0)),
            campaign_hold_max_drawdown_from_peak_bps=float(
                _cfg(cfg, "risk.campaign_hold_max_drawdown_from_peak_bps", 0.0)
            ),
            campaign_hold_min_recent_bias_bps=float(
                _cfg(cfg, "risk.campaign_hold_min_recent_bias_bps", -999.0)
            ),
            profit_roll_exit_enabled=bool(_cfg(cfg, "risk.profit_roll_exit_enabled", False)),
            profit_roll_arm_eur=float(_cfg(cfg, "risk.profit_roll_arm_eur", 0.0)),
            profit_roll_retrace_eur=float(_cfg(cfg, "risk.profit_roll_retrace_eur", 0.0)),
            reentry_min_move_bps=float(_cfg(cfg, "risk.reentry_min_move_bps", 0.0)),
            reentry_require_price_at_or_below_last_entry=bool(
                _cfg(cfg, "risk.reentry_require_price_at_or_below_last_entry", False)
            ),
            reentry_last_entry_tolerance_bps=float(
                _cfg(cfg, "risk.reentry_last_entry_tolerance_bps", 0.0)
            ),
            reentry_cooldown_bars_after_trailing_stop=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_trailing_stop", 0)
            ),
            reentry_cooldown_bars_after_whipsaw_stop_loss=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_whipsaw_stop_loss", 0)
            ),
            reentry_whipsaw_hard_stop_max_bars=int(
                _cfg(cfg, "risk.reentry_whipsaw_hard_stop_max_bars", 0)
            ),
            reentry_loss_cluster_window_bars=int(
                _cfg(cfg, "risk.reentry_loss_cluster_window_bars", 0)
            ),
            reentry_cooldown_bars_after_loss_cluster=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_loss_cluster", 0)
            ),
            reentry_cooldown_bars_after_weak_exit=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_weak_exit", 0)
            ),
            rebalance_min_delta_eur=float(_cfg(cfg, "risk.rebalance_min_delta_eur", 0.0)),
            position_epsilon_btc=float(_cfg(cfg, "risk.position_epsilon_btc", position_epsilon_btc)),
            position_epsilon_eur=float(_cfg(cfg, "risk.position_epsilon_eur", 0.0)),
            min_entry_depth_eur=float(_cfg(cfg, "risk.min_entry_depth_eur", 0.0)),
            max_entry_notional_to_depth_ratio=float(
                _cfg(cfg, "risk.max_entry_notional_to_depth_ratio", 0.0)
            ),
            full_position_only=bool(_cfg(cfg, "risk.full_position_only", False)),
        )
    )

    order_builder = OrderBuilder(
        OrderConfig(
            order_type=str(_cfg(cfg, "order.type", "market")),
            post_only=bool(_cfg(cfg, "order.post_only", False)),
            limit_offset_bps=float(_cfg(cfg, "order.limit_offset_bps", 3.0)),
            min_trade_btc=min_trade_btc,
            slice_count=int(_cfg(cfg, "order.slice_count", 1)),
            cycle_trade_eur=float(_cfg(cfg, "order.cycle_trade_eur", 0.0)),
        )
    )

    starting_cash = float(_cfg(cfg, "general.starting_cash_eur", 100.0))
    state_manager = StateManager(starting_cash_eur=starting_cash)

    return feature_engine, alpha_model, cost_model, gate, risk, order_builder, state_manager


def run_core(ctx: ProcessContext) -> None:
    ctx.config = _load_runtime_config(ctx.config)
    cfg = copy.deepcopy(ctx.config)
    alpha_override_path = _cfg(cfg, "alpha.override_path", "")
    if alpha_override_path:
        cfg = apply_yaml_overlay(cfg, str(alpha_override_path))
    feature_engine, alpha_model, cost_model, gate, risk_manager, order_builder, state_manager = _build_components(cfg)

    # Simulation should deploy the full budget amount when it decides to take risk.
    # Otherwise vol/gate scaling can shrink the position (confusing for sim UX).
    if ctx.mode == "sim":
        risk_manager.config.use_vol_scaling = bool(_cfg(cfg, "risk.use_vol_scaling", False))
        risk_manager.config.use_gate_size_factor = bool(_cfg(cfg, "risk.use_gate_size_factor", False))
        # In sim we want a single "amount" knob: use the max exposure as the cycle notional by default.
        try:
            order_builder.config.cycle_trade_eur = float(risk_manager.config.max_exposure_eur)
        except Exception:
            pass

    current_starting_cash_eur = float(_cfg(cfg, "general.starting_cash_eur", 100.0))
    # When we reset the simulation budget, ignore any fills/reports older than the cutoff
    # to avoid applying "old world" fills to the new StateManager.
    ignore_reports_before_ts: datetime | None = None
    budget_cutoff_ts: datetime | None = None

    def _load_runtime_params(config: Dict[str, Any]) -> Dict[str, Any]:
        stale_seconds_local = float(_cfg(config, "core.stale_seconds", 10.0))
        md_interval_seconds_local = max(1.0, float(_cfg(config, "md.interval_seconds", 60.0)))
        # Core staleness uses market-event arrival. With bar aggregation, events only arrive
        # once per bar, so the timeout must not be shorter than the bar cadence (+small slack).
        stale_seconds_local = max(stale_seconds_local, md_interval_seconds_local + 30.0)
        max_orders_per_min_local = max(0, int(_cfg(config, "core.max_orders_per_min", 20)))
        rate_limit_pause_sec_local = float(_cfg(config, "core.rate_limit_pause_sec", 2.0))
        auto_resume_rate_limit_local = bool(_cfg(config, "core.auto_resume_rate_limit", False))
        heartbeat_interval_local = float(_cfg(config, "core.heartbeat_interval", 1.0))
        telemetry_interval_local = float(_cfg(config, "core.telemetry_interval", 1.0))

        news_min_impact_local = float(_cfg(config, "news.min_impact", 0.5))
        max_exposure_mode_local = str(_cfg(config, "risk.max_exposure_mode", "fixed") or "fixed").strip().lower()
        if max_exposure_mode_local not in {"fixed", "equity", "cash"}:
            max_exposure_mode_local = "fixed"
        max_exposure_fraction_raw = _cfg(config, "risk.max_exposure_fraction", 1.0)
        if max_exposure_fraction_raw is None:
            max_exposure_fraction_raw = 1.0
        max_exposure_fraction_local = max(0.0, min(1.0, float(max_exposure_fraction_raw)))

        cycle_trade_mode_local = str(_cfg(config, "order.cycle_trade_mode", "fixed") or "fixed").strip().lower()
        if cycle_trade_mode_local not in {"fixed", "exposure", "equity", "cash"}:
            cycle_trade_mode_local = "fixed"
        cycle_trade_fraction_raw = _cfg(config, "order.cycle_trade_fraction", 1.0)
        if cycle_trade_fraction_raw is None:
            cycle_trade_fraction_raw = 1.0
        cycle_trade_fraction_local = max(0.0, min(1.0, float(cycle_trade_fraction_raw)))
        min_entry_notional_eur_local = max(0.0, float(_cfg(config, "exec.min_entry_notional_eur", 0.0)))

        floor_anchor_window_bars_local = max(1, int(_cfg(config, "policy.floor_anchor.window_bars", 0)))
        floor_anchor_percentile_local = float(_cfg(config, "policy.floor_anchor.percentile", 0.2))
        floor_anchor_percentile_local = max(0.0, min(1.0, floor_anchor_percentile_local))
        profit_corridor_window_bars_local = max(1, int(_cfg(config, "policy.profit_corridor.window_bars", 0)))
        profit_corridor_fast_window_bars_local = max(
            1, int(_cfg(config, "policy.profit_corridor.fast_window_bars", 1440))
        )
        profit_corridor_fast_min_bars_local = max(
            1, int(_cfg(config, "policy.profit_corridor.fast_min_bars", 240))
        )
        profit_corridor_fast_min_bars_local = min(
            profit_corridor_fast_window_bars_local,
            profit_corridor_fast_min_bars_local,
        )
        profit_corridor_fast_blend_weight_local = float(
            _cfg(config, "policy.profit_corridor.fast_blend_weight", 0.35)
        )
        profit_corridor_fast_blend_weight_local = max(0.0, min(1.0, profit_corridor_fast_blend_weight_local))
        profit_corridor_robust_range_enabled_local = bool(
            _cfg(config, "policy.profit_corridor.robust_range_enabled", True)
        )
        profit_corridor_robust_low_pct_local = float(
            _cfg(config, "policy.profit_corridor.robust_low_pct", 2.0)
        )
        profit_corridor_robust_high_pct_local = float(
            _cfg(config, "policy.profit_corridor.robust_high_pct", 98.0)
        )
        profit_corridor_robust_low_pct_local = max(0.0, min(100.0, profit_corridor_robust_low_pct_local))
        profit_corridor_robust_high_pct_local = max(0.0, min(100.0, profit_corridor_robust_high_pct_local))
        if profit_corridor_robust_high_pct_local <= profit_corridor_robust_low_pct_local:
            profit_corridor_robust_high_pct_local = min(100.0, profit_corridor_robust_low_pct_local + 1.0)

        return {
            "stale_seconds": stale_seconds_local,
            "md_interval_seconds": md_interval_seconds_local,
            "max_orders_per_min": max_orders_per_min_local,
            "rate_limit_pause_sec": rate_limit_pause_sec_local,
            "auto_resume_rate_limit": auto_resume_rate_limit_local,
            "heartbeat_interval": heartbeat_interval_local,
            "telemetry_interval": telemetry_interval_local,
            # Optional: let news/impact influence the decision edge and entry gating.
            # Defaults are neutral (off) unless configured.
            "news_edge_scale_bps": float(_cfg(config, "news.edge_scale_bps", 0.0)),
            "news_block_long_entries": bool(_cfg(config, "news.block_long_entries", False)),
            "news_risk_off_threshold": float(_cfg(config, "news.risk_off_threshold", 0.25)),
            "news_min_impact": news_min_impact_local,
            "news_require_long_bias": bool(_cfg(config, "news.require_long_bias_for_entries", False)),
            "news_long_entry_min_sentiment": float(_cfg(config, "news.long_entry_min_sentiment", 0.0)),
            "news_long_entry_min_impact": float(_cfg(config, "news.long_entry_min_impact", news_min_impact_local)),
            # Optional long-entry veto in clearly falling market context.
            # Disabled unless configured with negative thresholds.
            "block_long_context_return_bps_below": float(
                _cfg(config, "risk.block_long_if_context_return_bps_below", 0.0)
            ),
            "block_long_trend_return_bps_below": float(
                _cfg(config, "risk.block_long_if_trend_return_bps_below", 0.0)
            ),
            "max_exposure_mode": max_exposure_mode_local,
            "max_exposure_fraction": max_exposure_fraction_local,
            "cycle_trade_mode": cycle_trade_mode_local,
            "cycle_trade_fraction": cycle_trade_fraction_local,
            "manual_entry_exit_only": bool(_cfg(config, "risk.manual_entry_exit_only", False)),
            "min_entry_notional_eur": min_entry_notional_eur_local,
            "floor_anchor_enabled": bool(_cfg(config, "policy.floor_anchor.enabled", False)),
            "floor_anchor_window_bars": floor_anchor_window_bars_local,
            "floor_anchor_min_bars": max(
                1, int(_cfg(config, "policy.floor_anchor.min_bars", floor_anchor_window_bars_local))
            ),
            "floor_anchor_percentile": floor_anchor_percentile_local,
            "floor_anchor_max_distance_bps": float(_cfg(config, "policy.floor_anchor.max_distance_bps", 0.0)),
            "floor_anchor_rebound_lookback_bars": max(
                1, int(_cfg(config, "policy.floor_anchor.rebound_lookback_bars", 8))
            ),
            "floor_anchor_min_rebound_bps": float(_cfg(config, "policy.floor_anchor.min_rebound_bps", 0.0)),
            "floor_anchor_max_rebound_bps": float(_cfg(config, "policy.floor_anchor.max_rebound_bps", 0.0)),
            "floor_anchor_entry_bonus_bps": float(_cfg(config, "policy.floor_anchor.entry_bonus_bps", 0.0)),
            "profit_corridor_enabled": bool(_cfg(config, "policy.profit_corridor.enabled", False)),
            "profit_corridor_window_bars": profit_corridor_window_bars_local,
            "profit_corridor_min_bars": max(
                1, int(_cfg(config, "policy.profit_corridor.min_bars", profit_corridor_window_bars_local))
            ),
            "profit_corridor_fast_window_bars": profit_corridor_fast_window_bars_local,
            "profit_corridor_fast_min_bars": profit_corridor_fast_min_bars_local,
            "profit_corridor_fast_blend_weight": profit_corridor_fast_blend_weight_local,
            "profit_corridor_robust_range_enabled": profit_corridor_robust_range_enabled_local,
            "profit_corridor_robust_low_pct": profit_corridor_robust_low_pct_local,
            "profit_corridor_robust_high_pct": profit_corridor_robust_high_pct_local,
            "profit_corridor_max_entry_position_pct": float(
                _cfg(config, "policy.profit_corridor.max_entry_position_pct", 0.0)
            ),
            "profit_corridor_staged_mode_enabled": bool(
                _cfg(config, "policy.profit_corridor.staged_mode_enabled", False)
            ),
            "profit_corridor_staged_entry_1_pct": float(
                _cfg(config, "policy.profit_corridor.staged_entry_1_pct", 10.0)
            ),
            "profit_corridor_staged_entry_2_pct": float(
                _cfg(config, "policy.profit_corridor.staged_entry_2_pct", 20.0)
            ),
            "profit_corridor_staged_entry_3_pct": float(
                _cfg(config, "policy.profit_corridor.staged_entry_3_pct", 30.0)
            ),
            "profit_corridor_staged_entry_4_pct": float(
                _cfg(config, "policy.profit_corridor.staged_entry_4_pct", 40.0)
            ),
            "profit_corridor_staged_no_buy_above_pct": float(
                _cfg(config, "policy.profit_corridor.staged_no_buy_above_pct", 50.0)
            ),
            "profit_corridor_staged_exit_step_pct": float(
                _cfg(config, "policy.profit_corridor.staged_exit_step_pct", 10.0)
            ),
            "profit_corridor_staged_hysteresis_pct": float(
                _cfg(config, "policy.profit_corridor.staged_hysteresis_pct", 0.75)
            ),
            "profit_corridor_staged_exit_retrace_pct": float(
                _cfg(config, "policy.profit_corridor.staged_exit_retrace_pct", 0.4)
            ),
            "profit_corridor_staged_entry_wait_bars": max(
                0, int(_cfg(config, "policy.profit_corridor.staged_entry_wait_bars", 6))
            ),
            "profit_corridor_staged_transition_smoothing_bars": max(
                1, int(_cfg(config, "policy.profit_corridor.staged_transition_smoothing_bars", 3))
            ),
            "profit_corridor_staged_require_rising": bool(
                _cfg(config, "policy.profit_corridor.staged_require_rising", True)
            ),
            "profit_corridor_staged_profit_target_enabled": bool(
                _cfg(config, "policy.profit_corridor.staged_profit_target_enabled", False)
            ),
            "profit_corridor_staged_profit_target_base_pct": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_base_pct", 0.0)
            ),
            "profit_corridor_staged_profit_target_min_pct": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_min_pct", 0.0)
            ),
            "profit_corridor_staged_profit_target_max_pct": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_max_pct", 100.0)
            ),
            "profit_corridor_staged_profit_target_mult_10": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_mult_10", 1.25)
            ),
            "profit_corridor_staged_profit_target_mult_20": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_mult_20", 1.10)
            ),
            "profit_corridor_staged_profit_target_mult_30": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_mult_30", 1.00)
            ),
            "profit_corridor_staged_profit_target_mult_40": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_mult_40", 0.90)
            ),
            "profit_corridor_staged_profit_target_mult_50": float(
                _cfg(config, "policy.profit_corridor.staged_profit_target_mult_50", 0.80)
            ),
        }

    trading_enabled = bool(_cfg(cfg, "core.trading_enabled", True))
    trading_disable_reason: str | None = None
    runtime_params = _load_runtime_params(cfg)
    stale_seconds = float(runtime_params["stale_seconds"])
    md_interval_seconds = float(runtime_params["md_interval_seconds"])
    max_orders_per_min = int(runtime_params["max_orders_per_min"])
    rate_limit_pause_sec = float(runtime_params["rate_limit_pause_sec"])
    auto_resume_rate_limit = bool(runtime_params["auto_resume_rate_limit"])
    heartbeat_interval = float(runtime_params["heartbeat_interval"])
    telemetry_interval = float(runtime_params["telemetry_interval"])
    news_edge_scale_bps = float(runtime_params["news_edge_scale_bps"])
    news_block_long_entries = bool(runtime_params["news_block_long_entries"])
    news_risk_off_threshold = float(runtime_params["news_risk_off_threshold"])
    news_min_impact = float(runtime_params["news_min_impact"])
    news_require_long_bias = bool(runtime_params["news_require_long_bias"])
    news_long_entry_min_sentiment = float(runtime_params["news_long_entry_min_sentiment"])
    news_long_entry_min_impact = float(runtime_params["news_long_entry_min_impact"])
    block_long_context_return_bps_below = float(runtime_params["block_long_context_return_bps_below"])
    block_long_trend_return_bps_below = float(runtime_params["block_long_trend_return_bps_below"])
    max_exposure_mode = str(runtime_params["max_exposure_mode"])
    max_exposure_fraction = float(runtime_params["max_exposure_fraction"])
    cycle_trade_mode = str(runtime_params["cycle_trade_mode"])
    cycle_trade_fraction = float(runtime_params["cycle_trade_fraction"])
    manual_entry_exit_only = bool(runtime_params["manual_entry_exit_only"])
    dynamic_sizing_params: Dict[str, Any] = {
        "max_exposure_mode": max_exposure_mode,
        "max_exposure_fraction": max_exposure_fraction,
        "cycle_trade_mode": cycle_trade_mode,
        "cycle_trade_fraction": cycle_trade_fraction,
        "min_entry_notional_eur": float(runtime_params["min_entry_notional_eur"]),
    }
    floor_anchor_enabled = bool(runtime_params["floor_anchor_enabled"])
    floor_anchor_window_bars = int(runtime_params["floor_anchor_window_bars"])
    floor_anchor_min_bars = int(runtime_params["floor_anchor_min_bars"])
    floor_anchor_percentile = float(runtime_params["floor_anchor_percentile"])
    floor_anchor_max_distance_bps = float(runtime_params["floor_anchor_max_distance_bps"])
    floor_anchor_rebound_lookback_bars = int(runtime_params["floor_anchor_rebound_lookback_bars"])
    floor_anchor_min_rebound_bps = float(runtime_params["floor_anchor_min_rebound_bps"])
    floor_anchor_max_rebound_bps = float(runtime_params["floor_anchor_max_rebound_bps"])
    floor_anchor_entry_bonus_bps = float(runtime_params["floor_anchor_entry_bonus_bps"])
    profit_corridor_enabled = bool(runtime_params["profit_corridor_enabled"])
    profit_corridor_window_bars = int(runtime_params["profit_corridor_window_bars"])
    profit_corridor_min_bars = int(runtime_params["profit_corridor_min_bars"])
    profit_corridor_fast_window_bars = int(runtime_params["profit_corridor_fast_window_bars"])
    profit_corridor_fast_min_bars = int(runtime_params["profit_corridor_fast_min_bars"])
    profit_corridor_fast_blend_weight = float(runtime_params["profit_corridor_fast_blend_weight"])
    profit_corridor_robust_range_enabled = bool(runtime_params["profit_corridor_robust_range_enabled"])
    profit_corridor_robust_low_pct = float(runtime_params["profit_corridor_robust_low_pct"])
    profit_corridor_robust_high_pct = float(runtime_params["profit_corridor_robust_high_pct"])
    profit_corridor_max_entry_position_pct = float(runtime_params["profit_corridor_max_entry_position_pct"])
    profit_corridor_staged_mode_enabled = bool(runtime_params["profit_corridor_staged_mode_enabled"])
    profit_corridor_staged_entry_1_pct = float(runtime_params["profit_corridor_staged_entry_1_pct"])
    profit_corridor_staged_entry_2_pct = float(runtime_params["profit_corridor_staged_entry_2_pct"])
    profit_corridor_staged_entry_3_pct = float(runtime_params["profit_corridor_staged_entry_3_pct"])
    profit_corridor_staged_entry_4_pct = float(runtime_params["profit_corridor_staged_entry_4_pct"])
    profit_corridor_staged_no_buy_above_pct = float(runtime_params["profit_corridor_staged_no_buy_above_pct"])
    profit_corridor_staged_exit_step_pct = float(runtime_params["profit_corridor_staged_exit_step_pct"])
    profit_corridor_staged_hysteresis_pct = float(runtime_params["profit_corridor_staged_hysteresis_pct"])
    profit_corridor_staged_exit_retrace_pct = float(runtime_params["profit_corridor_staged_exit_retrace_pct"])
    profit_corridor_staged_entry_wait_bars = int(runtime_params["profit_corridor_staged_entry_wait_bars"])
    profit_corridor_staged_transition_smoothing_bars = int(
        runtime_params["profit_corridor_staged_transition_smoothing_bars"]
    )
    profit_corridor_staged_require_rising = bool(runtime_params["profit_corridor_staged_require_rising"])
    profit_corridor_staged_profit_target_enabled = bool(
        runtime_params["profit_corridor_staged_profit_target_enabled"]
    )
    profit_corridor_staged_profit_target_base_pct = float(
        runtime_params["profit_corridor_staged_profit_target_base_pct"]
    )
    profit_corridor_staged_profit_target_min_pct = float(
        runtime_params["profit_corridor_staged_profit_target_min_pct"]
    )
    profit_corridor_staged_profit_target_max_pct = float(
        runtime_params["profit_corridor_staged_profit_target_max_pct"]
    )
    profit_corridor_staged_profit_target_mult_10 = float(
        runtime_params["profit_corridor_staged_profit_target_mult_10"]
    )
    profit_corridor_staged_profit_target_mult_20 = float(
        runtime_params["profit_corridor_staged_profit_target_mult_20"]
    )
    profit_corridor_staged_profit_target_mult_30 = float(
        runtime_params["profit_corridor_staged_profit_target_mult_30"]
    )
    profit_corridor_staged_profit_target_mult_40 = float(
        runtime_params["profit_corridor_staged_profit_target_mult_40"]
    )
    profit_corridor_staged_profit_target_mult_50 = float(
        runtime_params["profit_corridor_staged_profit_target_mult_50"]
    )

    last_market_arrival = time.time()
    last_mark_price: float | None = None
    # In cycle mode (fixed notional enter/exit), we must prevent multiple in-flight orders; otherwise the
    # core can emit multiple entries before fills arrive and the simulation can overspend cash.
    inflight_order_ids: set[str] = set()
    inflight_exit_order_ids: set[str] = set()
    order_aliases_by_id: dict[str, set[str]] = {}
    terminal_order_ids: set[str] = set()
    last_heartbeat = 0.0
    last_telemetry = 0.0
    hb_seq = 0
    order_seq = 0
    last_expected_cost_bps = 0.0
    last_alpha_type = str(_cfg(cfg, "alpha.type", "momentum") or "momentum").strip().lower() or "momentum"
    last_active_strategy = ""
    last_alpha_regime = ""
    last_alpha_regime_reason = ""
    order_times: Deque[float] = deque()
    floor_anchor_prices: Deque[float] = deque(maxlen=floor_anchor_window_bars or None)
    profit_corridor_prices: Deque[float] = deque(maxlen=profit_corridor_window_bars or None)
    profit_corridor_fast_prices: Deque[float] = deque(maxlen=profit_corridor_fast_window_bars or None)
    resume_after: float | None = None
    latest_news: NewsEvent | None = None
    position_reference_pending = False
    warmup_enabled_default = ctx.mode in {"live", "paper"}
    runtime_pipeline: Dict[str, Any] = {
        "feature_engine": feature_engine,
        "alpha_model": alpha_model,
        "cost_model": cost_model,
        "gate": gate,
        "risk_manager": risk_manager,
        "order_builder": order_builder,
    }

    def _emit_emergency_exit(reason: str) -> None:
        # In trading modes, hard safety stops should flatten any open position immediately.
        if ctx.mode not in {"sim", "paper", "live"}:
            return
        if inflight_exit_order_ids:
            return
        active_order_builder = runtime_pipeline["order_builder"]
        try:
            pos_btc = float(state_manager.position.position_btc)
        except Exception:
            return
        eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
        if abs(pos_btc) <= max(1e-12, eps):
            return
        side = "sell" if pos_btc > 0 else "buy"
        qty = abs(pos_btc)
        intent = OrderIntent(
            ts=datetime.now(timezone.utc),
            side=side,
            qty_btc=qty,
            order_type="market",
            limit_price=None,
            post_only=False,
            client_id=f"emergency_exit_{int(time.time() * 1000)}",
            reason=reason,
            meta={"emergency_exit": True, "note": "hard_stop_flatten"},
        )
        if try_put(ctx.q_order_intent, intent):
            inflight_order_ids.add(intent.client_id)
            inflight_exit_order_ids.add(intent.client_id)

    def _disable_trading(reason: str, propagate: bool = False, clear_resume_after: bool = True) -> None:
        nonlocal trading_enabled, trading_disable_reason, resume_after
        state_changed = trading_enabled or trading_disable_reason != reason
        trading_enabled = False
        trading_disable_reason = reason
        if clear_resume_after:
            resume_after = None
        if not state_changed:
            return
        _send_journal(ctx, "core_trading_disabled", {"reason": reason, "propagated": propagate})
        if not propagate:
            return
        # On severe loss/drawdown stops, close positions first and then STOP/CANCEL_ALL.
        if reason in {"daily_loss_limit", "max_drawdown"}:
            _emit_emergency_exit(reason)
        stop_ts = datetime.now(timezone.utc)
        _queue_propagated_disable_commands(ctx, stop_ts, reason)

    def _enable_trading(reason: str, action: str, propagate: bool = False) -> None:
        nonlocal trading_enabled, trading_disable_reason, resume_after
        state_changed = (not trading_enabled) or (trading_disable_reason is not None)
        trading_enabled = True
        trading_disable_reason = None
        resume_after = None
        if not state_changed:
            return
        _send_journal(ctx, "core_trading_enabled", {"reason": reason, "action": action})
        if not propagate:
            return
        resume_ts = datetime.now(timezone.utc)
        try_put(ctx.q_control_exec, ControlCommand(ts=resume_ts, action="RESUME", reason=reason))

    def _is_hard_risk_reason(reason: str | None) -> bool:
        if not reason:
            return False
        return reason in HARD_RISK_REASONS

    def _apply_dynamic_sizing(pre_state: Any) -> None:
        _apply_dynamic_sizing_to_components(
            pre_state,
            runtime_pipeline["risk_manager"],
            runtime_pipeline["order_builder"],
            dynamic_sizing_params,
        )

    def _apply_dynamic_sizing_to_components(
        pre_state: Any,
        active_risk_manager: Any,
        active_order_builder: Any,
        params: Dict[str, Any],
    ) -> None:
        max_exposure_mode_local = str(params.get("max_exposure_mode", "fixed") or "fixed")
        max_exposure_fraction_local = float(params.get("max_exposure_fraction", 1.0) or 0.0)
        cycle_trade_mode_local = str(params.get("cycle_trade_mode", "fixed") or "fixed")
        cycle_trade_fraction_local = float(params.get("cycle_trade_fraction", 1.0) or 0.0)
        min_entry_notional_eur_local = max(0.0, float(params.get("min_entry_notional_eur", 0.0) or 0.0))

        def _floor_active_notional(notional: float, *, reference_eur: float) -> float:
            floored = max(0.0, float(notional))
            if floored <= 0.0 or min_entry_notional_eur_local <= 0.0:
                return floored
            # If a lane is active and there is enough capital for the configured minimum,
            # do not shrink sizing below the entry notional that exec will reject anyway.
            if floored + 1e-9 >= min_entry_notional_eur_local:
                return floored
            if float(reference_eur) + 1e-9 < min_entry_notional_eur_local:
                return floored
            return float(min_entry_notional_eur_local)

        # Optional live sizing modes:
        # - risk.max_exposure_mode=equity|cash: update max exposure every tick from current account state.
        # - order.cycle_trade_mode=exposure|equity|cash: update cycle notional every tick.
        if max_exposure_mode_local == "equity":
            try:
                equity_eur = max(0.0, float(pre_state.equity_eur))
                active_risk_manager.config.max_exposure_eur = _floor_active_notional(
                    equity_eur * max_exposure_fraction_local,
                    reference_eur=equity_eur,
                )
            except Exception:
                pass
        elif max_exposure_mode_local == "cash":
            try:
                cash_eur = max(0.0, float(pre_state.cash_eur))
                active_risk_manager.config.max_exposure_eur = _floor_active_notional(
                    cash_eur * max_exposure_fraction_local,
                    reference_eur=cash_eur,
                )
            except Exception:
                pass

        if cycle_trade_mode_local == "fixed":
            return
        if cycle_trade_mode_local == "exposure":
            ref = float(getattr(active_risk_manager.config, "max_exposure_eur", 0.0) or 0.0)
        elif cycle_trade_mode_local == "equity":
            ref = float(getattr(pre_state, "equity_eur", 0.0) or 0.0)
        else:  # "cash"
            ref = float(getattr(pre_state, "cash_eur", 0.0) or 0.0)
        try:
            active_order_builder.config.cycle_trade_eur = _floor_active_notional(
                ref * cycle_trade_fraction_local,
                reference_eur=max(0.0, ref),
            )
        except Exception:
            pass

    def _prime_price_windows(close_prices: List[Any]) -> None:
        nonlocal floor_anchor_prices, profit_corridor_prices, profit_corridor_fast_prices
        floor_anchor_prices.clear()
        profit_corridor_prices.clear()
        profit_corridor_fast_prices.clear()
        if floor_anchor_enabled and floor_anchor_window_bars > 0:
            for raw_px in close_prices[-floor_anchor_window_bars:]:
                try:
                    px = float(raw_px)
                except Exception:
                    continue
                if px > 0.0:
                    floor_anchor_prices.append(px)
        if profit_corridor_enabled and profit_corridor_window_bars > 0:
            for raw_px in close_prices[-profit_corridor_window_bars:]:
                try:
                    px = float(raw_px)
                except Exception:
                    continue
                if px > 0.0:
                    profit_corridor_prices.append(px)
        if profit_corridor_enabled and profit_corridor_fast_window_bars > 0:
            for raw_px in close_prices[-profit_corridor_fast_window_bars:]:
                try:
                    px = float(raw_px)
                except Exception:
                    continue
                if px > 0.0:
                    profit_corridor_fast_prices.append(px)

    def _send_progress_heartbeat() -> None:
        nonlocal hb_seq, last_heartbeat
        hb_seq += 1
        _send_heartbeat(ctx, hb_seq)
        last_heartbeat = time.time()

    def _run_warmup_with_heartbeat(
        active_cfg: Dict[str, Any],
        active_feature_engine: Any,
        active_alpha_model: Any,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        errors: List[Exception] = []
        done = threading.Event()

        def _worker() -> None:
            try:
                result["report"] = warmup_feature_and_alpha(active_cfg, active_feature_engine, active_alpha_model)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=_worker, name="core_warmup", daemon=True)
        worker.start()

        tick_sec = 0.25
        if heartbeat_interval > 0.0:
            tick_sec = max(0.05, min(0.5, heartbeat_interval * 0.5))
        while not done.wait(timeout=tick_sec):
            _send_progress_heartbeat()

        worker.join()
        if errors:
            raise errors[0]
        return dict(result.get("report") or {})

    def _reload_runtime_components(reason: str) -> bool:
        nonlocal cfg, alpha_override_path, feature_engine, alpha_model, cost_model, gate, risk_manager, order_builder
        nonlocal last_alpha_type, last_active_strategy, last_alpha_regime, last_alpha_regime_reason
        nonlocal stale_seconds, md_interval_seconds, max_orders_per_min, rate_limit_pause_sec
        nonlocal auto_resume_rate_limit, heartbeat_interval, telemetry_interval
        nonlocal news_edge_scale_bps, news_block_long_entries, news_risk_off_threshold
        nonlocal news_min_impact, news_require_long_bias, news_long_entry_min_sentiment
        nonlocal news_long_entry_min_impact, block_long_context_return_bps_below
        nonlocal block_long_trend_return_bps_below, max_exposure_mode, max_exposure_fraction
        nonlocal cycle_trade_mode, cycle_trade_fraction, manual_entry_exit_only
        nonlocal floor_anchor_enabled, floor_anchor_window_bars
        nonlocal dynamic_sizing_params
        nonlocal floor_anchor_min_bars, floor_anchor_percentile, floor_anchor_max_distance_bps
        nonlocal floor_anchor_rebound_lookback_bars, floor_anchor_min_rebound_bps
        nonlocal floor_anchor_max_rebound_bps, floor_anchor_entry_bonus_bps
        nonlocal profit_corridor_enabled, profit_corridor_window_bars, profit_corridor_min_bars
        nonlocal profit_corridor_fast_window_bars, profit_corridor_fast_min_bars
        nonlocal profit_corridor_fast_blend_weight
        nonlocal profit_corridor_robust_range_enabled, profit_corridor_robust_low_pct
        nonlocal profit_corridor_robust_high_pct
        nonlocal profit_corridor_max_entry_position_pct
        nonlocal floor_anchor_prices, profit_corridor_prices, profit_corridor_fast_prices
        nonlocal profit_corridor_staged_mode_enabled
        nonlocal profit_corridor_staged_entry_1_pct, profit_corridor_staged_entry_2_pct
        nonlocal profit_corridor_staged_entry_3_pct, profit_corridor_staged_entry_4_pct
        nonlocal profit_corridor_staged_no_buy_above_pct, profit_corridor_staged_exit_step_pct
        nonlocal profit_corridor_staged_hysteresis_pct, profit_corridor_staged_exit_retrace_pct
        nonlocal profit_corridor_staged_entry_wait_bars
        nonlocal profit_corridor_staged_transition_smoothing_bars, profit_corridor_staged_require_rising
        nonlocal profit_corridor_staged_profit_target_enabled
        nonlocal profit_corridor_staged_profit_target_base_pct
        nonlocal profit_corridor_staged_profit_target_min_pct
        nonlocal profit_corridor_staged_profit_target_max_pct
        nonlocal profit_corridor_staged_profit_target_mult_10
        nonlocal profit_corridor_staged_profit_target_mult_20
        nonlocal profit_corridor_staged_profit_target_mult_30
        nonlocal profit_corridor_staged_profit_target_mult_40
        nonlocal profit_corridor_staged_profit_target_mult_50
        nonlocal hb_seq, last_heartbeat

        runtime_path = _runtime_config_path(ctx.config)
        old_floor_prices = list(floor_anchor_prices)
        old_profit_prices = list(profit_corridor_prices)
        old_fast_profit_prices = list(profit_corridor_fast_prices)
        old_risk_manager = risk_manager
        old_order_builder = order_builder

        try:
            # Reload can be triggered while the lane is actively trading. Emit a heartbeat
            # before doing any potentially slow work so the watchdog sees forward progress.
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            last_heartbeat = time.time()
            ctx.config = _load_runtime_config(ctx.config)
            reloaded_cfg = copy.deepcopy(ctx.config)
            reloaded_alpha_override_path = _cfg(reloaded_cfg, "alpha.override_path", "")
            if reloaded_alpha_override_path:
                reloaded_cfg = apply_yaml_overlay(reloaded_cfg, str(reloaded_alpha_override_path))

            (
                new_feature_engine,
                new_alpha_model,
                new_cost_model,
                new_gate,
                new_risk_manager,
                new_order_builder,
                _,
            ) = _build_components(reloaded_cfg)
            _copy_risk_runtime_state(old_risk_manager, new_risk_manager)

            if ctx.mode == "sim":
                new_risk_manager.config.use_vol_scaling = bool(_cfg(reloaded_cfg, "risk.use_vol_scaling", False))
                new_risk_manager.config.use_gate_size_factor = bool(
                    _cfg(reloaded_cfg, "risk.use_gate_size_factor", False)
                )
                try:
                    new_order_builder.config.cycle_trade_eur = float(new_risk_manager.config.max_exposure_eur)
                except Exception:
                    pass

            local_warmup_enabled = bool(_cfg(reloaded_cfg, "core.warmup.enabled", warmup_enabled_default))
            # Reloads rebuild feature/alpha state. Default to rehydrating that state again,
            # otherwise live rotation lanes can get stuck cold after every runtime reload.
            reload_warmup_enabled = bool(_cfg(reloaded_cfg, "core.warmup.reload_enabled", local_warmup_enabled))
            warmup_report: Dict[str, Any] | None = None
            warmup_close_prices: List[Any] = []
            if local_warmup_enabled and reload_warmup_enabled:
                warmup_report = _run_warmup_with_heartbeat(reloaded_cfg, new_feature_engine, new_alpha_model)
                warmup_close_prices = list(warmup_report.pop("_close_prices", []) or [])

            reloaded_runtime_params = _load_runtime_params(reloaded_cfg)
            reload_state = state_manager.snapshot(
                datetime.now(timezone.utc),
                float(last_mark_price) if last_mark_price is not None else 0.0,
            )
            reloaded_dynamic_sizing_params = {
                "max_exposure_mode": str(reloaded_runtime_params["max_exposure_mode"]),
                "max_exposure_fraction": float(reloaded_runtime_params["max_exposure_fraction"]),
                "cycle_trade_mode": str(reloaded_runtime_params["cycle_trade_mode"]),
                "cycle_trade_fraction": float(reloaded_runtime_params["cycle_trade_fraction"]),
                "min_entry_notional_eur": float(reloaded_runtime_params["min_entry_notional_eur"]),
            }
            _apply_dynamic_sizing_to_components(
                reload_state,
                old_risk_manager,
                old_order_builder,
                reloaded_dynamic_sizing_params,
            )
            _apply_dynamic_sizing_to_components(
                reload_state,
                new_risk_manager,
                new_order_builder,
                reloaded_dynamic_sizing_params,
            )
            cfg = reloaded_cfg
            alpha_override_path = reloaded_alpha_override_path
            feature_engine = new_feature_engine
            alpha_model = new_alpha_model
            cost_model = new_cost_model
            gate = new_gate
            risk_manager = new_risk_manager
            order_builder = new_order_builder
            stale_seconds = float(reloaded_runtime_params["stale_seconds"])
            md_interval_seconds = float(reloaded_runtime_params["md_interval_seconds"])
            max_orders_per_min = int(reloaded_runtime_params["max_orders_per_min"])
            rate_limit_pause_sec = float(reloaded_runtime_params["rate_limit_pause_sec"])
            auto_resume_rate_limit = bool(reloaded_runtime_params["auto_resume_rate_limit"])
            heartbeat_interval = float(reloaded_runtime_params["heartbeat_interval"])
            telemetry_interval = float(reloaded_runtime_params["telemetry_interval"])
            news_edge_scale_bps = float(reloaded_runtime_params["news_edge_scale_bps"])
            news_block_long_entries = bool(reloaded_runtime_params["news_block_long_entries"])
            news_risk_off_threshold = float(reloaded_runtime_params["news_risk_off_threshold"])
            news_min_impact = float(reloaded_runtime_params["news_min_impact"])
            news_require_long_bias = bool(reloaded_runtime_params["news_require_long_bias"])
            news_long_entry_min_sentiment = float(reloaded_runtime_params["news_long_entry_min_sentiment"])
            news_long_entry_min_impact = float(reloaded_runtime_params["news_long_entry_min_impact"])
            block_long_context_return_bps_below = float(
                reloaded_runtime_params["block_long_context_return_bps_below"]
            )
            block_long_trend_return_bps_below = float(
                reloaded_runtime_params["block_long_trend_return_bps_below"]
            )
            max_exposure_mode = str(reloaded_runtime_params["max_exposure_mode"])
            max_exposure_fraction = float(reloaded_runtime_params["max_exposure_fraction"])
            cycle_trade_mode = str(reloaded_runtime_params["cycle_trade_mode"])
            cycle_trade_fraction = float(reloaded_runtime_params["cycle_trade_fraction"])
            manual_entry_exit_only = bool(reloaded_runtime_params["manual_entry_exit_only"])
            dynamic_sizing_params = dict(reloaded_dynamic_sizing_params)
            floor_anchor_enabled = bool(reloaded_runtime_params["floor_anchor_enabled"])
            floor_anchor_window_bars = int(reloaded_runtime_params["floor_anchor_window_bars"])
            floor_anchor_min_bars = int(reloaded_runtime_params["floor_anchor_min_bars"])
            floor_anchor_percentile = float(reloaded_runtime_params["floor_anchor_percentile"])
            floor_anchor_max_distance_bps = float(reloaded_runtime_params["floor_anchor_max_distance_bps"])
            floor_anchor_rebound_lookback_bars = int(reloaded_runtime_params["floor_anchor_rebound_lookback_bars"])
            floor_anchor_min_rebound_bps = float(reloaded_runtime_params["floor_anchor_min_rebound_bps"])
            floor_anchor_max_rebound_bps = float(reloaded_runtime_params["floor_anchor_max_rebound_bps"])
            floor_anchor_entry_bonus_bps = float(reloaded_runtime_params["floor_anchor_entry_bonus_bps"])
            profit_corridor_enabled = bool(reloaded_runtime_params["profit_corridor_enabled"])
            profit_corridor_window_bars = int(reloaded_runtime_params["profit_corridor_window_bars"])
            profit_corridor_min_bars = int(reloaded_runtime_params["profit_corridor_min_bars"])
            profit_corridor_fast_window_bars = int(reloaded_runtime_params["profit_corridor_fast_window_bars"])
            profit_corridor_fast_min_bars = int(reloaded_runtime_params["profit_corridor_fast_min_bars"])
            profit_corridor_fast_blend_weight = float(reloaded_runtime_params["profit_corridor_fast_blend_weight"])
            profit_corridor_robust_range_enabled = bool(
                reloaded_runtime_params["profit_corridor_robust_range_enabled"]
            )
            profit_corridor_robust_low_pct = float(reloaded_runtime_params["profit_corridor_robust_low_pct"])
            profit_corridor_robust_high_pct = float(reloaded_runtime_params["profit_corridor_robust_high_pct"])
            profit_corridor_max_entry_position_pct = float(
                reloaded_runtime_params["profit_corridor_max_entry_position_pct"]
            )
            profit_corridor_staged_mode_enabled = bool(
                reloaded_runtime_params["profit_corridor_staged_mode_enabled"]
            )
            profit_corridor_staged_entry_1_pct = float(
                reloaded_runtime_params["profit_corridor_staged_entry_1_pct"]
            )
            profit_corridor_staged_entry_2_pct = float(
                reloaded_runtime_params["profit_corridor_staged_entry_2_pct"]
            )
            profit_corridor_staged_entry_3_pct = float(
                reloaded_runtime_params["profit_corridor_staged_entry_3_pct"]
            )
            profit_corridor_staged_entry_4_pct = float(
                reloaded_runtime_params["profit_corridor_staged_entry_4_pct"]
            )
            profit_corridor_staged_no_buy_above_pct = float(
                reloaded_runtime_params["profit_corridor_staged_no_buy_above_pct"]
            )
            profit_corridor_staged_exit_step_pct = float(
                reloaded_runtime_params["profit_corridor_staged_exit_step_pct"]
            )
            profit_corridor_staged_hysteresis_pct = float(
                reloaded_runtime_params["profit_corridor_staged_hysteresis_pct"]
            )
            profit_corridor_staged_exit_retrace_pct = float(
                reloaded_runtime_params["profit_corridor_staged_exit_retrace_pct"]
            )
            profit_corridor_staged_entry_wait_bars = int(
                reloaded_runtime_params["profit_corridor_staged_entry_wait_bars"]
            )
            profit_corridor_staged_transition_smoothing_bars = int(
                reloaded_runtime_params["profit_corridor_staged_transition_smoothing_bars"]
            )
            profit_corridor_staged_require_rising = bool(
                reloaded_runtime_params["profit_corridor_staged_require_rising"]
            )
            profit_corridor_staged_profit_target_enabled = bool(
                reloaded_runtime_params["profit_corridor_staged_profit_target_enabled"]
            )
            profit_corridor_staged_profit_target_base_pct = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_base_pct"]
            )
            profit_corridor_staged_profit_target_min_pct = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_min_pct"]
            )
            profit_corridor_staged_profit_target_max_pct = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_max_pct"]
            )
            profit_corridor_staged_profit_target_mult_10 = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_mult_10"]
            )
            profit_corridor_staged_profit_target_mult_20 = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_mult_20"]
            )
            profit_corridor_staged_profit_target_mult_30 = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_mult_30"]
            )
            profit_corridor_staged_profit_target_mult_40 = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_mult_40"]
            )
            profit_corridor_staged_profit_target_mult_50 = float(
                reloaded_runtime_params["profit_corridor_staged_profit_target_mult_50"]
            )
            runtime_pipeline["feature_engine"] = feature_engine
            runtime_pipeline["alpha_model"] = alpha_model
            runtime_pipeline["cost_model"] = cost_model
            runtime_pipeline["gate"] = gate
            runtime_pipeline["risk_manager"] = risk_manager
            runtime_pipeline["order_builder"] = order_builder

            last_alpha_type = str(_cfg(cfg, "alpha.type", "momentum") or "momentum").strip().lower() or "momentum"
            last_active_strategy = ""
            last_alpha_regime = ""
            last_alpha_regime_reason = ""
            floor_anchor_prices = deque(maxlen=floor_anchor_window_bars or None)
            profit_corridor_prices = deque(maxlen=profit_corridor_window_bars or None)
            profit_corridor_fast_prices = deque(maxlen=profit_corridor_fast_window_bars or None)

            if warmup_close_prices:
                _prime_price_windows(warmup_close_prices)
            else:
                if floor_anchor_enabled and floor_anchor_window_bars > 0:
                    for raw_px in old_floor_prices[-floor_anchor_window_bars:]:
                        try:
                            px = float(raw_px)
                        except Exception:
                            continue
                        if px > 0.0:
                            floor_anchor_prices.append(px)
                if profit_corridor_enabled and profit_corridor_window_bars > 0:
                    for raw_px in old_profit_prices[-profit_corridor_window_bars:]:
                        try:
                            px = float(raw_px)
                        except Exception:
                            continue
                        if px > 0.0:
                            profit_corridor_prices.append(px)
                if profit_corridor_enabled and profit_corridor_fast_window_bars > 0:
                    for raw_px in old_fast_profit_prices[-profit_corridor_fast_window_bars:]:
                        try:
                            px = float(raw_px)
                        except Exception:
                            continue
                        if px > 0.0:
                            profit_corridor_fast_prices.append(px)

            _send_journal(
                ctx,
                "core_reload_applied",
                {
                    "reason": reason,
                    "config_path": runtime_path or None,
                    "alpha_type": last_alpha_type,
                    "alpha_model_class": type(alpha_model).__name__,
                    "alpha_override_path": alpha_override_path or None,
                    "max_exposure_mode": max_exposure_mode,
                    "max_exposure_fraction": max_exposure_fraction,
                    "cycle_trade_mode": cycle_trade_mode,
                    "cycle_trade_fraction": cycle_trade_fraction,
                    "manual_entry_exit_only": manual_entry_exit_only,
                    "profit_corridor_window_bars": profit_corridor_window_bars,
                    "profit_corridor_fast_window_bars": profit_corridor_fast_window_bars,
                    "profit_corridor_fast_blend_weight": profit_corridor_fast_blend_weight,
                    "profit_corridor_robust_range_enabled": profit_corridor_robust_range_enabled,
                    "profit_corridor_robust_low_pct": profit_corridor_robust_low_pct,
                    "profit_corridor_robust_high_pct": profit_corridor_robust_high_pct,
                    "warmup_applied": bool(warmup_close_prices),
                },
            )
            if warmup_report is not None:
                warmup_payload = dict(warmup_report)
                warmup_payload["reason"] = "reload"
                _send_journal(ctx, "core_warmup", warmup_payload)
            return True
        except Exception as exc:
            _send_journal(
                ctx,
                "core_reload_error",
                {
                    "reason": reason,
                    "config_path": runtime_path or None,
                    "error": str(exc),
                },
            )
            return False

    def _drain_control_commands(now: float, disable_seen_this_tick: bool) -> bool:
        """
        Deterministic semantics:
        - If any disable action (PAUSE/STOP) is seen in this tick, trading ends disabled.
        - START/RESUME is applied only if no disable happened in this tick.
        """
        nonlocal resume_after, state_manager, risk_manager, order_times, order_seq, current_starting_cash_eur, ignore_reports_before_ts, budget_cutoff_ts, position_reference_pending
        saw_disable = False
        disable_reason: str | None = None
        saw_rate_limit_pause = False
        saw_enable = False
        enable_reason: str | None = None
        enable_action: str | None = None
        saw_reload = False
        reload_reason: str | None = None
        reload_count = 0

        while True:
            try:
                cmd: ControlCommand = ctx.q_control_core.get_nowait()
            except Empty:
                break

            cmd_reason = cmd.reason or cmd.action.lower()
            if cmd.action == "PAUSE":
                saw_disable = True
                disable_reason = cmd_reason
                if cmd.reason == "rate_limit":
                    saw_rate_limit_pause = True
                    pause_for = float(rate_limit_pause_sec)
                    payload = cmd.payload if isinstance(cmd.payload, dict) else {}
                    try:
                        payload_pause = float(payload.get("resume_after_sec", pause_for))
                    except Exception:
                        payload_pause = pause_for
                    if payload_pause > pause_for:
                        pause_for = payload_pause
                    resume_after = now + max(0.0, pause_for)
            elif cmd.action == "FLATTEN":
                # Force an emergency exit intent even if trading is paused/stopped.
                _emit_emergency_exit(cmd_reason or "flatten")
                saw_disable = True
                disable_reason = cmd_reason or "flatten"
            elif cmd.action == "STOP":
                # A manual/global stop should not leave live inventory behind.
                payload = cmd.payload if isinstance(cmd.payload, dict) else {}
                if not bool(payload.get("skip_emergency_exit", False)):
                    _emit_emergency_exit(cmd_reason or "stop")
                saw_disable = True
                disable_reason = cmd_reason
            elif cmd.action in {"START", "RESUME"}:
                saw_enable = True
                enable_reason = cmd_reason
                enable_action = cmd.action
            elif cmd.action == "SET_BUDGET":
                payload = cmd.payload if isinstance(cmd.payload, dict) else {}

                def _as_pos_float(key: str, default: float | None = None) -> float | None:
                    if key not in payload:
                        return default
                    try:
                        v = float(payload.get(key))
                    except Exception:
                        return None
                    if not (v > 0.0):
                        return None
                    return v

                amount_eur = _as_pos_float("amount_eur", None)
                if amount_eur is not None:
                    starting_cash_eur = float(amount_eur)
                    max_exposure_eur = float(amount_eur)
                else:
                    starting_cash_eur = _as_pos_float("starting_cash_eur", None)
                    max_exposure_eur = _as_pos_float("max_exposure_eur", None)
                reset = bool(payload.get("reset", True))

                if starting_cash_eur is None or max_exposure_eur is None:
                    _send_journal(
                        ctx,
                        "core_error",
                        {
                            "reason": "invalid_budget_payload",
                            "payload": payload,
                        },
                    )
                    continue

                if reset:
                    current_starting_cash_eur = float(starting_cash_eur)
                    state_manager = StateManager(starting_cash_eur=current_starting_cash_eur)
                    order_times.clear()
                    order_seq = 0
                    inflight_order_ids.clear()
                    inflight_exit_order_ids.clear()
                    ignore_reports_before_ts = cmd.ts
                    budget_cutoff_ts = cmd.ts

                # Always apply the new max exposure (even without a reset).
                rcfg = risk_manager.config
                risk_manager = RiskManager(
                    RiskConfig(
                        max_exposure_eur=float(max_exposure_eur),
                        vol_target_bps=rcfg.vol_target_bps,
                        daily_loss_limit_eur=rcfg.daily_loss_limit_eur,
                        max_drawdown_pct=rcfg.max_drawdown_pct,
                        cooldown_bars=rcfg.cooldown_bars,
                        allow_short=rcfg.allow_short,
                        use_vol_scaling=getattr(rcfg, "use_vol_scaling", True),
                        use_gate_size_factor=getattr(rcfg, "use_gate_size_factor", True),
                        entry_edge_bps=getattr(rcfg, "entry_edge_bps", 0.0),
                        entry_cost_buffer_bps=getattr(rcfg, "entry_cost_buffer_bps", 0.0),
                        entry_cost_coverage_ratio=getattr(rcfg, "entry_cost_coverage_ratio", 1.0),
                        entry_cost_roundtrip_multiplier=getattr(
                            rcfg, "entry_cost_roundtrip_multiplier", 1.0
                        ),
                        entry_min_atr_to_cost_ratio=getattr(
                            rcfg, "entry_min_atr_to_cost_ratio", 0.0
                        ),
                        override_max_structure_range_pos=getattr(
                            rcfg, "override_max_structure_range_pos", 1.0
                        ),
                        override_min_drawdown_from_peak_bps=getattr(
                            rcfg, "override_min_drawdown_from_peak_bps", 0.0
                        ),
                        override_min_drawdown_to_cost_ratio=getattr(
                            rcfg, "override_min_drawdown_to_cost_ratio", 0.0
                        ),
                        override_min_slope_short_bps=getattr(
                            rcfg, "override_min_slope_short_bps", -999.0
                        ),
                        override_max_trend_return_bps=getattr(
                            rcfg, "override_max_trend_return_bps", 0.0
                        ),
                        override_max_context_range_pos=getattr(
                            rcfg, "override_max_context_range_pos", 1.0
                        ),
                        late_entry_block_context_range_pos=getattr(
                            rcfg, "late_entry_block_context_range_pos", 1.0
                        ),
                        late_entry_block_structure_range_pos=getattr(
                            rcfg, "late_entry_block_structure_range_pos", 1.0
                        ),
                        late_entry_block_max_context_drawdown_bps=getattr(
                            rcfg, "late_entry_block_max_context_drawdown_bps", 0.0
                        ),
                        late_entry_block_min_trend_return_bps=getattr(
                            rcfg, "late_entry_block_min_trend_return_bps", 0.0
                        ),
                        late_entry_block_min_return_bps=getattr(
                            rcfg, "late_entry_block_min_return_bps", 0.0
                        ),
                        exit_edge_bps=getattr(rcfg, "exit_edge_bps", 0.0),
                        exit_bypass_gate_edge_bps=getattr(rcfg, "exit_bypass_gate_edge_bps", 0.0),
                        min_hold_bars=getattr(rcfg, "min_hold_bars", 0),
                        failed_start_exit_enabled=getattr(rcfg, "failed_start_exit_enabled", False),
                        failed_start_min_bars=getattr(rcfg, "failed_start_min_bars", 0),
                        failed_start_max_bars=getattr(rcfg, "failed_start_max_bars", 0),
                        failed_start_min_rebound_bps=getattr(rcfg, "failed_start_min_rebound_bps", 0.0),
                        failed_start_loss_bps=getattr(rcfg, "failed_start_loss_bps", 0.0),
                        chop_break_even_reclaim_enabled=getattr(
                            rcfg, "chop_break_even_reclaim_enabled", False
                        ),
                        chop_break_even_reclaim_min_bars=getattr(
                            rcfg, "chop_break_even_reclaim_min_bars", 0
                        ),
                        chop_break_even_reclaim_min_drawdown_bps=getattr(
                            rcfg, "chop_break_even_reclaim_min_drawdown_bps", 0.0
                        ),
                        chop_break_even_reclaim_max_edge_bps=getattr(
                            rcfg, "chop_break_even_reclaim_max_edge_bps", 0.0
                        ),
                        chop_break_even_reclaim_cross_window_bars=getattr(
                            rcfg, "chop_break_even_reclaim_cross_window_bars", 0
                        ),
                        chop_break_even_reclaim_min_crosses=getattr(
                            rcfg, "chop_break_even_reclaim_min_crosses", 0
                        ),
                        peak_profit_retrace_enabled=getattr(rcfg, "peak_profit_retrace_enabled", False),
                        peak_profit_retrace_arm_bps=getattr(rcfg, "peak_profit_retrace_arm_bps", 0.0),
                        peak_profit_retrace_pct=getattr(rcfg, "peak_profit_retrace_pct", 0.0),
                        require_break_even_for_exit=getattr(rcfg, "require_break_even_for_exit", False),
                        allow_reversal_exit_after_break_even=getattr(
                            rcfg, "allow_reversal_exit_after_break_even", False
                        ),
                        time_break_even_floor_enabled=getattr(
                            rcfg, "time_break_even_floor_enabled", False
                        ),
                        time_break_even_floor_bars=getattr(rcfg, "time_break_even_floor_bars", 0),
                        red_candle_exit_enabled=getattr(rcfg, "red_candle_exit_enabled", False),
                        red_candle_window_bars=getattr(rcfg, "red_candle_window_bars", 0),
                        green_candle_take_exit_enabled=getattr(
                            rcfg, "green_candle_take_exit_enabled", False
                        ),
                        green_candle_take_min_bars=getattr(rcfg, "green_candle_take_min_bars", 0),
                        green_candle_take_max_bars=getattr(rcfg, "green_candle_take_max_bars", 0),
                        green_candle_take_required_green_bars=getattr(
                            rcfg, "green_candle_take_required_green_bars", 0
                        ),
                        green_candle_take_min_profit_bps=getattr(
                            rcfg, "green_candle_take_min_profit_bps", 0.0
                        ),
                        min_exit_profit_bps=getattr(rcfg, "min_exit_profit_bps", 0.0),
                        hard_take_profit_bps=getattr(rcfg, "hard_take_profit_bps", 0.0),
                        dynamic_profit_target_enabled=getattr(rcfg, "dynamic_profit_target_enabled", False),
                        dynamic_profit_target_bps_at_low=getattr(rcfg, "dynamic_profit_target_bps_at_low", 0.0),
                        dynamic_profit_break_even_from_high_pct=getattr(
                            rcfg,
                            "dynamic_profit_break_even_from_high_pct",
                            0.0,
                        ),
                        hard_stop_loss_bps=getattr(rcfg, "hard_stop_loss_bps", 0.0),
                        hard_take_profit_only_in_range=getattr(rcfg, "hard_take_profit_only_in_range", False),
                        trailing_stop_enabled=getattr(rcfg, "trailing_stop_enabled", False),
                        trailing_activation_bps=getattr(rcfg, "trailing_activation_bps", 0.0),
                        trailing_stop_bps=getattr(rcfg, "trailing_stop_bps", 0.0),
                        trailing_stop_atr_mult=getattr(rcfg, "trailing_stop_atr_mult", 0.0),
                        reentry_min_move_bps=getattr(rcfg, "reentry_min_move_bps", 0.0),
                        reentry_cooldown_bars_after_trailing_stop=getattr(
                            rcfg, "reentry_cooldown_bars_after_trailing_stop", 0
                        ),
                        reentry_cooldown_bars_after_whipsaw_stop_loss=getattr(
                            rcfg, "reentry_cooldown_bars_after_whipsaw_stop_loss", 0
                        ),
                        reentry_whipsaw_hard_stop_max_bars=getattr(
                            rcfg, "reentry_whipsaw_hard_stop_max_bars", 0
                        ),
                        reentry_loss_cluster_window_bars=getattr(
                            rcfg, "reentry_loss_cluster_window_bars", 0
                        ),
                        reentry_cooldown_bars_after_loss_cluster=getattr(
                            rcfg, "reentry_cooldown_bars_after_loss_cluster", 0
                        ),
                        reentry_cooldown_bars_after_weak_exit=getattr(
                            rcfg, "reentry_cooldown_bars_after_weak_exit", 0
                        ),
                        rebalance_min_delta_eur=getattr(rcfg, "rebalance_min_delta_eur", 0.0),
                        position_epsilon_btc=getattr(rcfg, "position_epsilon_btc", 1e-12),
                        position_epsilon_eur=getattr(rcfg, "position_epsilon_eur", 0.0),
                        min_entry_depth_eur=getattr(rcfg, "min_entry_depth_eur", 0.0),
                        max_entry_notional_to_depth_ratio=getattr(
                            rcfg, "max_entry_notional_to_depth_ratio", 0.0
                        ),
                        full_position_only=getattr(rcfg, "full_position_only", False),
                    )
                )
                runtime_pipeline["risk_manager"] = risk_manager
                # Sim UX: keep cycle trade notional in sync with the configured amount.
                if ctx.mode == "sim":
                    try:
                        order_builder.config.cycle_trade_eur = float(max_exposure_eur)
                    except Exception:
                        pass

                _send_journal(
                    ctx,
                    "core_budget_reset",
                    {
                        "starting_cash_eur": float(starting_cash_eur),
                        "max_exposure_eur": float(max_exposure_eur),
                        "reset": reset,
                        "cutoff_ts": cmd.ts.isoformat(),
                    },
                )

                saw_disable = True
                disable_reason = "budget_reset"
            elif cmd.action == "SYNC_ACCOUNT":
                payload = cmd.payload if isinstance(cmd.payload, dict) else {}

                def _as_nonneg_float(key: str, default: float = 0.0) -> float:
                    try:
                        raw = payload.get(key, default)
                        value = float(raw)
                    except Exception:
                        return float(default)
                    return max(0.0, value)

                cash_eur = _as_nonneg_float("cash_eur", 0.0)
                position_btc = _as_nonneg_float("position_btc", 0.0)
                reset = bool(payload.get("reset", True))
                entry_ts_raw = payload.get("entry_ts")
                if reset:
                    state_manager = StateManager(starting_cash_eur=cash_eur)
                    order_times.clear()
                    order_seq = 0
                    inflight_order_ids.clear()
                    inflight_exit_order_ids.clear()
                    ignore_reports_before_ts = cmd.ts
                    budget_cutoff_ts = cmd.ts

                pos = state_manager.position
                pos.cash_eur = cash_eur
                pos.position_btc = position_btc
                reference_price = _as_nonneg_float("avg_entry_price", 0.0)
                if reference_price <= 0.0:
                    reference_price = _as_nonneg_float("mark_price", 0.0)
                if reference_price <= 0.0 and last_mark_price is not None:
                    reference_price = max(0.0, float(last_mark_price))

                if position_btc > 0.0 and reference_price > 0.0:
                    pos.avg_entry_price = reference_price
                    pos.cost_basis_eur = position_btc * reference_price
                    position_reference_pending = False
                elif position_btc > 0.0:
                    # Existing exchange position without entry metadata:
                    # defer neutral entry reference to next market mark.
                    pos.avg_entry_price = 0.0
                    pos.cost_basis_eur = 0.0
                    position_reference_pending = True
                else:
                    pos.avg_entry_price = 0.0
                    pos.cost_basis_eur = 0.0
                    position_reference_pending = False

                equity_ref = cash_eur + (position_btc * reference_price if reference_price > 0.0 else 0.0)
                if equity_ref <= 0.0:
                    equity_ref = cash_eur
                pos.peak_equity_eur = max(0.0, equity_ref)
                pos.day_start_equity_eur = max(0.0, equity_ref)
                current_starting_cash_eur = max(0.0, equity_ref)
                _reset_risk_account_state_from_sync(
                    risk_manager,
                    sync_ts=cmd.ts,
                    equity_ref=equity_ref,
                    realized_pnl_eur=pos.realized_pnl_eur,
                )
                restored_bars_in_position = _restore_risk_position_state_from_sync(
                    risk_manager,
                    position_btc=position_btc,
                    reference_price=reference_price,
                    entry_ts_raw=entry_ts_raw,
                    sync_ts=cmd.ts,
                    bar_seconds=md_interval_seconds,
                )
                recovered_reentry_state = None
                if position_btc <= 0.0:
                    recovered_reentry_state = _restore_risk_flat_reentry_state_from_journal(
                        risk_manager,
                        cfg,
                        sync_ts=cmd.ts,
                        bar_seconds=md_interval_seconds,
                    )

                _send_journal(
                    ctx,
                    "core_account_synced",
                    {
                        "source": payload.get("source"),
                        "pair": payload.get("pair"),
                        "base_asset": payload.get("base_asset"),
                        "quote_asset": payload.get("quote_asset"),
                        "cash_eur": cash_eur,
                        "position_btc": position_btc,
                        "reference_price": reference_price,
                        "entry_ts": str(entry_ts_raw or "").strip() or None,
                        "restored_bars_in_position": restored_bars_in_position,
                        "reference_pending": position_reference_pending,
                        "reset": reset,
                    },
                )
                if recovered_reentry_state:
                    _send_journal(
                        ctx,
                        "core_reentry_state_recovered",
                        {
                            "source": "journal",
                            "last_long_exit_price": recovered_reentry_state.get("last_long_exit_price"),
                            "last_long_entry_price": recovered_reentry_state.get("last_long_entry_price"),
                            "reentry_cooldown_remaining": recovered_reentry_state.get(
                                "reentry_cooldown_remaining"
                            ),
                            "short_loss_cluster_count": recovered_reentry_state.get(
                                "short_loss_cluster_count"
                            ),
                            "short_loss_cluster_age_bars": recovered_reentry_state.get(
                                "short_loss_cluster_age_bars"
                            ),
                            "exit_reason": recovered_reentry_state.get("exit_reason"),
                            "exit_ts": recovered_reentry_state.get("exit_ts"),
                        },
                    )
            elif cmd.action == "RELOAD":
                saw_reload = True
                reload_reason = cmd_reason
                reload_count += 1
            elif cmd.action == "CANCEL_ALL":
                # Valid control action, but not a core state transition.
                _send_journal(ctx, "core_control", {"action": cmd.action, "reason": cmd.reason})
            else:
                _send_journal(
                    ctx,
                    "core_error",
                    {"reason": "unknown_control_action", "action": cmd.action, "cmd_reason": cmd.reason},
                )

        if saw_disable:
            _disable_trading(
                disable_reason or "stop",
                propagate=False,
                clear_resume_after=not saw_rate_limit_pause,
            )
            return True
        if saw_reload and reload_reason is not None:
            _send_journal(
                ctx,
                "core_control",
                {"action": "RELOAD", "reason": reload_reason, "batched_count": reload_count},
            )
            _reload_runtime_components(reload_reason)
        if saw_enable and not disable_seen_this_tick and enable_reason is not None and enable_action is not None:
            _enable_trading(enable_reason, enable_action)
        return disable_seen_this_tick

    _send_journal(ctx, "core_start", {"mode": ctx.mode})
    warmup_enabled = bool(_cfg(cfg, "core.warmup.enabled", warmup_enabled_default))
    if warmup_enabled:
        try:
            report = _run_warmup_with_heartbeat(cfg, feature_engine, alpha_model)
            warmup_close_prices = report.pop("_close_prices", [])
            if floor_anchor_enabled and floor_anchor_window_bars > 0 and isinstance(warmup_close_prices, list):
                for raw_px in warmup_close_prices[-floor_anchor_window_bars:]:
                    try:
                        px = float(raw_px)
                    except Exception:
                        continue
                    if px > 0.0:
                        floor_anchor_prices.append(px)
            if profit_corridor_enabled and profit_corridor_window_bars > 0 and isinstance(warmup_close_prices, list):
                for raw_px in warmup_close_prices[-profit_corridor_window_bars:]:
                    try:
                        px = float(raw_px)
                    except Exception:
                        continue
                    if px > 0.0:
                        profit_corridor_prices.append(px)
            if (
                profit_corridor_enabled
                and profit_corridor_fast_window_bars > 0
                and isinstance(warmup_close_prices, list)
            ):
                for raw_px in warmup_close_prices[-profit_corridor_fast_window_bars:]:
                    try:
                        px = float(raw_px)
                    except Exception:
                        continue
                    if px > 0.0:
                        profit_corridor_fast_prices.append(px)
            _send_journal(ctx, "core_warmup", report)
        except Exception as exc:
            _send_journal(ctx, "core_warmup_error", {"error": str(exc)})

    while not ctx.stop_event.is_set():
        try:
            now = time.time()
            disable_seen_this_tick = False

            # control commands: drain to process all pending state transitions deterministically
            disable_seen_this_tick = _drain_control_commands(now, disable_seen_this_tick)
            feature_engine = runtime_pipeline["feature_engine"]
            alpha_model = runtime_pipeline["alpha_model"]
            cost_model = runtime_pipeline["cost_model"]
            gate = runtime_pipeline["gate"]
            risk_manager = runtime_pipeline["risk_manager"]
            order_builder = runtime_pipeline["order_builder"]

            # execution reports / fills (best-effort; never kill the loop)
            while True:
                try:
                    msg = ctx.q_exec_report.get_nowait()
                except Empty:
                    break
                if isinstance(msg, Fill):
                    if ignore_reports_before_ts is not None and msg.ts <= ignore_reports_before_ts:
                        continue
                    fill_ids: set[str] = set()
                    if msg.order_id:
                        fill_ids.add(str(msg.order_id))
                        fill_ids.update(order_aliases_by_id.pop(str(msg.order_id), set()))
                    try:
                        fill_meta = getattr(msg, "meta", {})
                        if isinstance(fill_meta, dict):
                            raw_aliases = fill_meta.get("order_aliases")
                            if isinstance(raw_aliases, str):
                                fill_ids.add(raw_aliases)
                            elif isinstance(raw_aliases, (list, tuple, set)):
                                fill_ids.update(str(a) for a in raw_aliases if a is not None)
                    except Exception:
                        pass
                    for inflight_id in fill_ids:
                        inflight_order_ids.discard(inflight_id)
                        inflight_exit_order_ids.discard(inflight_id)
                    state_manager.apply_fill(msg)
                elif hasattr(msg, "status"):
                    # keep for telemetry or audit
                    try:
                        if ignore_reports_before_ts is not None and getattr(msg, "ts", None) is not None and msg.ts <= ignore_reports_before_ts:
                            continue
                    except Exception:
                        pass
                    try:
                        oid = str(getattr(msg, "order_id", "") or "").strip()
                        st = str(getattr(msg, "status", "") or "").upper().strip()
                        meta = getattr(msg, "meta", {})
                        aliases: list[str] = []
                        if isinstance(meta, dict):
                            raw_aliases = meta.get("order_aliases")
                            if isinstance(raw_aliases, str):
                                aliases = [raw_aliases]
                            elif isinstance(raw_aliases, (list, tuple, set)):
                                aliases = [str(a) for a in raw_aliases if a is not None]
                        if oid and aliases:
                            known_aliases = order_aliases_by_id.setdefault(oid, set())
                            known_aliases.update(alias for alias in aliases if alias and alias != oid)
                        if oid:
                            if st in {"FILLED", "CANCELED", "REJECTED"}:
                                inflight_order_ids.discard(oid)
                                inflight_exit_order_ids.discard(oid)
                                for alias in aliases:
                                    inflight_order_ids.discard(alias)
                                    inflight_exit_order_ids.discard(alias)
                                    terminal_order_ids.add(alias)
                                terminal_order_ids.add(oid)
                                order_aliases_by_id.pop(oid, None)
                            elif st:
                                should_add = oid not in terminal_order_ids
                                if should_add:
                                    inflight_order_ids.add(oid)
                                    if any(alias in inflight_exit_order_ids for alias in aliases):
                                        inflight_exit_order_ids.add(oid)
                                for alias in aliases:
                                    if alias in terminal_order_ids:
                                        continue
                                    if should_add:
                                        inflight_order_ids.add(alias)
                    except Exception:
                        pass
                    _send_journal(
                        ctx,
                        "exec_report",
                        {
                            "ts": getattr(msg, "ts", datetime.now(timezone.utc)).isoformat(),
                            "order_id": getattr(msg, "order_id", ""),
                            "status": getattr(msg, "status", ""),
                            "latency_ms": getattr(msg, "latency_ms", 0.0),
                        },
                    )

            # market events: block briefly, then keep only latest for backpressure safety
            event: MarketEvent | None = None
            dropped_market_events = 0
            try:
                event = ctx.q_market_core.get(timeout=0.5)
                while True:
                    try:
                        event = ctx.q_market_core.get_nowait()
                        dropped_market_events += 1
                    except Empty:
                        break
            except Empty:
                event = None

            # non-blocking news consumption (latest snapshot wins)
            if ctx.q_impact_core is not None:
                while True:
                    try:
                        msg = ctx.q_impact_core.get_nowait()
                    except Empty:
                        break
                    if isinstance(msg, NewsEvent):
                        latest_news = msg

            # auto-resume after rate-limit pause (only if no disable happened this tick)
            if (
                auto_resume_rate_limit
                and resume_after is not None
                and time.time() >= resume_after
                and trading_disable_reason == "rate_limit"
                and not disable_seen_this_tick
            ):
                _enable_trading("rate_limit_resume", "AUTO_RESUME")

            if event is not None:
                last_market_arrival = time.time()
                last_mark_price = float(event.close)
                if floor_anchor_enabled and floor_anchor_window_bars > 0:
                    floor_anchor_prices.append(float(event.close))
                if profit_corridor_enabled and profit_corridor_window_bars > 0:
                    profit_corridor_prices.append(float(event.close))
                if profit_corridor_enabled and profit_corridor_fast_window_bars > 0:
                    profit_corridor_fast_prices.append(float(event.close))
                if trading_disable_reason == "stale_market_data" and not disable_seen_this_tick:
                    _enable_trading("market_data_fresh", "AUTO_RESUME", propagate=True)
                try:
                    active_feature_engine = runtime_pipeline["feature_engine"]
                    active_alpha_model = runtime_pipeline["alpha_model"]
                    active_cost_model = runtime_pipeline["cost_model"]
                    active_gate = runtime_pipeline["gate"]
                    active_risk_manager = runtime_pipeline["risk_manager"]
                    active_order_builder = runtime_pipeline["order_builder"]

                    if position_reference_pending and state_manager.position.position_btc > 0.0:
                        ref = float(event.close)
                        state_manager.position.avg_entry_price = ref
                        state_manager.position.cost_basis_eur = float(state_manager.position.position_btc) * ref
                        state_manager.position.peak_equity_eur = max(
                            float(state_manager.position.peak_equity_eur),
                            float(state_manager.position.cash_eur) + float(state_manager.position.position_btc) * ref,
                        )
                        current_starting_cash_eur = (
                            float(state_manager.position.cash_eur) + float(state_manager.position.position_btc) * ref
                        )
                        position_reference_pending = False
                        _send_journal(
                            ctx,
                            "core_position_reference_initialized",
                            {"reference_price": ref, "source": "market_mark"},
                        )

                    # deterministic pipeline: features -> alpha -> cost -> gate -> risk -> intents
                    features = active_feature_engine.compute(event)
                    news_snapshot = {
                        "present": False,
                        "sentiment_score": 0.0,
                        "impact_score": 0.0,
                        "impact": 0.0,
                        "source_count": 0,
                        "age_sec": 0.0,
                        "age": 0.0,
                        "event_id": None,
                    }
                    if latest_news is not None:
                        news_age_sec = max(0.0, (event.ts - latest_news.ts).total_seconds())
                        news_snapshot = {
                            "present": True,
                            "sentiment_score": float(latest_news.sentiment_score),
                            "impact_score": float(latest_news.impact_score),
                            "impact": float(latest_news.impact_score),
                            "source_count": int(latest_news.source_count),
                            "age_sec": float(news_age_sec),
                            "age": float(news_age_sec),
                            "event_id": latest_news.event_id,
                        }
                    features.values["news_sentiment"] = float(news_snapshot["sentiment_score"])
                    features.values["news_impact"] = float(news_snapshot["impact_score"])
                    features.values["news_source_count"] = float(news_snapshot["source_count"])
                    features.values["news_age_sec"] = float(news_snapshot["age_sec"])
                    floor_price = 0.0
                    floor_distance_bps = 0.0
                    floor_rebound_bps = 0.0
                    floor_ready = False
                    floor_buy_zone = False
                    floor_rebound_ok = False
                    corridor_low_price = 0.0
                    corridor_high_price = 0.0
                    corridor_break_even_anchor_price = 0.0
                    corridor_position_pct = 0.0
                    corridor_ready = False
                    corridor_base_ready = False
                    corridor_fast_ready = False
                    corridor_base_low_price = 0.0
                    corridor_base_high_price = 0.0
                    corridor_fast_low_price = 0.0
                    corridor_fast_high_price = 0.0
                    if floor_anchor_enabled and len(floor_anchor_prices) >= floor_anchor_min_bars:
                        sorted_prices = sorted(float(px) for px in floor_anchor_prices if float(px) > 0.0)
                        if sorted_prices:
                            floor_idx = min(
                                len(sorted_prices) - 1,
                                max(0, int(round((len(sorted_prices) - 1) * floor_anchor_percentile))),
                            )
                            floor_price = float(sorted_prices[floor_idx])
                            if floor_price > 0.0:
                                floor_distance_bps = ((float(event.close) / floor_price) - 1.0) * 10000.0
                                floor_ready = True
                        rebound_window = [
                            float(px)
                            for px in list(floor_anchor_prices)[-floor_anchor_rebound_lookback_bars:]
                            if float(px) > 0.0
                        ]
                        if rebound_window:
                            local_floor = min(rebound_window)
                            if local_floor > 0.0:
                                floor_rebound_bps = ((float(event.close) / local_floor) - 1.0) * 10000.0
                    if floor_ready and floor_anchor_max_distance_bps > 0.0:
                        floor_buy_zone = floor_distance_bps <= floor_anchor_max_distance_bps
                    if floor_ready:
                        floor_rebound_ok = floor_rebound_bps >= floor_anchor_min_rebound_bps
                        if floor_rebound_ok and floor_anchor_max_rebound_bps > 0.0:
                            floor_rebound_ok = floor_rebound_bps <= floor_anchor_max_rebound_bps
                    features.values["floor_price"] = float(floor_price)
                    features.values["floor_distance_bps"] = float(floor_distance_bps)
                    features.values["floor_rebound_bps"] = float(floor_rebound_bps)
                    features.values["floor_ready"] = 1.0 if floor_ready else 0.0
                    features.values["floor_buy_zone"] = 1.0 if floor_buy_zone else 0.0
                    features.values["floor_rebound_ok"] = 1.0 if floor_rebound_ok else 0.0
                    if profit_corridor_enabled:
                        def _window_low_high(window: List[float]) -> tuple[float, float]:
                            if not window:
                                return 0.0, 0.0
                            if not profit_corridor_robust_range_enabled:
                                return float(min(window)), float(max(window))
                            sorted_window = sorted(window)
                            low_q = max(0.0, min(1.0, profit_corridor_robust_low_pct / 100.0))
                            high_q = max(0.0, min(1.0, profit_corridor_robust_high_pct / 100.0))
                            if high_q <= low_q:
                                high_q = min(1.0, low_q + 0.01)
                            low = _quantile_from_sorted(sorted_window, low_q)
                            high = _quantile_from_sorted(sorted_window, high_q)
                            if high <= low:
                                low = float(sorted_window[0])
                                high = float(sorted_window[-1])
                            return float(low), float(high)

                        base_window = [float(px) for px in profit_corridor_prices if float(px) > 0.0]
                        if len(base_window) >= profit_corridor_min_bars:
                            base_low, base_high = _window_low_high(base_window)
                            if base_high > base_low > 0.0:
                                corridor_base_ready = True
                                corridor_base_low_price = float(base_low)
                                corridor_base_high_price = float(base_high)

                        fast_window = [float(px) for px in profit_corridor_fast_prices if float(px) > 0.0]
                        if len(fast_window) >= profit_corridor_fast_min_bars:
                            fast_low, fast_high = _window_low_high(fast_window)
                            if fast_high > fast_low > 0.0:
                                corridor_fast_ready = True
                                corridor_fast_low_price = float(fast_low)
                                corridor_fast_high_price = float(fast_high)

                        if corridor_base_ready and corridor_fast_ready:
                            blend_w = max(0.0, min(1.0, float(profit_corridor_fast_blend_weight)))
                            corridor_low_price = (
                                (1.0 - blend_w) * corridor_base_low_price
                                + blend_w * corridor_fast_low_price
                            )
                            corridor_high_price = (
                                (1.0 - blend_w) * corridor_base_high_price
                                + blend_w * corridor_fast_high_price
                            )
                            if corridor_high_price <= corridor_low_price:
                                corridor_low_price = min(corridor_base_low_price, corridor_fast_low_price)
                                corridor_high_price = max(corridor_base_high_price, corridor_fast_high_price)
                            corridor_ready = corridor_high_price > corridor_low_price > 0.0
                        elif corridor_base_ready:
                            corridor_low_price = corridor_base_low_price
                            corridor_high_price = corridor_base_high_price
                            corridor_ready = True
                        elif corridor_fast_ready:
                            corridor_low_price = corridor_fast_low_price
                            corridor_high_price = corridor_fast_high_price
                            corridor_ready = True

                        if corridor_ready:
                            corridor_position_pct = (
                                (float(event.close) - corridor_low_price)
                                / (corridor_high_price - corridor_low_price)
                            ) * 100.0
                            corridor_position_pct = max(0.0, min(100.0, corridor_position_pct))
                            break_even_from_high_pct = max(
                                0.0,
                                float(
                                    getattr(
                                        active_risk_manager.config,
                                        "dynamic_profit_break_even_from_high_pct",
                                        0.0,
                                    )
                                    or 0.0
                                ),
                            )
                            corridor_break_even_anchor_price = corridor_high_price - (
                                (corridor_high_price - corridor_low_price)
                                * (break_even_from_high_pct / 100.0)
                            )
                    features.values["corridor_low_price"] = float(corridor_low_price)
                    features.values["corridor_high_price"] = float(corridor_high_price)
                    features.values["corridor_break_even_anchor_price"] = float(corridor_break_even_anchor_price)
                    features.values["corridor_position_pct"] = float(corridor_position_pct)
                    features.values["corridor_ready"] = 1.0 if corridor_ready else 0.0
                    features.values["corridor_base_ready"] = 1.0 if corridor_base_ready else 0.0
                    features.values["corridor_fast_ready"] = 1.0 if corridor_fast_ready else 0.0
                    features.values["corridor_base_low_price"] = float(corridor_base_low_price)
                    features.values["corridor_base_high_price"] = float(corridor_base_high_price)
                    features.values["corridor_fast_low_price"] = float(corridor_fast_low_price)
                    features.values["corridor_fast_high_price"] = float(corridor_fast_high_price)
                    features.values["corridor_fast_blend_weight"] = float(profit_corridor_fast_blend_weight)
                    features.values["corridor_robust_range_enabled"] = (
                        1.0 if profit_corridor_robust_range_enabled else 0.0
                    )
                    features.values["corridor_robust_low_pct"] = float(profit_corridor_robust_low_pct)
                    features.values["corridor_robust_high_pct"] = float(profit_corridor_robust_high_pct)
                    features.values["corridor_staged_mode_enabled"] = (
                        1.0 if profit_corridor_staged_mode_enabled else 0.0
                    )
                    features.values["corridor_staged_entry_1_pct"] = float(profit_corridor_staged_entry_1_pct)
                    features.values["corridor_staged_entry_2_pct"] = float(profit_corridor_staged_entry_2_pct)
                    features.values["corridor_staged_entry_3_pct"] = float(profit_corridor_staged_entry_3_pct)
                    features.values["corridor_staged_entry_4_pct"] = float(profit_corridor_staged_entry_4_pct)
                    features.values["corridor_staged_no_buy_above_pct"] = float(
                        profit_corridor_staged_no_buy_above_pct
                    )
                    features.values["corridor_staged_exit_step_pct"] = float(profit_corridor_staged_exit_step_pct)
                    features.values["corridor_staged_hysteresis_pct"] = float(
                        profit_corridor_staged_hysteresis_pct
                    )
                    features.values["corridor_staged_exit_retrace_pct"] = float(
                        profit_corridor_staged_exit_retrace_pct
                    )
                    features.values["corridor_staged_entry_wait_bars"] = float(
                        profit_corridor_staged_entry_wait_bars
                    )
                    features.values["corridor_staged_transition_smoothing_bars"] = float(
                        profit_corridor_staged_transition_smoothing_bars
                    )
                    features.values["corridor_staged_require_rising"] = (
                        1.0 if profit_corridor_staged_require_rising else 0.0
                    )
                    features.values["corridor_staged_profit_target_enabled"] = (
                        1.0 if profit_corridor_staged_profit_target_enabled else 0.0
                    )
                    features.values["corridor_staged_profit_target_base_pct"] = float(
                        profit_corridor_staged_profit_target_base_pct
                    )
                    features.values["corridor_staged_profit_target_min_pct"] = float(
                        profit_corridor_staged_profit_target_min_pct
                    )
                    features.values["corridor_staged_profit_target_max_pct"] = float(
                        profit_corridor_staged_profit_target_max_pct
                    )
                    features.values["corridor_staged_profit_target_mult_10"] = float(
                        profit_corridor_staged_profit_target_mult_10
                    )
                    features.values["corridor_staged_profit_target_mult_20"] = float(
                        profit_corridor_staged_profit_target_mult_20
                    )
                    features.values["corridor_staged_profit_target_mult_30"] = float(
                        profit_corridor_staged_profit_target_mult_30
                    )
                    features.values["corridor_staged_profit_target_mult_40"] = float(
                        profit_corridor_staged_profit_target_mult_40
                    )
                    features.values["corridor_staged_profit_target_mult_50"] = float(
                        profit_corridor_staged_profit_target_mult_50
                    )

                    alpha = active_alpha_model.predict(features)
                    alpha_meta = alpha.meta if isinstance(alpha.meta, dict) else {}
                    _inject_alpha_features(features, alpha_meta)
                    alpha_regime = str(alpha_meta.get("regime", "") or "").strip().lower() or None
                    last_active_strategy = (
                        str(alpha_meta.get("active_strategy", "") or "").strip().lower()
                    )
                    last_alpha_regime = str(alpha_regime or "")
                    last_alpha_regime_reason = (
                        str(alpha_meta.get("regime_reason", "") or "").strip().lower()
                    )
                    # Treat news as an additive edge component in bps (config-controlled).
                    news_sent = float(features.values.get("news_sentiment", 0.0))
                    news_imp = float(features.values.get("news_impact", 0.0))
                    news_bias_bps = news_edge_scale_bps * news_sent * news_imp
                    floor_bonus_bps = 0.0
                    if (
                        floor_anchor_enabled
                        and floor_ready
                        and floor_buy_zone
                        and floor_rebound_ok
                        and floor_anchor_entry_bonus_bps > 0.0
                    ):
                        proximity_weight = 1.0
                        if floor_anchor_max_distance_bps > 0.0:
                            proximity_weight = 1.0 - max(0.0, floor_distance_bps) / float(floor_anchor_max_distance_bps)
                        proximity_weight = max(0.0, min(1.0, proximity_weight))
                        rebound_weight = 1.0
                        if floor_anchor_max_rebound_bps > floor_anchor_min_rebound_bps:
                            rebound_weight = 1.0 - (
                                (floor_rebound_bps - floor_anchor_min_rebound_bps)
                                / (floor_anchor_max_rebound_bps - floor_anchor_min_rebound_bps)
                            )
                        rebound_weight = max(0.0, min(1.0, rebound_weight))
                        floor_bonus_bps = float(floor_anchor_entry_bonus_bps) * proximity_weight * rebound_weight
                    effective_edge_bps = float(alpha.edge_bps) + float(news_bias_bps) + float(floor_bonus_bps)
                    cost = active_cost_model.estimate(features, active_order_builder.config.order_type)
                    last_expected_cost_bps = float(cost.expected_cost_bps or 0.0)
                    gate_decision = active_gate.evaluate(features, cost, effective_edge_bps)
                    pre_state = state_manager.snapshot(event.ts, event.close)
                    _apply_dynamic_sizing(pre_state)
                    risk = active_risk_manager.decide(
                        pre_state,
                        features,
                        gate_decision,
                        effective_edge_bps,
                        expected_cost_bps=float(cost.expected_cost_bps),
                        regime=alpha_regime,
                    )

                    # Optional: prevent opening new long positions under a strong "risk_off" news regime.
                    # Always allow exits (this only applies while flat).
                    if news_block_long_entries:
                        try:
                            eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
                        except Exception:
                            eps = 0.0
                        if abs(float(pre_state.position_btc)) <= max(1e-12, eps):
                            if (
                                float(risk.target_position_btc) > max(1e-12, eps)
                                and news_imp >= news_min_impact
                                and news_sent <= -abs(news_risk_off_threshold)
                            ):
                                risk = RiskDecision(
                                    ts=risk.ts,
                                    allow=risk.allow,
                                    target_position_btc=0.0,
                                    reason="news_risk_off",
                                    cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                )

                    # Optional: require a positive impact/news bias before opening a fresh long.
                    # This is a stricter gate than risk_off blocking: no new long unless the 24h impact
                    # view is at least mildly constructive.
                    if news_require_long_bias:
                        try:
                            eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
                        except Exception:
                            eps = 0.0
                        if abs(float(pre_state.position_btc)) <= max(1e-12, eps):
                            if float(risk.target_position_btc) > max(1e-12, eps):
                                if (
                                    news_sent < news_long_entry_min_sentiment
                                    or news_imp < news_long_entry_min_impact
                                ):
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="news_long_bias_required",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )

                    # Optional: block new long entries when both medium-term context and trend are clearly negative.
                    if (
                        block_long_context_return_bps_below < 0.0
                        and block_long_trend_return_bps_below < 0.0
                    ):
                        try:
                            eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
                        except Exception:
                            eps = 0.0
                        if abs(float(pre_state.position_btc)) <= max(1e-12, eps):
                            if float(risk.target_position_btc) > max(1e-12, eps):
                                ctx_ret = float(features.values.get("context_return_bps", 0.0))
                                trend_ret = float(features.values.get("trend_return_bps", 0.0))
                                if (
                                    ctx_ret <= block_long_context_return_bps_below
                                    and trend_ret <= block_long_trend_return_bps_below
                                ):
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="context_downtrend_block",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )

                    # Optional coin-specific floor-anchor policy: only open a new long
                    # when price is still close enough to its rolling "floor".
                    if floor_anchor_enabled and floor_anchor_max_distance_bps > 0.0:
                        try:
                            eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
                        except Exception:
                            eps = 0.0
                        if abs(float(pre_state.position_btc)) <= max(1e-12, eps):
                            if float(risk.target_position_btc) > max(1e-12, eps):
                                if not floor_ready:
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="floor_anchor_not_ready",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )
                                elif not floor_buy_zone:
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="floor_anchor_too_high",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )
                                elif not floor_rebound_ok:
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="floor_anchor_wait_rebound",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )

                    # Global corridor readiness guard: do not open fresh longs until
                    # the corridor state is fully initialized after startup/reload.
                    if profit_corridor_enabled:
                        try:
                            eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
                        except Exception:
                            eps = 0.0
                        if abs(float(pre_state.position_btc)) <= max(1e-12, eps):
                            if float(risk.target_position_btc) > max(1e-12, eps) and not corridor_ready:
                                risk = RiskDecision(
                                    ts=risk.ts,
                                    allow=risk.allow,
                                    target_position_btc=0.0,
                                    reason="corridor_not_ready",
                                    cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                )

                    # Optional corridor policy: block new long entries when price is already too high
                    # inside the rolling corridor (e.g. top 25% of the observed range).
                    if (
                        profit_corridor_enabled
                        and not profit_corridor_staged_mode_enabled
                        and profit_corridor_max_entry_position_pct > 0.0
                    ):
                        try:
                            eps = float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0)
                        except Exception:
                            eps = 0.0
                        if abs(float(pre_state.position_btc)) <= max(1e-12, eps):
                            if float(risk.target_position_btc) > max(1e-12, eps):
                                if not corridor_ready:
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="corridor_not_ready",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )
                                elif corridor_position_pct >= profit_corridor_max_entry_position_pct:
                                    risk = RiskDecision(
                                        ts=risk.ts,
                                        allow=risk.allow,
                                        target_position_btc=0.0,
                                        reason="corridor_too_high",
                                        cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                                    )

                    # Manual-entry mode: never increase exposure automatically.
                    # Exits/reductions stay untouched so open/manual positions can still roll out.
                    if manual_entry_exit_only:
                        eps = max(
                            1e-12,
                            float(getattr(active_risk_manager.config, "position_epsilon_btc", 0.0) or 0.0),
                        )
                        cur_pos = float(active_risk_manager.effective_position_btc(pre_state, price_hint=event.close))
                        tgt_pos = float(risk.target_position_btc)
                        if abs(tgt_pos) > (abs(cur_pos) + eps):
                            risk = RiskDecision(
                                ts=risk.ts,
                                allow=risk.allow,
                                target_position_btc=float(cur_pos),
                                reason="manual_entry_exit_only",
                                cooldown_remaining=int(getattr(risk, "cooldown_remaining", 0) or 0),
                            )

                    if _is_hard_risk_reason(risk.reason):
                        _disable_trading(risk.reason or "risk_hard_stop", propagate=True)
                        disable_seen_this_tick = True

                    # Apply control commands again before intent emission: PAUSE/STOP should stop new intents ASAP.
                    disable_seen_this_tick = _drain_control_commands(time.time(), disable_seen_this_tick)

                    intents: List[OrderIntent] = []
                    max_orders_hit = False
                    can_emit_orders = False
                    if trading_enabled and risk.allow:
                        # Entry is gated by market quality/cost checks.
                        # Exit/reduce orders are allowed even if gate blocks.
                        eps = max(1e-12, float(getattr(active_order_builder.config, "min_trade_btc", 0.0) or 0.0))
                        cur_pos = float(active_risk_manager.effective_position_btc(pre_state, price_hint=event.close))
                        tgt_pos = float(risk.target_position_btc)
                        reducing_exposure = abs(tgt_pos) + eps < abs(cur_pos)
                        flattening = abs(tgt_pos) <= eps and abs(cur_pos) > eps
                        can_emit_orders = bool(gate_decision.allow or reducing_exposure or flattening)
                    if can_emit_orders:
                        now_rl = time.time()
                        while order_times and now_rl - order_times[0] > 60.0:
                            order_times.popleft()
                        if max_orders_per_min <= 0 or len(order_times) >= max_orders_per_min:
                            max_orders_hit = True
                            _disable_trading("max_orders_per_min", propagate=True)
                            disable_seen_this_tick = True
                        else:
                            cycle_mode = bool(
                                getattr(active_order_builder.config, "cycle_trade_eur", 0.0)
                                and active_order_builder.config.cycle_trade_eur > 0
                            )
                            if cycle_mode and inflight_order_ids:
                                orders = []
                            elif (reducing_exposure or flattening) and inflight_exit_order_ids:
                                orders = []
                            else:
                                buy_fee_bps = None
                                buy_price_buffer_bps = None
                                if ctx.mode in {"paper", "sim"} and active_order_builder.config.order_type == "market":
                                    try:
                                        spread_bps = float(features.values.get("spread_bps", 0.0))
                                        exec_slip_bps = float(_cfg(cfg, "execution.slippage_bps", 0.0))
                                        buy_fee_bps = float(cost.fee_bps)
                                        buy_price_buffer_bps = float(spread_bps) / 2.0 + float(exec_slip_bps)
                                    except Exception:
                                        buy_fee_bps = None
                                        buy_price_buffer_bps = None
                                orders = active_order_builder.build(
                                    risk,
                                    active_risk_manager.effective_position_btc(pre_state, price_hint=event.close),
                                    event.close,
                                    cash_eur=pre_state.cash_eur,
                                    buy_fee_bps=buy_fee_bps,
                                    buy_price_buffer_bps=buy_price_buffer_bps,
                                )
                            for order in orders:
                                order_seq += 1
                                intent = OrderIntent(
                                    ts=event.ts,
                                    side=order.side,
                                    qty_btc=order.qty_btc,
                                    order_type=order.order_type,
                                    limit_price=order.price,
                                    post_only=order.post_only,
                                    client_id=f"intent_{event.ts.timestamp()}_{order_seq}",
                                    reason=risk.reason,
                                    meta={
                                        "edge_bps": effective_edge_bps,
                                        "edge_bps_raw": alpha.edge_bps,
                                        "news_bias_bps": news_bias_bps,
                                        "floor_bonus_bps": floor_bonus_bps,
                                        "expected_cost_bps": cost.expected_cost_bps,
                                        "reference_price": event.close,
                                    },
                                )
                                if try_put(ctx.q_order_intent, intent):
                                    intents.append(intent)
                                    order_times.append(now_rl)
                                    inflight_order_ids.add(intent.client_id)
                                    if reducing_exposure or flattening:
                                        inflight_exit_order_ids.add(intent.client_id)

                    _send_journal(
                        ctx,
                        "core_decision",
                        {
                            "ts": event.ts.isoformat(),
                            "trading_enabled": trading_enabled,
                            "trading_disable_reason": trading_disable_reason,
                            "alpha_type": last_alpha_type,
                            "alpha_active_strategy": last_active_strategy or None,
                            "features": features.values,
                            "alpha": {
                                "type": last_alpha_type,
                                "active_strategy": last_active_strategy or None,
                                "model_class": type(active_alpha_model).__name__,
                                "edge_bps": alpha.edge_bps,
                                "edge_bps_effective": effective_edge_bps,
                                "news_bias_bps": news_bias_bps,
                                "floor_bonus_bps": floor_bonus_bps,
                                "meta": alpha.meta,
                            },
                            "cost": {
                                "fee_bps": cost.fee_bps,
                                "spread_bps": cost.spread_bps,
                                "slippage_bps": cost.slippage_bps,
                                "expected_cost_bps": cost.expected_cost_bps,
                            },
                            "gate": {
                                "allow": gate_decision.allow,
                                "size_factor": gate_decision.size_factor,
                                "reason": gate_decision.reason,
                            },
                            "news": news_snapshot,
                            "sizing": {
                                "max_exposure_mode": max_exposure_mode,
                                "max_exposure_eur": float(active_risk_manager.config.max_exposure_eur),
                                "cycle_trade_mode": cycle_trade_mode,
                                "cycle_trade_eur": float(active_order_builder.config.cycle_trade_eur),
                                "manual_entry_exit_only": manual_entry_exit_only,
                            },
                            "risk": {
                                "allow": risk.allow,
                                "target_btc": risk.target_position_btc,
                                "reason": risk.reason,
                                "cooldown_remaining": int(getattr(risk, "cooldown_remaining", 0) or 0),
                                "reentry_cooldown_remaining": int(
                                    getattr(active_risk_manager, "_reentry_cooldown_remaining", 0) or 0
                                ),
                                "short_loss_cluster_count": int(
                                    getattr(active_risk_manager, "_short_loss_cluster_count", 0) or 0
                                ),
                                "short_loss_cluster_age_bars": int(
                                    getattr(active_risk_manager, "_short_loss_cluster_age_bars", 0) or 0
                                ),
                                "last_long_exit_price": float(
                                    getattr(active_risk_manager, "_last_long_exit_price", 0.0) or 0.0
                                ),
                                "dynamic_profit_target_bps": float(
                                    getattr(active_risk_manager, "_dynamic_profit_target_bps_active", 0.0) or 0.0
                                ),
                                # Corridor staged-mode runtime state (for relay/UI):
                                # reflects actual "roll armed" state inside RiskManager.
                                "corridor_exit_armed": bool(
                                    getattr(active_risk_manager, "_corridor_exit_armed", False)
                                ),
                                "corridor_exit_peak_pct": float(
                                    getattr(active_risk_manager, "_corridor_exit_peak_pct", 0.0) or 0.0
                                ),
                                "corridor_entry_stage_pct": float(
                                    getattr(active_risk_manager, "_corridor_entry_stage_pct", 0.0) or 0.0
                                ),
                            },
                            "intents": [intent.client_id for intent in intents],
                            "max_orders_hit": max_orders_hit,
                            "dropped_market_events": dropped_market_events,
                            "equity": pre_state.equity_eur,
                        },
                    )
                except Exception as exc:  # defensive: keep core loop alive under malformed inputs
                    _send_journal(
                        ctx,
                        "core_error",
                        {
                            "reason": "decision_exception",
                            "error": str(exc),
                            "event_ts": event.ts.isoformat(),
                        },
                    )

            # stale detection
            if stale_seconds > 0 and (time.time() - last_market_arrival) > stale_seconds:
                if trading_enabled:
                    _disable_trading("stale_market_data", propagate=True)
                    _send_journal(ctx, "core_stale", {"last_market_arrival": last_market_arrival})

            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                hb_seq += 1
                _send_heartbeat(ctx, hb_seq)
                last_heartbeat = now
            if now - last_telemetry >= telemetry_interval:
                last_telemetry = now
                # Budget/exposure observability: use the latest mark price if available.
                pos = state_manager.position
                mark = float(last_mark_price) if last_mark_price is not None else None
                position_value_eur = abs(float(pos.position_btc) * float(mark)) if mark is not None else 0.0
                equity_eur = float(pos.cash_eur) + (float(pos.position_btc) * float(mark) if mark is not None else 0.0)
                unrealized_pnl_eur = 0.0
                if mark is not None and pos.position_btc > 0:
                    unrealized_pnl_eur = (float(mark) - float(pos.avg_entry_price)) * float(pos.position_btc)
                _send_telemetry(
                    ctx,
                    {
                        "mode": ctx.mode,
                        "trading_enabled": trading_enabled,
                        "trading_disable_reason": trading_disable_reason,
                        "starting_cash_eur": current_starting_cash_eur,
                        "max_exposure_mode": max_exposure_mode,
                        "max_exposure_eur": risk_manager.config.max_exposure_eur,
                        "cycle_trade_mode": cycle_trade_mode,
                        "cycle_trade_eur": float(order_builder.config.cycle_trade_eur),
                        "manual_entry_exit_only": manual_entry_exit_only,
                        "alpha_type": last_alpha_type,
                        "alpha_active_strategy": last_active_strategy or None,
                        "alpha_regime": last_alpha_regime or None,
                        "alpha_regime_reason": last_alpha_regime_reason or None,
                        "cash_eur": float(pos.cash_eur),
                        "position_btc": float(pos.position_btc),
                        "avg_entry_price": float(pos.avg_entry_price),
                        "realized_pnl_eur": float(pos.realized_pnl_eur),
                        "unrealized_pnl_eur": float(unrealized_pnl_eur),
                        "equity_eur": float(equity_eur),
                        "position_value_eur": float(position_value_eur),
                        "mark_price": mark,
                        "expected_cost_bps": float(last_expected_cost_bps),
                        "inflight_orders": len(inflight_order_ids),
                        "last_market_event_age": now - last_market_arrival,
                        "queue_market_core": queue_depth(ctx.q_market_core),
                        "queue_impact_core": queue_depth(ctx.q_impact_core) if ctx.q_impact_core is not None else 0,
                        "queue_order_intent": queue_depth(ctx.q_order_intent),
                        "queue_exec_report": queue_depth(ctx.q_exec_report),
                        "queue_control_core": queue_depth(ctx.q_control_core),
                        "queue_control_exec": queue_depth(ctx.q_control_exec),
                        "queue_journal": queue_depth(ctx.q_journal),
                        "queue_telemetry": queue_depth(ctx.q_telemetry),
                        "orders_last_min": len(order_times),
                        "budget_cutoff_ts": budget_cutoff_ts.isoformat() if budget_cutoff_ts is not None else None,
                    },
                )
        except Exception as exc:
            # Never kill the core loop: journal and continue.
            _send_journal(ctx, "core_error", {"reason": "core_loop_exception", "error": str(exc)})

    _send_journal(ctx, "core_stop", {})
