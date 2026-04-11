from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from trading.meta.rotation_shadow import ShadowCandidate, TradeSummary
from trading.meta.strategy_views import STRATEGY_NAMES

OPENAI_PARAMETER_OVERRIDE_RULES: dict[str, dict[str, float | int | str]] = {
    "ROTATION_ENTRY_EDGE_BPS": {"kind": "float", "min": 0.3, "max": 16.0},
    "ROTATION_ENTRY_COST_COVERAGE_RATIO": {"kind": "float", "min": 0.30, "max": 1.40},
    "ROTATION_GATE_COST_COVERAGE_RATIO": {"kind": "float", "min": 0.30, "max": 1.40},
    "ROTATION_ENTRY_MIN_ATR_TO_COST_RATIO": {"kind": "float", "min": 0.50, "max": 2.00},
    "ROTATION_LATE_ENTRY_BLOCK_CONTEXT_RANGE_POS": {"kind": "float", "min": 0.70, "max": 0.99},
    "ROTATION_LATE_ENTRY_BLOCK_STRUCTURE_RANGE_POS": {"kind": "float", "min": 0.75, "max": 0.995},
    "ROTATION_LATE_ENTRY_BLOCK_MAX_CONTEXT_DRAWDOWN_BPS": {"kind": "float", "min": 6.0, "max": 120.0},
    "ROTATION_LATE_ENTRY_BLOCK_MIN_TREND_RETURN_BPS": {"kind": "float", "min": 30.0, "max": 420.0},
    "ROTATION_LATE_ENTRY_BLOCK_MIN_RETURN_BPS": {"kind": "float", "min": 2.0, "max": 140.0},
    "ROTATION_REENTRY_COOLDOWN_BARS_AFTER_WEAK_EXIT": {"kind": "int", "min": 0, "max": 16},
    "ROTATION_FAILED_START_MIN_BARS": {"kind": "int", "min": 0, "max": 8},
    "ROTATION_FAILED_START_MAX_BARS": {"kind": "int", "min": 1, "max": 10},
    "ROTATION_FAILED_START_MIN_REBOUND_BPS": {"kind": "float", "min": 0.0, "max": 90.0},
    "ROTATION_FAILED_START_LOSS_BPS": {"kind": "float", "min": 10.0, "max": 140.0},
    "ROTATION_TRAILING_ACTIVATION_BPS": {"kind": "float", "min": 8.0, "max": 80.0},
    "ROTATION_TRAILING_STOP_BPS": {"kind": "float", "min": 4.0, "max": 32.0},
    "ROTATION_MIN_EXIT_PROFIT_BPS": {"kind": "float", "min": 2.0, "max": 36.0},
    "ROTATION_TIME_BREAK_EVEN_FLOOR_BARS": {"kind": "int", "min": 0, "max": 32},
    "ROTATION_CAMPAIGN_HOLD_ENABLED": {"kind": "int", "min": 0, "max": 1},
    "ROTATION_CAMPAIGN_HOLD_MIN_BARS": {"kind": "int", "min": 0, "max": 16},
    "ROTATION_CAMPAIGN_HOLD_MIN_PROFIT_BPS": {"kind": "float", "min": 0.0, "max": 30.0},
    "ROTATION_CAMPAIGN_HOLD_MIN_TREND_BPS": {"kind": "float", "min": 30.0, "max": 240.0},
    "ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS": {"kind": "float", "min": 8.0, "max": 100.0},
}


@dataclass
class OpenAIMetaResult:
    ok: bool
    mode: str
    model: str | None
    recommendation: dict[str, Any]
    input_payload: dict[str, Any]
    response_payload: dict[str, Any] | None
    warning: str | None = None


def availability_error() -> str | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "OPENAI_API_KEY is not set"
    if api_key == "DEIN_NEUER_OPENAI_KEY":
        return "OPENAI_API_KEY still contains placeholder value"
    return None


def _sdk_available() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def _parameter_override_schema() -> dict[str, float | int]:
    schema: dict[str, float | int] = {}
    for key, rule in OPENAI_PARAMETER_OVERRIDE_RULES.items():
        schema[key] = 0 if str(rule.get("kind")) == "int" else 0.0
    return schema


def _sanitize_parameter_overrides(payload: object) -> dict[str, float | int]:
    if not isinstance(payload, dict):
        return {}
    sanitized: dict[str, float | int] = {}
    for raw_key, raw_value in payload.items():
        key = str(raw_key or "").strip().upper()
        rule = OPENAI_PARAMETER_OVERRIDE_RULES.get(key)
        if rule is None:
            continue
        kind = str(rule.get("kind") or "float")
        try:
            if kind == "int":
                value: float | int = int(round(float(raw_value)))
            else:
                value = float(raw_value)
        except Exception:
            continue
        value = max(float(rule["min"]), min(float(rule["max"]), float(value)))
        if kind == "int":
            sanitized[key] = int(round(value))
        else:
            sanitized[key] = round(float(value), 4)
    return sanitized


