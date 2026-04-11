from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from typing import Any

from btc_news_arrow.benchmark import run_benchmark
from btc_news_arrow.optimizer import evaluate_hybrid_model


DEFAULT_BASELINE_PATH = Path("tools/regression_baseline.json")


def load_regression_baseline(path: str | Path = DEFAULT_BASELINE_PATH) -> dict[str, Any]:
    baseline_path = Path(path)
    if not baseline_path.exists():
        raise FileNotFoundError(f"Regression baseline not found: {baseline_path}")
    with baseline_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Regression baseline must be a JSON object")
    return data


def run_regression_check(
    *,
    config_path: str | Path,
    db_path: str | Path,
    baseline_path: str | Path = DEFAULT_BASELINE_PATH,
    bench_db_path: str | Path = "btc_news_arrow_regression_bench.db",
    dedupe_existing: int | None = None,
    dedupe_probes: int | None = None,
    label_rows: int | None = None,
    label_batch_size: int | None = None,
    perf_floor_ratio: float = 0.7,
    check_hybrid_eval: bool = False,
    hybrid_windows: list[str] | None = None,
    hybrid_lookback_days: int = 30,
    hybrid_min_samples: int = 120,
    require_hybrid_samples: bool = False,
) -> dict[str, Any]:
    baseline = load_regression_baseline(path=baseline_path)
    ratio = max(0.0, min(1.0, float(perf_floor_ratio)))
    checks: list[dict[str, Any]] = []
    profile = baseline.get("benchmark_profile", {})
    dedupe_existing_resolved = _resolve_int_setting(dedupe_existing, profile.get("dedupe_existing"), 1500, minimum=50)
    dedupe_probes_resolved = _resolve_int_setting(dedupe_probes, profile.get("dedupe_probes"), 800, minimum=50)
    label_rows_resolved = _resolve_int_setting(label_rows, profile.get("label_rows"), 6000, minimum=100)
    label_batch_size_resolved = _resolve_int_setting(label_batch_size, profile.get("label_batch_size"), 500, minimum=1)

    bench = run_benchmark(
        db_path=bench_db_path,
        dedupe_existing=dedupe_existing_resolved,
        dedupe_probes=dedupe_probes_resolved,
        label_rows=label_rows_resolved,
        label_batch_size=label_batch_size_resolved,
        keep_db=False,
        overwrite_db=True,
    )

    perf_baseline = baseline.get("performance", {})
    _append_perf_check(
        checks,
        name="dedupe.probes_per_second",
        measured=_read_metric(bench, ["dedupe", "probes_per_second"]),
        baseline=perf_baseline.get("dedupe_probes_per_second"),
        ratio=ratio,
    )
    _append_perf_check(
        checks,
        name="labels.batch_rows_per_second",
        measured=_read_metric(bench, ["labels", "batch_rows_per_second"]),
        baseline=perf_baseline.get("labels_batch_rows_per_second"),
        ratio=ratio,
    )
    _append_perf_check(
        checks,
        name="labels.speedup_vs_single_row",
        measured=_read_metric(bench, ["labels", "speedup_vs_single_row"]),
        baseline=perf_baseline.get("labels_speedup_vs_single_row"),
        ratio=ratio,
    )

    hybrid_summary = None
    hybrid_windows_resolved = hybrid_windows or ["1h", "24h"]
    if check_hybrid_eval:
        hybrid_summary = evaluate_hybrid_model(
            config_path=config_path,
            db_path=db_path,
            windows=hybrid_windows_resolved,
            lookback_days=max(1, int(hybrid_lookback_days)),
            min_samples=max(20, int(hybrid_min_samples)),
        )
        _append_hybrid_checks(
            checks=checks,
            hybrid_summary=hybrid_summary,
            baseline=baseline.get("hybrid_eval", {}),
            require_hybrid_samples=require_hybrid_samples,
        )

    failed = [c for c in checks if c.get("status") == "fail"]
    return {
        "ok": len(failed) == 0,
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "checks": checks,
        "performance": bench,
        "hybrid_eval": hybrid_summary,
        "baseline_path": str(Path(baseline_path)),
        "perf_floor_ratio": ratio,
        "benchmark_profile_used": {
            "dedupe_existing": dedupe_existing_resolved,
            "dedupe_probes": dedupe_probes_resolved,
            "label_rows": label_rows_resolved,
            "label_batch_size": label_batch_size_resolved,
        },
    }


