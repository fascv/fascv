from pathlib import Path

import pytest

from btc_news_arrow.benchmark import format_benchmark_summary, run_benchmark


def test_run_benchmark_returns_expected_sections(tmp_path):
    db_path = tmp_path / "bench.db"
    summary = run_benchmark(
        db_path=db_path,
        dedupe_existing=120,
        dedupe_probes=80,
        label_rows=300,
        label_batch_size=50,
        keep_db=True,
    )

    assert summary["ok"] is True
    assert summary["dedupe"]["existing_items"] == 120
    assert summary["dedupe"]["probes"] == 80
    assert summary["dedupe"]["probes_per_second"] > 0
    assert summary["labels"]["rows"] == 300
    assert summary["labels"]["batch_rows_per_second"] > 0
    assert Path(summary["db_path"]).exists()


def test_run_benchmark_deletes_db_when_keep_disabled(tmp_path):
    db_path = tmp_path / "bench_drop.db"
    summary = run_benchmark(
        db_path=db_path,
        dedupe_existing=80,
        dedupe_probes=80,
        label_rows=200,
        label_batch_size=40,
        keep_db=False,
    )
    assert summary["ok"] is True
    assert not Path(summary["db_path"]).exists()


def test_format_benchmark_summary_contains_sections(tmp_path):
    db_path = tmp_path / "bench_fmt.db"
    summary = run_benchmark(
        db_path=db_path,
        dedupe_existing=80,
        dedupe_probes=80,
        label_rows=200,
        label_batch_size=50,
        keep_db=False,
    )
    rendered = format_benchmark_summary(summary, as_json=False)
    assert "Dedupe:" in rendered
    assert "Labels:" in rendered


def test_run_benchmark_refuses_existing_db_without_overwrite(tmp_path):
    db_path = tmp_path / "bench_existing.db"
    db_path.write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_benchmark(
            db_path=db_path,
            dedupe_existing=80,
            dedupe_probes=80,
            label_rows=200,
            label_batch_size=50,
            keep_db=False,
            overwrite_db=False,
        )