def build_prompt_payload(
    *,
    current_profile: str,
    active_state: dict[str, Any],
    trade_summary: TradeSummary,
    candidates: list[ShadowCandidate],
    model_info: dict[str, Any],
    universe_report: dict[str, Any] | None = None,
    watch_pool_strategy_summary: dict[str, Any] | None = None,
    recent_trade_examples: list[dict[str, Any]] | None = None,
    recent_no_trade_examples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    strategy_rankings = active_state.get("strategy_rankings") if isinstance(active_state.get("strategy_rankings"), dict) else {}
    selected_strategy_map = active_state.get("selected_strategy_map") if isinstance(active_state.get("selected_strategy_map"), dict) else {}
    selected = [str(item).upper() for item in active_state.get("selected") or []]
    watch_symbols = [str(item).upper() for item in active_state.get("watch_symbols") or []]
    top_candidates = []
    for item in candidates[:8]:
        top_candidates.append(
            {
                "symbol": item.symbol,
                "p_profit": round(float(item.p_profit), 4),
                "strategy_primary": item.strategy_primary,
                "selected": bool(item.selected),
                "gate_reason": item.gate_reason,
                "open_notional": round(float(item.open_notional), 6),
                "age_sec": round(float(item.age_sec), 2),
                "edge_bps_effective": round(float(item.decision_context.get("edge_bps_effective") or 0.0), 3),
                "expected_cost_bps": round(float(item.decision_context.get("expected_cost_bps") or 0.0), 3),
                "phase": str(item.decision_context.get("phase") or ""),
            }
        )

    compact_rankings: dict[str, list[dict[str, Any]]] = {}
    for strategy, rows in strategy_rankings.items():
        if not isinstance(rows, list):
            continue
        compact_rankings[str(strategy)] = [
            {
                "symbol": str(row.get("symbol") or "").upper(),
                "score": round(float(row.get("score") or 0.0), 4),
                "gate_reason": str(row.get("gate_reason") or ""),
                "eligible": bool(row.get("eligible")),
            }
            for row in rows[:4]
            if isinstance(row, dict)
        ]
    universe_strategy_rankings: dict[str, list[dict[str, Any]]] = {}
    universe_strategy_summary = {}
    compact_strategy_breakdown: dict[str, dict[str, Any]] = {}
    compact_symbol_breakdown: dict[str, dict[str, Any]] = {}
    compact_regime_breakdown: dict[str, dict[str, Any]] = {}
    compact_session_breakdown: dict[str, dict[str, Any]] = {}
    compact_exit_path_summary = dict(getattr(trade_summary, "exit_path_summary", {}) or {})
    compact_entry_path_summary = dict(getattr(trade_summary, "entry_path_summary", {}) or {})
    compact_no_trade_summary = dict(getattr(trade_summary, "no_trade_summary", {}) or {})
    for strategy, payload in (trade_summary.strategy_breakdown or {}).items():
        if not isinstance(payload, dict):
            continue
        exit_path_summary = payload.get("exit_path_summary") if isinstance(payload.get("exit_path_summary"), dict) else {}
        entry_path_summary = payload.get("entry_path_summary") if isinstance(payload.get("entry_path_summary"), dict) else {}
        compact_strategy_breakdown[str(strategy)] = {
            "trade_count": int(payload.get("trade_count") or 0),
            "net_pnl": round(float(payload.get("net_pnl") or 0.0), 8),
            "win_rate": round(float(payload.get("win_rate") or 0.0), 6),
            "avg_hold_sec": round(float(payload.get("avg_hold_sec") or 0.0), 2),
            "exit_reasons": dict(payload.get("exit_reasons") or {}),
            "top_symbols": list(payload.get("top_symbols") or []),
            "exit_path_summary": {
                "sample_count": int(exit_path_summary.get("sample_count") or 0),
                "early_exit_rate": round(float(exit_path_summary.get("early_exit_rate") or 0.0), 6),
                "protective_exit_rate": round(float(exit_path_summary.get("protective_exit_rate") or 0.0), 6),
                "failed_start_recovery_rate": round(
                    float(exit_path_summary.get("failed_start_recovery_rate") or 0.0),
                    6,
                ),
                "shakeout_then_run_rate": round(float(exit_path_summary.get("shakeout_then_run_rate") or 0.0), 6),
                "micro_pop_loss_run_rate": round(float(exit_path_summary.get("micro_pop_loss_run_rate") or 0.0), 6),
            },
            "entry_path_summary": {
                "sample_count": int(entry_path_summary.get("sample_count") or 0),
                "poor_entry_rate": round(float(entry_path_summary.get("poor_entry_rate") or 0.0), 6),
                "top_zone_entry_rate": round(float(entry_path_summary.get("top_zone_entry_rate") or 0.0), 6),
                "late_entry_rate": round(float(entry_path_summary.get("late_entry_rate") or 0.0), 6),
                "avg_entry_quality_score_bps": round(
                    float(entry_path_summary.get("avg_entry_quality_score_bps") or 0.0),
                    6,
                ),
            },
        }
    for symbol, payload in sorted(
        (trade_summary.symbol_breakdown or {}).items(),
        key=lambda item: (
            -int((item[1] or {}).get("trade_count") or 0),
            float((item[1] or {}).get("net_pnl") or 0.0),
            item[0],
        ),
    )[:12]:
        if not isinstance(payload, dict):
            continue
        exit_path_summary = payload.get("exit_path_summary") if isinstance(payload.get("exit_path_summary"), dict) else {}
        entry_path_summary = payload.get("entry_path_summary") if isinstance(payload.get("entry_path_summary"), dict) else {}
        compact_symbol_breakdown[str(symbol).upper()] = {
            "trade_count": int(payload.get("trade_count") or 0),
            "net_pnl": round(float(payload.get("net_pnl") or 0.0), 8),
            "win_rate": round(float(payload.get("win_rate") or 0.0), 6),
            "last_exit_ts": payload.get("last_exit_ts"),
            "exit_reasons": dict(payload.get("exit_reasons") or {}),
            "strategies": dict(payload.get("strategies") or {}),
            "exit_path_summary": {
                "sample_count": int(exit_path_summary.get("sample_count") or 0),
                "early_exit_rate": round(float(exit_path_summary.get("early_exit_rate") or 0.0), 6),
                "protective_exit_rate": round(float(exit_path_summary.get("protective_exit_rate") or 0.0), 6),
                "failed_start_recovery_rate": round(
                    float(exit_path_summary.get("failed_start_recovery_rate") or 0.0),
                    6,
                ),
                "shakeout_then_run_rate": round(float(exit_path_summary.get("shakeout_then_run_rate") or 0.0), 6),
                "micro_pop_loss_run_rate": round(float(exit_path_summary.get("micro_pop_loss_run_rate") or 0.0), 6),
            },
            "entry_path_summary": {
                "sample_count": int(entry_path_summary.get("sample_count") or 0),
                "poor_entry_rate": round(float(entry_path_summary.get("poor_entry_rate") or 0.0), 6),
                "top_zone_entry_rate": round(float(entry_path_summary.get("top_zone_entry_rate") or 0.0), 6),
                "late_entry_rate": round(float(entry_path_summary.get("late_entry_rate") or 0.0), 6),
                "avg_entry_quality_score_bps": round(
                    float(entry_path_summary.get("avg_entry_quality_score_bps") or 0.0),
                    6,
                ),
            },
        }
    for regime, payload in sorted((getattr(trade_summary, "regime_breakdown", {}) or {}).items()):
        if not isinstance(payload, dict):
            continue
        if int(payload.get("trade_count") or 0) <= 0 and int(payload.get("no_trade_count") or 0) <= 0:
            continue
        no_trade_summary = payload.get("no_trade_summary") if isinstance(payload.get("no_trade_summary"), dict) else {}
        compact_regime_breakdown[str(regime).lower()] = {
            "trade_count": int(payload.get("trade_count") or 0),
            "net_pnl": round(float(payload.get("net_pnl") or 0.0), 8),
            "win_rate": round(float(payload.get("win_rate") or 0.0), 6),
            "no_trade_count": int(payload.get("no_trade_count") or 0),
            "no_trade_summary": {
                "sample_count": int(no_trade_summary.get("sample_count") or 0),
                "missed_entry_rate": round(float(no_trade_summary.get("missed_entry_rate") or 0.0), 6),
                "correct_block_rate": round(float(no_trade_summary.get("correct_block_rate") or 0.0), 6),
                "avg_post_decision_close_30m_bps": round(
                    float(no_trade_summary.get("avg_post_decision_close_30m_bps") or 0.0),
                    6,
                ),
                "avg_post_decision_close_60m_bps": round(
                    float(no_trade_summary.get("avg_post_decision_close_60m_bps") or 0.0),
                    6,
                ),
            },
        }
    for session, payload in sorted((getattr(trade_summary, "session_breakdown", {}) or {}).items()):
        if not isinstance(payload, dict):
            continue
        if int(payload.get("trade_count") or 0) <= 0 and int(payload.get("no_trade_count") or 0) <= 0:
            continue
        no_trade_summary = payload.get("no_trade_summary") if isinstance(payload.get("no_trade_summary"), dict) else {}
        compact_session_breakdown[str(session).lower()] = {
            "trade_count": int(payload.get("trade_count") or 0),
            "net_pnl": round(float(payload.get("net_pnl") or 0.0), 8),
            "win_rate": round(float(payload.get("win_rate") or 0.0), 6),
            "no_trade_count": int(payload.get("no_trade_count") or 0),
            "no_trade_summary": {
                "sample_count": int(no_trade_summary.get("sample_count") or 0),
                "missed_entry_rate": round(float(no_trade_summary.get("missed_entry_rate") or 0.0), 6),
                "correct_block_rate": round(float(no_trade_summary.get("correct_block_rate") or 0.0), 6),
                "avg_post_decision_close_30m_bps": round(
                    float(no_trade_summary.get("avg_post_decision_close_30m_bps") or 0.0),
                    6,
                ),
            },
        }
    if isinstance(universe_report, dict):
        raw_rankings = universe_report.get("strategy_rankings")
        if isinstance(raw_rankings, dict):
            for strategy, rows in raw_rankings.items():
                if not isinstance(rows, list):
                    continue
                universe_strategy_rankings[str(strategy)] = [
                    {
                        "symbol": str(row.get("symbol") or "").upper(),
                        "rank_score": round(float(row.get("rank_score") or 0.0), 4),
                        "avg_strategy_score": round(float(row.get("avg_strategy_score") or 0.0), 4),
                        "strategy_candidate_ratio": round(float(row.get("strategy_candidate_ratio") or 0.0), 4),
                        "bucket": str(row.get("bucket") or ""),
                        "gate_reason": str(row.get("latest_gate_reason") or row.get("dominant_gate_reason") or ""),
                    }
                    for row in rows[:5]
                    if isinstance(row, dict)
                ]
        if isinstance(universe_report.get("strategy_summary"), dict):
            universe_strategy_summary = dict(universe_report.get("strategy_summary") or {})

    watch_pool_market_health = {
        "strategy_count": 0,
        "strategies_with_buy_ready": 0,
        "strategies_with_ml_positive": 0,
        "total_buy_ready_candidates": 0,
        "avg_top_p_profit_across_strategies": 0.0,
        "dominant_gate_reasons": {},
    }
    gate_totals: dict[str, int] = {}
    if isinstance(watch_pool_strategy_summary, dict):
        avg_top_p_values: list[float] = []
        watch_pool_market_health["strategy_count"] = len(watch_pool_strategy_summary)
        for payload in watch_pool_strategy_summary.values():
            if not isinstance(payload, dict):
                continue
            buy_ready_count = int(payload.get("buy_ready_count") or 0)
            ml_positive_count = int(payload.get("ml_positive_count") or 0)
            avg_top_p = float(payload.get("avg_top_p_profit") or 0.0)
            if buy_ready_count > 0:
                watch_pool_market_health["strategies_with_buy_ready"] += 1
            if ml_positive_count > 0 or avg_top_p >= 0.56:
                watch_pool_market_health["strategies_with_ml_positive"] += 1
            watch_pool_market_health["total_buy_ready_candidates"] += buy_ready_count
            if avg_top_p > 0.0:
                avg_top_p_values.append(avg_top_p)
            gate_counts = payload.get("dominant_gate_reasons") if isinstance(payload.get("dominant_gate_reasons"), dict) else {}
            for reason, count in gate_counts.items():
                label = str(reason or "").strip()
                if not label:
                    continue
                gate_totals[label] = gate_totals.get(label, 0) + int(count or 0)
        if avg_top_p_values:
            watch_pool_market_health["avg_top_p_profit_across_strategies"] = round(
                sum(avg_top_p_values) / float(len(avg_top_p_values)),
                6,
            )
    if gate_totals:
        watch_pool_market_health["dominant_gate_reasons"] = dict(
            sorted(gate_totals.items(), key=lambda item: (-item[1], item[0]))[:5]
        )

    strategy_weight_schema = {strategy: 0.0 for strategy in STRATEGY_NAMES}
    strategy_action_schema = {
        strategy: {"mode": "pause|watch|secondary|primary", "slot_target": 0, "top_symbols": ["SYMBOL"]}
        for strategy in STRATEGY_NAMES
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shadow_only": True,
        "current_profile": current_profile,
        "selected": selected,
        "watch_symbols": watch_symbols,
        "selected_strategy_map": {str(k).upper(): str(v) for k, v in selected_strategy_map.items()},
        "trade_summary": {
            "trade_count": trade_summary.trade_count,
            "net_pnl": round(float(trade_summary.net_pnl), 8),
            "win_rate": round(float(trade_summary.win_rate), 6),
            "avg_win": round(float(trade_summary.avg_win), 8),
            "avg_loss": round(float(trade_summary.avg_loss), 8),
            "last_exit_ts": trade_summary.last_exit_ts,
            "exit_reasons": trade_summary.exit_reasons,
            "exit_path_summary": {
                "sample_count": int(compact_exit_path_summary.get("sample_count") or 0),
                "early_exit_rate": round(float(compact_exit_path_summary.get("early_exit_rate") or 0.0), 6),
                "protective_exit_rate": round(float(compact_exit_path_summary.get("protective_exit_rate") or 0.0), 6),
                "failed_start_recovery_rate": round(
                    float(compact_exit_path_summary.get("failed_start_recovery_rate") or 0.0),
                    6,
                ),
                "trailing_missed_run_rate": round(
                    float(compact_exit_path_summary.get("trailing_missed_run_rate") or 0.0),
                    6,
                ),
                "shakeout_then_run_rate": round(
                    float(compact_exit_path_summary.get("shakeout_then_run_rate") or 0.0),
                    6,
                ),
                "micro_pop_loss_run_rate": round(
                    float(compact_exit_path_summary.get("micro_pop_loss_run_rate") or 0.0),
                    6,
                ),
            },
            "entry_path_summary": {
                "sample_count": int(compact_entry_path_summary.get("sample_count") or 0),
                "poor_entry_rate": round(float(compact_entry_path_summary.get("poor_entry_rate") or 0.0), 6),
                "top_zone_entry_rate": round(float(compact_entry_path_summary.get("top_zone_entry_rate") or 0.0), 6),
                "late_entry_rate": round(float(compact_entry_path_summary.get("late_entry_rate") or 0.0), 6),
                "avg_entry_quality_score_bps": round(
                    float(compact_entry_path_summary.get("avg_entry_quality_score_bps") or 0.0),
                    6,
                ),
            },
            "no_trade_summary": {
                "sample_count": int(compact_no_trade_summary.get("sample_count") or 0),
                "tunable_sample_count": int(compact_no_trade_summary.get("tunable_sample_count") or 0),
                "missed_entry_rate": round(float(compact_no_trade_summary.get("missed_entry_rate") or 0.0), 6),
                "correct_block_rate": round(float(compact_no_trade_summary.get("correct_block_rate") or 0.0), 6),
                "tunable_missed_entry_rate": round(
                    float(compact_no_trade_summary.get("tunable_missed_entry_rate") or 0.0),
                    6,
                ),
                "tunable_correct_block_rate": round(
                    float(compact_no_trade_summary.get("tunable_correct_block_rate") or 0.0),
                    6,
                ),
                "avg_post_decision_close_30m_bps": round(
                    float(compact_no_trade_summary.get("avg_post_decision_close_30m_bps") or 0.0),
                    6,
                ),
                "avg_post_decision_close_60m_bps": round(
                    float(compact_no_trade_summary.get("avg_post_decision_close_60m_bps") or 0.0),
                    6,
                ),
            },
            "strategy_breakdown": compact_strategy_breakdown,
            "symbol_breakdown": compact_symbol_breakdown,
            "regime_breakdown": compact_regime_breakdown,
            "session_breakdown": compact_session_breakdown,
        },
        "ml_model": model_info,
        "top_candidates": top_candidates,
        "strategy_rankings": compact_rankings,
        "watch_pool_strategy_summary": watch_pool_strategy_summary or {},
        "watch_pool_market_health": watch_pool_market_health,
        "recent_trade_examples": list(recent_trade_examples or [])[:6],
        "recent_no_trade_examples": list(recent_no_trade_examples or [])[:4],
        "universe_scan": {
            "generated_at": (universe_report or {}).get("generated_at"),
            "snapshots_used": (universe_report or {}).get("snapshots_used"),
            "symbol_count": (universe_report or {}).get("symbol_count"),
            "recommended_pool_size": (universe_report or {}).get("recommended_pool_size"),
            "strategy_summary": universe_strategy_summary,
            "strategy_rankings": universe_strategy_rankings,
        },
        "response_schema": {
            "profile": "scalp_breakout|scalp_uptrend|scalp_guarded|scalp_guarded_open|scalp_lockdown|hold",
            "risk_mode": "normal|cautious|stop_new_entries",
            "strategy_weights": strategy_weight_schema,
            "strategy_actions": strategy_action_schema,
            "candidate_overrides": ["SYMBOL"],
            "avoid_symbols": ["SYMBOL"],
            "parameter_overrides": _parameter_override_schema(),
            "confidence": 0.0,
            "notes": "short diagnosis and rationale",
        },
    }


def fallback_recommendation(
    *,
    current_profile: str,
    trade_summary: TradeSummary,
    candidates: list[ShadowCandidate],
    watch_pool_strategy_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    top = candidates[:4]
    avg_p = sum(item.p_profit for item in top) / float(len(top)) if top else 0.0
    positive_candidates = sum(1 for item in top if item.p_profit >= 0.55)
    strategy_summary = watch_pool_strategy_summary or {}
    strategy_trade_summary = trade_summary.strategy_breakdown or {}
    symbol_trade_summary = trade_summary.symbol_breakdown or {}
    exit_path_summary = getattr(trade_summary, "exit_path_summary", {}) or {}
    no_trade_summary = getattr(trade_summary, "no_trade_summary", {}) or {}
    early_exit_rate = float(exit_path_summary.get("early_exit_rate") or 0.0)
    protective_exit_rate = float(exit_path_summary.get("protective_exit_rate") or 0.0)
    failed_start_recovery_rate = float(exit_path_summary.get("failed_start_recovery_rate") or 0.0)
    trailing_missed_run_rate = float(exit_path_summary.get("trailing_missed_run_rate") or 0.0)
    shakeout_then_run_rate = float(exit_path_summary.get("shakeout_then_run_rate") or 0.0)
    micro_pop_loss_run_rate = float(exit_path_summary.get("micro_pop_loss_run_rate") or 0.0)
    missed_entry_rate = float(
        no_trade_summary.get("tunable_missed_entry_rate", no_trade_summary.get("missed_entry_rate") or 0.0) or 0.0
    )
    correct_block_rate = float(
        no_trade_summary.get("tunable_correct_block_rate", no_trade_summary.get("correct_block_rate") or 0.0) or 0.0
    )
    avg_post_decision_close_30m_bps = float(no_trade_summary.get("avg_post_decision_close_30m_bps") or 0.0)
    avg_post_decision_close_60m_bps = float(no_trade_summary.get("avg_post_decision_close_60m_bps") or 0.0)
    early_exit_pressure = max(
        0.0,
        early_exit_rate
        + (0.80 * failed_start_recovery_rate)
        + (0.55 * trailing_missed_run_rate)
        + (0.70 * shakeout_then_run_rate)
        + (0.95 * micro_pop_loss_run_rate)
        - (0.70 * protective_exit_rate),
    )
    missed_entry_pressure = max(
        0.0,
        missed_entry_rate
        + (0.30 * max(0.0, avg_post_decision_close_30m_bps) / 18.0)
        + (0.45 * max(0.0, avg_post_decision_close_60m_bps) / 24.0)
        - (0.70 * correct_block_rate),
    )

    profile = current_profile or "scalp_guarded"
    risk_mode = "normal"
    notes = []

    if trade_summary.trade_count >= 4 and trade_summary.net_pnl < -0.10:
        if max(early_exit_pressure, missed_entry_pressure) >= 0.50 and avg_p >= 0.52:
            profile = "scalp_breakout" if missed_entry_pressure > early_exit_pressure and avg_p >= 0.56 else "scalp_uptrend"
            risk_mode = "cautious"
            notes.append("negative_window_but_exit_or_gate_too_early")
        else:
            profile = "scalp_lockdown"
            risk_mode = "cautious"
            notes.append("recent_trade_window_negative")
    elif avg_p >= 0.58 and positive_candidates >= 2:
        profile = "scalp_breakout"
        notes.append("multiple_candidates_above_ml_threshold")
    elif avg_p < 0.48:
        profile = "scalp_guarded"
        risk_mode = "cautious"
        notes.append("ml_candidates_weak")

    if trade_summary.trade_count >= 6 and trade_summary.win_rate < 0.34 and trade_summary.net_pnl < -0.18:
        if max(early_exit_pressure, missed_entry_pressure) >= 0.55 and early_exit_pressure > (protective_exit_rate + 0.10):
            risk_mode = "cautious"
            profile = "scalp_guarded"
            notes.append("loss_cluster_but_exit_too_early")
        else:
            risk_mode = "stop_new_entries"
            profile = "scalp_lockdown"
            notes.append("loss_cluster_detected")
    if missed_entry_pressure >= 0.45 and avg_p >= 0.54:
        profile = "scalp_breakout" if avg_p >= 0.57 else "scalp_uptrend"
        if risk_mode == "stop_new_entries":
            risk_mode = "cautious"
        notes.append("gates_too_strict_recent_missed_entries")

    strategy_weights = {strategy: round(1.0 / float(len(STRATEGY_NAMES)), 4) for strategy in STRATEGY_NAMES}
    if profile == "scalp_breakout":
        strategy_weights = {
            "staircase": 0.20,
            "pullback_continuation": 0.16,
            "breakout_retest": 0.28,
            "continuation": 0.10,
            "breakout": 0.18,
            "relative_strength": 0.08,
            "rebound": 0.0,
        }
    elif profile == "scalp_uptrend":
        strategy_weights = {
            "staircase": 0.26,
            "pullback_continuation": 0.24,
            "breakout_retest": 0.18,
            "continuation": 0.10,
            "breakout": 0.08,
            "relative_strength": 0.14,
            "rebound": 0.0,
        }
    elif profile == "scalp_lockdown":
        strategy_weights = {
            "staircase": 0.16,
            "pullback_continuation": 0.26,
            "breakout_retest": 0.10,
            "continuation": 0.26,
            "breakout": 0.05,
            "relative_strength": 0.12,
            "rebound": 0.05,
        }
    elif profile == "scalp_guarded":
        strategy_weights = {
            "staircase": 0.22,
            "pullback_continuation": 0.24,
            "breakout_retest": 0.18,
            "continuation": 0.18,
            "breakout": 0.08,
            "relative_strength": 0.10,
            "rebound": 0.0,
        }

    for strategy, stats in strategy_trade_summary.items():
        if strategy not in strategy_weights or not isinstance(stats, dict):
            continue
        trade_count = int(stats.get("trade_count") or 0)
        net_pnl = float(stats.get("net_pnl") or 0.0)
        win_rate = float(stats.get("win_rate") or 0.0)
        failed_start_rate = 0.0
        strategy_exit_path = stats.get("exit_path_summary") if isinstance(stats.get("exit_path_summary"), dict) else {}
        strategy_early_exit_rate = float(strategy_exit_path.get("early_exit_rate") or 0.0)
        exit_reasons = stats.get("exit_reasons") if isinstance(stats.get("exit_reasons"), dict) else {}
        if trade_count > 0 and isinstance(exit_reasons, dict):
            failed_start_rate = float(exit_reasons.get("failed_start_exit") or 0.0) / float(trade_count)
        if trade_count >= 4 and (net_pnl < -0.05 or failed_start_rate >= 0.5) and strategy_early_exit_rate < 0.30:
            strategy_weights[strategy] *= 0.55
        elif trade_count >= 4 and net_pnl > 0.03 and win_rate >= 0.5:
            strategy_weights[strategy] *= 1.2

    total_weight = sum(max(0.0, value) for value in strategy_weights.values())
    if total_weight > 0.0:
        strategy_weights = {
            strategy: round(max(0.0, value) / total_weight, 4)
            for strategy, value in strategy_weights.items()
        }

    avoid_symbols: list[str] = []
    for symbol, stats in sorted(symbol_trade_summary.items()):
        if not isinstance(stats, dict):
            continue
        trade_count = int(stats.get("trade_count") or 0)
        net_pnl = float(stats.get("net_pnl") or 0.0)
        exit_reasons = stats.get("exit_reasons") if isinstance(stats.get("exit_reasons"), dict) else {}
        symbol_exit_path = stats.get("exit_path_summary") if isinstance(stats.get("exit_path_summary"), dict) else {}
        symbol_early_exit_rate = float(symbol_exit_path.get("early_exit_rate") or 0.0)
        failed_start_rate = (
            float(exit_reasons.get("failed_start_exit") or 0.0) / float(trade_count)
            if trade_count > 0 else 0.0
        )
        if trade_count >= 4 and (net_pnl <= -0.08 or (net_pnl < 0.0 and failed_start_rate >= 0.5)) and symbol_early_exit_rate < 0.35:
            avoid_symbols.append(str(symbol).upper())
    avoid_symbols = avoid_symbols[:8]

    strategy_actions: dict[str, Any] = {}
    if strategy_summary:
        for strategy in STRATEGY_NAMES:
            payload = strategy_summary.get(strategy) if isinstance(strategy_summary, dict) else {}
            top_rows = payload.get("top_candidates") if isinstance(payload, dict) else []
            buy_ready = int(payload.get("buy_ready_count") or 0) if isinstance(payload, dict) else 0
            avg_top_p = float(payload.get("avg_top_p_profit") or 0.0) if isinstance(payload, dict) else 0.0
            trade_stats = strategy_trade_summary.get(strategy) if isinstance(strategy_trade_summary.get(strategy), dict) else {}
            trade_count = int(trade_stats.get("trade_count") or 0)
            failed_start_rate = 0.0
            trade_exit_path = trade_stats.get("exit_path_summary") if isinstance(trade_stats.get("exit_path_summary"), dict) else {}
            trade_early_exit_rate = float(trade_exit_path.get("early_exit_rate") or 0.0)
            if trade_count > 0:
                exit_reasons = trade_stats.get("exit_reasons") if isinstance(trade_stats.get("exit_reasons"), dict) else {}
                failed_start_rate = float(exit_reasons.get("failed_start_exit") or 0.0) / float(trade_count)
            mode = "watch"
            slot_target = 0
            if risk_mode == "stop_new_entries":
                mode = "pause"
            elif (
                trade_count >= 4
                and failed_start_rate >= 0.5
                and float(trade_stats.get("net_pnl") or 0.0) < 0.0
                and trade_early_exit_rate < 0.35
            ):
                mode = "pause"
            elif buy_ready > 0 and avg_top_p >= 0.58:
                mode = "primary"
                slot_target = 1
            elif avg_top_p >= 0.53:
                mode = "secondary"
                slot_target = 1 if buy_ready > 0 else 0
            elif avg_top_p < 0.43 and not top_rows:
                mode = "pause"
            strategy_actions[str(strategy)] = {
                "mode": mode,
                "slot_target": slot_target,
                "top_symbols": [
                    str(item.get("symbol") or "").upper()
                    for item in top_rows[:3]
                    if isinstance(item, dict)
                ],
            }

    return {
        "profile": profile,
        "risk_mode": risk_mode,
        "strategy_weights": strategy_weights,
        "strategy_actions": strategy_actions,
        "candidate_overrides": [item.symbol for item in top if item.p_profit >= 0.57][:4],
        "avoid_symbols": avoid_symbols,
        "parameter_overrides": {},
        "confidence": round(max(0.15, min(0.85, avg_p)), 4),
        "notes": "; ".join(notes) if notes else "fallback_heuristic_only",
    }


def _extract_text(response: Any) -> str:
    if isinstance(response, dict):
        text = response.get("output_text")
        if isinstance(text, str) and text.strip():
            return text
        output = response.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    value = part.get("text")
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
                if parts:
                    return "\n".join(parts)
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    output = getattr(response, "output", None)
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for part in content:
                value = getattr(part, "text", None)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
        if parts:
            return "\n".join(parts)
    raise RuntimeError("No text output in OpenAI response")


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.startswith("```")]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("OpenAI returned no JSON object")
    candidate = stripped[start : end + 1]
    normalized = (
        candidate
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
        .replace("‘", "'")
    )
    attempts = [
        normalized,
        re.sub(r",(\s*[}\]])", r"\1", normalized),
    ]
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            payload = json.loads(attempt)
        except Exception as exc:
            last_error = exc
            continue
        if not isinstance(payload, dict):
            last_error = RuntimeError("OpenAI JSON output is not an object")
            continue
        return payload
    excerpt = normalized[:500].replace("\n", "\\n")
    raise RuntimeError(f"{last_error}; raw_excerpt={excerpt}")


def _build_compact_prompt_payload(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "generated_at": prompt_payload.get("generated_at"),
        "shadow_only": bool(prompt_payload.get("shadow_only")),
        "current_profile": prompt_payload.get("current_profile"),
        "selected": list(prompt_payload.get("selected") or []),
        "watch_symbols": list(prompt_payload.get("watch_symbols") or []),
        "selected_strategy_map": dict(prompt_payload.get("selected_strategy_map") or {}),
        "trade_summary": dict(prompt_payload.get("trade_summary") or {}),
        "watch_pool_strategy_summary": dict(prompt_payload.get("watch_pool_strategy_summary") or {}),
        "watch_pool_market_health": dict(prompt_payload.get("watch_pool_market_health") or {}),
        "recent_trade_examples": list(prompt_payload.get("recent_trade_examples") or []),
        "recent_no_trade_examples": list(prompt_payload.get("recent_no_trade_examples") or []),
        "response_schema": dict(prompt_payload.get("response_schema") or {}),
    }
    trade_summary = compact["trade_summary"]
    if isinstance(trade_summary, dict):
        trade_summary.pop("symbol_breakdown", None)
    return compact


def _http_post_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"OpenAI transport error: {exc.reason}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI HTTP response is not a JSON object")
    return parsed


