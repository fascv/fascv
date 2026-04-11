from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trading.types import Fill


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if out != out:
        return float(default)
    return out


def _safe_iso(ts: Any) -> str | None:
    if isinstance(ts, datetime):
        return ts.isoformat()
    return None


@dataclass
class _ExitCampaign:
    symbol: str
    pair: str
    entry_ts: datetime
    entry_source: str
    entry_price: float
    avg_entry_price: float
    entry_qty: float
    max_qty: float
    realized_pnl_at_entry_eur: float
    peak_ts: datetime
    peak_price: float
    peak_pnl_eur: float
    peak_pnl_bps: float
    min_pnl_eur: float
    min_pnl_bps: float
    mark_count: int = 0
    buy_fill_count: int = 0
    sell_fill_count: int = 0
    pending_exit_reason: str | None = None
    pending_exit_ts: datetime | None = None
    pending_exit_price: float = 0.0
    pending_exit_target_qty: float = 0.0
    pending_exit_expected_cost_bps: float = 0.0
    alpha_type: str | None = None
    active_strategy: str | None = None


class ExitLearningObserver:
    """Records realized exit-learning observations without changing trading decisions."""

    version = 1

    def __init__(self, *, symbol: str = "", pair: str = "", position_epsilon: float = 1e-12):
        self.symbol = str(symbol or "").strip().upper()
        self.pair = str(pair or "").strip().upper()
        self.position_epsilon = max(1e-12, float(position_epsilon or 1e-12))
        self._campaign: _ExitCampaign | None = None

    def reset(self) -> None:
        self._campaign = None

    def seed_open_position(
        self,
        *,
        ts: datetime,
        position: Any,
        mark_price: float,
        source: str = "sync_account",
    ) -> None:
        if self._position_qty(position) <= self.position_epsilon:
            self.reset()
            return
        self._ensure_campaign(ts=ts, position=position, price=mark_price, source=source)

    def on_mark(
        self,
        *,
        ts: datetime,
        position: Any,
        price: float,
        expected_cost_bps: float = 0.0,
        alpha_type: str | None = None,
        active_strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        qty = self._position_qty(position)
        price = max(0.0, float(price or 0.0))
        if qty <= self.position_epsilon:
            if self._campaign is None:
                return []
            return [
                self._finalize(
                    ts=ts,
                    exit_price=price,
                    realized_pnl_after_eur=self._realized_pnl(position),
                    exit_source="position_flat_detected",
                )
            ]
        campaign = self._ensure_campaign(ts=ts, position=position, price=price, source="mark_detected")
        campaign.pending_exit_expected_cost_bps = max(
            0.0,
            float(expected_cost_bps or campaign.pending_exit_expected_cost_bps or 0.0),
        )
        campaign.alpha_type = str(alpha_type).strip().lower() if alpha_type else campaign.alpha_type
        campaign.active_strategy = (
            str(active_strategy).strip().lower() if active_strategy else campaign.active_strategy
        )
        self._update_peak_and_floor(ts=ts, position=position, price=price)
        return []

    def on_decision(
        self,
        *,
        ts: datetime,
        position: Any,
        price: float,
        reason: str | None,
        target_qty: float,
        expected_cost_bps: float,
        alpha_type: str | None = None,
        active_strategy: str | None = None,
    ) -> None:
        campaign = self._campaign
        if campaign is None:
            return
        current_qty = self._position_qty(position)
        if current_qty <= self.position_epsilon:
            return
        campaign.alpha_type = str(alpha_type).strip().lower() if alpha_type else campaign.alpha_type
        campaign.active_strategy = (
            str(active_strategy).strip().lower() if active_strategy else campaign.active_strategy
        )
        campaign.pending_exit_expected_cost_bps = max(0.0, float(expected_cost_bps or 0.0))
        target_qty = max(0.0, float(target_qty or 0.0))
        if target_qty + self.position_epsilon < current_qty:
            campaign.pending_exit_reason = str(reason or "reduce_position").strip() or "reduce_position"
            campaign.pending_exit_ts = ts
            campaign.pending_exit_price = max(0.0, float(price or 0.0))
            campaign.pending_exit_target_qty = target_qty

    def on_fill(self, *, fill: Fill, before_position: Any, after_position: Any) -> list[dict[str, Any]]:
        side = str(getattr(fill, "side", "") or "").strip().lower()
        fill_ts = getattr(fill, "ts", None)
        ts = fill_ts if isinstance(fill_ts, datetime) else datetime.now(timezone.utc)
        fill_price = max(0.0, _safe_float(getattr(fill, "price", 0.0), 0.0))
        before_qty = self._position_qty(before_position)
        after_qty = self._position_qty(after_position)
        if side == "buy":
            if before_qty <= self.position_epsilon and after_qty > self.position_epsilon:
                self.reset()
                campaign = self._ensure_campaign(
                    ts=ts,
                    position=after_position,
                    price=fill_price,
                    source="buy_fill",
                )
                campaign.buy_fill_count = 1
                return []
            if self._campaign is not None:
                self._campaign.buy_fill_count += 1
                self._campaign.max_qty = max(self._campaign.max_qty, after_qty)
                self._update_peak_and_floor(ts=ts, position=after_position, price=fill_price)
            return []
        if side != "sell":
            return []
        if self._campaign is None and before_qty > self.position_epsilon:
            self._ensure_campaign(ts=ts, position=before_position, price=fill_price, source="sell_fill_detected")
        if self._campaign is None:
            return []
        self._campaign.sell_fill_count += 1
        if before_qty > self.position_epsilon:
            self._update_peak_and_floor(ts=ts, position=before_position, price=fill_price)
        if after_qty <= self.position_epsilon:
            return [
                self._finalize(
                    ts=ts,
                    exit_price=fill_price,
                    realized_pnl_after_eur=self._realized_pnl(after_position),
                    exit_source="sell_fill",
                    fill=fill,
                )
            ]
        self._update_peak_and_floor(ts=ts, position=after_position, price=fill_price)
        return []

    def snapshot(self) -> dict[str, Any] | None:
        campaign = self._campaign
        if campaign is None:
            return None
        return {
            "version": self.version,
            "symbol": campaign.symbol,
            "pair": campaign.pair,
            "entry_ts": campaign.entry_ts.isoformat(),
            "entry_source": campaign.entry_source,
            "entry_price": campaign.entry_price,
            "avg_entry_price": campaign.avg_entry_price,
            "entry_qty": campaign.entry_qty,
            "max_qty": campaign.max_qty,
            "peak_ts": campaign.peak_ts.isoformat(),
            "peak_price": campaign.peak_price,
            "peak_pnl_eur": campaign.peak_pnl_eur,
            "peak_pnl_bps": campaign.peak_pnl_bps,
            "min_pnl_eur": campaign.min_pnl_eur,
            "min_pnl_bps": campaign.min_pnl_bps,
            "mark_count": campaign.mark_count,
            "pending_exit_reason": campaign.pending_exit_reason,
        }

    def _ensure_campaign(
        self,
        *,
        ts: datetime,
        position: Any,
        price: float,
        source: str,
    ) -> _ExitCampaign:
        if self._campaign is not None:
            return self._campaign
        qty = self._position_qty(position)
        avg_entry = self._avg_entry(position)
        if avg_entry <= 0.0:
            avg_entry = max(0.0, float(price or 0.0))
        pnl_eur, pnl_bps = self._pnl(position=position, price=price)
        campaign = _ExitCampaign(
            symbol=self.symbol,
            pair=self.pair,
            entry_ts=ts,
            entry_source=source,
            entry_price=avg_entry,
            avg_entry_price=avg_entry,
            entry_qty=qty,
            max_qty=qty,
            realized_pnl_at_entry_eur=self._realized_pnl(position),
            peak_ts=ts,
            peak_price=max(0.0, float(price or 0.0)),
            peak_pnl_eur=pnl_eur,
            peak_pnl_bps=pnl_bps,
            min_pnl_eur=pnl_eur,
            min_pnl_bps=pnl_bps,
        )
        self._campaign = campaign
        return campaign

    def _update_peak_and_floor(self, *, ts: datetime, position: Any, price: float) -> None:
        campaign = self._campaign
        if campaign is None:
            return
        qty = self._position_qty(position)
        if qty <= self.position_epsilon:
            return
        campaign.mark_count += 1
        campaign.max_qty = max(campaign.max_qty, qty)
        avg_entry = self._avg_entry(position)
        if avg_entry > 0.0:
            campaign.avg_entry_price = avg_entry
        pnl_eur, pnl_bps = self._pnl(position=position, price=price)
        if pnl_eur > campaign.peak_pnl_eur:
            campaign.peak_pnl_eur = pnl_eur
            campaign.peak_pnl_bps = pnl_bps
            campaign.peak_price = max(0.0, float(price or 0.0))
            campaign.peak_ts = ts
        if pnl_eur < campaign.min_pnl_eur:
            campaign.min_pnl_eur = pnl_eur
            campaign.min_pnl_bps = pnl_bps

    def _finalize(
        self,
        *,
        ts: datetime,
        exit_price: float,
        realized_pnl_after_eur: float,
        exit_source: str,
        fill: Fill | None = None,
    ) -> dict[str, Any]:
        campaign = self._campaign
        if campaign is None:
            return {}
        exit_price = max(0.0, float(exit_price or 0.0))
        avg_entry = campaign.avg_entry_price
        exit_pnl_bps = ((exit_price / avg_entry) - 1.0) * 10000.0 if avg_entry > 0.0 else 0.0
        exit_open_pnl_eur = ((exit_price - avg_entry) * campaign.max_qty) if avg_entry > 0.0 else 0.0
        retrace_eur = max(0.0, campaign.peak_pnl_eur - exit_open_pnl_eur)
        retrace_bps = max(0.0, campaign.peak_pnl_bps - exit_pnl_bps)
        hold_seconds = max(0.0, (ts - campaign.entry_ts).total_seconds())
        realized_delta = realized_pnl_after_eur - campaign.realized_pnl_at_entry_eur
        payload = {
            "version": self.version,
            "symbol": campaign.symbol,
            "pair": campaign.pair,
            "entry_ts": campaign.entry_ts.isoformat(),
            "entry_source": campaign.entry_source,
            "exit_ts": ts.isoformat(),
            "exit_source": exit_source,
            "hold_seconds": hold_seconds,
            "entry_price": campaign.entry_price,
            "avg_entry_price": avg_entry,
            "exit_price": exit_price,
            "entry_qty": campaign.entry_qty,
            "max_qty": campaign.max_qty,
            "peak_ts": campaign.peak_ts.isoformat(),
            "peak_price": campaign.peak_price,
            "peak_pnl_eur": campaign.peak_pnl_eur,
            "peak_pnl_bps": campaign.peak_pnl_bps,
            "min_pnl_eur": campaign.min_pnl_eur,
            "min_pnl_bps": campaign.min_pnl_bps,
            "exit_open_pnl_eur": exit_open_pnl_eur,
            "exit_pnl_bps": exit_pnl_bps,
            "retrace_from_peak_eur": retrace_eur,
            "retrace_from_peak_bps": retrace_bps,
            "retrace_from_peak_pct": (retrace_eur / campaign.peak_pnl_eur * 100.0)
            if campaign.peak_pnl_eur > 0.0
            else 0.0,
            "realized_pnl_delta_eur": realized_delta,
            "mark_count": campaign.mark_count,
            "buy_fill_count": campaign.buy_fill_count,
            "sell_fill_count": campaign.sell_fill_count,
            "exit_reason": campaign.pending_exit_reason,
            "exit_decision_ts": _safe_iso(campaign.pending_exit_ts),
            "exit_decision_price": campaign.pending_exit_price,
            "exit_decision_target_qty": campaign.pending_exit_target_qty,
            "expected_cost_bps": campaign.pending_exit_expected_cost_bps,
            "alpha_type": campaign.alpha_type,
            "active_strategy": campaign.active_strategy,
        }
        if fill is not None:
            payload["exit_order_id"] = getattr(fill, "order_id", None)
            payload["exit_fee_eur"] = _safe_float(getattr(fill, "fee_eur", 0.0), 0.0)
            payload["exit_qty"] = _safe_float(getattr(fill, "qty_btc", 0.0), 0.0)
        self.reset()
        return payload

    def _position_qty(self, position: Any) -> float:
        return max(0.0, _safe_float(getattr(position, "position_btc", 0.0), 0.0))

    def _avg_entry(self, position: Any) -> float:
        return max(0.0, _safe_float(getattr(position, "avg_entry_price", 0.0), 0.0))

    def _realized_pnl(self, position: Any) -> float:
        return _safe_float(getattr(position, "realized_pnl_eur", 0.0), 0.0)

    def _pnl(self, *, position: Any, price: float) -> tuple[float, float]:
        qty = self._position_qty(position)
        avg_entry = self._avg_entry(position)
        if qty <= self.position_epsilon or avg_entry <= 0.0 or price <= 0.0:
            return 0.0, 0.0
        pnl_eur = (float(price) - avg_entry) * qty
        pnl_bps = ((float(price) / avg_entry) - 1.0) * 10000.0
        return pnl_eur, pnl_bps
