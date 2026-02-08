from __future__ import annotations

from typing import Any, Dict, Optional

from trading.alpha.momentum import MomentumAlpha, MomentumConfig
from trading.cost.model import CostModel, FeeConfig, SlippageConfig
from trading.data.base import MarketDataSource
from trading.execution.base import ExecutionAdapter
from trading.execution.state import StateManager
from trading.features.engine import FeatureEngine
from trading.gate.gate import GateConfig, TradeabilityGate
from trading.journal.writer import JournalWriter
from trading.order.builder import OrderBuilder, OrderConfig
from trading.pipeline import TradingPipeline
from trading.risk.sizing import RiskConfig, RiskManager


def _cfg(d: Dict[str, Any], path: str, default: Any) -> Any:
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def build_pipeline(
    cfg: Dict[str, Any],
    data_source: MarketDataSource,
    execution: ExecutionAdapter,
    journal_path: Optional[str] = None,
) -> TradingPipeline:
    default_micro = _cfg(cfg, "data.default_micro", {})

    feature_engine = FeatureEngine(
        return_window=int(_cfg(cfg, "features.return_window", 1)),
        atr_window=int(_cfg(cfg, "features.atr_window", 14)),
        volume_z_window=int(_cfg(cfg, "features.volume_z_window", 30)),
        default_micro=default_micro,
    )

    alpha_type = _cfg(cfg, "alpha.type", "momentum")
    if alpha_type != "momentum":
        raise ValueError(f"Unsupported alpha type: {alpha_type}")
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

    risk_manager = RiskManager(
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

    journal = JournalWriter(journal_path) if journal_path else None

    return TradingPipeline(
        data_source=data_source,
        feature_engine=feature_engine,
        alpha_model=alpha_model,
        cost_model=cost_model,
        gate=gate,
        risk_manager=risk_manager,
        order_builder=order_builder,
        execution=execution,
        state_manager=state_manager,
        journal=journal,
    )