def call_openai_meta(
    *,
    prompt_payload: dict[str, Any],
    fallback: dict[str, Any],
) -> OpenAIMetaResult:
    error = availability_error()
    model_name = str(os.getenv("ROTATION_OPENAI_MODEL", "gpt-5-mini")).strip() or "gpt-5-mini"
    if error is not None:
        return OpenAIMetaResult(
            ok=True,
            mode="fallback",
            model=None,
            recommendation=fallback,
            input_payload=prompt_payload,
            response_payload=None,
            warning=error,
        )

    timeout_seconds = max(5.0, float(os.getenv("ROTATION_OPENAI_TIMEOUT_SEC", "40")))
    max_output_tokens = max(512, int(os.getenv("ROTATION_OPENAI_MAX_OUTPUT_TOKENS", "1400")))
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    system_prompt = (
        "You are a cautious meta-controller for a live long-only crypto scalping system. "
        "You never place orders. You only recommend regime/profile/risk changes and strategy weighting. "
        "This is an uptrend-first system: prefer staircase, pullback_continuation, breakout_retest, continuation, and relative_strength over direct breakout or rebound unless they are clearly strongest. "
        "Use the 28-coin watch-pool strategy summary and watch_pool_market_health as the primary decision surface, and use the wider universe scan only as context. "
        "Explicitly separate opportunity weakness, execution weakness, gate strictness, and late-entry top-chasing. "
        "Use trade_summary.exit_path_summary and recent_trade_examples to decide whether losses came from exits that were too early, micro-pop loss exits, failed-start recovery exits, or protective exits that were justified. "
        "Use trade_summary.entry_path_summary and recent_trade_examples.entry_snapshot to decide whether entries were late, top-zone chases, or low-quality breakouts after the move was already mostly spent. "
        "Use trade_summary.no_trade_summary and recent_no_trade_examples to decide whether the system blocked valid entries that later ran, or correctly blocked weak setups. "
        "If watch-pool opportunity is still healthy but recent losses mostly look like early exits, do not solve that by collapsing into hold or stop_new_entries. "
        "If watch-pool opportunity is healthy but recent losses mostly look like late-entry top-chasing, do not solve that mainly with avoid_symbols; tighten entry selection and late-entry guards first. "
        "If watch-pool opportunity is healthy and recent_no_trade_examples show blocked symbols later running, treat that primarily as gate strictness rather than market weakness. "
        "When losses are mainly likely_exit_too_early, failed_start_recovery, shakeout_then_run, or micro_pop_loss_run cases, do not use avoid_symbols as the primary fix; prefer exit-management and hold/trailing adjustments. "
        "When losses are mainly likely_late_entry_top_chase or entry_and_exit_weak, prefer parameter_overrides that tighten late-entry blocks and entry quality before banning symbols. "
        "Use trade_summary.strategy_breakdown to identify which strategies are currently producing real losses or failed starts, but do not overreact to tiny positive or negative PnL with only one or two trades. "
        "Use trade_summary.symbol_breakdown and recent_trade_examples to avoid repeating fresh losing symbols or failed-start clusters. "
        "Only choose stop_new_entries when opportunity quality is broadly weak or recent losses are broad and not mainly caused by premature exits. "
        "Do not default to hold unless most strategy buckets are weak, gated, or recent performance is clearly too poor. "
        "Use parameter_overrides sparingly and only from response_schema.parameter_overrides. "
        "Favor fewer bad trades over more activity. Return strict minified JSON only with no markdown and no commentary."
    )
    user_prompt = {
        "task": (
            "Review the shadow trading state and return only a JSON object matching response_schema. "
            "Decide profile, risk mode, strategy weights, and per-strategy actions for staircase, pullback_continuation, breakout_retest, continuation, breakout, relative_strength, and rebound. "
            "Use this order of importance: "
            "1) watch_pool_market_health and watch_pool_strategy_summary to judge whether the market still offers enough buy-ready setups, "
            "2) trade_summary.exit_path_summary and recent_trade_examples to judge whether realized losses came from premature exits or justified protective exits, "
            "3) trade_summary.entry_path_summary and recent_trade_examples.entry_snapshot to judge whether entries were late/top-zone chases or structurally weak, "
            "4) trade_summary.no_trade_summary, trade_summary.regime_breakdown, trade_summary.session_breakdown, and recent_no_trade_examples to judge whether entries were blocked too aggressively in specific regimes or sessions, "
            "5) trade_summary.strategy_breakdown and trade_summary.symbol_breakdown to find weak strategies and fresh losing symbols, "
            "5) current selected/watch state, "
            "6) universe_scan only as tie-break context. "
            "When recent_trade_examples show positive post-exit follow-through after flat or losing exits, treat that primarily as exit-management weakness rather than market weakness. "
            "When recent_trade_examples show high context_range_pos, high trend_return_bps, little pullback room, and weak entry_quality_score_bps, treat that primarily as late-entry top-chasing rather than bad market opportunity. "
            "When recent_no_trade_examples show blocked symbols later running with positive post-decision follow-through, treat that primarily as gate strictness rather than market weakness. "
            "When recent_trade_examples indicate likely_exit_too_early, failed_start_recovery, shakeout_then_run, or micro_pop_loss_run, prefer exit-management changes over avoid_symbols for those symbols. "
            "When recent_trade_examples indicate likely_late_entry_top_chase or entry_and_exit_weak, prefer parameter_overrides such as ROTATION_LATE_ENTRY_BLOCK_* or tighter entry filters before avoid_symbols. "
            "If watch-pool opportunity is healthy, avoid recommending hold or stop_new_entries unless losses are broad and not mainly early-exit driven. "
            "Use notes to state a compact diagnosis such as exit_too_early_watch_healthy, late_entries_top_chasing_watch_healthy, gates_too_strict_watch_healthy, protective_exits_market_weak, or broad_strategy_failure. "
            "Keep candidate_overrides, avoid_symbols, and parameter_overrides short. Do not invent fields. "
            "Return compact one-line JSON."
        ),
        "state": prompt_payload,
    }
    compact_user_prompt = {
        "task": user_prompt["task"],
        "state": _build_compact_prompt_payload(prompt_payload),
    }

    def _request_llm(compact: bool) -> tuple[Any, str]:
        payload_user_prompt = compact_user_prompt if compact else user_prompt
        if _sdk_available():
            from openai import OpenAI

            client = OpenAI(api_key=api_key, timeout=timeout_seconds)
            response_local: Any = client.responses.create(
                model=model_name,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload_user_prompt, ensure_ascii=True)},
                ],
                reasoning={"effort": "low"},
                text={"format": {"type": "json_object"}},
                max_output_tokens=max_output_tokens,
            )
            return response_local, "llm_sdk"
        response_local = _http_post_json(
            url="https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            payload={
                "model": model_name,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload_user_prompt, ensure_ascii=True)},
                ],
                "reasoning": {"effort": "low"},
                "text": {"format": {"type": "json_object"}},
                "max_output_tokens": max_output_tokens,
            },
            timeout_seconds=timeout_seconds,
        )
        return response_local, "llm_http"

    try:
        response, llm_mode = _request_llm(compact=False)
        try:
            parsed = _parse_json_object(_extract_text(response))
        except Exception:
            response, llm_mode = _request_llm(compact=True)
            parsed = _parse_json_object(_extract_text(response))
        recommendation = {
            "profile": str(parsed.get("profile") or fallback.get("profile") or "hold"),
            "risk_mode": str(parsed.get("risk_mode") or fallback.get("risk_mode") or "cautious"),
            "strategy_weights": dict(parsed.get("strategy_weights") or fallback.get("strategy_weights") or {}),
            "strategy_actions": dict(parsed.get("strategy_actions") or fallback.get("strategy_actions") or {}),
            "candidate_overrides": [
                str(item).upper()
                for item in (parsed.get("candidate_overrides") or [])
                if str(item).strip()
            ][:8],
            "avoid_symbols": [
                str(item).upper()
                for item in (parsed.get("avoid_symbols") or fallback.get("avoid_symbols") or [])
                if str(item).strip()
            ][:8],
            "parameter_overrides": _sanitize_parameter_overrides(
                parsed.get("parameter_overrides") or fallback.get("parameter_overrides") or {}
            ),
            "confidence": float(parsed.get("confidence") or fallback.get("confidence") or 0.0),
            "notes": str(parsed.get("notes") or fallback.get("notes") or ""),
        }
        return OpenAIMetaResult(
            ok=True,
            mode=llm_mode,
            model=model_name,
            recommendation=recommendation,
            input_payload=prompt_payload,
            response_payload=parsed,
            warning=None if llm_mode == "llm_sdk" else "openai_sdk_missing_used_stdlib_http",
        )
    except Exception as exc:
        return OpenAIMetaResult(
            ok=True,
            mode="fallback",
            model=model_name,
            recommendation=fallback,
            input_payload=prompt_payload,
            response_payload=None,
            warning=str(exc),
        )
