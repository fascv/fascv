from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class MarketEvent:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    micro: Dict[str, float] = field(default_factory=dict)


@dataclass
class Features:
    ts: datetime
    values: Dict[str, float]


@dataclass
class AlphaSignal:
    ts: datetime
    edge_bps: float
    p_up: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CostEstimate:
    ts: datetime
    fee_bps: float
    spread_bps: float
    slippage_bps: float
    expected_cost_bps: float


@dataclass
class GateDecision:
    ts: datetime
    allow: bool
    size_factor: float
    reason: Optional[str] = None


@dataclass
class RiskDecision:
    ts: datetime
    allow: bool
    target_position_btc: float
    reason: Optional[str] = None
    cooldown_remaining: int = 0


@dataclass
class Order:
    ts: datetime
    side: str  # "buy" or "sell"
    qty_btc: float
    order_type: str  # "market" or "limit"
    price: Optional[float] = None
    post_only: bool = False
    id: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    ts: datetime
    side: str
    qty_btc: float
    price: float
    fee_eur: float
    order_id: Optional[str] = None
    slippage_bps: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountState:
    ts: datetime
    cash_eur: float
    position_btc: float
    avg_entry_price: float
    realized_pnl_eur: float
    equity_eur: float
    peak_equity_eur: float
    drawdown_pct: float
    day_start_equity_eur: float


@dataclass
class BacktestResult:
    metrics: Dict[str, Any]
    trades: List[Dict[str, Any]]
    equity_curve: List[Dict[str, Any]]
