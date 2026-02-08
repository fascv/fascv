from __future__ import annotations

from typing import Any, Dict, List, Optional

from trading.alpha.base import AlphaModel
from trading.cost.model import CostModel
from trading.data.base import MarketDataSource
from trading.execution.base import ExecutionAdapter
from trading.execution.state import StateManager
from trading.features.engine import FeatureEngine
from trading.gate.gate import TradeabilityGate
from trading.journal.writer import JournalWriter
from trading.order.builder import OrderBuilder
from trading.reporting.report import Report, build_report
from trading.risk.sizing import RiskManager
from trading.types import AccountState, BacktestResult, Fill, MarketEvent


class TradingPipeline:
    def __init__(
        self,
        data_source: MarketDataSource,
        feature_engine: FeatureEngine,
        alpha_model: AlphaModel,
        cost_model: CostModel,
        gate: TradeabilityGate,
        risk_manager: RiskManager,
        order_builder: OrderBuilder,
        execution: ExecutionAdapter,
        state_manager: StateManager,
        journal: Optional[JournalWriter] = None,
    ):
        self.data_source = data_source
        self.feature_engine = feature_engine
        self.alpha_model = alpha_model
        self.cost_model = cost_model
        self.gate = gate
        self.risk_manager = risk_manager
        self.order_builder = order_builder
        self.execution = execution
        self.state_manager = state_manager
        self.journal = journal

        self.trades: List[Dict[str, Any]] = []
        self.equity_curve: List[Dict[str, Any]] = []
        self.expected_cost_bps: List[float] = []
        self.realized_cost_bps: List[float] = []
        self.turnover_eur: float = 0.0

        self._open_trade: Optional[Dict[str, Any]] = None

    def _record_trade_if_closed(self, state: AccountState, price: float) -> None:
        if self._open_trade and state.position_btc == 0.0:
            entry = self._open_trade
            pnl = state.realized_pnl_eur - entry["start_realized_pnl"]
            trade = {
                "entry_ts": entry["ts"],
                "exit_ts": state.ts.isoformat(),
                "entry_price": entry["price"],
                "exit_price": price,
                "pnl_eur": pnl,
            }
            self.trades.append(trade)
            self._open_trade = None
        if self._open_trade is None and state.position_btc > 0.0:
            self._open_trade = {
                "ts": state.ts.isoformat(),
                "price": price,
                "start_realized_pnl": state.realized_pnl_eur,
            }

    def _calc_realized_cost_bps(self, fills: List[Fill]) -> float:
        if not fills:
            return 0.0
        total_cost = 0.0
        total_notional = 0.0
        for fill in fills:
            notional = fill.qty_btc * fill.price
            total_notional += notional
            total_cost += fill.fee_eur
            if fill.slippage_bps:
                total_cost += notional * fill.slippage_bps / 10000.0
        if total_notional == 0:
            return 0.0
        return total_cost / total_notional * 10000.0

    def run_backtest(self) -> BacktestResult:
        prev_equity = None
        last_state: AccountState | None = None
        last_price: float | None = None
        for event in self.data_source:
            features = self.feature_engine.compute(event)
            alpha = self.alpha_model.predict(features)
            cost = self.cost_model.estimate(features, self.order_builder.config.order_type)
            gate = self.gate.evaluate(features, cost, alpha.edge_bps)

            pre_state = self.state_manager.snapshot(event.ts, event.close)
            risk = self.risk_manager.decide(pre_state, features, gate, alpha.edge_bps)

            orders = []
            if risk.allow:
                orders = self.order_builder.build(risk, pre_state.position_btc, event.close)
            self.execution.submit(orders)
            fills = self.execution.process(event, spread_bps=features.values.get("spread_bps", 0.0))
            for fill in fills:
                self.turnover_eur += abs(fill.qty_btc * fill.price)
                self.state_manager.apply_fill(fill)

            state = self.state_manager.snapshot(event.ts, event.close)
            last_state = state
            last_price = event.close
            self._record_trade_if_closed(state, event.close)

            ret = None
            if prev_equity is not None and prev_equity > 0:
                ret = (state.equity_eur / prev_equity) - 1.0
            self.equity_curve.append(
                {
                    "ts": event.ts.isoformat(),
                    "equity": state.equity_eur,
                    "drawdown_pct": state.drawdown_pct,
                    "ret": ret,
                }
            )
            prev_equity = state.equity_eur

            self.expected_cost_bps.append(cost.expected_cost_bps)
            self.realized_cost_bps.append(self._calc_realized_cost_bps(fills))

            if self.journal:
                self.journal.write(features, alpha, cost, gate, risk, orders, fills, state)

        if self._open_trade and last_state is not None and last_price is not None:
            entry = self._open_trade
            unrealized = (last_price - entry["price"]) * last_state.position_btc
            trade = {
                "entry_ts": entry["ts"],
                "exit_ts": last_state.ts.isoformat(),
                "entry_price": entry["price"],
                "exit_price": last_price,
                "pnl_eur": unrealized,
                "note": "open_position_marked_to_market",
            }
            self.trades.append(trade)
            self._open_trade = None

        report = build_report(
            equity_curve=self.equity_curve,
            trades=self.trades,
            expected_cost_bps=self.expected_cost_bps,
            realized_cost_bps=self.realized_cost_bps,
            turnover_eur=self.turnover_eur,
        )
        return BacktestResult(metrics=report.metrics, trades=report.trades, equity_curve=report.equity_curve)
