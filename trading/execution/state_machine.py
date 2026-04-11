from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


ALLOWED_TRANSITIONS = {
    "NEW": {"ACK", "REJECTED", "CANCELED"},
    "ACK": {"OPEN", "REJECTED", "CANCELED"},
    "OPEN": {"PARTIAL", "FILLED", "CANCELED"},
    "PARTIAL": {"PARTIAL", "FILLED", "CANCELED"},
    "FILLED": set(),
    "CANCELED": set(),
    "REJECTED": set(),
}


@dataclass
class OrderStatus:
    order_id: str
    state: str
    updated_at: datetime


class OrderStateMachine:
    def __init__(self) -> None:
        self._states: Dict[str, OrderStatus] = {}

    def state(self, order_id: str) -> Optional[OrderStatus]:
        return self._states.get(order_id)

    def transition(self, order_id: str, new_state: str, ts: datetime, allow_recovery: bool = False) -> bool:
        if new_state not in ALLOWED_TRANSITIONS:
            return False
        current = self._states.get(order_id)
        if current is None:
            if allow_recovery and new_state in {"ACK", "OPEN", "PARTIAL", "FILLED", "CANCELED", "REJECTED"}:
                self._states[order_id] = OrderStatus(order_id=order_id, state=new_state, updated_at=ts)
                return True
            current_state = "NEW"
        else:
            current_state = current.state
        if current_state == new_state:
            self._states[order_id] = OrderStatus(order_id=order_id, state=new_state, updated_at=ts)
            return True
        if new_state in ALLOWED_TRANSITIONS.get(current_state, set()):
            self._states[order_id] = OrderStatus(order_id=order_id, state=new_state, updated_at=ts)
            return True
        return False

    def open_orders_count(self) -> int:
        return sum(1 for s in self._states.values() if s.state in {"ACK", "OPEN", "PARTIAL"})

    def cancel_all(self, ts: datetime) -> Dict[str, str]:
        canceled: Dict[str, str] = {}
        for order_id, status in list(self._states.items()):
            if status.state in {"ACK", "OPEN", "PARTIAL", "NEW"}:
                self._states[order_id] = OrderStatus(order_id=order_id, state="CANCELED", updated_at=ts)
                canceled[order_id] = "CANCELED"
        return canceled
