from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import sqrt
from pathlib import Path
from time import sleep
from typing import Any

import yaml

from btc_news_arrow.config import load_config
from btc_news_arrow.learner import Learner
from btc_news_arrow.llm_rater import LLMRater
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import parse_datetime, parse_duration, utcnow


@dataclass(slots=True)
class Attempt:
    window: str
    success: bool
    message: str
    meta: dict[str, object]


@dataclass(slots=True)
class HybridSample:
    window: str
    horizon_minutes: int
    timestamp_utc: object
    rule_value: float
    learn_value: float
    target_return: float


def _reference_now_from_storage(storage: Storage):
    try:
        row = storage.conn.execute("SELECT MAX(timestamp_utc) AS ts FROM items").fetchone()
    except Exception:
        row = None
    latest_ts = None
    if row is not None:
        try:
            latest_ts = parse_datetime(row["ts"])
        except Exception:
            latest_ts = None
    return latest_ts or utcnow()


def optimize_llm_config(
    config_path: str | Path,
    db_path: str | Path,
    windows: list[str],
    cycles: int = 3,
    sleep_seconds: int = 20,
) -> dict[str, object]:
    cfg_path = Path(config_path)
    raw_cfg = _read_raw_config(cfg_path)

    history: list[dict[str, object]] = []
    for cycle in range(1, max(1, int(cycles)) + 1):
        merged = load_config(cfg_path)
        attempts = _run_attempts(merged, db_path, windows)
        failures = [a for a in attempts if not a.success]
        history.append(
            {
                "cycle": cycle,
                "attempts": [
                    {
                        "window": a.window,
                        "success": a.success,
                        "message": a.message,
                        "meta": a.meta,
                    }
                    for a in attempts
                ],
            }
        )
        if not failures:
            return {"ok": True, "history": history, "updated": False}

        llm_raw = raw_cfg.setdefault("llm", {})
        _apply_tuning_step(llm_raw, failures)
        _write_raw_config(cfg_path, raw_cfg)

        if cycle < cycles and sleep_seconds > 0:
            sleep(float(sleep_seconds))

    return {"ok": False, "history": history, "updated": True}


