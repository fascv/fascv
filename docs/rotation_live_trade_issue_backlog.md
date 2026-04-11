# Rotation Live Trade Issue Backlog

This file is the durable handoff for live trade review findings that should
survive future chats and future strategy lab reruns.

## Scope

- Review window: `2026-03-15 04:00 CET` to `2026-03-15 18:17 CET`
- Source: fill-based roundtrips from `logs/journal_live_binance_*_rotation.jsonl`
- Covered exits: `28`

## Error Classes

- `execution_reconcile_error` (`4`)
  - `PLUME 10:33 CET`
    - `breakout`
    - `hard_stop_loss`
    - `-165.62 bps`
    - exit first hit `exec_sell_qty_clamped_to_balance` / `exec_sell_skipped_below_min_notional`
      and only then recovered the real position
  - `PLUME 11:49 CET`
    - `continuation`
    - `trailing_stop`
    - `+31.47 bps`
    - profitable exit, but still mixed with `entry_reference_recovered` and
      `account_sync_delta:event:add_order_insufficient_balance`
  - `CRV 15:39 CET`
    - `breakout`
    - `daily_loss_limit`
    - `-12.67 bps`
    - exit was mixed with recovery/account-sync behavior
  - `ZAMA 17:16 CET`
    - `breakout`
    - `daily_loss_limit`
    - `+32.20 bps`
    - profitable, but operationally contaminated by the same recovery pattern

- `too_early_stop_exit` (`3`)
  - `INIT 15:11 CET`
    - `breakout`
    - `hard_stop_loss`
    - `-115.06 bps`
    - hold `9.42 min`
  - `INIT 15:23 CET`
    - `breakout`
    - `hard_stop_loss`
    - `-94.14 bps`
    - hold `4.11 min`
  - `TST 13:58 CET`
    - `breakout`
    - `hard_stop_loss`
    - `-87.55 bps`
    - hold `5.53 min`

- `fast_loser` (`2`)
  - `PLUME 10:53 CET`
    - `breakout`
    - `failed_start_exit`
    - `-47.54 bps`
    - hold `8.98 min`
  - `ZK 12:02 CET`
    - `breakout`
    - `daily_loss_limit`
    - `-31.50 bps`
    - hold `10.21 min`

- `reconcile_only_exit` (`3`)
  - `FET 14:17 CET`
    - `breakout`
    - `trailing_stop`
    - `+345.56 bps`
    - extra `account_sync_delta:periodic` mirror; strategy result is still
      usable, but execution cleanliness is not perfect
  - `FLUX 17:21 CET`
    - `continuation`
    - `trailing_stop`
    - `+70.92 bps`
    - extra periodic sync mirror
  - `RENDER 08:06 CET`
    - `breakout`
    - `time_break_even_floor`
    - `+21.83 bps` gross, effectively flat after fees
    - extra periodic sync mirror

- `normal_or_unclear` (`16`)
  - This bucket contains the remaining exits whose main issue is not clearly an
    exit bug. Some are good winners, some are long-held neutral exits, and some
    are legacy positions that only happened to close inside the review window.

## Review Addendum

- Review window: `2026-03-16 00:00 UTC` to `2026-03-17 00:00 UTC`
- Source: `/trades/report` fill bundles aggregated by `(symbol, sellTime)` so a
  multi-lot flatten is counted as one exit event
- Covered exits: `37` unique exits from `46` fill bundles across `24` symbols
- Important interpretation rule:
  - `entry_reference_recovered` appeared around essentially the whole sample and
    is no longer discriminative on its own
  - only stronger artifacts like `account_sync_delta`, `insufficient_balance`,
    qty clamping, or reconcile errors should move an exit into the execution
    bug bucket
- Additional interpretation rule:
  - `9` exits were mixed-age flatten events where one sell closed multiple entry
    lots with very different hold times
  - those exits are still operationally real, but they are weak lab evidence
    for alpha tuning because the sell reason is shared across old and fresh lots

### Error Classes

- `execution_reconcile_error` (`3`)
  - `BONK 01:04 UTC`
    - `continuation`
    - `failed_start_exit`
    - `-83.48 bps`
    - exit still hit `account_sync_delta` plus `exec_sell_qty_clamped_to_balance`
  - `SENT 13:28 UTC`
    - `breakout`
    - `daily_loss_limit`
    - `-36.83 bps`
    - exit included `insufficient_balance`
  - `VIRTUAL 14:57 UTC`
    - `continuation`
    - `trailing_stop`
    - `+52.84 bps` aggregated across two lots
    - profitable, but mixed with `account_sync_delta` and `insufficient_balance`

