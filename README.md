# Binance USDC Rotation Trading Framework

This repository contains a modular Python trading system for running a rotating spot strategy on Binance USDC pairs. The project combines per-symbol live lanes, a central selector, runtime orchestration, a relay/dashboard for live monitoring, and a large regression test suite.

The repository started as a broader crypto trading framework and still contains research, replay, optimization, and legacy tooling. Its current operational focus is the Binance rotation engine.

## What The System Does

- Maintains a curated universe of tradable Binance USDC symbols
- Runs one isolated live lane per symbol with separate market data, core, execution, and journaling processes
- Promotes symbols from the watch universe into a smaller set of active trader slots
- Applies structure, spread, volume, corridor, and entry-quality rules before new buys
- Keeps inventory and exit-only symbols manageable even when they are outside the current active universe
- Exposes live state, candidates, reasons for blocked buys, and trade history through a relay server and web dashboard
- Includes simulation, replay, strategy-lab, shadow-analysis, and optimization tooling for research and tuning

## Core Architecture

- `trading/`
  Main engine, risk logic, launchers, runtime helpers, exchange adapters, and shared strategy code.
- `scripts/`
  Selector, lane orchestration, watch-pool refresh, guard scripts, status tools, research helpers, and operations scripts.
- `configs/`
  Live lane configs, selector environment files, runtime lane snapshots, and strategy overlays.
- `app/relay-server/`
  Web relay and dashboard for live state, candidate inspection, and operational visibility.
- `tests/`
  Regression coverage for selector behavior, risk logic, relay output, stale handling, restart recovery, execution, and strategy runtime behavior.

## Live Trading Model

The live system is organized in layers:

1. **Universe**
   A curated list of Binance USDC symbols that the system is willing to observe or trade.
2. **Watch Lanes**
   Per-symbol services that keep local state warm and continuously evaluate tradability.
3. **Active Trader Slots**
   A smaller subset of symbols that are currently allowed to compete for new entries.
4. **Relay / Dashboard**
   A central live view that shows positions, candidates, blocked reasons, runtime metadata, and recent trade activity.

The selector and runtime metadata are written into runtime files under `configs/` and consumed by the lane orchestration and relay components.

## Important Entry Points

- `trading/launch.py`
  Starts the multi-process engine for a single lane.
- `trading/rotation_universe.py`
  Defines the curated universe, lane pool, and special exit-only symbols.
- `scripts/rotation_auto_coin_selector.py`
  Builds and refreshes the rotation selection logic.
- `scripts/rotation_apply_active_lanes.py`
  Applies the selector result to the running lane set.
- `scripts/rotation_refresh_watch_pool.py`
  Refreshes the runtime watch pool.
- `app/relay-server/relay_server.py`
  Serves the live dashboard / relay API.

## Operational Characteristics

- Multi-process design with watchdogs and heartbeats
- Runtime reload support for live lanes
- Account sync and restart recovery paths
- Journal-based observability and trade mirror support
- Plain-language non-buy reasons in the relay/dashboard
- Designed for long-only spot rotation workflows on Binance USDC pairs

## Repository Notes

- The README describes the **current practical focus** of the repository.
- Older Kraken-oriented, optimizer, and historical research tooling still exists and remains useful for experimentation and offline work.
- Some files under `configs/` and `logs/` are runtime-generated or rewritten by live services.

## Safety Note

This repository contains live-trading code. Review configuration, exchange permissions, and operational safeguards carefully before using it with real funds.
