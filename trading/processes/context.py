from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import Event
from multiprocessing.queues import Queue
from typing import Any, Dict


@dataclass
class ProcessContext:
    mode: str
    config: Dict[str, Any]
    stop_event: Event
    q_market_core: Queue
    q_market_exec: Queue
    q_order_intent: Queue
    q_exec_report: Queue
    q_journal: Queue
    q_control_core: Queue
    q_control_exec: Queue
    q_telemetry: Queue
    q_heartbeat: Queue
