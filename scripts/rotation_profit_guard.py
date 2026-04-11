#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
ENV_FILE = REPO_ROOT / "configs" / "rotation_selector_watch_pool.env"
ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
REPORT_FILE = LOG_DIR / "rotation_profit_guard_report.json"
DECISIONS_FILE = LOG_DIR / "rotation_profit_guard_decisions.jsonl"

PROFILE_ORDER = ("scalp", "scalp_breakout", "scalp_uptrend", "scalp_guarded", "scalp_lockdown")
PROFILE_OVERRIDE_KEYS = (
    "ROTATION_NEUTRAL_FRACTION_MULT",
    "ROTATION_DOWN_FRACTION_MULT",
    "ROTATION_CONT_REBOUND_TRIGGER_BPS",
    "ROTATION_CONT_PULLBACK_MAX_BPS",
    "ROTATION_CONT_MAX_STRUCTURE_RANGE_POS",
    "ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS",
    "ROTATION_CONT_STAIRCASE_MAX_CONTEXT_RANGE_POS",
    "ROTATION_ENTRY_EDGE_BPS",
    "ROTATION_ENTRY_COST_BUFFER_BPS",
    "ROTATION_ENTRY_COST_COVERAGE_RATIO",
    "ROTATION_ENTRY_MIN_ATR_TO_COST_RATIO",
    "ROTATION_GATE_COST_COVERAGE_RATIO",
    "ROTATION_REENTRY_MIN_MOVE_BPS",
    "ROTATION_FAILED_START_MAX_BARS",
    "ROTATION_FAILED_START_MIN_REBOUND_BPS",
    "ROTATION_FAILED_START_LOSS_BPS",
    "ROTATION_MIN_EXIT_PROFIT_BPS",
    "ROTATION_TRAILING_ACTIVATION_BPS",
    "ROTATION_TRAILING_STOP_BPS",
    "ROTATION_CAMPAIGN_HOLD_ENABLED",
    "ROTATION_CAMPAIGN_HOLD_MIN_BARS",
    "ROTATION_CAMPAIGN_HOLD_MIN_PROFIT_BPS",
    "ROTATION_CAMPAIGN_HOLD_MIN_TREND_BPS",
    "ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS",
)


@dataclass
class CompletedTrade:
    symbol: str
    buy_ts: str
    sell_ts: str
    qty: float
    buy_avg: float
    sell_px: float
    realized_pnl: float
    exit_reason: str | None


@dataclass
class ProfitWindow:
    trades: list[CompletedTrade]
    last_fill_ts: datetime | None


@dataclass
class MarketSnapshot:
    row_count: int
    eligible_count: int
    breakout_count: int
    macro_up_count: int
    tight_spread_count: int
    avg_spread_bps: float
    top_opportunity_count: int


def _parse_ts(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw))
    except Exception:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _load_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    env: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key.strip()] = value.strip()
    return lines, env


def _write_env(path: Path, lines: list[str], env: dict[str, str]) -> None:
    written: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _value = stripped.split("=", 1)
            key = key.strip()
            if key in env:
                out.append(f"{key}={env[key]}")
                written.add(key)
                continue
        out.append(line)
    for key, value in env.items():
        if key not in written:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _effective_profile(env: dict[str, str]) -> str:
    override = env.get("ROTATION_PROFILE_OVERRIDE", "").strip()
    if override:
        return override
    return env.get("ROTATION_PROFILE", "scalp").strip()


def _find_exit_reason(events: list[dict], idx: int) -> str | None:
    for prev in reversed(events[:idx]):
        if prev.get("event_type") != "core_decision":
            continue
        risk = (prev.get("payload") or {}).get("risk") or {}
        if bool(risk.get("allow")) and float(risk.get("target_btc") or 0.0) == 0.0:
            reason = str(risk.get("reason") or "").strip()
            return reason or None
    return None


