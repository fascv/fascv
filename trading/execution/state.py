from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading.types import AccountState, Fill


@dataclass
class PositionState:
    cash_eur: float
    position_btc: float
    avg_entry_price: float
    realized_pnl_eur: float
    peak_equity_eur: float
    day_start_equity_eur: float


class StateManager:
    def __init__(self, starting_cash_eur: float):
        self.position = PositionState(
            cash_eur=starting_cash_eur,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            peak_equity_eur=starting_cash_eur,
            day_start_equity_eur=starting_cash_eur,
        )
        self._last_day = None

    def apply_fill(self, fill: Fill) -> None:
        pos = self.position
        notional = fill.qty_btc * fill.price
        if fill.side == "buy":
            pos.cash_eur -= notional + fill.fee_eur
            new_pos = pos.position_btc + fill.qty_btc
            if new_pos > 0:
                pos.avg_entry_price = (
                    (pos.avg_entry_price * pos.position_btc + fill.price * fill.qty_btc) / new_pos
                )
            pos.position_btc = new_pos
        else:
            pos.cash_eur += notional - fill.fee_eur
            realized = 0.0
            if pos.position_btc > 0:
                realized = (fill.price - pos.avg_entry_price) * fill.qty_btc
            pos.realized_pnl_eur += realized
            pos.position_btc -= fill.qty_btc
            if pos.position_btc <= 0:
                pos.avg_entry_price = 0.0

    def snapshot(self, ts: datetime, mark_price: float) -> AccountState:
        pos = self.position
        equity = pos.cash_eur + pos.position_btc * mark_price
        pos.peak_equity_eur = max(pos.peak_equity_eur, equity)
        drawdown_pct = 0.0
        if pos.peak_equity_eur > 0:
            drawdown_pct = (pos.peak_equity_eur - equity) / pos.peak_equity_eur * 100.0
        day = ts.date()
        if self._last_day != day:
            pos.day_start_equity_eur = equity
            self._last_day = day
        return AccountState(
            ts=ts,
            cash_eur=pos.cash_eur,
            position_btc=pos.position_btc,
            avg_entry_price=pos.avg_entry_price,
            realized_pnl_eur=pos.realized_pnl_eur,
            equity_eur=equity,
            peak_equity_eur=pos.peak_equity_eur,
            drawdown_pct=drawdown_pct,
            day_start_equity_eur=pos.day_start_equity_eur,
        )
