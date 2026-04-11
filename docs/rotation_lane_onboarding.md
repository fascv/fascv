# Rotation Lane Onboarding

Use this checklist whenever a new live rotation coin is added.

1. Confirm the pair is really tradable on the user account.
   Read-only first, then verify exchange filters:
   - `PRICE_FILTER`
   - `LOT_SIZE`
   - `NOTIONAL`

2. Start from a coin-specific optimized profile.
   Keep the alpha family from the last useful walk-forward result, then layer the live safety rules on top.

3. Apply the shared live safety scaffold.
   Required fields:
   - `risk.position_epsilon_eur`
   - `exec.sync_min_position_eur`
   - `full_position_only`
   - `require_break_even_for_exit`
   - hard stop / trailing / cooldown

4. Recompute lane capital fraction for all active lanes.
   Formula:
   - `lane_fraction = 1 / active_lane_count`

   Update in every active lane config:
   - `risk.max_exposure_fraction`
   - `order.cycle_trade_fraction`

5. Keep pair-specific exchange constraints explicit.
   Set:
   - `order.min_trade_btc`
   - fees
   - spread gate
   - ATR limits

6. Give the lane its own journals and its own ports.
   Reserve one control port block per lane:
   - control
   - exec
   - journal
   - core
   - md

7. Restart all affected lanes after a lane-count change.
   Reason:
   - the new lane fraction only becomes live after restart
   - warmup and account sync need a clean start
   Use:
   - `./scripts/rotation_guards_stop.sh`
   - `./scripts/rotation_guards_start.sh`

9. Keep the runtime detached from the terminal.
   The active lanes should run behind per-lane guards, not in an interactive shell.
   Check with:
   - `./scripts/rotation_guards_status.sh`

10. Verify the first live roundtrip before further tuning.
   Check:
   - actual fill size
   - exit reason
   - whether fees turned a tiny move into a loss

Current working pattern:
- `OP/USDC`: oscillation lane
- `NEAR/USDC`: liquid oscillation lane
- `ENA/USDC`: additional liquid oscillation lane
- `RENDER/USDC`: additional liquid oscillation lane
- `DOT/USDC`: additional watch/trade lane
- `HBAR/USDC`: additional watch/trade lane
- `ESP/USDC`: additional watch/trade lane
- `KITE/USDC`: additional watch/trade lane