def calibrate_regression_baseline(
    *,
    config_path: str | Path,
    db_path: str | Path,
    output_path: str | Path = DEFAULT_BASELINE_PATH,
    runs: int = 5,
    dedupe_existing: int = 1500,
    dedupe_probes: int = 800,
    label_rows: int = 6000,
    label_batch_size: int = 500,
    include_hybrid_eval: bool = False,
    hybrid_windows: list[str] | None = None,
    hybrid_lookback_days: int = 30,
    hybrid_min_samples: int = 120,
    hybrid_corr_margin: float = 0.05,
    hybrid_directional_margin: float = 0.05,
    overwrite: bool = False,
) -> dict[str, Any]:
    output = Path(output_path)
    if output.exists() and not overwrite:
        raise FileExistsError(
            f"Baseline file already exists: {output}. "
            "Use overwrite to replace it."
        )

    runs_n = max(1, int(runs))
    per_run: list[dict[str, float]] = []
    dedupe_rates: list[float] = []
    batch_rates: list[float] = []
    speedups: list[float] = []

    with TemporaryDirectory(prefix="btcnews_reg_cal_") as tmpdir:
        tmp_root = Path(tmpdir)
        for idx in range(runs_n):
            bench = run_benchmark(
                db_path=tmp_root / f"bench_{idx}.db",
                dedupe_existing=max(50, int(dedupe_existing)),
                dedupe_probes=max(50, int(dedupe_probes)),
                label_rows=max(100, int(label_rows)),
                label_batch_size=max(1, int(label_batch_size)),
                keep_db=False,
                overwrite_db=True,
            )
            dedupe_rate = float(_read_metric(bench, ["dedupe", "probes_per_second"]) or 0.0)
            batch_rate = float(_read_metric(bench, ["labels", "batch_rows_per_second"]) or 0.0)
            speedup = float(_read_metric(bench, ["labels", "speedup_vs_single_row"]) or 0.0)
            dedupe_rates.append(dedupe_rate)
            batch_rates.append(batch_rate)
            speedups.append(speedup)
            per_run.append(
                {
                    "run": float(idx + 1),
                    "dedupe_probes_per_second": dedupe_rate,
                    "labels_batch_rows_per_second": batch_rate,
                    "labels_speedup_vs_single_row": speedup,
                }
            )

    dedupe_existing_resolved = max(50, int(dedupe_existing))
    dedupe_probes_resolved = max(50, int(dedupe_probes))
    label_rows_resolved = max(100, int(label_rows))
    label_batch_size_resolved = max(1, int(label_batch_size))

    baseline: dict[str, Any] = {
        "benchmark_profile": {
            "dedupe_existing": dedupe_existing_resolved,
            "dedupe_probes": dedupe_probes_resolved,
            "label_rows": label_rows_resolved,
            "label_batch_size": label_batch_size_resolved,
        },
        "performance": {
            "dedupe_probes_per_second": float(median(dedupe_rates)),
            "labels_batch_rows_per_second": float(median(batch_rates)),
            "labels_speedup_vs_single_row": float(median(speedups)),
        },
        "hybrid_eval": {
            "hybrid_corr_min": -1.0,
            "hybrid_directional_accuracy_min": 0.0,
        },
    }

    hybrid_summary = None
    if include_hybrid_eval:
        hybrid_summary = evaluate_hybrid_model(
            config_path=config_path,
            db_path=db_path,
            windows=hybrid_windows or ["1h", "24h"],
            lookback_days=max(1, int(hybrid_lookback_days)),
            min_samples=max(20, int(hybrid_min_samples)),
        )
        if hybrid_summary.get("ok"):
            metrics = ((hybrid_summary.get("metrics") or {}).get("global") or {}).get("hybrid") or {}
            corr = _read_metric(metrics, ["corr"])
            dir_acc = _read_metric(metrics, ["directional_accuracy"])
            if corr is not None:
                baseline["hybrid_eval"]["hybrid_corr_min"] = max(
                    -1.0,
                    min(1.0, float(corr) - max(0.0, float(hybrid_corr_margin))),
                )
            if dir_acc is not None:
                baseline["hybrid_eval"]["hybrid_directional_accuracy_min"] = max(
                    0.0,
                    min(1.0, float(dir_acc) - max(0.0, float(hybrid_directional_margin))),
                )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, sort_keys=False)
        f.write("\n")

    return {
        "ok": True,
        "output_path": str(output),
        "runs": runs_n,
        "per_run": per_run,
        "baseline": baseline,
        "hybrid_eval": hybrid_summary,
    }