def optimize_hybrid_weights(
    config_path: str | Path,
    db_path: str | Path,
    windows: list[str],
    lookback_days: int = 30,
    min_samples: int = 120,
    grid_step: float = 0.05,
) -> dict[str, object]:
    cfg_path = Path(config_path)
    cfg = load_config(cfg_path)
    learner = Learner(cfg)
    storage = Storage(db_path)
    try:
        now = _reference_now_from_storage(storage)
        since = now - timedelta(days=max(1, int(lookback_days)))
        samples = _build_hybrid_samples(storage, learner, windows, since)
        if len(samples) < max(20, int(min_samples)):
            return {
                "ok": False,
                "reason": "not_enough_samples",
                "samples": len(samples),
                "min_samples": int(min_samples),
            }

        target = [s.target_return for s in samples]
        rule = [s.rule_value for s in samples]
        learn = [s.learn_value for s in samples]
        z_target = _zscore(target)
        z_rule = _zscore(rule)
        z_learn = _zscore(learn)

        if _is_constant(z_target) or (_is_constant(z_rule) and _is_constant(z_learn)):
            return {
                "ok": False,
                "reason": "insufficient_variance",
                "samples": len(samples),
            }

        hybrid_cfg = cfg.get("hybrid", {})
        opt_cfg = hybrid_cfg.get("optimizer", {})
        holdout_ratio = min(0.5, max(0.0, float(opt_cfg.get("holdout_ratio", 0.2))))
        min_holdout_samples = max(0, int(opt_cfg.get("min_holdout_samples", 50)))
        require_holdout_improvement = bool(opt_cfg.get("require_holdout_improvement", True))
        min_holdout_improvement = float(opt_cfg.get("min_holdout_improvement", 0.0))
        min_train_improvement = float(opt_cfg.get("min_train_improvement", 0.0))
        max_share_shift = max(0.0, float(opt_cfg.get("max_share_shift", 0.5)))

        raw_cfg = _read_raw_config(cfg_path)
        hybrid_raw = raw_cfg.setdefault("hybrid", {})
        llm_weight_old = float(hybrid_raw.get("llm_weight", hybrid_cfg.get("llm_weight", 0.35)))
        llm_weight_new = max(0.0, min(0.95, llm_weight_old))
        residual = 1.0 - llm_weight_new

        rule_weight_old = float(hybrid_raw.get("rule_weight", hybrid_cfg.get("rule_weight", 0.5)))
        learn_weight_old = float(hybrid_raw.get("learn_weight", hybrid_cfg.get("learn_weight", 0.15)))
        old_total = rule_weight_old + learn_weight_old
        if old_total <= 0:
            old_rule_share = 0.5
            old_learn_share = 0.5
        else:
            old_rule_share = rule_weight_old / old_total
            old_learn_share = learn_weight_old / old_total

        train_size = len(samples)
        holdout_size = 0
        if holdout_ratio > 0:
            candidate_holdout = int(round(len(samples) * holdout_ratio))
            if candidate_holdout >= min_holdout_samples and (len(samples) - candidate_holdout) >= 20:
                holdout_size = candidate_holdout
                train_size = len(samples) - holdout_size

        z_target_train = z_target[:train_size]
        z_rule_train = z_rule[:train_size]
        z_learn_train = z_learn[:train_size]
        z_target_holdout = z_target[train_size:] if holdout_size else []
        z_rule_holdout = z_rule[train_size:] if holdout_size else []
        z_learn_holdout = z_learn[train_size:] if holdout_size else []

        best_rule_share = None
        best_train_corr = -2.0
        candidates = _weight_grid(max(0.01, float(grid_step)))
        for rule_share in candidates:
            pred = _blend_series(z_rule_train, z_learn_train, rule_share)
            corr = _pearson(pred, z_target_train)
            if corr > best_train_corr:
                best_train_corr = corr
                best_rule_share = rule_share

        if best_rule_share is None:
            return {"ok": False, "reason": "no_candidate", "samples": len(samples)}

        rule_share = _clamp_rule_share(best_rule_share, old_rule_share, max_share_shift=max_share_shift)
        learn_share = 1.0 - rule_share

        pred_train_old = _blend_series(z_rule_train, z_learn_train, old_rule_share)
        pred_train_new = _blend_series(z_rule_train, z_learn_train, rule_share)
        corr_train_before = _pearson(pred_train_old, z_target_train)
        corr_train_after = _pearson(pred_train_new, z_target_train)

        if corr_train_after + 1e-12 < corr_train_before + min_train_improvement:
            return {
                "ok": False,
                "reason": "no_train_improvement",
                "samples": len(samples),
                "corr_train_before": corr_train_before,
                "corr_train_after": corr_train_after,
            }

        corr_holdout_before = None
        corr_holdout_after = None
        if holdout_size > 0:
            pred_holdout_old = _blend_series(z_rule_holdout, z_learn_holdout, old_rule_share)
            pred_holdout_new = _blend_series(z_rule_holdout, z_learn_holdout, rule_share)
            corr_holdout_before = _pearson(pred_holdout_old, z_target_holdout)
            corr_holdout_after = _pearson(pred_holdout_new, z_target_holdout)
            if require_holdout_improvement and corr_holdout_after + 1e-12 < corr_holdout_before + min_holdout_improvement:
                return {
                    "ok": False,
                    "reason": "no_holdout_improvement",
                    "samples": len(samples),
                    "holdout_samples": holdout_size,
                    "corr_holdout_before": corr_holdout_before,
                    "corr_holdout_after": corr_holdout_after,
                }

        pred_full_old = _blend_series(z_rule, z_learn, old_rule_share)
        pred_full_new = _blend_series(z_rule, z_learn, rule_share)
        corr_old = _pearson(pred_full_old, z_target)
        corr_new = _pearson(pred_full_new, z_target)

        rule_weight_new = residual * rule_share
        learn_weight_new = residual * learn_share
        hybrid_raw["rule_weight"] = round(rule_weight_new, 4)
        hybrid_raw["llm_weight"] = round(llm_weight_new, 4)
        hybrid_raw["learn_weight"] = round(learn_weight_new, 4)
        _write_raw_config(cfg_path, raw_cfg)

        by_window: dict[str, int] = {}
        for s in samples:
            by_window[s.window] = by_window.get(s.window, 0) + 1

        return {
            "ok": True,
            "samples": len(samples),
            "by_window": by_window,
            "corr_before": corr_old,
            "corr_after": corr_new,
            "corr_train_before": corr_train_before,
            "corr_train_after": corr_train_after,
            "corr_holdout_before": corr_holdout_before,
            "corr_holdout_after": corr_holdout_after,
            "weights": {
                "rule_weight": round(rule_weight_new, 4),
                "llm_weight": round(llm_weight_new, 4),
                "learn_weight": round(learn_weight_new, 4),
                "rule_share_non_llm": round(rule_share, 4),
                "learn_share_non_llm": round(learn_share, 4),
            },
            "guardrails": {
                "holdout_ratio": holdout_ratio,
                "holdout_samples": holdout_size,
                "max_share_shift": max_share_shift,
                "share_shift_applied": abs(rule_share - old_rule_share),
                "require_holdout_improvement": require_holdout_improvement,
                "min_holdout_improvement": min_holdout_improvement,
                "min_train_improvement": min_train_improvement,
            },
        }
    finally:
        storage.close()


