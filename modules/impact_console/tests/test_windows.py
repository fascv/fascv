from datetime import UTC, datetime, timedelta

from btc_news_arrow.aggregator import Aggregator
from btc_news_arrow.models import NewsItem
from btc_news_arrow.utils import parse_duration


def test_parse_duration_week():
    assert parse_duration("1w") == timedelta(weeks=1)


def test_aggregator_excludes_future_items():
    cfg = {
        "decay": {"half_life_minutes": 60},
        "thresholds": {"60m": 0.1, "default": 0.1},
    }
    agg = Aggregator(cfg)
    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)

    items = [
        NewsItem(
            timestamp_utc=now - timedelta(minutes=5),
            source="fed",
            title="Past positive",
            impact=0.5,
        ),
        NewsItem(
            timestamp_utc=now + timedelta(minutes=10),
            source="fed",
            title="Future negative",
            impact=-1.0,
        ),
    ]

    result = agg.arrow_for_window(items, window_minutes=60, now=now)

    assert result.contributing_items == 1
    assert result.arrow.startswith("▲")


def test_aggregator_dampens_repeated_source_burst():
    cfg = {
        "decay": {"half_life_minutes": 10_000},
        "thresholds": {"60m": 0.1, "default": 0.1},
        "aggregation": {"source_repeat_half_life_items": 1.0},
    }
    agg = Aggregator(cfg)
    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)

    repeated_source = [
        NewsItem(timestamp_utc=now - timedelta(minutes=1), source="gdelt:site-a", title="A", impact=1.0),
        NewsItem(timestamp_utc=now - timedelta(minutes=2), source="gdelt:site-a", title="B", impact=1.0),
    ]
    diverse_sources = [
        NewsItem(timestamp_utc=now - timedelta(minutes=1), source="gdelt:site-a", title="A", impact=1.0),
        NewsItem(timestamp_utc=now - timedelta(minutes=2), source="gdelt:site-b", title="B", impact=1.0),
    ]

    repeated = agg.arrow_for_window(repeated_source, window_minutes=60, now=now)
    diverse = agg.arrow_for_window(diverse_sources, window_minutes=60, now=now)

    assert repeated.score < diverse.score


def test_aggregator_dampens_repeated_event_cluster_across_sources():
    cfg = {
        "decay": {"half_life_minutes": 10_000},
        "thresholds": {"60m": 0.1, "default": 0.1},
        "aggregation": {
            "source_repeat_half_life_items": 10_000,
            "cluster_repeat_half_life_items": 1.0,
        },
    }
    agg = Aggregator(cfg)
    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)

    same_cluster = [
        NewsItem(
            timestamp_utc=now - timedelta(minutes=1),
            source="feed-a",
            title="ETF approval update",
            impact=1.0,
            raw={"event_cluster_id": "cluster-1"},
        ),
        NewsItem(
            timestamp_utc=now - timedelta(minutes=2),
            source="feed-b",
            title="ETF approval update from another source",
            impact=1.0,
            raw={"event_cluster_id": "cluster-1"},
        ),
    ]
    different_cluster = [
        NewsItem(
            timestamp_utc=now - timedelta(minutes=1),
            source="feed-a",
            title="ETF approval update",
            impact=1.0,
            raw={"event_cluster_id": "cluster-1"},
        ),
        NewsItem(
            timestamp_utc=now - timedelta(minutes=2),
            source="feed-b",
            title="CPI shock update",
            impact=1.0,
            raw={"event_cluster_id": "cluster-2"},
        ),
    ]

    repeated = agg.arrow_for_window(same_cluster, window_minutes=60, now=now)
    diverse = agg.arrow_for_window(different_cluster, window_minutes=60, now=now)

    assert repeated.score < diverse.score