def _discover_profit_window(cutoff: datetime) -> ProfitWindow:
    trades: list[CompletedTrade] = []
    last_fill_ts: datetime | None = None
    for path in sorted(LOG_DIR.glob("journal_live_binance_*_usdc_rotation.jsonl")):
        symbol = path.name.split("_")[3].upper()
        events: list[dict] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        pos = 0.0
        cost = 0.0
        buy_fee = 0.0
        entry_ts = ""
        for idx, event in enumerate(events):
            if event.get("event_type") != "fill":
                continue
            event_ts = _parse_ts(str(event.get("ts") or ""))
            if event_ts is not None and (last_fill_ts is None or event_ts > last_fill_ts):
                last_fill_ts = event_ts
            payload = event.get("payload") or {}
            side = str(payload.get("side") or "").lower()
            qty = float(payload.get("qty_btc") or 0.0)
            price = float(payload.get("price") or 0.0)
            fee = float(payload.get("fee_eur") or 0.0)
            ts = str(event.get("ts") or "")
            if side == "buy":
                if pos <= 1e-12:
                    entry_ts = ts
                pos += qty
                cost += qty * price
                buy_fee += fee
                continue
            if side != "sell" or qty <= 0.0 or pos <= 0.0:
                continue

            matched = min(qty, pos)
            avg_cost = cost / pos if pos > 0.0 else 0.0
            alloc_buy_fee = buy_fee * (matched / pos) if pos > 0.0 else 0.0
            realized = (price - avg_cost) * matched - alloc_buy_fee - fee

            remain = pos - matched
            sell_dt = _parse_ts(ts)
            if sell_dt is not None and sell_dt >= cutoff:
                trades.append(
                    CompletedTrade(
                        symbol=symbol,
                        buy_ts=entry_ts,
                        sell_ts=ts,
                        qty=matched,
                        buy_avg=avg_cost,
                        sell_px=price,
                        realized_pnl=realized,
                        exit_reason=_find_exit_reason(events, idx),
                    )
                )
            if remain > 1e-12:
                ratio = remain / pos
                cost *= ratio
                buy_fee *= ratio
            else:
                cost = 0.0
                buy_fee = 0.0
                entry_ts = ""
            pos = remain
    trades.sort(key=lambda item: item.sell_ts)
    return ProfitWindow(trades=trades, last_fill_ts=last_fill_ts)


def _load_market_snapshot(path: Path) -> MarketSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return MarketSnapshot(
            row_count=0,
            eligible_count=0,
            breakout_count=0,
            macro_up_count=0,
            tight_spread_count=0,
            avg_spread_bps=0.0,
            top_opportunity_count=0,
        )

    rows = payload.get("all_rows") or payload.get("rows") or []
    if not isinstance(rows, list):
        rows = []

    eligible_count = 0
    breakout_count = 0
    macro_up_count = 0
    tight_spread_count = 0
    top_opportunity_count = 0
    spreads: list[float] = []

    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        spread_bps = float(
            raw_row.get("fast_spread_bps")
            or raw_row.get("spread_bps")
            or 0.0
        )
        if spread_bps > 0.0:
            spreads.append(spread_bps)
        if 0.0 < spread_bps <= 12.0:
            tight_spread_count += 1

        eligible = bool(raw_row.get("eligible")) or bool(raw_row.get("keep_open"))
        breakout = (
            bool(raw_row.get("fast_impulse"))
            or bool(raw_row.get("fast_staircase"))
            or bool(raw_row.get("strong_continuation_context"))
            or bool(raw_row.get("staircase_trend"))
        )
        macro_up = (
            bool(raw_row.get("macro_up_context"))
            or bool(raw_row.get("mid_trend_up_1h"))
            or bool(raw_row.get("mid_trend_up_6h_balanced"))
            or bool(raw_row.get("broad_uptrend_context"))
        )

        if eligible:
            eligible_count += 1
        if breakout:
            breakout_count += 1
        if macro_up:
            macro_up_count += 1
        if eligible or breakout:
            top_opportunity_count += 1

    avg_spread_bps = sum(spreads) / float(len(spreads)) if spreads else 0.0
    return MarketSnapshot(
        row_count=len(rows),
        eligible_count=eligible_count,
        breakout_count=breakout_count,
        macro_up_count=macro_up_count,
        tight_spread_count=tight_spread_count,
        avg_spread_bps=avg_spread_bps,
        top_opportunity_count=top_opportunity_count,
    )