def evaluate_hybrid_model(
    config_path: str | Path,
    db_path: str | Path,
    windows: list[str],
    lookback_days: int = 30,
    min_samples: int = 120,
) -> dict[str, object]:
    cfg_path = Path(config_path)
    cfg = load_config(cfg_path)
    learner = Learner(cfg)
    storage = Storage(db_path)
    try:
        now = _reference_now_from_storage(storage)
        since = now - timedelta(days=max(1, int(lookback_days)))
        samples = _build_hybrid_samples(storage, learner, windows, since)
        if len(samples) < max(20, int(min_samples)):
            return {
                "ok": False,
                "reason": "not_enough_samples",
                "samples": len(samples),
                "min_samples": int(min_samples),
            }

        hybrid_cfg = cfg.get("hybrid", {})
        rule_weight = float(hybrid_cfg.get("rule_weight", 0.5))
        learn_weight = float(hybrid_cfg.get("learn_weight", 0.15))
        non_llm_total = rule_weight + learn_weight
        if non_llm_total <= 0:
            rule_share = 0.5
            learn_share = 0.5
        else:
            rule_share = rule_weight / non_llm_total
            learn_share = learn_weight / non_llm_total

        global_metrics = _evaluate_prediction_metrics(samples, rule_share=rule_share)
        evaluation_cfg = cfg.get("hybrid", {}).get("evaluation", {})
        walk_forward = _walk_forward_metrics(
            samples,
            current_rule_share=rule_share,
            min_train_samples=max(20, int(evaluation_cfg.get("walk_forward_min_train_samples", 60))),
            step_samples=max(5, int(evaluation_cfg.get("walk_forward_step_samples", 25))),
            grid_step=max(0.01, float(evaluation_cfg.get("walk_forward_grid_step", 0.1))),
            trading_fee_bps=max(0.0, float(evaluation_cfg.get("trading_fee_bps", 2.0))),
        )

        by_window_counts: dict[str, int] = {}
        by_window_metrics: dict[str, dict[str, object]] = {}
        by_window_walk_forward: dict[str, dict[str, object]] = {}
        for window in sorted({s.window for s in samples}):
            win_samples = [s for s in samples if s.window == window]
            by_window_counts[window] = len(win_samples)
            by_window_metrics[window] = _evaluate_prediction_metrics(win_samples, rule_share=rule_share)
            by_window_walk_forward[window] = _walk_forward_metrics(
                win_samples,
                current_rule_share=rule_share,
                min_train_samples=max(20, int(evaluation_cfg.get("walk_forward_min_train_samples", 60))),
                step_samples=max(5, int(evaluation_cfg.get("walk_forward_step_samples", 25))),
                grid_step=max(0.01, float(evaluation_cfg.get("walk_forward_grid_step", 0.1))),
                trading_fee_bps=max(0.0, float(evaluation_cfg.get("trading_fee_bps", 2.0))),
            )

        return {
            "ok": True,
            "samples": len(samples),
            "by_window": by_window_counts,
            "weights_non_llm": {
                "rule_share": round(rule_share, 4),
                "learn_share": round(learn_share, 4),
            },
            "metrics": {
                "global": global_metrics,
                "by_window": by_window_metrics,
                "walk_forward": {
                    "global": walk_forward,
                    "by_window": by_window_walk_forward,
                },
            },
        }
    finally:
        storage.close()


