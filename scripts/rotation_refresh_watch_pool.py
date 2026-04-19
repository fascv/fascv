#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.rotation_scope import rotation_rows, rotation_selected_symbols, rotation_watch_symbols
from trading.rotation_universe import POOL
from trading.meta.strategy_views import STRATEGY_NAMES
ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
UNIVERSE_REPORT_FILE = REPO_ROOT / "logs" / "shadow_usdc_scalp_report.json"
META_REPORT_FILE = REPO_ROOT / "logs" / "rotation_meta_shadow_report.json"
OUTPUT_ENV_FILE = REPO_ROOT / "configs" / "rotation_watch_pool_runtime.env"
OUTPUT_REPORT_FILE = REPO_ROOT / "logs" / "rotation_watch_pool_refresh_report.json"
MODE_BONUS = {
    "primary": 520.0,
    "secondary": 380.0,
    "watch": 220.0,
    "pause": -200.0,
}
BUCKET_BONUS = {
    "core_keep": 240.0,
    "keep": 180.0,
    "review": 70.0,
    "hard_reject": -260.0,
}
GATE_PENALTY = {
    "still_dumping": -420.0,
    "macro_downtrend": -220.0,
    "no_macro_support_1h": -120.0,
    "structure_downtrend": -180.0,
    "spread": -90.0,
    "depth": -70.0,
    "post_dump_recovery_pending": -55.0,
}
RECENT_SYMBOL_PENALTY = -520.0
META_AVOID_PENALTY = -1200.0
RECENT_LIVE_SELECTION_BLOCK_PENALTY = -900.0


def _shell_assign(name: str, value: object) -> str:
    return f"{name}={shlex.quote(str(value))}"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _symbol_list(raw: Any) -> list[str]:
    return rotation_watch_symbols({"watch_symbols": raw}, include_selected=False)


