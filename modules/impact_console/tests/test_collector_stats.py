from datetime import UTC, datetime

from btc_news_arrow.classifier import Classifier
from btc_news_arrow.collector import Collector
from btc_news_arrow.models import NewsItem
from btc_news_arrow.scorer import Scorer
from btc_news_arrow.storage import Storage


class StubCollector(Collector):
    def _fetch_feed(self, feed_cfg, default_ts):  # noqa: ANN001
        name = str(feed_cfg.get("name"))
        if name == "broken_feed":
            return [], "feed unreachable"
        item = NewsItem(
            timestamp_utc=default_ts,
            source=name,
            title=f"{name} published",
            summary="btc update",
            guid=f"{name}-1",
            url=f"https://example.com/{name}-1",
        )
        return [item], None

    def _fetch_gdelt(self, default_ts):  # noqa: ANN001
        item = NewsItem(
            timestamp_utc=default_ts,
            source="gdelt:example.com",
            title="GDELT item",
            summary="btc mentioned",
            guid="gdelt-1",
            url="https://example.com/gdelt-1",
        )
        return [item], None


def _config() -> dict:
    return {
        "feeds": [
            {"name": "ok_feed", "type": "rss", "url": "https://example.com/ok"},
            {"name": "broken_feed", "type": "rss", "url": "https://example.com/broken"},
        ],
        "gdelt": {"enabled": True, "endpoint": "https://gdelt.example.com"},
        "dedupe": {"fuzzy_threshold": 0.92, "lookback_hours": 24},
        "ingestion": {"max_future_skew_seconds": 120},
        "http": {"user_agent": "test"},
        "source_weights": {"ok_feed": 1.0, "gdelt": 0.6},
        "category_weights": {"other": 0.5},
        "trigger_multipliers": {},
        "keyword_rules": {
            "categories": {},
            "polarity": {"positive": [], "negative": []},
            "category_bias": {},
        },
        "heuristics": {},
    }


def test_collect_once_returns_stats_with_source_breakdown(tmp_path):
    storage = Storage(tmp_path / "collector_stats.db")
    try:
        collector = StubCollector(
            config=_config(),
            storage=storage,
            classifier=Classifier(_config()),
            scorer=Scorer(_config()),
        )
        items, inserted, stats = collector.collect_once(
            include_gdelt=True,
            now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        )

        assert len(items) == 2
        assert inserted == 2
        assert stats["totals"]["raw_items"] == 2
        assert stats["totals"]["errors"] == 1
        assert stats["totals"]["processed"] == 2
        assert stats["totals"]["inserted"] == 2
        assert stats["sources"]["ok_feed"]["processed"] == 1
        assert stats["sources"]["broken_feed"]["errors"] == 1
        assert stats["sources"]["gdelt"]["fetched"] == 1
    finally:
        storage.close()


class ClusterStubCollector(Collector):
    def _fetch_feed(self, feed_cfg, default_ts):  # noqa: ANN001
        name = str(feed_cfg.get("name"))
        title = "ETF approval update hits market"
        item = NewsItem(
            timestamp_utc=default_ts,
            source=name,
            title=title,
            summary="Bitcoin market reacts to ETF approval update",
            guid=f"{name}-cluster",
            url=f"https://example.com/{name}/cluster",
        )
        return [item], None

    def _fetch_gdelt(self, default_ts):  # noqa: ANN001
        return [], None


def test_collector_assigns_story_cluster_ids(tmp_path):
    cfg = _config()
    cfg["feeds"] = [
        {"name": "feed_a", "type": "rss", "url": "https://example.com/a"},
        {"name": "feed_b", "type": "rss", "url": "https://example.com/b"},
    ]
    cfg["gdelt"]["enabled"] = False
    storage = Storage(tmp_path / "collector_cluster.db")
    try:
        collector = ClusterStubCollector(
            config=cfg,
            storage=storage,
            classifier=Classifier(cfg),
            scorer=Scorer(cfg),
        )
        items, inserted, _ = collector.collect_once(
            include_gdelt=False,
            now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        )
        assert inserted == 2
        assert len(items) == 2
        cluster_ids = {str((i.raw or {}).get("event_cluster_id")) for i in items}
        assert len(cluster_ids) == 1
        assert "None" not in cluster_ids
    finally:
        storage.close()
