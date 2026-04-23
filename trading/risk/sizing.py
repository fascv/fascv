from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from collections import deque
from typing import Optional

from trading.types import AccountState, Features, GateDecision, RiskDecision


@dataclass
class RiskConfig:
    max_exposure_eur: float
    vol_target_bps: float
    daily_loss_limit_eur: float
    max_drawdown_pct: float
    cooldown_bars: int
    allow_short: bool = False
    # If False, ignore ATR-based volatility scaling (use full max exposure when taking risk).
    use_vol_scaling: bool = True
    # If False, ignore GateDecision.size_factor for sizing (use full max exposure when allowed).
    use_gate_size_factor: bool = True
    # Require stronger edge to open a new long from flat; helps reduce fee-heavy churn.
    entry_edge_bps: float = 0.0
    # Require edge to exceed expected costs by this extra buffer before opening from flat.
    entry_cost_buffer_bps: float = 0.0
    # Fraction of expected costs required at entry (1.0 strict, <1.0 allows earlier scalp entries).
    entry_cost_coverage_ratio: float = 1.0
    # New long entries should usually cover the full roundtrip, not only the next execution leg.
    entry_cost_roundtrip_multiplier: float = 1.0
    # Block new entries when current ATR is too small relative to expected one-side execution cost.
    entry_min_atr_to_cost_ratio: float = 0.0
    # If True, bypass ATR/edge/cost entry checks and rely on upstream selector/alpha logic.
    disable_entry_edge_gate: bool = False
    # Extra guard for override-style entries (staircase / impulse): do not buy directly at the
    # local micro-peak without enough pullback room to realistically pay roundtrip costs.
    override_max_structure_range_pos: float = 1.0
    override_min_drawdown_from_peak_bps: float = 0.0
    override_min_drawdown_to_cost_ratio: float = 0.0
    override_min_slope_short_bps: float = -999.0
    # Extra late-burst guard for override-style entries: avoid buying already-extended
    # staircase/impulse pushes when the trend leg and local context are both too stretched.
    override_max_trend_return_bps: float = 0.0
    override_max_context_range_pos: float = 1.0
    # Generic late-entry guard for flat long entries: block top-zone buys when the move is
    # already stretched and price is still too close to the local/context peak.
    late_entry_block_context_range_pos: float = 1.0
    late_entry_block_structure_range_pos: float = 1.0
    late_entry_block_max_context_drawdown_bps: float = 0.0
    late_entry_block_min_trend_return_bps: float = 0.0
    late_entry_block_min_return_bps: float = 0.0
    # Close an existing long when edge falls below this level (can be <= 0.0).
    exit_edge_bps: float = 0.0
    # Minimum negative edge required to bypass gate and force a flattening long exit.
    # Example: -20.0 means bypass exits only when edge is <= -20 bps.
    exit_bypass_gate_edge_bps: float = 0.0
    # If True, automatic exits may only happen when the trade is already in profit.
    # Loss/safety exits stay disabled; position changes then come only from profit exits,
    # manual actions, or account sync.
    profit_only_auto_exits: bool = False
    # Keep a position for at least N bars before evaluating regular exit signals.
    min_hold_bars: int = 0
    # Enable a dedicated early-failure exit shortly after entry when the expected rebound
    # never materializes and price moves directly against the position.
    failed_start_exit_enabled: bool = False
    # Do not allow failed-start exits immediately after entry; require at least N bars first.
    failed_start_min_bars: int = 0
    # Only evaluate the failed-start exit during the first N bars after entry.
    failed_start_max_bars: int = 0
    # Require at least this many bps of favorable excursion to consider the rebound "real".
    # If the post-entry peak stays below this, the trade is treated as a failed start candidate.
    failed_start_min_rebound_bps: float = 0.0
    # Adverse move from entry that triggers the failed-start exit when no real rebound happened.
    failed_start_loss_bps: float = 0.0
    # Chop exit: if a trade has been open for a while, dipped meaningfully below entry,
    # and then only recovers back to break-even, allow a clean flatten.
    chop_break_even_reclaim_enabled: bool = False
    chop_break_even_reclaim_min_bars: int = 0
    chop_break_even_reclaim_min_drawdown_bps: float = 0.0
    # Only trigger reclaim-exit when edge is weak enough (e.g. <= 0 keeps strong trends alive).
    chop_break_even_reclaim_max_edge_bps: float = 0.0
    # Require visible chop around entry in the recent window (back-and-forth crossings).
    chop_break_even_reclaim_cross_window_bars: int = 0
    chop_break_even_reclaim_min_crosses: int = 0
    # If True, normal long exits are delayed until price covers expected exit cost plus optional profit buffer.
    # Emergency hard stops are handled outside of this rule.
    require_break_even_for_exit: bool = False
    # If True, a long that already traded above the current break-even threshold may still be closed
    # on a clear reversal signal while falling back, even if price is currently below break-even again.
    allow_reversal_exit_after_break_even: bool = False
    # After N bars in-position, flatten if price is still at/below current break-even.
    time_break_even_floor_enabled: bool = False
    time_break_even_floor_bars: int = 0
    # When in profit (at/above break-even), flatten on the first completed red rolling window.
    red_candle_exit_enabled: bool = False
    red_candle_window_bars: int = 0
    # Fast scalp take-profit: flatten after 2-3 green candles from entry.
    green_candle_take_exit_enabled: bool = False
    green_candle_take_min_bars: int = 0
    green_candle_take_max_bars: int = 0
    green_candle_take_required_green_bars: int = 0
    green_candle_take_min_profit_bps: float = 0.0
    # Extra profit buffer on top of expected exit costs before allowing a normal long exit.
    min_exit_profit_bps: float = 0.0
    # Hard long take-profit buffer (net of expected exit costs). If reached, flatten regardless of edge.
    hard_take_profit_bps: float = 0.0
    # Optional corridor-based dynamic profit target for long exits.
    # When enabled, this replaces the fixed profit target with a target derived from the recent price corridor.
    dynamic_profit_target_enabled: bool = False
    # Profit target at the recent corridor low, in bps (e.g. 500 = +5.0%).
    dynamic_profit_target_bps_at_low: float = 0.0
    # Break-even anchor measured as percent of the recent corridor range below the high
    # (e.g. 30 = high minus 30% of (high-low)).
    dynamic_profit_break_even_from_high_pct: float = 0.0
    # Hard stop-loss distance from avg entry in bps. Triggers an immediate flatten, independent of break-even rules.
    hard_stop_loss_bps: float = 0.0
    # If True, hard take-profit is only active in "range" regime (no fixed TP in trend/breakout).
    hard_take_profit_only_in_range: bool = False
    # Dynamic long trailing stop (winner protection that can keep trend trades running).
    trailing_stop_enabled: bool = False
    # Arm trailing only after price moves at least this many bps above avg_entry (+ expected exit costs).
    trailing_activation_bps: float = 0.0
    # Minimum trailing distance in bps from local peak.
    trailing_stop_bps: float = 0.0
    # Additional trailing distance as ATR multiplier (distance = max(trailing_stop_bps, atr_bps * mult)).
    trailing_stop_atr_mult: float = 0.0
    # Hold an open long through a still-intact uptrend campaign instead of repeatedly
    # clipping small wins and re-entering higher.
    campaign_hold_enabled: bool = False
    campaign_hold_min_bars: int = 0
    campaign_hold_min_profit_bps: float = 0.0
    campaign_hold_min_trend_bps: float = 0.0
    campaign_hold_max_range_pos: float = 1.0
    campaign_hold_max_drawdown_from_peak_bps: float = 0.0
    campaign_hold_min_recent_bias_bps: float = -999.0
    # Profit-lock exit based on retrace from peak profit:
    # arm after peak profit >= arm_bps, then flatten when current profit has retraced
    # retrace_pct of that peak profit (e.g. arm=400, retrace=25 => exit at <=300 bps).
    peak_profit_retrace_enabled: bool = False
    peak_profit_retrace_arm_bps: float = 0.0
    peak_profit_retrace_pct: float = 0.0
    # Rolling profit exit:
    # arm after open PnL reaches arm_eur, or when the live profit-only path
    # uses the staged base target percent as a price-based arming threshold.
    # A positive retrace_eur keeps the legacy fixed-EUR behavior; otherwise
    # retrace_pct locks a share of the peak profit.
    profit_roll_exit_enabled: bool = False
    profit_roll_arm_eur: float = 0.0
    profit_roll_retrace_eur: float = 0.0
    profit_roll_retrace_pct: float = 50.0
    profit_roll_min_retrace_eur: float = 0.02
    profit_roll_min_keep_profit_bps: float = 2.0
    # After a trailing-stop exit, block same-symbol re-entry for N completed bars.
    reentry_cooldown_bars_after_trailing_stop: int = 0
    # After a very short hard-stop-loss whipsaw, block same-symbol re-entry for N completed bars.
    reentry_cooldown_bars_after_whipsaw_stop_loss: int = 0
    # Only treat a hard stop as a whipsaw when the trade was this young or younger.
    reentry_whipsaw_hard_stop_max_bars: int = 0
    # If repeated short stop-loss exits happen inside this many bars, treat them as a loss cluster.
    reentry_loss_cluster_window_bars: int = 0
    # After a repeated short stop-loss cluster, block same-symbol re-entry for longer.
    reentry_cooldown_bars_after_loss_cluster: int = 0
    # After weak full exits such as time-break-even or failed-start, force a short
    # same-symbol cooling period before the next flat long entry.
    reentry_cooldown_bars_after_weak_exit: int = 0
    # After a full long exit, require at least this price move from exit level before re-entering long.
    reentry_min_move_bps: float = 0.0
    # If True, allow same-symbol re-entry only once price has returned to the previous
    # long campaign entry level (or lower).
    reentry_require_price_at_or_below_last_entry: bool = False
    # Optional tolerance above last entry level for the re-entry check.
    reentry_last_entry_tolerance_bps: float = 0.0
    # Ignore tiny target-position changes (in quote notional); avoids fee-heavy micro rebalances.
    rebalance_min_delta_eur: float = 0.0
    # Treat tiny leftover positions as flat (exchange dust / below minimum tradable size).
    position_epsilon_btc: float = 1e-12
    # Optional notional-based dust floor. If the position value is below this threshold, treat it as flat.
    position_epsilon_eur: float = 0.0
    # Block/cap new long entries when top-of-book notional depth is too thin.
    min_entry_depth_eur: float = 0.0
    # Maximum ratio target_notional / top_depth_notional for new long entries.
    # <= 0 disables ratio checks.
    max_entry_notional_to_depth_ratio: float = 0.0
    # If True (long-only), enforce "all-in/all-out" behavior:
    # - while in a long position, hold size until an explicit flatten/stop signal triggers
    # - no partial rebalances and no pyramiding/add-ons in-position.
    full_position_only: bool = False


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._cooldown_remaining = 0
        self._current_day: Optional[date] = None
        self._day_start_equity: Optional[float] = None
        self._last_realized_pnl: Optional[float] = None
        self._last_position_sign: int = 0
        self._bars_in_position: int = 0
        self._trail_position_sign: int = 0
        self._trail_peak_price: float = 0.0
        self._trailing_armed: bool = False
        self._position_trough_price: float = 0.0
        self._last_long_exit_price: float = 0.0
        self._last_long_entry_price: float = 0.0
        self._reentry_cooldown_remaining: int = 0
        self._short_loss_cluster_count: int = 0
        self._short_loss_cluster_age_bars: int = 0
        self._dynamic_profit_target_bps_active: float = 0.0
        self._last_effective_position_btc: float = 0.0
        self._recent_prices = deque(maxlen=64)
        self._corridor_smooth_pos_pct: float = -1.0
        self._corridor_prev_smooth_pos_pct: float = -1.0
        self._corridor_armed_stage_pct: float = 0.0
        self._corridor_armed_stage_age_bars: int = 0
        self._corridor_lowest_pos_pct: float = 0.0
        self._corridor_pending_entry_stage_pct: float = 0.0
        self._corridor_entry_stage_pct: float = 0.0
        self._corridor_exit_armed: bool = False
        self._corridor_exit_peak_pct: float = 0.0

    def _compute_dynamic_profit_target_bps(self, entry_price: float, features: Features) -> float:
        if not bool(getattr(self.config, "dynamic_profit_target_enabled", False)):
            return 0.0
        entry_price = float(entry_price or 0.0)
        if entry_price <= 0.0:
            return 0.0
        corridor_low = float(features.values.get("corridor_low_price", 0.0) or 0.0)
        corridor_high = float(features.values.get("corridor_high_price", 0.0) or 0.0)
        if corridor_low <= 0.0 or corridor_high <= corridor_low:
            return 0.0
        low_target_bps = max(0.0, float(getattr(self.config, "dynamic_profit_target_bps_at_low", 0.0) or 0.0))
        break_even_from_high_pct = max(
            0.0,
            float(getattr(self.config, "dynamic_profit_break_even_from_high_pct", 0.0) or 0.0),
        )
        if low_target_bps <= 0.0:
            return 0.0
        corridor_range = corridor_high - corridor_low
        anchor_price = corridor_high - (corridor_range * (break_even_from_high_pct / 100.0))
        if anchor_price <= corridor_low:
            return 0.0
        weight = (anchor_price - entry_price) / (anchor_price - corridor_low)
        weight = max(0.0, min(1.0, weight))
        return low_target_bps * weight

    def _update_day(self, state: AccountState) -> None:
        day = state.ts.date()
        if self._current_day != day:
            self._current_day = day
            self._day_start_equity = state.day_start_equity_eur

    def update_kill_switches(self, state: AccountState) -> Optional[str]:
        self._update_day(state)
        day_start = self._day_start_equity if self._day_start_equity is not None else state.equity_eur
        day_pnl = state.equity_eur - day_start
        if day_pnl <= -self.config.daily_loss_limit_eur:
            self._cooldown_remaining = max(self._cooldown_remaining, self.config.cooldown_bars)
            return "daily_loss_limit"
        if state.drawdown_pct >= self.config.max_drawdown_pct:
            self._cooldown_remaining = max(self._cooldown_remaining, self.config.cooldown_bars)
            return "max_drawdown"
        if self._last_realized_pnl is None:
            self._last_realized_pnl = state.realized_pnl_eur
        elif state.realized_pnl_eur < self._last_realized_pnl:
            self._cooldown_remaining = max(self._cooldown_remaining, self.config.cooldown_bars)
            self._last_realized_pnl = state.realized_pnl_eur
            return "cooldown_loss"
        self._last_realized_pnl = state.realized_pnl_eur
        return None

    def _material_position_increase(
        self,
        current_position_btc: float,
        *,
        previous_position_btc: Optional[float] = None,
    ) -> bool:
        current_qty = abs(float(current_position_btc or 0.0))
        prev_qty = abs(
            float(
                self._last_effective_position_btc
                if previous_position_btc is None
                else previous_position_btc
            )
            or 0.0
        )
        if current_qty <= 0.0 or prev_qty <= 0.0:
            return False
        qty_eps = max(1e-12, float(getattr(self.config, "position_epsilon_btc", 1e-12) or 1e-12))
        increase_qty = current_qty - prev_qty
        if increase_qty <= 0.0:
            return False
        # Treat meaningful scale-ins as a fresh entry campaign. Otherwise a tiny residual long can
        # inherit an old entry timestamp and let weak exits fire immediately after a new buy.
        return increase_qty > max(qty_eps * 4.0, prev_qty * 0.05)

    def _update_position_tracker(self, state: AccountState, *, price_hint: float = 0.0) -> None:
        sign = self._position_sign(state, price_hint=price_hint)
        current_qty = abs(self.effective_position_btc(state, price_hint=price_hint))
        prev_qty = abs(float(getattr(self, "_last_effective_position_btc", 0.0) or 0.0))
        material_increase = (
            sign != 0
            and sign == self._last_position_sign
            and self._material_position_increase(current_qty, previous_position_btc=prev_qty)
        )
        if sign == 0:
            self._bars_in_position = 0
        elif sign != self._last_position_sign or material_increase:
            self._bars_in_position = 1
            if material_increase:
                seed = max(
                    0.0,
                    float(price_hint or 0.0),
                    float(getattr(state, "avg_entry_price", 0.0) or 0.0),
                )
                self._trail_peak_price = seed
                self._position_trough_price = seed
                self._trailing_armed = False
                self._dynamic_profit_target_bps_active = 0.0
                recent_prices = getattr(self, "_recent_prices", None)
                if hasattr(recent_prices, "clear"):
                    recent_prices.clear()
        else:
            self._bars_in_position += 1
        self._last_position_sign = sign
        self._last_effective_position_btc = current_qty if sign != 0 else 0.0

    def _position_sign(self, state: AccountState, price_hint: float = 0.0) -> int:
        qty_eps = max(1e-12, float(getattr(self.config, "position_epsilon_btc", 1e-12) or 1e-12))
        pos = float(getattr(state, "position_btc", 0.0) or 0.0)
        if abs(pos) <= qty_eps:
            return 0
        notional_eps = max(0.0, float(getattr(self.config, "position_epsilon_eur", 0.0) or 0.0))
        if notional_eps > 0.0:
            ref_price = max(
                0.0,
                float(price_hint or 0.0),
                float(getattr(state, "avg_entry_price", 0.0) or 0.0),
            )
            if ref_price > 0.0 and abs(pos) * ref_price < notional_eps:
                return 0
        return 1 if pos > 0.0 else -1

    def effective_position_btc(self, state: AccountState, price_hint: float = 0.0) -> float:
        pos = float(getattr(state, "position_btc", 0.0) or 0.0)
        if self._position_sign(state, price_hint=price_hint) == 0:
            return 0.0
        return pos

    def decide(
        self,
        state: AccountState,
        features: Features,
        gate: GateDecision,
        predicted_edge_bps: float,
        expected_cost_bps: float = 0.0,
        regime: Optional[str] = None,
    ) -> RiskDecision:
        price = float(features.values.get("price", 0.0))
        reason = self.update_kill_switches(state)
        prev_sign = self._last_position_sign
        self._update_position_tracker(state, price_hint=price)
        if self._short_loss_cluster_count > 0:
            self._short_loss_cluster_age_bars = max(
                0, int(getattr(self, "_short_loss_cluster_age_bars", 0) or 0)
            ) + 1
        eps = max(1e-12, float(getattr(self.config, "position_epsilon_btc", 1e-12) or 1e-12))
        pos = float(state.position_btc)
        current_sign = self._last_position_sign
        if prev_sign > 0 and current_sign == 0 and price > 0.0:
            # Value-based re-entry anchor: remember where the previous long was fully exited.
            self._last_long_exit_price = price
            self._corridor_entry_stage_pct = 0.0
            self._corridor_armed_stage_age_bars = 0
            self._corridor_pending_entry_stage_pct = 0.0
            self._corridor_exit_armed = False
            self._corridor_exit_peak_pct = 0.0
        elif current_sign > 0:
            self._reentry_cooldown_remaining = 0
            avg_entry_now = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry_now > 0.0:
                self._last_long_entry_price = avg_entry_now
        pos = self.effective_position_btc(state, price_hint=price)
        entry_edge_bps = max(0.0, float(getattr(self.config, "entry_edge_bps", 0.0)))
        entry_cost_buffer_bps = max(0.0, float(getattr(self.config, "entry_cost_buffer_bps", 0.0)))
        entry_cost_coverage_ratio = max(
            0.0,
            min(1.0, float(getattr(self.config, "entry_cost_coverage_ratio", 1.0))),
        )
        entry_cost_roundtrip_multiplier = max(
            1.0, float(getattr(self.config, "entry_cost_roundtrip_multiplier", 1.0) or 1.0)
        )
        entry_min_atr_to_cost_ratio = max(
            0.0, float(getattr(self.config, "entry_min_atr_to_cost_ratio", 0.0) or 0.0)
        )
        disable_entry_edge_gate = bool(getattr(self.config, "disable_entry_edge_gate", False))
        override_max_structure_range_pos = max(
            0.0,
            min(1.0, float(getattr(self.config, "override_max_structure_range_pos", 1.0) or 1.0)),
        )
        override_min_drawdown_from_peak_bps = max(
            0.0,
            float(getattr(self.config, "override_min_drawdown_from_peak_bps", 0.0) or 0.0),
        )
        override_min_drawdown_to_cost_ratio = max(
            0.0,
            float(getattr(self.config, "override_min_drawdown_to_cost_ratio", 0.0) or 0.0),
        )
        override_min_slope_short_bps = float(
            getattr(self.config, "override_min_slope_short_bps", -999.0) or -999.0
        )
        override_max_trend_return_bps = max(
            0.0,
            float(getattr(self.config, "override_max_trend_return_bps", 0.0) or 0.0),
        )
        override_max_context_range_pos = max(
            0.0,
            min(1.0, float(getattr(self.config, "override_max_context_range_pos", 1.0) or 1.0)),
        )
        late_entry_block_context_range_pos = max(
            0.0,
            min(
                1.0,
                float(getattr(self.config, "late_entry_block_context_range_pos", 1.0) or 1.0),
            ),
        )
        late_entry_block_structure_range_pos = max(
            0.0,
            min(
                1.0,
                float(getattr(self.config, "late_entry_block_structure_range_pos", 1.0) or 1.0),
            ),
        )
        late_entry_block_max_context_drawdown_bps = max(
            0.0,
            float(getattr(self.config, "late_entry_block_max_context_drawdown_bps", 0.0) or 0.0),
        )
        late_entry_block_min_trend_return_bps = max(
            0.0,
            float(getattr(self.config, "late_entry_block_min_trend_return_bps", 0.0) or 0.0),
        )
        late_entry_block_min_return_bps = max(
            0.0,
            float(getattr(self.config, "late_entry_block_min_return_bps", 0.0) or 0.0),
        )
        exit_edge_bps = float(getattr(self.config, "exit_edge_bps", 0.0))
        exit_bypass_gate_edge_bps = float(getattr(self.config, "exit_bypass_gate_edge_bps", 0.0))
        profit_only_auto_exits = bool(getattr(self.config, "profit_only_auto_exits", False))
        min_hold_bars = max(0, int(getattr(self.config, "min_hold_bars", 0)))
        failed_start_exit_enabled = bool(getattr(self.config, "failed_start_exit_enabled", False))
        failed_start_min_bars = max(0, int(getattr(self.config, "failed_start_min_bars", 0)))
        failed_start_max_bars = max(0, int(getattr(self.config, "failed_start_max_bars", 0)))
        failed_start_min_rebound_bps = max(
            0.0,
            float(getattr(self.config, "failed_start_min_rebound_bps", 0.0)),
        )
        failed_start_loss_bps = max(0.0, float(getattr(self.config, "failed_start_loss_bps", 0.0)))
        chop_break_even_reclaim_enabled = bool(
            getattr(self.config, "chop_break_even_reclaim_enabled", False)
        )
        chop_break_even_reclaim_min_bars = max(
            0,
            int(getattr(self.config, "chop_break_even_reclaim_min_bars", 0)),
        )
        chop_break_even_reclaim_min_drawdown_bps = max(
            0.0,
            float(getattr(self.config, "chop_break_even_reclaim_min_drawdown_bps", 0.0)),
        )
        chop_break_even_reclaim_max_edge_bps = float(
            getattr(self.config, "chop_break_even_reclaim_max_edge_bps", 0.0)
        )
        chop_break_even_reclaim_cross_window_bars = max(
            0,
            int(getattr(self.config, "chop_break_even_reclaim_cross_window_bars", 0)),
        )
        chop_break_even_reclaim_min_crosses = max(
            0,
            int(getattr(self.config, "chop_break_even_reclaim_min_crosses", 0)),
        )
        require_break_even_for_exit = bool(getattr(self.config, "require_break_even_for_exit", False))
        allow_reversal_exit_after_break_even = bool(
            getattr(self.config, "allow_reversal_exit_after_break_even", False)
        )
        min_exit_profit_bps = max(0.0, float(getattr(self.config, "min_exit_profit_bps", 0.0)))
        green_candle_take_exit_enabled = bool(
            getattr(self.config, "green_candle_take_exit_enabled", False)
        )
        green_candle_take_min_bars = max(
            0,
            int(getattr(self.config, "green_candle_take_min_bars", 0)),
        )
        green_candle_take_max_bars = max(
            0,
            int(getattr(self.config, "green_candle_take_max_bars", 0)),
        )
        green_candle_take_required_green_bars = max(
            0,
            int(getattr(self.config, "green_candle_take_required_green_bars", 0)),
        )
        green_candle_take_min_profit_bps = max(
            0.0,
            float(getattr(self.config, "green_candle_take_min_profit_bps", 0.0)),
        )
        time_break_even_floor_enabled = bool(
            getattr(self.config, "time_break_even_floor_enabled", False)
        )
        time_break_even_floor_bars = max(
            0,
            int(getattr(self.config, "time_break_even_floor_bars", 0)),
        )
        red_candle_exit_enabled = bool(getattr(self.config, "red_candle_exit_enabled", False))
        red_candle_window_bars = max(
            0,
            int(getattr(self.config, "red_candle_window_bars", 0)),
        )
        hard_take_profit_bps = max(0.0, float(getattr(self.config, "hard_take_profit_bps", 0.0)))
        dynamic_profit_target_enabled = bool(getattr(self.config, "dynamic_profit_target_enabled", False))
        hard_stop_loss_bps = max(0.0, float(getattr(self.config, "hard_stop_loss_bps", 0.0)))
        hard_take_profit_only_in_range = bool(getattr(self.config, "hard_take_profit_only_in_range", False))
        trailing_stop_enabled = bool(getattr(self.config, "trailing_stop_enabled", False))
        trailing_activation_bps = max(0.0, float(getattr(self.config, "trailing_activation_bps", 0.0)))
        trailing_stop_bps = max(0.0, float(getattr(self.config, "trailing_stop_bps", 0.0)))
        trailing_stop_atr_mult = max(0.0, float(getattr(self.config, "trailing_stop_atr_mult", 0.0)))
        campaign_hold_enabled = bool(getattr(self.config, "campaign_hold_enabled", False))
        campaign_hold_min_bars = max(0, int(getattr(self.config, "campaign_hold_min_bars", 0)))
        campaign_hold_min_profit_bps = max(
            0.0, float(getattr(self.config, "campaign_hold_min_profit_bps", 0.0) or 0.0)
        )
        campaign_hold_min_trend_bps = max(
            0.0, float(getattr(self.config, "campaign_hold_min_trend_bps", 0.0) or 0.0)
        )
        campaign_hold_max_range_pos = max(
            0.0, min(1.0, float(getattr(self.config, "campaign_hold_max_range_pos", 1.0) or 1.0))
        )
        campaign_hold_max_drawdown_from_peak_bps = max(
            0.0,
            float(getattr(self.config, "campaign_hold_max_drawdown_from_peak_bps", 0.0) or 0.0),
        )
        campaign_hold_min_recent_bias_bps = float(
            getattr(self.config, "campaign_hold_min_recent_bias_bps", -999.0) or -999.0
        )
        peak_profit_retrace_enabled = bool(getattr(self.config, "peak_profit_retrace_enabled", False))
        peak_profit_retrace_arm_bps = max(
            0.0,
            float(getattr(self.config, "peak_profit_retrace_arm_bps", 0.0)),
        )
        peak_profit_retrace_pct = max(
            0.0,
            min(100.0, float(getattr(self.config, "peak_profit_retrace_pct", 0.0))),
        )
        profit_roll_exit_enabled = bool(getattr(self.config, "profit_roll_exit_enabled", False))
        profit_roll_arm_eur = max(0.0, float(getattr(self.config, "profit_roll_arm_eur", 0.0) or 0.0))
        profit_roll_retrace_eur = max(
            0.0, float(getattr(self.config, "profit_roll_retrace_eur", 0.0) or 0.0)
        )
        profit_roll_retrace_pct_raw = getattr(self.config, "profit_roll_retrace_pct", 50.0)
        profit_roll_retrace_pct = max(
            0.0,
            min(
                100.0,
                float(50.0 if profit_roll_retrace_pct_raw is None else profit_roll_retrace_pct_raw),
            ),
        )
        profit_roll_min_retrace_eur = max(
            0.0,
            float(getattr(self.config, "profit_roll_min_retrace_eur", 0.02) or 0.0),
        )
        profit_roll_min_keep_profit_bps = max(
            0.0,
            float(getattr(self.config, "profit_roll_min_keep_profit_bps", 2.0) or 0.0),
        )
        reentry_cooldown_bars_after_trailing_stop = max(
            0,
            int(getattr(self.config, "reentry_cooldown_bars_after_trailing_stop", 0) or 0),
        )
        reentry_cooldown_bars_after_whipsaw_stop_loss = max(
            0,
            int(getattr(self.config, "reentry_cooldown_bars_after_whipsaw_stop_loss", 0) or 0),
        )
        reentry_whipsaw_hard_stop_max_bars = max(
            0,
            int(getattr(self.config, "reentry_whipsaw_hard_stop_max_bars", 0) or 0),
        )
        reentry_loss_cluster_window_bars = max(
            0,
            int(getattr(self.config, "reentry_loss_cluster_window_bars", 0) or 0),
        )
        reentry_cooldown_bars_after_loss_cluster = max(
            0,
            int(getattr(self.config, "reentry_cooldown_bars_after_loss_cluster", 0) or 0),
        )
        reentry_cooldown_bars_after_weak_exit = max(
            0,
            int(getattr(self.config, "reentry_cooldown_bars_after_weak_exit", 0) or 0),
        )
        reentry_min_move_bps = max(0.0, float(getattr(self.config, "reentry_min_move_bps", 0.0)))
        reentry_require_price_at_or_below_last_entry = bool(
            getattr(self.config, "reentry_require_price_at_or_below_last_entry", False)
        )
        reentry_last_entry_tolerance_bps = max(
            0.0, float(getattr(self.config, "reentry_last_entry_tolerance_bps", 0.0) or 0.0)
        )
        rebalance_min_delta_eur = max(0.0, float(getattr(self.config, "rebalance_min_delta_eur", 0.0)))
        full_position_only = bool(getattr(self.config, "full_position_only", False))
        min_entry_depth_eur = max(0.0, float(getattr(self.config, "min_entry_depth_eur", 0.0)))
        max_entry_notional_to_depth_ratio = max(
            0.0,
            float(getattr(self.config, "max_entry_notional_to_depth_ratio", 0.0)),
        )
        if profit_only_auto_exits:
            # Keep profit-taking exits, but block all loss/safety-style auto-flattens.
            exit_edge_bps = float("-inf")
            exit_bypass_gate_edge_bps = float("-inf")
            failed_start_exit_enabled = False
            failed_start_min_bars = 0
            chop_break_even_reclaim_enabled = False
            require_break_even_for_exit = True
            hard_stop_loss_bps = 0.0
            trailing_stop_enabled = False
            allow_reversal_exit_after_break_even = False
            time_break_even_floor_enabled = False
            red_candle_exit_enabled = False
            full_position_only = True
        regime_norm = str(regime or "").strip().lower()
        corridor_mode_enabled = float(features.values.get("corridor_staged_mode_enabled", 0.0) or 0.0) >= 0.5

        def _feature_bps_or_none(*names: str) -> float | None:
            for name in names:
                if name in features.values:
                    try:
                        return max(0.0, float(features.values.get(name, 0.0) or 0.0))
                    except Exception:
                        return 0.0
            return None

        # Keep trailing state per position direction.
        current_sign = self._position_sign(state, price_hint=price)
        if price > 0.0:
            self._recent_prices.append(float(price))
        if current_sign == 0:
            self._dynamic_profit_target_bps_active = 0.0
        elif current_sign > 0 and dynamic_profit_target_enabled and self._dynamic_profit_target_bps_active <= 0.0:
            self._dynamic_profit_target_bps_active = self._compute_dynamic_profit_target_bps(
                float(getattr(state, "avg_entry_price", 0.0) or 0.0),
                features,
            )
        if current_sign != self._trail_position_sign:
            self._trail_position_sign = current_sign
            self._trail_peak_price = 0.0
            self._trailing_armed = False
        if current_sign <= 0:
            self._position_trough_price = 0.0
        elif prev_sign != current_sign:
            seed = max(0.0, float(getattr(state, "avg_entry_price", 0.0) or 0.0))
            if seed <= 0.0 and price > 0.0:
                seed = price
            if price > 0.0:
                self._position_trough_price = min(seed or price, price)
            else:
                self._position_trough_price = seed
        elif current_sign > 0 and price > 0.0:
            if self._position_trough_price <= 0.0:
                self._position_trough_price = price
            else:
                self._position_trough_price = min(self._position_trough_price, price)
        if current_sign == 1:
            seed = max(0.0, float(getattr(state, "avg_entry_price", 0.0) or 0.0))
            if self._trail_peak_price <= 0.0:
                self._trail_peak_price = max(seed, price if price > 0.0 else 0.0)
            elif price > self._trail_peak_price:
                self._trail_peak_price = price

        def _break_even_required_price() -> float:
            if price <= 0.0:
                return 0.0
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return 0.0
            profit_target_bps = min_exit_profit_bps
            if dynamic_profit_target_enabled and pos > eps:
                profit_target_bps = float(self._dynamic_profit_target_bps_active)
            need_bps = max(0.0, float(expected_cost_bps or 0.0)) + profit_target_bps
            return avg_entry * (1.0 + (need_bps / 10000.0))

        def _break_even_profit_bps(avg_entry: float) -> float:
            avg_entry = float(avg_entry or 0.0)
            if avg_entry <= 0.0:
                return 0.0
            required_price = _break_even_required_price()
            if required_price <= avg_entry:
                return 0.0
            return max(0.0, (required_price / avg_entry - 1.0) * 10000.0)

        def _reversal_exit_after_break_even_ok() -> bool:
            if not allow_reversal_exit_after_break_even:
                return False
            if pos <= eps or price <= 0.0:
                return False
            required_price = _break_even_required_price()
            if required_price <= 0.0:
                return False
            peak = float(self._trail_peak_price or 0.0)
            if peak < required_price:
                return False
            return peak > 0.0 and price < peak

        def _time_break_even_floor_hit() -> bool:
            if not time_break_even_floor_enabled:
                return False
            if pos <= eps or price <= 0.0:
                return False
            floor_bars = max(0, int(time_break_even_floor_bars))
            if floor_bars <= 0 or self._bars_in_position < floor_bars:
                return False
            required_price = _break_even_required_price()
            if required_price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            current_profit_bps = (price / avg_entry - 1.0) * 10000.0
            peak = max(float(self._trail_peak_price or 0.0), avg_entry)
            mild_floor_loss_bps = max(
                12.0,
                max(0.0, float(expected_cost_bps or 0.0)) + 6.0,
            )
            if peak < required_price and current_profit_bps < -mild_floor_loss_bps:
                return False
            runner_grace_active, current_profit_bps, drawdown_from_peak_bps, red_closes = (
                _strong_runner_grace_state(
                    max_bars=max(floor_bars + 4, 8),
                    peak_profit_floor_bps=max(28.0, trailing_activation_bps * 0.8),
                    max_loss_bps=max(10.0, failed_start_loss_bps * 0.55),
                )
            )
            if (
                runner_grace_active
                and price <= required_price
                and current_profit_bps >= -(max(0.0, float(expected_cost_bps or 0.0)) + 8.0)
                and red_closes < 2
                and drawdown_from_peak_bps <= max(26.0, failed_start_loss_bps * 0.90)
            ):
                return False
            continuation_reclaim_grace = (
                (
                    float(features.values.get("alpha_continuation_await_liftoff", 0.0) or 0.0) >= 0.5
                    or float(features.values.get("alpha_continuation_armed", 0.0) or 0.0) >= 0.5
                )
                and float(features.values.get("alpha_up_structure", 0.0) or 0.0) >= 0.5
                and float(features.values.get("alpha_down_structure", 0.0) or 0.0) < 0.5
                and max(
                    0.0,
                    float(features.values.get("alpha_structure_range_pos", 0.0) or 0.0),
                )
                <= 0.10
                and float(features.values.get("alpha_structure_slope_short_bps", 0.0) or 0.0) >= 0.0
                and float(predicted_edge_bps or 0.0) >= -2.0
            )
            if continuation_reclaim_grace:
                drawdown_from_peak_bps = max(
                    0.0,
                    float(features.values.get("alpha_structure_drawdown_from_peak_bps", 0.0) or 0.0),
                )
                recent_bias_bps = float(features.values.get("alpha_recent_bias_bps", 0.0) or 0.0)
                reclaim_loss_cap_bps = max(
                    mild_floor_loss_bps,
                    max(0.0, float(expected_cost_bps or 0.0)) + 8.0,
                    max(0.0, float(features.values.get("atr_bps", 0.0) or 0.0)) * 1.35,
                )
                # Do not flatten a fresh continuation attempt directly at the local structure floor
                # while short-horizon slope has already stabilized again. That pattern produced the
                # early RENDER sell that exited the dip just before the move resumed.
                if (
                    current_profit_bps >= -reclaim_loss_cap_bps
                    and drawdown_from_peak_bps >= max(32.0, reclaim_loss_cap_bps * 1.5)
                    and recent_bias_bps >= -36.0
                ):
                    return False
            breakout_state_up = float(features.values.get("alpha_breakout_state_up", 0.0) or 0.0) >= 0.5
            breakout_state_down = float(features.values.get("alpha_breakout_state_down", 0.0) or 0.0) >= 0.5
            breakout_up_bps = float(features.values.get("alpha_breakout_up_bps", 0.0) or 0.0)
            breakout_down_bps = float(features.values.get("alpha_breakout_down_bps", 0.0) or 0.0)
            trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
            context_rebound_bps = max(
                0.0,
                float(features.values.get("context_rebound_bps", 0.0) or 0.0),
            )
            context_range_pos = max(
                0.0,
                float(features.values.get("context_range_pos", 0.0) or 0.0),
            )
            spread_bps = max(
                0.0,
                float(features.values.get("spread_bps", 0.0) or 0.0),
            )
            breakout_floor_grace_bars = max(2, floor_bars // 3) if floor_bars > 0 else 0
            if (
                breakout_state_up
                and not breakout_state_down
                and breakout_floor_grace_bars > 0
                and self._bars_in_position <= (floor_bars + breakout_floor_grace_bars)
                and breakout_up_bps >= max(2.0, max(0.0, float(expected_cost_bps or 0.0)) * 0.10)
                and breakout_down_bps >= -max(4.0, max(0.0, float(expected_cost_bps or 0.0)) * 0.35)
                and trend_return_bps >= max(80.0, trailing_activation_bps * 2.2)
                and context_rebound_bps >= max(180.0, trailing_activation_bps * 6.0)
                and 0.35 <= context_range_pos <= 0.74
                and float(predicted_edge_bps or 0.0) >= max(10.0, max(0.0, float(expected_cost_bps or 0.0)) * 0.35)
            ):
                peak = max(float(self._trail_peak_price or 0.0), avg_entry)
                breakout_drawdown_from_peak_bps = max(0.0, (peak - price) / avg_entry * 10000.0)
                breakout_red_closes = _recent_red_closes(2)
                breakout_loss_cap_bps = max(
                    mild_floor_loss_bps,
                    max(0.0, float(expected_cost_bps or 0.0)) + 10.0,
                )
                if (
                    current_profit_bps >= -breakout_loss_cap_bps
                    and breakout_drawdown_from_peak_bps <= max(24.0, failed_start_loss_bps * 0.90)
                    and breakout_red_closes < 2
                ):
                    return False
            swing_reclaim_grace_bars = max(4, min(144, floor_bars * 2)) if floor_bars > 0 else 0
            if (
                swing_reclaim_grace_bars > 0
                and self._bars_in_position <= (floor_bars + swing_reclaim_grace_bars)
                and float(features.values.get("alpha_down_structure", 0.0) or 0.0) < 0.5
                and trend_return_bps >= max(18.0, trailing_activation_bps * 0.65)
                and context_rebound_bps >= max(240.0, trailing_activation_bps * 7.5)
                and 0.68 <= context_range_pos <= 0.86
                and spread_bps <= max(24.0, max(0.0, float(expected_cost_bps or 0.0)) + 6.0)
                and float(predicted_edge_bps or 0.0) >= -max(
                    2.0,
                    max(0.0, float(expected_cost_bps or 0.0)) * 0.15,
                )
            ):
                peak = max(float(self._trail_peak_price or 0.0), avg_entry)
                swing_drawdown_from_peak_bps = max(0.0, (peak - price) / avg_entry * 10000.0)
                swing_red_closes = _recent_red_closes(2)
                swing_loss_cap_bps = max(
                    mild_floor_loss_bps,
                    max(0.0, float(expected_cost_bps or 0.0)) + 8.0,
                )
                # Keep constructive late swing reclaims alive a bit longer while they digest near
                # break-even. That avoids flattening a recovering rebound just before the next leg
                # resumes, but still exits on deeper slippage or a clean two-red rollover.
                if (
                    current_profit_bps >= -swing_loss_cap_bps
                    and swing_drawdown_from_peak_bps <= max(42.0, failed_start_loss_bps * 1.60)
                    and swing_red_closes < 2
                ):
                    return False
            return price <= required_price

        def _red_candle_exit_hit() -> bool:
            if not red_candle_exit_enabled:
                return False
            if pos <= eps or price <= 0.0:
                return False
            window_bars = max(0, int(red_candle_window_bars))
            # Require N consecutive down-closing completed bars.
            # With close-only sampling this means the last N+1 closes are
            # strictly descending: p0 > p1 > ... > pN.
            if window_bars <= 0 or len(self._recent_prices) < (window_bars + 1):
                return False
            if not _break_even_exit_ok():
                return False
            window = list(self._recent_prices)[-(window_bars + 1):]
            prev_px = float(window[0] or 0.0)
            if prev_px <= 0.0:
                return False
            for px in window[1:]:
                cur_px = float(px or 0.0)
                if cur_px <= 0.0 or cur_px >= prev_px:
                    return False
                prev_px = cur_px
            return True

        def _green_candle_take_exit_hit() -> bool:
            if not green_candle_take_exit_enabled:
                return False
            if pos <= eps or price <= 0.0:
                return False
            if _campaign_hold_active():
                return False
            if green_candle_take_required_green_bars <= 0:
                return False
            if green_candle_take_min_bars > 0 and self._bars_in_position < green_candle_take_min_bars:
                return False
            if green_candle_take_max_bars > 0 and self._bars_in_position > green_candle_take_max_bars:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            current_profit_bps = (price / avg_entry - 1.0) * 10000.0
            required_price = _break_even_required_price()
            required_break_even_profit_bps = 0.0
            if required_price > 0.0 and avg_entry > 0.0:
                required_break_even_profit_bps = max(
                    0.0,
                    (required_price / avg_entry - 1.0) * 10000.0,
                )
            required_profit_bps = max(
                green_candle_take_min_profit_bps,
                required_break_even_profit_bps,
            )
            if current_profit_bps < required_profit_bps:
                return False
            needed_window = max(2, self._bars_in_position + 1)
            if len(self._recent_prices) < needed_window:
                return False
            window = list(self._recent_prices)[-needed_window:]
            green_bars = 0
            for prev_px, cur_px in zip(window[:-1], window[1:]):
                prev_v = float(prev_px or 0.0)
                cur_v = float(cur_px or 0.0)
                if prev_v <= 0.0 or cur_v <= 0.0:
                    continue
                if cur_v > prev_v:
                    green_bars += 1
            if green_bars < green_candle_take_required_green_bars:
                return False
            # Require the most recent required candles to remain green (avoid exiting into rollover).
            tail_needed = green_candle_take_required_green_bars + 1
            if len(window) < tail_needed:
                return False
            tail = window[-tail_needed:]
            for prev_px, cur_px in zip(tail[:-1], tail[1:]):
                if float(cur_px or 0.0) <= float(prev_px or 0.0):
                    return False
            return True

        def _recent_red_closes(window_bars: int = 2) -> int:
            if window_bars <= 0 or len(self._recent_prices) < 2:
                return 0
            red_closes = 0
            prices = list(self._recent_prices)
            for prev_px, cur_px in zip(reversed(prices[:-1]), reversed(prices[1:])):
                prev_v = float(prev_px or 0.0)
                cur_v = float(cur_px or 0.0)
                if prev_v <= 0.0 or cur_v <= 0.0 or cur_v >= prev_v:
                    break
                red_closes += 1
                if red_closes >= window_bars:
                    break
            return red_closes

        def _strong_runner_grace_state(
            *,
            max_bars: int,
            peak_profit_floor_bps: float,
            max_loss_bps: float,
        ) -> tuple[bool, float, float, int]:
            if pos <= eps or price <= 0.0 or max_bars <= 0 or self._bars_in_position <= 0:
                return False, 0.0, 0.0, 0
            if self._bars_in_position > max_bars:
                return False, 0.0, 0.0, 0
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False, 0.0, 0.0, 0
            current_profit_bps = (price / avg_entry - 1.0) * 10000.0
            peak = max(float(self._trail_peak_price or 0.0), avg_entry)
            peak_profit_bps = max(0.0, (peak / avg_entry - 1.0) * 10000.0)
            drawdown_from_peak_bps = max(0.0, (peak - price) / avg_entry * 10000.0)
            up_structure = float(features.values.get("alpha_up_structure", 0.0) or 0.0) >= 0.5
            down_structure = float(features.values.get("alpha_down_structure", 0.0) or 0.0) >= 0.5
            active_leg_rise = float(features.values.get("alpha_active_leg_rise", 0.0) or 0.0) >= 0.5
            continuation_bias = (
                float(features.values.get("alpha_continuation_await_liftoff", 0.0) or 0.0) >= 0.5
                or float(features.values.get("alpha_continuation_armed", 0.0) or 0.0) >= 0.5
            )
            override_style = (
                float(features.values.get("alpha_staircase_override", 0.0) or 0.0) >= 0.5
                or float(features.values.get("alpha_impulse_override", 0.0) or 0.0) >= 0.5
            )
            trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
            recent_bias_bps = float(features.values.get("alpha_recent_bias_bps", 0.0) or 0.0)
            range_pos = max(0.0, float(features.values.get("alpha_structure_range_pos", 0.0) or 0.0))
            red_closes = _recent_red_closes(2)
            if down_structure:
                return False, current_profit_bps, drawdown_from_peak_bps, red_closes
            strong_structure = up_structure and (active_leg_rise or continuation_bias or override_style)
            strong_context = (
                trend_return_bps >= max(24.0, peak_profit_floor_bps * 0.60)
                or peak_profit_bps >= peak_profit_floor_bps
            )
            strong_peak_context = (
                peak_profit_bps >= peak_profit_floor_bps * 1.20
                and trend_return_bps >= max(18.0, peak_profit_floor_bps * 0.45)
            )
            if not strong_context or not (strong_structure or strong_peak_context):
                return False, current_profit_bps, drawdown_from_peak_bps, red_closes
            if range_pos > 0.88 and peak_profit_bps < peak_profit_floor_bps * 1.15:
                return False, current_profit_bps, drawdown_from_peak_bps, red_closes
            if recent_bias_bps < -42.0 and peak_profit_bps < peak_profit_floor_bps * 1.20:
                return False, current_profit_bps, drawdown_from_peak_bps, red_closes
            allowed_loss_bps = max(
                18.0,
                max_loss_bps,
                max(0.0, float(expected_cost_bps or 0.0)) + 10.0,
            )
            if current_profit_bps < -allowed_loss_bps:
                return False, current_profit_bps, drawdown_from_peak_bps, red_closes
            return True, current_profit_bps, drawdown_from_peak_bps, red_closes

        def _failed_start_exit_hit() -> bool:
            if not failed_start_exit_enabled or pos <= eps:
                return False
            if failed_start_max_bars <= 0 or self._bars_in_position <= 0:
                return False
            breakout_state_up = float(features.values.get("alpha_breakout_state_up", 0.0) or 0.0) >= 0.5
            breakout_state_down = float(features.values.get("alpha_breakout_state_down", 0.0) or 0.0) >= 0.5
            breakout_down_bps = float(features.values.get("alpha_breakout_down_bps", 0.0) or 0.0)
            breakout_extension_downside_bps = max(
                8.0,
                min(
                    20.0,
                    max(
                        max(0.0, float(expected_cost_bps or 0.0)) * 0.60,
                        failed_start_loss_bps * 0.35,
                    ),
                ),
            )
            breakout_timeout_grace = (
                breakout_state_up
                or (
                    not breakout_state_down
                    and breakout_down_bps >= -breakout_extension_downside_bps
                )
            )
            failed_start_effective_max_bars = failed_start_max_bars
            if breakout_timeout_grace:
                failed_start_effective_max_bars += max(1, min(6, failed_start_max_bars - failed_start_min_bars + 1))
            if failed_start_min_bars > 0 and self._bars_in_position < failed_start_min_bars:
                return False
            if self._bars_in_position > failed_start_effective_max_bars:
                return False
            if price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            if failed_start_loss_bps <= 0.0:
                return False
            drawdown_bps = max(0.0, (avg_entry - price) / avg_entry * 10000.0)
            peak = max(float(self._trail_peak_price or 0.0), avg_entry)
            rebound_bps = max(0.0, (peak - avg_entry) / avg_entry * 10000.0)
            if rebound_bps >= failed_start_min_rebound_bps:
                return False
            breakout_up_bps = float(features.values.get("alpha_breakout_up_bps", 0.0) or 0.0)
            trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
            context_range_pos = max(0.0, float(features.values.get("context_range_pos", 0.0) or 0.0))
            context_rebound_bps = max(
                0.0,
                float(features.values.get("context_rebound_bps", 0.0) or 0.0),
            )
            breakout_loss_buffer_bps = max(
                6.0,
                max(0.0, float(expected_cost_bps or 0.0)) * 0.35,
                failed_start_loss_bps * 0.18,
            )
            breakout_bottom_reclaim_constructive = (
                context_range_pos <= 0.12
                and context_rebound_bps >= max(10.0, min(18.0, failed_start_min_rebound_bps * 0.45))
            )
            breakout_still_constructive = (
                not breakout_state_down
                and breakout_down_bps >= -max(3.0, min(12.0, max(0.0, float(expected_cost_bps or 0.0)) * 0.35))
                and breakout_up_bps >= -(failed_start_loss_bps * 0.85)
                and (
                    trend_return_bps >= max(42.0, failed_start_min_rebound_bps * 1.35)
                    or breakout_bottom_reclaim_constructive
                )
                and context_range_pos <= 0.86
            )
            if breakout_still_constructive and drawdown_bps < (failed_start_loss_bps + breakout_loss_buffer_bps):
                return False
            # Keep true continuation trades alive a bit longer even if the first lift-off is slow.
            # Without this, the bot clips many valid uptrends before the second push arrives.
            continuation_grace = (
                float(features.values.get("alpha_continuation_await_liftoff", 0.0) or 0.0) >= 0.5
                or float(features.values.get("alpha_continuation_armed", 0.0) or 0.0) >= 0.5
            )
            if continuation_grace:
                up_structure = float(features.values.get("alpha_up_structure", 0.0) or 0.0) >= 0.5
                down_structure = float(features.values.get("alpha_down_structure", 0.0) or 0.0) >= 0.5
                active_leg_rise = float(features.values.get("alpha_active_leg_rise", 0.0) or 0.0) >= 0.5
                override_style = (
                    float(features.values.get("alpha_staircase_override", 0.0) or 0.0) >= 0.5
                    or float(features.values.get("alpha_impulse_override", 0.0) or 0.0) >= 0.5
                )
                atr_bps = max(0.0, float(features.values.get("atr_bps", 0.0) or 0.0))
                trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
                range_pos = max(0.0, float(features.values.get("alpha_structure_range_pos", 0.0) or 0.0))
                drawdown_from_peak_bps = max(
                    0.0,
                    float(features.values.get("alpha_structure_drawdown_from_peak_bps", 0.0) or 0.0),
                )
                young_trade_continuation_grace = (
                    up_structure
                    and not down_structure
                    and active_leg_rise
                    and self._bars_in_position <= max(failed_start_min_bars + 1, 2)
                    and trend_return_bps >= max(12.0, max(0.0, float(expected_cost_bps or 0.0)) * 0.75)
                    and range_pos <= 0.98
                    and drawdown_from_peak_bps <= max(18.0, failed_start_loss_bps * 0.55)
                )
                if young_trade_continuation_grace:
                    grace_loss_buffer_bps = max(
                        8.0,
                        atr_bps * 0.45,
                        max(0.0, float(expected_cost_bps or 0.0)) * 0.45,
                        failed_start_loss_bps * 0.20,
                    )
                    if drawdown_bps < (failed_start_loss_bps + grace_loss_buffer_bps):
                        return False
                if (
                    up_structure
                    and not down_structure
                    and active_leg_rise
                    and not override_style
                    and trend_return_bps >= max(18.0, failed_start_min_rebound_bps * 0.75)
                    and range_pos <= 0.82
                    and drawdown_from_peak_bps <= max(35.0, failed_start_loss_bps * 0.75)
                ):
                    return False
            # Give staircase continuation moves one extra chance if they are still mid-structure.
            # This protects orderly step-up trends from being cut on the first shallow wobble,
            # while still rejecting late/top-heavy burst entries.
            staircase_grace = float(features.values.get("alpha_staircase_override", 0.0) or 0.0) >= 0.5
            if staircase_grace:
                up_structure = float(features.values.get("alpha_up_structure", 0.0) or 0.0) >= 0.5
                down_structure = float(features.values.get("alpha_down_structure", 0.0) or 0.0) >= 0.5
                active_leg_rise = float(features.values.get("alpha_active_leg_rise", 0.0) or 0.0) >= 0.5
                trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
                range_pos = max(0.0, float(features.values.get("alpha_structure_range_pos", 0.0) or 0.0))
                slope_short_bps = float(features.values.get("alpha_structure_slope_short_bps", 0.0) or 0.0)
                drawdown_from_peak_bps = max(
                    0.0,
                    float(features.values.get("alpha_structure_drawdown_from_peak_bps", 0.0) or 0.0),
                )
                current_profit_bps = (price / avg_entry - 1.0) * 10000.0
                if (
                    up_structure
                    and not down_structure
                    and active_leg_rise
                    and trend_return_bps >= max(42.0, failed_start_min_rebound_bps * 1.6)
                    and slope_short_bps >= 1.5
                    and range_pos <= 0.72
                    and 18.0 <= drawdown_from_peak_bps <= max(34.0, failed_start_loss_bps * 0.7)
                    and current_profit_bps >= -(failed_start_loss_bps * 0.55)
                ):
                    return False
            runner_grace_active, _, drawdown_from_peak_bps, red_closes = _strong_runner_grace_state(
                max_bars=max(failed_start_max_bars, failed_start_min_bars + 4, 6),
                peak_profit_floor_bps=max(24.0, failed_start_min_rebound_bps * 0.85),
                max_loss_bps=failed_start_loss_bps * 0.65,
            )
            if (
                runner_grace_active
                and red_closes < 2
                and drawdown_from_peak_bps <= max(48.0, failed_start_loss_bps * 2.25)
            ):
                return False
            return drawdown_bps >= failed_start_loss_bps

        def _chop_break_even_reclaim_hit() -> bool:
            if not chop_break_even_reclaim_enabled or pos <= eps:
                return False
            if chop_break_even_reclaim_min_bars <= 0 or self._bars_in_position < chop_break_even_reclaim_min_bars:
                return False
            if price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            trough = float(self._position_trough_price or 0.0)
            if trough <= 0.0:
                return False
            drawdown_bps = max(0.0, (avg_entry - trough) / avg_entry * 10000.0)
            if drawdown_bps < chop_break_even_reclaim_min_drawdown_bps:
                return False
            if chop_break_even_reclaim_cross_window_bars > 0 and chop_break_even_reclaim_min_crosses > 0:
                if len(self._recent_prices) < chop_break_even_reclaim_cross_window_bars:
                    return False
                window = list(self._recent_prices)[-chop_break_even_reclaim_cross_window_bars:]
                # Count state changes above/below entry; this is the "rumeiern" filter.
                crosses = 0
                prev = float(window[0] or 0.0) >= avg_entry
                for px in window[1:]:
                    cur = float(px or 0.0) >= avg_entry
                    if cur != prev:
                        crosses += 1
                    prev = cur
                if crosses < chop_break_even_reclaim_min_crosses:
                    return False
            required_price = _break_even_required_price()
            if required_price <= 0.0:
                return False
            if price < required_price:
                return False
            return predicted_edge_bps <= chop_break_even_reclaim_max_edge_bps

        def _break_even_exit_ok() -> bool:
            if not require_break_even_for_exit:
                return True
            required_price = _break_even_required_price()
            if required_price <= 0.0:
                return True
            return price >= required_price

        def _cost_floor_exit_ok(extra_profit_bps: float = 0.0) -> bool:
            if price <= 0.0:
                return True
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return True
            need_bps = max(0.0, float(expected_cost_bps or 0.0)) + max(
                0.0, float(extra_profit_bps or 0.0)
            )
            return price >= avg_entry * (1.0 + (need_bps / 10000.0))

        def _hard_take_profit_hit() -> bool:
            if pos <= eps:
                return False
            if hard_take_profit_only_in_range and regime_norm != "range":
                return False
            if price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            profit_target_bps = hard_take_profit_bps
            if dynamic_profit_target_enabled:
                profit_target_bps = float(self._dynamic_profit_target_bps_active)
            if profit_target_bps <= 0.0:
                return False
            need_bps = max(0.0, float(expected_cost_bps or 0.0)) + profit_target_bps
            required_price = avg_entry * (1.0 + (need_bps / 10000.0))
            return price >= required_price

        def _hard_stop_loss_hit() -> bool:
            if hard_stop_loss_bps <= 0.0:
                return False
            if price <= 0.0 or abs(pos) <= eps:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            dist = hard_stop_loss_bps / 10000.0
            if pos > eps:
                stop_price = avg_entry * (1.0 - dist)
                return price <= stop_price
            if pos < -eps:
                stop_price = avg_entry * (1.0 + dist)
                return price >= stop_price
            return False

        def _campaign_hold_active() -> bool:
            if not campaign_hold_enabled or pos <= eps or price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            if campaign_hold_min_bars > 0 and self._bars_in_position < campaign_hold_min_bars:
                return False
            current_profit_bps = (price / avg_entry - 1.0) * 10000.0
            required_profit_bps = max(
                campaign_hold_min_profit_bps,
                max(0.0, float(expected_cost_bps or 0.0)) * 0.85,
            )
            if current_profit_bps < required_profit_bps:
                return False
            if float(features.values.get("trend_return_bps", 0.0) or 0.0) < campaign_hold_min_trend_bps:
                return False
            up_structure = float(features.values.get("alpha_up_structure", 0.0) or 0.0) >= 0.5
            active_leg_rise = float(features.values.get("alpha_active_leg_rise", 0.0) or 0.0) >= 0.5
            campaign_bias = float(features.values.get("alpha_campaign_hold_bias", 0.0) or 0.0) >= 0.5
            if not campaign_bias and not (up_structure and active_leg_rise):
                return False
            range_pos = max(0.0, float(features.values.get("alpha_structure_range_pos", 0.0) or 0.0))
            if campaign_hold_max_range_pos < 1.0 and range_pos > campaign_hold_max_range_pos:
                return False
            drawdown_from_peak_bps = max(
                0.0,
                float(features.values.get("alpha_structure_drawdown_from_peak_bps", 0.0) or 0.0),
            )
            if (
                campaign_hold_max_drawdown_from_peak_bps > 0.0
                and drawdown_from_peak_bps > campaign_hold_max_drawdown_from_peak_bps
            ):
                return False
            recent_bias_bps = float(features.values.get("alpha_recent_bias_bps", 0.0) or 0.0)
            if recent_bias_bps < campaign_hold_min_recent_bias_bps:
                return False
            return True

        def _trailing_stop_hit() -> bool:
            if not trailing_stop_enabled or pos <= eps:
                return False
            if price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            peak = float(self._trail_peak_price or 0.0)
            if peak <= 0.0:
                return False

            # Arm only after enough positive excursion above entry and expected exit costs.
            arm_need_bps = max(0.0, float(expected_cost_bps or 0.0)) + trailing_activation_bps
            arm_price = avg_entry * (1.0 + (arm_need_bps / 10000.0))
            if not self._trailing_armed:
                if peak >= arm_price:
                    self._trailing_armed = True
                else:
                    return False

            atr_bps = max(0.0, float(features.values.get("atr_bps", 0.0)))
            trail_need_bps = max(trailing_stop_bps, atr_bps * trailing_stop_atr_mult)
            if trail_need_bps <= 0.0:
                return False
            peak_profit_bps = max(0.0, (peak / avg_entry - 1.0) * 10000.0)
            early_rebound_entry = (
                float(features.values.get("alpha_continuation_early_liftoff", 0.0) or 0.0) >= 0.5
                or float(features.values.get("alpha_swing_micro_valley_rebound", 0.0) or 0.0) >= 0.5
                or float(features.values.get("alpha_swing_valley_rebound", 0.0) or 0.0) >= 0.5
            )
            break_even_profit_bps = _break_even_profit_bps(avg_entry)
            min_peak_profit_to_lock_bps = 0.0
            if break_even_profit_bps > 0.0:
                close_only_lock_buffer_bps = max(
                    4.0,
                    atr_bps * 0.45,
                    break_even_profit_bps * 0.35,
                    trail_need_bps * 0.30,
                )
                min_peak_profit_to_lock_bps = (
                    break_even_profit_bps + trail_need_bps + close_only_lock_buffer_bps
                )
            if early_rebound_entry:
                slow_start_lock_buffer_bps = max(
                    12.0,
                    atr_bps * 0.60,
                    trail_need_bps * 0.70,
                    arm_need_bps * 0.35,
                )
                min_peak_profit_to_lock_bps = max(
                    min_peak_profit_to_lock_bps,
                    arm_need_bps + slow_start_lock_buffer_bps,
                )
            if min_peak_profit_to_lock_bps > 0.0 and peak_profit_bps < min_peak_profit_to_lock_bps:
                return False
            if _campaign_hold_active():
                return False
            runner_grace_active, current_profit_bps, drawdown_from_peak_bps, red_closes = (
                _strong_runner_grace_state(
                    max_bars=max(campaign_hold_min_bars + 6, 10),
                    peak_profit_floor_bps=max(32.0, arm_need_bps * 0.75),
                    max_loss_bps=max(10.0, failed_start_loss_bps * 0.45),
                )
            )
            if runner_grace_active:
                trail_need_bps = max(
                    trail_need_bps,
                    trailing_stop_bps * 1.55,
                    atr_bps * max(trailing_stop_atr_mult, 1.20),
                )
            # Profit-lock: after trailing is armed, do not allow the effective stop level
            # to fall below cost-adjusted break-even.
            trailing_stop_price = peak * (1.0 - (trail_need_bps / 10000.0))
            break_even_floor_price = _break_even_required_price()
            if break_even_floor_price <= 0.0:
                break_even_floor_price = avg_entry
            stop_price = max(trailing_stop_price, break_even_floor_price)
            if (
                runner_grace_active
                and price <= stop_price
                and current_profit_bps >= -(max(0.0, float(expected_cost_bps or 0.0)) + 6.0)
                and red_closes < 2
                and drawdown_from_peak_bps < (trail_need_bps * 1.20)
            ):
                return False
            early_rebound_grace_bars = max(min_hold_bars + 3, campaign_hold_min_bars + 2, 6)
            if (
                early_rebound_entry
                and self._bars_in_position <= early_rebound_grace_bars
                and price <= stop_price
                and current_profit_bps >= -(max(0.0, float(expected_cost_bps or 0.0)) + 8.0)
                and red_closes < 2
                and drawdown_from_peak_bps < max(trail_need_bps * 1.35, 18.0)
            ):
                return False
            return price <= stop_price

        def _peak_profit_retrace_hit() -> bool:
            if not peak_profit_retrace_enabled or pos <= eps:
                return False
            if peak_profit_retrace_arm_bps <= 0.0 or peak_profit_retrace_pct <= 0.0:
                return False
            if price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            peak = float(self._trail_peak_price or 0.0)
            if peak <= avg_entry:
                return False
            peak_profit_bps = (peak / avg_entry - 1.0) * 10000.0
            if peak_profit_bps < peak_profit_retrace_arm_bps:
                return False
            current_profit_bps = (price / avg_entry - 1.0) * 10000.0
            keep_bps = peak_profit_bps * (1.0 - (peak_profit_retrace_pct / 100.0))
            if current_profit_bps > keep_bps:
                return False
            if _campaign_hold_active():
                return False
            if not _break_even_exit_ok():
                return False
            return True

        def _profit_roll_exit_hit() -> bool:
            if not profit_roll_exit_enabled or pos <= eps:
                return False
            if price <= 0.0:
                return False
            avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
            if avg_entry <= 0.0:
                return False
            peak = float(self._trail_peak_price or 0.0)
            if peak <= 0.0:
                return False
            open_pnl_eur = (price - avg_entry) * pos
            peak_pnl_eur = (peak - avg_entry) * pos
            # In the current live profit-only path, a zero EUR sockel means:
            # use the staged base target percent as the roll arming threshold.
            profit_roll_arm_pct = 0.0
            if profit_only_auto_exits and profit_roll_arm_eur <= 0.0:
                profit_roll_arm_pct = max(
                    0.0,
                    float(features.values.get("corridor_staged_profit_target_base_pct", 0.0) or 0.0),
                )
            if profit_roll_arm_eur > 0.0:
                if peak_pnl_eur < profit_roll_arm_eur:
                    return False
            elif profit_roll_arm_pct > 0.0:
                peak_profit_pct = ((peak / avg_entry) - 1.0) * 100.0
                if peak_profit_pct < profit_roll_arm_pct:
                    return False
            else:
                return False
            if profit_roll_retrace_eur > 0.0:
                retrace_need_eur = max(profit_roll_retrace_eur, profit_roll_min_retrace_eur)
            else:
                retrace_need_eur = max(
                    peak_pnl_eur * (profit_roll_retrace_pct / 100.0),
                    profit_roll_min_retrace_eur,
                )
            if retrace_need_eur <= 0.0:
                return False
            if open_pnl_eur > peak_pnl_eur - retrace_need_eur:
                return False
            if _campaign_hold_active():
                return False
            if not _cost_floor_exit_ok(profit_roll_min_keep_profit_bps):
                return False
            return True

        def _arm_reentry_cooldown(exit_reason: str) -> int:
            reason = str(exit_reason or "").strip().lower()
            bars_in_position = max(0, int(getattr(self, "_bars_in_position", 0) or 0))
            weak_exit_reasons = {
                "time_break_even_floor",
                "failed_start_exit",
                "chop_break_even_reclaim",
            }
            if reason == "trailing_stop":
                self._short_loss_cluster_count = 0
                self._short_loss_cluster_age_bars = 0
                return max(
                    int(getattr(self, "_reentry_cooldown_remaining", 0) or 0),
                    reentry_cooldown_bars_after_trailing_stop,
                )
            if (
                reason == "hard_stop_loss"
                and reentry_cooldown_bars_after_whipsaw_stop_loss > 0
                and reentry_whipsaw_hard_stop_max_bars > 0
                and bars_in_position <= reentry_whipsaw_hard_stop_max_bars
            ):
                cluster_count = 1
                previous_cluster_count = max(
                    0, int(getattr(self, "_short_loss_cluster_count", 0) or 0)
                )
                previous_cluster_age = max(
                    0, int(getattr(self, "_short_loss_cluster_age_bars", 0) or 0)
                )
                if (
                    reentry_loss_cluster_window_bars > 0
                    and previous_cluster_count > 0
                    and previous_cluster_age <= reentry_loss_cluster_window_bars
                ):
                    cluster_count = previous_cluster_count + 1
                self._short_loss_cluster_count = cluster_count
                self._short_loss_cluster_age_bars = 0
                cooldown = max(
                    int(getattr(self, "_reentry_cooldown_remaining", 0) or 0),
                    reentry_cooldown_bars_after_whipsaw_stop_loss,
                )
                if cluster_count >= 2 and reentry_cooldown_bars_after_loss_cluster > 0:
                    cooldown = max(cooldown, reentry_cooldown_bars_after_loss_cluster)
                return cooldown
            if reason in weak_exit_reasons:
                self._short_loss_cluster_count = 0
                self._short_loss_cluster_age_bars = 0
                return max(
                    int(getattr(self, "_reentry_cooldown_remaining", 0) or 0),
                    reentry_cooldown_bars_after_weak_exit,
                )
            if reason in {
                "hard_take_profit",
                "green_candle_take_exit",
                "red_candle_exit",
                "peak_profit_retrace",
                "profit_roll_exit",
                "edge_exit",
                "exit_bypass_gate",
                "reversal_exit_after_break_even",
            }:
                self._short_loss_cluster_count = 0
                self._short_loss_cluster_age_bars = 0
            return int(getattr(self, "_reentry_cooldown_remaining", 0) or 0)

        def _corridor_staged_mode_decision() -> RiskDecision | None:
            if not corridor_mode_enabled or self.config.allow_short:
                return None

            if self._cooldown_remaining > 0:
                self._cooldown_remaining -= 1
                return RiskDecision(
                    ts=features.ts,
                    allow=False,
                    target_position_btc=state.position_btc,
                    reason=reason or "cooldown",
                    cooldown_remaining=self._cooldown_remaining,
                )

            corridor_ready = float(features.values.get("corridor_ready", 0.0) or 0.0) >= 0.5
            raw_pos = max(0.0, min(100.0, float(features.values.get("corridor_position_pct", 0.0) or 0.0)))
            smoothing_bars = max(
                1,
                int(float(features.values.get("corridor_staged_transition_smoothing_bars", 3.0) or 3.0)),
            )
            smoothing_alpha = 2.0 / float(smoothing_bars + 1)
            prior_smooth = (
                float(self._corridor_smooth_pos_pct)
                if float(self._corridor_smooth_pos_pct) >= 0.0
                else raw_pos
            )
            if corridor_ready:
                smooth = (
                    raw_pos
                    if float(self._corridor_smooth_pos_pct) < 0.0
                    else prior_smooth + ((raw_pos - prior_smooth) * smoothing_alpha)
                )
            else:
                smooth = prior_smooth
            self._corridor_prev_smooth_pos_pct = prior_smooth
            self._corridor_smooth_pos_pct = smooth

            entry_levels_raw = [
                float(features.values.get("corridor_staged_entry_1_pct", 10.0) or 10.0),
                float(features.values.get("corridor_staged_entry_2_pct", 20.0) or 20.0),
                float(features.values.get("corridor_staged_entry_3_pct", 30.0) or 30.0),
                float(features.values.get("corridor_staged_entry_4_pct", 40.0) or 40.0),
            ]
            no_buy_above = max(
                0.0,
                min(
                    100.0,
                    float(features.values.get("corridor_staged_no_buy_above_pct", 50.0) or 50.0),
                ),
            )
            exit_step_pct = max(
                0.1,
                float(features.values.get("corridor_staged_exit_step_pct", 10.0) or 10.0),
            )
            hysteresis_pct = max(
                0.0,
                float(features.values.get("corridor_staged_hysteresis_pct", 0.75) or 0.75),
            )
            exit_retrace_pct = max(
                0.0,
                float(features.values.get("corridor_staged_exit_retrace_pct", 0.4) or 0.4),
            )
            entry_wait_bars = max(
                0,
                int(float(features.values.get("corridor_staged_entry_wait_bars", 6.0) or 6.0)),
            )
            require_rising = float(features.values.get("corridor_staged_require_rising", 1.0) or 1.0) >= 0.5
            entry_levels = sorted(
                {
                    level
                    for level in entry_levels_raw
                    if level > 0.0 and level < max(1.0, no_buy_above)
                }
            )
            if not entry_levels:
                fallback_level = min(40.0, max(1.0, no_buy_above - 1.0))
                entry_levels = [fallback_level]

            if smooth >= (no_buy_above + hysteresis_pct):
                self._corridor_armed_stage_pct = 0.0
                self._corridor_armed_stage_age_bars = 0
                self._corridor_lowest_pos_pct = smooth

            if pos > eps:
                if self._corridor_entry_stage_pct <= 0.0:
                    seeded = float(self._corridor_pending_entry_stage_pct or 0.0)
                    if seeded <= 0.0:
                        seeded = min((level for level in entry_levels if smooth <= level), default=entry_levels[-1])
                    self._corridor_entry_stage_pct = seeded
                    self._corridor_pending_entry_stage_pct = 0.0
                entry_stage = float(self._corridor_entry_stage_pct or 0.0)
                # Live profit-only mode keeps corridor staging for entries, but exits must
                # be owned solely by the standalone profit-roll logic below.
                if profit_only_auto_exits and profit_roll_exit_enabled:
                    self._corridor_exit_armed = False
                    self._corridor_exit_peak_pct = 0.0
                    return None
                profit_target_enabled = (
                    float(features.values.get("corridor_staged_profit_target_enabled", 0.0) or 0.0)
                    >= 0.5
                )
                if profit_target_enabled and entry_stage > 0.0:
                    avg_entry = float(getattr(state, "avg_entry_price", 0.0) or 0.0)
                    # `eps` is a quantity tolerance (BTC/coin units), not a price threshold.
                    # Using it here can accidentally disable staged profit targets for
                    # low-priced symbols (e.g. avg_entry < min_trade_btc).
                    if avg_entry > 0.0 and price > 0.0:
                        # Optional absolute-profit roll mode for corridor exits:
                        # arm by quote-currency PnL and exit on quote-currency retrace.
                        # Keep this opt-in by requiring a positive retrace threshold so
                        # existing staged-percent behavior stays unchanged by default.
                        use_abs_roll = (
                            profit_roll_exit_enabled
                            and profit_roll_arm_eur > 0.0
                            and profit_roll_retrace_eur > 0.0
                        )
                        if use_abs_roll:
                            peak_price = max(float(self._trail_peak_price or 0.0), price)
                            open_pnl_eur = (price - avg_entry) * pos
                            peak_pnl_eur = (peak_price - avg_entry) * pos
                            current_profit_pct = ((price / avg_entry) - 1.0) * 100.0
                            peak_profit_pct = ((peak_price / avg_entry) - 1.0) * 100.0

                            if peak_pnl_eur >= profit_roll_arm_eur:
                                self._corridor_exit_armed = True
                                self._corridor_exit_peak_pct = max(
                                    float(self._corridor_exit_peak_pct or 0.0),
                                    peak_profit_pct,
                                )

                            if self._corridor_exit_armed:
                                self._corridor_exit_peak_pct = max(
                                    float(self._corridor_exit_peak_pct or 0.0),
                                    current_profit_pct,
                                )
                                retrace_eur = max(0.0, peak_pnl_eur - open_pnl_eur)
                                if retrace_eur >= profit_roll_retrace_eur:
                                    required_price = _break_even_required_price()
                                    if required_price > 0.0 and price < required_price:
                                        return RiskDecision(
                                            ts=features.ts,
                                            allow=True,
                                            target_position_btc=state.position_btc,
                                            reason="corridor_stage_hold_break_even",
                                            cooldown_remaining=0,
                                        )
                                    self._reentry_cooldown_remaining = _arm_reentry_cooldown(
                                        "profit_roll_exit"
                                    )
                                    self._corridor_entry_stage_pct = 0.0
                                    self._corridor_pending_entry_stage_pct = 0.0
                                    self._corridor_exit_armed = False
                                    self._corridor_exit_peak_pct = 0.0
                                    return RiskDecision(
                                        ts=features.ts,
                                        allow=True,
                                        target_position_btc=0.0,
                                        reason="profit_roll_exit",
                                        cooldown_remaining=max(0, self._cooldown_remaining),
                                    )
                                return RiskDecision(
                                    ts=features.ts,
                                    allow=True,
                                    target_position_btc=state.position_btc,
                                    reason="corridor_stage_hold",
                                    cooldown_remaining=0,
                                )

                        base_target_pct = max(
                            0.0,
                            float(features.values.get("corridor_staged_profit_target_base_pct", 0.0) or 0.0),
                        )
                        min_target_pct = max(
                            0.0,
                            float(features.values.get("corridor_staged_profit_target_min_pct", 0.0) or 0.0),
                        )
                        max_target_pct = max(
                            min_target_pct,
                            float(
                                features.values.get(
                                    "corridor_staged_profit_target_max_pct",
                                    max(min_target_pct, 100.0),
                                )
                                or max(min_target_pct, 100.0)
                            ),
                        )

                        stage_mult_10 = max(
                            0.1,
                            float(features.values.get("corridor_staged_profit_target_mult_10", 1.25) or 1.25),
                        )
                        stage_mult_20 = max(
                            0.1,
                            float(features.values.get("corridor_staged_profit_target_mult_20", 1.10) or 1.10),
                        )
                        stage_mult_30 = max(
                            0.1,
                            float(features.values.get("corridor_staged_profit_target_mult_30", 1.00) or 1.00),
                        )
                        stage_mult_40 = max(
                            0.1,
                            float(features.values.get("corridor_staged_profit_target_mult_40", 0.90) or 0.90),
                        )
                        stage_mult_50 = max(
                            0.1,
                            float(features.values.get("corridor_staged_profit_target_mult_50", 0.80) or 0.80),
                        )

                        if entry_stage <= 10.0:
                            stage_mult = stage_mult_10
                        elif entry_stage <= 20.0:
                            stage_mult = stage_mult_20
                        elif entry_stage <= 30.0:
                            stage_mult = stage_mult_30
                        elif entry_stage <= 40.0:
                            stage_mult = stage_mult_40
                        else:
                            stage_mult = stage_mult_50

                        target_profit_pct = base_target_pct * stage_mult
                        target_profit_pct = max(min_target_pct, min(max_target_pct, target_profit_pct))

                        if target_profit_pct > 0.0:
                            current_profit_pct = ((price / avg_entry) - 1.0) * 100.0
                            if current_profit_pct >= target_profit_pct:
                                self._corridor_exit_armed = True
                                self._corridor_exit_peak_pct = max(
                                    float(self._corridor_exit_peak_pct or 0.0),
                                    current_profit_pct,
                                )
                            if self._corridor_exit_armed:
                                self._corridor_exit_peak_pct = max(
                                    float(self._corridor_exit_peak_pct or 0.0),
                                    current_profit_pct,
                                )
                                drop_from_peak = max(
                                    0.0,
                                    float(self._corridor_exit_peak_pct or 0.0) - current_profit_pct,
                                )
                                # Use the configured staged retrace threshold for profit-target roll exits,
                                # so ops can tune a clear absolute pullback level (e.g. 0.25%).
                                retrace_trigger_pct = max(0.08, exit_retrace_pct)
                                if drop_from_peak >= retrace_trigger_pct:
                                    required_price = _break_even_required_price()
                                    if required_price > 0.0 and price < required_price:
                                        return RiskDecision(
                                            ts=features.ts,
                                            allow=True,
                                            target_position_btc=state.position_btc,
                                            reason="corridor_stage_hold_break_even",
                                            cooldown_remaining=0,
                                        )
                                    self._reentry_cooldown_remaining = _arm_reentry_cooldown(
                                        "profit_roll_exit"
                                    )
                                    self._corridor_entry_stage_pct = 0.0
                                    self._corridor_pending_entry_stage_pct = 0.0
                                    self._corridor_exit_armed = False
                                    self._corridor_exit_peak_pct = 0.0
                                    return RiskDecision(
                                        ts=features.ts,
                                        allow=True,
                                        target_position_btc=0.0,
                                        reason="profit_roll_exit",
                                        cooldown_remaining=max(0, self._cooldown_remaining),
                                    )
                else:
                    arm_stage = min(no_buy_above, self._corridor_entry_stage_pct + exit_step_pct)
                    if smooth >= max(0.0, arm_stage - hysteresis_pct):
                        self._corridor_exit_armed = True
                        self._corridor_exit_peak_pct = max(float(self._corridor_exit_peak_pct or 0.0), smooth)
                    if self._corridor_exit_armed:
                        self._corridor_exit_peak_pct = max(float(self._corridor_exit_peak_pct or 0.0), smooth)
                        drop_from_peak = max(0.0, float(self._corridor_exit_peak_pct or 0.0) - smooth)
                        sharp_break = smooth <= max(0.0, arm_stage - hysteresis_pct)
                        rolling_break = drop_from_peak >= exit_retrace_pct
                        if sharp_break or rolling_break:
                            # For staged roll exits, require cost-aware break-even first.
                            # This only gates the exit trigger itself; it does not create an
                            # earlier standalone break-even exit signal.
                            required_price = _break_even_required_price()
                            if required_price > 0.0 and price < required_price:
                                return RiskDecision(
                                    ts=features.ts,
                                    allow=True,
                                    target_position_btc=state.position_btc,
                                    reason="corridor_stage_hold_break_even",
                                    cooldown_remaining=0,
                                )
                            self._reentry_cooldown_remaining = _arm_reentry_cooldown("profit_roll_exit")
                            self._corridor_entry_stage_pct = 0.0
                            self._corridor_pending_entry_stage_pct = 0.0
                            self._corridor_exit_armed = False
                            self._corridor_exit_peak_pct = 0.0
                            return RiskDecision(
                                ts=features.ts,
                                allow=True,
                                target_position_btc=0.0,
                                reason="profit_roll_exit",
                                cooldown_remaining=max(0, self._cooldown_remaining),
                            )
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=state.position_btc,
                    reason="corridor_stage_hold",
                    cooldown_remaining=0,
                )

            self._corridor_entry_stage_pct = 0.0
            self._corridor_exit_armed = False
            self._corridor_exit_peak_pct = 0.0

            if self._reentry_cooldown_remaining > 0:
                self._reentry_cooldown_remaining -= 1
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="reentry_cooldown",
                    cooldown_remaining=self._reentry_cooldown_remaining,
                )
            if (
                reentry_min_move_bps > 0.0
                and self._last_long_exit_price > 0.0
                and price > 0.0
            ):
                move_bps = abs(price - self._last_long_exit_price) / self._last_long_exit_price * 10000.0
                if move_bps < reentry_min_move_bps:
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="reentry_move_too_small",
                        cooldown_remaining=0,
                    )
            if (
                reentry_require_price_at_or_below_last_entry
                and self._last_long_entry_price > 0.0
                and price > 0.0
            ):
                max_reentry_price = self._last_long_entry_price * (
                    1.0 + (reentry_last_entry_tolerance_bps / 10000.0)
                )
                if price > max_reentry_price:
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="reentry_above_last_entry",
                        cooldown_remaining=0,
                    )
            if not corridor_ready:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="corridor_not_ready",
                    cooldown_remaining=0,
                )
            if smooth > no_buy_above:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="corridor_too_high",
                    cooldown_remaining=0,
                )

            breached_levels = [level for level in entry_levels if smooth <= (level - hysteresis_pct)]
            if breached_levels:
                breached_stage = min(breached_levels)
                if self._corridor_armed_stage_pct <= 0.0:
                    self._corridor_armed_stage_pct = breached_stage
                    self._corridor_armed_stage_age_bars = 0
                else:
                    prior_stage = float(self._corridor_armed_stage_pct)
                    self._corridor_armed_stage_pct = min(self._corridor_armed_stage_pct, breached_stage)
                    if self._corridor_armed_stage_pct < prior_stage:
                        self._corridor_armed_stage_age_bars = 0
                if self._corridor_lowest_pos_pct <= 0.0:
                    self._corridor_lowest_pos_pct = smooth
                else:
                    self._corridor_lowest_pos_pct = min(self._corridor_lowest_pos_pct, smooth)

            armed_stage = float(self._corridor_armed_stage_pct or 0.0)
            if armed_stage <= 0.0:
                self._corridor_armed_stage_age_bars = 0
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="corridor_wait_stage_breach",
                    cooldown_remaining=0,
                )

            if require_rising:
                return_bps = float(features.values.get("return_bps", 0.0) or 0.0)
                trend_return_bps = float(features.values.get("trend_return_bps", 0.0) or 0.0)
                smooth_delta = smooth - prior_smooth
                price_falling = return_bps < 0.0
                structure_falling = smooth_delta < 0.0
                trend_falling = trend_return_bps < 0.0
                if price_falling or (structure_falling and trend_falling):
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="corridor_wait_rising_price",
                        cooldown_remaining=0,
                    )

            lowest_stage = min(entry_levels) if entry_levels else armed_stage
            if smooth > (armed_stage - hysteresis_pct):
                self._corridor_armed_stage_age_bars = 0
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="corridor_wait_stage_touch",
                    cooldown_remaining=0,
                )
            if armed_stage > (lowest_stage + 1e-9):
                self._corridor_armed_stage_age_bars = max(
                    0, int(getattr(self, "_corridor_armed_stage_age_bars", 0) or 0)
                ) + 1
                if self._corridor_armed_stage_age_bars < entry_wait_bars:
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="corridor_wait_lower_stage_window",
                        cooldown_remaining=0,
                    )
            else:
                self._corridor_armed_stage_age_bars = 0

            if price <= 0.0:
                return RiskDecision(
                    ts=features.ts,
                    allow=False,
                    target_position_btc=state.position_btc,
                    reason="invalid_price",
                    cooldown_remaining=0,
                )
            atr_bps = float(features.values.get("atr_bps", 0.0) or 0.0)
            vol_scale = 1.0
            if self.config.use_vol_scaling and atr_bps > 0.0:
                vol_scale = min(1.0, self.config.vol_target_bps / atr_bps)
            target_eur = self.config.max_exposure_eur * vol_scale
            if target_eur <= 0.0:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="corridor_no_target",
                    cooldown_remaining=0,
                )
            target_btc = target_eur / price
            self._corridor_pending_entry_stage_pct = armed_stage
            self._corridor_armed_stage_age_bars = 0
            self._corridor_exit_armed = False
            self._corridor_exit_peak_pct = 0.0
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=target_btc,
                reason="corridor_stage_entry",
                cooldown_remaining=0,
            )

        corridor_decision = _corridor_staged_mode_decision()
        if corridor_decision is not None:
            return corridor_decision

        if _hard_take_profit_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("hard_take_profit")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="hard_take_profit",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _green_candle_take_exit_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("green_candle_take_exit")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="green_candle_take_exit",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _time_break_even_floor_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("time_break_even_floor")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="time_break_even_floor",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _red_candle_exit_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("red_candle_exit")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="red_candle_exit",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _chop_break_even_reclaim_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("chop_break_even_reclaim")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="chop_break_even_reclaim",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _failed_start_exit_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("failed_start_exit")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="failed_start_exit",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _hard_stop_loss_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("hard_stop_loss")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="hard_stop_loss",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _trailing_stop_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("trailing_stop")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="trailing_stop",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _peak_profit_retrace_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("peak_profit_retrace")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="peak_profit_retrace",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if _profit_roll_exit_hit():
            self._reentry_cooldown_remaining = _arm_reentry_cooldown("profit_roll_exit")
            return RiskDecision(
                ts=features.ts,
                allow=True,
                target_position_btc=0.0,
                reason="profit_roll_exit",
                cooldown_remaining=max(0, self._cooldown_remaining),
            )
        if price > 0.0 and abs(state.position_btc * price) > self.config.max_exposure_eur:
            # Being over the exposure cap should not freeze exits via cooldown.
            # Keep the reason for telemetry, but let regular reduction/flatten logic run.
            reason = reason or "max_exposure"
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return RiskDecision(
                ts=features.ts,
                allow=False,
                target_position_btc=state.position_btc,
                reason=reason or "cooldown",
                cooldown_remaining=self._cooldown_remaining,
            )
        # Exit must never be blocked by entry gating.
        #
        # Gate decisions are meant to block *new risk* (entries/position increases) when conditions
        # are bad (wide spread, high volatility, off-session, edge below costs).
        # If we already have a position and the model wants to go flat, allow that reduction even
        # when the gate blocks.
        if not gate.allow:
            if pos > eps and min_hold_bars > 0 and self._bars_in_position < min_hold_bars:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=state.position_btc,
                    reason="hold_min_bars",
                    cooldown_remaining=0,
                )
            if pos > eps and _campaign_hold_active():
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=state.position_btc,
                    reason="campaign_hold",
                    cooldown_remaining=0,
                )
            # Long-only default: allow gate-bypassed flattening only under clearly negative edge.
            if pos > eps and predicted_edge_bps <= exit_bypass_gate_edge_bps:
                if not _break_even_exit_ok():
                    if _reversal_exit_after_break_even_ok():
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="reversal_exit_after_break_even",
                            cooldown_remaining=0,
                        )
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=state.position_btc,
                        reason="wait_break_even",
                        cooldown_remaining=0,
                    )
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="exit_bypass_gate",
                    cooldown_remaining=0,
                )
            # If shorts are allowed and we're short: allow flattening when the signal no longer supports shorts.
            if pos < -eps and self.config.allow_short and predicted_edge_bps >= 0.0:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=0.0,
                    reason="exit_bypass_gate",
                    cooldown_remaining=0,
                )
            return RiskDecision(
                ts=features.ts,
                allow=False,
                target_position_btc=state.position_btc,
                reason="gate_block",
                cooldown_remaining=0,
            )
        if not self.config.allow_short:
            if pos > eps:
                if min_hold_bars > 0 and self._bars_in_position < min_hold_bars:
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=state.position_btc,
                        reason="hold_min_bars",
                        cooldown_remaining=0,
                    )
                if predicted_edge_bps <= exit_edge_bps:
                    if not _break_even_exit_ok():
                        if _reversal_exit_after_break_even_ok():
                            return RiskDecision(
                                ts=features.ts,
                                allow=True,
                                target_position_btc=0.0,
                                reason="reversal_exit_after_break_even",
                                cooldown_remaining=0,
                            )
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=state.position_btc,
                            reason="wait_break_even",
                            cooldown_remaining=0,
                        )
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="edge_exit",
                        cooldown_remaining=0,
                    )
                if full_position_only:
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=state.position_btc,
                        reason="hold_full_position",
                        cooldown_remaining=0,
                    )
            else:
                alpha_staircase_override = bool(
                    float(features.values.get("alpha_staircase_override", 0.0) or 0.0) > 0.0
                )
                alpha_impulse_override = bool(
                    float(features.values.get("alpha_impulse_override", 0.0) or 0.0) > 0.0
                )
                staircase_edge_surplus_bps = max(
                    0.0,
                    float(predicted_edge_bps or 0.0) - max(0.0, float(expected_cost_bps or 0.0)),
                )
                if self._reentry_cooldown_remaining > 0:
                    self._reentry_cooldown_remaining -= 1
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="reentry_cooldown",
                        cooldown_remaining=self._reentry_cooldown_remaining,
                    )
                effective_reentry_min_move_bps = reentry_min_move_bps
                if alpha_staircase_override and effective_reentry_min_move_bps > 0.0:
                    # Staircase re-entries are expected to happen on shallower pullbacks than
                    # generic continuation retries; keep the guard, but stop requiring a full reset.
                    effective_reentry_min_move_bps = min(effective_reentry_min_move_bps, 18.0)
                    if staircase_edge_surplus_bps >= max(
                        4.0,
                        max(0.0, float(expected_cost_bps or 0.0)) * 0.35,
                    ):
                        effective_reentry_min_move_bps = min(effective_reentry_min_move_bps, 12.0)
                if (
                    effective_reentry_min_move_bps > 0.0
                    and self._last_long_exit_price > 0.0
                    and price > 0.0
                ):
                    move_bps = abs(price - self._last_long_exit_price) / self._last_long_exit_price * 10000.0
                    if move_bps < effective_reentry_min_move_bps:
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="reentry_move_too_small",
                            cooldown_remaining=0,
                        )
                if (
                    reentry_require_price_at_or_below_last_entry
                    and self._last_long_entry_price > 0.0
                    and price > 0.0
                ):
                    max_reentry_price = self._last_long_entry_price * (
                        1.0 + (reentry_last_entry_tolerance_bps / 10000.0)
                    )
                    if price > max_reentry_price:
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="reentry_above_last_entry",
                            cooldown_remaining=0,
                        )
                context_range_pos = max(
                    0.0,
                    min(1.0, float(features.values.get("context_range_pos", 0.0) or 0.0)),
                )
                structure_range_pos = max(
                    0.0,
                    min(1.0, float(features.values.get("alpha_structure_range_pos", 0.0) or 0.0)),
                )
                trend_return_bps = max(
                    0.0,
                    float(features.values.get("trend_return_bps", 0.0) or 0.0),
                )
                return_bps = max(
                    0.0,
                    float(features.values.get("return_bps", 0.0) or 0.0),
                )
                context_drawdown_from_peak_bps = _feature_bps_or_none(
                    "context_drawdown_from_peak_bps",
                    "context_drawdown_bps",
                )
                structure_drawdown_from_peak_bps = _feature_bps_or_none(
                    "alpha_structure_drawdown_from_peak_bps",
                )
                pullback_room_drawdown_bps = context_drawdown_from_peak_bps
                if pullback_room_drawdown_bps is None:
                    pullback_room_drawdown_bps = structure_drawdown_from_peak_bps
                late_entry_top_zone = (
                    context_range_pos > late_entry_block_context_range_pos
                    or structure_range_pos > late_entry_block_structure_range_pos
                )
                late_entry_extension = True
                if late_entry_block_min_trend_return_bps > 0.0:
                    late_entry_extension = (
                        late_entry_extension
                        and trend_return_bps >= late_entry_block_min_trend_return_bps
                    )
                if late_entry_block_min_return_bps > 0.0:
                    late_entry_extension = (
                        late_entry_extension and return_bps >= late_entry_block_min_return_bps
                    )
                late_entry_pullback_room_too_small = (
                    late_entry_block_max_context_drawdown_bps > 0.0
                    and pullback_room_drawdown_bps is not None
                    and pullback_room_drawdown_bps <= late_entry_block_max_context_drawdown_bps
                )
                if (
                    not alpha_staircase_override
                    and
                    late_entry_top_zone
                    and late_entry_extension
                    and late_entry_pullback_room_too_small
                ):
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=0.0,
                        reason="late_entry_top_zone",
                        cooldown_remaining=0,
                )
                if alpha_staircase_override or alpha_impulse_override:
                    drawdown_from_peak_bps = max(0.0, float(structure_drawdown_from_peak_bps or 0.0))
                    slope_short_bps = float(
                        features.values.get("alpha_structure_slope_short_bps", 0.0) or 0.0
                    )
                    effective_override_max_structure_range_pos = override_max_structure_range_pos
                    effective_required_override_drawdown_bps = max(
                        override_min_drawdown_from_peak_bps,
                        max(0.0, float(expected_cost_bps or 0.0)) * override_min_drawdown_to_cost_ratio,
                    )
                    effective_override_min_slope_short_bps = override_min_slope_short_bps
                    if alpha_staircase_override:
                        # Staircase lives close to local highs by design; use the override-specific
                        # guards instead of the generic top-zone block and allow shallower pullback
                        # room before calling it "too close".
                        effective_override_max_structure_range_pos = min(
                            1.0,
                            max(effective_override_max_structure_range_pos, 0.9985),
                        )
                        effective_required_override_drawdown_bps = min(
                            effective_required_override_drawdown_bps,
                            max(1.0, max(0.0, float(expected_cost_bps or 0.0)) * 0.20),
                        )
                        effective_override_min_slope_short_bps = min(
                            effective_override_min_slope_short_bps,
                            0.0,
                        )
                        if staircase_edge_surplus_bps >= max(
                            6.0,
                            max(0.0, float(expected_cost_bps or 0.0)) * 0.50,
                        ):
                            effective_override_max_structure_range_pos = min(
                                1.0,
                                max(effective_override_max_structure_range_pos, 0.9992),
                            )
                            effective_required_override_drawdown_bps = min(
                                effective_required_override_drawdown_bps,
                                0.5,
                            )
                    if structure_range_pos > effective_override_max_structure_range_pos:
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="override_too_close_to_peak",
                            cooldown_remaining=0,
                        )
                    if drawdown_from_peak_bps < effective_required_override_drawdown_bps:
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="override_no_pullback_room",
                            cooldown_remaining=0,
                        )
                    if slope_short_bps < effective_override_min_slope_short_bps:
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="override_short_slope_weak",
                            cooldown_remaining=0,
                        )
                    if (
                        override_max_trend_return_bps > 0.0
                        and trend_return_bps > override_max_trend_return_bps
                        and context_range_pos > override_max_context_range_pos
                    ):
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="override_extended_trend",
                            cooldown_remaining=0,
                        )
                atr_bps = max(0.0, float(features.values.get("atr_bps", 0.0) or 0.0))
                spread_bps = max(0.0, float(features.values.get("spread_bps", 0.0) or 0.0))
                alpha_swing_micro_rebound = (
                    float(features.values.get("alpha_swing_micro_valley_rebound", 0.0) or 0.0) >= 0.5
                )
                alpha_swing_valley_rebound = (
                    float(features.values.get("alpha_swing_valley_rebound", 0.0) or 0.0) >= 0.5
                )
                effective_entry_min_atr_to_cost_ratio = entry_min_atr_to_cost_ratio
                effective_entry_cost_coverage_ratio = entry_cost_coverage_ratio
                if context_range_pos <= 0.45 and spread_bps <= 12.0:
                    # Let genuine bottom swing rebounds open a bit earlier without
                    # weakening the generic entry guards for continuation/breakout.
                    if alpha_swing_micro_rebound:
                        effective_entry_min_atr_to_cost_ratio = min(
                            effective_entry_min_atr_to_cost_ratio,
                            0.48,
                        )
                        effective_entry_cost_coverage_ratio = min(
                            effective_entry_cost_coverage_ratio,
                            0.42,
                        )
                    elif alpha_swing_valley_rebound:
                        effective_entry_min_atr_to_cost_ratio = min(
                            effective_entry_min_atr_to_cost_ratio,
                            0.58,
                        )
                        effective_entry_cost_coverage_ratio = min(
                            effective_entry_cost_coverage_ratio,
                            0.44,
                        )
                if not disable_entry_edge_gate:
                    if effective_entry_min_atr_to_cost_ratio > 0.0:
                        required_atr_bps = (
                            max(0.0, float(expected_cost_bps or 0.0)) * effective_entry_min_atr_to_cost_ratio
                        )
                        if atr_bps < required_atr_bps:
                            return RiskDecision(
                                ts=features.ts,
                                allow=True,
                                target_position_btc=0.0,
                                reason="atr_below_entry_costs",
                                cooldown_remaining=0,
                            )
                    required_entry_edge_bps = max(
                        entry_edge_bps,
                        max(0.0, float(expected_cost_bps or 0.0))
                        * entry_cost_roundtrip_multiplier
                        * effective_entry_cost_coverage_ratio
                        + entry_cost_buffer_bps,
                    )
                    if predicted_edge_bps <= required_entry_edge_bps:
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=0.0,
                            reason="edge_below_entry",
                            cooldown_remaining=0,
                        )

        atr_bps = float(features.values.get("atr_bps", 0.0))
        vol_scale = 1.0
        if self.config.use_vol_scaling and atr_bps > 0:
            vol_scale = min(1.0, self.config.vol_target_bps / atr_bps)
        gate_scale = float(gate.size_factor) if self.config.use_gate_size_factor else 1.0
        target_eur = self.config.max_exposure_eur * gate_scale * vol_scale
        if price <= 0:
            return RiskDecision(
                ts=features.ts,
                allow=False,
                target_position_btc=state.position_btc,
                reason="invalid_price",
                cooldown_remaining=0,
            )
        target_btc = target_eur / price

        # Entry liquidity guard: prevent oversized buys in thin books.
        # Uses top-of-book notional depth published by market data as `depth`.
        if target_btc > (pos + eps):
            depth_eur = max(0.0, float(features.values.get("depth", 0.0) or 0.0))
            if min_entry_depth_eur > 0.0 and depth_eur < min_entry_depth_eur:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=pos,
                    reason="entry_depth_low",
                    cooldown_remaining=0,
                )
            if max_entry_notional_to_depth_ratio > 0.0:
                if depth_eur <= 0.0:
                    return RiskDecision(
                        ts=features.ts,
                        allow=True,
                        target_position_btc=pos,
                        reason="entry_depth_unknown",
                        cooldown_remaining=0,
                    )
                max_entry_eur = depth_eur * max_entry_notional_to_depth_ratio
                if target_eur > (max_entry_eur + 1e-9):
                    capped_target_btc = max_entry_eur / price
                    if capped_target_btc <= (pos + eps):
                        return RiskDecision(
                            ts=features.ts,
                            allow=True,
                            target_position_btc=pos,
                            reason="entry_depth_cap",
                            cooldown_remaining=0,
                        )
                    target_eur = max_entry_eur
                    target_btc = capped_target_btc
        if predicted_edge_bps < 0.0 and self.config.allow_short:
            target_btc = -target_btc
        # Guard against fee-heavy chop: when break-even gating is enabled, do not allow
        # long position reductions below break-even (except explicit hard exits above).
        if pos > eps and target_btc < pos:
            if not _break_even_exit_ok():
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=pos,
                    reason="wait_break_even_reduce",
                    cooldown_remaining=0,
                )
        if rebalance_min_delta_eur > 0.0:
            delta_eur = abs(target_btc - pos) * price
            if delta_eur < rebalance_min_delta_eur:
                return RiskDecision(
                    ts=features.ts,
                    allow=True,
                    target_position_btc=pos,
                    reason="rebalance_deadband",
                    cooldown_remaining=0,
                )
        if dynamic_profit_target_enabled and pos <= eps and target_btc > eps:
            self._dynamic_profit_target_bps_active = self._compute_dynamic_profit_target_bps(price, features)
        return RiskDecision(
            ts=features.ts,
            allow=True,
            target_position_btc=target_btc,
            reason=None,
            cooldown_remaining=0,
        )