- `too_early_stop_exit` (`5`)
  - `TST 00:39 UTC`
    - `breakout`
    - `failed_start_exit`
    - `-67.58 bps`
    - hold `16.92 min`
  - `VIRTUAL 01:34 UTC`
    - `breakout`
    - `failed_start_exit`
    - `-69.45 bps`
    - hold `7.24 min`
  - `FET 01:57 UTC`
    - `breakout`
    - `failed_start_exit`
    - `-74.63 bps`
    - hold `7.43 min`
  - `MANTRA 11:04 UTC`
    - `continuation`
    - `hard_stop_loss`
    - `-114.04 bps`
    - hold `2.08 min`
  - `BREV 13:56 UTC`
    - `breakout`
    - `hard_stop_loss`
    - `-106.22 bps`
    - hold `16.84 min`

- `too_early_floor_exit` (`5`)
  - `ESP 00:44 UTC`
    - `breakout`
    - `time_break_even_floor`
    - `-69.50 bps`
    - hold `19.61 min`
  - `SEI 03:29 UTC`
    - `breakout`
    - `time_break_even_floor`
    - `-62.89 bps`
    - hold `19.28 min`
  - `FIL 04:02 UTC`
    - `breakout`
    - `time_break_even_floor`
    - `-51.64 bps`
    - hold `18.33 min`
  - `NEAR 12:30 UTC`
    - `breakout`
    - `time_break_even_floor`
    - `-33.28 bps`
    - hold `19.65 min`
  - `APT 14:35 UTC`
    - `continuation`
    - `time_break_even_floor`
    - `+10.61 bps`
    - hold `18.16 min`
    - technically green, but still very thin for a floor-style exit

- `portfolio_loss_clamp` (`5`)
  - `CFX 01:51 UTC`
    - `breakout`
    - `daily_loss_limit`
    - `-79.57 bps`
    - hold `12.70 min`
  - `CFX 02:50 UTC`
    - `breakout`
    - `daily_loss_limit`
    - `-41.20 bps`
    - hold `5.69 min`
  - `FLUX 03:46 UTC`
    - `continuation`
    - `daily_loss_limit`
    - `-105.62 bps`
    - hold `7.89 min`
  - `IMX 09:01 UTC`
    - `breakout`
    - `daily_loss_limit`
    - `-42.21 bps`
    - hold `0.11 min`
  - `SKY 13:36 UTC`
    - `breakout`
    - `daily_loss_limit`
    - `-31.02 bps`
    - hold `10.07 min`

- `mixed_age_exit` (`8`)
  - These are real exits, but one sell flattened multiple lots with very
    different hold times. Examples:
    - `ETHFI 08:15 UTC`: `failed_start_exit`, hold range `8.82` to `325.47 min`
    - `VIRTUAL 10:23 UTC`: `time_break_even_floor`, hold range `18.00` to
      `536.50 min`
    - `VIRTUAL 14:38 UTC`: `daily_loss_limit`, hold range `6.27` to
      `273.02 min`
    - `DOT 07:52 UTC`: `time_break_even_floor`, hold range `0.59` to `82.36 min`
  - Treat these as weak alpha evidence until the sell logic is reviewed against
    lot age and stacked entry history.

- `normal_or_unclear` (`11`)
  - This bucket contains the remaining single-lot or non-pathological exits,
    including normal trailing winners like `KERNEL 01:11 UTC`, `CFX 03:34 UTC`,
    and `FET 11:11 UTC`.

### Per Strategy Follow-up

`breakout`

- Dominates the new problem sample with `13` of `18` problem exits.
- The new pain cluster is no longer only very-early stops.
- `time_break_even_floor` and `daily_loss_limit` now matter at least as much as
  `failed_start_exit` / `hard_stop_loss`.
- Fresh breakout lab evidence should prioritize clean single-lot exits from this
  new window over mixed-age flatten events.

`continuation`

- `5` of `18` problem exits came from continuation lanes.
- The main continuation issues in this window were:
  - one extreme `hard_stop_loss` loser on `MANTRA`
  - one weak `time_break_even_floor` on `APT`
  - one `daily_loss_limit` loser on `FLUX`
  - two execution-contaminated cases on `BONK` and `VIRTUAL`
