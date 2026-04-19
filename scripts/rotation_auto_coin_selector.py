#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sys


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.rotation_universe import POOL, PORTS
from trading.rotation_strategy_runtime import (
    ROTATION_RUNTIME_CONFIG_VERSION,
    build_selected_alpha_map,
)

DEFAULT_EXCLUDED_BASE_SYMBOLS: frozenset[str] = frozenset(
    {
        "EUR",
        "EURI",
        "USD",
        "USDC",
        "USDT",
        "FDUSD",
        "BUSD",
        "TUSD",
        "USDP",
        "DAI",
        "USDS",
        "PYUSD",
        "EURC",
    }
)

ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
START_SCRIPT = REPO_ROOT / "scripts" / "rotation_guards_start.sh"
CONFIG_TEMPLATE = REPO_ROOT / "configs" / "live_binance_kaito_usdc_rotation.yaml"
SELECTOR_CACHE_FILE = REPO_ROOT / "logs" / "rotation_selector_rows_cache.json"
COIN_EXPERIENCE_DEFAULT_LOOKBACK_DAYS = 21.0
COIN_EXPERIENCE_DEFAULT_HALF_LIFE_DAYS = 5.0
COIN_EXPERIENCE_DEFAULT_MIN_TRADES = 2
COIN_EXPERIENCE_DEFAULT_MIN_WEIGHTED_TRADES = 1.2
COIN_EXPERIENCE_DEFAULT_FULL_WEIGHT_TRADES = 5.0
COIN_EXPERIENCE_DEFAULT_MAX_ABS_SCORE = 36.0
COIN_EXPERIENCE_DEFAULT_MIN_ABS_SCORE = 2.0
FAST_SCOUT_LINE_LIMIT = 800
FAST_SCOUT_MAX_BYTES = 512 * 1024
LIVE_DECISION_LINE_LIMIT = 192
LIVE_DECISION_MAX_BYTES = 192 * 1024
LIVE_DECISION_BLOCK_WINDOW_SEC = 180.0
LIVE_DECISION_STALE_SEC = 240.0
LIVE_DECISION_DIRECT_BLOCK_REASONS = {
    "atr_below_entry_costs",
    "override_too_close_to_peak",
}
LIVE_DECISION_GATE_BLOCK_REASONS = {
    "edge_below_costs",
}

PROFILE_PRESETS: dict[str, dict[str, float | int]] = {
    "default": {
        "cont_rebound_confirm_bars": 1,
        "cont_trend_min_bps": 8.0,
        "cont_rebound_trigger_bps": 0.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 45.0,
        "cont_max_chase_bps": 16.0,
        "cont_max_range_pos": 0.72,
        "cont_min_volume_z": -999.0,
        "cont_max_structure_range_pos": 1.0,
        "cont_stall_recovery_max_range_pos": 0.86,
        "cont_range_continuation_max_range_pos": 0.88,
        "cont_init_max_range_pos": 0.68,
        "cont_hard_block_above_range_pos": 0.86,
        "cont_init_hard_block_above_range_pos": 0.77,
        "cont_hard_block_max_drawdown_bps": 120.0,
        "cont_init_hard_block_max_drawdown_bps": 170.0,
        "cont_staircase_min_trend_bps": 55.0,
        "cont_staircase_min_ret_bps": -10.0,
        "cont_staircase_min_volume_z": -1.2,
        "cont_staircase_min_slope_medium_bps": 1.0,
        "cont_staircase_min_slope_long_bps": 0.25,
        "cont_staircase_min_drawdown_from_peak_bps": 0.0,
        "cont_staircase_max_drawdown_from_peak_bps": 65.0,
        "cont_staircase_max_context_range_pos": 0.92,
        "cont_staircase_max_spread_bps": 18.0,
        "cont_staircase_require_up_structure": 1,
        "entry_edge_bps": 2.5,
        "entry_cost_buffer_bps": 1.0,
        "entry_cost_coverage_ratio": 1.0,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 1.0,
        "override_max_structure_range_pos": 0.99,
        "override_min_drawdown_from_peak_bps": 4.0,
        "override_min_drawdown_to_cost_ratio": 0.5,
        "override_min_slope_short_bps": 0.0,
        "late_entry_block_context_range_pos": 0.90,
        "late_entry_block_structure_range_pos": 0.99,
        "late_entry_block_max_context_drawdown_bps": 22.0,
        "late_entry_block_min_trend_return_bps": 100.0,
        "late_entry_block_min_return_bps": 12.0,
        "gate_cost_coverage_ratio": 1.0,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 2,
        "min_hold_bars": 6,
        "hard_stop_loss_bps": 120.0,
        "reentry_min_move_bps": 30.0,
        "reentry_cooldown_bars_after_weak_exit": 4,
        "min_exit_profit_bps": 8.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 3,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 2.0,
        "time_break_even_floor_bars": 6,
        "failed_start_min_bars": 2,
        "failed_start_max_bars": 3,
        "failed_start_min_rebound_bps": 0.0,
        "failed_start_loss_bps": 110.0,
        "trailing_activation_bps": 12.0,
        "trailing_stop_bps": 8.0,
        "campaign_hold_enabled": 0,
        "campaign_hold_min_bars": 0,
        "campaign_hold_min_profit_bps": 0.0,
        "campaign_hold_min_trend_bps": 0.0,
        "campaign_hold_max_range_pos": 1.0,
        "campaign_hold_max_drawdown_from_peak_bps": 0.0,
        "campaign_hold_min_recent_bias_bps": -999.0,
        "neutral_fraction_mult": 1.0,
        "down_fraction_mult": 0.6,
    },
    "scalp": {
        "cont_rebound_confirm_bars": 0,
        "cont_trend_min_bps": 2.0,
        "cont_rebound_trigger_bps": 8.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 70.0,
        "cont_max_chase_bps": 42.0,
        "cont_max_range_pos": 0.88,
        "cont_min_volume_z": -1.0,
        "cont_max_structure_range_pos": 1.00,
        "cont_stall_recovery_max_range_pos": 0.92,
        "cont_range_continuation_max_range_pos": 0.92,
        "cont_init_max_range_pos": 0.82,
        "cont_hard_block_above_range_pos": 0.992,
        "cont_init_hard_block_above_range_pos": 0.975,
        "cont_hard_block_max_drawdown_bps": 70.0,
        "cont_init_hard_block_max_drawdown_bps": 90.0,
        "cont_impulse_min_ret_bps": -10.0,
        "cont_impulse_min_volume_z": -1.6,
        "cont_impulse_max_context_range_pos": 1.0,
        "cont_impulse_max_extension_bps": 0.0,
        "cont_impulse_require_up_structure": 0,
        "cont_staircase_min_trend_bps": 50.0,
        "cont_staircase_min_ret_bps": -10.0,
        "cont_staircase_min_volume_z": -1.2,
        "cont_staircase_min_slope_medium_bps": 1.0,
        "cont_staircase_min_slope_long_bps": 0.25,
        "cont_staircase_min_drawdown_from_peak_bps": 0.0,
        "cont_staircase_max_drawdown_from_peak_bps": 60.0,
        "cont_staircase_max_context_range_pos": 0.92,
        "cont_staircase_max_spread_bps": 18.0,
        "cont_staircase_require_up_structure": 1,
        "entry_edge_bps": 1.5,
        "entry_cost_buffer_bps": 0.3,
        "entry_cost_coverage_ratio": 0.65,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 0.95,
        "override_max_structure_range_pos": 0.99,
        "override_min_drawdown_from_peak_bps": 5.0,
        "override_min_drawdown_to_cost_ratio": 0.6,
        "override_min_slope_short_bps": 0.2,
        "late_entry_block_context_range_pos": 0.89,
        "late_entry_block_structure_range_pos": 0.985,
        "late_entry_block_max_context_drawdown_bps": 24.0,
        "late_entry_block_min_trend_return_bps": 85.0,
        "late_entry_block_min_return_bps": 10.0,
        "gate_cost_coverage_ratio": 0.60,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 3,
        "min_hold_bars": 3,
        "hard_stop_loss_bps": 110.0,
        "reentry_min_move_bps": 30.0,
        "reentry_cooldown_bars_after_weak_exit": 4,
        "min_exit_profit_bps": 8.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 0,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 2.0,
        "time_break_even_floor_bars": 12,
        "failed_start_min_bars": 2,
        "failed_start_max_bars": 3,
        "failed_start_min_rebound_bps": 25.0,
        "failed_start_loss_bps": 45.0,
        "trailing_activation_bps": 16.0,
        "trailing_stop_bps": 8.0,
        "campaign_hold_enabled": 1,
        "campaign_hold_min_bars": 4,
        "campaign_hold_min_profit_bps": 10.0,
        "campaign_hold_min_trend_bps": 110.0,
        "campaign_hold_max_range_pos": 1.0,
        "campaign_hold_max_drawdown_from_peak_bps": 32.0,
        "campaign_hold_min_recent_bias_bps": -45.0,
        "neutral_fraction_mult": 0.35,
        "down_fraction_mult": 0.0,
        "md_interval_seconds": 60,
    },
    "scalp_breakout": {
        "cont_rebound_confirm_bars": 0,
        "cont_trend_min_bps": 3.5,
        "cont_rebound_trigger_bps": 5.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 75.0,
        "cont_max_chase_bps": 38.0,
        "cont_max_range_pos": 0.90,
        "cont_min_volume_z": -0.9,
        "cont_max_structure_range_pos": 0.99,
        "cont_stall_recovery_max_range_pos": 0.92,
        "cont_range_continuation_max_range_pos": 0.94,
        "cont_init_max_range_pos": 0.84,
        "cont_hard_block_above_range_pos": 0.97,
        "cont_init_hard_block_above_range_pos": 0.94,
        "cont_hard_block_max_drawdown_bps": 60.0,
        "cont_init_hard_block_max_drawdown_bps": 80.0,
        "cont_impulse_min_ret_bps": 1.0,
        "cont_impulse_min_volume_z": -1.2,
        "cont_impulse_max_context_range_pos": 0.98,
        "cont_impulse_max_extension_bps": 85.0,
        "cont_impulse_require_up_structure": 0,
        "cont_staircase_min_trend_bps": 40.0,
        "cont_staircase_min_ret_bps": -8.0,
        "cont_staircase_min_volume_z": -1.0,
        "cont_staircase_min_slope_medium_bps": 0.9,
        "cont_staircase_min_slope_long_bps": 0.2,
        "cont_staircase_min_drawdown_from_peak_bps": 4.0,
        "cont_staircase_max_drawdown_from_peak_bps": 80.0,
        "cont_staircase_max_context_range_pos": 0.95,
        "cont_staircase_max_spread_bps": 16.0,
        "cont_staircase_require_up_structure": 0,
        "entry_edge_bps": 2.0,
        "entry_cost_buffer_bps": 0.3,
        "entry_cost_coverage_ratio": 0.70,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 0.95,
        "override_max_structure_range_pos": 0.992,
        "override_min_drawdown_from_peak_bps": 4.0,
        "override_min_drawdown_to_cost_ratio": 0.5,
        "override_min_slope_short_bps": 0.1,
        "override_max_trend_return_bps": 320.0,
        "override_max_context_range_pos": 0.80,
        "late_entry_block_context_range_pos": 0.88,
        "late_entry_block_structure_range_pos": 0.985,
        "late_entry_block_max_context_drawdown_bps": 22.0,
        "late_entry_block_min_trend_return_bps": 90.0,
        "late_entry_block_min_return_bps": 14.0,
        "gate_cost_coverage_ratio": 0.62,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 4,
        "min_hold_bars": 3,
        "hard_stop_loss_bps": 85.0,
        "reentry_min_move_bps": 65.0,
        "reentry_cooldown_bars_after_weak_exit": 5,
        "min_exit_profit_bps": 9.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 0,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 4.0,
        "time_break_even_floor_bars": 8,
        "failed_start_min_bars": 1,
        "failed_start_max_bars": 2,
        "failed_start_min_rebound_bps": 24.0,
        "failed_start_loss_bps": 24.0,
        "trailing_activation_bps": 18.0,
        "trailing_stop_bps": 9.0,
        "campaign_hold_enabled": 1,
        "campaign_hold_min_bars": 3,
        "campaign_hold_min_profit_bps": 10.0,
        "campaign_hold_min_trend_bps": 125.0,
        "campaign_hold_max_range_pos": 1.0,
        "campaign_hold_max_drawdown_from_peak_bps": 42.0,
        "campaign_hold_min_recent_bias_bps": -60.0,
        "neutral_fraction_mult": 0.30,
        "down_fraction_mult": 0.0,
        "md_interval_seconds": 60,
    },
    "scalp_guarded": {
        "cont_rebound_confirm_bars": 0,
        "cont_trend_min_bps": 5.0,
        "cont_rebound_trigger_bps": 8.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 60.0,
        "cont_max_chase_bps": 28.0,
        "cont_max_range_pos": 0.82,
        "cont_min_volume_z": -0.8,
        "cont_max_structure_range_pos": 0.97,
        "cont_stall_recovery_max_range_pos": 0.90,
        "cont_range_continuation_max_range_pos": 0.90,
        "cont_init_max_range_pos": 0.78,
        "cont_hard_block_above_range_pos": 0.95,
        "cont_init_hard_block_above_range_pos": 0.92,
        "cont_hard_block_max_drawdown_bps": 55.0,
        "cont_init_hard_block_max_drawdown_bps": 70.0,
        "cont_impulse_min_ret_bps": 2.0,
        "cont_impulse_min_volume_z": -1.2,
        "cont_impulse_max_context_range_pos": 0.96,
        "cont_impulse_max_extension_bps": 70.0,
        "cont_impulse_require_up_structure": 0,
        "cont_staircase_min_trend_bps": 58.0,
        "cont_staircase_min_ret_bps": -8.0,
        "cont_staircase_min_volume_z": -0.9,
        "cont_staircase_min_slope_medium_bps": 1.2,
        "cont_staircase_min_slope_long_bps": 0.35,
        "cont_staircase_min_drawdown_from_peak_bps": 6.0,
        "cont_staircase_max_drawdown_from_peak_bps": 75.0,
        "cont_staircase_max_context_range_pos": 0.94,
        "cont_staircase_max_spread_bps": 18.0,
        "cont_staircase_require_up_structure": 0,
        "entry_edge_bps": 1.4,
        "entry_cost_buffer_bps": 0.0,
        "entry_cost_coverage_ratio": 0.60,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 0.85,
        "override_max_structure_range_pos": 0.985,
        "override_min_drawdown_from_peak_bps": 6.0,
        "override_min_drawdown_to_cost_ratio": 0.7,
        "override_min_slope_short_bps": 0.35,
        "override_max_trend_return_bps": 340.0,
        "override_max_context_range_pos": 0.82,
        "late_entry_block_context_range_pos": 0.84,
        "late_entry_block_structure_range_pos": 0.97,
        "late_entry_block_max_context_drawdown_bps": 24.0,
        "late_entry_block_min_trend_return_bps": 70.0,
        "late_entry_block_min_return_bps": 8.0,
        "gate_cost_coverage_ratio": 0.50,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 5,
        "min_hold_bars": 4,
        "hard_stop_loss_bps": 90.0,
        "reentry_min_move_bps": 75.0,
        "reentry_cooldown_bars_after_weak_exit": 5,
        "min_exit_profit_bps": 10.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 0,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 6.0,
        "time_break_even_floor_bars": 10,
        "failed_start_min_bars": 1,
        "failed_start_max_bars": 2,
        "failed_start_min_rebound_bps": 30.0,
        "failed_start_loss_bps": 26.0,
        "trailing_activation_bps": 20.0,
        "trailing_stop_bps": 12.0,
        "campaign_hold_enabled": 1,
        "campaign_hold_min_bars": 4,
        "campaign_hold_min_profit_bps": 11.0,
        "campaign_hold_min_trend_bps": 115.0,
        "campaign_hold_max_range_pos": 1.0,
        "campaign_hold_max_drawdown_from_peak_bps": 28.0,
        "campaign_hold_min_recent_bias_bps": -35.0,
        "neutral_fraction_mult": 0.25,
        "down_fraction_mult": 0.0,
        "md_interval_seconds": 60,
    },
    "scalp_guarded_open": {
        "cont_rebound_confirm_bars": 0,
        "cont_trend_min_bps": 5.0,
        "cont_rebound_trigger_bps": 8.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 60.0,
        "cont_max_chase_bps": 28.0,
        "cont_max_range_pos": 0.82,
        "cont_min_volume_z": -0.8,
        "cont_max_structure_range_pos": 0.97,
        "cont_stall_recovery_max_range_pos": 0.90,
        "cont_range_continuation_max_range_pos": 0.90,
        "cont_init_max_range_pos": 0.78,
        "cont_hard_block_above_range_pos": 0.95,
        "cont_init_hard_block_above_range_pos": 0.92,
        "cont_hard_block_max_drawdown_bps": 55.0,
        "cont_init_hard_block_max_drawdown_bps": 70.0,
        "cont_impulse_min_ret_bps": 2.0,
        "cont_impulse_min_volume_z": -1.2,
        "cont_impulse_max_context_range_pos": 0.96,
        "cont_impulse_max_extension_bps": 70.0,
        "cont_impulse_require_up_structure": 0,
        "cont_staircase_min_trend_bps": 58.0,
        "cont_staircase_min_ret_bps": -8.0,
        "cont_staircase_min_volume_z": -0.9,
        "cont_staircase_min_slope_medium_bps": 1.2,
        "cont_staircase_min_slope_long_bps": 0.35,
        "cont_staircase_min_drawdown_from_peak_bps": 6.0,
        "cont_staircase_max_drawdown_from_peak_bps": 75.0,
        "cont_staircase_max_context_range_pos": 0.94,
        "cont_staircase_max_spread_bps": 18.0,
        "cont_staircase_require_up_structure": 0,
        "entry_edge_bps": 1.4,
        "entry_cost_buffer_bps": 0.0,
        "entry_cost_coverage_ratio": 0.60,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 0.85,
        "override_max_structure_range_pos": 0.985,
        "override_min_drawdown_from_peak_bps": 6.0,
        "override_min_drawdown_to_cost_ratio": 0.7,
        "override_min_slope_short_bps": 0.35,
        "override_max_trend_return_bps": 340.0,
        "override_max_context_range_pos": 0.82,
        "late_entry_block_context_range_pos": 0.84,
        "late_entry_block_structure_range_pos": 0.97,
        "late_entry_block_max_context_drawdown_bps": 24.0,
        "late_entry_block_min_trend_return_bps": 70.0,
        "late_entry_block_min_return_bps": 8.0,
        "gate_cost_coverage_ratio": 0.50,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 5,
        "min_hold_bars": 4,
        "hard_stop_loss_bps": 90.0,
        "reentry_min_move_bps": 75.0,
        "reentry_cooldown_bars_after_weak_exit": 5,
        "min_exit_profit_bps": 10.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 0,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 6.0,
        "time_break_even_floor_bars": 10,
        "failed_start_min_bars": 1,
        "failed_start_max_bars": 2,
        "failed_start_min_rebound_bps": 30.0,
        "failed_start_loss_bps": 26.0,
        "trailing_activation_bps": 20.0,
        "trailing_stop_bps": 12.0,
        "campaign_hold_enabled": 1,
        "campaign_hold_min_bars": 4,
        "campaign_hold_min_profit_bps": 11.0,
        "campaign_hold_min_trend_bps": 115.0,
        "campaign_hold_max_range_pos": 1.0,
        "campaign_hold_max_drawdown_from_peak_bps": 28.0,
        "campaign_hold_min_recent_bias_bps": -35.0,
        "neutral_fraction_mult": 0.25,
        "down_fraction_mult": 0.0,
        "md_interval_seconds": 60,
    },
    "scalp_uptrend": {
        "cont_rebound_confirm_bars": 0,
        "cont_trend_min_bps": 6.0,
        "cont_rebound_trigger_bps": 12.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 42.0,
        "cont_max_chase_bps": 22.0,
        "cont_max_range_pos": 0.76,
        "cont_min_volume_z": -0.7,
        "cont_max_structure_range_pos": 0.94,
        "cont_stall_recovery_max_range_pos": 0.88,
        "cont_range_continuation_max_range_pos": 0.88,
        "cont_init_max_range_pos": 0.74,
        "cont_hard_block_above_range_pos": 0.91,
        "cont_init_hard_block_above_range_pos": 0.87,
        "cont_hard_block_max_drawdown_bps": 45.0,
        "cont_init_hard_block_max_drawdown_bps": 60.0,
        "cont_impulse_min_ret_bps": 8.0,
        "cont_impulse_min_volume_z": -0.8,
        "cont_impulse_max_context_range_pos": 0.93,
        "cont_impulse_max_extension_bps": 62.0,
        "cont_impulse_require_up_structure": 1,
        "cont_staircase_min_trend_bps": 62.0,
        "cont_staircase_min_ret_bps": -6.0,
        "cont_staircase_min_volume_z": -0.8,
        "cont_staircase_min_slope_medium_bps": 1.4,
        "cont_staircase_min_slope_long_bps": 0.45,
        "cont_staircase_min_drawdown_from_peak_bps": 8.0,
        "cont_staircase_max_drawdown_from_peak_bps": 68.0,
        "cont_staircase_max_context_range_pos": 0.90,
        "cont_staircase_max_spread_bps": 16.0,
        "cont_staircase_require_up_structure": 1,
        "entry_edge_bps": 3.4,
        "entry_cost_buffer_bps": 0.8,
        "entry_cost_coverage_ratio": 0.88,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 1.15,
        "override_max_structure_range_pos": 0.982,
        "override_min_drawdown_from_peak_bps": 7.0,
        "override_min_drawdown_to_cost_ratio": 0.8,
        "override_min_slope_short_bps": 0.45,
        "override_max_trend_return_bps": 420.0,
        "override_max_context_range_pos": 0.84,
        "late_entry_block_context_range_pos": 0.82,
        "late_entry_block_structure_range_pos": 0.95,
        "late_entry_block_max_context_drawdown_bps": 22.0,
        "late_entry_block_min_trend_return_bps": 105.0,
        "late_entry_block_min_return_bps": 10.0,
        "gate_cost_coverage_ratio": 0.78,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 6,
        "min_hold_bars": 4,
        "hard_stop_loss_bps": 86.0,
        "reentry_min_move_bps": 95.0,
        "reentry_cooldown_bars_after_weak_exit": 4,
        "min_exit_profit_bps": 12.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 0,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 7.0,
        "time_break_even_floor_bars": 12,
        "failed_start_min_bars": 2,
        "failed_start_max_bars": 2,
        "failed_start_min_rebound_bps": 34.0,
        "failed_start_loss_bps": 24.0,
        "trailing_activation_bps": 24.0,
        "trailing_stop_bps": 12.0,
        "campaign_hold_enabled": 1,
        "campaign_hold_min_bars": 5,
        "campaign_hold_min_profit_bps": 12.0,
        "campaign_hold_min_trend_bps": 130.0,
        "campaign_hold_max_range_pos": 0.96,
        "campaign_hold_max_drawdown_from_peak_bps": 24.0,
        "campaign_hold_min_recent_bias_bps": -30.0,
        "neutral_fraction_mult": 0.22,
        "down_fraction_mult": 0.0,
        "md_interval_seconds": 60,
    },
    "scalp_lockdown": {
        "cont_rebound_confirm_bars": 0,
        "cont_trend_min_bps": 10.0,
        "cont_rebound_trigger_bps": 18.0,
        "cont_pullback_min_bps": 0.0,
        "cont_pullback_max_bps": 35.0,
        "cont_max_chase_bps": 18.0,
        "cont_max_range_pos": 0.68,
        "cont_min_volume_z": -0.5,
        "cont_max_structure_range_pos": 0.88,
        "cont_stall_recovery_max_range_pos": 0.84,
        "cont_range_continuation_max_range_pos": 0.86,
        "cont_init_max_range_pos": 0.64,
        "cont_hard_block_above_range_pos": 0.84,
        "cont_init_hard_block_above_range_pos": 0.80,
        "cont_hard_block_max_drawdown_bps": 35.0,
        "cont_init_hard_block_max_drawdown_bps": 45.0,
        "cont_impulse_min_ret_bps": 18.0,
        "cont_impulse_min_volume_z": -0.5,
        "cont_impulse_max_context_range_pos": 0.88,
        "cont_impulse_max_extension_bps": 45.0,
        "cont_impulse_require_up_structure": 1,
        "cont_staircase_min_trend_bps": 85.0,
        "cont_staircase_min_ret_bps": -6.0,
        "cont_staircase_min_volume_z": -0.7,
        "cont_staircase_min_slope_medium_bps": 2.0,
        "cont_staircase_min_slope_long_bps": 0.6,
        "cont_staircase_min_drawdown_from_peak_bps": 8.0,
        "cont_staircase_max_drawdown_from_peak_bps": 45.0,
        "cont_staircase_max_context_range_pos": 0.88,
        "cont_staircase_max_spread_bps": 16.0,
        "cont_staircase_require_up_structure": 1,
        "entry_edge_bps": 6.0,
        "entry_cost_buffer_bps": 1.5,
        "entry_cost_coverage_ratio": 1.0,
        "entry_cost_roundtrip_multiplier": 2.0,
        "entry_min_atr_to_cost_ratio": 1.25,
        "override_max_structure_range_pos": 0.98,
        "override_min_drawdown_from_peak_bps": 8.0,
        "override_min_drawdown_to_cost_ratio": 0.85,
        "override_min_slope_short_bps": 0.6,
        "late_entry_block_context_range_pos": 0.78,
        "late_entry_block_structure_range_pos": 0.90,
        "late_entry_block_max_context_drawdown_bps": 18.0,
        "late_entry_block_min_trend_return_bps": 60.0,
        "late_entry_block_min_return_bps": 6.0,
        "gate_cost_coverage_ratio": 0.95,
        "gate_cost_roundtrip_multiplier": 2.0,
        "cooldown_bars": 10,
        "min_hold_bars": 4,
        "hard_stop_loss_bps": 80.0,
        "reentry_min_move_bps": 120.0,
        "reentry_cooldown_bars_after_weak_exit": 6,
        "min_exit_profit_bps": 16.0,
        "green_candle_take_exit_enabled": 0,
        "green_candle_take_min_bars": 2,
        "green_candle_take_max_bars": 0,
        "green_candle_take_required_green_bars": 2,
        "green_candle_take_min_profit_bps": 10.0,
        "time_break_even_floor_bars": 8,
        "failed_start_min_bars": 2,
        "failed_start_max_bars": 2,
        "failed_start_min_rebound_bps": 40.0,
        "failed_start_loss_bps": 24.0,
        "trailing_activation_bps": 30.0,
        "trailing_stop_bps": 14.0,
        "campaign_hold_enabled": 1,
        "campaign_hold_min_bars": 5,
        "campaign_hold_min_profit_bps": 14.0,
        "campaign_hold_min_trend_bps": 150.0,
        "campaign_hold_max_range_pos": 1.0,
        "campaign_hold_max_drawdown_from_peak_bps": 18.0,
        "campaign_hold_min_recent_bias_bps": -25.0,
        "neutral_fraction_mult": 0.20,
        "down_fraction_mult": 0.0,
        "md_interval_seconds": 60,
    },
}

STRATEGY_NAMES: tuple[str, ...] = (
    "staircase",
    "pullback_continuation",
    "breakout_retest",
    "continuation",
    "breakout",
    "relative_strength",
    "rebound",
)
CORE_STRATEGY_NAMES: tuple[str, ...] = (
    "staircase",
    "continuation",
    "breakout",
    "rebound",
)
STRATEGY_ACTION_MODE_RANK: dict[str, int] = {
    "pause": 0,
    "watch": 1,
    "secondary": 2,
    "primary": 3,
}
STRATEGY_ACTION_MODE_MULTIPLIER: dict[str, float] = {
    "pause": 0.0,
    "watch": 0.75,
    "secondary": 1.0,
    "primary": 1.15,
}

PROFILE_STRATEGY_CYCLES: dict[str, tuple[str, ...]] = {
    "default": ("continuation", "pullback_continuation", "breakout_retest", "relative_strength"),
    "scalp": ("breakout_retest", "staircase", "pullback_continuation", "continuation"),
    "scalp_breakout": ("breakout_retest", "breakout", "staircase", "pullback_continuation"),
    "scalp_guarded": ("staircase", "pullback_continuation", "continuation", "breakout_retest"),
    "scalp_guarded_open": ("staircase", "pullback_continuation", "continuation", "breakout_retest"),
    "scalp_uptrend": ("staircase", "pullback_continuation", "breakout_retest", "relative_strength"),
    "scalp_lockdown": ("continuation", "pullback_continuation", "staircase", "relative_strength"),
}

PROFILE_FAST_TRACK_LIMITS: dict[str, int] = {
    "default": 1,
    "scalp": 2,
    "scalp_breakout": 2,
    "scalp_guarded": 1,
    "scalp_guarded_open": 1,
    "scalp_uptrend": 1,
    "scalp_lockdown": 0,
}

STRATEGY_RANK_LIMIT = 12
GUARDED_REBOUND_PROFILES = {"scalp_guarded", "scalp_guarded_open"}


def _enabled_strategy_names() -> tuple[str, ...]:
    raw = str(os.environ.get("ROTATION_ENABLED_STRATEGIES", "") or "").strip().lower()
    if not raw:
        return CORE_STRATEGY_NAMES
    enabled: list[str] = []
    for item in raw.split(","):
        strategy = str(item).strip().lower()
        if strategy in STRATEGY_NAMES and strategy not in enabled:
            enabled.append(strategy)
    if enabled:
        return tuple(enabled)
    return CORE_STRATEGY_NAMES


def _sanitize_swing_bands(
    buy_band: object,
    sell_band: object,
    *,
    default_buy: float = 0.42,
    default_sell: float = 0.67,
) -> tuple[float, float]:
    try:
        buy = float(buy_band)
    except Exception:
        buy = float(default_buy)
    try:
        sell = float(sell_band)
    except Exception:
        sell = float(default_sell)
    buy = max(0.01, min(0.98, buy))
    sell = max(0.02, min(0.99, sell))
    min_gap = 0.01
    if sell <= buy + min_gap:
        if buy + min_gap < 0.99:
            sell = buy + min_gap
        else:
            buy = max(0.01, sell - min_gap)
    return buy, sell


