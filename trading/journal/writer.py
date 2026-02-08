from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict

from trading.types import (
    AlphaSignal,
    CostEstimate,
    Features,
    GateDecision,
    RiskDecision,
    AccountState,
    Order,
    Fill,
)


class JournalWriter:
    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def write(
        self,
        features: Features,
        alpha: AlphaSignal,
        cost: CostEstimate,
        gate: GateDecision,
        risk: RiskDecision,
        orders: list[Order],
        fills: list[Fill],
        state: AccountState,
    ) -> None:
        record: Dict[str, Any] = {
            "ts": features.ts.isoformat(),
            "features": features.values,
            "alpha": {"edge_bps": alpha.edge_bps, "p_up": alpha.p_up, "meta": alpha.meta},
            "cost": self._serialize(asdict(cost)),
            "gate": self._serialize(asdict(gate)),
            "risk": self._serialize(asdict(risk)),
            "orders": [self._serialize(asdict(o)) for o in orders],
            "fills": [self._serialize(asdict(f)) for f in fills],
            "state": self._serialize(asdict(state)),
        }
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()

    def _serialize(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: self._serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._serialize(v) for v in obj]
        return obj
