from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from trading.types import AccountState, Features, GateDecision, RiskDecision


@dataclass
class RiskConfig:
    max_exposure_eur: float
    vol_target_bps: float
    daily_loss_limit_eur: float
    max_drawdown_pct: float
    cooldown_bars: int
    allow_short: bool = False


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._cooldown_remaining = 0
        self._current_day: Optional[date] = None
        self._day_start_equity: Optional[float] = None
        self._last_realized_pnl: Optional[float] = None

    def _update_day(self, state: AccountState) -> None:
        day = state.ts.date()
        if self._current_day != day:
            self._current_day = day
            self._day_start_equity = state.day_start_equity_eur

    def update_kill_switches(self, state: AccountState) -> Optional[str]:
        self._update_day(state)
        day_start = self._day_start_equity if self._day_start_equity is not None else state.equity_eur
        day_pnl = state.equity_eur - day_start
        if day_pnl <= -self.config.daily_loss_limit_eur:
            self._cooldown_remaining = max(self._cooldown_remaining, self.config.cooldown_bars)
            return "daily_loss_limit"
        if state.drawdown_pct >= self.config.max_drawdown_pct:
            self._cooldown_remaining = max(self._cooldown_remaining, self.config.cooldown_bars)
            return "max_drawdown"
        if self._last_realized_pnl is None:
            self._last_realized_pnl = state.realized_pnl_eur
        elif state.realized_pnl_eur < self._last_realized_pnl:
            self._cooldown_remaining = max(self._cooldown_remaining, self.config.cooldown_bars)
            self._last_realized_pnl = state.realized_pnl_eur
            return "cooldown_loss"
        self._last_realized_pnl = state.realized_pnl_eur
        return None

    def decide(
        self,
        state: AccountState,
        features: Features,
        gate: GateDecision,
        predicted_edge_bps: float,
    ) -> RiskDecision:
        reason = self.update_kill_switches(state)
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return RiskDecision(
                ts=features.ts,
                allow=False,
                target_position_btc=state.position_btc,
                reason=reason or "cooldown",
                cooldown_remaining=self._cooldown_remaining,
            )
        if not gate.allow:
            return RiskDecision(
                ts=features.ts,
                allow=False,
                target_position_btc=state.position_btc,
                reason="gate_block",
                cooldown_remaining=0,
            )
        if predicted_edge_bps <= 0.0 and not self.config.allow_short:
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="no_long_edge",
                cooldown_remaining=0,
            )

        atr_bps = float(features.values.get("atr_bps", 0.0))
        vol_scale = 1.0
        if atr_bps > 0:
            vol_scale = min(1.0, self.config.vol_target_bps / atr_bps)
        target_eur = self.config.max_exposure_eur * gate.size_factor * vol_scale
        price = float(features.values.get("price", 0.0))
        if price <= 0:
            return RiskDecision(
                ts=features.ts,
                allow=False,
                target_position_btc=state.position_btc,
                reason="invalid_price",
                cooldown_remaining=0,
            )
        target_btc = target_eur / price
        if predicted_edge_bps < 0.0 and self.config.allow_short:
            target_btc = -target_btc
        return RiskDecision(
            ts=features.ts,
            allow=True,
            target_position_btc=target_btc,
            reason=None,
            cooldown_remaining=0,
        )
