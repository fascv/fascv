from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from btc_news_arrow.optimizer import evaluate_hybrid_model
from btc_news_arrow.utils import utcnow


DEFAULT_REPORT_PATH = Path("diagnostics/hybrid_eval_latest.json")
DEFAULT_HISTORY_DIR = Path("diagnostics/hybrid_eval_history")
EPS = 1e-12


def generate_hybrid_eval_report(
    *,
    config_path: str | Path,
    db_path: str | Path,
    windows: list[str] | None = None,
    lookback_days: int = 30,
    min_samples: int = 120,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    history_dir: str | Path = DEFAULT_HISTORY_DIR,
    keep_history: bool = True,
) -> dict[str, Any]:
    windows_resolved = [w.strip() for w in (windows or ["1h", "24h"]) if w.strip()]
    if not windows_resolved:
        windows_resolved = ["1h", "24h"]

    summary = evaluate_hybrid_model(
        config_path=config_path,
        db_path=db_path,
        windows=windows_resolved,
        lookback_days=max(1, int(lookback_days)),
        min_samples=max(20, int(min_samples)),
    )

    now_iso = utcnow().isoformat()
    current = {
        "generated_at_utc": now_iso,
        "windows": windows_resolved,
        "lookback_days": max(1, int(lookback_days)),
        "min_samples": max(20, int(min_samples)),
        "ok": bool(summary.get("ok")),
        "samples": int(summary.get("samples") or 0),
        "reason": summary.get("reason"),
        "weights_non_llm": summary.get("weights_non_llm"),
        "metrics": summary.get("metrics") or {},
        "by_window": summary.get("by_window") or {},
    }

    report_file = Path(report_path)
    previous = _load_previous_current(report_file)
    delta = _build_delta(previous=previous, current=current)
    trend = _build_trend(delta)

    report = {
        "generated_at_utc": now_iso,
        "current": current,
        "previous": previous,
        "delta": delta,
        "trend": trend,
        "summary": summary,
    }
    _write_json(report_file, report)

    if keep_history:
        history_root = Path(history_dir)
        ts_slug = now_iso.replace("-", "").replace(":", "").replace("+00:00", "Z")
        _write_json(history_root / f"hybrid_eval_{ts_slug}.json", report)

    return report


def format_hybrid_report_summary(report: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report, indent=2, sort_keys=False)

    current = report.get("current") or {}
    trend = report.get("trend") or {}
    lines = [
        (
            "Hybrid report: "
            f"ok={current.get('ok')} samples={current.get('samples')} "
            f"trend={trend.get('status')}"
        ),
        (
            "Generated: "
            f"{report.get('generated_at_utc')} "
            f"windows={current.get('windows')} "
            f"lookback_days={current.get('lookback_days')}"
        ),
    ]
    if not current.get("ok"):
        lines.append(f"Reason: {current.get('reason')}")

    global_delta = (report.get("delta") or {}).get("global") or {}
    for metric_key in ("hybrid_corr", "hybrid_directional_accuracy", "hybrid_mae"):
        d = global_delta.get(metric_key) or {}
        lines.append(
            (
                f"- {metric_key}: prev={_fmt_num(d.get('previous'))} "
                f"curr={_fmt_num(d.get('current'))} "
                f"delta={_fmt_signed(d.get('delta'))} "
                f"trend={d.get('trend')}"
            )
        )
    return "\n".join(lines)


def _build_delta(*, previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    prev_ok = bool(previous and previous.get("ok"))
    curr_ok = bool(current.get("ok"))
    out: dict[str, Any] = {
        "ok_transition": {
            "previous_ok": prev_ok if previous is not None else None,
            "current_ok": curr_ok,
        },
        "global": _compare_scope(previous, current, window=None),
        "by_window": {},
    }

    windows = sorted(set((current.get("metrics") or {}).get("by_window", {}).keys()))
    prev_windows = sorted(set(((previous or {}).get("metrics") or {}).get("by_window", {}).keys()))
    for window in sorted(set(windows + prev_windows)):
        out["by_window"][window] = _compare_scope(previous, current, window=window)

    return out


def _compare_scope(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    window: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric_key in ("hybrid_corr", "hybrid_directional_accuracy", "hybrid_mae"):
        prev_val = _extract_metric(previous, metric_key, window=window)
        cur_val = _extract_metric(current, metric_key, window=window)
        delta = None
        trend = "unavailable"
        effect = "flat"
        if prev_val is not None and cur_val is not None:
            delta = cur_val - prev_val
            if abs(delta) <= EPS:
                trend = "flat"
                effect = "flat"
            elif metric_key == "hybrid_mae":
                trend = "improved" if delta < 0 else "degraded"
                effect = "down" if delta < 0 else "up"
            else:
                trend = "improved" if delta > 0 else "degraded"
                effect = "up" if delta > 0 else "down"
        out[metric_key] = {
            "previous": prev_val,
            "current": cur_val,
            "delta": delta,
            "trend": trend,
            "effect": effect,
        }
    return out


def _extract_metric(
    payload: dict[str, Any] | None,
    metric_key: str,
    *,
    window: str | None,
) -> float | None:
    if not isinstance(payload, dict):
        return None
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return None
    if window is None:
        hybrid = ((metrics.get("global") or {}).get("hybrid") or {})
    else:
        by_window = metrics.get("by_window")
        if not isinstance(by_window, dict):
            return None
        hybrid = ((by_window.get(window) or {}).get("hybrid") or {})
    if not isinstance(hybrid, dict):
        return None

    map_key = {
        "hybrid_corr": "corr",
        "hybrid_directional_accuracy": "directional_accuracy",
        "hybrid_mae": "mae",
    }.get(metric_key, metric_key)
    try:
        value = hybrid.get(map_key)
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_trend(delta: dict[str, Any]) -> dict[str, Any]:
    improved = 0
    degraded = 0
    flat = 0
    unavailable = 0

    def consume(scope: dict[str, Any]) -> None:
        nonlocal improved, degraded, flat, unavailable
        for metric in scope.values():
            trend = (metric or {}).get("trend")
            if trend == "improved":
                improved += 1
            elif trend == "degraded":
                degraded += 1
            elif trend == "flat":
                flat += 1
            else:
                unavailable += 1

    consume(delta.get("global") or {})
    for scope in (delta.get("by_window") or {}).values():
        consume(scope or {})

    if improved == 0 and degraded == 0:
        status = "flat" if flat > 0 else "unavailable"
    elif improved > 0 and degraded == 0:
        status = "improved"
    elif degraded > 0 and improved == 0:
        status = "degraded"
    else:
        status = "mixed"

    return {
        "status": status,
        "counts": {
            "improved": improved,
            "degraded": degraded,
            "flat": flat,
            "unavailable": unavailable,
        },
    }


def _load_previous_current(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    cur = payload.get("current")
    if not isinstance(cur, dict):
        return None
    return cur


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")


def _fmt_num(value: object) -> str:
    try:
        if value is None:
            return "None"
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return "None"


def _fmt_signed(value: object) -> str:
    try:
        if value is None:
            return "None"
        return f"{float(value):+.6f}"
    except (TypeError, ValueError):
        return "None"
