#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.meta.openai_meta import (  # noqa: E402
    build_prompt_payload,
    call_openai_meta,
    fallback_recommendation,
)
from trading.meta.rotation_autotune import (  # noqa: E402
    SUPPORTED_AUTOTUNE_PROFILES,
    recommend_rotation_autotune,
)
from trading.meta.rotation_shadow import (  # noqa: E402
    LogisticTradeModel,
    build_recent_no_trade_examples,
    build_recent_trade_examples,
    build_shadow_candidates,
    build_watch_pool_strategy_summary,
    build_trade_summary,
    export_samples_jsonl,
    extract_counterfactual_samples,
    extract_trade_samples,
    load_model,
    load_rotation_state,
    merge_env_file,
    save_model,
    train_trade_model,
)
from trading.meta.strategy_views import STRATEGY_NAMES  # noqa: E402
from scripts.rotation_auto_coin_selector import build_selector_payload, _run_selector  # noqa: E402


LOG_DIR = REPO_ROOT / "logs"
ACTIVE_FILE = REPO_ROOT / "configs" / "rotation_active_lanes.json"
ENV_FILE = REPO_ROOT / "configs" / "rotation_meta_shadow.env"
DEFAULT_OPENAI_SECRET_ENV_FILE = Path.home() / ".config" / "codex" / "rotation-openai.env"
DEFAULT_TRADING_SECRETS_ENV_FILE = Path.home() / ".config" / "codex" / "trading-secrets.env"
SELECTOR_RUNTIME_ENV_FILE = REPO_ROOT / "configs" / "rotation_meta_runtime.env"
DATASET_FILE = LOG_DIR / "rotation_trade_ml_dataset.jsonl"
MODEL_FILE = LOG_DIR / "rotation_trade_ml_model.json"
ML_REPORT_FILE = LOG_DIR / "rotation_ml_shadow_report.json"
OPENAI_REPORT_FILE = LOG_DIR / "rotation_openai_shadow_report.json"
META_REPORT_FILE = LOG_DIR / "rotation_meta_shadow_report.json"
AUTOTUNE_REPORT_FILE = LOG_DIR / "rotation_profile_autotune_report.json"
UNIVERSE_REPORT_FILE = LOG_DIR / "shadow_usdc_scalp_report.json"
SELECTOR_APPLY_REPORT_FILE = LOG_DIR / "rotation_meta_selector_apply.json"
WATCH_POOL_REFRESH_SCRIPT = REPO_ROOT / "scripts" / "rotation_refresh_watch_pool.py"
WATCH_POOL_REFRESH_REPORT_FILE = LOG_DIR / "rotation_watch_pool_refresh_report.json"
CORE_META_STRATEGIES: tuple[str, ...] = ("staircase", "continuation", "breakout", "rebound")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _shell_assign(name: str, value: object) -> str:
    return f"{name}={shlex.quote(str(value))}"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(float(os.getenv(name, str(default)))))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw not in {"0", "false", "no", "off", "disabled"}