def _recommend_profile(
    current: str,
    trades: list[CompletedTrade],
    min_trades: int,
    last_fill_age_minutes: float | None,
    market: MarketSnapshot,
) -> tuple[str, str]:
    normalized_current = "scalp_breakout" if current == "scalp" else current
    if normalized_current not in PROFILE_ORDER:
        return current, "unknown_current_profile"

    net_pnl = sum(item.realized_pnl for item in trades)
    loss_count = sum(1 for item in trades if item.realized_pnl <= 0.0)
    loss_rate = (loss_count / float(len(trades))) if trades else 0.0
    reason_counts = Counter(item.exit_reason or "unknown" for item in trades)
    failed_start_share = (
        reason_counts.get("failed_start_exit", 0) / float(len(trades))
        if trades
        else 0.0
    )

    breakout_market = (
        market.top_opportunity_count >= 3
        and market.breakout_count >= 2
        and market.macro_up_count >= 2
        and market.tight_spread_count >= 3
        and (market.avg_spread_bps <= 12.5 or market.avg_spread_bps == 0.0)
    )
    guarded_market = (
        market.top_opportunity_count >= 1
        and (market.avg_spread_bps <= 14.5 or market.avg_spread_bps == 0.0)
    )
    thin_market = (
        market.top_opportunity_count == 0
        or (market.tight_spread_count <= 1 and market.avg_spread_bps >= 14.0)
    )

    if len(trades) < min_trades:
        if len(trades) >= 3 and net_pnl <= -0.12 and failed_start_share >= 0.66:
            return "scalp_guarded", "short_window_failed_starts"
        if normalized_current == "scalp_lockdown" and last_fill_age_minutes is not None and last_fill_age_minutes >= 75.0:
            return "scalp_uptrend", "lockdown_inactive"
        if normalized_current in {"scalp_guarded", "scalp_uptrend", "scalp_breakout"} and breakout_market:
            return "scalp_breakout", "breakout_market_regime"
        if normalized_current == "scalp_breakout" and thin_market:
            return "scalp_uptrend", "market_cooling"
        return normalized_current, "insufficient_trades"

    good_cycle = net_pnl >= 0.10 and loss_rate <= 0.45 and failed_start_share <= 0.35
    weak_cycle = net_pnl < -0.05 or loss_rate > 0.55 or failed_start_share >= 0.45
    very_bad_cycle = net_pnl <= -0.25 or loss_rate >= 0.70 or failed_start_share >= 0.60

    if very_bad_cycle:
        if breakout_market and market.macro_up_count >= 3:
            return "scalp_uptrend", "uptrend_only_drawdown_reset"
        return "scalp_lockdown", "continued_drawdown"

    if normalized_current == "scalp_lockdown":
        if last_fill_age_minutes is not None and last_fill_age_minutes >= 75.0:
            return "scalp_uptrend", "lockdown_inactive"
        if good_cycle and guarded_market:
            return "scalp_uptrend", "stabilized"
        return "scalp_lockdown", "hold_lockdown"

    if normalized_current == "scalp_breakout":
        if weak_cycle:
            return "scalp_uptrend", "breakout_drawdown"
        if thin_market:
            return "scalp_uptrend", "market_cooling"
        return "scalp_breakout", "hold_breakout"

    if normalized_current == "scalp_uptrend":
        if weak_cycle and thin_market:
            return "scalp_lockdown", "uptrend_market_thin"
        if breakout_market and good_cycle:
            return "scalp_breakout", "uptrend_promote_breakout"
        return "scalp_uptrend", "hold_uptrend"

    if breakout_market and not weak_cycle:
        return "scalp_breakout", "breakout_market_regime"
    if guarded_market:
        return "scalp_uptrend", "hold_uptrend"
    return "scalp_guarded", "hold_guarded"