def _run_attempts(config: dict[str, Any], db_path: str | Path, windows: list[str]) -> list[Attempt]:
    rater = LLMRater(config)
    out: list[Attempt] = []
    storage = Storage(db_path)
    try:
        now = utcnow()
        for window in windows:
            delta = parse_duration(window)
            minutes = int(delta.total_seconds() // 60)
            since = now - timedelta(minutes=minutes)
            items = storage.get_items_since(since)
            try:
                payload = rater.rate_window(items=items, window_minutes=minutes, now=now)
                out.append(
                    Attempt(
                        window=window,
                        success=True,
                        message="ok",
                        meta=payload.meta,
                    )
                )
            except Exception as exc:
                out.append(
                    Attempt(
                        window=window,
                        success=False,
                        message=str(exc),
                        meta={},
                    )
                )
        return out
    finally:
        storage.close()


def _build_hybrid_samples(
    storage: Storage,
    learner: Learner,
    windows: list[str],
    since,
) -> list[HybridSample]:
    out: list[HybridSample] = []
    for window in windows:
        try:
            minutes = max(1, int(parse_duration(window).total_seconds() // 60))
        except ValueError:
            continue
        horizon = min(learner.horizons_minutes, key=lambda h: abs(h - minutes))
        labeled = storage.get_labeled_items(horizon_minutes=horizon, since_ts=since)
        for _, item, target in labeled:
            out.append(
                HybridSample(
                    window=window,
                    horizon_minutes=horizon,
                    timestamp_utc=item.timestamp_utc,
                    rule_value=float(item.impact),
                    learn_value=float(learner.predict_item_return_mixed(storage, item, window_minutes=minutes)),
                    target_return=float(target),
                )
            )
    return sorted(out, key=lambda s: getattr(s, "timestamp_utc"))


def _weight_grid(step: float) -> list[float]:
    values: list[float] = []
    x = 0.0
    safe_step = max(0.001, min(0.5, float(step)))
    while x < 1.0:
        values.append(round(x, 6))
        x += safe_step
    values.append(1.0)
    return sorted(set(values))


def _blend_series(rule_series: list[float], learn_series: list[float], rule_share: float) -> list[float]:
    learn_share = 1.0 - float(rule_share)
    return [
        float(rule_share) * rule_series[i] + learn_share * learn_series[i]
        for i in range(min(len(rule_series), len(learn_series)))
    ]


def _clamp_rule_share(candidate: float, current: float, max_share_shift: float) -> float:
    shift = max(0.0, float(max_share_shift))
    low = max(0.0, float(current) - shift)
    high = min(1.0, float(current) + shift)
    return min(high, max(low, float(candidate)))


def _evaluate_prediction_metrics(samples: list[HybridSample], rule_share: float) -> dict[str, object]:
    learn_share = 1.0 - float(rule_share)
    target = [float(s.target_return) for s in samples]
    rule_pred = [float(s.rule_value) for s in samples]
    learn_pred = [float(s.learn_value) for s in samples]
    hybrid_pred = [
        float(rule_share) * rule_pred[i] + learn_share * learn_pred[i]
        for i in range(len(samples))
    ]
    return {
        "rule": _signal_metrics(rule_pred, target),
        "learn": _signal_metrics(learn_pred, target),
        "hybrid": _signal_metrics(hybrid_pred, target),
    }


def _walk_forward_metrics(
    samples: list[HybridSample],
    *,
    current_rule_share: float,
    min_train_samples: int,
    step_samples: int,
    grid_step: float,
    trading_fee_bps: float,
) -> dict[str, object]:
    n = len(samples)
    if n < max(5, int(min_train_samples) + int(step_samples)):
        return {
            "folds": 0,
            "samples_oos": 0,
            "adaptive_rule_share_mean": None,
            "adaptive_rule_share_std": None,
            "current_hybrid": _signal_metrics([], []),
            "adaptive_hybrid": _signal_metrics([], []),
            "trading": {
                "current_hybrid": _trading_metrics([], [], fee_bps=trading_fee_bps),
                "adaptive_hybrid": _trading_metrics([], [], fee_bps=trading_fee_bps),
            },
        }

    targets: list[float] = []
    pred_current: list[float] = []
    pred_adaptive: list[float] = []
    chosen_shares: list[float] = []
    folds = 0

    i = max(1, int(min_train_samples))
    step = max(1, int(step_samples))
    while i < n:
        train = samples[:i]
        test = samples[i : min(n, i + step)]
        if not test:
            break
        fitted_share = _fit_best_rule_share(train, grid_step=grid_step)
        chosen_shares.append(fitted_share)
        folds += 1

        for sample in test:
            targets.append(sample.target_return)
            pred_current.append(
                current_rule_share * sample.rule_value + (1.0 - current_rule_share) * sample.learn_value
            )
            pred_adaptive.append(
                fitted_share * sample.rule_value + (1.0 - fitted_share) * sample.learn_value
            )
        i += step

    share_mean, share_std = _mean_std(chosen_shares)
    return {
        "folds": folds,
        "samples_oos": len(targets),
        "adaptive_rule_share_mean": share_mean,
        "adaptive_rule_share_std": share_std,
        "current_hybrid": _signal_metrics(pred_current, targets),
        "adaptive_hybrid": _signal_metrics(pred_adaptive, targets),
        "trading": {
            "current_hybrid": _trading_metrics(pred_current, targets, fee_bps=trading_fee_bps),
            "adaptive_hybrid": _trading_metrics(pred_adaptive, targets, fee_bps=trading_fee_bps),
        },
    }


def _fit_best_rule_share(samples: list[HybridSample], *, grid_step: float) -> float:
    if not samples:
        return 0.5
    target = [s.target_return for s in samples]
    rule = [s.rule_value for s in samples]
    learn = [s.learn_value for s in samples]

    best = 0.5
    best_corr = -2.0
    for share in _weight_grid(max(0.01, float(grid_step))):
        pred = [share * rule[i] + (1.0 - share) * learn[i] for i in range(len(samples))]
        corr = _pearson(pred, target)
        if corr > best_corr:
            best = share
            best_corr = corr
    return best


def _mean_std(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, sqrt(max(0.0, var))


def _trading_metrics(predictions: list[float], target: list[float], *, fee_bps: float) -> dict[str, float | int | None]:
    n = min(len(predictions), len(target))
    if n <= 0:
        return {
            "n": 0,
            "active_ratio": 0.0,
            "turnover_ratio": 0.0,
            "win_rate": None,
            "avg_pnl": 0.0,
            "cum_pnl": 0.0,
            "sharpe_like": 0.0,
        }

    fee = max(0.0, float(fee_bps)) / 10_000.0
    pnl: list[float] = []
    positions: list[int] = []
    prev_pos = 0
    for i in range(n):
        pred = float(predictions[i])
        pos = 1 if pred > 0 else (-1 if pred < 0 else 0)
        gross = pos * float(target[i])
        turn = abs(pos - prev_pos)
        # Charge half-fee per side change (0->1 = 1 side, 1->-1 = 2 sides).
        cost = 0.5 * turn * fee
        pnl_i = gross - cost
        pnl.append(pnl_i)
        positions.append(pos)
        prev_pos = pos

    active = sum(1 for p in positions if p != 0)
    turnover_events = sum(1 for i in range(1, len(positions)) if positions[i] != positions[i - 1])
    wins = sum(1 for v in pnl if v > 0)
    mean = sum(pnl) / len(pnl)
    var = sum((v - mean) ** 2 for v in pnl) / len(pnl)
    std = sqrt(max(0.0, var))
    sharpe_like = (mean / std) * sqrt(len(pnl)) if std > 1e-12 else 0.0
    return {
        "n": len(pnl),
        "active_ratio": active / len(positions),
        "turnover_ratio": turnover_events / max(1, len(positions) - 1),
        "win_rate": wins / len(pnl),
        "avg_pnl": mean,
        "cum_pnl": sum(pnl),
        "sharpe_like": sharpe_like,
    }


def _signal_metrics(predictions: list[float], target: list[float]) -> dict[str, float | None]:
    corr = _pearson(predictions, target)
    directional = _directional_accuracy(predictions, target)
    mae = _mean_abs_error(predictions, target)
    return {
        "corr": corr,
        "directional_accuracy": directional,
        "mae": mae,
    }


def _directional_accuracy(predictions: list[float], target: list[float]) -> float | None:
    n = min(len(predictions), len(target))
    usable = [
        (predictions[i], target[i])
        for i in range(n)
        if abs(predictions[i]) > 1e-12 and abs(target[i]) > 1e-12
    ]
    if not usable:
        return None
    hits = sum(1 for p, t in usable if (p > 0 and t > 0) or (p < 0 and t < 0))
    return hits / len(usable)


def _mean_abs_error(predictions: list[float], target: list[float]) -> float:
    n = min(len(predictions), len(target))
    if n <= 0:
        return 0.0
    return sum(abs(predictions[i] - target[i]) for i in range(n)) / n


def _zscore(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = sqrt(var)
    if std <= 1e-12:
        return [0.0 for _ in values]
    return [(v - mean) / std for v in values]


def _pearson(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    if n <= 1:
        return 0.0
    sx = sum(x[:n])
    sy = sum(y[:n])
    sxx = sum(v * v for v in x[:n])
    syy = sum(v * v for v in y[:n])
    sxy = sum(x[i] * y[i] for i in range(n))
    num = n * sxy - sx * sy
    den_x = n * sxx - sx * sx
    den_y = n * syy - sy * sy
    den = sqrt(max(0.0, den_x * den_y))
    if den <= 1e-12:
        return 0.0
    return num / den


def _is_constant(values: list[float]) -> bool:
    if not values:
        return True
    v0 = values[0]
    return all(abs(v - v0) <= 1e-12 for v in values)


def _apply_tuning_step(llm_cfg: dict[str, Any], failures: list[Attempt]) -> None:
    msg = " | ".join(f.message.lower() for f in failures)
    max_items = int(llm_cfg.get("max_items", 6))
    snippet_chars = int(llm_cfg.get("snippet_chars", 280))
    timeout_seconds = int(llm_cfg.get("timeout_seconds", 120))
    retries = int(llm_cfg.get("request_retries", 1))
    cooldown = int(llm_cfg.get("request_cooldown_seconds", 600))
    max_events = int(llm_cfg.get("max_events", 5))
    model = str(llm_cfg.get("model", "gpt-4.1-mini"))
    fallback = str(llm_cfg.get("fallback_model", "gpt-5-mini"))

    # Prefer faster/smaller requests first.
    llm_cfg["max_items"] = max(2, max_items - 1)
    llm_cfg["snippet_chars"] = max(120, snippet_chars - 40)
    llm_cfg["max_events"] = max(3, max_events - 1)
    llm_cfg["timeout_seconds"] = min(180, timeout_seconds + 15)
    llm_cfg["request_retries"] = min(2, retries + 1)
    llm_cfg["request_cooldown_seconds"] = min(1800, cooldown + 120)

    # If model often returns no text/invalid JSON, prioritize fallback model.
    if "no output text" in msg or "invalid json" in msg:
        if fallback and fallback != model:
            llm_cfg["model"] = fallback
            llm_cfg["fallback_model"] = model


def _read_raw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_raw_config(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)
