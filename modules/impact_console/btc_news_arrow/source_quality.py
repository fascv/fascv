from __future__ import annotations

import json
from datetime import datetime, timedelta
from math import sqrt
from pathlib import Path
from typing import Any

from btc_news_arrow.config import load_config
from btc_news_arrow.learner import Learner
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import ensure_utc, parse_duration, utcnow


DEFAULT_REPORT_PATH = Path("diagnostics/source_quality_latest.json")
DEFAULT_HISTORY_DIR = Path("diagnostics/source_quality_history")
EPS = 1e-12


def generate_source_quality_report(
    *,
    config_path: str | Path,
    db_path: str | Path,
    windows: list[str] | None = None,
    lookback_days: int = 30,
    min_samples_per_source: int = 8,
    top_n: int = 25,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    history_dir: str | Path = DEFAULT_HISTORY_DIR,
    keep_history: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    run_now = ensure_utc(now or utcnow())
    windows_resolved = [w.strip() for w in (windows or ["1h", "24h"]) if w.strip()]
    if not windows_resolved:
        windows_resolved = ["1h", "24h"]

    cfg = load_config(config_path)
    learner = Learner(cfg)
    storage = Storage(db_path)
    try:
        since = run_now - timedelta(days=max(1, int(lookback_days)))
        by_window: dict[str, Any] = {}
        total_samples = 0
        source_union: set[str] = set()

        for window in windows_resolved:
            try:
                minutes = max(1, int(parse_duration(window).total_seconds() // 60))
            except ValueError:
                continue
            horizon = min(learner.horizons_minutes, key=lambda h: abs(h - minutes))
            labeled = storage.get_labeled_items(horizon_minutes=horizon, since_ts=since)
            source_rows: dict[str, list[tuple[float, float]]] = {}
            all_pred: list[float] = []
            all_target: list[float] = []

            for _, item, target in labeled:
                source_rows.setdefault(item.source, []).append((float(item.impact), float(target)))
                all_pred.append(float(item.impact))
                all_target.append(float(target))

            sources_out: list[dict[str, Any]] = []
            for source, rows in source_rows.items():
                source_union.add(source)
                if len(rows) < max(1, int(min_samples_per_source)):
                    continue
                pred = [r[0] for r in rows]
                target = [r[1] for r in rows]
                metrics = _signal_metrics(pred, target)
                sources_out.append(
                    {
                        "source": source,
                        "samples": len(rows),
                        "impact_mean": _mean(pred),
                        "impact_abs_mean": _mean_abs(pred),
                        "target_mean": _mean(target),
                        "target_abs_mean": _mean_abs(target),
                        "corr": metrics["corr"],
                        "directional_accuracy": metrics["directional_accuracy"],
                        "mae": metrics["mae"],
                    }
                )

            sources_out = sorted(
                sources_out,
                key=lambda r: (-int(r.get("samples") or 0), -abs(float(r.get("corr") or 0.0))),
            )[: max(1, int(top_n))]
            total_samples += len(labeled)

            by_window[window] = {
                "requested_window": window,
                "window_minutes": minutes,
                "horizon_minutes": horizon,
                "samples_total": len(labeled),
                "sources_total": len(source_rows),
                "sources_with_min_samples": len(sources_out),
                "global": _signal_metrics(all_pred, all_target),
                "sources": sources_out,
            }
    finally:
        storage.close()

    current = {
        "generated_at_utc": run_now.isoformat(),
        "windows": windows_resolved,
        "lookback_days": max(1, int(lookback_days)),
        "min_samples_per_source": max(1, int(min_samples_per_source)),
        "top_n": max(1, int(top_n)),
        "overall": {
            "samples_total": total_samples,
            "sources_total": len(source_union),
        },
        "by_window": by_window,
    }

    report_file = Path(report_path)
    previous = _load_previous_current(report_file)
    delta = _build_delta(previous=previous, current=current)
    trend = _build_trend(delta)
    report = {
        "generated_at_utc": run_now.isoformat(),
        "current": current,
        "previous": previous,
        "delta": delta,
        "trend": trend,
    }
    _write_json(report_file, report)

    if keep_history:
        ts_slug = run_now.strftime("%Y%m%dT%H%M%SZ")
        _write_json(Path(history_dir) / f"source_quality_{ts_slug}.json", report)

    return report


def format_source_quality_summary(report: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report, indent=2, sort_keys=False)

    current = report.get("current") or {}
    trend = report.get("trend") or {}
    overall = current.get("overall") or {}
    lines = [
        (
            "Source quality report: "
            f"trend={trend.get('status')} "
            f"samples={overall.get('samples_total')} "
            f"sources={overall.get('sources_total')}"
        ),
        (
            f"Generated: {report.get('generated_at_utc')} "
            f"windows={current.get('windows')} "
            f"lookback_days={current.get('lookback_days')}"
        ),
    ]

    by_window = current.get("by_window") or {}
    for window in sorted(by_window.keys()):
        payload = by_window.get(window) or {}
        glob = payload.get("global") or {}
        lines.append(
            (
                f"- {window}: samples={payload.get('samples_total')} "
                f"corr={_fmt_num(glob.get('corr'))} "
                f"dir_acc={_fmt_num(glob.get('directional_accuracy'))} "
                f"mae={_fmt_num(glob.get('mae'))}"
            )
        )
        top_sources = payload.get("sources") or []
        for row in top_sources[:5]:
            lines.append(
                (
                    f"  {row.get('source')}: n={row.get('samples')} "
                    f"corr={_fmt_num(row.get('corr'))} "
                    f"dir_acc={_fmt_num(row.get('directional_accuracy'))}"
                )
            )
    return "\n".join(lines)


def _build_delta(*, previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    has_previous = isinstance(previous, dict)
    out: dict[str, Any] = {
        "overall_samples_delta": (
            _safe_int((current.get("overall") or {}).get("samples_total"))
            - _safe_int(((previous or {}).get("overall") or {}).get("samples_total"))
        )
        if has_previous
        else None,
        "by_window": {},
    }
    cur_windows = set((current.get("by_window") or {}).keys())
    prev_windows = set(((previous or {}).get("by_window") or {}).keys())
    for window in sorted(cur_windows | prev_windows):
        cur_w = (current.get("by_window") or {}).get(window) or {}
        prev_w = ((previous or {}).get("by_window") or {}).get(window) or {}
        if not has_previous:
            out["by_window"][window] = {
                "samples_delta": None,
                "global_corr_delta": None,
                "global_dir_acc_delta": None,
                "global_mae_delta": None,
            }
            continue
        out["by_window"][window] = {
            "samples_delta": _safe_int(cur_w.get("samples_total")) - _safe_int(prev_w.get("samples_total")),
            "global_corr_delta": _delta_float((prev_w.get("global") or {}).get("corr"), (cur_w.get("global") or {}).get("corr")),
            "global_dir_acc_delta": _delta_float(
                (prev_w.get("global") or {}).get("directional_accuracy"),
                (cur_w.get("global") or {}).get("directional_accuracy"),
            ),
            "global_mae_delta": _delta_float((prev_w.get("global") or {}).get("mae"), (cur_w.get("global") or {}).get("mae")),
        }
    return out


def _build_trend(delta: dict[str, Any]) -> dict[str, Any]:
    corr_deltas: list[float] = []
    mae_deltas: list[float] = []
    for payload in (delta.get("by_window") or {}).values():
        corr = payload.get("global_corr_delta")
        mae = payload.get("global_mae_delta")
        if corr is not None:
            corr_deltas.append(float(corr))
        if mae is not None:
            mae_deltas.append(float(mae))

    if not corr_deltas and not mae_deltas:
        return {"status": "unavailable", "avg_global_corr_delta": None, "avg_global_mae_delta": None}
    avg_corr = sum(corr_deltas) / len(corr_deltas) if corr_deltas else 0.0
    avg_mae = sum(mae_deltas) / len(mae_deltas) if mae_deltas else 0.0

    improved_votes = 0
    degraded_votes = 0
    if avg_corr > 0.005:
        improved_votes += 1
    elif avg_corr < -0.005:
        degraded_votes += 1
    if avg_mae < -0.0001:
        improved_votes += 1
    elif avg_mae > 0.0001:
        degraded_votes += 1

    if improved_votes > degraded_votes:
        status = "improved"
    elif degraded_votes > improved_votes:
        status = "degraded"
    elif improved_votes == 0 and degraded_votes == 0:
        status = "flat"
    else:
        status = "mixed"
    return {
        "status": status,
        "avg_global_corr_delta": avg_corr,
        "avg_global_mae_delta": avg_mae,
    }


def _signal_metrics(predictions: list[float], target: list[float]) -> dict[str, float | None]:
    return {
        "corr": _pearson(predictions, target),
        "directional_accuracy": _directional_accuracy(predictions, target),
        "mae": _mean_abs_error(predictions, target),
    }


def _directional_accuracy(predictions: list[float], target: list[float]) -> float | None:
    n = min(len(predictions), len(target))
    usable = [
        (predictions[i], target[i])
        for i in range(n)
        if abs(predictions[i]) > EPS and abs(target[i]) > EPS
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
    if den <= EPS:
        return 0.0
    return num / den


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _mean_abs(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(abs(v) for v in values) / len(values)


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _delta_float(previous: object, current: object) -> float | None:
    if previous is None or current is None:
        return None
    return _safe_float(current) - _safe_float(previous)


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
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "None"