def _profile_values(name: str) -> dict[str, float | int]:
    profile_name = str(name or "default").strip().lower()
    if profile_name not in PROFILE_PRESETS:
        raise ValueError(f"Unknown profile: {profile_name}")
    profile = dict(PROFILE_PRESETS[profile_name])
    for env_name, key in (
        ("ROTATION_NEUTRAL_FRACTION_MULT", "neutral_fraction_mult"),
        ("ROTATION_DOWN_FRACTION_MULT", "down_fraction_mult"),
        ("ROTATION_CONT_REBOUND_CONFIRM_BARS", "cont_rebound_confirm_bars"),
        ("ROTATION_CONT_TREND_MIN_BPS", "cont_trend_min_bps"),
        ("ROTATION_CONT_REBOUND_TRIGGER_BPS", "cont_rebound_trigger_bps"),
        ("ROTATION_CONT_MIN_VOLUME_Z", "cont_min_volume_z"),
        ("ROTATION_CONT_MAX_RANGE_POS", "cont_max_range_pos"),
        ("ROTATION_CONT_PULLBACK_MAX_BPS", "cont_pullback_max_bps"),
        ("ROTATION_CONT_MAX_STRUCTURE_RANGE_POS", "cont_max_structure_range_pos"),
        (
            "ROTATION_CONT_RANGE_CONTINUATION_MAX_RANGE_POS",
            "cont_range_continuation_max_range_pos",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MIN_TREND_BPS",
            "cont_staircase_min_trend_bps",
        ),
        ("ROTATION_CONT_STAIRCASE_MIN_RET_BPS", "cont_staircase_min_ret_bps"),
        (
            "ROTATION_CONT_STAIRCASE_MIN_VOLUME_Z",
            "cont_staircase_min_volume_z",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS",
            "cont_staircase_min_slope_medium_bps",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MIN_SLOPE_LONG_BPS",
            "cont_staircase_min_slope_long_bps",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS",
            "cont_staircase_min_drawdown_from_peak_bps",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MAX_DRAWDOWN_FROM_PEAK_BPS",
            "cont_staircase_max_drawdown_from_peak_bps",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MAX_CONTEXT_RANGE_POS",
            "cont_staircase_max_context_range_pos",
        ),
        (
            "ROTATION_CONT_STAIRCASE_MAX_SPREAD_BPS",
            "cont_staircase_max_spread_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MAX_CONTEXT_RANGE_POS",
            "cont_early_liftoff_max_context_range_pos",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MAX_STRUCTURE_RANGE_POS",
            "cont_early_liftoff_max_structure_range_pos",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_CONTEXT_REBOUND_BPS",
            "cont_early_liftoff_min_context_rebound_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MAX_SPREAD_BPS",
            "cont_early_liftoff_max_spread_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_VOLUME_Z",
            "cont_early_liftoff_min_volume_z",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_RET_BPS",
            "cont_early_liftoff_min_ret_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_SLOPE_SHORT_BPS",
            "cont_early_liftoff_min_slope_short_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_SLOPE_MEDIUM_BPS",
            "cont_early_liftoff_min_slope_medium_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_DRAWDOWN_FROM_PEAK_BPS",
            "cont_early_liftoff_min_drawdown_from_peak_bps",
        ),
        (
            "ROTATION_CONT_EARLY_LIFTOFF_MIN_TREND_BPS",
            "cont_early_liftoff_min_trend_bps",
        ),
        ("ROTATION_BREAKOUT_TRIGGER_BPS", "breakout_trigger_bps"),
        (
            "ROTATION_BREAKOUT_BOTTOM_COUNTERTREND_BLOCK_MAX_CONTEXT_RANGE_POS",
            "breakout_bottom_countertrend_block_max_context_range_pos",
        ),
        (
            "ROTATION_BREAKOUT_BOTTOM_COUNTERTREND_BLOCK_MAX_CONTEXT_REBOUND_BPS",
            "breakout_bottom_countertrend_block_max_context_rebound_bps",
        ),
        (
            "ROTATION_BREAKOUT_BOTTOM_COUNTERTREND_BLOCK_MAX_TREND_RETURN_BPS",
            "breakout_bottom_countertrend_block_max_trend_return_bps",
        ),
        (
            "ROTATION_BREAKOUT_TOP_ZONE_BLOCK_MIN_CONTEXT_RANGE_POS",
            "breakout_top_zone_block_min_context_range_pos",
        ),
        (
            "ROTATION_BREAKOUT_TOP_ZONE_BLOCK_MAX_CONTEXT_REBOUND_BPS",
            "breakout_top_zone_block_max_context_rebound_bps",
        ),
        (
            "ROTATION_BREAKOUT_TOP_ZONE_BLOCK_MIN_VOLUME_Z",
            "breakout_top_zone_block_min_volume_z",
        ),
        (
            "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_CONTEXT_RANGE_POS",
            "breakout_thin_rebound_block_context_range_pos",
        ),
        (
            "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_CONTEXT_REBOUND_BPS",
            "breakout_thin_rebound_block_context_rebound_bps",
        ),
        (
            "ROTATION_BREAKOUT_THIN_REBOUND_BLOCK_MIN_SPREAD_BPS",
            "breakout_thin_rebound_block_min_spread_bps",
        ),
        (
            "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_CONTEXT_RANGE_POS",
            "breakout_mid_rebound_block_context_range_pos",
        ),
        (
            "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_CONTEXT_REBOUND_BPS",
            "breakout_mid_rebound_block_context_rebound_bps",
        ),
        (
            "ROTATION_BREAKOUT_MID_REBOUND_BLOCK_MIN_VOLUME_Z",
            "breakout_mid_rebound_block_min_volume_z",
        ),
        (
            "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_CONTEXT_RANGE_POS",
            "breakout_late_rebound_block_context_range_pos",
        ),
        (
            "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_CONTEXT_REBOUND_BPS",
            "breakout_late_rebound_block_context_rebound_bps",
        ),
        (
            "ROTATION_BREAKOUT_LATE_REBOUND_BLOCK_MIN_VOLUME_Z",
            "breakout_late_rebound_block_min_volume_z",
        ),
        ("ROTATION_ENTRY_EDGE_BPS", "entry_edge_bps"),
        ("ROTATION_ENTRY_COST_BUFFER_BPS", "entry_cost_buffer_bps"),
        ("ROTATION_ENTRY_COST_COVERAGE_RATIO", "entry_cost_coverage_ratio"),
        ("ROTATION_ENTRY_COST_ROUNDTRIP_MULTIPLIER", "entry_cost_roundtrip_multiplier"),
        ("ROTATION_ENTRY_MIN_ATR_TO_COST_RATIO", "entry_min_atr_to_cost_ratio"),
        ("ROTATION_DISABLE_ENTRY_EDGE_GATE", "disable_entry_edge_gate"),
        ("ROTATION_BLOCK_LONG_IF_CONTEXT_RETURN_BPS_BELOW", "block_long_if_context_return_bps_below"),
        ("ROTATION_BLOCK_LONG_IF_TREND_RETURN_BPS_BELOW", "block_long_if_trend_return_bps_below"),
        ("ROTATION_SWING_REVERSAL_THRESHOLD_BPS", "swing_reversal_threshold_bps"),
        ("ROTATION_SWING_MIN_RANGE_BPS", "swing_min_range_bps"),
        ("ROTATION_SWING_MICRO_REBOUND_MAX_SPREAD_BPS", "swing_micro_rebound_max_spread_bps"),
        (
            "ROTATION_SWING_MICRO_REBOUND_MAX_CONTEXT_RANGE_POS",
            "swing_micro_rebound_max_context_range_pos",
        ),
        (
            "ROTATION_SWING_MICRO_REBOUND_MIN_CONTEXT_REBOUND_BPS",
            "swing_micro_rebound_min_context_rebound_bps",
        ),
        (
            "ROTATION_SWING_MICRO_REBOUND_MIN_RET_BPS",
            "swing_micro_rebound_min_ret_bps",
        ),
        (
            "ROTATION_SWING_MICRO_REBOUND_CONFIRM_REBOUND_BPS",
            "swing_micro_rebound_confirm_rebound_bps",
        ),
        (
            "ROTATION_SWING_MICRO_REBOUND_CONFIRM_MIN_RET_BPS",
            "swing_micro_rebound_confirm_min_ret_bps",
        ),
        ("ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS", "late_entry_block_context_range_pos"),
        ("ROTATION_LATE_ENTRY_BLOCK_STRUCTURE_RANGE_POS", "late_entry_block_structure_range_pos"),
        (
            "ROTATION_LATE_ENTRY_BLOCK_MAX_CONTEXT_DRAWDOWN_BPS",
            "late_entry_block_max_context_drawdown_bps",
        ),
        (
            "ROTATION_LATE_ENTRY_BLOCK_MIN_TREND_RETURN_BPS",
            "late_entry_block_min_trend_return_bps",
        ),
        ("ROTATION_LATE_ENTRY_BLOCK_MIN_RETURN_BPS", "late_entry_block_min_return_bps"),
        ("ROTATION_OVERRIDE_MAX_STRUCTURE_RANGE_POS", "override_max_structure_range_pos"),
        ("ROTATION_OVERRIDE_MIN_DRAWDOWN_FROM_PEAK_BPS", "override_min_drawdown_from_peak_bps"),
        ("ROTATION_OVERRIDE_MIN_DRAWDOWN_TO_COST_RATIO", "override_min_drawdown_to_cost_ratio"),
        ("ROTATION_OVERRIDE_MIN_SLOPE_SHORT_BPS", "override_min_slope_short_bps"),
        ("ROTATION_OVERRIDE_MAX_TREND_RETURN_BPS", "override_max_trend_return_bps"),
        ("ROTATION_OVERRIDE_MAX_CONTEXT_RANGE_POS", "override_max_context_range_pos"),
        ("ROTATION_GATE_COST_COVERAGE_RATIO", "gate_cost_coverage_ratio"),
        ("ROTATION_GATE_COST_ROUNDTRIP_MULTIPLIER", "gate_cost_roundtrip_multiplier"),
        ("ROTATION_GATE_SAFETY_MARGIN_BPS", "gate_safety_margin_bps"),
        ("ROTATION_MAX_SPREAD_BPS", "max_spread_bps"),
        (
            "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_TRAILING_STOP",
            "reentry_cooldown_bars_after_trailing_stop",
        ),
        (
            "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WHIPSAW_STOP_LOSS",
            "reentry_cooldown_bars_after_whipsaw_stop_loss",
        ),
        (
            "ROTATION_REENTRY_WHIPSAW_HARD_STOP_MAX_BARS",
            "reentry_whipsaw_hard_stop_max_bars",
        ),
        (
            "ROTATION_REENTRY_LOSS_CLUSTER_WINDOW_BARS",
            "reentry_loss_cluster_window_bars",
        ),
        (
            "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_LOSS_CLUSTER",
            "reentry_cooldown_bars_after_loss_cluster",
        ),
        (
            "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WEAK_EXIT",
            "reentry_cooldown_bars_after_weak_exit",
        ),
        ("ROTATION_REENTRY_MIN_MOVE_BPS", "reentry_min_move_bps"),
        (
            "ROTATION_REENTRY_REQUIRE_PRICE_AT_OR_BELOW_LAST_ENTRY",
            "reentry_require_price_at_or_below_last_entry",
        ),
        ("ROTATION_REENTRY_LAST_ENTRY_TOLERANCE_BPS", "reentry_last_entry_tolerance_bps"),
        ("ROTATION_FAILED_START_MIN_BARS", "failed_start_min_bars"),
        ("ROTATION_FAILED_START_MAX_BARS", "failed_start_max_bars"),
        ("ROTATION_FAILED_START_MIN_REBOUND_BPS", "failed_start_min_rebound_bps"),
        ("ROTATION_FAILED_START_LOSS_BPS", "failed_start_loss_bps"),
        ("ROTATION_HARD_STOP_LOSS_BPS", "hard_stop_loss_bps"),
        ("ROTATION_MIN_EXIT_PROFIT_BPS", "min_exit_profit_bps"),
        ("ROTATION_DAILY_LOSS_LIMIT_EUR", "daily_loss_limit_eur"),
        ("ROTATION_MAX_DRAWDOWN_PCT", "max_drawdown_pct"),
        ("ROTATION_GREEN_CANDLE_TAKE_MIN_BARS", "green_candle_take_min_bars"),
        ("ROTATION_GREEN_CANDLE_TAKE_MAX_BARS", "green_candle_take_max_bars"),
        ("ROTATION_GREEN_CANDLE_TAKE_REQUIRED_GREEN_BARS", "green_candle_take_required_green_bars"),
        ("ROTATION_GREEN_CANDLE_TAKE_MIN_PROFIT_BPS", "green_candle_take_min_profit_bps"),
        ("ROTATION_TRAILING_ACTIVATION_BPS", "trailing_activation_bps"),
        ("ROTATION_TRAILING_STOP_BPS", "trailing_stop_bps"),
        ("ROTATION_CAMPAIGN_HOLD_ENABLED", "campaign_hold_enabled"),
        ("ROTATION_CAMPAIGN_HOLD_MIN_BARS", "campaign_hold_min_bars"),
        ("ROTATION_CAMPAIGN_HOLD_MIN_PROFIT_BPS", "campaign_hold_min_profit_bps"),
        ("ROTATION_CAMPAIGN_HOLD_MIN_TREND_BPS", "campaign_hold_min_trend_bps"),
        (
            "ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS",
            "campaign_hold_max_drawdown_from_peak_bps",
        ),
        ("ROTATION_TIME_BREAK_EVEN_FLOOR_BARS", "time_break_even_floor_bars"),
        ("ROTATION_MD_INTERVAL_SECONDS", "md_interval_seconds"),
        ("ROTATION_EXEC_RECONCILE_INTERVAL_SEC", "exec_reconcile_interval_sec"),
        ("ROTATION_PROFIT_ROLL_EXIT_ENABLED", "profit_roll_exit_enabled"),
        ("ROTATION_PROFIT_ROLL_ARM_EUR", "profit_roll_arm_eur"),
        ("ROTATION_PROFIT_ROLL_RETRACE_EUR", "profit_roll_retrace_eur"),
        ("ROTATION_PROFIT_ROLL_RETRACE_PCT", "profit_roll_retrace_pct"),
        ("ROTATION_PROFIT_ROLL_MIN_RETRACE_EUR", "profit_roll_min_retrace_eur"),
        ("ROTATION_PROFIT_ROLL_MIN_KEEP_PROFIT_BPS", "profit_roll_min_keep_profit_bps"),
        ("ROTATION_SWING_LOOKBACK_BARS", "swing_lookback_bars"),
        ("ROTATION_SWING_BUY_BAND", "swing_buy_band"),
        ("ROTATION_SWING_SELL_BAND", "swing_sell_band"),
        ("ROTATION_SWING_MOMENTUM_LOOKBACK", "swing_momentum_lookback"),
        ("ROTATION_SWING_EDGE_SCALE", "swing_edge_scale"),
        ("ROTATION_SWING_MAX_EDGE_BPS", "swing_max_edge_bps"),
        ("ROTATION_CORRIDOR_STAGED_MODE_ENABLED", "corridor_staged_mode_enabled"),
        ("ROTATION_CORRIDOR_STAGED_ENTRY_1_PCT", "corridor_staged_entry_1_pct"),
        ("ROTATION_CORRIDOR_STAGED_ENTRY_2_PCT", "corridor_staged_entry_2_pct"),
        ("ROTATION_CORRIDOR_STAGED_ENTRY_3_PCT", "corridor_staged_entry_3_pct"),
        ("ROTATION_CORRIDOR_STAGED_ENTRY_4_PCT", "corridor_staged_entry_4_pct"),
        ("ROTATION_CORRIDOR_STAGED_NO_BUY_ABOVE_PCT", "corridor_staged_no_buy_above_pct"),
        ("ROTATION_CORRIDOR_STAGED_EXIT_STEP_PCT", "corridor_staged_exit_step_pct"),
        ("ROTATION_CORRIDOR_STAGED_HYSTERESIS_PCT", "corridor_staged_hysteresis_pct"),
        ("ROTATION_CORRIDOR_STAGED_EXIT_RETRACE_PCT", "corridor_staged_exit_retrace_pct"),
        ("ROTATION_CORRIDOR_STAGED_ENTRY_WAIT_BARS", "corridor_staged_entry_wait_bars"),
        (
            "ROTATION_CORRIDOR_STAGED_TRANSITION_SMOOTHING_BARS",
            "corridor_staged_transition_smoothing_bars",
        ),
        ("ROTATION_CORRIDOR_STAGED_REQUIRE_RISING", "corridor_staged_require_rising"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_ENABLED", "corridor_staged_profit_target_enabled"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_BASE_PCT", "corridor_staged_profit_target_base_pct"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MIN_PCT", "corridor_staged_profit_target_min_pct"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MAX_PCT", "corridor_staged_profit_target_max_pct"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MULT_10", "corridor_staged_profit_target_mult_10"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MULT_20", "corridor_staged_profit_target_mult_20"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MULT_30", "corridor_staged_profit_target_mult_30"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MULT_40", "corridor_staged_profit_target_mult_40"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_TARGET_MULT_50", "corridor_staged_profit_target_mult_50"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_AUTO_ENABLED", "corridor_staged_profit_auto_enabled"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_AUTO_WIDTH_SCALE", "corridor_staged_profit_auto_width_scale"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_AUTO_WIDTH_OFFSET", "corridor_staged_profit_auto_width_offset"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_AUTO_MIN_PCT", "corridor_staged_profit_auto_min_pct"),
        ("ROTATION_CORRIDOR_STAGED_PROFIT_AUTO_MAX_PCT", "corridor_staged_profit_auto_max_pct"),
        ("ROTATION_CORRIDOR_WINDOW_BARS", "corridor_window_bars"),
        ("ROTATION_CORRIDOR_MIN_BARS", "corridor_min_bars"),
        ("ROTATION_CORRIDOR_FAST_WINDOW_BARS", "corridor_fast_window_bars"),
        ("ROTATION_CORRIDOR_FAST_MIN_BARS", "corridor_fast_min_bars"),
        ("ROTATION_CORRIDOR_FAST_BLEND_WEIGHT", "corridor_fast_blend_weight"),
        ("ROTATION_CORRIDOR_ROBUST_RANGE_ENABLED", "corridor_robust_range_enabled"),
        ("ROTATION_CORRIDOR_ROBUST_LOW_PCT", "corridor_robust_low_pct"),
        ("ROTATION_CORRIDOR_ROBUST_HIGH_PCT", "corridor_robust_high_pct"),
        (
            "ROTATION_CORRIDOR_SHORT_HORIZON_ENTRY_GUARD_ENABLED",
            "corridor_short_horizon_entry_guard_enabled",
        ),
        (
            "ROTATION_CORRIDOR_SHORT_HORIZON_ENTRY_WINDOW_BARS",
            "corridor_short_horizon_entry_window_bars",
        ),
        (
            "ROTATION_CORRIDOR_SHORT_HORIZON_ENTRY_MIN_BARS",
            "corridor_short_horizon_entry_min_bars",
        ),
        (
            "ROTATION_CORRIDOR_SHORT_HORIZON_NO_BUY_ABOVE_PCT",
            "corridor_short_horizon_no_buy_above_pct",
        ),
        ("ROTATION_FEATURE_CONTEXT_WINDOW_BARS", "feature_context_window_bars"),
        ("ROTATION_SAFETY_EXITS_ENABLED", "safety_exits_enabled"),
        ("ROTATION_REQUIRE_BREAK_EVEN_FOR_EXIT", "require_break_even_for_exit"),
    ):
        raw = os.environ.get(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            profile[key] = float(raw)
        except Exception:
            continue
    alpha_type_raw = str(os.environ.get("ROTATION_ALPHA_TYPE", "") or "").strip().lower()
    if alpha_type_raw:
        profile["alpha_type"] = alpha_type_raw
    disable_safety_raw = str(os.environ.get("ROTATION_DISABLE_SAFETY_EXITS", "") or "").strip().lower()
    if disable_safety_raw in {"1", "true", "yes", "on"}:
        profile["safety_exits_enabled"] = 0.0
    profile.setdefault("swing_reversal_threshold_bps", 0.4)
    profile.setdefault("swing_lookback_bars", 20.0)
    profile.setdefault("swing_buy_band", 0.42)
    profile.setdefault("swing_sell_band", 0.67)
    profile.setdefault("swing_momentum_lookback", 7.0)
    profile.setdefault("swing_edge_scale", 1.1)
    profile.setdefault("swing_max_edge_bps", 155.0)
    profile.setdefault("feature_context_window_bars", 576.0)
    profile.setdefault("daily_loss_limit_eur", 100000.0)
    profile.setdefault("max_drawdown_pct", 100000.0)
    profile.setdefault("safety_exits_enabled", 1.0)
    profile.setdefault("require_break_even_for_exit", 1.0)
    profile.setdefault("exec_reconcile_interval_sec", 5.0)
    profile.setdefault("profit_roll_exit_enabled", 0.0)
    profile.setdefault("profit_roll_arm_eur", 0.0)
    profile.setdefault("profit_roll_retrace_eur", 0.0)
    profile.setdefault("profit_roll_retrace_pct", 50.0)
    profile.setdefault("profit_roll_min_retrace_eur", 0.02)
    profile.setdefault("profit_roll_min_keep_profit_bps", 2.0)
    profile.setdefault("alpha_type", "continuation")
    profile.setdefault("swing_min_range_bps", 52.0)
    profile.setdefault("swing_micro_rebound_max_spread_bps", 8.0)
    profile.setdefault("gate_safety_margin_bps", 0.5)
    profile.setdefault("max_spread_bps", 22.0)
    profile.setdefault("disable_entry_edge_gate", 0.0)
    profile.setdefault("block_long_if_context_return_bps_below", 0.0)
    profile.setdefault("block_long_if_trend_return_bps_below", 0.0)
    profile.setdefault("swing_micro_rebound_max_context_range_pos", 0.38)
    profile.setdefault("swing_micro_rebound_min_context_rebound_bps", 0.0)
    profile.setdefault("swing_micro_rebound_min_ret_bps", -2.0)
    profile.setdefault("swing_micro_rebound_confirm_rebound_bps", 0.0)
    profile.setdefault("swing_micro_rebound_confirm_min_ret_bps", 0.0)
    profile.setdefault("breakout_trigger_bps", 6.0)
    profile.setdefault("breakout_bottom_countertrend_block_max_context_range_pos", 0.22)
    profile.setdefault("breakout_bottom_countertrend_block_max_context_rebound_bps", 140.0)
    profile.setdefault("breakout_bottom_countertrend_block_max_trend_return_bps", -55.0)
    profile.setdefault("breakout_top_zone_block_min_context_range_pos", 0.98)
    profile.setdefault("breakout_top_zone_block_max_context_rebound_bps", 90.0)
    profile.setdefault("breakout_top_zone_block_min_volume_z", 0.4)
    profile.setdefault("breakout_thin_rebound_block_context_range_pos", 0.60)
    profile.setdefault("breakout_thin_rebound_block_context_rebound_bps", 400.0)
    profile.setdefault("breakout_thin_rebound_block_min_spread_bps", 18.0)
    profile.setdefault("breakout_mid_rebound_block_context_range_pos", 0.48)
    profile.setdefault("breakout_mid_rebound_block_context_rebound_bps", 760.0)
    profile.setdefault("breakout_mid_rebound_block_min_volume_z", 0.0)
    profile.setdefault("breakout_late_rebound_block_context_range_pos", 0.72)
    profile.setdefault("breakout_late_rebound_block_context_rebound_bps", 1400.0)
    profile.setdefault("breakout_late_rebound_block_min_volume_z", 0.35)
    profile.setdefault("reentry_cooldown_bars_after_trailing_stop", 24)
    profile.setdefault("reentry_cooldown_bars_after_whipsaw_stop_loss", 8)
    profile.setdefault("reentry_whipsaw_hard_stop_max_bars", 4)
    profile.setdefault("reentry_loss_cluster_window_bars", 30)
    profile.setdefault("reentry_cooldown_bars_after_loss_cluster", 20)
    profile.setdefault("reentry_cooldown_bars_after_weak_exit", 4)
    profile.setdefault("reentry_require_price_at_or_below_last_entry", 0.0)
    profile.setdefault("reentry_last_entry_tolerance_bps", 0.0)
    profile.setdefault("corridor_staged_mode_enabled", 0.0)
    profile.setdefault("corridor_staged_entry_1_pct", 10.0)
    profile.setdefault("corridor_staged_entry_2_pct", 20.0)
    profile.setdefault("corridor_staged_entry_3_pct", 30.0)
    profile.setdefault("corridor_staged_entry_4_pct", 40.0)
    profile.setdefault("corridor_staged_no_buy_above_pct", 50.0)
    profile.setdefault("corridor_staged_exit_step_pct", 10.0)
    profile.setdefault("corridor_staged_hysteresis_pct", 0.75)
    profile.setdefault("corridor_staged_exit_retrace_pct", 0.4)
    profile.setdefault("corridor_staged_entry_wait_bars", 6.0)
    profile.setdefault("corridor_staged_transition_smoothing_bars", 3.0)
    profile.setdefault("corridor_staged_require_rising", 1.0)
    profile.setdefault("corridor_staged_profit_target_enabled", 1.0)
    profile.setdefault("corridor_staged_profit_target_base_pct", 0.73)
    profile.setdefault("corridor_staged_profit_target_min_pct", 0.30)
    profile.setdefault("corridor_staged_profit_target_max_pct", 1.60)
    profile.setdefault("corridor_staged_profit_target_mult_10", 1.25)
    profile.setdefault("corridor_staged_profit_target_mult_20", 1.10)
    profile.setdefault("corridor_staged_profit_target_mult_30", 1.00)
    profile.setdefault("corridor_staged_profit_target_mult_40", 0.90)
    profile.setdefault("corridor_staged_profit_target_mult_50", 0.80)
    profile.setdefault("corridor_staged_profit_auto_enabled", 1.0)
    profile.setdefault("corridor_staged_profit_auto_width_scale", 0.55)
    profile.setdefault("corridor_staged_profit_auto_width_offset", 0.02)
    profile.setdefault("corridor_staged_profit_auto_min_pct", 0.35)
    profile.setdefault("corridor_staged_profit_auto_max_pct", 1.40)
    profile.setdefault("corridor_window_bars", 10080.0)
    profile.setdefault("corridor_min_bars", 720.0)
    profile.setdefault("corridor_fast_window_bars", 1440.0)
    profile.setdefault("corridor_fast_min_bars", 240.0)
    profile.setdefault("corridor_fast_blend_weight", 0.0)
    profile.setdefault("corridor_robust_range_enabled", 1.0)
    profile.setdefault("corridor_robust_low_pct", 2.0)
    profile.setdefault("corridor_robust_high_pct", 98.0)
    profile.setdefault("corridor_short_horizon_entry_guard_enabled", 1.0)
    profile.setdefault("corridor_short_horizon_entry_window_bars", 1440.0)
    profile.setdefault("corridor_short_horizon_entry_min_bars", 720.0)
    profile.setdefault("corridor_short_horizon_no_buy_above_pct", 55.0)
    swing_buy_band, swing_sell_band = _sanitize_swing_bands(
        profile.get("swing_buy_band"),
        profile.get("swing_sell_band"),
    )
    profile["swing_buy_band"] = swing_buy_band
    profile["swing_sell_band"] = swing_sell_band
    return profile


def _selector_cache_max_age_sec() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_REFRESH_SEC", "60")
    try:
        return max(1.0, float(raw))
    except Exception:
        return 60.0


def _selector_scan_timeout_sec() -> float:
    # A full watch-pool scan can legitimately exceed 90s when the selector
    # blends short and long horizon corridor data per symbol.
    raw = os.environ.get("ROTATION_SELECTOR_SCAN_TIMEOUT_SEC", "210")
    try:
        return max(5.0, float(raw))
    except Exception:
        return 210.0


def _strategy_weight_overrides() -> tuple[dict[str, float], str]:
    enabled_strategies = _enabled_strategy_names()
    raw_values: dict[str, float] = {}
    saw_override = False
    default_weight = 1.0 / float(len(enabled_strategies))
    for strategy in enabled_strategies:
        raw = os.environ.get(f"ROTATION_STRATEGY_WEIGHT_{strategy.upper()}")
        if raw is None or str(raw).strip() == "":
            continue
        saw_override = True
        try:
            raw_values[strategy] = max(0.0, float(raw))
        except Exception:
            continue
    if not saw_override:
        return ({strategy: default_weight for strategy in enabled_strategies}, "profile_default")
    total = sum(raw_values.get(strategy, 0.0) for strategy in enabled_strategies)
    if total <= 0.0:
        return ({strategy: default_weight for strategy in enabled_strategies}, "env_invalid")
    normalized = {
        strategy: raw_values.get(strategy, 0.0) / total
        for strategy in enabled_strategies
    }
    return normalized, "env_override"


def _strategy_weight_multipliers() -> tuple[dict[str, float], str]:
    enabled_strategies = _enabled_strategy_names()
    normalized, source = _strategy_weight_overrides()
    avg_weight = 1.0 / float(len(enabled_strategies))
    multipliers = {
        strategy: max(0.55, min(1.50, normalized.get(strategy, avg_weight) / avg_weight))
        for strategy in enabled_strategies
    }
    return multipliers, source


def _meta_risk_mode() -> str:
    return str(os.environ.get("ROTATION_META_RISK_MODE", "") or "").strip().lower()


def _split_symbol_list(raw: object) -> list[str]:
    values: list[str] = []
    for item in str(raw or "").split(","):
        symbol = str(item).strip().upper()
        if symbol and symbol not in values:
            values.append(symbol)
    return values


def _strategy_action_overrides() -> tuple[dict[str, dict[str, str | int | float]], str]:
    enabled_strategies = _enabled_strategy_names()
    overrides: dict[str, dict[str, str | int | float]] = {}
    saw_override = False
    for strategy in enabled_strategies:
        mode_raw = os.environ.get(f"ROTATION_STRATEGY_ACTION_{strategy.upper()}")
        slot_target_raw = os.environ.get(f"ROTATION_STRATEGY_SLOT_TARGET_{strategy.upper()}")
        if mode_raw is None and slot_target_raw is None:
            continue
        if (mode_raw is not None and str(mode_raw).strip() != "") or (
            slot_target_raw is not None and str(slot_target_raw).strip() != ""
        ):
            saw_override = True
        mode = str(mode_raw or "watch").strip().lower()
        if mode not in STRATEGY_ACTION_MODE_RANK:
            mode = "watch"
        try:
            slot_target = max(0, int(float(slot_target_raw or 0)))
        except Exception:
            slot_target = 0
        overrides[strategy] = {
            "mode": mode,
            "slot_target": slot_target,
            "mode_rank": STRATEGY_ACTION_MODE_RANK.get(mode, 1),
            "mode_mult": STRATEGY_ACTION_MODE_MULTIPLIER.get(mode, 0.75),
        }
    if not saw_override:
        return {}, "profile_default"
    for strategy in enabled_strategies:
        overrides.setdefault(
            strategy,
            {
                "mode": "watch",
                "slot_target": 0,
                "mode_rank": STRATEGY_ACTION_MODE_RANK["watch"],
                "mode_mult": STRATEGY_ACTION_MODE_MULTIPLIER["watch"],
            },
        )
    return overrides, "env_override"


def _meta_symbol_overrides() -> tuple[set[str], dict[str, list[str]], set[str], str]:
    enabled_strategies = _enabled_strategy_names()
    candidate_overrides = set(_split_symbol_list(os.environ.get("ROTATION_META_CANDIDATE_OVERRIDES", "")))
    avoid_symbols = set(_split_symbol_list(os.environ.get("ROTATION_META_AVOID_SYMBOLS", "")))
    strategy_top_symbols: dict[str, list[str]] = {}
    saw_override = bool(candidate_overrides or avoid_symbols)
    for strategy in enabled_strategies:
        values = _split_symbol_list(os.environ.get(f"ROTATION_STRATEGY_TOP_SYMBOLS_{strategy.upper()}", ""))
        strategy_top_symbols[strategy] = values
        if values:
            saw_override = True
    return candidate_overrides, strategy_top_symbols, avoid_symbols, ("env_override" if saw_override else "profile_default")


def _selector_active_sticky_minutes() -> float:
    raw = os.environ.get("ROTATION_ACTIVE_STICKY_MINUTES", "10")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 10.0


def _selector_sticky_switch_margin_bonus() -> float:
    raw = os.environ.get("ROTATION_ACTIVE_STICKY_SWITCH_MARGIN_BONUS", "24")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 24.0


def _selector_strategy_switch_margin_bonus() -> float:
    raw = os.environ.get("ROTATION_ACTIVE_STRATEGY_SWITCH_MARGIN_BONUS", "18")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 18.0


def _selector_alpha_switch_margin_bonus() -> float:
    raw = os.environ.get("ROTATION_ACTIVE_ALPHA_SWITCH_MARGIN_BONUS", "18")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 18.0


def _sticky_switch_margin_bonus(
    symbol: str,
    previous_selected_since: dict[str, str],
    generated_at: datetime,
) -> float:
    sticky_minutes = _selector_active_sticky_minutes()
    if sticky_minutes <= 0.0:
        return 0.0
    since_ts = _parse_timestamp(previous_selected_since.get(symbol))
    if since_ts is None:
        return 0.0
    active_seconds = max(0.0, (generated_at - since_ts).total_seconds())
    sticky_seconds = sticky_minutes * 60.0
    if active_seconds >= sticky_seconds:
        return 0.0
    fade = 1.0 - (active_seconds / sticky_seconds)
    return _selector_sticky_switch_margin_bonus() * max(0.0, fade)


def _strategy_slot_plan_from_actions(top: int) -> list[str]:
    enabled_strategies = _enabled_strategy_names()
    strategy_order = {strategy: idx for idx, strategy in enumerate(enabled_strategies)}
    actions, action_source = _strategy_action_overrides()
    if action_source != "env_override":
        return []
    strategy_weights, _weight_source = _strategy_weight_overrides()
    active_rows: list[tuple[str, str, int, float]] = []
    for strategy in enabled_strategies:
        action = actions.get(strategy, {})
        mode = str(action.get("mode") or "watch").strip().lower()
        if mode == "pause":
            continue
        slot_target = max(0, int(action.get("slot_target") or 0))
        effective_weight = max(0.0, float(strategy_weights.get(strategy, 0.0))) * float(
            action.get("mode_mult") or 0.75
        )
        if slot_target <= 0 and mode in {"primary", "secondary"} and effective_weight > 0.0:
            slot_target = 1
        if slot_target > 0 or effective_weight > 0.0:
            active_rows.append((strategy, mode, slot_target, effective_weight))

    if not active_rows:
        return []

    plan: list[str] = []
    for strategy, mode, slot_target, effective_weight in sorted(
        active_rows,
        key=lambda item: (
            -item[2],
            -STRATEGY_ACTION_MODE_RANK.get(item[1], 1),
            -item[3],
            strategy_order.get(item[0], len(enabled_strategies)),
        ),
    ):
        del mode, effective_weight
        if slot_target > 0:
            plan.extend([strategy] * slot_target)

    weighted_cycle: list[str] = []
    for strategy, mode, _slot_target, effective_weight in sorted(
        active_rows,
        key=lambda item: (
            -STRATEGY_ACTION_MODE_RANK.get(item[1], 1),
            -item[3],
            strategy_order.get(item[0], len(enabled_strategies)),
        ),
    ):
        del mode
        repeats = max(1, int(round(max(0.05, effective_weight) * 10.0)))
        weighted_cycle.extend([strategy] * repeats)

    if not weighted_cycle:
        weighted_cycle = [item[0] for item in active_rows]

    idx = 0
    while len(plan) < max(1, int(top)) and weighted_cycle:
        plan.append(weighted_cycle[idx % len(weighted_cycle)])
        idx += 1
    return plan[: max(1, int(top))]


def _soft_breakout_hint(row: dict) -> bool:
    return (
        not bool(row.get("macro_down_context"))
        and _as_float(row.get("spread_bps"), 9999.0) <= 20.0
        and bool(row.get("up_structure"))
        and _as_float(row.get("pos_pct"), 100.0) <= 90.0
        and (
            _as_float(row.get("fast_impulse_score"), 0.0) >= 6.0
            or _as_float(row.get("rel60_bps"), 0.0) >= 35.0
            or _as_float(row.get("fast_ret_90s_bps"), 0.0) >= 6.0
        )
    )


def _soft_staircase_hint(row: dict) -> bool:
    return (
        not bool(row.get("macro_down_context"))
        and _as_float(row.get("spread_bps"), 9999.0) <= 20.0
        and bool(row.get("up_structure"))
        and _as_float(row.get("pos_24h_pct"), 100.0) <= 90.0
        and (
            _as_float(row.get("staircase_score"), 0.0) >= 70.0
            or _as_float(row.get("fast_staircase_score"), 0.0) >= 8.0
            or _as_float(row.get("staircase_positive_share"), 0.0) >= 0.62
        )
    )


def _soft_pullback_continuation_hint(row: dict) -> bool:
    return (
        _macro_watch_ok(row)
        and not bool(row.get("macro_down_context"))
        and bool(row.get("up_structure"))
        and _as_float(row.get("spread_bps"), 9999.0) <= 28.0
        and _as_float(row.get("pos_pct"), 100.0) <= 86.0
        and _as_float(row.get("geo_drawdown_from_peak_bps", row.get("structure_drawdown_bps", 0.0)), 0.0) >= 6.0
        and (
            bool(row.get("strong_continuation_context"))
            or bool(row.get("trend_ready"))
            or _as_float(row.get("score_trend"), 0.0) >= 125.0
        )
    )


def _soft_breakout_retest_hint(row: dict) -> bool:
    return (
        not bool(row.get("macro_down_context"))
        and bool(row.get("up_structure"))
        and _as_float(row.get("spread_bps"), 9999.0) <= 22.0
        and _as_float(row.get("fast_impulse_score"), 0.0) >= 8.0
        and _as_float(row.get("geo_drawdown_from_peak_bps", row.get("structure_drawdown_bps", 0.0)), 0.0) >= 6.0
        and _as_float(row.get("pos_pct"), 100.0) <= 90.0
    )


def _soft_continuation_hint(row: dict) -> bool:
    return (
        _macro_watch_ok(row)
        and _as_float(row.get("spread_bps"), 9999.0) <= 24.0
        and bool(row.get("up_structure"))
        and (
            _as_float(row.get("score_trend"), 0.0) >= 80.0
            or bool(row.get("strong_continuation_context"))
            or bool(row.get("trend_ready"))
        )
    )


def _soft_relative_strength_hint(row: dict) -> bool:
    return (
        not bool(row.get("macro_down_context"))
        and bool(row.get("up_structure"))
        and _as_float(row.get("spread_bps"), 9999.0) <= 18.0
        and _as_float(row.get("net24_pct"), 0.0) >= 0.75
        and _as_float(row.get("rel60_bps"), 0.0) >= 25.0
    )


def _soft_rebound_hint(row: dict) -> bool:
    return (
        not bool(row.get("still_dumping"))
        and _as_float(row.get("spread_bps"), 9999.0) <= 20.0
        and (
            bool(row.get("recent_rebound_ready"))
            or bool(row.get("post_dump_recovery_ready"))
            or bool(row.get("bottom_candidate"))
            or _is_early_bottom_reversal_candidate(row)
        )
    )


def _is_early_bottom_reversal_candidate(row: dict) -> bool:
    if bool(row.get("still_dumping")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 12.0:
        return False
    if str(row.get("structure_phase", "") or "").strip().lower() != "bottom":
        return False
    if str(row.get("active_leg", "") or "").strip().lower() != "rise":
        return False
    if not bool(row.get("up_structure")) or bool(row.get("down_structure")):
        return False
    if not bool(row.get("previous_selloff")):
        return False
    if not bool(row.get("recent_rebound_ready")):
        return False
    if not bool(row.get("base_ready")) or not bool(row.get("higher_low_ready")):
        return False
    if _as_float(row.get("pos_24h_pct"), _as_float(row.get("pos_pct"), 100.0)) > 28.0:
        return False
    if _as_float(row.get("pos_pct"), 100.0) > 40.0:
        return False
    if _as_float(row.get("bars_since_30m_low"), 9999.0) > 4.0:
        return False
    if _as_float(row.get("bars_since_swing_low"), 9999.0) > 5.0:
        return False
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    if rebound30 < 10.0 or rebound30 > 32.0:
        return False
    if _as_float(row.get("ret15_bps"), 0.0) < 8.0:
        return False
    if _as_float(row.get("rel15_bps"), 0.0) < 1.5:
        return False
    if _as_float(row.get("ret60_bps"), 0.0) < -45.0:
        return False
    if _as_float(row.get("ret120_bps"), 0.0) < -90.0:
        return False
    if _as_float(row.get("geo_drawdown_from_peak_bps"), 0.0) > 95.0:
        return False
    return True


def _is_controlled_bottom_rebound_candidate(row: dict) -> bool:
    if bool(row.get("still_dumping")) or bool(row.get("rebound_in_downtrend")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 16.0:
        return False
    if not bool(row.get("bottom_zone")) or not bool(row.get("in_valley_context")):
        return False
    if _as_float(row.get("pos_24h_pct"), _as_float(row.get("pos_pct"), 100.0)) > 32.0:
        return False
    structure_phase = str(row.get("structure_phase", "") or "").strip().lower()
    if structure_phase not in {"bottom", "lift_off"}:
        return False
    if _as_float(row.get("structure_confidence"), 0.0) < 0.65:
        return False
    if min(
        _as_float(row.get("bars_since_30m_low"), 9999.0),
        _as_float(row.get("bars_since_swing_low"), 9999.0),
    ) > 12.0:
        return False
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    rebound60 = _as_float(row.get("rebound_from_60m_low_bps"), 0.0)
    if rebound30 < 35.0 and rebound60 < 90.0:
        return False
    return bool(row.get("previous_selloff")) or rebound60 >= 110.0


def _allow_guarded_rebound_live(row: dict, profile_name: str) -> bool:
    profile_name = str(profile_name or "").strip().lower()
    if profile_name not in GUARDED_REBOUND_PROFILES:
        return False
    if bool(row.get("keep_open")):
        return True
    early_bottom_reversal = _is_early_bottom_reversal_candidate(row)
    gate_reason = str(row.get("gate_reason", "") or "").strip().lower()
    if gate_reason in {"falling_now", "still_dumping"}:
        return False
    if gate_reason in {"macro_downtrend", "macro_down_context"} and not early_bottom_reversal:
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 16.0:
        return False
    if early_bottom_reversal:
        return True
    if bool(row.get("recent_rebound_ready")) or bool(row.get("post_dump_recovery_ready")):
        return True
    if bool(row.get("bottom_candidate")) and _as_float(row.get("pos_pct"), 100.0) <= 35.0:
        return True
    return _is_controlled_bottom_rebound_candidate(row)


def _inject_guarded_rebound_slot(plan: list[str], profile_name: str, top: int) -> list[str]:
    profile_name = str(profile_name or "").strip().lower()
    top = max(1, int(top))
    if profile_name not in GUARDED_REBOUND_PROFILES:
        return plan[:top]
    trimmed = list(plan[:top])
    if not trimmed:
        return trimmed
    if "rebound" in trimmed:
        return trimmed
    if len(trimmed) < top:
        trimmed.append("rebound")
        return trimmed[:top]
    for idx in range(len(trimmed) - 1, -1, -1):
        if trimmed[idx] in {"continuation", "pullback_continuation", "breakout_retest"}:
            trimmed[idx] = "rebound"
            return trimmed
    trimmed[-1] = "rebound"
    return trimmed


def _load_selector_cache(max_age_sec: float) -> dict | None:
    if not SELECTOR_CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(SELECTOR_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    cached_at_raw = payload.get("cached_at")
    if cached_at_raw is None:
        return None
    try:
        cached_at = datetime.fromisoformat(str(cached_at_raw))
    except Exception:
        return None
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    age_sec = max(0.0, (datetime.now(timezone.utc) - cached_at).total_seconds())
    if age_sec > max(1.0, float(max_age_sec)):
        return None
    cached = dict(payload.get("result") or {})
    if not isinstance(cached, dict):
        return None
    cached["row_source"] = f"{str(cached.get('row_source', 'live'))}:cache"
    cached["cache_age_sec"] = age_sec
    cached["cached_result_used"] = True
    return cached


def _write_selector_cache(result: dict) -> None:
    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    SELECTOR_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_selector() -> dict:
    refresh_sec = _selector_cache_max_age_sec()
    cached = _load_selector_cache(refresh_sec)
    if cached is not None:
        return cached
    scan_timeout = _selector_scan_timeout_sec()
    try:
        out = subprocess.check_output(
            ["python3", "scripts/select_rotation_watchlist.py"],
            cwd=REPO_ROOT,
            text=True,
            timeout=scan_timeout,
        )
        result = json.loads(out)
        if isinstance(result, dict):
            result.setdefault("row_source", "live")
            _write_selector_cache(result)
        return result
    except Exception as exc:
        stale_cached = _load_selector_cache(86400.0)
        if stale_cached is not None:
            stale_cached["fallback_rows_used"] = True
            stale_cached["fallback_reason"] = "selector_error_using_cache"
            stale_cached["errors_sample"] = [str(exc)]
            return stale_cached
        return {
            "ok": False,
            "rows": [],
            "row_source": "error",
            "fallback_rows_used": True,
            "fallback_reason": "selector_error_no_cache",
            "errors_total": 1,
            "errors_sample": [str(exc)],
        }


def _excluded_base_symbols() -> set[str]:
    out = set(DEFAULT_EXCLUDED_BASE_SYMBOLS)
    raw = os.environ.get("ROTATION_EXCLUDED_BASE_SYMBOLS", "")
    for item in str(raw or "").split(","):
        symbol = str(item or "").strip().upper()
        if symbol:
            out.add(symbol)
    return out


def _is_excluded_base_symbol(symbol: str) -> bool:
    token = str(symbol or "").strip().upper()
    if not token:
        return True
    return token in _excluded_base_symbols()


def _env_symbols(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in str(raw).split(","):
        symbol = str(item or "").strip().upper()
        if (
            not symbol
            or symbol in seen
            or symbol not in PORTS
            or _is_excluded_base_symbol(symbol)
        ):
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _parse_timestamp(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _load_previous_payload() -> dict:
    if not ACTIVE_FILE.exists():
        return {}
    try:
        obj = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    return obj


def _load_previous_state(payload: dict | None = None) -> tuple[list[str], dict[str, str], list[str]]:
    obj = payload if isinstance(payload, dict) else _load_previous_payload()
    if not obj:
        return [], {}, []
    raw = obj.get("selected") or []
    selected = [
        str(x).upper()
        for x in raw
        if str(x).upper() in PORTS and not _is_excluded_base_symbol(str(x).upper())
    ]
    raw_watch = obj.get("watch_symbols")
    if not isinstance(raw_watch, list):
        raw_watch = list(raw)
    watch_symbols = [
        str(x).upper()
        for x in raw_watch
        if str(x).upper() in PORTS and not _is_excluded_base_symbol(str(x).upper())
    ]
    selected_since: dict[str, str] = {}
    raw_since = obj.get("selected_since") or {}
    if isinstance(raw_since, dict):
        for symbol, ts in raw_since.items():
            key = str(symbol).upper()
            if key not in PORTS:
                continue
            parsed = _parse_timestamp(ts)
            if parsed is None:
                continue
            selected_since[key] = parsed.isoformat()

    fallback_ts = _parse_timestamp(obj.get("generated_at"))
    if fallback_ts is not None:
        fallback_iso = fallback_ts.isoformat()
        for symbol in selected:
            selected_since.setdefault(symbol, fallback_iso)

    return selected, selected_since, watch_symbols


def _normalize_float_map(raw: object) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            out[str(key).upper()] = round(float(value or 0.0), 12)
        except Exception:
            continue
    return dict(sorted(out.items()))


def _normalize_profile_values(raw: object) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        try:
            out[str(key)] = round(float(value), 12)
        except Exception:
            continue
    return dict(sorted(out.items()))


def _apply_signature(payload: dict | None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    return {
        "selected": [
            str(x).upper()
            for x in payload.get("selected", [])
            if str(x).upper() in PORTS and not _is_excluded_base_symbol(str(x).upper())
        ],
        "watch_symbols": [
            str(x).upper()
            for x in payload.get("watch_symbols", [])
            if str(x).upper() in PORTS and not _is_excluded_base_symbol(str(x).upper())
        ],
        "fraction": round(float(payload.get("fraction", 0.0) or 0.0), 12),
        "profile": str(payload.get("profile", "") or ""),
        "selected_fraction_map": _normalize_float_map(payload.get("selected_fraction_map") or {}),
        "selected_strategy_map": {
            str(key).upper(): str(value).strip().lower()
            for key, value in (payload.get("selected_strategy_map") or {}).items()
            if str(key).strip() and str(value).strip()
        },
        "selected_alpha_map": {
            str(key).upper(): str(value).strip().lower()
            for key, value in (payload.get("selected_alpha_map") or {}).items()
            if str(key).strip() and str(value).strip()
        },
        "profile_values": _normalize_profile_values(payload.get("profile_values") or {}),
        "runtime_config_version": int(payload.get("runtime_config_version", 0) or 0),
    }


def _profile_signature_changed(previous_payload: dict | None, current_payload: dict | None) -> bool:
    previous_payload = previous_payload if isinstance(previous_payload, dict) else {}
    current_payload = current_payload if isinstance(current_payload, dict) else {}
    previous = _normalize_profile_values(previous_payload.get("profile_values") or {})
    current = _normalize_profile_values(current_payload.get("profile_values") or {})
    if previous or current:
        return previous != current
    return str(previous_payload.get("profile", "") or "") != str(current_payload.get("profile", "") or "")


def _load_previous_rows_snapshot() -> list[dict]:
    if not ACTIVE_FILE.exists():
        return []
    try:
        obj = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    rows = obj.get("all_rows")
    if not isinstance(rows, list):
        rows = obj.get("rows")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        out.append(item)
    return out


def _coin_experience_settings() -> dict[str, float | int | bool]:
    return {
        "enabled": _env_flag("ROTATION_COIN_EXPERIENCE_PRIOR_ENABLED", default=False),
        "lookback_days": max(
            1.0,
            _as_float(
                os.environ.get("ROTATION_COIN_EXPERIENCE_LOOKBACK_DAYS"),
                COIN_EXPERIENCE_DEFAULT_LOOKBACK_DAYS,
            ),
        ),
        "half_life_days": max(
            0.25,
            _as_float(
                os.environ.get("ROTATION_COIN_EXPERIENCE_HALF_LIFE_DAYS"),
                COIN_EXPERIENCE_DEFAULT_HALF_LIFE_DAYS,
            ),
        ),
        "min_trades": max(
            1,
            int(
                _as_float(
                    os.environ.get("ROTATION_COIN_EXPERIENCE_MIN_TRADES"),
                    COIN_EXPERIENCE_DEFAULT_MIN_TRADES,
                )
            ),
        ),
        "min_weighted_trades": max(
            0.1,
            _as_float(
                os.environ.get("ROTATION_COIN_EXPERIENCE_MIN_WEIGHTED_TRADES"),
                COIN_EXPERIENCE_DEFAULT_MIN_WEIGHTED_TRADES,
            ),
        ),
        "full_weight_trades": max(
            1.0,
            _as_float(
                os.environ.get("ROTATION_COIN_EXPERIENCE_FULL_WEIGHT_TRADES"),
                COIN_EXPERIENCE_DEFAULT_FULL_WEIGHT_TRADES,
            ),
        ),
        "max_abs_score": max(
            0.0,
            _as_float(
                os.environ.get("ROTATION_COIN_EXPERIENCE_MAX_ABS_SCORE"),
                COIN_EXPERIENCE_DEFAULT_MAX_ABS_SCORE,
            ),
        ),
        "min_abs_score": max(
            0.0,
            _as_float(
                os.environ.get("ROTATION_COIN_EXPERIENCE_MIN_ABS_SCORE"),
                COIN_EXPERIENCE_DEFAULT_MIN_ABS_SCORE,
            ),
        ),
    }


def _coin_experience_base_symbol(raw_symbol: object) -> str:
    symbol = str(raw_symbol or "").strip().upper().replace("/", "").replace("_", "").replace("-", "")
    if symbol.endswith("USDC") and len(symbol) > 4:
        symbol = symbol[:-4]
    return symbol


def _parse_trade_timestamp(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _build_coin_experience_priors_from_rows(
    trade_rows: list[dict],
    *,
    now: datetime,
    settings: dict[str, float | int | bool],
) -> tuple[dict[str, dict[str, float | int | bool | str]], dict[str, object]]:
    lookback_days = max(1.0, float(settings.get("lookback_days", COIN_EXPERIENCE_DEFAULT_LOOKBACK_DAYS) or 0.0))
    half_life_days = max(0.25, float(settings.get("half_life_days", COIN_EXPERIENCE_DEFAULT_HALF_LIFE_DAYS) or 0.0))
    min_trades = max(1, int(settings.get("min_trades", COIN_EXPERIENCE_DEFAULT_MIN_TRADES) or 1))
    min_weighted_trades = max(
        0.1,
        float(settings.get("min_weighted_trades", COIN_EXPERIENCE_DEFAULT_MIN_WEIGHTED_TRADES) or 0.0),
    )
    full_weight_trades = max(
        1.0,
        float(settings.get("full_weight_trades", COIN_EXPERIENCE_DEFAULT_FULL_WEIGHT_TRADES) or 0.0),
    )
    max_abs_score = max(
        0.0,
        float(settings.get("max_abs_score", COIN_EXPERIENCE_DEFAULT_MAX_ABS_SCORE) or 0.0),
    )
    min_abs_score = max(
        0.0,
        float(settings.get("min_abs_score", COIN_EXPERIENCE_DEFAULT_MIN_ABS_SCORE) or 0.0),
    )
    now_utc = now.astimezone(timezone.utc)
    stats: dict[str, dict[str, float | int | str]] = {}
    skipped_old = 0
    skipped_invalid = 0

    for row in trade_rows:
        if not isinstance(row, dict) or not bool(row.get("closed", True)):
            continue
        symbol = _coin_experience_base_symbol(row.get("symbol"))
        if not symbol or symbol not in PORTS:
            continue
        sell_ts = _parse_trade_timestamp(row.get("sellTime"))
        if sell_ts is None:
            skipped_invalid += 1
            continue
        age_days = max(0.0, (now_utc - sell_ts).total_seconds() / 86400.0)
        if age_days > lookback_days:
            skipped_old += 1
            continue
        pnl = _as_float(row.get("proceedsUsdc"), 0.0)
        buy_gross = max(1e-9, _as_float(row.get("buyGrossUsdc"), 0.0))
        pnl_bps = max(-700.0, min(700.0, (pnl / buy_gross) * 10000.0))
        weight = 0.5 ** (age_days / half_life_days)
        item = stats.setdefault(
            symbol,
            {
                "raw_trade_count": 0,
                "weighted_trade_count": 0.0,
                "weighted_win_count": 0.0,
                "weighted_pnl_usdc": 0.0,
                "weighted_pnl_bps": 0.0,
                "weighted_gross_profit_usdc": 0.0,
                "weighted_gross_loss_usdc": 0.0,
                "last_sell_time": "",
            },
        )
        item["raw_trade_count"] = int(item["raw_trade_count"]) + 1
        item["weighted_trade_count"] = float(item["weighted_trade_count"]) + weight
        item["weighted_pnl_usdc"] = float(item["weighted_pnl_usdc"]) + (pnl * weight)
        item["weighted_pnl_bps"] = float(item["weighted_pnl_bps"]) + (pnl_bps * weight)
        if pnl > 0.0:
            item["weighted_win_count"] = float(item["weighted_win_count"]) + weight
            item["weighted_gross_profit_usdc"] = float(item["weighted_gross_profit_usdc"]) + (pnl * weight)
        elif pnl < 0.0:
            item["weighted_gross_loss_usdc"] = float(item["weighted_gross_loss_usdc"]) + (pnl * weight)
        last_sell = str(item.get("last_sell_time", "") or "")
        sell_iso = sell_ts.isoformat()
        if not last_sell or sell_iso > last_sell:
            item["last_sell_time"] = sell_iso

    priors: dict[str, dict[str, float | int | bool | str]] = {}
    applied_symbols = 0
    neutral_symbols = 0
    for symbol, item in stats.items():
        raw_count = int(item.get("raw_trade_count", 0) or 0)
        weighted_count = float(item.get("weighted_trade_count", 0.0) or 0.0)
        if weighted_count <= 0.0:
            continue
        win_rate = float(item.get("weighted_win_count", 0.0) or 0.0) / weighted_count
        expectancy_bps = float(item.get("weighted_pnl_bps", 0.0) or 0.0) / weighted_count
        expectancy_usdc = float(item.get("weighted_pnl_usdc", 0.0) or 0.0) / weighted_count
        gross_profit = float(item.get("weighted_gross_profit_usdc", 0.0) or 0.0)
        gross_loss = float(item.get("weighted_gross_loss_usdc", 0.0) or 0.0)
        if gross_profit > 0.0 and gross_loss < 0.0:
            profit_factor = gross_profit / abs(gross_loss)
        elif gross_profit > 0.0:
            profit_factor = 999.0
        else:
            profit_factor = 0.0
        sample_ok = raw_count >= min_trades and weighted_count >= min_weighted_trades
        score = 0.0
        if sample_ok and max_abs_score > 0.0:
            pf_for_score = max(0.25, min(4.0, profit_factor if profit_factor > 0.0 else 0.25))
            score = expectancy_bps * 0.45
            score += (win_rate - 0.52) * 32.0
            score += math.log(pf_for_score, 2.0) * 7.0
            sample_confidence = min(1.0, weighted_count / full_weight_trades)
            score *= sample_confidence
            score = max(-max_abs_score, min(max_abs_score, score))
            if abs(score) < min_abs_score:
                score = 0.0
        if abs(score) > 0.0:
            applied_symbols += 1
        else:
            neutral_symbols += 1
        priors[symbol] = {
            "score": round(score, 6),
            "sample_ok": bool(sample_ok),
            "raw_trade_count": raw_count,
            "weighted_trade_count": round(weighted_count, 6),
            "win_rate": round(win_rate, 6),
            "expectancy_bps": round(expectancy_bps, 6),
            "expectancy_usdc": round(expectancy_usdc, 8),
            "profit_factor": round(profit_factor, 6) if profit_factor < 999.0 else 999.0,
            "last_sell_time": str(item.get("last_sell_time", "") or ""),
        }

    info = {
        "lookback_days": lookback_days,
        "half_life_days": half_life_days,
        "min_trades": min_trades,
        "min_weighted_trades": min_weighted_trades,
        "full_weight_trades": full_weight_trades,
        "max_abs_score": max_abs_score,
        "min_abs_score": min_abs_score,
        "trade_rows_seen": len(trade_rows),
        "symbols_seen": len(stats),
        "symbols_scored": applied_symbols,
        "symbols_neutral": neutral_symbols,
        "skipped_old_rows": skipped_old,
        "skipped_invalid_rows": skipped_invalid,
    }
    return priors, info


def _load_coin_experience_priors(
    *,
    now: datetime,
    quote_asset: str = "USDC",
) -> tuple[dict[str, dict[str, float | int | bool | str]], dict[str, object]]:
    settings = _coin_experience_settings()
    info: dict[str, object] = dict(settings)
    if not bool(settings.get("enabled", False)):
        info.update({"status": "disabled", "symbols_scored": 0})
        return {}, info
    if str(quote_asset or "").strip().upper() != "USDC":
        info.update({"status": "unsupported_quote_asset", "symbols_scored": 0})
        return {}, info
    now_utc = now.astimezone(timezone.utc)
    lookback_days = float(settings.get("lookback_days", COIN_EXPERIENCE_DEFAULT_LOOKBACK_DAYS) or 0.0)
    from_dt = now_utc - timedelta(days=max(1.0, lookback_days))
    from_iso = from_dt.isoformat().replace("+00:00", "Z")
    to_iso = now_utc.isoformat().replace("+00:00", "Z")
    info.update({"from_iso": from_iso, "to_iso": to_iso})
    try:
        from trading.binance.trade_mirror import collect_trades_mirror

        report = collect_trades_mirror(from_iso, to_iso)
        trade_rows = report.get("tradeRows") if isinstance(report, dict) else []
        if not isinstance(trade_rows, list):
            trade_rows = []
        priors, build_info = _build_coin_experience_priors_from_rows(
            [row for row in trade_rows if isinstance(row, dict)],
            now=now_utc,
            settings=settings,
        )
        info.update(build_info)
        info.update({"status": "ok", "source": "binance_trade_mirror"})
        return priors, info
    except Exception as exc:
        info.update(
            {
                "status": "error",
                "error": str(exc).strip() or repr(exc),
                "symbols_scored": 0,
            }
        )
        return {}, info


def _apply_coin_experience_priors(
    rows: list[dict],
    priors: dict[str, dict[str, float | int | bool | str]],
    *,
    enabled: bool,
) -> None:
    for row in rows:
        symbol = _coin_experience_base_symbol(row.get("symbol"))
        prior = priors.get(symbol, {})
        score = _as_float(prior.get("score"), 0.0)
        row["coin_experience_prior_enabled"] = bool(enabled)
        row["coin_experience_score"] = round(score, 6)
        row["coin_experience_sample_ok"] = bool(prior.get("sample_ok", False))
        row["coin_experience_trade_count"] = int(prior.get("raw_trade_count", 0) or 0)
        row["coin_experience_weighted_trade_count"] = round(
            _as_float(prior.get("weighted_trade_count"), 0.0),
            6,
        )
        row["coin_experience_win_rate"] = round(_as_float(prior.get("win_rate"), 0.0), 6)
        row["coin_experience_expectancy_bps"] = round(
            _as_float(prior.get("expectancy_bps"), 0.0),
            6,
        )
        row["coin_experience_expectancy_usdc"] = round(
            _as_float(prior.get("expectancy_usdc"), 0.0),
            8,
        )
        row["coin_experience_profit_factor"] = round(
            _as_float(prior.get("profit_factor"), 0.0),
            6,
        )
        row["coin_experience_last_sell_time"] = str(prior.get("last_sell_time", "") or "")
        if not enabled or abs(score) <= 0.0:
            continue
        strategy_scores = row.get("strategy_scores")
        if not isinstance(strategy_scores, dict) or not strategy_scores:
            continue
        existing_ordered = sorted(
            (
                (str(strategy), _as_float(value, 0.0))
                for strategy, value in strategy_scores.items()
                if _as_float(value, 0.0) > 0.0
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if not existing_ordered:
            continue
        row["score"] = round(_as_float(row.get("score"), 0.0) + score, 6)
        existing_base_meta = existing_ordered[0][1]
        if len(existing_ordered) > 1:
            existing_base_meta += 0.18 * sum(value for _, value in existing_ordered[1:])
        meta_extra = _candidate_meta_score(row) - existing_base_meta
        adjusted_scores = {
            str(strategy): round(max(0.0, _as_float(value, 0.0) + score), 6)
            for strategy, value in strategy_scores.items()
        }
        adjusted_scores = {
            strategy: value
            for strategy, value in adjusted_scores.items()
            if value > 0.0
        }
        row["strategy_scores"] = adjusted_scores
        ordered = sorted(adjusted_scores.items(), key=lambda item: item[1], reverse=True)
        row["strategy_tags"] = [strategy for strategy, _ in ordered]
        row["strategy_primary"] = ordered[0][0] if ordered else ""
        row["strategy_primary_score"] = round(ordered[0][1], 6) if ordered else 0.0
        if ordered:
            meta_score = ordered[0][1]
            if len(ordered) > 1:
                meta_score += 0.18 * sum(value for _, value in ordered[1:])
            meta_score += meta_extra
        else:
            meta_score = min(0.0, _candidate_meta_score(row) + score)
        row["strategy_meta_score"] = round(meta_score, 6)


def _tail_lines_from_end(
    path: Path,
    lines: int,
    *,
    chunk_size: int = 8192,
    max_bytes: int = FAST_SCOUT_MAX_BYTES,
) -> list[str]:
    lines = max(0, int(lines))
    if lines <= 0 or not path.exists():
        return []
    collected: list[bytes] = []
    pending = b""
    total_read = 0
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        while pos > 0 and len(collected) < lines and total_read < max_bytes:
            to_read = min(chunk_size, pos)
            pos -= to_read
            fh.seek(pos)
            chunk = fh.read(to_read)
            total_read += len(chunk)
            data = chunk + pending
            parts = data.split(b"\n")
            pending = parts[0]
            for part in reversed(parts[1:]):
                if part.strip():
                    collected.append(part)
                if len(collected) >= lines:
                    break
        if len(collected) < lines and pending.strip():
            collected.append(pending)
    collected = list(reversed(collected[:lines]))
    return [raw.decode("utf-8", errors="replace") for raw in collected]


def _load_recent_market_events(symbol: str, max_events: int = 256) -> list[dict]:
    slug = str(symbol or "").strip().lower()
    if not slug:
        return []
    path = REPO_ROOT / "logs" / f"journal_live_binance_{slug}_usdc_rotation.jsonl"
    events: list[dict] = []
    for raw in _tail_lines_from_end(path, FAST_SCOUT_LINE_LIMIT):
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if str(item.get("event_type", "")) != "market":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item.get("data")
        if not isinstance(payload, dict):
            continue
        ts = _parse_timestamp(payload.get("ts") or item.get("ts"))
        if ts is None:
            continue
        micro = payload.get("micro") if isinstance(payload.get("micro"), dict) else {}
        try:
            events.append(
                {
                    "ts": ts,
                    "open": float(payload.get("open", 0.0) or 0.0),
                    "high": float(payload.get("high", 0.0) or 0.0),
                    "low": float(payload.get("low", 0.0) or 0.0),
                    "close": float(payload.get("close", 0.0) or 0.0),
                    "volume": float(payload.get("volume", 0.0) or 0.0),
                    "spread_bps": float(micro.get("spread_bps", 0.0) or 0.0),
                    "depth": float(micro.get("depth", 0.0) or 0.0),
                    "imbalance": float(micro.get("imbalance", 0.0) or 0.0),
                }
            )
        except Exception:
            continue
    if len(events) > max_events:
        events = events[-max_events:]
    return events


def _load_recent_core_decisions(symbol: str, max_events: int = 16) -> list[dict]:
    slug = str(symbol or "").strip().lower()
    if not slug:
        return []
    path = REPO_ROOT / "logs" / f"journal_live_binance_{slug}_usdc_rotation.jsonl"
    events: list[dict] = []
    for raw in _tail_lines_from_end(
        path,
        LIVE_DECISION_LINE_LIMIT,
        max_bytes=LIVE_DECISION_MAX_BYTES,
    ):
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if str(item.get("event_type", "")) != "core_decision":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item.get("data")
        if not isinstance(payload, dict):
            continue
        ts = _parse_timestamp(payload.get("ts") or item.get("ts"))
        if ts is None:
            continue
        gate = payload.get("gate") if isinstance(payload.get("gate"), dict) else {}
        risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
        alpha = payload.get("alpha") if isinstance(payload.get("alpha"), dict) else {}
        alpha_meta = alpha.get("meta") if isinstance(alpha.get("meta"), dict) else {}
        events.append(
            {
                "ts": ts,
                "trading_enabled": bool(payload.get("trading_enabled", True)),
                "trading_disable_reason": str(payload.get("trading_disable_reason", "") or ""),
                "gate_allow": bool(gate.get("allow")),
                "gate_reason": str(gate.get("reason", "") or "").strip().lower(),
                "risk_reason": str(risk.get("reason", "") or "").strip().lower(),
                "edge_bps": _as_float(alpha.get("edge_bps_effective", alpha.get("edge_bps", 0.0)), 0.0),
                "expected_cost_bps": _as_float(
                    (payload.get("cost") or {}).get("expected_cost_bps"),
                    0.0,
                ),
                "alpha_type": str(payload.get("alpha_type", "") or ""),
                "alpha_meta": alpha_meta,
            }
        )
    if len(events) > max_events:
        events = events[-max_events:]
    return events


def _live_selection_block_reason(decision: dict) -> str:
    risk_reason = str(decision.get("risk_reason", "") or "").strip().lower()
    gate_reason = str(decision.get("gate_reason", "") or "").strip().lower()
    if risk_reason in LIVE_DECISION_DIRECT_BLOCK_REASONS:
        return risk_reason
    if risk_reason == "gate_block" and gate_reason in LIVE_DECISION_GATE_BLOCK_REASONS:
        return gate_reason
    return ""


def _is_pause_decision(decision: dict) -> bool:
    return (
        not bool(decision.get("trading_enabled", True))
        and str(decision.get("trading_disable_reason", "") or "").strip().lower() == "pause"
    )


def _recent_live_decision_feedback(symbol: str) -> dict:
    decisions = _load_recent_core_decisions(symbol)
    if not decisions:
        return {}
    last = decisions[-1]
    now_dt = datetime.now(timezone.utc)
    age_sec = max(0.0, (now_dt - last["ts"]).total_seconds())
    recent = [
        item
        for item in decisions
        if max(0.0, (last["ts"] - item["ts"]).total_seconds()) <= LIVE_DECISION_BLOCK_WINDOW_SEC
    ]
    recent_feedback = [item for item in recent if not _is_pause_decision(item)]
    feedback_last = recent_feedback[-1] if recent_feedback else None
    block_reasons = [reason for reason in (_live_selection_block_reason(item) for item in recent_feedback) if reason]
    recent_gate_block_count = len(block_reasons)
    range_too_small_count = sum(
        1
        for item in recent_feedback
        if str((item.get("alpha_meta") or {}).get("swing_state", "") or "").strip().lower() == "range_too_small"
    )
    last_block_reason = _live_selection_block_reason(feedback_last or {})
    recent_live_selection_block = False
    trading_disable_reason = str(last.get("trading_disable_reason", "") or "").strip().lower()
    feedback_age_sec = (
        max(0.0, (now_dt - feedback_last["ts"]).total_seconds()) if feedback_last is not None else float("inf")
    )
    if age_sec <= LIVE_DECISION_STALE_SEC:
        if trading_disable_reason and trading_disable_reason != "pause":
            recent_live_selection_block = True
        elif feedback_age_sec <= LIVE_DECISION_STALE_SEC and last_block_reason in LIVE_DECISION_DIRECT_BLOCK_REASONS:
            recent_live_selection_block = True
        elif feedback_age_sec <= LIVE_DECISION_STALE_SEC and recent_gate_block_count >= 2 and (
            last_block_reason in LIVE_DECISION_GATE_BLOCK_REASONS
            or range_too_small_count >= 2
        ):
            recent_live_selection_block = True
    return {
        "live_decision_age_sec": age_sec,
        "live_trading_enabled": bool(last.get("trading_enabled", True)),
        "live_trading_disable_reason": str(last.get("trading_disable_reason", "") or ""),
        "live_recent_gate_reason": str(last.get("gate_reason", "") or ""),
        "live_recent_risk_reason": str(last.get("risk_reason", "") or ""),
        "live_recent_alpha_type": str(last.get("alpha_type", "") or ""),
        "live_recent_edge_bps": _as_float(last.get("edge_bps"), 0.0),
        "live_recent_expected_cost_bps": _as_float(last.get("expected_cost_bps"), 0.0),
        "live_recent_block_reason": last_block_reason,
        "live_recent_gate_block_count": recent_gate_block_count,
        "live_recent_range_too_small_count": range_too_small_count,
        "recent_live_selection_block": recent_live_selection_block,
        "recent_live_selection_block_reason": (
            str(last.get("trading_disable_reason", "") or "trading_disabled")
            if trading_disable_reason and trading_disable_reason != "pause"
            else last_block_reason
        ),
    }


def _ret_bps(last_price: float, ref_price: float | None) -> float:
    if ref_price is None or ref_price <= 0.0 or last_price <= 0.0:
        return 0.0
    return ((last_price / ref_price) - 1.0) * 10000.0


def _price_at_or_before(events: list[dict], cutoff: datetime) -> float | None:
    candidates = [evt for evt in events if evt["ts"] <= cutoff and evt["close"] > 0.0]
    if candidates:
        return float(candidates[-1]["close"])
    if events:
        value = float(events[0]["close"])
        return value if value > 0.0 else None
    return None


def _sum_volume(events: list[dict], start: datetime, end: datetime) -> float:
    total = 0.0
    for evt in events:
        if start < evt["ts"] <= end:
            total += float(evt["volume"])
    return total


def _consecutive_up_closes(events: list[dict]) -> int:
    streak = 0
    for prev, cur in zip(reversed(events[:-1]), reversed(events[1:])):
        if float(cur["close"]) > float(prev["close"]):
            streak += 1
            continue
        break
    return streak


def _fast_scout_metrics(symbol: str) -> dict:
    if str(os.environ.get("ROTATION_FAST_SCOUT_ENABLED", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return {}
    events = _load_recent_market_events(symbol)
    if len(events) < 4:
        return {}
    now_dt = datetime.now(timezone.utc)
    last_evt = events[-1]
    age_sec = max(0.0, (now_dt - last_evt["ts"]).total_seconds())
    if age_sec > 45.0:
        return {
            "fast_data_stale": True,
            "fast_data_age_sec": age_sec,
        }

    last_close = float(last_evt["close"])
    ret15 = _ret_bps(last_close, _price_at_or_before(events, last_evt["ts"] - timedelta(seconds=15)))
    ret30 = _ret_bps(last_close, _price_at_or_before(events, last_evt["ts"] - timedelta(seconds=30)))
    ret60 = _ret_bps(last_close, _price_at_or_before(events, last_evt["ts"] - timedelta(seconds=60)))
    ret90 = _ret_bps(last_close, _price_at_or_before(events, last_evt["ts"] - timedelta(seconds=90)))

    recent_start = last_evt["ts"] - timedelta(seconds=30)
    prev_start = last_evt["ts"] - timedelta(seconds=60)
    recent30_volume = _sum_volume(events, recent_start, last_evt["ts"])
    prev30_volume = _sum_volume(events, prev_start, recent_start)
    volume_ratio = recent30_volume / max(1e-9, prev30_volume) if prev30_volume > 0.0 else (2.0 if recent30_volume > 0.0 else 0.0)

    last60 = [evt for evt in events if evt["ts"] >= (last_evt["ts"] - timedelta(seconds=60))]
    lows = [float(evt["low"]) for evt in last60 if float(evt["low"]) > 0.0]
    highs = [float(evt["high"]) for evt in last60 if float(evt["high"]) > 0.0]
    rebound60 = _ret_bps(last_close, min(lows) if lows else None)
    breakout60 = _ret_bps(last_close, max(highs[:-1]) if len(highs) > 1 else None)
    up_streak = _consecutive_up_closes(events)
    spread_bps = float(last_evt["spread_bps"])
    depth = float(last_evt["depth"])
    imbalance = float(last_evt["imbalance"])

    impulse_score = (
        max(0.0, ret15) * 1.6
        + max(0.0, ret30)
        + (0.5 * max(0.0, ret60))
        + (0.25 * max(0.0, ret90))
        + (0.4 * max(0.0, rebound60))
        + (6.0 * max(0.0, volume_ratio - 1.0))
        + (6.0 * max(0.0, imbalance))
        - (0.5 * max(0.0, spread_bps - 10.0))
    )
    fast_impulse = (
        spread_bps <= 18.0
        and depth >= 50.0
        and imbalance >= -0.35
        and up_streak >= 2
        and volume_ratio >= 1.10
        and (
            (ret15 >= 8.0 and ret30 >= 14.0)
            or (ret30 >= 18.0 and rebound60 >= 16.0)
            or (ret60 >= 28.0 and breakout60 >= 10.0)
        )
    )
    staircase_score = (
        max(0.0, ret30) * 0.8
        + max(0.0, ret60) * 1.0
        + max(0.0, ret90) * 0.5
        + (0.35 * max(0.0, rebound60))
        + (5.0 * max(0.0, volume_ratio - 0.85))
        + (4.0 * max(0.0, up_streak - 1))
        - (0.4 * max(0.0, spread_bps - 12.0))
    )
    fast_staircase = (
        spread_bps <= 18.0
        and depth >= 50.0
        and imbalance >= -0.40
        and up_streak >= 2
        and volume_ratio >= 0.85
        and ret30 >= 6.0
        and ret60 >= 18.0
        and ret90 >= 24.0
        and rebound60 >= 10.0
    )
    return {
        "fast_data_stale": False,
        "fast_data_age_sec": age_sec,
        "fast_market_points": len(events),
        "fast_ret_15s_bps": ret15,
        "fast_ret_30s_bps": ret30,
        "fast_ret_60s_bps": ret60,
        "fast_ret_90s_bps": ret90,
        "fast_rebound_60s_bps": rebound60,
        "fast_breakout_60s_bps": breakout60,
        "fast_volume_30s": recent30_volume,
        "fast_volume_prev_30s": prev30_volume,
        "fast_volume_ratio": volume_ratio,
        "fast_up_streak": up_streak,
        "fast_spread_bps": spread_bps,
        "fast_depth": depth,
        "fast_imbalance": imbalance,
        "fast_impulse_score": impulse_score,
        "fast_impulse": fast_impulse,
        "fast_staircase_score": staircase_score,
        "fast_staircase": fast_staircase,
    }


def _augment_rows_with_fast_scout(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        symbol = str(row.get("symbol", "")).upper()
        if symbol in PORTS:
            row.update(_fast_scout_metrics(symbol))
        out.append(row)
    return out


def _augment_rows_with_live_decisions(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        symbol = str(row.get("symbol", "")).upper()
        if symbol in PORTS:
            row.update(_recent_live_decision_feedback(symbol))
        out.append(row)
    return out


def _ensure_lane_config(symbol: str) -> Path:
    slug = symbol.lower()
    path = REPO_ROOT / "configs" / f"live_binance_{slug}_usdc_rotation.yaml"
    if path.exists():
        return path
    template_path = CONFIG_TEMPLATE
    if not template_path.exists():
        raise FileNotFoundError(f"missing lane config template: {template_path}")
    text = template_path.read_text(encoding="utf-8")
    text = text.replace("KAITO/USDC", f"{symbol}/USDC")
    text = text.replace(
        "journal_live_binance_kaito_usdc_rotation",
        f"journal_live_binance_{slug}_usdc_rotation",
    )
    text = text.replace("target=KAITO", f"target={symbol}")
    path.write_text(text, encoding="utf-8")
    return path


def _scaled_bars_for_interval(base_bars: int, interval_seconds: int, *, minimum: int = 1) -> int:
    interval_seconds = max(1, int(interval_seconds))
    scaled = round(float(base_bars) * (60.0 / float(interval_seconds)))
    return max(minimum, int(scaled))


def _set_fraction(
    symbol: str,
    fraction: float,
    profile: dict[str, float | int],
    row: dict | None = None,
) -> None:
    slug = symbol.lower()
    path = _ensure_lane_config(symbol)
    init_symbol = symbol.upper() == "INIT"
    md_interval_seconds = max(1, int(float(profile.get("md_interval_seconds", 60) or 60)))
    exec_reconcile_interval_sec = max(
        1.0, float(profile.get("exec_reconcile_interval_sec", 5.0) or 5.0)
    )
    profit_roll_exit_enabled = bool(int(float(profile.get("profit_roll_exit_enabled", 0.0) or 0.0)))
    profit_roll_arm_eur = max(0.0, float(profile.get("profit_roll_arm_eur", 0.0) or 0.0))
    profit_roll_retrace_eur = max(
        0.0, float(profile.get("profit_roll_retrace_eur", 0.0) or 0.0)
    )
    profit_roll_retrace_pct_raw = profile.get("profit_roll_retrace_pct", 50.0)
    profit_roll_retrace_pct = max(
        0.0,
        min(
            100.0,
            float(50.0 if profit_roll_retrace_pct_raw is None else profit_roll_retrace_pct_raw),
        ),
    )
    profit_roll_min_retrace_eur = max(
        0.0, float(profile.get("profit_roll_min_retrace_eur", 0.02) or 0.0)
    )
    profit_roll_min_keep_profit_bps = max(
        0.0, float(profile.get("profit_roll_min_keep_profit_bps", 2.0) or 0.0)
    )
    # Keep EUR/USDC on absolute roll mode with a practical retrace threshold.
    # The global selector profile still uses retrace=0.0 for legacy lanes.
    if symbol.upper() == "EUR" and profit_roll_arm_eur > 0.0 and profit_roll_retrace_eur <= 0.0:
        profit_roll_retrace_eur = 0.02
    cont_rebound_confirm_bars_base = int(profile["cont_rebound_confirm_bars"])
    cont_rebound_confirm_bars = (
        0
        if cont_rebound_confirm_bars_base <= 0
        else _scaled_bars_for_interval(cont_rebound_confirm_bars_base, md_interval_seconds, minimum=1)
    )
    cont_trend_min_bps = float(profile["cont_trend_min_bps"])
    cont_rebound_trigger_bps = float(profile["cont_rebound_trigger_bps"])
    cont_pullback_min_bps = float(profile["cont_pullback_min_bps"])
    cont_pullback_max_bps = float(profile["cont_pullback_max_bps"])
    cont_max_chase_bps = float(profile["cont_max_chase_bps"])
    cont_min_volume_z = float(profile["cont_min_volume_z"])
    cont_max_structure_range_pos = float(profile["cont_max_structure_range_pos"])
    cont_stall_recovery_max_range_pos = float(
        profile.get("cont_stall_recovery_max_range_pos", 0.86)
    )
    cont_range_continuation_max_range_pos = float(
        profile.get("cont_range_continuation_max_range_pos", 0.88)
    )
    cont_max_range_pos = float(
        profile["cont_init_max_range_pos"] if init_symbol else profile["cont_max_range_pos"]
    )
    cont_hard_block_above_range_pos = float(
        profile["cont_init_hard_block_above_range_pos"]
        if init_symbol
        else profile["cont_hard_block_above_range_pos"]
    )
    cont_hard_block_max_drawdown_bps = float(
        profile["cont_init_hard_block_max_drawdown_bps"]
        if init_symbol
        else profile["cont_hard_block_max_drawdown_bps"]
    )
    cont_impulse_min_ret_bps = float(profile.get("cont_impulse_min_ret_bps", -10.0))
    cont_impulse_min_volume_z = float(profile.get("cont_impulse_min_volume_z", -1.6))
    cont_impulse_max_context_range_pos = float(
        profile.get("cont_impulse_max_context_range_pos", 1.0)
    )
    cont_impulse_max_extension_bps = float(profile.get("cont_impulse_max_extension_bps", 0.0))
    cont_impulse_require_up_structure = int(profile.get("cont_impulse_require_up_structure", 0))
    cont_staircase_min_trend_bps = float(profile.get("cont_staircase_min_trend_bps", 0.0))
    cont_staircase_min_ret_bps = float(profile.get("cont_staircase_min_ret_bps", -12.0))
    cont_staircase_min_volume_z = float(profile.get("cont_staircase_min_volume_z", -999.0))
    cont_staircase_min_slope_medium_bps = float(
        profile.get("cont_staircase_min_slope_medium_bps", 0.0)
    )
    cont_staircase_min_slope_long_bps = float(
        profile.get("cont_staircase_min_slope_long_bps", 0.0)
    )
    cont_staircase_min_drawdown_from_peak_bps = float(
        profile.get("cont_staircase_min_drawdown_from_peak_bps", 0.0)
    )
    cont_staircase_max_drawdown_from_peak_bps = float(
        profile.get("cont_staircase_max_drawdown_from_peak_bps", 120.0)
    )
    cont_staircase_max_context_range_pos = float(
        profile.get("cont_staircase_max_context_range_pos", 1.0)
    )
    cont_staircase_max_spread_bps = float(
        profile.get("cont_staircase_max_spread_bps", 22.0)
    )
    cont_staircase_require_up_structure = int(
        profile.get("cont_staircase_require_up_structure", 0)
    )
    cont_early_liftoff_max_context_range_pos = float(
        profile.get("cont_early_liftoff_max_context_range_pos", -1.0)
    )
    cont_early_liftoff_max_structure_range_pos = float(
        profile.get("cont_early_liftoff_max_structure_range_pos", 0.55)
    )
    cont_early_liftoff_min_context_rebound_bps = float(
        profile.get("cont_early_liftoff_min_context_rebound_bps", -1.0)
    )
    cont_early_liftoff_max_spread_bps = float(
        profile.get("cont_early_liftoff_max_spread_bps", 14.0)
    )
    cont_early_liftoff_min_volume_z = float(
        profile.get("cont_early_liftoff_min_volume_z", -1.1)
    )
    cont_early_liftoff_min_ret_bps = float(
        profile.get("cont_early_liftoff_min_ret_bps", -1.0)
    )
    cont_early_liftoff_min_slope_short_bps = float(
        profile.get("cont_early_liftoff_min_slope_short_bps", 2.4)
    )
    cont_early_liftoff_min_slope_medium_bps = float(
        profile.get("cont_early_liftoff_min_slope_medium_bps", 2.4)
    )
    cont_early_liftoff_min_drawdown_from_peak_bps = float(
        profile.get("cont_early_liftoff_min_drawdown_from_peak_bps", 100.0)
    )
    cont_early_liftoff_min_trend_bps = float(
        profile.get("cont_early_liftoff_min_trend_bps", -260.0)
    )
    breakout_trigger_bps = float(profile.get("breakout_trigger_bps", 6.0))
    breakout_bottom_countertrend_block_max_context_range_pos = float(
        profile.get("breakout_bottom_countertrend_block_max_context_range_pos", 0.22)
    )
    breakout_bottom_countertrend_block_max_context_rebound_bps = float(
        profile.get("breakout_bottom_countertrend_block_max_context_rebound_bps", 140.0)
    )
    breakout_bottom_countertrend_block_max_trend_return_bps = float(
        profile.get("breakout_bottom_countertrend_block_max_trend_return_bps", -55.0)
    )
    breakout_top_zone_block_min_context_range_pos = float(
        profile.get("breakout_top_zone_block_min_context_range_pos", 0.98)
    )
    breakout_top_zone_block_max_context_rebound_bps = float(
        profile.get("breakout_top_zone_block_max_context_rebound_bps", 90.0)
    )
    breakout_top_zone_block_min_volume_z = float(
        profile.get("breakout_top_zone_block_min_volume_z", 0.4)
    )
    breakout_thin_rebound_block_context_range_pos = float(
        profile.get("breakout_thin_rebound_block_context_range_pos", 0.60)
    )
    breakout_thin_rebound_block_context_rebound_bps = float(
        profile.get("breakout_thin_rebound_block_context_rebound_bps", 400.0)
    )
    breakout_thin_rebound_block_min_spread_bps = float(
        profile.get("breakout_thin_rebound_block_min_spread_bps", 18.0)
    )
    breakout_mid_rebound_block_context_range_pos = float(
        profile.get("breakout_mid_rebound_block_context_range_pos", 0.48)
    )
    breakout_mid_rebound_block_context_rebound_bps = float(
        profile.get("breakout_mid_rebound_block_context_rebound_bps", 760.0)
    )
    breakout_mid_rebound_block_min_volume_z = float(
        profile.get("breakout_mid_rebound_block_min_volume_z", 0.0)
    )
    breakout_late_rebound_block_context_range_pos = float(
        profile.get("breakout_late_rebound_block_context_range_pos", 0.72)
    )
    breakout_late_rebound_block_context_rebound_bps = float(
        profile.get("breakout_late_rebound_block_context_rebound_bps", 1400.0)
    )
    breakout_late_rebound_block_min_volume_z = float(
        profile.get("breakout_late_rebound_block_min_volume_z", 0.35)
    )
    swing_reversal_threshold_bps = float(profile.get("swing_reversal_threshold_bps", 0.4))
    swing_lookback_bars = int(max(2.0, float(profile.get("swing_lookback_bars", 20.0))))
    swing_buy_band, swing_sell_band = _sanitize_swing_bands(
        profile.get("swing_buy_band", 0.42),
        profile.get("swing_sell_band", 0.67),
    )
    swing_momentum_lookback = int(max(1.0, float(profile.get("swing_momentum_lookback", 7.0))))
    swing_edge_scale = float(profile.get("swing_edge_scale", 1.1))
    swing_max_edge_bps = float(profile.get("swing_max_edge_bps", 155.0))
    corridor_staged_mode_enabled = bool(int(float(profile.get("corridor_staged_mode_enabled", 0.0) or 0.0)))
    corridor_staged_entry_1_pct = float(profile.get("corridor_staged_entry_1_pct", 10.0))
    corridor_staged_entry_2_pct = float(profile.get("corridor_staged_entry_2_pct", 20.0))
    corridor_staged_entry_3_pct = float(profile.get("corridor_staged_entry_3_pct", 30.0))
    corridor_staged_entry_4_pct = float(profile.get("corridor_staged_entry_4_pct", 40.0))
    corridor_staged_no_buy_above_pct = float(profile.get("corridor_staged_no_buy_above_pct", 50.0))
    corridor_staged_exit_step_pct = float(profile.get("corridor_staged_exit_step_pct", 10.0))
    corridor_staged_hysteresis_pct = float(profile.get("corridor_staged_hysteresis_pct", 0.75))
    corridor_staged_exit_retrace_pct = float(profile.get("corridor_staged_exit_retrace_pct", 0.4))
    corridor_staged_entry_wait_bars = int(
        max(0.0, float(profile.get("corridor_staged_entry_wait_bars", 6.0) or 6.0))
    )
    corridor_staged_transition_smoothing_bars = int(
        max(1.0, float(profile.get("corridor_staged_transition_smoothing_bars", 3.0) or 3.0))
    )
    corridor_staged_require_rising = bool(
        int(float(profile.get("corridor_staged_require_rising", 1.0) or 1.0))
    )
    corridor_staged_profit_target_enabled = bool(
        int(float(profile.get("corridor_staged_profit_target_enabled", 1.0) or 1.0))
    )
    corridor_staged_profit_target_base_pct = max(
        0.0, float(profile.get("corridor_staged_profit_target_base_pct", 0.73) or 0.73)
    )
    corridor_staged_profit_target_min_pct = max(
        0.0, float(profile.get("corridor_staged_profit_target_min_pct", 0.30) or 0.30)
    )
    corridor_staged_profit_target_max_pct = max(
        corridor_staged_profit_target_min_pct,
        float(profile.get("corridor_staged_profit_target_max_pct", 1.60) or 1.60),
    )
    corridor_staged_profit_target_mult_10 = max(
        0.1, float(profile.get("corridor_staged_profit_target_mult_10", 1.25) or 1.25)
    )
    corridor_staged_profit_target_mult_20 = max(
        0.1, float(profile.get("corridor_staged_profit_target_mult_20", 1.10) or 1.10)
    )
    corridor_staged_profit_target_mult_30 = max(
        0.1, float(profile.get("corridor_staged_profit_target_mult_30", 1.00) or 1.00)
    )
    corridor_staged_profit_target_mult_40 = max(
        0.1, float(profile.get("corridor_staged_profit_target_mult_40", 0.90) or 0.90)
    )
    corridor_staged_profit_target_mult_50 = max(
        0.1, float(profile.get("corridor_staged_profit_target_mult_50", 0.80) or 0.80)
    )
    corridor_staged_profit_auto_enabled = bool(
        int(float(profile.get("corridor_staged_profit_auto_enabled", 1.0) or 1.0))
    )
    corridor_staged_profit_auto_width_scale = max(
        0.0, float(profile.get("corridor_staged_profit_auto_width_scale", 0.55) or 0.55)
    )
    corridor_staged_profit_auto_width_offset = float(
        profile.get("corridor_staged_profit_auto_width_offset", 0.02) or 0.02
    )
    corridor_staged_profit_auto_min_pct = max(
        0.0, float(profile.get("corridor_staged_profit_auto_min_pct", 0.35) or 0.35)
    )
    corridor_staged_profit_auto_max_pct = max(
        corridor_staged_profit_auto_min_pct,
        float(profile.get("corridor_staged_profit_auto_max_pct", 1.40) or 1.40),
    )
    if corridor_staged_profit_auto_enabled:
        row_obj = row if isinstance(row, dict) else {}
        near_width_pct = float(
            row_obj.get("corridor_near_width_pct", row_obj.get("width72_pct", 0.0)) or 0.0
        )
        auto_base_pct = (
            (near_width_pct * corridor_staged_profit_auto_width_scale)
            + corridor_staged_profit_auto_width_offset
        )
        auto_base_pct = max(corridor_staged_profit_auto_min_pct, min(corridor_staged_profit_auto_max_pct, auto_base_pct))
        corridor_staged_profit_target_base_pct = auto_base_pct
    corridor_fast_blend_weight = max(
        0.0,
        min(1.0, float(profile.get("corridor_fast_blend_weight", 0.35))),
    )
    corridor_robust_range_enabled = bool(
        int(float(profile.get("corridor_robust_range_enabled", 1.0) or 1.0))
    )
    corridor_robust_low_pct = max(
        0.0,
        min(100.0, float(profile.get("corridor_robust_low_pct", 2.0) or 2.0)),
    )
    corridor_robust_high_pct = max(
        0.0,
        min(100.0, float(profile.get("corridor_robust_high_pct", 98.0) or 98.0)),
    )
    if corridor_robust_high_pct <= corridor_robust_low_pct:
        corridor_robust_high_pct = min(100.0, corridor_robust_low_pct + 1.0)
    alpha_type = str(profile.get("alpha_type", "continuation") or "continuation").strip().lower()
    if alpha_type not in {"momentum", "mean_reversion", "continuation", "breakout", "swing", "auto"}:
        alpha_type = "continuation"
    safety_exits_raw = profile.get("safety_exits_enabled", 1.0)
    if safety_exits_raw is None:
        safety_exits_raw = 1.0
    safety_exits_enabled = bool(int(float(safety_exits_raw)))
    require_break_even_raw = profile.get("require_break_even_for_exit", 1.0)
    if require_break_even_raw is None:
        require_break_even_raw = 1.0
    require_break_even_for_exit = bool(int(float(require_break_even_raw)))
    if not safety_exits_enabled:
        require_break_even_for_exit = False
    swing_min_range_bps = float(profile.get("swing_min_range_bps", 52.0))
    swing_micro_rebound_max_spread_bps = float(profile.get("swing_micro_rebound_max_spread_bps", 8.0))
    swing_micro_rebound_max_context_range_pos = float(
        profile.get("swing_micro_rebound_max_context_range_pos", 0.38)
    )
    swing_micro_rebound_min_context_rebound_bps = float(
        profile.get("swing_micro_rebound_min_context_rebound_bps", 0.0)
    )
    swing_micro_rebound_min_ret_bps = float(profile.get("swing_micro_rebound_min_ret_bps", -2.0))
    swing_micro_rebound_confirm_rebound_bps = float(
        profile.get("swing_micro_rebound_confirm_rebound_bps", 0.0)
    )
    swing_micro_rebound_confirm_min_ret_bps = float(
        profile.get("swing_micro_rebound_confirm_min_ret_bps", 0.0)
    )
    feature_return_window = _scaled_bars_for_interval(1, md_interval_seconds, minimum=1)
    feature_atr_window = _scaled_bars_for_interval(3, md_interval_seconds, minimum=3)
    feature_volume_z_window = _scaled_bars_for_interval(4, md_interval_seconds, minimum=4)
    feature_trend_window = _scaled_bars_for_interval(48, md_interval_seconds, minimum=48)
    feature_context_window = _scaled_bars_for_interval(
        int(max(48.0, float(profile.get("feature_context_window_bars", 576.0) or 576.0))),
        md_interval_seconds,
        minimum=48,
    )
    cont_lookback = _scaled_bars_for_interval(4, md_interval_seconds, minimum=4)
    cont_recent_bias_lookback = _scaled_bars_for_interval(4, md_interval_seconds, minimum=4)
    scaled_cooldown_bars = _scaled_bars_for_interval(int(profile["cooldown_bars"]), md_interval_seconds, minimum=1)
    scaled_min_hold_bars = _scaled_bars_for_interval(int(profile["min_hold_bars"]), md_interval_seconds, minimum=1)
    scaled_time_break_even_floor_bars = _scaled_bars_for_interval(
        int(profile["time_break_even_floor_bars"]),
        md_interval_seconds,
        minimum=1,
    )
    scaled_green_take_min_bars = _scaled_bars_for_interval(
        int(profile["green_candle_take_min_bars"]),
        md_interval_seconds,
        minimum=1,
    )
    green_take_max_bars = int(profile["green_candle_take_max_bars"])
    scaled_green_take_max_bars = (
        0
        if green_take_max_bars <= 0
        else _scaled_bars_for_interval(green_take_max_bars, md_interval_seconds, minimum=1)
    )
    scaled_failed_start_max_bars = _scaled_bars_for_interval(
        int(profile["failed_start_max_bars"]),
        md_interval_seconds,
        minimum=1,
    )
    scaled_failed_start_min_bars = (
        _scaled_bars_for_interval(int(profile["failed_start_min_bars"]), md_interval_seconds, minimum=1)
        if int(profile.get("failed_start_min_bars", 0)) > 0
        else 0
    )
    scaled_campaign_hold_min_bars = (
        _scaled_bars_for_interval(int(profile["campaign_hold_min_bars"]), md_interval_seconds, minimum=1)
        if int(profile["campaign_hold_min_bars"]) > 0
        else 0
    )
    scaled_chop_reclaim_min_bars = _scaled_bars_for_interval(50, md_interval_seconds, minimum=10)
    scaled_chop_reclaim_cross_window_bars = _scaled_bars_for_interval(30, md_interval_seconds, minimum=10)
    corridor_window_bars_base = int(max(24.0, float(profile.get("corridor_window_bars", 4320.0) or 4320.0)))
    corridor_min_bars_base = int(max(12.0, float(profile.get("corridor_min_bars", 720.0) or 720.0)))
    corridor_fast_window_bars_base = int(
        max(12.0, float(profile.get("corridor_fast_window_bars", 1440.0) or 1440.0))
    )
    corridor_fast_min_bars_base = int(
        max(6.0, float(profile.get("corridor_fast_min_bars", 240.0) or 240.0))
    )
    corridor_fast_min_bars_base = min(corridor_fast_min_bars_base, corridor_fast_window_bars_base)
    corridor_short_horizon_entry_guard_enabled = bool(
        int(float(profile.get("corridor_short_horizon_entry_guard_enabled", 1.0) or 1.0))
    )
    corridor_short_horizon_entry_window_bars_base = int(
        max(
            12.0,
            float(profile.get("corridor_short_horizon_entry_window_bars", 1440.0) or 1440.0),
        )
    )
    corridor_short_horizon_entry_min_bars_base = int(
        max(
            6.0,
            float(profile.get("corridor_short_horizon_entry_min_bars", 720.0) or 720.0),
        )
    )
    corridor_short_horizon_entry_min_bars_base = min(
        corridor_short_horizon_entry_min_bars_base,
        corridor_short_horizon_entry_window_bars_base,
    )
    corridor_short_horizon_no_buy_above_pct = max(
        0.0,
        min(
            100.0,
            float(profile.get("corridor_short_horizon_no_buy_above_pct", 55.0) or 55.0),
        ),
    )
    scaled_profit_corridor_window_bars = _scaled_bars_for_interval(
        corridor_window_bars_base,
        md_interval_seconds,
        minimum=max(24, corridor_window_bars_base),
    )
    scaled_profit_corridor_min_bars = _scaled_bars_for_interval(
        corridor_min_bars_base,
        md_interval_seconds,
        minimum=max(12, corridor_min_bars_base),
    )
    scaled_profit_corridor_fast_window_bars = _scaled_bars_for_interval(
        corridor_fast_window_bars_base,
        md_interval_seconds,
        minimum=max(12, corridor_fast_window_bars_base),
    )
    scaled_profit_corridor_fast_min_bars = _scaled_bars_for_interval(
        corridor_fast_min_bars_base,
        md_interval_seconds,
        minimum=max(6, corridor_fast_min_bars_base),
    )
    scaled_profit_corridor_short_horizon_entry_window_bars = _scaled_bars_for_interval(
        corridor_short_horizon_entry_window_bars_base,
        md_interval_seconds,
        minimum=max(12, corridor_short_horizon_entry_window_bars_base),
    )
    scaled_profit_corridor_short_horizon_entry_min_bars = _scaled_bars_for_interval(
        corridor_short_horizon_entry_min_bars_base,
        md_interval_seconds,
        minimum=max(6, corridor_short_horizon_entry_min_bars_base),
    )
    warmup_window_hours = max(72.0, float(corridor_window_bars_base) / 60.0)
    scaled_warmup_min_target = max(120, min(3000, corridor_min_bars_base))
    scaled_warmup_max_target = max(3000, corridor_window_bars_base)
    scaled_warmup_min_bars = _scaled_bars_for_interval(
        scaled_warmup_min_target,
        md_interval_seconds,
        minimum=scaled_warmup_min_target,
    )
    scaled_warmup_max_bars = _scaled_bars_for_interval(
        scaled_warmup_max_target,
        md_interval_seconds,
        minimum=scaled_warmup_max_target,
    )
    text = path.read_text(encoding="utf-8")
    lines = []
    section = ""
    in_warmup = False
    profit_roll_block_written = False
    in_alpha_continuation = False
    in_alpha_breakout = False
    in_alpha_swing = False
    in_alpha_auto = False
    in_alpha_auto_swing = False
    in_policy_profit_corridor = False
    for line in text.splitlines():
        if line and not line.startswith(" "):
            section = line.strip().rstrip(":")
            in_warmup = False
            in_alpha_continuation = False
            in_alpha_breakout = False
            in_alpha_swing = False
            in_alpha_auto = False
            in_alpha_auto_swing = False
            in_policy_profit_corridor = False
        elif section != "core":
            in_warmup = False

        stripped = line.strip()
        if section != "alpha":
            in_alpha_continuation = False
            in_alpha_breakout = False
            in_alpha_swing = False
            in_alpha_auto = False
            in_alpha_auto_swing = False
        elif line.startswith("  ") and not line.startswith("    "):
            in_alpha_auto = stripped == "auto:"
            in_alpha_auto_swing = False
        elif in_alpha_auto and line.startswith("    ") and not line.startswith("      "):
            in_alpha_auto_swing = stripped == "swing:"
        if section == "policy":
            if line.startswith("  ") and not line.startswith("    "):
                in_policy_profit_corridor = stripped == "profit_corridor:"
        else:
            in_policy_profit_corridor = False

        # Normalize alpha.continuation values by dropping any existing block
        # and re-inserting canonical values at override_path.
        if section == "alpha":
            if stripped == "continuation:" and line.startswith("  ") and not line.startswith("    "):
                in_alpha_continuation = True
                continue
            if stripped == "breakout:" and line.startswith("  ") and not line.startswith("    "):
                in_alpha_breakout = True
                continue
            if stripped == "swing:" and line.startswith("  ") and not line.startswith("    "):
                in_alpha_swing = True
                continue
            if in_alpha_continuation:
                if line.startswith("  ") and not line.startswith("    "):
                    in_alpha_continuation = False
                else:
                    continue
            if in_alpha_breakout:
                if line.startswith("  ") and not line.startswith("    "):
                    in_alpha_breakout = False
                else:
                    continue
            if in_alpha_swing:
                if line.startswith("  ") and not line.startswith("    "):
                    in_alpha_swing = False
                else:
                    continue

        if section == "alpha" and stripped.startswith("type:"):
            lines.append(f"  type: {alpha_type}")
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("threshold_bps:"):
            lines.append(f"      threshold_bps: {swing_reversal_threshold_bps}")
            lines.append(f"      reversal_threshold_bps: {swing_reversal_threshold_bps}")
            lines.append(f"      min_range_bps: {swing_min_range_bps}")
            lines.append(f"      micro_rebound_max_spread_bps: {swing_micro_rebound_max_spread_bps}")
            lines.append(
                "      micro_rebound_max_context_range_pos: "
                f"{swing_micro_rebound_max_context_range_pos}"
            )
            lines.append(
                "      micro_rebound_min_context_rebound_bps: "
                f"{swing_micro_rebound_min_context_rebound_bps}"
            )
            lines.append(f"      micro_rebound_min_ret_bps: {swing_micro_rebound_min_ret_bps}")
            lines.append(
                "      micro_rebound_confirm_rebound_bps: "
                f"{swing_micro_rebound_confirm_rebound_bps}"
            )
            lines.append(
                "      micro_rebound_confirm_min_ret_bps: "
                f"{swing_micro_rebound_confirm_min_ret_bps}"
            )
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("reversal_threshold_bps:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("min_range_bps:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("micro_rebound_max_spread_bps:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("micro_rebound_max_context_range_pos:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("micro_rebound_min_context_rebound_bps:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("micro_rebound_min_ret_bps:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("micro_rebound_confirm_rebound_bps:"):
            continue
        elif section == "alpha" and in_alpha_auto_swing and stripped.startswith("micro_rebound_confirm_min_ret_bps:"):
            continue
        elif section == "alpha" and stripped.startswith("override_path:"):
            lines.append(line)
            lines.append("  continuation:")
            lines.append(f"    lookback: {cont_lookback}")
            lines.append(f"    trend_min_bps: {cont_trend_min_bps}")
            lines.append(f"    rebound_trigger_bps: {cont_rebound_trigger_bps}")
            lines.append(f"    rebound_confirm_bars: {cont_rebound_confirm_bars}")
            lines.append(f"    pullback_min_bps: {cont_pullback_min_bps}")
            lines.append(f"    pullback_max_bps: {cont_pullback_max_bps}")
            lines.append(f"    recent_bias_lookback: {cont_recent_bias_lookback}")
            lines.append(f"    max_chase_bps: {cont_max_chase_bps}")
            lines.append(f"    min_volume_z: {cont_min_volume_z}")
            lines.append(f"    max_structure_range_pos: {cont_max_structure_range_pos}")
            lines.append(
                f"    stall_recovery_max_range_pos: {cont_stall_recovery_max_range_pos}"
            )
            lines.append(
                f"    range_continuation_max_range_pos: {cont_range_continuation_max_range_pos}"
            )
            lines.append(f"    max_range_pos: {cont_max_range_pos}")
            lines.append(
                f"    hard_block_above_range_pos: {cont_hard_block_above_range_pos}"
            )
            lines.append(
                f"    hard_block_max_drawdown_bps: {cont_hard_block_max_drawdown_bps}"
            )
            lines.append(f"    impulse_min_ret_bps: {cont_impulse_min_ret_bps}")
            lines.append(f"    impulse_min_volume_z: {cont_impulse_min_volume_z}")
            lines.append(
                f"    impulse_max_context_range_pos: {cont_impulse_max_context_range_pos}"
            )
            lines.append(
                f"    impulse_max_extension_bps: {cont_impulse_max_extension_bps}"
            )
            lines.append(
                "    impulse_require_up_structure: "
                + ("true" if cont_impulse_require_up_structure else "false")
            )
            lines.append(f"    staircase_min_trend_bps: {cont_staircase_min_trend_bps}")
            lines.append(f"    staircase_min_ret_bps: {cont_staircase_min_ret_bps}")
            lines.append(f"    staircase_min_volume_z: {cont_staircase_min_volume_z}")
            lines.append(
                f"    staircase_min_slope_medium_bps: {cont_staircase_min_slope_medium_bps}"
            )
            lines.append(
                f"    staircase_min_slope_long_bps: {cont_staircase_min_slope_long_bps}"
            )
            lines.append(
                "    staircase_min_drawdown_from_peak_bps: "
                f"{cont_staircase_min_drawdown_from_peak_bps}"
            )
            lines.append(
                "    staircase_max_drawdown_from_peak_bps: "
                f"{cont_staircase_max_drawdown_from_peak_bps}"
            )
            lines.append(
                "    staircase_max_context_range_pos: "
                f"{cont_staircase_max_context_range_pos}"
            )
            lines.append(f"    staircase_max_spread_bps: {cont_staircase_max_spread_bps}")
            lines.append(
                "    staircase_require_up_structure: "
                + ("true" if cont_staircase_require_up_structure else "false")
            )
            lines.append(
                "    early_liftoff_max_context_range_pos: "
                f"{cont_early_liftoff_max_context_range_pos}"
            )
            lines.append(
                "    early_liftoff_max_structure_range_pos: "
                f"{cont_early_liftoff_max_structure_range_pos}"
            )
            lines.append(
                "    early_liftoff_min_context_rebound_bps: "
                f"{cont_early_liftoff_min_context_rebound_bps}"
            )
            lines.append(
                f"    early_liftoff_max_spread_bps: {cont_early_liftoff_max_spread_bps}"
            )
            lines.append(
                f"    early_liftoff_min_volume_z: {cont_early_liftoff_min_volume_z}"
            )
            lines.append(
                f"    early_liftoff_min_ret_bps: {cont_early_liftoff_min_ret_bps}"
            )
            lines.append(
                "    early_liftoff_min_slope_short_bps: "
                f"{cont_early_liftoff_min_slope_short_bps}"
            )
            lines.append(
                "    early_liftoff_min_slope_medium_bps: "
                f"{cont_early_liftoff_min_slope_medium_bps}"
            )
            lines.append(
                "    early_liftoff_min_drawdown_from_peak_bps: "
                f"{cont_early_liftoff_min_drawdown_from_peak_bps}"
            )
            lines.append(
                f"    early_liftoff_min_trend_bps: {cont_early_liftoff_min_trend_bps}"
            )
            lines.append(f"    bar_seconds: {md_interval_seconds}")
            lines.append("    reference_bar_seconds: 60")
            lines.append("  breakout:")
            lines.append("    lookback: 12")
            lines.append(f"    trigger_bps: {breakout_trigger_bps}")
            lines.append("    scale: 3.0")
            lines.append("    max_edge_bps: 400.0")
            lines.append(
                "    bottom_countertrend_block_max_context_range_pos: "
                f"{breakout_bottom_countertrend_block_max_context_range_pos}"
            )
            lines.append(
                "    bottom_countertrend_block_max_context_rebound_bps: "
                f"{breakout_bottom_countertrend_block_max_context_rebound_bps}"
            )
            lines.append(
                "    bottom_countertrend_block_max_trend_return_bps: "
                f"{breakout_bottom_countertrend_block_max_trend_return_bps}"
            )
            lines.append(
                "    top_zone_block_min_context_range_pos: "
                f"{breakout_top_zone_block_min_context_range_pos}"
            )
            lines.append(
                "    top_zone_block_max_context_rebound_bps: "
                f"{breakout_top_zone_block_max_context_rebound_bps}"
            )
            lines.append(
                "    top_zone_block_min_volume_z: "
                f"{breakout_top_zone_block_min_volume_z}"
            )
            lines.append(
                "    thin_rebound_block_context_range_pos: "
                f"{breakout_thin_rebound_block_context_range_pos}"
            )
            lines.append(
                "    thin_rebound_block_context_rebound_bps: "
                f"{breakout_thin_rebound_block_context_rebound_bps}"
            )
            lines.append(
                "    thin_rebound_block_min_spread_bps: "
                f"{breakout_thin_rebound_block_min_spread_bps}"
            )
            lines.append(
                "    mid_rebound_block_context_range_pos: "
                f"{breakout_mid_rebound_block_context_range_pos}"
            )
            lines.append(
                "    mid_rebound_block_context_rebound_bps: "
                f"{breakout_mid_rebound_block_context_rebound_bps}"
            )
            lines.append(
                "    mid_rebound_block_min_volume_z: "
                f"{breakout_mid_rebound_block_min_volume_z}"
            )
            lines.append(
                "    late_rebound_block_context_range_pos: "
                f"{breakout_late_rebound_block_context_range_pos}"
            )
            lines.append(
                "    late_rebound_block_context_rebound_bps: "
                f"{breakout_late_rebound_block_context_rebound_bps}"
            )
            lines.append(
                "    late_rebound_block_min_volume_z: "
                f"{breakout_late_rebound_block_min_volume_z}"
            )
            lines.append("  swing:")
            lines.append(f"    lookback: {swing_lookback_bars}")
            lines.append(f"    buy_band: {swing_buy_band}")
            lines.append(f"    sell_band: {swing_sell_band}")
            lines.append(f"    momentum_lookback: {swing_momentum_lookback}")
            lines.append(f"    reversal_threshold_bps: {swing_reversal_threshold_bps}")
            lines.append(f"    edge_scale: {swing_edge_scale}")
            lines.append(f"    min_range_bps: {swing_min_range_bps}")
            lines.append(f"    micro_rebound_max_spread_bps: {swing_micro_rebound_max_spread_bps}")
            lines.append(
                "    micro_rebound_max_context_range_pos: "
                f"{swing_micro_rebound_max_context_range_pos}"
            )
            lines.append(
                "    micro_rebound_min_context_rebound_bps: "
                f"{swing_micro_rebound_min_context_rebound_bps}"
            )
            lines.append(f"    micro_rebound_min_ret_bps: {swing_micro_rebound_min_ret_bps}")
            lines.append(
                "    micro_rebound_confirm_rebound_bps: "
                f"{swing_micro_rebound_confirm_rebound_bps}"
            )
            lines.append(
                "    micro_rebound_confirm_min_ret_bps: "
                f"{swing_micro_rebound_confirm_min_ret_bps}"
            )
            lines.append(f"    max_edge_bps: {swing_max_edge_bps}")
        elif section == "features" and stripped.startswith("return_window:"):
            lines.append(f"  return_window: {feature_return_window}")
        elif section == "features" and stripped.startswith("atr_window:"):
            lines.append(f"  atr_window: {feature_atr_window}")
        elif section == "features" and stripped.startswith("volume_z_window:"):
            lines.append(f"  volume_z_window: {feature_volume_z_window}")
        elif section == "features" and stripped.startswith("trend_window:"):
            lines.append(f"  trend_window: {feature_trend_window}")
        elif section == "features" and stripped.startswith("context_window:"):
            lines.append(f"  context_window: {feature_context_window}")
        elif section == "gate" and stripped.startswith("safety_margin_bps:"):
            lines.append(f"  safety_margin_bps: {float(profile.get('gate_safety_margin_bps', 0.5))}")
            lines.append(f"  cost_coverage_ratio: {float(profile['gate_cost_coverage_ratio'])}")
            lines.append(
                "  cost_roundtrip_multiplier: "
                f"{float(profile['gate_cost_roundtrip_multiplier'])}"
            )
        elif section == "gate" and stripped.startswith("cost_coverage_ratio:"):
            continue
        elif section == "gate" and stripped.startswith("cost_roundtrip_multiplier:"):
            continue
        elif section == "gate" and stripped.startswith("max_spread_bps:"):
            lines.append(f"  max_spread_bps: {float(profile.get('max_spread_bps', 22.0))}")
        elif section == "gate" and stripped.startswith("max_atr_bps:"):
            lines.append("  max_atr_bps: 180.0")
        elif section == "risk" and stripped.startswith("max_exposure_mode:"):
            lines.append("  max_exposure_mode: equity")
        elif section == "risk" and stripped.startswith("max_exposure_fraction:"):
            lines.append(f"  max_exposure_fraction: {fraction}")
        elif section == "risk" and stripped.startswith("entry_edge_bps:"):
            lines.append(f"  entry_edge_bps: {float(profile['entry_edge_bps'])}")
        elif section == "risk" and stripped.startswith("entry_cost_buffer_bps:"):
            lines.append(f"  entry_cost_buffer_bps: {float(profile['entry_cost_buffer_bps'])}")
            lines.append(
                f"  entry_cost_coverage_ratio: {float(profile['entry_cost_coverage_ratio'])}"
            )
            lines.append(
                "  entry_cost_roundtrip_multiplier: "
                f"{float(profile['entry_cost_roundtrip_multiplier'])}"
            )
            lines.append(
                "  entry_min_atr_to_cost_ratio: "
                f"{float(profile['entry_min_atr_to_cost_ratio'])}"
            )
            lines.append(
                "  disable_entry_edge_gate: "
                + ("true" if int(float(profile.get("disable_entry_edge_gate", 0.0))) else "false")
            )
            lines.append(
                "  override_max_structure_range_pos: "
                f"{float(profile['override_max_structure_range_pos'])}"
            )
            lines.append(
                "  override_min_drawdown_from_peak_bps: "
                f"{float(profile['override_min_drawdown_from_peak_bps'])}"
            )
            lines.append(
                "  override_min_drawdown_to_cost_ratio: "
                f"{float(profile['override_min_drawdown_to_cost_ratio'])}"
            )
            lines.append(
                "  override_min_slope_short_bps: "
                f"{float(profile['override_min_slope_short_bps'])}"
            )
            lines.append(
                "  override_max_trend_return_bps: "
                f"{float(profile.get('override_max_trend_return_bps', 0.0))}"
            )
            lines.append(
                "  override_max_context_range_pos: "
                f"{float(profile.get('override_max_context_range_pos', 1.0))}"
            )
            lines.append(
                "  late_entry_block_context_range_pos: "
                f"{float(profile.get('late_entry_block_context_range_pos', 1.0))}"
            )
            lines.append(
                "  late_entry_block_structure_range_pos: "
                f"{float(profile.get('late_entry_block_structure_range_pos', 1.0))}"
            )
            lines.append(
                "  late_entry_block_max_context_drawdown_bps: "
                f"{float(profile.get('late_entry_block_max_context_drawdown_bps', 0.0))}"
            )
            lines.append(
                "  late_entry_block_min_trend_return_bps: "
                f"{float(profile.get('late_entry_block_min_trend_return_bps', 0.0))}"
            )
            lines.append(
                "  late_entry_block_min_return_bps: "
                f"{float(profile.get('late_entry_block_min_return_bps', 0.0))}"
            )
        elif section == "risk" and stripped.startswith("entry_cost_coverage_ratio:"):
            continue
        elif section == "risk" and stripped.startswith("entry_cost_roundtrip_multiplier:"):
            continue
        elif section == "risk" and stripped.startswith("entry_min_atr_to_cost_ratio:"):
            continue
        elif section == "risk" and stripped.startswith("disable_entry_edge_gate:"):
            continue
        elif section == "risk" and stripped.startswith("late_entry_block_"):
            continue
        elif section == "risk" and stripped.startswith("override_max_structure_range_pos:"):
            continue
        elif section == "risk" and stripped.startswith("override_min_drawdown_from_peak_bps:"):
            continue
        elif section == "risk" and stripped.startswith("override_min_drawdown_to_cost_ratio:"):
            continue
        elif section == "risk" and stripped.startswith("override_min_slope_short_bps:"):
            continue
        elif section == "risk" and stripped.startswith("override_max_trend_return_bps:"):
            continue
        elif section == "risk" and stripped.startswith("override_max_context_range_pos:"):
            continue
        elif section == "risk" and stripped.startswith("block_long_if_context_return_bps_below:"):
            lines.append(
                "  block_long_if_context_return_bps_below: "
                f"{float(profile.get('block_long_if_context_return_bps_below', 0.0))}"
            )
        elif section == "risk" and stripped.startswith("block_long_if_trend_return_bps_below:"):
            lines.append(
                "  block_long_if_trend_return_bps_below: "
                f"{float(profile.get('block_long_if_trend_return_bps_below', 0.0))}"
            )
        elif section == "risk" and stripped.startswith("cooldown_bars:"):
            lines.append(f"  cooldown_bars: {scaled_cooldown_bars}")
        elif section == "risk" and stripped.startswith("min_hold_bars:"):
            lines.append(f"  min_hold_bars: {scaled_min_hold_bars}")
        elif section == "risk" and stripped.startswith("daily_loss_limit_eur:"):
            lines.append(
                f"  daily_loss_limit_eur: {float(profile.get('daily_loss_limit_eur', 100000.0))}"
            )
        elif section == "risk" and stripped.startswith("max_drawdown_pct:"):
            lines.append(
                f"  max_drawdown_pct: {float(profile.get('max_drawdown_pct', 100000.0))}"
            )
        elif section == "risk" and stripped.startswith("reentry_min_move_bps:"):
            lines.append(f"  reentry_min_move_bps: {float(profile['reentry_min_move_bps'])}")
            lines.append(
                "  reentry_require_price_at_or_below_last_entry: "
                + (
                    "true"
                    if bool(
                        int(float(profile.get("reentry_require_price_at_or_below_last_entry", 0.0) or 0.0))
                    )
                    else "false"
                )
            )
            lines.append(
                "  reentry_last_entry_tolerance_bps: "
                f"{float(profile.get('reentry_last_entry_tolerance_bps', 0.0) or 0.0)}"
            )
            lines.append(
                "  reentry_cooldown_bars_after_trailing_stop: "
                f"{int(profile.get('reentry_cooldown_bars_after_trailing_stop', 0))}"
            )
            lines.append(
                "  reentry_cooldown_bars_after_whipsaw_stop_loss: "
                f"{int(profile.get('reentry_cooldown_bars_after_whipsaw_stop_loss', 0))}"
            )
            lines.append(
                "  reentry_whipsaw_hard_stop_max_bars: "
                f"{int(profile.get('reentry_whipsaw_hard_stop_max_bars', 0))}"
            )
            lines.append(
                "  reentry_loss_cluster_window_bars: "
                f"{int(profile.get('reentry_loss_cluster_window_bars', 0))}"
            )
            lines.append(
                "  reentry_cooldown_bars_after_loss_cluster: "
                f"{int(profile.get('reentry_cooldown_bars_after_loss_cluster', 0))}"
            )
            lines.append(
                "  reentry_cooldown_bars_after_weak_exit: "
                f"{int(profile.get('reentry_cooldown_bars_after_weak_exit', 0))}"
            )
        elif section == "risk" and stripped.startswith("reentry_cooldown_bars_after_trailing_stop:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_cooldown_bars_after_whipsaw_stop_loss:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_whipsaw_hard_stop_max_bars:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_loss_cluster_window_bars:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_cooldown_bars_after_loss_cluster:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_cooldown_bars_after_weak_exit:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_require_price_at_or_below_last_entry:"):
            continue
        elif section == "risk" and stripped.startswith("reentry_last_entry_tolerance_bps:"):
            continue
        elif section == "risk" and stripped.startswith("full_position_only:"):
            lines.append("  full_position_only: true")
        elif section == "risk" and stripped.startswith("require_break_even_for_exit:"):
            lines.append(
                "  require_break_even_for_exit: "
                + ("true" if require_break_even_for_exit else "false")
            )
        elif section == "risk" and stripped.startswith("allow_reversal_exit_after_break_even:"):
            lines.append(
                "  allow_reversal_exit_after_break_even: "
                + ("true" if (safety_exits_enabled and require_break_even_for_exit) else "false")
            )
        elif section == "risk" and stripped.startswith("hard_stop_loss_bps:"):
            lines.append(
                f"  hard_stop_loss_bps: {float(profile['hard_stop_loss_bps']) if safety_exits_enabled else 0.0}"
            )
        elif section == "risk" and stripped.startswith("min_exit_profit_bps:"):
            lines.append(f"  min_exit_profit_bps: {float(profile['min_exit_profit_bps'])}")
        elif section == "risk" and stripped.startswith("time_break_even_floor_enabled:"):
            lines.append(
                "  time_break_even_floor_enabled: "
                + ("true" if safety_exits_enabled else "false")
            )
        elif section == "risk" and stripped.startswith("time_break_even_floor_bars:"):
            lines.append(f"  time_break_even_floor_bars: {scaled_time_break_even_floor_bars}")
        elif section == "risk" and stripped.startswith("hard_take_profit_bps:"):
            lines.append("  hard_take_profit_bps: 0.0")
        elif section == "risk" and stripped.startswith("hard_take_profit_only_in_range:"):
            lines.append("  hard_take_profit_only_in_range: false")
        elif section == "risk" and stripped.startswith("trailing_stop_enabled:"):
            lines.append(
                "  trailing_stop_enabled: "
                + ("true" if safety_exits_enabled else "false")
            )
        elif section == "risk" and stripped.startswith("trailing_activation_bps:"):
            lines.append(f"  trailing_activation_bps: {float(profile['trailing_activation_bps'])}")
        elif section == "risk" and stripped.startswith("trailing_stop_bps:"):
            lines.append(f"  trailing_stop_bps: {float(profile['trailing_stop_bps'])}")
        elif section == "risk" and stripped.startswith("trailing_stop_atr_mult:"):
            lines.append("  trailing_stop_atr_mult: 0.0")
            lines.append(
                "  campaign_hold_enabled: "
                + ("true" if int(profile["campaign_hold_enabled"]) else "false")
            )
            lines.append(f"  campaign_hold_min_bars: {scaled_campaign_hold_min_bars}")
            lines.append(
                f"  campaign_hold_min_profit_bps: {float(profile['campaign_hold_min_profit_bps'])}"
            )
            lines.append(
                f"  campaign_hold_min_trend_bps: {float(profile['campaign_hold_min_trend_bps'])}"
            )
            lines.append(
                f"  campaign_hold_max_range_pos: {float(profile['campaign_hold_max_range_pos'])}"
            )
            lines.append(
                "  campaign_hold_max_drawdown_from_peak_bps: "
                f"{float(profile['campaign_hold_max_drawdown_from_peak_bps'])}"
            )
            lines.append(
                f"  campaign_hold_min_recent_bias_bps: {float(profile['campaign_hold_min_recent_bias_bps'])}"
            )
        elif section == "risk" and stripped.startswith("campaign_hold_"):
            continue
        elif section == "risk" and stripped.startswith("red_candle_exit_enabled:"):
            lines.append("  red_candle_exit_enabled: false")
        elif section == "risk" and stripped.startswith("red_candle_window_bars:"):
            lines.append("  red_candle_window_bars: 1")
            lines.append(
                "  green_candle_take_exit_enabled: "
                + ("true" if int(profile["green_candle_take_exit_enabled"]) else "false")
            )
            lines.append(
                f"  green_candle_take_min_bars: {scaled_green_take_min_bars}"
            )
            lines.append(
                f"  green_candle_take_max_bars: {scaled_green_take_max_bars}"
            )
            lines.append(
                "  green_candle_take_required_green_bars: "
                f"{int(profile['green_candle_take_required_green_bars'])}"
            )
            lines.append(
                f"  green_candle_take_min_profit_bps: {float(profile['green_candle_take_min_profit_bps'])}"
            )
        elif section == "risk" and stripped.startswith("green_candle_take_"):
            continue
        elif section == "risk" and stripped.startswith("failed_start_exit_enabled:"):
            lines.append(
                "  failed_start_exit_enabled: "
                + ("true" if safety_exits_enabled else "false")
            )
        elif section == "risk" and stripped.startswith("failed_start_min_bars:"):
            continue
        elif section == "risk" and stripped.startswith("failed_start_max_bars:"):
            lines.append(f"  failed_start_min_bars: {scaled_failed_start_min_bars}")
            lines.append(f"  failed_start_max_bars: {scaled_failed_start_max_bars}")
        elif section == "risk" and stripped.startswith("failed_start_min_rebound_bps:"):
            lines.append(
                f"  failed_start_min_rebound_bps: {float(profile['failed_start_min_rebound_bps'])}"
            )
        elif section == "risk" and stripped.startswith("failed_start_loss_bps:"):
            lines.append(f"  failed_start_loss_bps: {float(profile['failed_start_loss_bps'])}")
            lines.append(
                "  chop_break_even_reclaim_enabled: "
                + ("true" if safety_exits_enabled else "false")
            )
            lines.append(f"  chop_break_even_reclaim_min_bars: {scaled_chop_reclaim_min_bars}")
            lines.append("  chop_break_even_reclaim_min_drawdown_bps: 50.0")
            lines.append("  chop_break_even_reclaim_max_edge_bps: 0.0")
            lines.append(
                f"  chop_break_even_reclaim_cross_window_bars: {scaled_chop_reclaim_cross_window_bars}"
            )
            lines.append("  chop_break_even_reclaim_min_crosses: 3")
            lines.append(
                "  peak_profit_retrace_enabled: "
                + ("true" if safety_exits_enabled else "false")
            )
            lines.append("  peak_profit_retrace_arm_bps: 120.0")
        elif section == "risk" and stripped.startswith("chop_break_even_reclaim_"):
            continue
        elif section == "risk" and stripped.startswith("peak_profit_retrace_pct:"):
            if not profit_roll_block_written:
                lines.append(
                    f"  peak_profit_retrace_pct: {float(profile.get('peak_profit_retrace_pct', 50.0) or 50.0)}"
                )
                lines.append(
                    "  profit_roll_exit_enabled: "
                    + ("true" if profit_roll_exit_enabled else "false")
                )
                lines.append(f"  profit_roll_arm_eur: {profit_roll_arm_eur}")
                lines.append(f"  profit_roll_retrace_eur: {profit_roll_retrace_eur}")
                lines.append(f"  profit_roll_retrace_pct: {profit_roll_retrace_pct}")
                lines.append(f"  profit_roll_min_retrace_eur: {profit_roll_min_retrace_eur}")
                lines.append(f"  profit_roll_min_keep_profit_bps: {profit_roll_min_keep_profit_bps}")
                lines.append(
                    "  corridor_step_mode_enabled: "
                    + ("true" if corridor_staged_mode_enabled else "false")
                )
                profit_roll_block_written = True
        elif section == "risk" and stripped.startswith("peak_profit_retrace_"):
            continue
        elif section == "risk" and stripped.startswith("profit_roll_exit_enabled:"):
            continue
        elif section == "risk" and stripped.startswith("profit_roll_arm_eur:"):
            continue
        elif section == "risk" and stripped.startswith("profit_roll_retrace_eur:"):
            continue
        elif section == "risk" and stripped.startswith("profit_roll_retrace_pct:"):
            continue
        elif section == "risk" and stripped.startswith("profit_roll_min_retrace_eur:"):
            continue
        elif section == "risk" and stripped.startswith("profit_roll_min_keep_profit_bps:"):
            continue
        elif section == "risk" and stripped.startswith("corridor_step_mode_enabled:"):
            continue
        elif section == "risk" and stripped.startswith("min_entry_depth_eur:"):
            continue
        elif section == "risk" and stripped.startswith("max_entry_notional_to_depth_ratio:"):
            continue
        elif section == "risk" and stripped.startswith("position_epsilon_eur:"):
            lines.append("  position_epsilon_eur: 3.0")
            lines.append("  min_entry_depth_eur: 40.0")
            lines.append("  max_entry_notional_to_depth_ratio: 1.0")
        elif section == "order" and stripped.startswith("cycle_trade_mode:"):
            lines.append("  cycle_trade_mode: equity")
        elif section == "order" and stripped.startswith("cycle_trade_fraction:"):
            lines.append(f"  cycle_trade_fraction: {fraction}")
        elif section == "md" and stripped.startswith("interval_seconds:"):
            lines.append(f"  interval_seconds: {md_interval_seconds}")
        elif section == "md" and stripped.startswith("stale_seconds:"):
            lines.append("  stale_seconds: 180")
        elif section == "md" and stripped.startswith("stale_book_seconds:"):
            lines.append("  stale_book_seconds: 180")
        elif section == "md" and stripped.startswith("stale_trade_seconds:"):
            lines.append("  stale_trade_seconds: 180")
        elif section == "exec" and stripped.startswith("sync_min_position_eur:"):
            lines.append("  sync_min_position_eur: 3.0")
            lines.append("  min_entry_notional_eur: 6.0")
            lines.append("  sell_balance_buffer_btc: 0.0")
        elif section == "exec" and stripped.startswith("reconcile_interval_sec:"):
            lines.append(f"  reconcile_interval_sec: {exec_reconcile_interval_sec}")
        elif section == "exec" and stripped.startswith("min_entry_notional_eur:"):
            continue
        elif section == "exec" and stripped.startswith("sell_balance_buffer_btc:"):
            continue
        elif section == "impact" and stripped.startswith("enabled:"):
            lines.append("  enabled: false")
        elif section == "impact" and stripped.startswith("interval_seconds:"):
            lines.append("  interval_seconds: 300")
        elif section == "news" and stripped.startswith("require_long_bias_for_entries:"):
            lines.append("  require_long_bias_for_entries: false")
        elif section == "core" and stripped.startswith("stale_seconds:"):
            lines.append("  stale_seconds: 180")
        elif section == "core" and stripped == "warmup:":
            lines.append(line)
            in_warmup = True
        elif section == "core" and in_warmup and stripped.startswith("enabled:"):
            lines.append("    enabled: true")
        elif section == "core" and in_warmup and stripped.startswith("window_hours:"):
            lines.append(f"    window_hours: {warmup_window_hours:.1f}")
        elif section == "core" and in_warmup and stripped.startswith("min_bars:"):
            lines.append(f"    min_bars: {scaled_warmup_min_bars}")
        elif section == "core" and in_warmup and stripped.startswith("max_bars:"):
            lines.append(f"    max_bars: {scaled_warmup_max_bars}")
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("enabled:"):
            lines.append("    enabled: true")
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("window_bars:"):
            lines.append(f"    window_bars: {scaled_profit_corridor_window_bars}")
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("min_bars:"):
            lines.append(f"    min_bars: {scaled_profit_corridor_min_bars}")
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("max_entry_position_pct:"):
            lines.append("    max_entry_position_pct: 0.0")
            lines.append(
                "    short_horizon_entry_guard_enabled: "
                + ("true" if corridor_short_horizon_entry_guard_enabled else "false")
            )
            lines.append(
                f"    short_horizon_entry_window_bars: {scaled_profit_corridor_short_horizon_entry_window_bars}"
            )
            lines.append(
                f"    short_horizon_entry_min_bars: {scaled_profit_corridor_short_horizon_entry_min_bars}"
            )
            lines.append(
                f"    short_horizon_no_buy_above_pct: {corridor_short_horizon_no_buy_above_pct}"
            )
            lines.append(f"    fast_window_bars: {scaled_profit_corridor_fast_window_bars}")
            lines.append(f"    fast_min_bars: {scaled_profit_corridor_fast_min_bars}")
            lines.append(f"    fast_blend_weight: {corridor_fast_blend_weight}")
            lines.append(
                "    robust_range_enabled: "
                + ("true" if corridor_robust_range_enabled else "false")
            )
            lines.append(f"    robust_low_pct: {corridor_robust_low_pct}")
            lines.append(f"    robust_high_pct: {corridor_robust_high_pct}")
            lines.append(
                "    staged_mode_enabled: "
                + ("true" if corridor_staged_mode_enabled else "false")
            )
            lines.append(f"    staged_entry_1_pct: {corridor_staged_entry_1_pct}")
            lines.append(f"    staged_entry_2_pct: {corridor_staged_entry_2_pct}")
            lines.append(f"    staged_entry_3_pct: {corridor_staged_entry_3_pct}")
            lines.append(f"    staged_entry_4_pct: {corridor_staged_entry_4_pct}")
            lines.append(f"    staged_no_buy_above_pct: {corridor_staged_no_buy_above_pct}")
            lines.append(f"    staged_exit_step_pct: {corridor_staged_exit_step_pct}")
            lines.append(f"    staged_hysteresis_pct: {corridor_staged_hysteresis_pct}")
            lines.append(f"    staged_exit_retrace_pct: {corridor_staged_exit_retrace_pct}")
            lines.append(f"    staged_entry_wait_bars: {corridor_staged_entry_wait_bars}")
            lines.append(
                "    staged_transition_smoothing_bars: "
                f"{corridor_staged_transition_smoothing_bars}"
            )
            lines.append(
                "    staged_require_rising: "
                + ("true" if corridor_staged_require_rising else "false")
            )
            lines.append(
                "    staged_profit_target_enabled: "
                + ("true" if corridor_staged_profit_target_enabled else "false")
            )
            lines.append(
                f"    staged_profit_target_base_pct: {corridor_staged_profit_target_base_pct}"
            )
            lines.append(
                f"    staged_profit_target_min_pct: {corridor_staged_profit_target_min_pct}"
            )
            lines.append(
                f"    staged_profit_target_max_pct: {corridor_staged_profit_target_max_pct}"
            )
            lines.append(
                f"    staged_profit_target_mult_10: {corridor_staged_profit_target_mult_10}"
            )
            lines.append(
                f"    staged_profit_target_mult_20: {corridor_staged_profit_target_mult_20}"
            )
            lines.append(
                f"    staged_profit_target_mult_30: {corridor_staged_profit_target_mult_30}"
            )
            lines.append(
                f"    staged_profit_target_mult_40: {corridor_staged_profit_target_mult_40}"
            )
            lines.append(
                f"    staged_profit_target_mult_50: {corridor_staged_profit_target_mult_50}"
            )
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("staged_"):
            continue
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("short_horizon_"):
            continue
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("fast_"):
            continue
        elif section == "policy" and in_policy_profit_corridor and stripped.startswith("robust_"):
            continue
        else:
            lines.append(line)
    normalized_lines: list[str] = []
    for ln in lines:
        stripped_ln = ln.strip()
        if stripped_ln.startswith("require_break_even_for_exit:"):
            ln = "  require_break_even_for_exit: " + (
                "true" if require_break_even_for_exit else "false"
            )
        elif stripped_ln.startswith("allow_reversal_exit_after_break_even:"):
            ln = "  allow_reversal_exit_after_break_even: " + (
                "true" if (safety_exits_enabled and require_break_even_for_exit) else "false"
            )
        elif stripped_ln.startswith("hard_stop_loss_bps:"):
            ln = (
                f"  hard_stop_loss_bps: {float(profile['hard_stop_loss_bps'])}"
                if safety_exits_enabled
                else "  hard_stop_loss_bps: 0.0"
            )
        elif stripped_ln.startswith("time_break_even_floor_enabled:"):
            ln = "  time_break_even_floor_enabled: " + (
                "true" if safety_exits_enabled else "false"
            )
        elif stripped_ln.startswith("trailing_stop_enabled:"):
            ln = "  trailing_stop_enabled: " + ("true" if safety_exits_enabled else "false")
        elif stripped_ln.startswith("failed_start_exit_enabled:"):
            ln = "  failed_start_exit_enabled: " + (
                "true" if safety_exits_enabled else "false"
            )
        elif stripped_ln.startswith("chop_break_even_reclaim_enabled:"):
            ln = "  chop_break_even_reclaim_enabled: " + (
                "true" if safety_exits_enabled else "false"
            )
        elif stripped_ln.startswith("peak_profit_retrace_enabled:"):
            ln = "  peak_profit_retrace_enabled: " + (
                "true" if safety_exits_enabled else "false"
            )
        normalized_lines.append(ln)
    path.write_text("\n".join(normalized_lines) + "\n", encoding="utf-8")


def _macro_base_gate(row: dict) -> bool:
    # Macro is the base gate for lane admission:
    # - reject clear macro-down context
    # - accept clear macro-up context
    # - accept neutral/soft macro only when short-horizon scalp readiness is present
    if bool(row.get("macro_down_context")):
        return False
    if bool(row.get("macro_up_context")):
        return True
    return bool(row.get("macro_soft_support")) and (
        bool(row.get("short_horizon_scalp_ok")) or bool(row.get("staircase_trend"))
    )


def _macro_watch_ok(row: dict) -> bool:
    if _persistent_downdrift_entry_block(row):
        return False
    return _macro_base_gate(row)


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _simple_swing_selector_mode_enabled() -> bool:
    return _env_flag("ROTATION_SELECTOR_SIMPLE_SWING_MODE", default=False)


def _selector_bypass_watch_pool_enabled() -> bool:
    return _env_flag("ROTATION_SELECTOR_BYPASS_WATCH_POOL", default=False)


def _watch_all_pool_enabled() -> bool:
    return _env_flag("ROTATION_SELECTOR_WATCH_ALL_POOL", default=False)


def _macro_fraction_adjust_enabled() -> bool:
    return _env_flag("ROTATION_ENABLE_MACRO_FRACTION_ADJUST", default=False)


def _simple_swing_buy_band() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_BUY_BAND", "")
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("ROTATION_SWING_BUY_BAND", "0.25")
    try:
        return max(0.01, min(0.95, float(raw)))
    except Exception:
        return 0.25


def _simple_swing_min_ret15_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MIN_RET15_BPS", "0.0")
    try:
        return float(raw)
    except Exception:
        return 0.0


def _simple_swing_min_rebound_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MIN_REBOUND_BPS", "8.0")
    try:
        return float(raw)
    except Exception:
        return 8.0


def _simple_swing_min_turns24() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MIN_TURNS24", "45.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 45.0


def _simple_swing_min_width72_pct() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MIN_WIDTH72_PCT", "8.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 8.0


def _simple_swing_min_rebound60_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MIN_REBOUND60_BPS", "35.0")
    try:
        return float(raw)
    except Exception:
        return 35.0


def _simple_swing_cycle_lookback_bars() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_CYCLE_LOOKBACK_BARS", "4320")
    try:
        return max(1.0, float(raw))
    except Exception:
        return 4320.0


def _simple_swing_cycle_min_rebound_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_CYCLE_MIN_REBOUND_BPS", "120.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 120.0


def _simple_swing_adaptive_cycle_enabled() -> bool:
    return _env_flag("ROTATION_SELECTOR_ADAPTIVE_CYCLE_ENABLED", default=True)


def _simple_swing_adaptive_cycle_min_confidence() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_CONFIDENCE", "0.32")
    try:
        return max(0.0, min(1.0, float(raw)))
    except Exception:
        return 0.32


def _simple_swing_adaptive_cycle_min_swing_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_SWING_BPS", "60.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 60.0


def _simple_swing_adaptive_cycle_min_half_bars() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_HALF_BARS", "72")
    try:
        return max(1.0, float(raw))
    except Exception:
        return 72.0


def _simple_swing_adaptive_cycle_max_half_bars() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MAX_HALF_BARS", "1728")
    try:
        return max(_simple_swing_adaptive_cycle_min_half_bars() + 1.0, float(raw))
    except Exception:
        return max(_simple_swing_adaptive_cycle_min_half_bars() + 1.0, 1728.0)


def _simple_swing_adaptive_cycle_min_phase_progress() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MIN_PHASE_PROGRESS", "0.20")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 0.20


def _simple_swing_adaptive_cycle_max_phase_progress() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ADAPTIVE_CYCLE_MAX_PHASE_PROGRESS", "2.20")
    try:
        return max(_simple_swing_adaptive_cycle_min_phase_progress() + 0.05, float(raw))
    except Exception:
        return 2.20


def _simple_swing_crash_guard_enabled() -> bool:
    return _env_flag("ROTATION_SELECTOR_SIMPLE_SWING_CRASH_GUARD_ENABLED", default=True)


def _simple_swing_crash_guard_min_ret60_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_CRASH_GUARD_MIN_RET60_BPS", "-600.0")
    try:
        return float(raw)
    except Exception:
        return -600.0


def _simple_swing_crash_guard_min_ret30_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_CRASH_GUARD_MIN_RET30_BPS", "-450.0")
    try:
        return float(raw)
    except Exception:
        return -450.0


def _simple_swing_micro_valley_required() -> bool:
    return _env_flag("ROTATION_SELECTOR_SIMPLE_SWING_REQUIRE_MICRO_VALLEY", default=True)


def _simple_swing_micro_valley_max_age_bars() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_MAX_AGE_BARS", "18")
    try:
        return max(1.0, float(raw))
    except Exception:
        return 18.0


def _simple_swing_micro_valley_max_30m_age_bars() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_MAX_30M_AGE_BARS", "8")
    try:
        return max(1.0, float(raw))
    except Exception:
        return 8.0


def _simple_swing_micro_valley_min_rebound30_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_MIN_REBOUND30_BPS", "3.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 3.0


def _simple_swing_micro_valley_max_rebound30_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_MAX_REBOUND30_BPS", "38.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 38.0


def _simple_swing_micro_valley_min_confirmation_signals() -> int:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_MIN_CONFIRM_SIGNALS", "2")
    try:
        return max(1, int(float(raw)))
    except Exception:
        return 2


def _simple_swing_micro_valley_confirm_min_ret15_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_CONFIRM_MIN_RET15_BPS", "0.0")
    try:
        return float(raw)
    except Exception:
        return 0.0


def _simple_swing_micro_valley_confirm_min_slope_short_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_CONFIRM_MIN_SLOPE_SHORT_BPS", "0.0")
    try:
        return float(raw)
    except Exception:
        return 0.0


def _simple_swing_micro_valley_confirm_min_rebound30_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_CONFIRM_MIN_REBOUND30_BPS", "5.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 5.0


def _simple_swing_micro_valley_min_quality_score() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_SIMPLE_SWING_MICRO_VALLEY_MIN_QUALITY_SCORE", "35.0")
    try:
        return max(0.0, float(raw))
    except Exception:
        return 35.0


def _selector_entry_quality_enabled() -> bool:
    return _env_flag("ROTATION_SELECTOR_ENTRY_QUALITY_ENABLED", default=False)


def _selector_entry_quality_min_score() -> float:
    return max(0.0, _as_float(os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_SCORE"), 52.0))


def _selector_entry_quality_min_confirmations() -> int:
    return max(
        1,
        int(_as_float(os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_CONFIRMATIONS"), 4.0)),
    )


def _selector_entry_quality_min_ret30_bps() -> float:
    return _as_float(os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_RET30_BPS"), 0.0)


def _selector_entry_quality_min_rebound30_bps() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_REBOUND30_BPS")
    if raw is None or str(raw).strip() == "":
        return _simple_swing_min_rebound_bps()
    return max(0.0, _as_float(raw, _simple_swing_min_rebound_bps()))


def _selector_entry_quality_min_slope_short_bps() -> float:
    return _as_float(os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_SLOPE_SHORT_BPS"), 0.0)


def _selector_entry_quality_max_spread_bps() -> float:
    return max(0.0, _as_float(os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MAX_SPREAD_BPS"), 22.0))


def _selector_entry_quality_block_active_leg_fall() -> bool:
    return _env_flag("ROTATION_SELECTOR_ENTRY_QUALITY_BLOCK_ACTIVE_LEG_FALL", default=True)


def _selector_entry_quality_min_quote_volume_5m() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_5M_QUOTE_VOLUME")
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("ROTATION_MIN_5M_QUOTE_VOLUME", "0.0")
    return max(0.0, _as_float(raw, 0.0))


def _selector_entry_quality_min_quote_volume_60m() -> float:
    raw = os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_MIN_60M_QUOTE_VOLUME")
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("ROTATION_MIN_60M_QUOTE_VOLUME", "0.0")
    return max(0.0, _as_float(raw, 0.0))


def _selector_entry_quality_volume_require_both() -> bool:
    raw = os.environ.get("ROTATION_SELECTOR_ENTRY_QUALITY_VOLUME_REQUIRE_BOTH")
    if raw is None or str(raw).strip() == "":
        return _env_flag("ROTATION_VOLUME_GATE_REQUIRE_BOTH", default=True)
    return _env_flag("ROTATION_SELECTOR_ENTRY_QUALITY_VOLUME_REQUIRE_BOTH", default=True)


def _row_quote_volume_5m(row: dict) -> float:
    return _as_float(row.get("quote_volume_5m", row.get("qv_5m")), 0.0)


def _row_quote_volume_60m(row: dict) -> float:
    return _as_float(row.get("quote_volume_60m", row.get("qv_60m")), 0.0)


def _simple_swing_micro_valley_confirmation_signal_count(row: dict) -> int:
    ret15 = _as_float(row.get("ret15_bps"), 0.0)
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    slope_short = _as_float(row.get("structure_slope_short_bps"), 0.0)
    active_leg = str(row.get("active_leg", "") or "").strip().lower()
    signals = 0
    if ret15 >= _simple_swing_micro_valley_confirm_min_ret15_bps():
        signals += 1
    if slope_short >= _simple_swing_micro_valley_confirm_min_slope_short_bps():
        signals += 1
    if rebound30 >= _simple_swing_micro_valley_confirm_min_rebound30_bps():
        signals += 1
    if bool(row.get("recent_rebound_ready")) or active_leg == "rise":
        signals += 1
    return signals


def _simple_swing_micro_valley_confirmation_ok(row: dict) -> bool:
    return _simple_swing_micro_valley_confirmation_signal_count(row) >= _simple_swing_micro_valley_min_confirmation_signals()


def _simple_swing_micro_valley_quality_score(row: dict) -> float:
    buy_band_pct = _simple_swing_buy_band() * 100.0
    pos = _simple_swing_position_pct(row)
    bars_since_swing_low = _as_float(row.get("bars_since_swing_low"), _as_float(row.get("bars_since_valley"), 999.0))
    bars_since_30m_low = _as_float(row.get("bars_since_30m_low"), bars_since_swing_low)
    rebound30 = max(0.0, _as_float(row.get("rebound_from_30m_low_bps"), 0.0))
    spread = max(0.0, _as_float(row.get("spread_bps"), 0.0))
    min_rebound30 = _simple_swing_micro_valley_min_rebound30_bps()
    max_rebound30 = max(min_rebound30, _simple_swing_micro_valley_max_rebound30_bps())

    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    pos_score = _clamp01((buy_band_pct - pos) / max(1.0, buy_band_pct))
    age_score = _clamp01(1.0 - (bars_since_swing_low / _simple_swing_micro_valley_max_age_bars()))
    local_age_score = _clamp01(1.0 - (bars_since_30m_low / _simple_swing_micro_valley_max_30m_age_bars()))
    if rebound30 < min_rebound30:
        rebound_score = _clamp01(rebound30 / max(1.0, min_rebound30))
    elif rebound30 > max_rebound30:
        rebound_score = _clamp01(1.0 - ((rebound30 - max_rebound30) / max(1.0, max_rebound30)))
    else:
        span = max(1.0, max_rebound30 - min_rebound30)
        ideal = min_rebound30 + (0.35 * span)
        rebound_score = _clamp01(1.0 - (abs(rebound30 - ideal) / (0.65 * span)))
    confirmation_score = _clamp01(
        _simple_swing_micro_valley_confirmation_signal_count(row)
        / float(_simple_swing_micro_valley_min_confirmation_signals())
    )
    spread_score = _clamp01(1.0 - (spread / 20.0))
    context_mult = 1.0 if bool(row.get("in_valley_context")) else (0.85 if bool(row.get("in_valley_context_relaxed")) else 0.70)
    quality = (
        (34.0 * pos_score)
        + (18.0 * age_score)
        + (14.0 * local_age_score)
        + (20.0 * rebound_score)
        + (10.0 * confirmation_score)
        + (4.0 * spread_score)
    ) * context_mult
    return max(0.0, min(100.0, quality))


def _simple_swing_entry_quality_volume_ok(row: dict) -> bool:
    min_5m = _selector_entry_quality_min_quote_volume_5m()
    min_60m = _selector_entry_quality_min_quote_volume_60m()
    checks: list[bool] = []
    if min_5m > 0.0:
        checks.append(_row_quote_volume_5m(row) >= min_5m)
    if min_60m > 0.0:
        checks.append(_row_quote_volume_60m(row) >= min_60m)
    if not checks:
        return True
    if _selector_entry_quality_volume_require_both():
        return all(checks)
    return any(checks)


def _simple_swing_entry_quality_confirmation_count(row: dict) -> int:
    ret15 = _as_float(row.get("ret15_bps"), 0.0)
    ret30 = _as_float(row.get("ret30_bps"), 0.0)
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    slope_short = _as_float(row.get("structure_slope_short_bps"), 0.0)
    active_leg = str(row.get("active_leg", "") or "").strip().lower()
    confirmations = 0
    if ret15 >= _simple_swing_min_ret15_bps():
        confirmations += 1
    if ret30 >= _selector_entry_quality_min_ret30_bps():
        confirmations += 1
    if rebound30 >= _selector_entry_quality_min_rebound30_bps():
        confirmations += 1
    if slope_short >= _selector_entry_quality_min_slope_short_bps():
        confirmations += 1
    if active_leg == "rise" or bool(row.get("recent_rebound_ready")):
        confirmations += 1
    if _simple_swing_entry_quality_volume_ok(row):
        confirmations += 1
    if _simple_swing_micro_valley_quality_score(row) >= _simple_swing_micro_valley_min_quality_score():
        confirmations += 1
    return confirmations


def _simple_swing_entry_quality_score(row: dict) -> float:
    ret15 = max(0.0, _as_float(row.get("ret15_bps"), 0.0))
    ret30 = max(0.0, _as_float(row.get("ret30_bps"), 0.0))
    rebound30 = max(0.0, _as_float(row.get("rebound_from_30m_low_bps"), 0.0))
    slope_short = max(0.0, _as_float(row.get("structure_slope_short_bps"), 0.0))
    spread = max(0.0, _as_float(row.get("spread_bps"), 0.0))
    active_leg = str(row.get("active_leg", "") or "").strip().lower()
    micro_score = _simple_swing_micro_valley_quality_score(row)
    min_5m = _selector_entry_quality_min_quote_volume_5m()
    min_60m = _selector_entry_quality_min_quote_volume_60m()
    volume_ratios: list[float] = []
    if min_5m > 0.0:
        volume_ratios.append(min(1.0, _row_quote_volume_5m(row) / min_5m))
    if min_60m > 0.0:
        volume_ratios.append(min(1.0, _row_quote_volume_60m(row) / min_60m))
    volume_score = 8.0 * (
        sum(volume_ratios) / float(len(volume_ratios))
        if volume_ratios
        else 1.0
    )
    leg_score = 8.0 if active_leg == "rise" else (4.0 if bool(row.get("recent_rebound_ready")) else 0.0)
    spread_penalty = max(0.0, spread - 12.0) * 0.8
    quality = (
        min(22.0, ret15 * 0.9)
        + min(18.0, ret30 * 0.35)
        + min(18.0, rebound30 * 0.45)
        + min(12.0, slope_short * 4.0)
        + min(18.0, micro_score * 0.30)
        + volume_score
        + leg_score
        - spread_penalty
    )
    return max(0.0, min(100.0, quality))


def _simple_swing_entry_quality_gate_reason(row: dict) -> str:
    if bool(row.get("keep_open")) or not _selector_entry_quality_enabled():
        return ""
    if bool(row.get("still_dumping")):
        return "rule_entry_quality_still_dumping"
    max_spread = _selector_entry_quality_max_spread_bps()
    if max_spread > 0.0 and _as_float(row.get("spread_bps"), 9999.0) > max_spread:
        return "rule_entry_quality_spread"
    if not _simple_swing_entry_quality_volume_ok(row):
        return "rule_entry_quality_volume"
    active_leg = str(row.get("active_leg", "") or "").strip().lower()
    if _selector_entry_quality_block_active_leg_fall() and active_leg == "fall":
        return "rule_entry_quality_still_falling"
    if _as_float(row.get("ret30_bps"), 0.0) < _selector_entry_quality_min_ret30_bps():
        return "rule_entry_quality_ret30"
    if _as_float(row.get("rebound_from_30m_low_bps"), 0.0) < _selector_entry_quality_min_rebound30_bps():
        return "rule_entry_quality_no_rebound"
    if (
        _as_float(row.get("structure_slope_short_bps"), 0.0)
        < _selector_entry_quality_min_slope_short_bps()
        and not (active_leg == "rise" and bool(row.get("recent_rebound_ready")))
    ):
        return "rule_entry_quality_slope"
    if _simple_swing_entry_quality_confirmation_count(row) < _selector_entry_quality_min_confirmations():
        return "rule_entry_quality_unconfirmed"
    if _simple_swing_entry_quality_score(row) < _selector_entry_quality_min_score():
        return "rule_entry_quality_weak"
    return ""


def _simple_swing_micro_valley_gate_reason(row: dict) -> str:
    if bool(row.get("keep_open")) or not _simple_swing_micro_valley_required():
        return ""
    # Treat missing context keys as a context miss to avoid fail-open on stale row schemas.
    if not bool(row.get("in_valley_context")) and not bool(row.get("in_valley_context_relaxed")):
        return "rule_micro_valley_context_miss"
    bars_since_swing_low = _as_float(row.get("bars_since_swing_low"), _as_float(row.get("bars_since_valley"), 999.0))
    if bars_since_swing_low > _simple_swing_micro_valley_max_age_bars():
        return "rule_micro_valley_too_old"
    bars_since_30m_low = _as_float(row.get("bars_since_30m_low"), bars_since_swing_low)
    if bars_since_30m_low > _simple_swing_micro_valley_max_30m_age_bars():
        return "rule_micro_valley_too_old"
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    min_rebound30 = _simple_swing_micro_valley_min_rebound30_bps()
    max_rebound30 = max(min_rebound30, _simple_swing_micro_valley_max_rebound30_bps())
    if rebound30 < min_rebound30:
        return "rule_micro_valley_no_rebound"
    if rebound30 > max_rebound30:
        return "rule_micro_valley_too_late"
    if not _simple_swing_micro_valley_confirmation_ok(row):
        return "rule_micro_valley_unconfirmed"
    if _simple_swing_micro_valley_quality_score(row) < _simple_swing_micro_valley_min_quality_score():
        return "rule_micro_valley_weak"
    return ""


def _simple_swing_adaptive_cycle_fit(row: dict) -> bool:
    if bool(row.get("keep_open")) or not _simple_swing_adaptive_cycle_enabled():
        return False
    fit_flag = bool(row.get("adaptive_cycle_fit_ok")) or bool(row.get("cycle_fit_adaptive_ok"))
    if fit_flag:
        return True
    if row.get("adaptive_cycle_enabled") is False:
        return False
    confidence = _as_float(row.get("adaptive_cycle_confidence"), 0.0)
    if confidence < _simple_swing_adaptive_cycle_min_confidence():
        return False
    half_bars = _as_float(row.get("adaptive_cycle_half_bars"), 0.0)
    if half_bars < _simple_swing_adaptive_cycle_min_half_bars():
        return False
    if half_bars > _simple_swing_adaptive_cycle_max_half_bars():
        return False
    swing_median_bps = _as_float(row.get("adaptive_cycle_swing_median_bps"), 0.0)
    if swing_median_bps < _simple_swing_adaptive_cycle_min_swing_bps():
        return False
    phase_progress = _as_float(row.get("adaptive_cycle_phase_progress"), 0.0)
    if phase_progress < _simple_swing_adaptive_cycle_min_phase_progress():
        return False
    if phase_progress > _simple_swing_adaptive_cycle_max_phase_progress():
        return False
    bars_since_pivot = _as_float(row.get("adaptive_cycle_bars_since_last_pivot"), 0.0)
    window_high = _as_float(row.get("adaptive_cycle_window_high_bars"), 0.0)
    if window_high > 0.0 and bars_since_pivot > (window_high * 1.15):
        return False
    return bool(row.get("adaptive_cycle_window_active")) or phase_progress >= 0.55


def _simple_swing_recent_cycle_fit(row: dict) -> bool:
    if bool(row.get("keep_open")):
        return True
    if not bool(row.get("cycle_fit_profit_step_ok", True)):
        return False
    cycle_fit_ok = bool(row.get("cycle_fit_ok"))
    if cycle_fit_ok:
        return True
    if _simple_swing_adaptive_cycle_fit(row):
        return True
    turns24 = _as_float(row.get("turns24"), 0.0)
    if turns24 < _simple_swing_min_turns24():
        return False
    width72_pct = _as_float(row.get("width72_pct"), 0.0)
    if width72_pct < _simple_swing_min_width72_pct():
        return False
    completed = _as_float(row.get("cycle_fit_completed_cycles"), 0.0)
    success_rate = _as_float(row.get("cycle_fit_success_rate"), 0.0)
    if completed >= 1.0 and success_rate >= 0.30:
        return True
    bars_since_valley = _as_float(row.get("bars_since_valley"), 999999.0)
    rebound_from_valley = _as_float(row.get("geo_rebound_from_valley_bps"), 0.0)
    if bars_since_valley > _simple_swing_cycle_lookback_bars():
        return False
    if rebound_from_valley < _simple_swing_cycle_min_rebound_bps():
        return False
    return True


def _simple_swing_recent_crash_gate_reason(row: dict) -> str:
    if bool(row.get("keep_open")) or not _simple_swing_crash_guard_enabled():
        return ""
    min_ret60 = _as_float(row.get("crash_7d_min_ret60_bps"), 0.0)
    min_ret30 = _as_float(row.get("crash_7d_min_ret30_bps"), 0.0)
    if min_ret60 <= _simple_swing_crash_guard_min_ret60_bps():
        return "rule_7d_crash_event"
    if min_ret30 <= _simple_swing_crash_guard_min_ret30_bps():
        return "rule_7d_crash_event"
    return ""


def _simple_swing_history_fit(row: dict) -> bool:
    return _simple_swing_recent_cycle_fit(row)


def _simple_swing_position_pct(row: dict) -> float:
    return _as_float(
        row.get("pos_7d_nocrash_pct"),
        _as_float(
            row.get("pos_7d_pct"),
            _as_float(
                row.get("pos_48h_pct"),
                _as_float(
                    row.get("pos_36h_pct"),
                    _as_float(row.get("pos_24h_pct"), _as_float(row.get("pos_pct"), 100.0)),
                ),
            ),
        ),
    )


def _simple_swing_is_rising(row: dict) -> bool:
    ret15 = _as_float(row.get("ret15_bps"), 0.0)
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    slope_short = _as_float(row.get("structure_slope_short_bps"), 0.0)
    active_leg = str(row.get("active_leg", "") or "").strip().lower()
    if ret15 <= _simple_swing_min_ret15_bps():
        return False
    if active_leg == "rise":
        return True
    if slope_short > 0.0:
        return True
    return rebound30 >= _simple_swing_min_rebound_bps()


def _simple_swing_entry_ready(row: dict) -> bool:
    if bool(row.get("keep_open")):
        return True
    if not _simple_swing_history_fit(row):
        return False
    if _simple_swing_recent_crash_gate_reason(row):
        return False
    if _simple_swing_micro_valley_gate_reason(row):
        return False
    if not _simple_swing_is_rising(row):
        return False
    if _simple_swing_entry_quality_gate_reason(row):
        return False
    return True


def _simple_swing_gate_reason(row: dict) -> str:
    if bool(row.get("keep_open")):
        return "keep_open"
    if (
        row.get("turns24") is None
        or row.get("width72_pct") is None
        or row.get("cycle_fit_profit_step_ok") is None
    ):
        # Partial/cache rows should not be reported as a cycle-miss.
        return "data_pending"
    if not _simple_swing_recent_cycle_fit(row):
        return "rule_3day_cycle_miss"
    crash_reason = _simple_swing_recent_crash_gate_reason(row)
    if crash_reason:
        return crash_reason
    micro_valley_reason = _simple_swing_micro_valley_gate_reason(row)
    if micro_valley_reason:
        return micro_valley_reason
    if not _simple_swing_is_rising(row):
        return "rule_not_rising_yet"
    entry_quality_reason = _simple_swing_entry_quality_gate_reason(row)
    if entry_quality_reason:
        return entry_quality_reason
    return ""


def _apply_simple_swing_row_gates(rows: list[dict]) -> list[dict]:
    if not _simple_swing_selector_mode_enabled():
        return rows
    for row in rows:
        if bool(row.get("hard_excluded")):
            # Preserve selector hard-block reasons (e.g. persistent/listing
            # downdrift) instead of overwriting with simple-swing gate labels.
            row.setdefault("simple_swing_micro_valley_score", 0.0)
            row.setdefault("simple_swing_micro_valley_confirmed", False)
            row.setdefault("simple_swing_entry_quality_score", 0.0)
            row.setdefault("simple_swing_entry_quality_confirmations", 0)
            row.setdefault("simple_swing_entry_quality_ok", False)
            row.setdefault("simple_swing_entry_quality_reason", "")
            continue
        row["simple_swing_micro_valley_score"] = round(_simple_swing_micro_valley_quality_score(row), 6)
        row["simple_swing_micro_valley_confirmed"] = bool(_simple_swing_micro_valley_confirmation_ok(row))
        row["simple_swing_entry_quality_score"] = round(_simple_swing_entry_quality_score(row), 6)
        row["simple_swing_entry_quality_confirmations"] = int(
            _simple_swing_entry_quality_confirmation_count(row)
        )
        row["simple_swing_entry_quality_ok"] = not bool(
            _simple_swing_entry_quality_gate_reason(row)
        )
        reason = _simple_swing_gate_reason(row)
        row["simple_swing_entry_quality_reason"] = (
            _simple_swing_entry_quality_gate_reason(row)
            if _selector_entry_quality_enabled()
            else ""
        )
        row["gate_reason"] = reason
    return rows


def _simple_swing_selector_score(row: dict) -> float:
    pos = _simple_swing_position_pct(row)
    ret15 = _as_float(row.get("ret15_bps"), 0.0)
    rebound30 = _as_float(row.get("rebound_from_30m_low_bps"), 0.0)
    slope_short = _as_float(row.get("structure_slope_short_bps"), 0.0)
    bars_since_swing_low = _as_float(row.get("bars_since_swing_low"), _as_float(row.get("bars_since_valley"), 999.0))
    micro_valley_score = _simple_swing_micro_valley_quality_score(row)
    micro_valley_confirm_bonus = 22.0 if _simple_swing_micro_valley_confirmation_ok(row) else 0.0
    freshness_bonus = max(0.0, (_simple_swing_micro_valley_max_age_bars() - bars_since_swing_low) * 0.8)
    buy_band_pct = _simple_swing_buy_band() * 100.0
    band_score = max(0.0, buy_band_pct - pos) * 9.0
    return (
        band_score
        + max(0.0, ret15) * 3.5
        + max(0.0, rebound30) * 0.7
        + max(0.0, slope_short) * 8.0
        + (micro_valley_score * 3.2)
        + micro_valley_confirm_bonus
        + freshness_bonus
    )


def _persistent_downdrift_entry_block(row: dict) -> bool:
    if bool(row.get("keep_open")):
        return False
    gate_reason = str(row.get("gate_reason", "") or "").strip().lower()
    if gate_reason.startswith("persistent_downdrift_"):
        return True
    if gate_reason.startswith("non_falling_longtrend_"):
        return True
    if bool(row.get("non_falling_longtrend_blocked")):
        return True
    return bool(row.get("persistent_downdrift_blocked"))


def _active_candidate_gate(row: dict) -> bool:
    if _persistent_downdrift_entry_block(row):
        return False
    if _simple_swing_selector_mode_enabled():
        if bool(row.get("meta_avoid_symbol")) and not bool(row.get("keep_open")):
            return False
        return bool(row.get("keep_open")) or _simple_swing_entry_ready(row)
    if bool(row.get("meta_avoid_symbol")) and not bool(row.get("keep_open")):
        return False
    tags = row.get("strategy_tags")
    if isinstance(tags, list) and tags:
        return True
    return (
        bool(row.get("keep_open"))
        or bool(row.get("eligible"))
        or _is_breakout_candidate(row)
        or _is_staircase_candidate(row)
        or _is_continuation_candidate(row)
        or _is_rebound_candidate(row)
    )


def _strategy_gate_reason_allows_score_override(row: dict, strategy: str) -> bool:
    gate_reason = str(row.get("gate_reason", "") or "").strip().lower()
    if not gate_reason:
        return True
    if gate_reason == "no_valley_context":
        return strategy in {"continuation", "breakout", "staircase"}
    if gate_reason == "structure_stall":
        return strategy in {"continuation", "staircase"}
    if gate_reason.startswith("no_setup_"):
        return strategy in {"continuation", "breakout", "staircase"}
    if gate_reason.startswith("no_trend_setup_"):
        return strategy in {"continuation", "breakout", "staircase"}
    return False


def _strategy_slot_ready_without_watchlist_eligible(row: dict, strategy: str) -> bool:
    if _strategy_score(row, strategy) <= 0.0:
        return False
    if not _strategy_gate_reason_allows_score_override(row, strategy):
        return False
    if bool(row.get("macro_down_context")) or bool(row.get("still_dumping")):
        return False
    if strategy == "continuation":
        if bool(row.get("rebound_in_downtrend")) or bool(row.get("countertrend_rebound")):
            return False
        return (
            not bool(row.get("down_structure"))
            and (
                bool(row.get("trend_ready"))
                or bool(row.get("strong_continuation_context"))
                or bool(row.get("up_structure"))
                or bool(row.get("staircase_trend"))
                or _as_float(row.get("score_trend"), 0.0) >= 120.0
                or _strategy_score(row, "continuation") >= 160.0
            )
        )
    if strategy == "breakout":
        if bool(row.get("rebound_in_downtrend")) or bool(row.get("countertrend_rebound")):
            return False
        if _as_float(row.get("pos_pct"), 100.0) > 78.0 and not bool(row.get("fast_impulse")):
            return False
        return (
            bool(row.get("fast_impulse"))
            or bool(row.get("up_structure"))
            or bool(row.get("short_horizon_scalp_ok"))
            or _as_float(row.get("ret15_bps"), 0.0) >= 10.0
            or _strategy_score(row, "breakout") >= 120.0
        )
    if strategy == "staircase":
        if bool(row.get("down_structure")):
            return False
        pos_24h_pct = _as_float(row.get("pos_24h_pct"), _as_float(row.get("pos_pct"), 100.0))
        if pos_24h_pct > 90.0 and not bool(row.get("fast_staircase")):
            return False
        return (
            bool(row.get("staircase_trend"))
            or _has_true_staircase_structure(row)
            or bool(row.get("up_structure"))
        )
    return False


def _strategy_slot_candidate_ready(row: dict, strategy: str) -> bool:
    if bool(row.get("keep_open")):
        return True
    if _persistent_downdrift_entry_block(row):
        return False
    if not _active_candidate_gate(row):
        return False
    if bool(row.get("recent_live_selection_block")) and not bool(row.get("eligible")):
        return False
    eligible = bool(row.get("eligible"))
    if strategy == "continuation":
        return eligible or _strategy_slot_ready_without_watchlist_eligible(row, "continuation")
    if strategy == "breakout":
        return (
            eligible
            or _is_fast_impulse(row)
            or _strategy_slot_ready_without_watchlist_eligible(row, "breakout")
        )
    if strategy in {"pullback_continuation", "breakout_retest", "relative_strength"}:
        return eligible
    if strategy == "rebound":
        return eligible or _strategy_score(row, "rebound") > 0.0
    if strategy != "staircase":
        return True
    if _strong_staircase_selector_exception(row):
        return True
    if not eligible and not _strategy_slot_ready_without_watchlist_eligible(row, "staircase"):
        return False
    gate_reason = str(row.get("gate_reason", "") or "").strip().lower()
    if gate_reason == "structure_rollover":
        return False
    pos_24h_pct = _as_float(row.get("pos_24h_pct"), _as_float(row.get("pos_pct"), 100.0))
    if pos_24h_pct > 90.0 and not bool(row.get("fast_staircase")):
        return False
    return True


def _is_fast_impulse(row: dict) -> bool:
    # Immediate scalping launcher:
    # if short-horizon momentum is clearly live, allow the symbol to jump into active slots.
    if bool(row.get("fast_impulse")):
        return True
    if bool(row.get("macro_down_context")):
        return False
    if not bool(row.get("short_horizon_scalp_ok")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 20.0:
        return False
    if _as_float(row.get("ret15_bps"), 0.0) < 25.0:
        return False
    if _as_float(row.get("structure_slope_short_bps"), 0.0) < 1.0:
        return False
    if _as_float(row.get("pos_pct"), 100.0) > 92.0:
        return False
    return True


def _is_fast_staircase(row: dict) -> bool:
    if bool(row.get("fast_staircase")):
        return True
    if not bool(row.get("staircase_trend")):
        return False
    if bool(row.get("macro_down_context")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 18.0:
        return False
    if _as_float(row.get("pos_24h_pct"), 100.0) > 88.0:
        return False
    return True


def _impulse_rank(row: dict) -> float:
    fast_score = _as_float(row.get("fast_impulse_score"), 0.0)
    if fast_score > 0.0:
        return fast_score
    ret15 = _as_float(row.get("ret15_bps"), 0.0)
    rel15 = _as_float(row.get("rel15_bps"), 0.0)
    slope_short = _as_float(row.get("structure_slope_short_bps"), 0.0)
    spread = _as_float(row.get("spread_bps"), 0.0)
    return ret15 + max(0.0, rel15) + (0.6 * max(0.0, slope_short)) - (0.2 * spread)


def _staircase_rank(row: dict) -> float:
    fast_score = _as_float(row.get("fast_staircase_score"), 0.0)
    staircase_score = _as_float(row.get("staircase_score"), 0.0)
    ret60 = _as_float(row.get("ret60_bps"), 0.0)
    ret120 = _as_float(row.get("ret120_bps"), 0.0)
    spread = _as_float(row.get("spread_bps"), 0.0)
    base_score = staircase_score + (0.6 * max(0.0, ret60)) + (0.25 * max(0.0, ret120)) - (0.25 * spread)
    if fast_score > 0.0:
        return max(fast_score, base_score)
    return base_score


def _fast_track_rank(row: dict) -> float:
    return max(_impulse_rank(row), _staircase_rank(row))


def _fast_track_strategy_choice(row: dict) -> tuple[str, float]:
    breakout_score = _strategy_score(row, "breakout")
    staircase_score = _strategy_score(row, "staircase")
    if staircase_score > breakout_score:
        return "staircase", staircase_score
    return "breakout", breakout_score


def _fast_track_allowed(row: dict) -> bool:
    if bool(row.get("keep_open")):
        return True
    _, strategy_score = _fast_track_strategy_choice(row)
    if strategy_score <= 0.0:
        return False
    return _can_open_new_selection(row)


def _gate_penalty(row: dict) -> float:
    reason = str(row.get("gate_reason", "") or "").strip().lower()
    if not reason:
        return 0.0
    if reason in {"still_dumping", "macro_downtrend", "macro_down_context"}:
        return 220.0
    if reason in {"spread", "depth", "coin_peak_reentry_pending", "overextended"}:
        return 120.0
    if reason in {"structure_rollover", "structure_stall"}:
        return 90.0
    if reason in {"post_dump_recovery_pending", "no_macro_support_1h"}:
        return 60.0
    if reason in {"no_trend_setup_range", "no_trend_setup_uptrend"}:
        return 45.0
    return 30.0


def _strategy_gate_penalty(row: dict, strategy: str) -> float:
    base = _gate_penalty(row)
    reason = str(row.get("gate_reason", "") or "").strip().lower()
    if not reason:
        return base
    if strategy in {"breakout", "staircase"} and reason in {
        "no_macro_support_1h",
        "no_trend_setup_range",
        "no_trend_setup_uptrend",
    }:
        if not bool(row.get("macro_down_context")) and (
            bool(row.get("up_structure"))
            or bool(row.get("staircase_trend"))
            or _as_float(row.get("fast_impulse_score"), 0.0) >= 8.0
            or _as_float(row.get("staircase_score"), 0.0) >= 80.0
            or _as_float(row.get("rel60_bps"), 0.0) >= 40.0
        ):
            return base * 0.35
    if strategy == "breakout" and reason == "structure_rollover":
        if _as_float(row.get("fast_impulse_score"), 0.0) >= 18.0:
            return base * 0.55
    if strategy == "continuation" and reason in {"no_macro_support_1h", "no_trend_setup_range"}:
        return base * 1.15
    return base


def _has_true_staircase_structure(row: dict) -> bool:
    if bool(row.get("fast_staircase")) or bool(row.get("staircase_trend")):
        return True
    pullback_count = _as_float(row.get("staircase_pullback_count"), 0.0)
    positive_share = _as_float(row.get("staircase_positive_share"), 0.0)
    max_pullback_run = _as_float(row.get("staircase_max_pullback_run_bps"), 9999.0)
    staircase_score = _as_float(row.get("staircase_score"), 0.0)
    if pullback_count >= 2.0 and positive_share >= 0.56 and max_pullback_run <= 110.0:
        return True
    return (
        staircase_score >= 78.0
        and positive_share >= 0.58
        and max_pullback_run <= 95.0
    )


def _strong_staircase_selector_exception(row: dict) -> bool:
    if not _has_true_staircase_structure(row):
        return False
    if bool(row.get("macro_down_context")) or not bool(row.get("macro_up_context")):
        return False
    if not bool(row.get("up_structure")):
        return False
    if _looks_like_late_burst_entry(row):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 12.0:
        return False
    if _as_float(row.get("pos_24h_pct"), 100.0) > 88.0:
        return False
    if _as_float(row.get("staircase_positive_share"), 0.0) < 0.70:
        return False
    if _as_float(row.get("staircase_max_pullback_run_bps"), 9999.0) > 25.0:
        return False
    if _as_float(row.get("ret60_bps"), 0.0) < 45.0:
        return False
    if _as_float(row.get("current_slope_bps_h"), 0.0) < 10.0:
        return False
    return _as_float(row.get("net24_pct"), 0.0) >= -0.35


def _strong_staircase_floor_override(row: dict, strategy_scores_raw: dict[str, float]) -> bool:
    staircase_score = _as_float(strategy_scores_raw.get("staircase"), 0.0)
    if staircase_score < 90.0:
        return False
    return _strong_staircase_selector_exception(row)


def _looks_like_late_burst_entry(row: dict) -> bool:
    return (
        _as_float(row.get("fast_impulse_score"), 0.0) >= 18.0
        and _as_float(row.get("fast_ret_90s_bps"), 0.0) >= 14.0
        and _as_float(row.get("rel60_bps"), 0.0) >= 55.0
        and _as_float(row.get("staircase_pullback_count"), 0.0) < 2.0
        and _as_float(row.get("staircase_positive_share"), 0.0) < 0.60
    )


def _drawdown_from_peak_bps(row: dict) -> float:
    return max(
        0.0,
        _as_float(
            row.get("geo_drawdown_from_peak_bps", row.get("structure_drawdown_bps", row.get("drawdown_from_peak_bps", 0.0))),
            0.0,
        ),
    )


def _is_pullback_continuation_candidate(row: dict) -> bool:
    return (
        bool(row.get("up_structure"))
        and not bool(row.get("macro_down_context"))
        and _as_float(row.get("score_trend"), 0.0) >= 125.0
        and _as_float(row.get("current_slope_bps_h"), 0.0) >= 1.2
        and _as_float(row.get("ret60_bps"), 0.0) >= 14.0
        and _as_float(row.get("pos_pct"), 100.0) <= 86.0
        and 6.0 <= _drawdown_from_peak_bps(row) <= 110.0
        and _as_float(row.get("spread_bps"), 9999.0) <= 28.0
        and not _looks_like_late_burst_entry(row)
    )


def _pullback_continuation_rank(row: dict) -> float:
    score = max(0.0, _as_float(row.get("score_trend"), 0.0))
    score += 42.0 if bool(row.get("strong_continuation_context")) else 0.0
    score += 30.0 if bool(row.get("up_structure")) else 0.0
    score += 24.0 if bool(row.get("macro_up_context")) else 0.0
    score += max(0.0, _as_float(row.get("current_slope_bps_h"), 0.0)) * 0.45
    score += max(0.0, _as_float(row.get("ret60_bps"), 0.0)) * 0.35
    score += max(0.0, min(50.0, _drawdown_from_peak_bps(row) - 4.0)) * 0.55
    score += 12.0 if 35.0 <= _as_float(row.get("pos_pct"), 100.0) <= 82.0 else 0.0
    score -= max(0.0, _as_float(row.get("spread_bps"), 0.0) - 8.0) * 4.8
    score -= _strategy_gate_penalty(row, "pullback_continuation") * 0.55
    return score


def _is_breakout_retest_candidate(row: dict) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if _looks_like_late_burst_entry(row):
        return False
    return (
        bool(row.get("up_structure"))
        and _as_float(row.get("rel60_bps"), 0.0) >= 20.0
        and _as_float(row.get("fast_impulse_score"), 0.0) >= 6.0
        and 4.0 <= _drawdown_from_peak_bps(row) <= 120.0
        and _as_float(row.get("pos_pct"), 100.0) <= 91.0
    )


def _breakout_retest_rank(row: dict) -> float:
    score = _impulse_rank(row) * 0.85
    score += 28.0 if bool(row.get("up_structure")) else 0.0
    score += 18.0 if bool(row.get("macro_up_context")) else 0.0
    score += 16.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += max(0.0, min(40.0, _drawdown_from_peak_bps(row) - 4.0)) * 0.65
    score += max(0.0, _as_float(row.get("rel60_bps"), 0.0)) * 0.22
    score += max(0.0, _as_float(row.get("ret15_bps"), 0.0)) * 0.25
    score -= max(0.0, _as_float(row.get("pos_pct"), 0.0) - 88.0) * 5.5
    score -= _strategy_gate_penalty(row, "breakout_retest") * 0.42
    return score


def _is_relative_strength_candidate(row: dict) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 18.0:
        return False
    return (
        bool(row.get("up_structure"))
        and _as_float(row.get("net24_pct"), 0.0) >= 0.75
        and _as_float(row.get("rel60_bps"), 0.0) >= 25.0
        and _as_float(row.get("score_trend"), 0.0) >= 80.0
    )


def _relative_strength_rank(row: dict) -> float:
    score = max(0.0, _as_float(row.get("net24_pct"), 0.0)) * 42.0
    score += max(0.0, _as_float(row.get("rel60_bps"), 0.0)) * 1.1
    score += max(0.0, _as_float(row.get("rel15_bps"), 0.0)) * 0.75
    score += max(0.0, _as_float(row.get("score_trend"), 0.0)) * 0.24
    score += 20.0 if bool(row.get("macro_up_context")) else 0.0
    score += 18.0 if bool(row.get("up_structure")) else 0.0
    score -= max(0.0, _as_float(row.get("spread_bps"), 0.0) - 6.0) * 5.5
    score -= max(0.0, _as_float(row.get("pos_pct"), 0.0) - 90.0) * 4.5
    score -= _strategy_gate_penalty(row, "relative_strength") * 0.35
    return score


def _is_breakout_candidate(row: dict) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if bool(row.get("fast_impulse")):
        return True
    if _as_float(row.get("fast_impulse_score"), 0.0) >= 12.0:
        return True
    if (
        bool(row.get("up_structure"))
        and _as_float(row.get("fast_impulse_score"), 0.0) >= 8.0
        and _as_float(row.get("rel60_bps"), 0.0) >= 35.0
    ):
        return True
    if not bool(row.get("short_horizon_scalp_ok")):
        return (
            bool(row.get("up_structure"))
            and _as_float(row.get("structure_slope_short_bps"), 0.0) >= 0.25
            and _as_float(row.get("fast_ret_90s_bps"), 0.0) >= 6.0
            and _as_float(row.get("rel60_bps"), 0.0) >= 45.0
            and _as_float(row.get("pos_pct"), 100.0) <= 88.0
        )
    if _as_float(row.get("pos_pct"), 100.0) > 95.0:
        return False
    return (
        _as_float(row.get("ret15_bps"), 0.0) >= 18.0
        and _as_float(row.get("structure_slope_short_bps"), 0.0) >= 0.8
    )


def _breakout_rank(row: dict) -> float:
    score = _impulse_rank(row)
    score += 35.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += 20.0 if bool(row.get("strong_continuation_context")) else 0.0
    score += 22.0 if bool(row.get("up_structure")) else 0.0
    score += 15.0 if bool(row.get("macro_up_context")) else 0.0
    score += max(0.0, _as_float(row.get("rel60_bps"), 0.0)) * 0.18
    score += max(0.0, _as_float(row.get("fast_ret_90s_bps"), 0.0)) * 0.9
    score -= max(0.0, _as_float(row.get("pos_pct"), 0.0) - 95.0) * 8.0
    score -= _strategy_gate_penalty(row, "breakout") * 0.40
    return score


def _is_staircase_candidate(row: dict) -> bool:
    if bool(row.get("macro_down_context")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if _looks_like_late_burst_entry(row) and not bool(row.get("staircase_trend")):
        return False
    has_staircase_structure_context = (
        bool(row.get("up_structure"))
        or bool(row.get("staircase_trend"))
        or bool(row.get("fast_staircase"))
    )
    if not has_staircase_structure_context:
        # Keep staircase tied to an actual staircase/uptrend chart shape.
        return False
    if bool(row.get("fast_staircase")) or bool(row.get("staircase_trend")):
        return True
    if _as_float(row.get("fast_staircase_score"), 0.0) >= 8.0 and _has_true_staircase_structure(row):
        return True
    if (
        _has_true_staircase_structure(row)
        and bool(row.get("up_structure"))
        and _as_float(row.get("pos_24h_pct"), 100.0) <= 92.0
    ):
        return True
    if (
        _has_true_staircase_structure(row)
        and not bool(row.get("still_dumping"))
        and _as_float(row.get("current_slope_bps_h"), 0.0) >= 1.0
        and _as_float(row.get("ret60_bps"), 0.0) >= 18.0
        and _as_float(row.get("pos_24h_pct"), 100.0) <= 92.0
        and (
            bool(row.get("macro_up_context"))
            or bool(row.get("up_structure"))
            or _as_float(row.get("staircase_positive_share"), 0.0) >= 0.66
        )
    ):
        return True
    return (
        _has_true_staircase_structure(row)
        and (bool(row.get("short_horizon_scalp_ok")) or bool(row.get("up_structure")))
        and _as_float(row.get("ret60_bps"), 0.0) >= 24.0
        and _as_float(row.get("current_slope_bps_h"), 0.0) >= 1.5
        and _as_float(row.get("pos_24h_pct"), 100.0) <= 90.0
    )


def _staircase_strategy_rank(row: dict) -> float:
    score = _staircase_rank(row)
    score += 30.0 if bool(row.get("staircase_trend")) else 0.0
    score += 20.0 if bool(row.get("trend_ready")) else 0.0
    score += 10.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += 20.0 if bool(row.get("up_structure")) else 0.0
    score += 18.0 if _has_true_staircase_structure(row) else 0.0
    score += max(0.0, _as_float(row.get("staircase_positive_share"), 0.0) - 0.50) * 90.0
    score += max(0.0, _as_float(row.get("current_slope_bps_h"), 0.0)) * 0.6
    score += 15.0 if bool(row.get("macro_up_context")) else 0.0
    score += 12.0 if 28.0 <= _as_float(row.get("pos_pct"), 100.0) <= 84.0 else 0.0
    score -= max(0.0, _as_float(row.get("staircase_max_pullback_run_bps"), 0.0) - 85.0) * 0.35
    score -= max(0.0, _as_float(row.get("pos_24h_pct"), 0.0) - 90.0) * 5.0
    score -= 120.0 if _looks_like_late_burst_entry(row) else 0.0
    score -= _strategy_gate_penalty(row, "staircase") * 0.32
    return score


def _is_continuation_candidate(row: dict) -> bool:
    if bool(row.get("macro_down_context")) or bool(row.get("still_dumping")):
        return False
    if not _macro_watch_ok(row):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 28.0:
        return False
    if (
        not bool(row.get("keep_open"))
        and _as_float(row.get("ret15_bps"), 0.0) <= -20.0
        and _as_float(row.get("rel15_bps"), 0.0) <= -15.0
        and not bool(row.get("fast_impulse"))
        and not bool(row.get("fast_staircase"))
    ):
        # Avoid classifying obvious short-term rollover charts as continuation.
        return False
    if bool(row.get("keep_open")) or bool(row.get("eligible")):
        return True
    return (
        bool(row.get("trend_ready"))
        or bool(row.get("strong_continuation_context"))
        or _is_pullback_continuation_candidate(row)
        or (
            _has_true_staircase_structure(row)
            and bool(row.get("up_structure"))
            and _as_float(row.get("pos_pct"), 100.0) <= 88.0
        )
        or (
            _as_float(row.get("score_trend"), 0.0) >= 145.0
            and bool(row.get("up_structure"))
        )
    )


def _continuation_rank(row: dict) -> float:
    score = max(0.0, _as_float(row.get("score_trend"), 0.0))
    score += 180.0 if bool(row.get("keep_open")) else 0.0
    score += 120.0 if bool(row.get("eligible")) else 0.0
    score += 45.0 if bool(row.get("trend_ready")) else 0.0
    score += 25.0 if bool(row.get("strong_continuation_context")) else 0.0
    score += 12.0 if bool(row.get("up_structure")) else 0.0
    score += 20.0 if bool(row.get("macro_up_context")) else 0.0
    score += 10.0 if bool(row.get("short_horizon_scalp_ok")) else 0.0
    score += 22.0 if _is_pullback_continuation_candidate(row) else 0.0
    score += 16.0 if _has_true_staircase_structure(row) else 0.0
    score += max(0.0, _as_float(row.get("ret15_bps"), 0.0)) * 0.8
    score += max(0.0, _as_float(row.get("current_slope_bps_h"), 0.0)) * 0.4
    score += 10.0 if 35.0 <= _as_float(row.get("pos_pct"), 100.0) <= 82.0 else 0.0
    score -= max(0.0, _as_float(row.get("spread_bps"), 0.0) - 8.0) * 6.0
    score -= max(0.0, _as_float(row.get("pos_pct"), 0.0) - 88.0) * 5.0
    score -= 35.0 if _looks_like_late_burst_entry(row) else 0.0
    score -= _strategy_gate_penalty(row, "continuation") * 0.70
    return score


def _is_rebound_candidate(row: dict) -> bool:
    early_bottom_reversal = _is_early_bottom_reversal_candidate(row)
    if bool(row.get("still_dumping")):
        return False
    if _as_float(row.get("spread_bps"), 9999.0) > 22.0:
        return False
    if bool(row.get("macro_down_context")) and not (
        bool(row.get("post_dump_recovery_ready"))
        and bool(row.get("geo_early_liftoff_trend"))
        and not bool(row.get("rebound_in_downtrend"))
    ) and not early_bottom_reversal:
        return False
    if bool(row.get("rebound_in_downtrend")) and not early_bottom_reversal and not (
        bool(row.get("post_dump_recovery_ready"))
        and bool(row.get("geo_early_liftoff_trend"))
    ):
        return False
    return (
        bool(row.get("bottom_candidate"))
        or bool(row.get("recent_rebound_ready"))
        or bool(row.get("post_dump_recovery_ready"))
        or bool(row.get("base_ready"))
        or (bool(row.get("fresh_bottom")) and bool(row.get("higher_low_ready")))
        or _is_controlled_bottom_rebound_candidate(row)
        or early_bottom_reversal
    )


def _rebound_rank(row: dict) -> float:
    early_bottom_reversal = _is_early_bottom_reversal_candidate(row)
    score = max(0.0, _as_float(row.get("score_bottom"), 0.0))
    score += 120.0 if bool(row.get("bottom_candidate")) else 0.0
    score += 80.0 if bool(row.get("recent_rebound_ready")) else 0.0
    score += 70.0 if bool(row.get("post_dump_recovery_ready")) else 0.0
    score += 40.0 if bool(row.get("base_ready")) else 0.0
    score += 20.0 if bool(row.get("higher_low_ready")) else 0.0
    score += 40.0 if bool(row.get("fresh_bottom")) else 0.0
    score += 50.0 if bool(row.get("geo_early_liftoff_trend")) else 0.0
    score += 70.0 if early_bottom_reversal else 0.0
    score += 20.0 if bool(row.get("macro_up_context")) else 0.0
    if bool(row.get("macro_down_context")):
        score -= 15.0 if early_bottom_reversal else 60.0
    if bool(row.get("rebound_in_downtrend")):
        score -= 15.0 if early_bottom_reversal else 35.0
    score += max(0.0, 60.0 - _as_float(row.get("pos_pct"), 60.0)) * 1.5
    score -= max(0.0, _as_float(row.get("spread_bps"), 0.0) - 8.0) * 4.0
    score -= _gate_penalty(row) * 0.40
    return score


def _strategy_score(row: dict, strategy: str) -> float:
    raw = row.get("strategy_scores")
    if isinstance(raw, dict):
        try:
            return float(raw.get(strategy, 0.0) or 0.0)
        except Exception:
            return 0.0
    return 0.0


def _candidate_meta_score(row: dict) -> float:
    raw = row.get("strategy_meta_score")
    try:
        return float(raw or 0.0)
    except Exception:
        return 0.0


def _has_strategy_qualification(row: dict) -> bool:
    raw = row.get("strategy_scores")
    if isinstance(raw, dict):
        for value in raw.values():
            if _as_float(value, 0.0) > 0.0:
                return True
    return False


def _ineligible_selection_exception(row: dict) -> bool:
    if bool(row.get("keep_open")):
        return True
    if _persistent_downdrift_entry_block(row):
        return False
    if _simple_swing_selector_mode_enabled() and _simple_swing_entry_ready(row):
        return True
    if _as_float(row.get("score"), float("-inf")) >= 0.0:
        return True
    if _strong_staircase_selector_exception(row):
        return True
    gate_reason = str(row.get("gate_reason", "") or "").strip().lower()
    early_bottom_reversal_exception = (
        gate_reason == "rebound_in_downtrend"
        and bool(row.get("bottom_zone"))
        and bool(row.get("recent_rebound_ready"))
        and bool(row.get("base_ready"))
        and bool(row.get("higher_low_ready"))
        and not bool(row.get("still_dumping"))
        and _as_float(row.get("bars_since_30m_low"), 999.0) <= 6.0
        and _as_float(row.get("bars_since_swing_low"), 999.0) <= 10.0
        and _as_float(row.get("rebound_from_30m_low_bps"), 0.0) >= 12.0
    )
    if early_bottom_reversal_exception:
        return True
    strategy_primary = str(row.get("strategy_primary", "") or "").strip().lower()
    if strategy_primary and _strategy_gate_reason_allows_score_override(row, strategy_primary):
        return True
    if _is_fast_impulse(row) or _is_fast_staircase(row):
        return True
    return False


def _can_open_new_selection(row: dict) -> bool:
    if bool(row.get("keep_open")):
        return True
    if _persistent_downdrift_entry_block(row):
        return False
    if _simple_swing_selector_mode_enabled():
        return _simple_swing_entry_ready(row)
    if not bool(row.get("eligible", True)):
        return _ineligible_selection_exception(row)
    if _candidate_meta_score(row) <= 0.0:
        return False
    if not _has_strategy_qualification(row):
        return False
    strategy_primary = str(row.get("strategy_primary", "") or "").strip().lower()
    if not strategy_primary:
        return False
    return _strategy_slot_candidate_ready(row, strategy_primary)


def _profile_base_score_floor(profile_name: str) -> float:
    profile_name = str(profile_name or "").strip().lower()
    if profile_name == "scalp_uptrend":
        return 120.0
    if profile_name == "scalp_lockdown":
        return 160.0
    if profile_name == "scalp_guarded":
        return 40.0
    if profile_name == "scalp_guarded_open":
        return float("-inf")
    return float("-inf")


def _local_live_strategy_block(row: dict, strategy: str, profile_name: str) -> bool:
    profile_name = str(profile_name or "").strip().lower()
    if profile_name not in {"scalp_guarded", "scalp_guarded_open", "scalp_uptrend", "scalp_lockdown"}:
        return False
    if bool(row.get("keep_open")):
        return False
    net24_pct = _as_float(row.get("net24_pct"), 0.0)
    pos_pct = _as_float(row.get("pos_24h_pct"), _as_float(row.get("pos_pct"), 100.0))
    if strategy == "rebound":
        return not _allow_guarded_rebound_live(row, profile_name)
    if profile_name == "scalp_uptrend":
        if strategy == "relative_strength":
            if net24_pct < 0.75 or not bool(row.get("up_structure")):
                return True
            if pos_pct > 90.0:
                return True
            return False
        if strategy == "pullback_continuation":
            if net24_pct < 0.10 or not bool(row.get("up_structure")):
                return True
            if pos_pct > 86.0:
                return True
            return False
        if strategy == "breakout_retest":
            if net24_pct < 0.15:
                return True
            if pos_pct > 90.0:
                return True
            return False
        if strategy == "continuation":
            if net24_pct < 0.45:
                return True
            if not (
                bool(row.get("up_structure"))
                or bool(row.get("staircase_trend"))
                or bool(row.get("macro_up_context"))
                or bool(row.get("strong_continuation_context"))
            ):
                return True
            exceptional_continuation = (
                _as_float(row.get("score"), 0.0) >= 600.0
                and _as_float(row.get("rel15_bps"), 0.0) >= 40.0
                and _as_float(row.get("spread_bps"), 9999.0) <= 8.5
                and net24_pct >= 1.0
            )
            if pos_pct > 84.0 and not bool(row.get("fast_staircase")) and not exceptional_continuation:
                return True
            return False
        if strategy == "breakout":
            if net24_pct < 0.15:
                return True
            if pos_pct > 90.0 and not bool(row.get("fast_impulse")):
                return True
            return False
        if strategy == "staircase":
            if net24_pct < 0.10:
                return True
            if not (bool(row.get("staircase_trend")) or bool(row.get("up_structure"))):
                return True
            if pos_pct > 88.0 and not bool(row.get("fast_staircase")):
                return True
            return False
        return net24_pct < 0.0
    if strategy == "relative_strength":
        return net24_pct < 0.4 or not bool(row.get("up_structure"))
    if strategy == "pullback_continuation":
        return net24_pct < 0.0 or not bool(row.get("up_structure"))
    if strategy == "breakout_retest":
        return net24_pct < 0.0
    if strategy == "staircase":
        if _strong_staircase_selector_exception(row):
            return False
        return net24_pct < 0.0
    return net24_pct < 0.0


def _strategy_rankings_from_rows(rows: list[dict]) -> dict[str, list[dict]]:
    enabled_strategies = _enabled_strategy_names()
    rankings: dict[str, list[dict]] = {name: [] for name in enabled_strategies}
    for strategy in enabled_strategies:
        ranked_rows = [
            row
            for row in rows
            if _strategy_score(row, strategy) > 0.0
            and (bool(row.get("keep_open")) or _candidate_meta_score(row) > 0.0)
        ]
        ranked_rows.sort(
            key=lambda row: (
                _strategy_score(row, strategy),
                _candidate_meta_score(row),
                _as_float(row.get("score"), 0.0),
            ),
            reverse=True,
        )
        rankings[strategy] = ranked_rows
    return rankings


def _annotate_rows_with_strategy_views(rows: list[dict], profile_name: str) -> dict[str, list[dict]]:
    enabled_strategies = _enabled_strategy_names()
    enabled_strategy_set = set(enabled_strategies)
    strategy_weight_multipliers, strategy_weight_source = _strategy_weight_multipliers()
    strategy_weights, _strategy_weights_source = _strategy_weight_overrides()
    strategy_actions, _strategy_action_source = _strategy_action_overrides()
    candidate_overrides, strategy_top_symbols, avoid_symbols, meta_override_source = _meta_symbol_overrides()
    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        strategy_scores_raw: dict[str, float] = {}
        if _is_staircase_candidate(row):
            strategy_scores_raw["staircase"] = round(_staircase_strategy_rank(row), 6)
        if _is_pullback_continuation_candidate(row):
            strategy_scores_raw["pullback_continuation"] = round(_pullback_continuation_rank(row), 6)
        if _is_breakout_retest_candidate(row):
            strategy_scores_raw["breakout_retest"] = round(_breakout_retest_rank(row), 6)
        if _is_continuation_candidate(row):
            strategy_scores_raw["continuation"] = round(_continuation_rank(row), 6)
        if _is_breakout_candidate(row):
            strategy_scores_raw["breakout"] = round(_breakout_rank(row), 6)
        if _is_relative_strength_candidate(row):
            strategy_scores_raw["relative_strength"] = round(_relative_strength_rank(row), 6)
        if _is_rebound_candidate(row):
            strategy_scores_raw["rebound"] = round(_rebound_rank(row), 6)
        strategy_scores_raw = {
            strategy: score
            for strategy, score in strategy_scores_raw.items()
            if strategy in enabled_strategy_set
            if not _local_live_strategy_block(row, strategy, profile_name)
        }
        if _simple_swing_selector_mode_enabled():
            if bool(row.get("keep_open")):
                keep_score = max(
                    _as_float(strategy_scores_raw.get("rebound"), 0.0),
                    _simple_swing_selector_score(row),
                )
                strategy_scores_raw = {"rebound": round(keep_score, 6)}
            elif (
                "rebound" in enabled_strategy_set
                and not _local_live_strategy_block(row, "rebound", profile_name)
                and _simple_swing_entry_ready(row)
            ):
                strategy_scores_raw = {
                    "rebound": round(_simple_swing_selector_score(row), 6)
                }
            else:
                strategy_scores_raw = {}

        meta_strategy_tags: list[str] = []
        for strategy in enabled_strategies:
            if _local_live_strategy_block(row, strategy, profile_name):
                continue
            if symbol not in strategy_top_symbols.get(strategy, []):
                continue
            action = strategy_actions.get(strategy, {})
            mode = str(action.get("mode") or "watch").strip().lower()
            if mode == "pause":
                continue
            slot_target = max(0, int(action.get("slot_target") or 0))
            bonus = 18.0 + (12.0 * slot_target)
            if mode == "secondary":
                bonus += 6.0
            elif mode == "primary":
                bonus += 12.0
            if strategy in strategy_scores_raw:
                strategy_scores_raw[strategy] = round(strategy_scores_raw[strategy] + bonus, 6)
                meta_strategy_tags.append(strategy)
                continue
            should_seed = False
            seed_score = 0.0
            if strategy == "breakout" and _soft_breakout_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _impulse_rank(row) * 0.25)
            elif strategy == "breakout_retest" and _soft_breakout_retest_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _breakout_retest_rank(row) * 0.18)
            elif strategy == "staircase" and _soft_staircase_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _staircase_rank(row) * 0.20)
            elif strategy == "pullback_continuation" and _soft_pullback_continuation_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _pullback_continuation_rank(row) * 0.16)
            elif strategy == "continuation" and _soft_continuation_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _continuation_rank(row) * 0.15)
            elif strategy == "relative_strength" and _soft_relative_strength_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _relative_strength_rank(row) * 0.18)
            elif strategy == "rebound" and _soft_rebound_hint(row):
                should_seed = True
                seed_score = bonus + max(0.0, _rebound_rank(row) * 0.15)
            if should_seed and seed_score > 0.0:
                strategy_scores_raw[strategy] = round(seed_score, 6)
                meta_strategy_tags.append(strategy)

        meta_avoid_symbol = symbol in avoid_symbols and not bool(row.get("keep_open"))
        if meta_avoid_symbol:
            strategy_scores_raw = {}
            meta_strategy_tags = []
        base_score_floor = _profile_base_score_floor(profile_name)
        if (
            not bool(row.get("keep_open"))
            and _as_float(row.get("score"), 0.0) < base_score_floor
        ):
            if _strong_staircase_floor_override(row, strategy_scores_raw):
                staircase_score = strategy_scores_raw.get("staircase")
                strategy_scores_raw = (
                    {"staircase": staircase_score}
                    if staircase_score is not None
                    else {}
                )
                meta_strategy_tags = [tag for tag in meta_strategy_tags if tag == "staircase"]
            else:
                strategy_scores_raw = {}
                meta_strategy_tags = []

        strategy_scores = {
            strategy: round(score * strategy_weight_multipliers.get(strategy, 1.0), 6)
            for strategy, score in strategy_scores_raw.items()
        }
        ordered = sorted(strategy_scores.items(), key=lambda item: item[1], reverse=True)
        ordered_raw = sorted(strategy_scores_raw.items(), key=lambda item: item[1], reverse=True)
        primary = ordered[0][0] if ordered else ""
        primary_score = ordered[0][1] if ordered else 0.0
        meta_score = ordered[0][1] if ordered else 0.0
        if len(ordered) > 1:
            meta_score += 0.18 * sum(score for _, score in ordered[1:])
        meta_boost = 0.0
        if symbol in candidate_overrides:
            meta_boost += 24.0
        if meta_strategy_tags:
            meta_boost += 10.0 * len(meta_strategy_tags)
        meta_score += meta_boost
        if meta_avoid_symbol:
            meta_score -= 2400.0

        row["strategy_scores_raw"] = strategy_scores_raw
        row["strategy_scores"] = strategy_scores
        row["strategy_tags"] = [name for name, _ in ordered]
        row["strategy_primary"] = primary
        row["strategy_primary_score"] = round(primary_score, 6)
        row["strategy_meta_score"] = round(meta_score, 6)
        row["strategy_primary_raw"] = ordered_raw[0][0] if ordered_raw else ""
        row["strategy_primary_score_raw"] = round(ordered_raw[0][1], 6) if ordered_raw else 0.0
        row["strategy_weight_multipliers"] = strategy_weight_multipliers
        row["strategy_weight_source"] = strategy_weight_source
        row["strategy_weights"] = strategy_weights
        row["meta_candidate_override"] = symbol in candidate_overrides
        row["meta_avoid_symbol"] = meta_avoid_symbol
        row["meta_strategy_top_tags"] = meta_strategy_tags
        row["meta_override_source"] = meta_override_source

    return _strategy_rankings_from_rows(rows)


def _strategy_slot_plan(profile_name: str, top: int) -> list[str]:
    enabled_strategies = _enabled_strategy_names()
    enabled_strategy_set = set(enabled_strategies)
    raw_override = str(os.environ.get("ROTATION_STRATEGY_SLOT_PLAN", "") or "").strip()
    if raw_override:
        override = [
            item.strip().lower()
            for item in raw_override.split(",")
            if item.strip().lower() in enabled_strategy_set
        ]
        if override:
            plan: list[str] = []
            while len(plan) < max(1, int(top)):
                plan.extend(override)
            return _inject_guarded_rebound_slot(plan, profile_name, top)
    action_plan = _strategy_slot_plan_from_actions(top)
    if action_plan:
        return _inject_guarded_rebound_slot(action_plan, profile_name, top)
    cycle = [
        strategy
        for strategy in PROFILE_STRATEGY_CYCLES.get(profile_name, PROFILE_STRATEGY_CYCLES["default"])
        if strategy in enabled_strategy_set
    ]
    for strategy in enabled_strategies:
        if strategy not in cycle:
            cycle.append(strategy)
    if not cycle:
        return []
    plan: list[str] = []
    while len(plan) < max(1, int(top)):
        plan.extend(cycle)
    return _inject_guarded_rebound_slot(plan, profile_name, top)


def _strategy_slot_plan_source(profile_name: str) -> str:
    raw_override = str(os.environ.get("ROTATION_STRATEGY_SLOT_PLAN", "") or "").strip()
    if raw_override:
        return "env_override"
    if _strategy_slot_plan_from_actions(1):
        return "env_actions"
    return f"profile:{profile_name}"


def _serialize_strategy_rankings(rankings: dict[str, list[dict]], limit: int = STRATEGY_RANK_LIMIT) -> dict[str, list[dict]]:
    enabled_strategies = _enabled_strategy_names()
    payload: dict[str, list[dict]] = {}
    for strategy in enabled_strategies:
        entries: list[dict] = []
        for row in rankings.get(strategy, [])[: max(1, int(limit))]:
            entries.append(
                {
                    "symbol": str(row.get("symbol", "")).upper(),
                    "score": round(_strategy_score(row, strategy), 6),
                    "raw_score": round(_as_float((row.get("strategy_scores_raw") or {}).get(strategy), 0.0), 6),
                    "weight_mult": round(_as_float((row.get("strategy_weight_multipliers") or {}).get(strategy), 1.0), 6),
                    "primary": str(row.get("strategy_primary", "") or ""),
                    "meta_score": round(_candidate_meta_score(row), 6),
                    "coin_experience_score": round(_as_float(row.get("coin_experience_score"), 0.0), 6),
                    "gate_reason": str(row.get("gate_reason", "") or ""),
                    "eligible": bool(row.get("eligible")),
                    "keep_open": bool(row.get("keep_open")),
                }
            )
        payload[strategy] = entries
    return payload


def _write_start_script(selected: list[str]) -> None:
    head = """#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd)\"
cd \"$ROOT_DIR\"

mkdir -p logs

wait_http() {
  local url=\"$1\"
  local deadline_s=\"${2:-20}\"
  local start_ts
  start_ts=\"$(date +%s)\"
  while true; do
    if curl -fsS -m 0.5 \"$url\" >/dev/null 2>&1; then
      return 0
    fi
    if (( \"$(date +%s)\" - start_ts >= deadline_s )); then
      return 1
    fi
    sleep 1
  done
}

start_lane() {
  local name=\"$1\"
  local config=\"$2\"
  local control_port=\"$3\"
  local exec_port=\"$4\"
  local journal_port=\"$5\"
  local core_port=\"$6\"
  local md_port=\"$7\"

  local pid_file=\"logs/${name}_rotation_guard.pid\"
  local child_pid_file=\"logs/${name}_rotation_guard.child.pid\"
  local disable_file=\"logs/${name}_rotation_guard.disabled\"
  local guard_log=\"logs/${name}_rotation_guard.log\"

  rm -f \"$disable_file\"

  if [[ -f \"$pid_file\" ]]; then
    local pid
    pid=\"$(cat \"$pid_file\" 2>/dev/null || true)\"
    if [[ -n \"$pid\" ]] && kill -0 \"$pid\" 2>/dev/null; then
      echo \"${name}: already running (guard pid=$pid)\"
      return 0
    fi
    rm -f \"$pid_file\"
  fi

  if command -v setsid >/dev/null 2>&1; then
    setsid env \\
      MODE=live \\
      CONFIG=\"$config\" \\
      START_IMPACT_CONSOLE=0 \\
      START_JOURNAL_GUI=0 \\
      START_CORE_GUI=0 \\
      START_MD_GUI=0 \\
      START_EXEC=0 \\
      CONTROL_PORT=\"$control_port\" \\
      EXEC_PORT=\"$exec_port\" \\
      JOURNAL_GUI_PORT=\"$journal_port\" \\
      CORE_GUI_PORT=\"$core_port\" \\
      MD_GUI_PORT=\"$md_port\" \\
      GUARD_LOG=\"$guard_log\" \\
      PID_FILE=\"$pid_file\" \\
      CHILD_PID_FILE=\"$child_pid_file\" \\
      DISABLE_FILE=\"$disable_file\" \\
      ./scripts/live_guard.sh >> \"$guard_log\" 2>&1 < /dev/null &
  else
    nohup env \\
      MODE=live \\
      CONFIG=\"$config\" \\
      START_IMPACT_CONSOLE=0 \\
      START_JOURNAL_GUI=0 \\
      START_CORE_GUI=0 \\
      START_MD_GUI=0 \\
      START_EXEC=0 \\
      CONTROL_PORT=\"$control_port\" \\
      EXEC_PORT=\"$exec_port\" \\
      JOURNAL_GUI_PORT=\"$journal_port\" \\
      CORE_GUI_PORT=\"$core_port\" \\
      MD_GUI_PORT=\"$md_port\" \\
      GUARD_LOG=\"$guard_log\" \\
      PID_FILE=\"$pid_file\" \\
      CHILD_PID_FILE=\"$child_pid_file\" \\
      DISABLE_FILE=\"$disable_file\" \\
      ./scripts/live_guard.sh >> \"$guard_log\" 2>&1 < /dev/null &
  fi

  sleep 1
  local pid
  pid=\"$(cat \"$pid_file\" 2>/dev/null || true)\"
  if [[ -z \"$pid\" ]]; then
    echo \"${name}: start requested; guard pid not yet written (check $guard_log)\" >&2
    return 1
  fi

  if wait_http \"http://127.0.0.1:${control_port}/health\" 25; then
    echo \"${name}: started (guard pid=$pid, control=http://127.0.0.1:${control_port}/)\"
  else
    echo \"${name}: guard running (pid=$pid), but control did not become ready yet (check $guard_log)\" >&2
    return 1
  fi
}

"""
    tail_lines = []
    for symbol in selected:
        ports = PORTS[symbol]
        slug = symbol.lower()
        tail_lines.append(
            f"start_lane \"{slug}\" \"configs/live_binance_{slug}_usdc_rotation.yaml\" "
            f"{ports[0]} {ports[1]} {ports[2]} {ports[3]} {ports[4]}"
        )
    START_SCRIPT.write_text(head + "\n".join(tail_lines) + "\n", encoding="utf-8")
    START_SCRIPT.chmod(0o755)


def _choose_selected(
    rows: list[dict],
    previous: list[str],
    previous_selected_since: dict[str, str],
    top: int,
    profile_name: str,
    switch_margin_score: float,
    min_active_minutes: float,
    active_retain_min_score: float,
    max_retain_position_pct: float,
    generated_at: datetime,
    previous_selected_strategy_map: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    top = max(1, int(top))
    row_map = {str(row["symbol"]).upper(): row for row in rows}
    previous_selected_strategy_map = {
        str(key).upper(): str(value or "").strip().lower()
        for key, value in (previous_selected_strategy_map or {}).items()
        if str(key).strip()
    }

    def _resolved_strategy(symbol: str, proposed_strategy: str | None) -> str:
        row = row_map.get(symbol)
        proposed = str(proposed_strategy or (row or {}).get("strategy_primary") or "continuation").strip().lower()
        previous_strategy = previous_selected_strategy_map.get(symbol, "")
        if row is None or not previous_strategy or previous_strategy == proposed:
            return proposed
        if previous_strategy not in _enabled_strategy_names():
            return proposed
        if not _strategy_slot_candidate_ready(row, previous_strategy):
            return proposed
        previous_score = _strategy_score(row, previous_strategy)
        proposed_score = _strategy_score(row, proposed)
        if previous_score <= 0.0:
            return proposed
        effective_switch_margin = switch_margin_score + _sticky_switch_margin_bonus(
            symbol,
            previous_selected_since,
            generated_at,
        )
        effective_switch_margin += _selector_strategy_switch_margin_bonus()
        previous_alpha = build_selected_alpha_map({symbol: previous_strategy}).get(symbol, "")
        proposed_alpha = build_selected_alpha_map({symbol: proposed}).get(symbol, "")
        if previous_alpha and proposed_alpha and previous_alpha != proposed_alpha:
            effective_switch_margin += _selector_alpha_switch_margin_bonus()
        if proposed_score <= previous_score + effective_switch_margin:
            return previous_strategy
        return proposed

    def _mark_selected(symbol: str, strategy: str, path: str) -> None:
        row = row_map.get(symbol)
        if row is None:
            return
        row["selected_active"] = True
        row["selected_strategy"] = str(strategy or row.get("strategy_primary") or "")
        row["selection_path"] = str(path or "")

    def _new_selection_position_ok(row: dict) -> bool:
        if bool(row.get("keep_open")):
            return True
        if _strong_staircase_selector_exception(row):
            return True
        return float(row.get("pos_pct", 100.0)) <= max_retain_position_pct

    for row in row_map.values():
        row["selected_active"] = False
        row["selected_strategy"] = ""
        row["selection_path"] = ""

    if _meta_risk_mode() == "stop_new_entries":
        selected: list[str] = []
        selected_strategy_map: dict[str, str] = {}
        for symbol in previous:
            row = row_map.get(symbol)
            if row is None or not bool(row.get("keep_open")):
                continue
            selected.append(symbol)
            selected_strategy_map[symbol] = _resolved_strategy(
                symbol,
                str(row.get("strategy_primary", "") or "keep_open"),
            )
            _mark_selected(symbol, selected_strategy_map[symbol], "keep_open")
            if len(selected) >= top:
                return selected[:top], selected_strategy_map, list(selected_strategy_map.values())
        for symbol, row in row_map.items():
            if symbol in selected or not bool(row.get("keep_open")):
                continue
            selected.append(symbol)
            selected_strategy_map[symbol] = _resolved_strategy(
                symbol,
                str(row.get("strategy_primary", "") or "keep_open"),
            )
            _mark_selected(symbol, selected_strategy_map[symbol], "keep_open")
            if len(selected) >= top:
                break
        return selected[:top], selected_strategy_map, list(selected_strategy_map.values())

    rankings = {
        strategy: [
            str(row["symbol"]).upper()
            for row in sorted(
                [row for row in rows if _strategy_score(row, strategy) > 0.0],
                key=lambda row: (
                    _strategy_score(row, strategy),
                    _candidate_meta_score(row),
                    _as_float(row.get("score"), 0.0),
                ),
                reverse=True,
            )
        ]
        for strategy in _enabled_strategy_names()
    }
    pinned = [
        symbol
        for symbol in previous
        if symbol in row_map and bool(row_map[symbol].get("keep_open"))
    ]
    previous_set = set(previous)
    for symbol, row in row_map.items():
        if bool(row.get("keep_open")) and symbol not in pinned:
            pinned.append(symbol)

    pinned = pinned[:top]
    selected = list(pinned)
    selected_strategy_map: dict[str, str] = {}
    strategy_sequence_used: list[str] = []
    for symbol in selected:
        selected_strategy_map[symbol] = _resolved_strategy(
            symbol,
            str(row_map[symbol].get("strategy_primary", "") or "keep_open"),
        )
        _mark_selected(symbol, selected_strategy_map[symbol], "keep_open")
    available = sorted(
        [
            str(row["symbol"]).upper()
            for row in rows
            if str(row["symbol"]).upper() not in selected
            and _new_selection_position_ok(row)
            and _can_open_new_selection(row)
            and _strategy_slot_candidate_ready(
                row,
                str(row.get("strategy_primary", "") or "continuation"),
            )
        ],
        key=lambda symbol: (
            _candidate_meta_score(row_map[symbol]),
            _as_float(row_map[symbol].get("score"), 0.0),
        ),
        reverse=True,
    )
    previous_free = [
        symbol
        for symbol in previous
        if symbol not in selected and symbol in row_map
    ]

    fast_track_limit = max(0, PROFILE_FAST_TRACK_LIMITS.get(profile_name, 1))
    fast_candidates = sorted(
        [
            symbol
            for symbol, row in row_map.items()
            if symbol not in selected
            and _new_selection_position_ok(row)
            and (_is_fast_impulse(row) or _is_fast_staircase(row))
            and _fast_track_allowed(row)
        ],
        key=lambda symbol: (
            max(
                _strategy_score(row_map[symbol], "breakout"),
                _strategy_score(row_map[symbol], "staircase"),
            ),
            _fast_track_rank(row_map[symbol]),
        ),
        reverse=True,
    )
    for symbol in fast_candidates[:fast_track_limit]:
        if symbol in selected:
            continue
        fast_strategy, _ = _fast_track_strategy_choice(row_map[symbol])
        selected.append(symbol)
        selected_strategy_map[symbol] = _resolved_strategy(symbol, fast_strategy)
        _mark_selected(symbol, selected_strategy_map[symbol], "fast_track")
        strategy_sequence_used.append(selected_strategy_map[symbol])
        if len(selected) >= top:
            return selected[:top], selected_strategy_map, strategy_sequence_used

    min_hold_seconds = max(0.0, float(min_active_minutes)) * 60.0
    if min_hold_seconds > 0.0:
        for symbol in previous_free:
            if symbol in selected:
                continue
            row = row_map[symbol]
            if not _active_candidate_gate(row):
                continue
            if not bool(row.get("eligible", True)) and not _ineligible_selection_exception(row):
                continue
            if bool(row.get("recent_live_selection_block")) and not bool(row.get("keep_open")):
                continue
            since_raw = previous_selected_since.get(symbol)
            since_ts = _parse_timestamp(since_raw)
            if since_ts is None:
                continue
            active_seconds = max(0.0, (generated_at - since_ts).total_seconds())
            if active_seconds >= min_hold_seconds:
                continue
            if float(row.get("pos_pct", 100.0)) > max_retain_position_pct:
                continue
            if not bool(row.get("keep_open")) and _candidate_meta_score(row) < active_retain_min_score:
                continue
            selected.append(symbol)
            selected_strategy_map[symbol] = _resolved_strategy(
                symbol,
                str(row.get("strategy_primary", "") or "continuation"),
            )
            _mark_selected(symbol, selected_strategy_map[symbol], "sticky_retention")
            strategy_sequence_used.append(selected_strategy_map[symbol])
            if len(selected) >= top:
                return selected[:top], selected_strategy_map, strategy_sequence_used

    strategy_plan = _strategy_slot_plan(profile_name, top)
    used_strategies = {value for value in selected_strategy_map.values() if value}
    for strategy in strategy_plan:
        if len(selected) >= top:
            break
        active_strategy = strategy
        if strategy in used_strategies:
            alternative = next(
                (
                    name
                    for name in strategy_plan
                    if name not in used_strategies
                    and any(symbol not in selected for symbol in rankings.get(name, []))
                ),
                None,
            )
            if alternative is not None:
                active_strategy = alternative
        challenger = next(
            (
                symbol
                for symbol in rankings.get(active_strategy, [])
                if symbol not in selected
                and _new_selection_position_ok(row_map[symbol])
                and (
                    bool(row_map[symbol].get("eligible", True))
                    or _ineligible_selection_exception(row_map[symbol])
                )
                and _strategy_slot_candidate_ready(row_map[symbol], active_strategy)
                and (
                    bool(row_map[symbol].get("keep_open"))
                    or _candidate_meta_score(row_map[symbol]) > 0.0
                )
            ),
            None,
        )

        keep_current = None
        for symbol in previous_free:
            if symbol in selected:
                continue
            row = row_map[symbol]
            if not bool(row.get("eligible", True)) and not _ineligible_selection_exception(row):
                continue
            if not _strategy_slot_candidate_ready(row, active_strategy):
                continue
            if float(row.get("pos_pct", 100.0)) > max_retain_position_pct:
                continue
            if not bool(row.get("keep_open")) and _candidate_meta_score(row) < active_retain_min_score:
                continue
            if _strategy_score(row, active_strategy) <= 0.0 and str(row.get("strategy_primary", "") or "") != active_strategy:
                continue
            keep_current = symbol
            break

        if challenger is None:
            if keep_current is None:
                continue
            selected.append(keep_current)
            selected_strategy_map[keep_current] = _resolved_strategy(keep_current, active_strategy)
            _mark_selected(keep_current, selected_strategy_map[keep_current], "strategy_plan_keep")
            strategy_sequence_used.append(selected_strategy_map[keep_current])
            used_strategies.add(selected_strategy_map[keep_current])
            continue

        if keep_current is not None:
            challenger_score = max(
                _strategy_score(row_map[challenger], active_strategy),
                _candidate_meta_score(row_map[challenger]),
            )
            current_score = max(
                _strategy_score(row_map[keep_current], active_strategy),
                _candidate_meta_score(row_map[keep_current]),
            )
            effective_switch_margin = switch_margin_score + _sticky_switch_margin_bonus(
                keep_current,
                previous_selected_since,
                generated_at,
            )
            if challenger_score <= current_score + effective_switch_margin:
                selected.append(keep_current)
                selected_strategy_map[keep_current] = _resolved_strategy(keep_current, active_strategy)
                _mark_selected(keep_current, selected_strategy_map[keep_current], "strategy_plan_keep")
                strategy_sequence_used.append(selected_strategy_map[keep_current])
                used_strategies.add(selected_strategy_map[keep_current])
                continue

        selected.append(challenger)
        selected_strategy_map[challenger] = _resolved_strategy(challenger, active_strategy)
        _mark_selected(challenger, selected_strategy_map[challenger], "strategy_plan")
        strategy_sequence_used.append(selected_strategy_map[challenger])
        used_strategies.add(selected_strategy_map[challenger])

    while len(selected) < top:
        challenger = next((symbol for symbol in available if symbol not in selected), None)

        keep_current = None
        for symbol in previous_free:
            if symbol in selected:
                continue
            row = row_map[symbol]
            if not bool(row.get("eligible", True)) and not _ineligible_selection_exception(row):
                continue
            if not _can_open_new_selection(row):
                continue
            if bool(row.get("recent_live_selection_block")) and not bool(row.get("keep_open")):
                continue
            if float(row.get("pos_pct", 100.0)) > max_retain_position_pct:
                continue
            if not bool(row.get("keep_open")) and _candidate_meta_score(row) < active_retain_min_score:
                continue
            keep_current = symbol
            break

        if challenger is None:
            if keep_current is None:
                break
            selected.append(keep_current)
            selected_strategy_map[keep_current] = _resolved_strategy(
                keep_current,
                str(row_map[keep_current].get("strategy_primary", "") or "continuation"),
            )
            _mark_selected(keep_current, selected_strategy_map[keep_current], "available_keep")
            continue

        if keep_current is not None:
            challenger_score = _candidate_meta_score(row_map[challenger])
            current_score = _candidate_meta_score(row_map[keep_current])
            effective_switch_margin = switch_margin_score + _sticky_switch_margin_bonus(
                keep_current,
                previous_selected_since,
                generated_at,
            )
            if challenger_score <= current_score + effective_switch_margin:
                selected.append(keep_current)
                selected_strategy_map[keep_current] = _resolved_strategy(
                    keep_current,
                    str(row_map[keep_current].get("strategy_primary", "") or "continuation"),
                )
                _mark_selected(keep_current, selected_strategy_map[keep_current], "available_keep")
                continue

        selected.append(challenger)
        selected_strategy_map[challenger] = _resolved_strategy(
            challenger,
            str(row_map[challenger].get("strategy_primary", "") or "continuation"),
        )
        _mark_selected(challenger, selected_strategy_map[challenger], "available_fill")

    return selected[:top], selected_strategy_map, strategy_sequence_used


def build_selector_payload(
    *,
    top: int,
    watch_top: int,
    profile_name: str,
    switch_margin_score: float,
    min_active_minutes: float,
    active_retain_min_score: float,
    max_retain_position_pct: float,
    selector_result: dict | None = None,
    previous_payload: dict | None = None,
    generated_at: datetime | None = None,
    persist_runtime: bool = False,
) -> dict:
    profile = _profile_values(profile_name)
    strategy_weights, strategy_weight_source = _strategy_weight_overrides()
    strategy_weight_multipliers, _strategy_weight_multiplier_source = _strategy_weight_multipliers()
    fixed_watch_symbols = _env_symbols("ROTATION_FIXED_WATCH_SYMBOLS")
    if _selector_bypass_watch_pool_enabled():
        fixed_watch_symbols = []

    result = dict(selector_result) if isinstance(selector_result, dict) else _run_selector()
    rows = result.get("rows", [])
    fallback_rows_used = False
    if not isinstance(rows, list) or not rows:
        previous_rows = _load_previous_rows_snapshot()
        if previous_rows:
            rows = previous_rows
            fallback_rows_used = True
        else:
            raise RuntimeError(
                "rotation selector returned no rows; keeping previous rotation_active_lanes.json"
            )
    rows = _augment_rows_with_fast_scout([dict(row) for row in rows if isinstance(row, dict)])
    rows = [
        row
        for row in rows
        if not _is_excluded_base_symbol(str(row.get("symbol", "")).upper())
    ]
    rows = _augment_rows_with_live_decisions(rows)
    rows = _apply_simple_swing_row_gates(rows)
    if fixed_watch_symbols:
        fixed_watch_set = set(fixed_watch_symbols)
        rows = [
            row
            for row in rows
            if str(row.get("symbol", "")).upper() in fixed_watch_set
        ]
    preferred_raw = result.get("selected") or []
    preferred_symbols: list[str] = []
    for item in preferred_raw:
        if isinstance(item, dict):
            symbol = str(item.get("symbol", "")).upper()
        else:
            symbol = str(item).upper()
        if symbol and symbol in PORTS and symbol not in preferred_symbols:
            preferred_symbols.append(symbol)
    if preferred_symbols:
        preferred_index = {symbol: idx for idx, symbol in enumerate(preferred_symbols)}

        def _row_key(row: dict) -> tuple[int, int, float]:
            symbol = str(row.get("symbol", "")).upper()
            if symbol in preferred_index:
                return (0, preferred_index[symbol], -float(row.get("score", 0.0)))
            return (1, 9999, -float(row.get("score", 0.0)))

        rows = sorted(rows, key=_row_key)
    generated_at = generated_at if generated_at is not None else datetime.now(timezone.utc)
    strategy_rankings_rows = _annotate_rows_with_strategy_views(rows, profile_name)
    coin_experience_priors, coin_experience_prior = _load_coin_experience_priors(
        now=generated_at,
        quote_asset=str(result.get("quote_asset", "USDC") or "USDC"),
    )
    _apply_coin_experience_priors(
        rows,
        coin_experience_priors,
        enabled=bool(coin_experience_prior.get("enabled", False)),
    )
    strategy_rankings_rows = _strategy_rankings_from_rows(rows)
    previous_payload = previous_payload if isinstance(previous_payload, dict) else _load_previous_payload()
    previous, previous_selected_since, previous_watch_symbols = _load_previous_state(previous_payload)
    previous_selected_strategy_map = {
        str(key).upper(): str(value or "").strip().lower()
        for key, value in (previous_payload.get("selected_strategy_map") or {}).items()
        if str(key).strip()
    }
    candidate_rows = [row for row in rows if _active_candidate_gate(row)]
    selection_rows = list(candidate_rows)
    if len(selection_rows) < max(1, int(top)):
        for row in rows:
            if row in selection_rows:
                continue
            if bool(row.get("keep_open")) or _macro_watch_ok(row):
                selection_rows.append(row)
    if len(selection_rows) < max(1, int(top)):
        selection_rows = list(rows)
    selected, selected_strategy_map, strategy_sequence_used = _choose_selected(
        rows=selection_rows,
        previous=previous,
        previous_selected_since=previous_selected_since,
        top=top,
        profile_name=profile_name,
        switch_margin_score=switch_margin_score,
        min_active_minutes=min_active_minutes,
        active_retain_min_score=active_retain_min_score,
        max_retain_position_pct=max_retain_position_pct,
        generated_at=generated_at,
        previous_selected_strategy_map=previous_selected_strategy_map,
    )
    row_map = {str(row["symbol"]).upper(): row for row in rows}
    watch_candidates = [
        row
        for row in rows
        if bool(row.get("keep_open"))
        or _macro_watch_ok(row)
    ]
    watch_set = set()
    watch_symbols: list[str] = []
    for symbol in selected:
        if symbol in PORTS and symbol not in watch_set:
            watch_symbols.append(symbol)
            watch_set.add(symbol)
    for symbol in previous_watch_symbols:
        if symbol not in PORTS or symbol in watch_set:
            continue
        row = row_map.get(symbol)
        if row is None:
            continue
        if (
            bool(row.get("keep_open"))
            or _macro_watch_ok(row)
        ):
            watch_symbols.append(symbol)
            watch_set.add(symbol)
    for row in watch_candidates:
        symbol = str(row.get("symbol", "")).upper()
        if symbol and symbol in PORTS and symbol not in watch_set:
            watch_symbols.append(symbol)
            watch_set.add(symbol)
    watch_top = int(watch_top)
    if watch_top > 0:
        keep = max(len(selected), watch_top)
        watch_symbols = watch_symbols[:keep]
    if fixed_watch_symbols:
        fixed_watch_set = set(fixed_watch_symbols)
        for symbol in selected:
            if symbol not in fixed_watch_set:
                fixed_watch_symbols.append(symbol)
                fixed_watch_set.add(symbol)
        watch_symbols = fixed_watch_symbols
    if _watch_all_pool_enabled():
        # Two-level mode: keep every universe symbol running in watch, while
        # selected slots alone control automatic new entries.
        watch_symbols = [symbol for symbol in POOL if symbol in PORTS]
    if not watch_symbols:
        watch_symbols = list(selected)

    # Keep risk budget anchored to configured slot count (top) so capital is always
    # split across the full lane plan (e.g. 8 slots => 12.5% per active lane),
    # even when fewer symbols are currently selected.
    slot_denominator = max(1, int(top))
    fraction = (1.0 / float(slot_denominator)) if selected else 0.0
    selected_fraction_map: dict[str, float] = {}
    for symbol in POOL:
        symbol_fraction = fraction if symbol in selected else 0.0
        if symbol_fraction > 0.0 and _macro_fraction_adjust_enabled():
            row = row_map.get(symbol, {})
            macro_class = str(row.get("macro_trend_class", "")).strip().lower()
            if macro_class == "neutral":
                symbol_fraction *= float(profile.get("neutral_fraction_mult", 1.0))
            elif macro_class == "down":
                symbol_fraction *= float(profile.get("down_fraction_mult", 1.0))
        selected_fraction_map[symbol] = symbol_fraction
        if persist_runtime:
            _set_fraction(symbol, symbol_fraction, profile, row_map.get(symbol, {}))

    if persist_runtime:
        _write_start_script(watch_symbols)

    selected_rows = [row_map[symbol] for symbol in selected if symbol in row_map]
    previous_set = set(previous)
    selected_since = {}
    now_iso = generated_at.isoformat()
    for symbol in selected:
        if symbol in previous_set:
            selected_since[symbol] = previous_selected_since.get(symbol, now_iso)
        else:
            selected_since[symbol] = now_iso

    payload = {
        "ok": True,
        "generated_at": now_iso,
        "selected": selected,
        "enabled_strategies": list(_enabled_strategy_names()),
        "watch_symbols": watch_symbols,
        "selected_since": selected_since,
        "fraction": fraction,
        "profile": profile_name,
        "switch_margin_score": switch_margin_score,
        "min_active_minutes": min_active_minutes,
        "active_retain_min_score": active_retain_min_score,
        "max_retain_position_pct": max_retain_position_pct,
        "watch_top": watch_top,
        "profile_values": profile,
        "strategy_weights": strategy_weights,
        "strategy_weight_multipliers": strategy_weight_multipliers,
        "strategy_weight_source": strategy_weight_source,
        "selected_fraction_map": selected_fraction_map,
        "runtime_config_version": ROTATION_RUNTIME_CONFIG_VERSION,
        "strategy_slot_plan": _strategy_slot_plan(profile_name, top),
        "strategy_slot_plan_source": _strategy_slot_plan_source(profile_name),
        "selected_strategy_map": selected_strategy_map,
        "selected_alpha_map": build_selected_alpha_map(selected_strategy_map),
        "selected_strategy_sequence": strategy_sequence_used,
        "strategy_rankings": _serialize_strategy_rankings(strategy_rankings_rows),
        "coin_experience_prior": coin_experience_prior,
        "selection_rows_total": len(selection_rows),
        "selection_relaxed": len(candidate_rows) < max(1, int(top)),
        "active_candidate_count": len(candidate_rows),
        "selector_row_source": str(result.get("row_source", "live")),
        "selector_rate_limit_detected": bool(result.get("rate_limit_detected", False)),
        "selector_fallback_rows_used": bool(fallback_rows_used or result.get("fallback_rows_used")),
        "selector_fallback_reason": str(result.get("fallback_reason", "") or ""),
        "selector_errors_total": int(result.get("errors_total", 0) or 0),
        "selector_errors_sample": result.get("errors_sample", [])[:12]
        if isinstance(result.get("errors_sample"), list)
        else [],
        "rows": selected_rows,
        "all_rows": rows,
    }
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Select the best active rotation set from the lane pool.")
    ap.add_argument("--top", type=int, default=4, help="Number of active coins to keep trading")
    ap.add_argument(
        "--watch-top",
        type=int,
        default=0,
        help="Number of preselected watch lanes to run (0 = all watch candidates).",
    )
    ap.add_argument(
        "--profile",
        type=str,
        default="default",
        choices=sorted(PROFILE_PRESETS.keys()),
        help="Config profile to apply to rotation lanes",
    )
    ap.add_argument(
        "--switch-margin-score",
        type=float,
        default=8.0,
        help="Minimum score advantage a challenger needs before replacing an existing active lane",
    )
    ap.add_argument(
        "--min-active-minutes",
        type=float,
        default=30.0,
        help="Minimum time to keep an active lane before it may be rotated out (unless it becomes ineligible)",
    )
    ap.add_argument(
        "--active-retain-min-score",
        type=float,
        default=40.0,
        help="Keep an existing active lane sticky only while its score stays at or above this level",
    )
    ap.add_argument(
        "--max-retain-position-pct",
        type=float,
        default=88.0,
        help="If lower than 100, avoid retaining a previous active coin once it is too high in its local range",
    )
    ap.add_argument("--apply", action="store_true", help="Apply the selected active set to the running lane guards")
    ap.add_argument("--json", action="store_true", help="Print JSON only")
    args = ap.parse_args()
    previous_payload = _load_previous_payload()
    payload = build_selector_payload(
        top=args.top,
        watch_top=args.watch_top,
        profile_name=args.profile,
        switch_margin_score=args.switch_margin_score,
        min_active_minutes=args.min_active_minutes,
        active_retain_min_score=args.active_retain_min_score,
        max_retain_position_pct=args.max_retain_position_pct,
        previous_payload=previous_payload,
        persist_runtime=bool(args.apply),
    )
    if args.apply or not args.json:
        ACTIVE_FILE.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    apply_needed = _apply_signature(previous_payload) != _apply_signature(payload)
    profile_changed = _profile_signature_changed(previous_payload, payload)
    if args.apply:
        if apply_needed or profile_changed:
            apply_cmd = ["python3", "scripts/rotation_apply_active_lanes.py"]
            if profile_changed:
                apply_cmd.append("--reload-running")
            subprocess.check_call(
                apply_cmd,
                cwd=REPO_ROOT,
            )
        elif not args.json:
            print("apply: skipped (rotation unchanged)")

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return

    selected = list(payload.get("selected", []))
    watch_symbols = list(payload.get("watch_symbols", []))
    selected_rows = list(payload.get("rows", []))
    selected_strategy_map = dict(payload.get("selected_strategy_map") or {})
    fraction = float(payload.get("fraction", 0.0) or 0.0)

    print("selected:", ", ".join(selected))
    print("watch:", ", ".join(watch_symbols))
    print(f"profile: {args.profile}")
    print(f"fraction_per_active: {fraction:.6f}")
    print(f"switch_margin_score: {args.switch_margin_score:.2f}")
    print(f"max_retain_position_pct: {args.max_retain_position_pct:.1f}")
    if payload["selector_fallback_rows_used"]:
        print(
            "note: selector rows fallback active "
            f"(source={payload['selector_row_source']}, reason={payload['selector_fallback_reason']})"
        )
    if payload["selector_rate_limit_detected"]:
        print(
            "note: upstream rate-limit detected "
            f"(errors_total={payload['selector_errors_total']})"
        )
    for row in selected_rows:
        print(
            f"{row['symbol']}: score={float(row['score']):.2f} "
            f"strategy={str(selected_strategy_map.get(str(row['symbol']).upper(), row.get('strategy_primary', '')))} "
            f"ret15={float(row.get('ret15_bps', 0.0)):+.1f}bps "
            f"rel15={float(row.get('rel15_bps', 0.0)):+.1f}bps "
            f"spread={float(row.get('spread_bps', 0.0)):.1f}bps "
            f"pos={float(row['pos_pct']):.1f}% "
            f"width24={float(row['width72_pct']):.1f}% "
            f"turns24={int(float(row['turns24']))} "
            f"net24={float(row['net24_pct']):+.2f}% "
            f"open={float(row['open_notional']):.2f}"
        )


if __name__ == "__main__":
    main()
