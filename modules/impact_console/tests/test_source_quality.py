from datetime import UTC, datetime, timedelta

from btc_news_arrow.models import NewsItem
from btc_news_arrow.source_quality import generate_source_quality_report
from btc_news_arrow.storage import Storage


def _insert_labeled_item(
    storage: Storage,
    *,
    ts: datetime,
    source: str,
    impact: float,
    ret: float,
    guid: str,
    horizon: int = 60,
) -> None:
    storage.insert_items(
        [
            NewsItem(
                timestamp_utc=ts,
                source=source,
                title=f"{source} {guid}",
                summary="",
                impact=float(impact),
                guid=guid,
                url=f"https://example.com/{guid}",
            )
        ]
    )
    item_id = storage.get_items_with_ids()[-1][0]
    storage.upsert_item_label(item_id=item_id, horizon_minutes=horizon, return_value=float(ret))


def test_source_quality_report_contains_per_source_metrics(tmp_path):
    db = tmp_path / "source_quality.db"
    storage = Storage(db)
    now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
    try:
        for i in range(6):
            _insert_labeled_item(
                storage,
                ts=now - timedelta(days=1, minutes=i),
                source="src_good",
                impact=0.4 + 0.05 * i,
                ret=0.01 + 0.002 * i,
                guid=f"g-{i}",
            )
        for i in range(6):
            _insert_labeled_item(
                storage,
                ts=now - timedelta(days=1, minutes=20 + i),
                source="src_bad",
                impact=0.5 + 0.04 * i,
                ret=-(0.01 + 0.002 * i),
                guid=f"b-{i}",
            )
    finally:
        storage.close()

    report_path = tmp_path / "latest.json"
    report = generate_source_quality_report(
        config_path="config.yaml",
        db_path=db,
        windows=["1h"],
        lookback_days=30,
        min_samples_per_source=5,
        top_n=10,
        report_path=report_path,
        history_dir=tmp_path / "history",
        keep_history=False,
        now=now,
    )

    assert report_path.exists()
    by_window = (report["current"]["by_window"] or {}).get("1h") or {}
    assert by_window["samples_total"] >= 12
    sources = {row["source"]: row for row in by_window["sources"]}
    assert "src_good" in sources
    assert "src_bad" in sources
    assert sources["src_good"]["corr"] > 0
    assert sources["src_bad"]["corr"] < 0


def test_source_quality_report_trend_comparison(tmp_path):
    db = tmp_path / "source_quality_trend.db"
    storage = Storage(db)
    now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
    try:
        for i in range(5):
            _insert_labeled_item(
                storage,
                ts=now - timedelta(days=1, minutes=i),
                source="src_a",
                impact=0.2 + 0.01 * i,
                ret=0.005 + 0.001 * i,
                guid=f"a1-{i}",
            )
    finally:
        storage.close()

    report_path = tmp_path / "latest.json"
    first = generate_source_quality_report(
        config_path="config.yaml",
        db_path=db,
        windows=["1h"],
        lookback_days=30,
        min_samples_per_source=3,
        top_n=10,
        report_path=report_path,
        history_dir=tmp_path / "history",
        keep_history=False,
        now=now,
    )
    assert first["trend"]["status"] in {"flat", "unavailable"}

    storage = Storage(db)
    try:
        for i in range(5):
            _insert_labeled_item(
                storage,
                ts=now - timedelta(hours=2, minutes=i),
                source="src_a",
                impact=0.35 + 0.02 * i,
                ret=0.02 + 0.003 * i,
                guid=f"a2-{i}",
            )
    finally:
        storage.close()

    second = generate_source_quality_report(
        config_path="config.yaml",
        db_path=db,
        windows=["1h"],
        lookback_days=30,
        min_samples_per_source=3,
        top_n=10,
        report_path=report_path,
        history_dir=tmp_path / "history",
        keep_history=False,
        now=now + timedelta(minutes=5),
    )
    delta = (second["delta"]["by_window"] or {}).get("1h") or {}
    assert "global_corr_delta" in delta
    assert second["trend"]["status"] in {"improved", "degraded", "flat", "mixed"}
