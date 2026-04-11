from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage


def run_benchmark(
    db_path: str | Path,
    dedupe_existing: int = 5000,
    dedupe_probes: int = 2000,
    label_rows: int = 20000,
    label_batch_size: int = 1000,
    keep_db: bool = False,
    overwrite_db: bool = False,
) -> dict[str, Any]:
    bench_db_path = Path(db_path)
    if bench_db_path.exists():
        if not overwrite_db:
            raise FileExistsError(
                f"Benchmark DB already exists: {bench_db_path}. "
                "Choose another path or enable overwrite."
            )
        bench_db_path.unlink()

    started_at = datetime.now(tz=UTC).isoformat()
    storage = Storage(bench_db_path)
    try:
        dedupe_result = benchmark_dedupe(
            storage=storage,
            existing=max(50, int(dedupe_existing)),
            probes=max(50, int(dedupe_probes)),
        )
        label_result = benchmark_label_upserts(
            storage=storage,
            rows=max(100, int(label_rows)),
            batch_size=max(1, int(label_batch_size)),
        )

        summary = {
            "ok": True,
            "started_at": started_at,
            "db_path": str(bench_db_path),
            "dedupe": dedupe_result,
            "labels": label_result,
        }
        return summary
    finally:
        storage.close()
        if not keep_db and bench_db_path.exists():
            bench_db_path.unlink()


def benchmark_dedupe(storage: Storage, existing: int, probes: int) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    items: list[NewsItem] = []
    for i in range(existing):
        items.append(
            NewsItem(
                timestamp_utc=now,
                source="bench_source",
                title=f"Bitcoin market update cycle {i}",
                summary="synthetic benchmark item",
                guid=f"bench-guid-{i}",
                url=f"https://example.com/bench/{i}",
                category="other",
                polarity=0,
                impact=0.0,
            )
        )
    storage.insert_items(items)

    matches = 0
    start = perf_counter()
    for i in range(probes):
        if i % 2 == 0:
            title = f"Bitcoin market update cycle {i % existing}"
        else:
            title = f"Unrelated macro bulletin {i}"
        if storage.has_similar_title(title=title, lookback_hours=24, threshold=0.9):
            matches += 1
    elapsed = max(1e-9, perf_counter() - start)

    return {
        "existing_items": existing,
        "probes": probes,
        "matches": matches,
        "seconds": elapsed,
        "probes_per_second": probes / elapsed,
        "avg_probe_ms": (elapsed / probes) * 1000.0,
    }


def benchmark_label_upserts(storage: Storage, rows: int, batch_size: int) -> dict[str, Any]:
    _truncate_item_labels(storage)
    single_rows = min(500, rows)

    start_single = perf_counter()
    for i in range(single_rows):
        ret = 0.0001 if i % 2 == 0 else -0.0001
        storage.upsert_item_label(item_id=i + 1, horizon_minutes=60, return_value=ret)
    elapsed_single = max(1e-9, perf_counter() - start_single)

    _truncate_item_labels(storage)
    start_batch = perf_counter()
    pending: list[tuple[int, int, float]] = []
    for i in range(rows):
        ret = 0.0001 if i % 2 == 0 else -0.0001
        pending.append((i + 1, 60, ret))
        if len(pending) >= batch_size:
            storage.upsert_item_labels(pending)
            pending.clear()
    if pending:
        storage.upsert_item_labels(pending)
    elapsed_batch = max(1e-9, perf_counter() - start_batch)

    per_row_single = elapsed_single / single_rows
    per_row_batch = elapsed_batch / rows
    speedup = per_row_single / per_row_batch if per_row_batch > 0 else None
    return {
        "rows": rows,
        "batch_size": batch_size,
        "single_row_sample_size": single_rows,
        "single_row_seconds": elapsed_single,
        "single_row_rows_per_second": single_rows / elapsed_single,
        "batch_seconds": elapsed_batch,
        "batch_rows_per_second": rows / elapsed_batch,
        "speedup_vs_single_row": speedup,
    }


def format_benchmark_summary(summary: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(summary, indent=2, sort_keys=False)

    dedupe = summary.get("dedupe", {})
    labels = summary.get("labels", {})
    lines = [
        f"Benchmark DB: {summary.get('db_path')}",
        (
            "Dedupe: "
            f"existing={dedupe.get('existing_items')} probes={dedupe.get('probes')} "
            f"matches={dedupe.get('matches')} "
            f"rate={_fmt_float(dedupe.get('probes_per_second'), 2)} probes/s "
            f"avg={_fmt_float(dedupe.get('avg_probe_ms'), 3)} ms"
        ),
        (
            "Labels: "
            f"rows={labels.get('rows')} batch={labels.get('batch_size')} "
            f"single_rate={_fmt_float(labels.get('single_row_rows_per_second'), 2)} rows/s "
            f"batch_rate={_fmt_float(labels.get('batch_rows_per_second'), 2)} rows/s "
            f"speedup={_fmt_float(labels.get('speedup_vs_single_row'), 2)}x"
        ),
    ]
    return "\n".join(lines)


def _truncate_item_labels(storage: Storage) -> None:
    storage.conn.execute("DELETE FROM item_labels")
    storage.conn.commit()


def _fmt_float(value: Any, digits: int) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"
