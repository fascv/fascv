from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from queue import Empty
from typing import Any, Deque, Dict, List

from trading.alpha.momentum import MomentumAlpha, MomentumConfig
from trading.cost.model import CostModel, FeeConfig, SlippageConfig
from trading.execution.state import StateManager
from trading.features.engine import FeatureEngine
from trading.gate.gate import GateConfig, TradeabilityGate
from trading.ipc.events import ControlCommand, Heartbeat, JournalEvent, OrderIntent, TelemetryEvent
from trading.ipc.queues import try_put, queue_depth
from trading.order.builder import OrderBuilder, OrderConfig
from trading.processes.context import ProcessContext
from trading.risk.sizing import RiskConfig, RiskManager
from trading.types import Fill, MarketEvent


def _cfg(cfg: Dict[str, Any], path: str, default: Any) -> Any:
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _send_journal(ctx: ProcessContext, event_type: str, payload: Dict[str, Any]) -> None:
    evt = JournalEvent(ts=datetime.now(timezone.utc), event_type=event_type, payload=payload)
    try_put(ctx.q_journal, evt)


def _send_telemetry(ctx: ProcessContext, data: Dict[str, Any]) -> None:
    evt = TelemetryEvent(ts=datetime.now(timezone.utc), process="core", data=data)
    try_put(ctx.q_telemetry, evt)


def _send_heartbeat(ctx: ProcessContext, seq: int) -> None:
    hb = Heartbeat(ts=datetime.now(timezone.utc), process="core", seq=seq)
    try_put(ctx.q_heartbeat, hb)


def _build_components(cfg: Dict[str, Any]) -> tuple[FeatureEngine, MomentumAlpha, CostModel, TradeabilityGate, RiskManager, OrderBuilder, StateManager]:
    default_micro = _cfg(cfg, "data.default_micro", {})
    feature_engine = FeatureEngine(
        return_window=int(_cfg(cfg, "features.return_window", 1)),
        atr_window=int(_cfg(cfg, "features.atr_window", 14)),
        volume_z_window=int(_cfg(cfg, "features.volume_z_window", 30)),
        default_micro=default_micro,
    )

    alpha_model = MomentumAlpha(
        MomentumConfig(
            lookback=int(_cfg(cfg, "alpha.lookback", 3)),
            threshold_bps=float(_cfg(cfg, "alpha.threshold_bps", 2.0)),
            scale=float(_cfg(cfg, "alpha.scale", 1.0)),
        )
    )

    cost_model = CostModel(
        fee_config=FeeConfig(
            maker_bps=float(_cfg(cfg, "cost.maker_fee_bps", 2.0)),
            taker_bps=float(_cfg(cfg, "cost.taker_fee_bps", 4.0)),
        ),
        slippage_config=SlippageConfig(
            base_bps=float(_cfg(cfg, "cost.slippage_base_bps", 1.0)),
            vol_mult=float(_cfg(cfg, "cost.slippage_vol_mult", 0.05)),
        ),
        spread_component_factor=float(_cfg(cfg, "cost.spread_component_factor", 0.5)),
    )

    gate = TradeabilityGate(
        GateConfig(
            safety_margin_bps=float(_cfg(cfg, "gate.safety_margin_bps", 1.0)),
            max_spread_bps=float(_cfg(cfg, "gate.max_spread_bps", 15.0)),
            max_atr_bps=float(_cfg(cfg, "gate.max_atr_bps", 200.0)),
            session_start_utc=int(_cfg(cfg, "gate.session_start_utc", 0)),
            session_end_utc=int(_cfg(cfg, "gate.session_end_utc", 24)),
            stale_seconds=int(_cfg(cfg, "gate.stale_seconds", 3600)),
        )
    )

    risk = RiskManager(
        RiskConfig(
            max_exposure_eur=float(_cfg(cfg, "risk.max_exposure_eur", 50.0)),
            vol_target_bps=float(_cfg(cfg, "risk.vol_target_bps", 80.0)),
            daily_loss_limit_eur=float(_cfg(cfg, "risk.daily_loss_limit_eur", 10.0)),
            max_drawdown_pct=float(_cfg(cfg, "risk.max_drawdown_pct", 10.0)),
            cooldown_bars=int(_cfg(cfg, "risk.cooldown_bars", 5)),
            allow_short=bool(_cfg(cfg, "risk.allow_short", False)),
        )
    )

    order_builder = OrderBuilder(
        OrderConfig(
            order_type=str(_cfg(cfg, "order.type", "market")),
            post_only=bool(_cfg(cfg, "order.post_only", False)),
            limit_offset_bps=float(_cfg(cfg, "order.limit_offset_bps", 3.0)),
            min_trade_btc=float(_cfg(cfg, "order.min_trade_btc", 0.0001)),
            slice_count=int(_cfg(cfg, "order.slice_count", 1)),
        )
    )

    starting_cash = float(_cfg(cfg, "general.starting_cash_eur", 100.0))
    state_manager = StateManager(starting_cash_eur=starting_cash)

    return feature_engine, alpha_model, cost_model, gate, risk, order_builder, state_manager


