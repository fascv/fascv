import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from btc_news_arrow.models import NewsItem
from btc_news_arrow.regression import calibrate_regression_baseline, run_regression_check
from btc_news_arrow.storage import Storage


def _write_cfg(path):
    path.write_text(
        yaml.safe_dump(
            {
                "trigger_multipliers": {},
                "learning": {
                    "horizons": ["60m"],
                    "market": {"endpoint": "https://api.binance.com", "symbol": "BTCUSDT"},
                    "model": {"confidence_k": 0.0},
                    "arrow": {"scale": 100.0, "thresholds": {"60m": 0.35, "default": 0.35}},
                },
                "hybrid": {
                    "rule_weight": 0.4,
                    "llm_weight": 0.0,
                    "learn_weight": 0.6,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )


def _write_baseline(path, dedupe, batch_rows, speedup):
    path.write_text(
        json.dumps(
            {
                "performance": {
                    "dedupe_probes_per_second": dedupe,
                    "labels_batch_rows_per_second": batch_rows,
                    "labels_speedup_vs_single_row": speedup,
                },
                "hybrid_eval": {
                    "hybrid_corr_min": -1.0,
                    "hybrid_directional_accuracy_min": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )


def test_regression_check_passes_with_reasonable_baseline(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    db = tmp_path / "db.sqlite"
    baseline = tmp_path / "baseline.json"
    bench_db = tmp_path / "bench.sqlite"
    _write_cfg(cfg)
    _write_baseline(baseline, dedupe=10.0, batch_rows=100.0, speedup=1.0)

    summary = run_regression_check(
        config_path=cfg,
        db_path=db,
        baseline_path=baseline,
        bench_db_path=bench_db,
        dedupe_existing=120,
        dedupe_probes=80,
        label_rows=300,
        label_batch_size=50,
        perf_floor_ratio=0.7,
        check_hybrid_eval=False,
    )

    assert summary["ok"] is True
    assert summary["checks_failed"] == 0


def test_regression_check_fails_with_too_strict_baseline(tmp_path):
    cfg = tmp_path / "cfg_strict.yaml"
    db = tmp_path / "db_strict.sqlite"
    baseline = tmp_path / "baseline_strict.json"
    bench_db = tmp_path / "bench_strict.sqlite"
    _write_cfg(cfg)
    _write_baseline(baseline, dedupe=1_000_000.0, batch_rows=1_000_000.0, speedup=1000.0)

    summary = run_regression_check(
        config_path=cfg,
        db_path=db,
        baseline_path=baseline,
        bench_db_path=bench_db,
        dedupe_existing=120,
        dedupe_probes=80,
        label_rows=300,
        label_batch_size=50,
        perf_floor_ratio=1.0,
        check_hybrid_eval=False,
    )

    assert summary["ok"] is False
    assert summary["checks_failed"] >= 1


def test_regression_check_hybrid_eval_skips_without_samples_by_default(tmp_path):
    cfg = tmp_path / "cfg_hybrid_skip.yaml"
    db = tmp_path / "db_hybrid_skip.sqlite"
    baseline = tmp_path / "baseline_hybrid_skip.json"
    bench_db = tmp_path / "bench_hybrid_skip.sqlite"
    _write_cfg(cfg)
    _write_baseline(baseline, dedupe=10.0, batch_rows=100.0, speedup=1.0)

    storage = Storage(db)
    try:
        now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
        item = NewsItem(
            timestamp_utc=now - timedelta(hours=2),
            source="src_pos",
            title="few",
            summary="",
            category="other",
            polarity=0,
            impact=0.2,
            guid="few-g-1",
            url="https://example.com/few-g-1",
        )
        storage.insert_items([item])
        item_id, _ = storage.get_items_with_ids()[0]
        storage.upsert_item_label(item_id=item_id, horizon_minutes=60, return_value=0.01)
        storage.apply_feature_updates({("source:src_pos", 60): (10, 0.1)})
    finally:
        storage.close()

    summary = run_regression_check(
        config_path=cfg,
        db_path=db,
        baseline_path=baseline,
        bench_db_path=bench_db,
        dedupe_existing=120,
        dedupe_probes=80,
        label_rows=300,
        label_batch_size=50,
        perf_floor_ratio=0.7,
        check_hybrid_eval=True,
        require_hybrid_samples=False,
        hybrid_windows=["1h"],
        hybrid_min_samples=40,
    )

    assert summary["ok"] is True
    assert any(c["status"] == "skip" and c["name"] == "hybrid_eval.samples" for c in summary["checks"])


def test_regression_check_uses_baseline_benchmark_profile_by_default(tmp_path):
    cfg = tmp_path / "cfg_profile.yaml"
    db = tmp_path / "db_profile.sqlite"
    baseline = tmp_path / "baseline_profile.json"
    bench_db = tmp_path / "bench_profile.sqlite"
    _write_cfg(cfg)
    baseline.write_text(
        json.dumps(
            {
                "benchmark_profile": {
                    "dedupe_existing": 123,
                    "dedupe_probes": 81,
                    "label_rows": 301,
                    "label_batch_size": 51,
                },
                "performance": {
                    "dedupe_probes_per_second": 10.0,
                    "labels_batch_rows_per_second": 100.0,
                    "labels_speedup_vs_single_row": 1.0,
                },
                "hybrid_eval": {
                    "hybrid_corr_min": -1.0,
                    "hybrid_directional_accuracy_min": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = run_regression_check(
        config_path=cfg,
        db_path=db,
        baseline_path=baseline,
        bench_db_path=bench_db,
        check_hybrid_eval=False,
    )

    assert summary["ok"] is True
    assert summary["benchmark_profile_used"] == {
        "dedupe_existing": 123,
        "dedupe_probes": 81,
        "label_rows": 301,
        "label_batch_size": 51,
    }


def test_regression_check_fails_with_invalid_baseline_metric(tmp_path):
    cfg = tmp_path / "cfg_invalid_baseline.yaml"
    db = tmp_path / "db_invalid_baseline.sqlite"
    baseline = tmp_path / "baseline_invalid.json"
    bench_db = tmp_path / "bench_invalid.sqlite"
    _write_cfg(cfg)
    baseline.write_text(
        json.dumps(
            {
                "performance": {
                    "dedupe_probes_per_second": 10.0,
                    "labels_batch_rows_per_second": 0.0,
                    "labels_speedup_vs_single_row": 1.0,
                },
                "hybrid_eval": {
                    "hybrid_corr_min": -1.0,
                    "hybrid_directional_accuracy_min": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = run_regression_check(
        config_path=cfg,
        db_path=db,
        baseline_path=baseline,
        bench_db_path=bench_db,
        dedupe_existing=120,
        dedupe_probes=80,
        label_rows=300,
        label_batch_size=50,
        perf_floor_ratio=0.7,
        check_hybrid_eval=False,
    )

    assert summary["ok"] is False
    bad = [c for c in summary["checks"] if c["name"] == "labels.batch_rows_per_second"]
    assert bad
    assert bad[0]["status"] == "fail"
    assert bad[0]["note"] == "invalid or missing baseline metric"


def test_calibrate_regression_baseline_writes_file(tmp_path):
    cfg = tmp_path / "cfg_cal.yaml"
    db = tmp_path / "db_cal.sqlite"
    out = tmp_path / "baseline_calibrated.json"
    _write_cfg(cfg)

    summary = calibrate_regression_baseline(
        config_path=cfg,
        db_path=db,
        output_path=out,
        runs=2,
        dedupe_existing=120,
        dedupe_probes=80,
        label_rows=300,
        label_batch_size=50,
        include_hybrid_eval=False,
        overwrite=False,
    )

    assert summary["ok"] is True
    assert out.exists()
    assert summary["baseline"]["benchmark_profile"]["dedupe_existing"] == 120
    assert summary["baseline"]["benchmark_profile"]["dedupe_probes"] == 80
    assert summary["baseline"]["benchmark_profile"]["label_rows"] == 300
    assert summary["baseline"]["benchmark_profile"]["label_batch_size"] == 50
    assert summary["baseline"]["performance"]["dedupe_probes_per_second"] > 0


def test_calibrate_regression_baseline_refuses_overwrite(tmp_path):
    cfg = tmp_path / "cfg_cal_overwrite.yaml"
    db = tmp_path / "db_cal_overwrite.sqlite"
    out = tmp_path / "baseline_exists.json"
    _write_cfg(cfg)
    out.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError):
        calibrate_regression_baseline(
            config_path=cfg,
            db_path=db,
            output_path=out,
            runs=1,
            dedupe_existing=80,
            dedupe_probes=80,
            label_rows=200,
            label_batch_size=50,
            include_hybrid_eval=False,
            overwrite=False,
        )
