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
    # EUR cost basis of the current position (includes buy fees). Used for fee-accurate PnL.
    cost_basis_eur: float
    peak_equity_eur: float
    day_start_equity_eur: float


class StateManager:
    def __init__(self, starting_cash_eur: float):
        self.position = PositionState(
            cash_eur=starting_cash_eur,
            position_btc=0.0,
            avg_entry_price=0.0,
            realized_pnl_eur=0.0,
            cost_basis_eur=0.0,
            peak_equity_eur=starting_cash_eur,
            day_start_equity_eur=starting_cash_eur,
        )
        self._last_day = None

    def apply_fill(self, fill: Fill) -> None:
        pos = self.position
        notional = fill.qty_btc * fill.price
        if fill.side == "buy":
            spend = notional + float(fill.fee_eur or 0.0)
            pos.cash_eur -= spend
            pos.cost_basis_eur += spend
            pos.position_btc += fill.qty_btc
            if pos.position_btc > 0:
                pos.avg_entry_price = pos.cost_basis_eur / pos.position_btc
        else:
            proceeds = notional - float(fill.fee_eur or 0.0)

            realized = 0.0
            if pos.position_btc > 0 and fill.qty_btc > 0:
                pos_before = pos.position_btc
                sell_qty = min(float(fill.qty_btc), float(pos_before))
                proceeds_sold = proceeds * (sell_qty / float(fill.qty_btc))
                pos.cash_eur += proceeds_sold
                # Pro-rate cost basis by sold fraction.
                basis_sold = 0.0
                if pos_before > 0 and pos.cost_basis_eur > 0:
                    basis_sold = pos.cost_basis_eur * (sell_qty / pos_before)
                realized = proceeds_sold - basis_sold
                pos.cost_basis_eur = max(0.0, pos.cost_basis_eur - basis_sold)
                pos.position_btc = pos_before - sell_qty
                if pos.position_btc <= 0:
                    pos.position_btc = 0.0
                    pos.avg_entry_price = 0.0
                    pos.cost_basis_eur = 0.0
                else:
                    pos.avg_entry_price = pos.cost_basis_eur / pos.position_btc
            else:
                # Fallback: if we don't have a long position tracked, just update position.
                pos.cash_eur += proceeds
                pos.position_btc -= fill.qty_btc
                if pos.position_btc <= 0:
                    pos.position_btc = 0.0
                    pos.avg_entry_price = 0.0
                    pos.cost_basis_eur = 0.0
            pos.realized_pnl_eur += realized

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