def _row_map(active_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rotation_rows(active_state):
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol:
            result[symbol] = row
    return result


def _weight_lookup(meta_report: dict[str, Any]) -> dict[str, float]:
    recommendation = meta_report.get("recommendation")
    if not isinstance(recommendation, dict):
        return {strategy: 0.25 for strategy in STRATEGY_NAMES}
    raw = recommendation.get("strategy_weights")
    if not isinstance(raw, dict):
        return {strategy: 0.25 for strategy in STRATEGY_NAMES}
    total = sum(max(0.0, float(raw.get(strategy) or 0.0)) for strategy in STRATEGY_NAMES)
    if total <= 0.0:
        return {strategy: 0.25 for strategy in STRATEGY_NAMES}
    return {
        strategy: max(0.0, float(raw.get(strategy) or 0.0)) / total
        for strategy in STRATEGY_NAMES
    }


def _add_score(
    scoreboard: dict[str, dict[str, Any]],
    symbol: str,
    score: float,
    reason: str,
) -> None:
    if not symbol:
        return
    state = scoreboard[symbol]
    state["symbol"] = symbol
    state["score"] += float(score)
    state["reasons"].append({"reason": reason, "score": round(float(score), 3)})


def build_watch_pool(
    *,
    active_state: dict[str, Any],
    universe_report: dict[str, Any],
    meta_report: dict[str, Any],
    target_size: int,
    allowed_symbols: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    target_size = max(4, int(target_size))
    allowed_values = [
        str(symbol).strip().upper()
        for symbol in (allowed_symbols or [])
        if str(symbol).strip()
    ]
    allowed_set = set(allowed_values) if allowed_values else None
    scoreboard: dict[str, dict[str, Any]] = defaultdict(lambda: {"symbol": "", "score": 0.0, "reasons": []})
    active_rows = _row_map(active_state)
    current_selected = rotation_selected_symbols(active_state)
    current_watch = rotation_watch_symbols(active_state, include_selected=False)
    recommendation = meta_report.get("recommendation") if isinstance(meta_report.get("recommendation"), dict) else {}
    strategy_actions = recommendation.get("strategy_actions") if isinstance(recommendation.get("strategy_actions"), dict) else {}
    strategy_weights = _weight_lookup(meta_report)
    avoid_symbols = set(_symbol_list(recommendation.get("avoid_symbols", [])))
    symbol_breakdown = {}
    trade_summary = meta_report.get("trade_summary")
    if isinstance(trade_summary, dict) and isinstance(trade_summary.get("symbol_breakdown"), dict):
        symbol_breakdown = dict(trade_summary.get("symbol_breakdown") or {})

    for idx, symbol in enumerate(current_selected):
        if allowed_set is not None and symbol not in allowed_set:
            continue
        row = active_rows.get(symbol, {})
        if bool(row.get("recent_live_selection_block")) and not bool(row.get("keep_open")):
            _add_score(scoreboard, symbol, 220.0 - (idx * 12.0), "current_selected_recent_live_block")
        else:
            _add_score(scoreboard, symbol, 10000.0 - (idx * 12.0), "current_selected")
    for idx, symbol in enumerate(current_watch):
        if allowed_set is not None and symbol not in allowed_set:
            continue
        _add_score(scoreboard, symbol, 140.0 - min(idx, 40), "current_watch")
    for symbol, row in active_rows.items():
        if allowed_set is not None and symbol not in allowed_set:
            continue
        gate = str(row.get("gate_reason", "") or "").strip().lower()
        if bool(row.get("recent_live_selection_block")) and symbol in current_selected and not bool(row.get("keep_open")):
            _add_score(scoreboard, symbol, -260.0, "recent_live_selection_block")
        if gate in GATE_PENALTY and symbol not in current_selected:
            _add_score(scoreboard, symbol, GATE_PENALTY[gate], f"gate:{gate}")
        if bool(row.get("keep_open")):
            _add_score(scoreboard, symbol, 800.0, "keep_open")
        if bool(row.get("eligible")):
            _add_score(scoreboard, symbol, 300.0, "eligible")

    candidate_overrides = _symbol_list(recommendation.get("candidate_overrides", []))
    for idx, symbol in enumerate(candidate_overrides):
        if allowed_set is not None and symbol not in allowed_set:
            continue
        _add_score(scoreboard, symbol, 1600.0 - (idx * 40.0), "meta_candidate_override")
    for symbol in avoid_symbols:
        if allowed_set is not None and symbol not in allowed_set:
            continue
        if symbol not in current_selected:
            _add_score(scoreboard, symbol, META_AVOID_PENALTY, "meta_avoid_symbol")

    for symbol, stats in symbol_breakdown.items():
        symbol_name = str(symbol).strip().upper()
        if not symbol_name or not isinstance(stats, dict):
            continue
        if allowed_set is not None and symbol_name not in allowed_set:
            continue
        trade_count = int(stats.get("trade_count") or 0)
        net_pnl = float(stats.get("net_pnl") or 0.0)
        exit_reasons = stats.get("exit_reasons") if isinstance(stats.get("exit_reasons"), dict) else {}
        failed_start_rate = (
            float(exit_reasons.get("failed_start_exit") or 0.0) / float(trade_count)
            if trade_count > 0 else 0.0
        )
        if symbol_name in current_selected:
            continue
        if trade_count >= 2 and (net_pnl <= -0.08 or (net_pnl < 0.0 and failed_start_rate >= 0.5)):
            _add_score(scoreboard, symbol_name, RECENT_SYMBOL_PENALTY, "recent_symbol_losses")

    for strategy in STRATEGY_NAMES:
        action = strategy_actions.get(strategy) if isinstance(strategy_actions.get(strategy), dict) else {}
        mode = str(action.get("mode") or "watch").strip().lower()
        slot_target = max(0, int(action.get("slot_target") or 0))
        weight = strategy_weights.get(strategy, 0.25)
        base_bonus = MODE_BONUS.get(mode, 120.0) + (slot_target * 85.0) + (weight * 180.0)
        for idx, symbol in enumerate(_symbol_list(action.get("top_symbols", []))):
            if allowed_set is not None and symbol not in allowed_set:
                continue
            _add_score(
                scoreboard,
                symbol,
                base_bonus - (idx * 28.0),
                f"meta_action:{strategy}:{mode}",
            )

    watch_summary = meta_report.get("watch_pool_strategy_summary")
    if isinstance(watch_summary, dict):
        for strategy in STRATEGY_NAMES:
            info = watch_summary.get(strategy)
            if not isinstance(info, dict):
                continue
            weight = strategy_weights.get(strategy, 0.25)
            for idx, item in enumerate(info.get("top_candidates", [])):
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                if allowed_set is not None and symbol not in allowed_set:
                    continue
                strategy_score = float(item.get("strategy_score") or 0.0)
                bonus = 260.0 + (weight * 120.0) + min(220.0, strategy_score * 0.6) - (idx * 18.0)
                _add_score(scoreboard, symbol, bonus, f"watch_summary:{strategy}")

    for idx, item in enumerate(universe_report.get("top_candidates", [])):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        if allowed_set is not None and symbol not in allowed_set:
            continue
        bucket = str(item.get("bucket", "")).strip().lower()
        bonus = 420.0 - (idx * 36.0) + BUCKET_BONUS.get(bucket, 0.0)
        _add_score(scoreboard, symbol, bonus, f"universe_top:{bucket or 'none'}")

    recommended_pool = _symbol_list(universe_report.get("recommended_pool", []))
    for idx, symbol in enumerate(recommended_pool[: max(target_size * 2, 40)]):
        if allowed_set is not None and symbol not in allowed_set:
            continue
        _add_score(scoreboard, symbol, 260.0 - (idx * 5.0), "recommended_pool")

    strategy_rankings = universe_report.get("strategy_rankings")
    if isinstance(strategy_rankings, dict):
        for strategy in STRATEGY_NAMES:
            ranking = strategy_rankings.get(strategy)
            if not isinstance(ranking, list):
                continue
            action = strategy_actions.get(strategy) if isinstance(strategy_actions.get(strategy), dict) else {}
            mode = str(action.get("mode") or "watch").strip().lower()
            if mode == "pause":
                continue
            weight = strategy_weights.get(strategy, 0.25)
            mode_mult = 1.25 if mode == "primary" else (1.0 if mode == "secondary" else 0.75)
            for idx, item in enumerate(ranking[:16]):
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                if allowed_set is not None and symbol not in allowed_set:
                    continue
                bucket = str(item.get("bucket", "")).strip().lower()
                base = (320.0 - (idx * 14.0)) * mode_mult
                base += BUCKET_BONUS.get(bucket, 0.0)
                base += weight * 140.0
                _add_score(scoreboard, symbol, base, f"strategy_rank:{strategy}:{bucket or 'none'}")

    ranked = sorted(
        scoreboard.values(),
        key=lambda item: (
            -float(item["score"]),
            item["symbol"] not in current_selected,
            item["symbol"] not in current_watch,
            item["symbol"],
        ),
    )

    watch_symbols: list[str] = []
    seen: set[str] = set()
    for symbol in current_selected:
        if symbol not in seen:
            watch_symbols.append(symbol)
            seen.add(symbol)
    for item in ranked:
        symbol = str(item["symbol"]).upper()
        if symbol in seen:
            continue
        watch_symbols.append(symbol)
        seen.add(symbol)
        if len(watch_symbols) >= target_size:
            break

    if len(watch_symbols) < target_size:
        fallback_symbols = current_watch + [
            str(item.get("symbol", "")).upper()
            for item in ranked
            if str(item.get("symbol", "")).upper() not in current_watch
        ]
        for symbol in fallback_symbols:
            if allowed_set is not None and symbol not in allowed_set:
                continue
            if symbol in seen:
                continue
            watch_symbols.append(symbol)
            seen.add(symbol)
            if len(watch_symbols) >= target_size:
                break

    if len(watch_symbols) < target_size and allowed_values:
        for symbol in allowed_values:
            if symbol in seen:
                continue
            watch_symbols.append(symbol)
            seen.add(symbol)
            if len(watch_symbols) >= target_size:
                break

    return watch_symbols[:target_size], {item["symbol"]: item for item in ranked}


def main() -> None:
    ap = argparse.ArgumentParser(description="Refresh the runtime watch pool from universe scan + meta recommendations.")
    ap.add_argument("--active-file", default=str(ACTIVE_FILE))
    ap.add_argument("--universe-report-file", default=str(UNIVERSE_REPORT_FILE))
    ap.add_argument("--meta-report-file", default=str(META_REPORT_FILE))
    ap.add_argument("--output-env-file", default=str(OUTPUT_ENV_FILE))
    ap.add_argument("--output-report-file", default=str(OUTPUT_REPORT_FILE))
    ap.add_argument("--target-size", type=int, default=int(os.getenv("WATCH_TOP", "28") or "28"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    active_state = _load_json(Path(args.active_file))
    universe_report = _load_json(Path(args.universe_report_file))
    meta_report = _load_json(Path(args.meta_report_file))
    generated_at = datetime.now(timezone.utc).isoformat()

    watch_symbols, scored = build_watch_pool(
        active_state=active_state,
        universe_report=universe_report,
        meta_report=meta_report,
        target_size=args.target_size,
        allowed_symbols=POOL,
    )

    env_lines = [
        "# Auto-generated by rotation_refresh_watch_pool.py",
        _shell_assign("ROTATION_WATCH_POOL_GENERATED_AT", generated_at),
        _shell_assign("ROTATION_FIXED_WATCH_SYMBOLS", ",".join(watch_symbols)),
        _shell_assign("ROTATION_SELECTOR_SYMBOLS", ",".join(watch_symbols)),
    ]
    output_env_file = Path(args.output_env_file)
    output_env_file.parent.mkdir(parents=True, exist_ok=True)
    output_env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    current_watch = _symbol_list(active_state.get("watch_symbols", []))
    report = {
        "generated_at": generated_at,
        "target_size": int(args.target_size),
        "watch_symbols": watch_symbols,
        "added_symbols": [symbol for symbol in watch_symbols if symbol not in current_watch],
        "removed_symbols": [symbol for symbol in current_watch if symbol not in watch_symbols],
        "top_ranked": list(scored.values())[:40],
        "source_files": {
            "active": str(Path(args.active_file)),
            "universe_report": str(Path(args.universe_report_file)),
            "meta_report": str(Path(args.meta_report_file)),
            "output_env": str(output_env_file),
        },
    }
    output_report_file = Path(args.output_report_file)
    output_report_file.parent.mkdir(parents=True, exist_ok=True)
    output_report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("watch_pool:", ", ".join(watch_symbols))
        print("added:", ", ".join(report["added_symbols"]) or "-")
        print("removed:", ", ".join(report["removed_symbols"]) or "-")


if __name__ == "__main__":
    main()
