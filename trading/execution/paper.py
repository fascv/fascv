from __future__ import annotations

from trading.execution.base import ExecutionAdapter
from trading.execution.backtest import BacktestSimulator
from trading.types import Fill, MarketEvent, Order


class PaperExecutionAdapter(ExecutionAdapter):
    def __init__(self, simulator: BacktestSimulator):
        self.simulator = simulator

    def submit(self, orders: list[Order]) -> None:
        self.simulator.submit(orders)

    def process(self, event: MarketEvent, spread_bps: float) -> list[Fill]:
        return self.simulator.process(event, spread_bps)
