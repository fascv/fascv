from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


ORDER_STATUS = {
    "NEW",
    "ACK",
    "OPEN",
    "PARTIAL",
    "FILLED",
    "CANCELED",
    "REJECTED",
}


@dataclass
class OrderIntent:
    ts: datetime
    side: str  # "buy" or "sell"
    qty_btc: float
    order_type: str  # "market" or "limit"
    limit_price: Optional[float] = None
    post_only: bool = False
    client_id: Optional[str] = None
    reason: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionReport:
    ts: datetime
    order_id: str
    status: str
    filled_qty_btc: float
    avg_price: float
    fee_eur: float
    latency_ms: float
    reason: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Heartbeat:
    ts: datetime
    process: str
    seq: int


@dataclass
class ControlCommand:
    ts: datetime
    action: str  # "START", "STOP", "PAUSE", "RESUME", "CANCEL_ALL", "RELOAD"
    reason: Optional[str] = None


@dataclass
class TelemetryEvent:
    ts: datetime
    process: str
    data: Dict[str, Any]


@dataclass
class JournalEvent:
    ts: datetime
    event_type: str
    payload: Dict[str, Any]