def _env_path(name: str, default: Path) -> Path:
    raw = str(os.getenv(name, "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return default


def _merge_optional_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) == 1 and "=" not in lines[0]:
        os.environ.setdefault("OPENAI_API_KEY", lines[0])
        return {"OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "")}
    return merge_env_file(path)


def _load_local_secret_envs() -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in (
        _env_path("ROTATION_OPENAI_SECRET_ENV_FILE", DEFAULT_OPENAI_SECRET_ENV_FILE),
        _env_path("CODEX_TRADING_SECRETS_ENV", DEFAULT_TRADING_SECRETS_ENV_FILE),
    ):
        merged.update(_merge_optional_env_file(path))
    return merged


def _preload_env_file(argv: list[str]) -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file", default=str(ENV_FILE))
    parsed, _ = parser.parse_known_args(argv)
    env_file = Path(parsed.env_file)
    merge_env_file(env_file)
    return env_file


def _model_info(model: LogisticTradeModel | None, source: str) -> dict:
    if model is None:
        return {
            "available": False,
            "source": source,
        }
    return {
        "available": True,
        "source": source,
        "model_version": model.model_version,
        "sample_count": model.sample_count,
        "positive_rate": model.positive_rate,
        "trained_at": model.trained_at,
        "train_metrics": model.train_metrics,
        "test_metrics": model.test_metrics,
        "training_diagnostics": model.training_diagnostics,
    }


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_iso8601(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _reuse_recent_openai_report(
    previous_report: dict | None,
    *,
    now: datetime,
    requested_model: str,
    min_interval_minutes: float,
) -> dict | None:
    if not isinstance(previous_report, dict):
        return None
    source_mode = str(previous_report.get("source_mode") or previous_report.get("mode") or "").strip().lower()
    if source_mode not in {"llm_sdk", "llm_http"}:
        return None
    previous_model = str(previous_report.get("model") or "").strip()
    if previous_model != requested_model:
        return None
    source_generated_at = _parse_iso8601(
        previous_report.get("source_generated_at") or previous_report.get("generated_at")
    )
    if source_generated_at is None:
        return None
    min_interval = max(0.0, float(min_interval_minutes))
    if min_interval <= 0.0:
        return None
    age = now - source_generated_at.astimezone(timezone.utc)
    if age < timedelta(0):
        return None
    if age < timedelta(minutes=min_interval):
        recommendation = previous_report.get("recommendation")
        if not isinstance(recommendation, dict):
            return None
        return {
            "mode": "reused_recent",
            "model": previous_model,
            "warning": f"reused_openai_result_within_{min_interval:g}min_window",
            "recommendation": recommendation,
            "response_payload": previous_report.get("response_payload"),
            "source_generated_at": source_generated_at.astimezone(timezone.utc).isoformat(),
            "source_mode": source_mode,
        }
    return None


def _build_autotune_profile_payloads(active_state: dict) -> tuple[dict[str, dict], dict[str, str]]:
    selector_result = _run_selector()
    current_profile = str(active_state.get("profile") or "scalp_guarded").strip().lower() or "scalp_guarded"
    profiles = list(dict.fromkeys([current_profile] + list(SUPPORTED_AUTOTUNE_PROFILES)))
    top = max(1, int(len(active_state.get("selected") or []) or _env_int("ACTIVE_TOP", 4) or 4))
    watch_top = _env_int("WATCH_TOP", int(active_state.get("watch_top") or 0))
    switch_margin_score = _env_float("SWITCH_MARGIN_SCORE", _safe_float(active_state.get("switch_margin_score"), 8.0))
    min_active_minutes = _env_float("MIN_ACTIVE_MINUTES", _safe_float(active_state.get("min_active_minutes"), 5.0))
    active_retain_min_score = _env_float(
        "ACTIVE_RETAIN_MIN_SCORE",
        _safe_float(active_state.get("active_retain_min_score"), 40.0),
    )
    max_retain_position_pct = _env_float(
        "MAX_RETAIN_POSITION_PCT",
        _safe_float(active_state.get("max_retain_position_pct"), 85.0),
    )
    generated_at = datetime.now(timezone.utc)
    payloads: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for profile in profiles:
        try:
            payloads[profile] = build_selector_payload(
                top=top,
                watch_top=watch_top,
                profile_name=profile,
                switch_margin_score=switch_margin_score,
                min_active_minutes=min_active_minutes,
                active_retain_min_score=active_retain_min_score,
                max_retain_position_pct=max_retain_position_pct,
                selector_result=selector_result,
                previous_payload=active_state,
                generated_at=generated_at,
                persist_runtime=False,
            )
        except Exception as exc:
            errors[profile] = str(exc)
    return payloads, errors


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _merge_autotune_recommendation(
    recommendation: dict,
    autotune: dict,
    *,
    stable_window: bool = False,
) -> dict:
    merged = dict(recommendation or {})
    if not isinstance(autotune, dict) or not autotune.get("enabled"):
        return merged

    confidence = max(0.0, _safe_float(autotune.get("confidence"), 0.0))
    score_margin = max(0.0, _safe_float(autotune.get("score_margin"), 0.0))
    trade_count = max(0, int(round(_safe_float(autotune.get("trade_count"), 0.0))))
    no_trade_sample_count = max(0, int(round(_safe_float(autotune.get("no_trade_sample_count"), 0.0))))
    parameter_overrides = autotune.get("parameter_overrides") if isinstance(autotune.get("parameter_overrides"), dict) else {}
    risk_mode_override = str(autotune.get("risk_mode_override") or "").strip().lower()
    early_exit_bias = max(0.0, _safe_float(autotune.get("early_exit_bias"), 0.0))
    protective_exit_bias = max(0.0, _safe_float(autotune.get("protective_exit_bias"), 0.0))
    missed_entry_bias = max(0.0, _safe_float(autotune.get("missed_entry_bias"), 0.0))
    correct_block_bias = max(0.0, _safe_float(autotune.get("correct_block_bias"), 0.0))
    shakeout_then_run_rate = max(0.0, _safe_float(autotune.get("shakeout_then_run_rate"), 0.0))
    micro_pop_loss_run_rate = max(0.0, _safe_float(autotune.get("micro_pop_loss_run_rate"), 0.0))
    failed_start_recovery_rate = max(0.0, _safe_float(autotune.get("failed_start_recovery_rate"), 0.0))
    hold_opportunity_rate = max(0.0, _safe_float(autotune.get("hold_opportunity_rate"), 0.0))
    exit_problem_symbols = {
        str(item).strip().upper()
        for item in (autotune.get("exit_problem_symbols") or [])
        if str(item).strip()
    }
    evidence_count = max(trade_count, no_trade_sample_count)
    min_profile_confidence = 0.62 if stable_window else 0.50
    min_profile_margin = 20.0 if stable_window else 12.0
    min_profile_evidence = 8 if stable_window else 6
    if confidence >= 0.35 and parameter_overrides:
        merged_overrides = dict(merged.get("parameter_overrides") or {})
        merged_overrides.update(parameter_overrides)
        merged["parameter_overrides"] = merged_overrides

    existing_avoids = [
        str(item).strip().upper()
        for item in (merged.get("avoid_symbols") or [])
        if str(item).strip()
    ]
    if exit_problem_symbols and early_exit_bias > (protective_exit_bias + 0.10) and failed_start_recovery_rate >= 0.35:
        existing_avoids = [symbol for symbol in existing_avoids if symbol not in exit_problem_symbols]
    for symbol in autotune.get("avoid_symbols") or []:
        symbol_value = str(symbol).strip().upper()
        if symbol_value and symbol_value not in existing_avoids:
            existing_avoids.append(symbol_value)
    if existing_avoids:
        merged["avoid_symbols"] = existing_avoids[:8]

    current_risk_mode = str(merged.get("risk_mode") or "").strip().lower()
    if (
        risk_mode_override in {"normal", "cautious"}
        and confidence >= min_profile_confidence
        and score_margin >= min_profile_margin
        and evidence_count >= min_profile_evidence
        and max(early_exit_bias, missed_entry_bias) > (max(protective_exit_bias, correct_block_bias) + 0.10)
        and current_risk_mode == "stop_new_entries"
    ):
        merged["risk_mode"] = risk_mode_override
        current_risk_mode = risk_mode_override

    if (
        current_risk_mode != "stop_new_entries"
        and confidence >= min_profile_confidence
        and score_margin >= min_profile_margin
        and evidence_count >= min_profile_evidence
    ):
        profile = str(autotune.get("recommended_profile") or merged.get("profile") or "").strip().lower()
        if profile:
            merged["profile"] = profile
            merged["profile_override"] = profile

    notes = str(merged.get("notes") or "").strip()
    autotune_note = str(autotune.get("reason") or "").strip()
    if autotune_note:
        merged["notes"] = f"{notes}; local_autotune:{autotune_note}" if notes else f"local_autotune:{autotune_note}"
    merged["local_autotune"] = {
        "recommended_profile": autotune.get("recommended_profile"),
        "confidence": confidence,
        "score_margin": score_margin,
        "risk_mode_override": risk_mode_override,
        "trade_count": trade_count,
        "no_trade_sample_count": no_trade_sample_count,
        "early_exit_bias": early_exit_bias,
        "protective_exit_bias": protective_exit_bias,
        "failed_start_recovery_rate": failed_start_recovery_rate,
        "hold_opportunity_rate": hold_opportunity_rate,
        "missed_entry_bias": missed_entry_bias,
        "correct_block_bias": correct_block_bias,
        "shakeout_then_run_rate": shakeout_then_run_rate,
        "micro_pop_loss_run_rate": micro_pop_loss_run_rate,
    }
    return merged


def _refresh_watch_pool(script_path: Path, report_path: Path) -> dict:
    if not script_path.is_file():
        return {
            "attempted": False,
            "ok": False,
            "reason": "script_missing",
            "path": str(script_path),
        }
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "attempted": True,
            "ok": False,
            "reason": "timeout",
            "path": str(script_path),
        }
    report = _load_json(report_path) or {}
    return {
        "attempted": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "path": str(script_path),
        "report_path": str(report_path),
        "stdout": proc.stdout.strip()[-4000:],
        "stderr": proc.stderr.strip()[-4000:],
        "report_generated_at": report.get("generated_at"),
        "watch_symbols": report.get("watch_symbols"),
        "added_symbols": report.get("added_symbols"),
        "removed_symbols": report.get("removed_symbols"),
    }


def _normalized_profile(recommendation: dict, current_profile: str) -> str:
    raw = str(recommendation.get("profile") or "").strip().lower()
    if raw in {"scalp", "scalp_breakout", "scalp_uptrend", "scalp_guarded", "scalp_lockdown"}:
        return raw
    if raw == "hold":
        return "scalp_lockdown"
    return str(current_profile or "scalp_guarded").strip().lower() or "scalp_guarded"


def _build_slot_plan(
    recommendation: dict,
    *,
    top: int,
) -> list[str]:
    runtime_strategies = [name for name in STRATEGY_NAMES if name in CORE_META_STRATEGIES]
    strategy_actions = recommendation.get("strategy_actions") if isinstance(recommendation.get("strategy_actions"), dict) else {}
    strategy_weights = recommendation.get("strategy_weights") if isinstance(recommendation.get("strategy_weights"), dict) else {}
    top = max(1, int(top))
    mode_rank = {"primary": 3, "secondary": 2, "watch": 1, "pause": 0}
    plan: list[str] = []
    active_rows: list[tuple[str, str, int, float]] = []
    for strategy in runtime_strategies:
        action = strategy_actions.get(strategy) if isinstance(strategy_actions.get(strategy), dict) else {}
        mode = str(action.get("mode") or "watch").strip().lower()
        slot_target = max(0, int(action.get("slot_target") or 0))
        weight = max(0.0, float(strategy_weights.get(strategy) or 0.0))
        if mode in {"primary", "secondary"} and (slot_target > 0 or weight > 0.0):
            active_rows.append((strategy, mode, slot_target, weight))

    active_rows.sort(
        key=lambda item: (
            -item[2],
            -mode_rank.get(item[1], 0),
            -item[3],
            runtime_strategies.index(item[0]),
        )
    )
    for strategy, _mode, slot_target, _weight in active_rows:
        plan.extend([strategy] * max(0, int(slot_target)))

    remaining = top - len(plan)
    if remaining > 0 and active_rows:
        weighted_cycle: list[str] = []
        for strategy, mode, _slot_target, weight in sorted(
            active_rows,
            key=lambda item: (
                -item[3],
                -mode_rank.get(item[1], 0),
                runtime_strategies.index(item[0]),
            ),
        ):
            repeats = max(1, int(round(max(0.05, weight) * 10.0)))
            weighted_cycle.extend([strategy] * repeats)
        if not weighted_cycle:
            weighted_cycle = [item[0] for item in active_rows]
        idx = 0
        while len(plan) < top and weighted_cycle:
            plan.append(weighted_cycle[idx % len(weighted_cycle)])
            idx += 1

    return plan[:top]


def _slot_plan_decision(
    recommendation: dict,
    *,
    top: int,
) -> tuple[list[str], str]:
    risk_mode = str(recommendation.get("risk_mode") or "").strip().lower()
    confidence = max(0.0, float(recommendation.get("confidence") or 0.0))
    min_confidence = max(0.0, float(os.getenv("ROTATION_META_MIN_CONFIDENCE_SLOT_PLAN", "0.55")))
    min_active_strategies = max(1, int(os.getenv("ROTATION_META_MIN_ACTIVE_STRATEGIES", "2")))
    strategy_actions = recommendation.get("strategy_actions") if isinstance(recommendation.get("strategy_actions"), dict) else {}
    active_strategies = []
    for strategy in CORE_META_STRATEGIES:
        action = strategy_actions.get(strategy) if isinstance(strategy_actions.get(strategy), dict) else {}
        mode = str(action.get("mode") or "").strip().lower()
        slot_target = max(0, int(action.get("slot_target") or 0))
        if mode in {"primary", "secondary"}:
            active_strategies.append(strategy)
            continue
        # Allow softer watch-mode strategy guidance to form a slot plan when the
        # LLM is cautious but still names concrete strategies/symbols worth lining up.
        if mode == "watch" and slot_target > 0:
            active_strategies.append(strategy)
    if risk_mode == "stop_new_entries":
        return [], "risk_stop_new_entries"
    if confidence < min_confidence:
        return [], f"confidence_below_{min_confidence:.2f}"
    if len(active_strategies) < min_active_strategies:
        return [], f"active_strategies_below_{min_active_strategies}"
    slot_plan = _build_slot_plan(recommendation, top=top)
    if len(set(slot_plan)) < min_active_strategies:
        return [], f"slot_plan_diversity_below_{min_active_strategies}"
    return slot_plan, "applied"


def _apply_hard_risk_caps(recommendation: dict, trade_summary) -> dict:
    capped = dict(recommendation or {})
    trade_count = max(0, int(getattr(trade_summary, "trade_count", 0) or 0))
    net_pnl = float(getattr(trade_summary, "net_pnl", 0.0) or 0.0)
    exit_reasons = getattr(trade_summary, "exit_reasons", {}) or {}
    failed_start_rate = (
        float(exit_reasons.get("failed_start_exit") or 0.0) / float(trade_count)
        if trade_count > 0 else 0.0
    )
    exit_path_summary = getattr(trade_summary, "exit_path_summary", {}) or {}
    early_exit_rate = _safe_float(exit_path_summary.get("early_exit_rate"), 0.0)
    protective_exit_rate = _safe_float(exit_path_summary.get("protective_exit_rate"), 0.0)
    failed_start_recovery_rate = _safe_float(exit_path_summary.get("failed_start_recovery_rate"), 0.0)
    trailing_missed_run_rate = _safe_float(exit_path_summary.get("trailing_missed_run_rate"), 0.0)
    shakeout_then_run_rate = _safe_float(exit_path_summary.get("shakeout_then_run_rate"), 0.0)
    micro_pop_loss_run_rate = _safe_float(exit_path_summary.get("micro_pop_loss_run_rate"), 0.0)
    early_exit_pressure = max(
        0.0,
        early_exit_rate
        + (0.80 * failed_start_recovery_rate)
        + (0.55 * trailing_missed_run_rate)
        + (0.70 * shakeout_then_run_rate)
        + (0.95 * micro_pop_loss_run_rate)
        - (0.70 * protective_exit_rate),
    )
    early_exit_cluster = (
        _safe_float(exit_path_summary.get("sample_count"), 0.0) >= 4.0
        and early_exit_pressure >= 0.55
        and (
            failed_start_recovery_rate >= 0.35
            or shakeout_then_run_rate >= 0.20
            or micro_pop_loss_run_rate >= 0.15
        )
    )
    severe_failed_start_cluster = trade_count >= 6 and net_pnl <= -0.18 and failed_start_rate >= 0.60
    hard_stop_cluster = trade_count >= 8 and net_pnl <= -0.25 and failed_start_rate >= 0.70
    if not (severe_failed_start_cluster or hard_stop_cluster):
        return capped

    risk_mode = "stop_new_entries" if hard_stop_cluster else "cautious"
    if early_exit_cluster:
        risk_mode = "cautious"
    strategy_weights = {
        "staircase": 0.0 if hard_stop_cluster else 0.22,
        "continuation": 0.52 if not hard_stop_cluster else 0.48,
        "breakout": 0.08 if not hard_stop_cluster else 0.0,
        "rebound": 0.18 if not hard_stop_cluster else 0.52,
    }
    if early_exit_cluster:
        strategy_weights = {
            "staircase": 0.34,
            "continuation": 0.34,
            "breakout": 0.22,
            "rebound": 0.10,
        }
    strategy_actions = {}
    for strategy in CORE_META_STRATEGIES:
        mode = "pause"
        slot_target = 0
        if risk_mode != "stop_new_entries":
            if early_exit_cluster and strategy in {"staircase", "breakout"}:
                mode = "secondary"
                slot_target = 1
            elif strategy == "continuation":
                mode = "secondary"
                slot_target = 1
        strategy_actions[strategy] = {
            "mode": mode,
            "slot_target": slot_target,
            "top_symbols": [],
        }

    notes = str(capped.get("notes") or "").strip()
    suffix = (
        f"hard_risk_cap: trade_count={trade_count} net_pnl={net_pnl:.6f} "
        f"failed_start_rate={failed_start_rate:.3f}"
    )
    if early_exit_cluster:
        suffix = (
            f"{suffix} early_exit_pressure={early_exit_pressure:.3f} "
            f"failed_start_recovery_rate={failed_start_recovery_rate:.3f} "
            f"shakeout_then_run_rate={shakeout_then_run_rate:.3f} "
            f"micro_pop_loss_run_rate={micro_pop_loss_run_rate:.3f}"
        )
    capped["profile"] = "scalp_guarded" if early_exit_cluster else "scalp_lockdown"
    capped["risk_mode"] = risk_mode
    capped["confidence"] = min(0.95, max(0.65, float(capped.get("confidence") or 0.0)))
    capped["strategy_weights"] = strategy_weights
    capped["strategy_actions"] = strategy_actions
    capped["candidate_overrides"] = []
    capped["notes"] = f"{notes}; {suffix}" if notes else suffix
    return capped


def _write_selector_runtime_env(
    path: Path,
    *,
    generated_at: str,
    recommendation: dict,
    current_profile: str,
    top: int,
    mode: str,
) -> dict:
    profile = _normalized_profile(recommendation, current_profile)
    slot_plan, slot_plan_reason = _slot_plan_decision(recommendation, top=top)
    strategy_actions = recommendation.get("strategy_actions") if isinstance(recommendation.get("strategy_actions"), dict) else {}
    strategy_weights = recommendation.get("strategy_weights") if isinstance(recommendation.get("strategy_weights"), dict) else {}
    parameter_overrides = recommendation.get("parameter_overrides") if isinstance(recommendation.get("parameter_overrides"), dict) else {}
    candidate_overrides = [
        str(item).strip().upper()
        for item in recommendation.get("candidate_overrides", [])
        if str(item).strip()
    ]
    avoid_symbols = [
        str(item).strip().upper()
        for item in recommendation.get("avoid_symbols", [])
        if str(item).strip()
    ]
    lines = [
        "# Auto-generated by rotation_meta_shadow.py",
        _shell_assign("ROTATION_META_RECOMMENDATION_GENERATED_AT", generated_at),
        _shell_assign("ROTATION_META_MODE", mode),
        _shell_assign("ROTATION_META_CONFIDENCE", recommendation.get("confidence", 0.0)),
        _shell_assign("ROTATION_META_RISK_MODE", str(recommendation.get("risk_mode") or "")),
        _shell_assign("ROTATION_META_SLOT_PLAN_REASON", slot_plan_reason),
        _shell_assign("ROTATION_PROFILE", profile),
        _shell_assign("ROTATION_PROFILE_OVERRIDE", str(recommendation.get("profile_override") or profile)),
        _shell_assign("ROTATION_STRATEGY_SLOT_PLAN", ",".join(slot_plan)),
        _shell_assign("ROTATION_META_CANDIDATE_OVERRIDES", ",".join(candidate_overrides)),
        _shell_assign("ROTATION_META_AVOID_SYMBOLS", ",".join(avoid_symbols)),
    ]
    for key in sorted(parameter_overrides):
        lines.append(_shell_assign(str(key), parameter_overrides[key]))
    for strategy in CORE_META_STRATEGIES:
        weight = float(strategy_weights.get(strategy) or 0.0)
        action = strategy_actions.get(strategy) if isinstance(strategy_actions.get(strategy), dict) else {}
        mode_value = str(action.get("mode") or "watch").strip().lower()
        slot_target = max(0, int(action.get("slot_target") or 0))
        top_symbols = [
            str(item).strip().upper()
            for item in action.get("top_symbols", [])
            if str(item).strip()
        ]
        lines.append(_shell_assign(f"ROTATION_STRATEGY_WEIGHT_{strategy.upper()}", weight))
        lines.append(_shell_assign(f"ROTATION_STRATEGY_ACTION_{strategy.upper()}", mode_value))
        lines.append(_shell_assign(f"ROTATION_STRATEGY_SLOT_TARGET_{strategy.upper()}", slot_target))
        lines.append(_shell_assign(f"ROTATION_STRATEGY_TOP_SYMBOLS_{strategy.upper()}", ",".join(top_symbols)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "generated_at": generated_at,
        "mode": mode,
        "profile": profile,
        "slot_plan": slot_plan,
        "path": str(path),
        "risk_mode": str(recommendation.get("risk_mode") or ""),
        "confidence": float(recommendation.get("confidence") or 0.0),
        "slot_plan_applied": bool(slot_plan),
        "slot_plan_reason": slot_plan_reason,
        "strategy_weights": strategy_weights,
        "strategy_actions": strategy_actions,
        "parameter_overrides": parameter_overrides,
        "candidate_overrides": candidate_overrides,
        "avoid_symbols": avoid_symbols,
    }


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    preloaded_env_file = _preload_env_file(argv)
    _load_local_secret_envs()

    ap = argparse.ArgumentParser(description="Build local ML shadow scores and optional OpenAI meta recommendations.")
    ap.add_argument("--env-file", default=str(preloaded_env_file))
    ap.add_argument("--active-file", default=str(ACTIVE_FILE))
    ap.add_argument("--log-dir", default=str(LOG_DIR))
    ap.add_argument("--dataset-file", default=str(DATASET_FILE))
    ap.add_argument("--model-file", default=str(MODEL_FILE))
    ap.add_argument("--ml-report-file", default=str(ML_REPORT_FILE))
    ap.add_argument("--openai-report-file", default=str(OPENAI_REPORT_FILE))
    ap.add_argument("--meta-report-file", default=str(META_REPORT_FILE))
    ap.add_argument("--autotune-report-file", default=str(AUTOTUNE_REPORT_FILE))
    ap.add_argument("--universe-report-file", default=str(UNIVERSE_REPORT_FILE))
    ap.add_argument("--selector-runtime-env-file", default=str(SELECTOR_RUNTIME_ENV_FILE))
    ap.add_argument("--selector-apply-report-file", default=str(SELECTOR_APPLY_REPORT_FILE))
    ap.add_argument(
        "--trade-lookback-hours",
        type=float,
        default=_env_float("ROTATION_META_TRADE_LOOKBACK_HOURS", 6.0),
    )
    ap.add_argument(
        "--min-samples",
        type=int,
        default=_env_int("ROTATION_META_MIN_SAMPLES", 24),
    )
    ap.add_argument(
        "--epochs",
        type=int,
        default=_env_int("ROTATION_META_EPOCHS", 700),
    )
    ap.add_argument(
        "--openai-min-interval-minutes",
        type=float,
        default=_env_float("ROTATION_OPENAI_MIN_INTERVAL_MINUTES", 15.0),
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--no-openai",
        action="store_true",
        default=(
            _env_bool("ROTATION_NO_OPENAI", False)
            or not _env_bool("ROTATION_OPENAI_ENABLED", True)
        ),
    )
    ap.add_argument("--no-autotune", action="store_true")
    ap.add_argument("--no-retrain", action="store_true")
    ap.add_argument("--no-selector-apply", action="store_true")
    ap.add_argument("--no-watch-pool-refresh", action="store_true")
    ap.add_argument("--watch-pool-refresh-script", default=str(WATCH_POOL_REFRESH_SCRIPT))
    ap.add_argument("--watch-pool-refresh-report-file", default=str(WATCH_POOL_REFRESH_REPORT_FILE))
    args = ap.parse_args(argv)
    env_file = Path(args.env_file)
    if env_file != preloaded_env_file:
        merge_env_file(env_file)

    log_dir = Path(args.log_dir)
    active_file = Path(args.active_file)
    dataset_file = Path(args.dataset_file)
    model_file = Path(args.model_file)
    ml_report_file = Path(args.ml_report_file)
    openai_report_file = Path(args.openai_report_file)
    meta_report_file = Path(args.meta_report_file)
    autotune_report_file = Path(args.autotune_report_file)
    universe_report_file = Path(args.universe_report_file)
    selector_runtime_env_file = Path(args.selector_runtime_env_file)
    selector_apply_report_file = Path(args.selector_apply_report_file)
    watch_pool_refresh_script = Path(args.watch_pool_refresh_script)
    watch_pool_refresh_report_file = Path(args.watch_pool_refresh_report_file)

    samples = extract_trade_samples(log_dir)
    counterfactual_samples = extract_counterfactual_samples(
        log_dir,
        lookback_hours=args.trade_lookback_hours,
    )
    export_samples_jsonl(dataset_file, samples)
    trade_summary = build_trade_summary(
        samples,
        lookback_hours=args.trade_lookback_hours,
        no_trade_samples=counterfactual_samples,
    )
    recent_trade_examples = build_recent_trade_examples(
        samples,
        lookback_hours=args.trade_lookback_hours,
        limit=6,
    )
    recent_no_trade_examples = build_recent_no_trade_examples(
        counterfactual_samples,
        lookback_hours=args.trade_lookback_hours,
        limit=4,
    )

    model: LogisticTradeModel | None = None
    model_source = "none"
    train_error = None
    if len(samples) >= int(args.min_samples) and not args.no_retrain:
        try:
            model = train_trade_model(samples, epochs=int(args.epochs))
            save_model(model_file, model)
            model_source = "fresh_train"
        except Exception as exc:
            train_error = str(exc)
    if model is None and model_file.exists():
        try:
            model = load_model(model_file)
            model_source = "existing_model"
        except Exception as exc:
            train_error = str(exc)

    active_state = load_rotation_state(active_file)
    universe_report = _load_json(universe_report_file)
    current_profile = str(active_state.get("profile") or "scalp_guarded")
    generated_at = datetime.now(timezone.utc).isoformat()
    generated_dt = _parse_iso8601(generated_at) or datetime.now(timezone.utc)
    candidates = build_shadow_candidates(log_dir, active_state, model) if model is not None else []
    watch_pool_strategy_summary = build_watch_pool_strategy_summary(active_state, candidates) if candidates else {}

    autotune_payloads, autotune_errors = ({}, {})
    if not args.no_autotune:
        autotune_payloads, autotune_errors = _build_autotune_profile_payloads(active_state)
    autotune_report = recommend_rotation_autotune(
        current_profile=current_profile,
        trade_summary=trade_summary,
        candidates=candidates,
        profile_payloads=autotune_payloads,
    ) if not args.no_autotune else {
        "enabled": False,
        "current_profile": current_profile,
        "recommended_profile": current_profile,
        "confidence": 0.0,
        "score_margin": 0.0,
        "parameter_overrides": {},
        "avoid_symbols": [],
        "evaluations": [],
        "reason": "disabled_via_cli",
    }
    autotune_report["generated_at"] = generated_at
    autotune_report["selector_errors"] = autotune_errors
    _write_json(autotune_report_file, autotune_report)
    ml_report = {
        "generated_at": generated_at,
        "current_profile": current_profile,
        "trade_lookback_hours": float(args.trade_lookback_hours),
        "trade_summary": {
            "trade_count": trade_summary.trade_count,
            "net_pnl": trade_summary.net_pnl,
            "win_rate": trade_summary.win_rate,
            "avg_win": trade_summary.avg_win,
            "avg_loss": trade_summary.avg_loss,
            "last_exit_ts": trade_summary.last_exit_ts,
            "exit_reasons": trade_summary.exit_reasons,
            "exit_path_summary": trade_summary.exit_path_summary,
            "no_trade_summary": trade_summary.no_trade_summary,
            "strategy_breakdown": trade_summary.strategy_breakdown,
            "symbol_breakdown": trade_summary.symbol_breakdown,
            "regime_breakdown": trade_summary.regime_breakdown,
            "session_breakdown": trade_summary.session_breakdown,
        },
        "model": _model_info(model, model_source),
        "train_error": train_error,
        "candidate_count": len(candidates),
        "counterfactual_sample_count": len(counterfactual_samples),
        "top_candidates": [item.to_dict() for item in candidates[:12]],
        "watch_pool_strategy_summary": watch_pool_strategy_summary,
    }
    _write_json(ml_report_file, ml_report)

    fallback = fallback_recommendation(
        current_profile=str(autotune_report.get("recommended_profile") or current_profile),
        trade_summary=trade_summary,
        candidates=candidates,
        watch_pool_strategy_summary=watch_pool_strategy_summary,
    )
    fallback = _merge_autotune_recommendation(fallback, autotune_report)
    prompt_payload = build_prompt_payload(
        current_profile=current_profile,
        active_state=active_state,
        trade_summary=trade_summary,
        candidates=candidates,
        model_info=_model_info(model, model_source),
        universe_report=universe_report,
        watch_pool_strategy_summary=watch_pool_strategy_summary,
        recent_trade_examples=recent_trade_examples,
        recent_no_trade_examples=recent_no_trade_examples,
    )
    previous_openai_report = _load_json(openai_report_file)
    requested_model = str(os.getenv("ROTATION_OPENAI_MODEL", "gpt-5-mini")).strip() or "gpt-5-mini"
    reused_openai_report = None if args.no_openai else _reuse_recent_openai_report(
        previous_openai_report,
        now=generated_dt,
        requested_model=requested_model,
        min_interval_minutes=args.openai_min_interval_minutes,
    )
    openai_result = None
    if reused_openai_report is None and not args.no_openai:
        openai_result = call_openai_meta(
            prompt_payload=prompt_payload,
            fallback=fallback,
        )

    openai_report = {
        "generated_at": generated_at,
        "trade_lookback_hours": float(args.trade_lookback_hours),
        "mode": (
            reused_openai_report["mode"]
            if reused_openai_report is not None
            else openai_result.mode if openai_result is not None else "disabled"
        ),
        "model": (
            reused_openai_report["model"]
            if reused_openai_report is not None
            else openai_result.model if openai_result is not None else None
        ),
        "warning": (
            reused_openai_report["warning"]
            if reused_openai_report is not None
            else openai_result.warning if openai_result is not None else "disabled_via_cli"
        ),
        "recommendation": {},
        "input_payload": prompt_payload,
        "response_payload": (
            reused_openai_report["response_payload"]
            if reused_openai_report is not None
            else openai_result.response_payload if openai_result is not None else None
        ),
        "source_generated_at": (
            reused_openai_report["source_generated_at"]
            if reused_openai_report is not None
            else generated_at if openai_result is not None and openai_result.mode in {"llm_sdk", "llm_http"} else None
        ),
        "source_mode": (
            reused_openai_report["source_mode"]
            if reused_openai_report is not None
            else openai_result.mode if openai_result is not None and openai_result.mode in {"llm_sdk", "llm_http"} else None
        ),
    }
    final_recommendation = _apply_hard_risk_caps(
        (
            reused_openai_report["recommendation"]
            if reused_openai_report is not None
            else openai_result.recommendation if openai_result is not None else fallback
        ),
        trade_summary,
    )
    final_recommendation = _merge_autotune_recommendation(
        final_recommendation,
        autotune_report,
        stable_window=reused_openai_report is not None,
    )
    openai_report["recommendation"] = final_recommendation
    openai_report["profile"] = final_recommendation.get("profile")
    openai_report["risk_mode"] = final_recommendation.get("risk_mode")
    openai_report["confidence"] = final_recommendation.get("confidence")
    openai_report["notes"] = final_recommendation.get("notes")
    _write_json(openai_report_file, openai_report)

    selector_top = max(1, int(len(active_state.get("selected") or []) or 4))
    selector_apply = _write_selector_runtime_env(
        selector_runtime_env_file,
        generated_at=generated_at,
        recommendation=openai_report["recommendation"],
        current_profile=current_profile,
        top=selector_top,
        mode=openai_report["mode"],
    ) if not args.no_selector_apply else {
        "generated_at": generated_at,
        "mode": "disabled_via_cli",
        "profile": current_profile,
        "slot_plan": [],
        "path": str(selector_runtime_env_file),
    }
    _write_json(selector_apply_report_file, selector_apply)

    watch_pool_refresh = (
        _refresh_watch_pool(watch_pool_refresh_script, watch_pool_refresh_report_file)
        if not args.no_watch_pool_refresh
        else {
            "attempted": False,
            "ok": False,
            "reason": "disabled_via_cli",
            "path": str(watch_pool_refresh_script),
        }
    )

    combined = {
        "generated_at": generated_at,
        "shadow_only": True,
        "current_profile": current_profile,
        "trade_lookback_hours": float(args.trade_lookback_hours),
        "trade_summary": ml_report["trade_summary"],
        "model": ml_report["model"],
        "candidate_count": len(candidates),
        "top_candidates": [item.to_dict() for item in candidates[:8]],
        "watch_pool_strategy_summary": watch_pool_strategy_summary,
        "autotune": autotune_report,
        "universe_report_at": (universe_report or {}).get("generated_at"),
        "universe_strategy_rankings": (universe_report or {}).get("strategy_rankings"),
        "selector_apply": selector_apply,
        "watch_pool_refresh": watch_pool_refresh,
        "meta_mode": openai_report["mode"],
        "recommendation": openai_report["recommendation"],
        "warning": openai_report["warning"],
    }
    _write_json(meta_report_file, combined)

    if args.json:
        print(json.dumps(combined, ensure_ascii=True, indent=2))
        return

    print(f"samples: {len(samples)}")
    print(f"model: {model_source}")
    print(f"current_profile: {current_profile}")
    print(f"trade_window: {trade_summary.trade_count} trades net={trade_summary.net_pnl:.6f}")
    print(f"meta_mode: {openai_report['mode']}")
    print(f"recommendation_profile: {openai_report['recommendation'].get('profile')}")
    top_symbols = ", ".join(item.symbol for item in candidates[:4]) if candidates else "(none)"
    print(f"top_candidates: {top_symbols}")


if __name__ == "__main__":
    main()