def _build_selector_cmd(env: dict[str, str]) -> list[str]:
    return [
        "python3",
        "scripts/rotation_auto_coin_selector.py",
        "--profile",
        _effective_profile(env) or "default",
        "--top",
        env.get("ACTIVE_TOP", "4"),
        "--watch-top",
        env.get("WATCH_TOP", "0"),
        "--min-active-minutes",
        env.get("MIN_ACTIVE_MINUTES", "5"),
        "--active-retain-min-score",
        env.get("ACTIVE_RETAIN_MIN_SCORE", "40"),
        "--switch-margin-score",
        env.get("SWITCH_MARGIN_SCORE", "8"),
        "--max-retain-position-pct",
        env.get("MAX_RETAIN_POSITION_PCT", "85"),
        "--apply",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze recent live rotation trades and adapt profile.")
    parser.add_argument("--env-file", default=str(ENV_FILE))
    parser.add_argument("--lookback-hours", type=float, default=4.0)
    parser.add_argument("--min-trades", type=int, default=6)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    env_path = Path(args.env_file).resolve()
    lines, env = _load_env(env_path)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max(0.5, float(args.lookback_hours)))
    window = _discover_profit_window(cutoff)
    trades = window.trades
    market = _load_market_snapshot(ACTIVE_FILE)
    current_profile = _effective_profile(env)
    last_fill_age_minutes = None
    if window.last_fill_ts is not None:
        last_fill_age_minutes = max(
            0.0,
            (now - window.last_fill_ts).total_seconds() / 60.0,
        )
    target_profile, recommendation_reason = _recommend_profile(
        current_profile,
        trades,
        max(1, int(args.min_trades)),
        last_fill_age_minutes,
        market,
    )

    net_pnl = sum(item.realized_pnl for item in trades)
    win_count = sum(1 for item in trades if item.realized_pnl > 0.0)
    loss_count = len(trades) - win_count
    reason_counts = Counter(item.exit_reason or "unknown" for item in trades)
    report = {
        "generated_at": now.isoformat(),
        "lookback_hours": float(args.lookback_hours),
        "cutoff": cutoff.isoformat(),
        "trade_count": len(trades),
        "net_pnl": round(net_pnl, 6),
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": round((win_count / len(trades)) if trades else 0.0, 6),
        "loss_rate": round((loss_count / len(trades)) if trades else 0.0, 6),
        "reason_counts": dict(reason_counts),
        "last_fill_ts": window.last_fill_ts.isoformat() if window.last_fill_ts is not None else None,
        "last_fill_age_minutes": round(last_fill_age_minutes, 3) if last_fill_age_minutes is not None else None,
        "market_snapshot": {
            "row_count": market.row_count,
            "eligible_count": market.eligible_count,
            "breakout_count": market.breakout_count,
            "macro_up_count": market.macro_up_count,
            "tight_spread_count": market.tight_spread_count,
            "avg_spread_bps": round(market.avg_spread_bps, 4),
            "top_opportunity_count": market.top_opportunity_count,
        },
        "current_profile": current_profile,
        "target_profile": target_profile,
        "recommendation_reason": recommendation_reason,
        "recent_trades": [
            {
                "symbol": item.symbol,
                "buy_ts": item.buy_ts,
                "sell_ts": item.sell_ts,
                "realized_pnl": round(item.realized_pnl, 6),
                "buy_avg": round(item.buy_avg, 8),
                "sell_px": round(item.sell_px, 8),
                "exit_reason": item.exit_reason,
            }
            for item in trades[-12:]
        ],
        "applied": False,
        "env_file": str(env_path),
    }

    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    if args.apply and target_profile != current_profile:
        env["ROTATION_PROFILE"] = target_profile
        if "ROTATION_PROFILE_OVERRIDE" in env:
            env["ROTATION_PROFILE_OVERRIDE"] = target_profile
        for key in PROFILE_OVERRIDE_KEYS:
            env.pop(key, None)
        _write_env(env_path, lines, env)
        selector_env = dict(os.environ)
        selector_env.update(env)
        subprocess.check_call(
            _build_selector_cmd(env),
            cwd=REPO_ROOT,
            env=selector_env,
        )
        report["applied"] = True
        REPORT_FILE.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        decision = {
            "ts": now.isoformat(),
            "from_profile": current_profile,
            "to_profile": target_profile,
            "reason": recommendation_reason,
            "trade_count": len(trades),
            "net_pnl": round(net_pnl, 6),
        }
        DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with DECISIONS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision, ensure_ascii=True) + "\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(
            json.dumps(
                {
                    "trade_count": report["trade_count"],
                    "net_pnl": report["net_pnl"],
                    "current_profile": current_profile,
                    "target_profile": target_profile,
                    "applied": report["applied"],
                    "recommendation_reason": recommendation_reason,
                },
                ensure_ascii=True,
            )
        )


if __name__ == "__main__":
    main()