def format_regression_summary(summary: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(summary, indent=2, sort_keys=False)

    lines = [
        f"Regression check: ok={summary.get('ok')} failed={summary.get('checks_failed')}/{summary.get('checks_total')}",
        f"Baseline: {summary.get('baseline_path')} | perf_floor_ratio={summary.get('perf_floor_ratio')}",
        f"Benchmark profile: {summary.get('benchmark_profile_used')}",
    ]
    for check in summary.get("checks", []):
        lines.append(
            f"- [{check.get('status')}] {check.get('name')}: "
            f"measured={check.get('measured')} "
            f"required={check.get('required')} "
            f"note={check.get('note')}"
        )
    return "\n".join(lines)


def format_calibration_summary(summary: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(summary, indent=2, sort_keys=False)

    baseline = summary.get("baseline", {})
    perf = baseline.get("performance", {})
    hybrid = baseline.get("hybrid_eval", {})
    return "\n".join(
        [
            f"Baseline calibration: output={summary.get('output_path')} runs={summary.get('runs')}",
            (
                "Performance baseline: "
                f"dedupe_probes_per_second={perf.get('dedupe_probes_per_second')} "
                f"labels_batch_rows_per_second={perf.get('labels_batch_rows_per_second')} "
                f"labels_speedup_vs_single_row={perf.get('labels_speedup_vs_single_row')}"
            ),
            (
                "Hybrid baseline: "
                f"hybrid_corr_min={hybrid.get('hybrid_corr_min')} "
                f"hybrid_directional_accuracy_min={hybrid.get('hybrid_directional_accuracy_min')}"
            ),
        ]
    )


def _append_perf_check(
    checks: list[dict[str, Any]],
    *,
    name: str,
    measured: float | None,
    baseline: object,
    ratio: float,
) -> None:
    baseline_value = _read_positive_float(baseline)
    if baseline_value is None:
        checks.append(
            {
                "name": name,
                "status": "fail",
                "measured": measured,
                "required": "positive baseline metric",
                "note": "invalid or missing baseline metric",
            }
        )
        return
    required = baseline_value * ratio
    if measured is None:
        checks.append(
            {
                "name": name,
                "status": "fail",
                "measured": None,
                "required": required,
                "note": "missing metric",
            }
        )
        return
    status = "pass" if measured + 1e-12 >= required else "fail"
    checks.append(
        {
            "name": name,
            "status": status,
            "measured": measured,
            "required": required,
            "note": "performance floor check",
        }
    )


def _append_hybrid_checks(
    *,
    checks: list[dict[str, Any]],
    hybrid_summary: dict[str, Any],
    baseline: dict[str, Any],
    require_hybrid_samples: bool,
) -> None:
    if not hybrid_summary.get("ok"):
        reason = str(hybrid_summary.get("reason"))
        if reason == "not_enough_samples" and not require_hybrid_samples:
            checks.append(
                {
                    "name": "hybrid_eval.samples",
                    "status": "skip",
                    "measured": hybrid_summary.get("samples"),
                    "required": hybrid_summary.get("min_samples"),
                    "note": "hybrid eval skipped due to insufficient labeled samples",
                }
            )
            return
        checks.append(
            {
                "name": "hybrid_eval.execution",
                "status": "fail",
                "measured": hybrid_summary.get("reason"),
                "required": "ok",
                "note": "hybrid evaluation failed",
            }
        )
        return

    metrics = ((hybrid_summary.get("metrics") or {}).get("global") or {})
    hybrid_corr = _read_metric(metrics, ["hybrid", "corr"])
    hybrid_dir = _read_metric(metrics, ["hybrid", "directional_accuracy"])
    min_corr = float(baseline.get("hybrid_corr_min", -1.0))
    min_dir = float(baseline.get("hybrid_directional_accuracy_min", 0.0))

    checks.append(
        {
            "name": "hybrid_eval.hybrid_corr",
            "status": "pass" if hybrid_corr is not None and hybrid_corr + 1e-12 >= min_corr else "fail",
            "measured": hybrid_corr,
            "required": min_corr,
            "note": "hybrid correlation floor",
        }
    )
    checks.append(
        {
            "name": "hybrid_eval.hybrid_directional_accuracy",
            "status": "pass" if hybrid_dir is not None and hybrid_dir + 1e-12 >= min_dir else "fail",
            "measured": hybrid_dir,
            "required": min_dir,
            "note": "hybrid directional accuracy floor",
        }
    )


def _read_metric(payload: dict[str, Any], keys: list[str]) -> float | None:
    cur: Any = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    try:
        if cur is None:
            return None
        return float(cur)
    except (TypeError, ValueError):
        return None


def _resolve_int_setting(
    explicit: int | None,
    profile_value: object,
    fallback: int,
    *,
    minimum: int,
) -> int:
    if explicit is not None:
        return max(minimum, int(explicit))
    try:
        if profile_value is not None:
            return max(minimum, int(profile_value))
    except (TypeError, ValueError):
        pass
    return max(minimum, int(fallback))


def _read_positive_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if out <= 0.0:
            return None
        return out
    except (TypeError, ValueError):
        return None