- This is enough to keep continuation on the watch list, but not yet enough to
  justify a broad entry retune from this sample alone.

## Per Strategy Follow-up

`breakout`

- Trader follow-up:
  - Separate true breakout exit behavior from recovery-contaminated exits before
    changing alpha logic.
  - Keep watching the interaction between `hard_stop_loss`,
    `failed_start_exit`, `daily_loss_limit`, and the sell recovery path.
- Lab / catalog follow-up:
  - `breakout ver.3` should focus on profit-preserving relief for very-early
    stop-outs.
  - Primary review targets:
    - early `hard_stop_loss` shape
    - `failed_start_exit` aggressiveness
    - how `daily_loss_limit` exits are treated in post-trade review
  - Do not tune breakout from CRV / ZAMA / PLUME recovery-contaminated exits as
    if they were clean alpha exits.
  - `2026-03-15` exploratory exit-relief rerun artifact:
    [breakout_rerun_2026-03-15_exit_relief.json](/home/andi/Schreibtisch/codex/bitcoin2/logs/rotation_strategy_labs/breakout_rerun_2026-03-15_exit_relief.json)
  - That rerun did not accept a `ver.3`: the best-ranked fallback was
    `breakout_6.0` with `net_pnl_delta = -0.034886`, and the dedicated
    `breakout_exit_relief_*` bundles showed `0` improved exits on the current
    sample set.

`continuation`

- Trader follow-up:
  - Re-check the sell-path recovery behavior on continuation lanes before using
    small trailing winners as evidence for a wider or tighter exit profile.
- Lab / catalog follow-up:
  - No immediate continuation entry-widening is justified from this window.
  - If `continuation ver.3` is tested, the focus should be exit persistence:
    small trailing winners, hold bias, and whether continuation gives away too
    much after the first clean profit.
  - Shared staircase and cost gates should remain untouched unless a cleaner
    live sample set says otherwise.

`staircase`

- Trader follow-up:
  - No fresh staircase-tagged live exit was observed in this review window.
  - A direct no-trade audit shows staircase is not mainly dying at the cost
    gate. Across live journals there are many `staircase_override` core
    decisions, but only a small fraction reaches an order intent.
  - On non-paused staircase-override decisions the dominant blockers are risk
    reasons, led by `late_entry_top_zone`, `override_too_close_to_peak`,
    `reentry_move_too_small`, and `daily_loss_limit`.
  - Current selector state also has no active staircase slot, so staircase
    cannot generate fresh live evidence until it is actually assigned again.
- Lab / catalog follow-up:
  - Keep `staircase ver.2` unchanged for now.
  - Treat staircase as a no-trade diagnosis problem before treating it as an
    exit-quality problem.
  - Do not reopen staircase just because it was absent in this specific review;
    wait for fresh clean staircase trades or a broader no-trade counterfactual
    review.

`rebound`

- Trader follow-up:
  - No fresh rebound / swing exit was observed in this review window.
- Lab / catalog follow-up:
  - Keep `rebound ver.2` unchanged for now.
  - Revisit only when fresh swing trades accumulate; today's pain is mainly a
    breakout plus execution-review problem, not a rebound problem.

## Mitigations Applied After Review

- `2026-03-15`
  - Core propagated hard-risk stops no longer emit a second emergency exit when
    the disable path already generated the flatten intent.
  - Exec now retries one sell after an immediate balance refresh when the local
    free base balance is obviously stale relative to the requested exit size.
  - Core now suppresses repeated `core_reentry_state_recovered` noise when the
    same historical flat-exit state is already applied in-memory.
  - Relay local trade reports and live-campaign reconstruction now ignore
    `account_sync_delta` pseudo-fills. This fixes the AXS dashboard phantom
    loss from `2026-03-15 01:25 CET`, where a synthetic sync buy/sell pair had
    been bundled as if it were a real trade and produced a fake `-9.03 USDC`
    net result.
  - These fixes target the concrete live patterns seen on `PLUME`, `CRV`,
    `ZAMA`, and the AXS dashboard report.
  - Remaining open strategy work is still mainly `breakout ver.3`, not another
    broad execution rewrite.
  - The first breakout exit-relief rerun is now documented and ended as a
    no-op, so the next breakout lab should wait for fresher clean stop data or
    a better daily-loss-limit model instead of re-running the same bundle.
