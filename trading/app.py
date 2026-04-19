from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from trading.alpha.factory import build_alpha_model
from trading.config_overlay import apply_yaml_overlay
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
    cfg = copy.deepcopy(cfg)
    alpha_override_path = _cfg(cfg, "alpha.override_path", "")
    if alpha_override_path:
        cfg = apply_yaml_overlay(cfg, str(alpha_override_path))
    default_micro = _cfg(cfg, "data.default_micro", {})
    min_trade_btc = float(_cfg(cfg, "order.min_trade_btc", 0.0001))
    position_epsilon_btc = max(1e-12, min_trade_btc)

    feature_engine = FeatureEngine(
        return_window=int(_cfg(cfg, "features.return_window", 1)),
        atr_window=int(_cfg(cfg, "features.atr_window", 14)),
        volume_z_window=int(_cfg(cfg, "features.volume_z_window", 30)),
        trend_window=int(_cfg(cfg, "features.trend_window", 0)),
        context_window=int(_cfg(cfg, "features.context_window", 0)),
        default_micro=default_micro,
    )

    alpha_model = build_alpha_model(cfg)

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
            cost_coverage_ratio=float(_cfg(cfg, "gate.cost_coverage_ratio", 1.0)),
            cost_roundtrip_multiplier=float(_cfg(cfg, "gate.cost_roundtrip_multiplier", 1.0)),
            max_spread_bps=float(_cfg(cfg, "gate.max_spread_bps", 15.0)),
            min_atr_bps=float(_cfg(cfg, "gate.min_atr_bps", 0.0)),
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
            use_vol_scaling=bool(_cfg(cfg, "risk.use_vol_scaling", True)),
            use_gate_size_factor=bool(_cfg(cfg, "risk.use_gate_size_factor", True)),
            entry_edge_bps=float(_cfg(cfg, "risk.entry_edge_bps", 0.0)),
            entry_cost_buffer_bps=float(_cfg(cfg, "risk.entry_cost_buffer_bps", 0.0)),
            entry_cost_coverage_ratio=float(_cfg(cfg, "risk.entry_cost_coverage_ratio", 1.0)),
            entry_cost_roundtrip_multiplier=float(
                _cfg(cfg, "risk.entry_cost_roundtrip_multiplier", 1.0)
            ),
            entry_min_atr_to_cost_ratio=float(_cfg(cfg, "risk.entry_min_atr_to_cost_ratio", 0.0)),
            disable_entry_edge_gate=bool(_cfg(cfg, "risk.disable_entry_edge_gate", False)),
            override_max_structure_range_pos=float(
                _cfg(cfg, "risk.override_max_structure_range_pos", 1.0)
            ),
            override_min_drawdown_from_peak_bps=float(
                _cfg(cfg, "risk.override_min_drawdown_from_peak_bps", 0.0)
            ),
            override_min_drawdown_to_cost_ratio=float(
                _cfg(cfg, "risk.override_min_drawdown_to_cost_ratio", 0.0)
            ),
            override_min_slope_short_bps=float(
                _cfg(cfg, "risk.override_min_slope_short_bps", -999.0)
            ),
            override_max_trend_return_bps=float(
                _cfg(cfg, "risk.override_max_trend_return_bps", 0.0)
            ),
            override_max_context_range_pos=float(
                _cfg(cfg, "risk.override_max_context_range_pos", 1.0)
            ),
            late_entry_block_context_range_pos=float(
                _cfg(cfg, "risk.late_entry_block_context_range_pos", 1.0)
            ),
            late_entry_block_structure_range_pos=float(
                _cfg(cfg, "risk.late_entry_block_structure_range_pos", 1.0)
            ),
            late_entry_block_max_context_drawdown_bps=float(
                _cfg(cfg, "risk.late_entry_block_max_context_drawdown_bps", 0.0)
            ),
            late_entry_block_min_trend_return_bps=float(
                _cfg(cfg, "risk.late_entry_block_min_trend_return_bps", 0.0)
            ),
            late_entry_block_min_return_bps=float(
                _cfg(cfg, "risk.late_entry_block_min_return_bps", 0.0)
            ),
            exit_edge_bps=float(_cfg(cfg, "risk.exit_edge_bps", 0.0)),
            exit_bypass_gate_edge_bps=float(_cfg(cfg, "risk.exit_bypass_gate_edge_bps", 0.0)),
            profit_only_auto_exits=bool(_cfg(cfg, "risk.profit_only_auto_exits", False)),
            min_hold_bars=int(_cfg(cfg, "risk.min_hold_bars", 0)),
            failed_start_exit_enabled=bool(_cfg(cfg, "risk.failed_start_exit_enabled", False)),
            failed_start_min_bars=int(_cfg(cfg, "risk.failed_start_min_bars", 0)),
            failed_start_max_bars=int(_cfg(cfg, "risk.failed_start_max_bars", 0)),
            failed_start_min_rebound_bps=float(_cfg(cfg, "risk.failed_start_min_rebound_bps", 0.0)),
            failed_start_loss_bps=float(_cfg(cfg, "risk.failed_start_loss_bps", 0.0)),
            chop_break_even_reclaim_enabled=bool(
                _cfg(cfg, "risk.chop_break_even_reclaim_enabled", False)
            ),
            chop_break_even_reclaim_min_bars=int(
                _cfg(cfg, "risk.chop_break_even_reclaim_min_bars", 0)
            ),
            chop_break_even_reclaim_min_drawdown_bps=float(
                _cfg(cfg, "risk.chop_break_even_reclaim_min_drawdown_bps", 0.0)
            ),
            chop_break_even_reclaim_max_edge_bps=float(
                _cfg(cfg, "risk.chop_break_even_reclaim_max_edge_bps", 0.0)
            ),
            chop_break_even_reclaim_cross_window_bars=int(
                _cfg(cfg, "risk.chop_break_even_reclaim_cross_window_bars", 0)
            ),
            chop_break_even_reclaim_min_crosses=int(
                _cfg(cfg, "risk.chop_break_even_reclaim_min_crosses", 0)
            ),
            require_break_even_for_exit=bool(_cfg(cfg, "risk.require_break_even_for_exit", False)),
            allow_reversal_exit_after_break_even=bool(
                _cfg(cfg, "risk.allow_reversal_exit_after_break_even", False)
            ),
            time_break_even_floor_enabled=bool(_cfg(cfg, "risk.time_break_even_floor_enabled", False)),
            time_break_even_floor_bars=int(_cfg(cfg, "risk.time_break_even_floor_bars", 0)),
            red_candle_exit_enabled=bool(_cfg(cfg, "risk.red_candle_exit_enabled", False)),
            red_candle_window_bars=int(_cfg(cfg, "risk.red_candle_window_bars", 0)),
            min_exit_profit_bps=float(_cfg(cfg, "risk.min_exit_profit_bps", 0.0)),
            hard_take_profit_bps=float(_cfg(cfg, "risk.hard_take_profit_bps", 0.0)),
            dynamic_profit_target_enabled=bool(_cfg(cfg, "risk.dynamic_profit_target_enabled", False)),
            dynamic_profit_target_bps_at_low=float(_cfg(cfg, "risk.dynamic_profit_target_bps_at_low", 0.0)),
            dynamic_profit_break_even_from_high_pct=float(
                _cfg(cfg, "risk.dynamic_profit_break_even_from_high_pct", 0.0)
            ),
            hard_stop_loss_bps=float(_cfg(cfg, "risk.hard_stop_loss_bps", 0.0)),
            hard_take_profit_only_in_range=bool(_cfg(cfg, "risk.hard_take_profit_only_in_range", False)),
            trailing_stop_enabled=bool(_cfg(cfg, "risk.trailing_stop_enabled", False)),
            trailing_activation_bps=float(_cfg(cfg, "risk.trailing_activation_bps", 0.0)),
            trailing_stop_bps=float(_cfg(cfg, "risk.trailing_stop_bps", 0.0)),
            trailing_stop_atr_mult=float(_cfg(cfg, "risk.trailing_stop_atr_mult", 0.0)),
            campaign_hold_enabled=bool(_cfg(cfg, "risk.campaign_hold_enabled", False)),
            campaign_hold_min_bars=int(_cfg(cfg, "risk.campaign_hold_min_bars", 0)),
            campaign_hold_min_profit_bps=float(_cfg(cfg, "risk.campaign_hold_min_profit_bps", 0.0)),
            campaign_hold_min_trend_bps=float(_cfg(cfg, "risk.campaign_hold_min_trend_bps", 0.0)),
            campaign_hold_max_range_pos=float(_cfg(cfg, "risk.campaign_hold_max_range_pos", 1.0)),
            campaign_hold_max_drawdown_from_peak_bps=float(
                _cfg(cfg, "risk.campaign_hold_max_drawdown_from_peak_bps", 0.0)
            ),
            campaign_hold_min_recent_bias_bps=float(
                _cfg(cfg, "risk.campaign_hold_min_recent_bias_bps", -999.0)
            ),
            peak_profit_retrace_enabled=bool(_cfg(cfg, "risk.peak_profit_retrace_enabled", False)),
            peak_profit_retrace_arm_bps=float(_cfg(cfg, "risk.peak_profit_retrace_arm_bps", 0.0)),
            peak_profit_retrace_pct=float(_cfg(cfg, "risk.peak_profit_retrace_pct", 0.0)),
            profit_roll_exit_enabled=bool(_cfg(cfg, "risk.profit_roll_exit_enabled", False)),
            profit_roll_arm_eur=float(_cfg(cfg, "risk.profit_roll_arm_eur", 0.0)),
            profit_roll_retrace_eur=float(_cfg(cfg, "risk.profit_roll_retrace_eur", 0.0)),
            profit_roll_retrace_pct=float(_cfg(cfg, "risk.profit_roll_retrace_pct", 50.0)),
            profit_roll_min_retrace_eur=float(_cfg(cfg, "risk.profit_roll_min_retrace_eur", 0.02)),
            profit_roll_min_keep_profit_bps=float(
                _cfg(cfg, "risk.profit_roll_min_keep_profit_bps", 2.0)
            ),
            reentry_min_move_bps=float(_cfg(cfg, "risk.reentry_min_move_bps", 0.0)),
            reentry_require_price_at_or_below_last_entry=bool(
                _cfg(cfg, "risk.reentry_require_price_at_or_below_last_entry", False)
            ),
            reentry_last_entry_tolerance_bps=float(
                _cfg(cfg, "risk.reentry_last_entry_tolerance_bps", 0.0)
            ),
            reentry_cooldown_bars_after_trailing_stop=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_trailing_stop", 0)
            ),
            reentry_cooldown_bars_after_whipsaw_stop_loss=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_whipsaw_stop_loss", 0)
            ),
            reentry_whipsaw_hard_stop_max_bars=int(
                _cfg(cfg, "risk.reentry_whipsaw_hard_stop_max_bars", 0)
            ),
            reentry_loss_cluster_window_bars=int(
                _cfg(cfg, "risk.reentry_loss_cluster_window_bars", 0)
            ),
            reentry_cooldown_bars_after_loss_cluster=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_loss_cluster", 0)
            ),
            reentry_cooldown_bars_after_weak_exit=int(
                _cfg(cfg, "risk.reentry_cooldown_bars_after_weak_exit", 0)
            ),
            rebalance_min_delta_eur=float(_cfg(cfg, "risk.rebalance_min_delta_eur", 0.0)),
            position_epsilon_btc=float(_cfg(cfg, "risk.position_epsilon_btc", position_epsilon_btc)),
            position_epsilon_eur=float(_cfg(cfg, "risk.position_epsilon_eur", 0.0)),
            full_position_only=bool(_cfg(cfg, "risk.full_position_only", False)),
        )
    )

    order_builder = OrderBuilder(
        OrderConfig(
            order_type=str(_cfg(cfg, "order.type", "market")),
            post_only=bool(_cfg(cfg, "order.post_only", False)),
            limit_offset_bps=float(_cfg(cfg, "order.limit_offset_bps", 3.0)),
            min_trade_btc=min_trade_btc,
            slice_count=int(_cfg(cfg, "order.slice_count", 1)),
            cycle_trade_eur=float(_cfg(cfg, "order.cycle_trade_eur", 0.0)),
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
