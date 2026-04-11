# Rotation Strategy Settings Catalog

This file explains where each live core strategy setting lives, which `ver.4`
artifact is current, and how the latest filtered lab rerun should be interpreted.

## Core Scope

The active core strategies are:

- `rebound`
- `staircase`
- `continuation`
- `breakout`

The source of truth is:

- [rotation_strategy_settings_catalog.yaml](/home/andi/Schreibtisch/codex/bitcoin2/configs/rotation_strategy_settings_catalog.yaml)
- [rotation_live_trade_issue_backlog.md](/home/andi/Schreibtisch/codex/bitcoin2/docs/rotation_live_trade_issue_backlog.md)

The latest catalog baseline is stored as `ver.4`. Historical lab artifacts remain:

- [rebound_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/rebound_ver.3.json)
- [staircase_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/staircase_ver.3.json)
- [continuation_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/continuation_ver.3.json)
- [breakout_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/breakout_ver.3.json)
- [index.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/index.json)

## Latest Rerun

The baseline decision history comes from the filtered `48h` rerun generated at
`2026-03-17 08:58:47 UTC`.

Important constraints:

- Technical execution artifacts were excluded from the trade sample set.
- The rebound lab was corrected to use the real live `swing micro_rebound` keys.
- The rerun therefore supersedes the older `ver.2` continuation/rebound wording.

High-level result:

- `rebound`: accepted `ver.3`
- `breakout`: accepted `ver.3`
- `continuation`: no better `ver.3`, carry forward current live bundle
- `staircase`: opened to `ver.4` after the structural phase bottleneck was fixed

## Live Apply Path

1. Edit [rotation_selector_watch_pool.env](/home/andi/Schreibtisch/codex/bitcoin2/configs/rotation_selector_watch_pool.env).
2. Mirror the strategy keys into [rotation_meta_runtime.env](/home/andi/Schreibtisch/codex/bitcoin2/configs/rotation_meta_runtime.env).
3. Restart `codex-rotation-selector.service`.
4. Verify [rotation_active_lanes.json](/home/andi/Schreibtisch/codex/bitcoin2/configs/rotation_active_lanes.json).
5. Verify the generated runtimes in [rotation_runtime_configs](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_runtime_configs).

## Strategy Notes

`rebound`

- Live key: `ROTATION_SWING_REVERSAL_THRESHOLD_BPS`
- Current live value: `0.0`
- Live artifact: [rebound_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/rebound_ver.3.json)
- Accepted candidate: `rebound_0.0`
- Result: total trade count `7 -> 8`, `net_pnl_delta = +0.002993`
- Interpretation: the reversal threshold stayed widened, and live `ver.3` was later refined to admit earlier low-context micro-rebounds at `min_range_bps=40.0`, `micro_rebound_max_spread_bps=18.0`, `micro_rebound_min_context_rebound_bps=120.0`
- Additional live refinement after the `PLUME` trade sold on `2026-03-17 16:13:25 CET`: if a micro-rebound has already reclaimed at least `180 bps`, it now waits for a green confirmation candle instead of buying on a still-red bar

`staircase`

- Live key: `ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS`
- Current live value: `0.0`
- Live artifact: [staircase.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/staircase.json)
- Accepted candidate: live `ver.4` structural unlock
- Result: the filtered `48h` lab is still sparse, but live selection immediately restored a real staircase slot
- Interpretation: `ver.4` does not blindly widen continuation; it unlocks constructive `stall` staircase entries, lowers the staircase trend floors only in healthy structure, and relaxes selector admission for true staircase shapes

`continuation`

- Live key: `ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS`
- Current live value: `0.95`
- Live artifact: [continuation_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/continuation_ver.3.json)
- Accepted candidate: none beyond current live baseline
- Result: current live bundle stayed best in the filtered `48h` rerun
- Interpretation: continuation is carried forward unchanged in `ver.3`

`breakout`

- Live key: `ROTATION_BREAKOUT_TRIGGER_BPS`
- Current live value: `7.0`
- Live artifact: [breakout_ver.3.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/breakout_ver.3.json)
- Accepted candidate: `breakout_exit_relief_force`
- Result: `net_pnl_delta = +0.457287`, `1` improved exit, fewer early-exit bugs
- Interpretation: keep the trigger at `7.0`; improve breakout tradeability through the stronger exit stack
- Post-trade live refinement: block weak-volume rebound breakouts already from mid-context when `context_range_pos >= 0.48`, `context_rebound_bps >= 760`, and `volume_z < 0.0`, and keep the stricter late-rebound block at `0.72 / 1400 / 0.35`
- Additional live refinement after the `ZK` trade on `2026-03-17 14:45:51 CET`: block reclaimed breakouts with already expensive microstructure at `0.60 / 400 / 18.0` for `context_range_pos / context_rebound_bps / spread_bps`
- Additional live refinement after the `PUMP` trade sold on `2026-03-17 16:11:41 CET`: block low-context countertrend breakouts at `0.22 / 140 / -80` for `context_range_pos / context_rebound_bps / trend_return_bps`
- Exit refinement: let bottom-reclaim breakouts survive the failed-start timer a bit longer when the local breakout is still intact, so `RESOLV`-style early clips do not repeat
- Additional exit refinement after the later `ZK` trade bought at `2026-03-17 14:59:05 CET`: the time-break-even floor now gives still-constructive breakouts a bounded extra grace window instead of flattening them immediately just below entry

## Practical Meaning

`ver.3` is not a blanket opening of all four strategies.

- `rebound` is the main tradeability opener, now slightly wider at the low-context micro-rebound edge without opening the late chase path.
- `breakout` is the new exit-quality improvement.
- `continuation` stays profit-oriented at the current live setting.
- `staircase` stays conservative because the sample quality is still too weak.