def run_core(ctx: ProcessContext) -> None:
    cfg = ctx.config
    feature_engine, alpha_model, cost_model, gate, risk_manager, order_builder, state_manager = _build_components(cfg)

    trading_enabled = bool(_cfg(cfg, "core.trading_enabled", True))
    stale_seconds = float(_cfg(cfg, "core.stale_seconds", 10.0))
    max_orders_per_min = int(_cfg(cfg, "core.max_orders_per_min", 20))
    rate_limit_pause_sec = float(_cfg(cfg, "core.rate_limit_pause_sec", 2.0))
    heartbeat_interval = float(_cfg(cfg, "core.heartbeat_interval", 1.0))
    telemetry_interval = float(_cfg(cfg, "core.telemetry_interval", 1.0))

    last_market_arrival = time.time()
    last_heartbeat = 0.0
    last_telemetry = 0.0
    hb_seq = 0
    order_seq = 0
    order_times: Deque[float] = deque()
    resume_after: float | None = None

    _send_journal(ctx, "core_start", {"mode": ctx.mode})

    while not ctx.stop_event.is_set():
        # control commands
        try:
            cmd: ControlCommand = ctx.q_control_core.get_nowait()
            if cmd.action in {"STOP", "PAUSE"}:
                trading_enabled = False
                if cmd.reason == "rate_limit":
                    resume_after = time.time() + rate_limit_pause_sec
            elif cmd.action in {"START", "RESUME"}:
                trading_enabled = True
                resume_after = None
        except Empty:
            pass

        # execution reports / fills
        try:
            msg = ctx.q_exec_report.get_nowait()
            if isinstance(msg, Fill):
                state_manager.apply_fill(msg)
            elif hasattr(msg, "status"):
                # keep for telemetry or audit
                _send_journal(ctx, "exec_report", {
                    "ts": getattr(msg, "ts", datetime.now(timezone.utc)).isoformat(),
                    "order_id": getattr(msg, "order_id", ""),
                    "status": getattr(msg, "status", ""),
                    "latency_ms": getattr(msg, "latency_ms", 0.0),
                })
        except Empty:
            pass

        # market events
        event: MarketEvent | None = None
        try:
            event = ctx.q_market_core.get(timeout=0.5)
        except Empty:
            event = None

        if resume_after is not None and time.time() >= resume_after:
            trading_enabled = True
            resume_after = None

        if event is not None:
            last_market_arrival = time.time()
            features = feature_engine.compute(event)
            alpha = alpha_model.predict(features)
            cost = cost_model.estimate(features, order_builder.config.order_type)
            gate_decision = gate.evaluate(features, cost, alpha.edge_bps)

            pre_state = state_manager.snapshot(event.ts, event.close)
            risk = risk_manager.decide(pre_state, features, gate_decision, alpha.edge_bps)

            intents: List[OrderIntent] = []
            if trading_enabled and risk.allow:
                now = time.time()
                while order_times and now - order_times[0] > 60.0:
                    order_times.popleft()
                if len(order_times) < max_orders_per_min:
                    orders = order_builder.build(risk, pre_state.position_btc, event.close)
                    for order in orders:
                        order_seq += 1
                        intent = OrderIntent(
                            ts=event.ts,
                            side=order.side,
                            qty_btc=order.qty_btc,
                            order_type=order.order_type,
                            limit_price=order.price,
                            post_only=order.post_only,
                            client_id=f"intent_{event.ts.timestamp()}_{order_seq}",
                            reason=risk.reason,
                            meta={"edge_bps": alpha.edge_bps, "expected_cost_bps": cost.expected_cost_bps},
                        )
                        if try_put(ctx.q_order_intent, intent):
                            intents.append(intent)
                            order_times.append(now)
                else:
                    gate_decision = gate_decision  # rate limit hit; no order

            _send_journal(
                ctx,
                "core_decision",
                {
                    "ts": event.ts.isoformat(),
                    "features": features.values,
                    "alpha": {"edge_bps": alpha.edge_bps, "meta": alpha.meta},
                    "cost": {
                        "fee_bps": cost.fee_bps,
                        "spread_bps": cost.spread_bps,
                        "slippage_bps": cost.slippage_bps,
                        "expected_cost_bps": cost.expected_cost_bps,
                    },
                    "gate": {"allow": gate_decision.allow, "size_factor": gate_decision.size_factor, "reason": gate_decision.reason},
                    "risk": {"allow": risk.allow, "target_btc": risk.target_position_btc, "reason": risk.reason},
                    "intents": [intent.client_id for intent in intents],
                    "equity": pre_state.equity_eur,
                },
            )

        # stale detection
        if stale_seconds > 0 and (time.time() - last_market_arrival) > stale_seconds:
            if trading_enabled:
                trading_enabled = False
                _send_journal(ctx, "core_stale", {"last_market_arrival": last_market_arrival})
                for q in (ctx.q_control_core, ctx.q_control_exec):
                    try_put(
                        q,
                        ControlCommand(ts=datetime.now(timezone.utc), action="STOP", reason="stale_market_data"),
                    )
                    try_put(
                        q,
                        ControlCommand(ts=datetime.now(timezone.utc), action="CANCEL_ALL", reason="stale_market_data"),
                    )

        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            hb_seq += 1
            _send_heartbeat(ctx, hb_seq)
            last_heartbeat = now
        if now - last_telemetry >= telemetry_interval:
            last_telemetry = now
            _send_telemetry(
                ctx,
                {
                    "mode": ctx.mode,
                    "trading_enabled": trading_enabled,
                    "last_market_event_age": now - last_market_arrival,
                    "queue_market_core": queue_depth(ctx.q_market_core),
                    "queue_order_intent": queue_depth(ctx.q_order_intent),
                },
            )

    _send_journal(ctx, "core_stop", {})