- `2026-03-16`
  - Core now suppresses duplicate reduce / flatten intents while an older exit
    order is still inflight. This directly targets the `VIRTUAL 14:57 UTC`
    pattern where a second trailing-stop sell was emitted before the first sell
    had reconciled.
  - Hard emergency exits now also skip enqueuing a second flatten intent when
    an older exit order is already in flight.
  - Exec no longer emits synthetic `account_sync_delta` fills while open orders
    are still present. This prevents pre-reconcile balance snapshots from being
    bundled as fake sells on top of a real exit order.
  - Exec now also ignores dust-only account-sync deltas below the effective
    min-notional floor. This directly targets the `BONK 01:04 UTC` dust tail,
    where a clamped sell left only sub-notional residue.
  - `time_break_even_floor` now requires at least a near-break-even excursion
    before it can act as a floor-style exit while the trade is materially
    underwater. In other words: it no longer doubles as a deep-loss time stop
    before break-even was ever actually threatened.
  - `time_break_even_floor` now also skips fresh continuation bottom-reclaim
    states when local structure is still intact. This directly targets the
    early `RENDER 17:19 CET` sell from `2026-03-16`, where the bot sold at
    `1.851` after a `1.854` buy and price later reached `1.8735`.
  - Mid-trade restart recovery now also resets the risk manager's internal
    day-start equity anchor to the recovered account snapshot. This fixes the
    later `VIRTUAL` quick-exit pattern where the restarted lane first saw one
    flat startup tick, then recovered the real open position, and had been
    misreading that jump as an immediate `daily_loss_limit` breach.
  - Historical audit confirms this was not coin-specific. Across the current
    live journals there were `1112` startup recoveries with an open position on
    `61` symbols; `31` of them were followed by `daily_loss_limit` within
    `5 min`, and `247` were followed by a first sell within `3 min`. The
    strongest repeat offenders in the current sample were `VIRTUAL`, `ETHFI`,
    `ROBO`, `ZAMA`, `RENDER`, `SENT`, and `NEAR`.
  - Launch/watchdog now records the actual restart trigger as a normal journal
    event (`launch_watchdog`) and mirrors the same reason into the guard log.
    The next recurrence should therefore identify whether the first trigger was
    a `heartbeat_stale`, a `critical_child_exit`, or an external signal.
  - The first concrete restart root cause is now confirmed: the recent
    `VIRTUAL` lane restarts were triggered by in-process `RELOAD`, not by the
    normal trading loop. Reload had still been doing a full warmup over several
    thousand bars, which stalled the core heartbeat long enough for the launch
    watchdog to kill the lane with `heartbeat_stale`.
  - Core reload now sends a heartbeat before reload work, skips heavy warmup by
    default during in-process reloads, and batches repeated `RELOAD` commands
    into one effective reload. This was rolled out live and rechecked by
    forcing a fresh `VIRTUAL` reload, which now applies cleanly without a new
    watchdog shutdown.
  - `staircase` now has a live risk-layer opening for real first trades:
    staircase-specific reentries accept shallower reset moves, the generic
    late-entry top-zone block no longer double-blocks staircase overrides, and
    the override peak/pullback/slope guards are relaxed only for
    `alpha_staircase_override`. This was applied live after targeted tests.
  - Staircase starvation also had a selector-side component: recent selector
    rotations had been filling the staircase slot with very late names such as
    `ATOM`, `VIRTUAL`, and earlier `SKY`, typically with `no_valley_context`
    and `24h` range position around `95-100 %`. The selector now skips these
    fake staircase slot-fills unless a strong staircase exception is present.
  - `rebound` now has a live swing-alpha opening for real first trades:
    strong micro rebounds in very tight local ranges may now still qualify even
    when the broader context range is high, as long as context rebound stays
    very strong, spread remains capped, and the local bar is only mildly red.
    This targets the observed `range_too_small -> edge_below_costs` starvation.

## Operating Rule For Future Labs

- Before a new strategy lab rerun, read this file together with
  [rotation_strategy_settings_catalog.yaml](/home/andi/Schreibtisch/codex/bitcoin2/configs/rotation_strategy_settings_catalog.yaml).
- If the live evidence is contaminated by recovery or account-sync artifacts,
  treat it as an execution bug first and not as strategy evidence.
- Only clean fill-based exits should push the alpha thresholds in a new
  `ver.3` candidate.
