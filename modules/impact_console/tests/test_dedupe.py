from datetime import UTC, datetime

from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import utcnow


def test_guid_url_duplicate_detection(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)
    try:
        item = NewsItem(
            timestamp_utc=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
            source="fed",
            title="Fed statement",
            summary="",
            url="https://example.com/fed-1",
            guid="fed-1",
        )
        inserted = storage.insert_items([item])
        assert inserted == 1

        assert storage.exists_guid_or_url("fed-1", None)
        assert storage.exists_guid_or_url(None, "https://example.com/fed-1")
    finally:
        storage.close()


def test_fuzzy_title_duplicate_detection(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)
    try:
        item = NewsItem(
            timestamp_utc=utcnow(),
            source="sec",
            title="SEC files lawsuit against major crypto firm",
            summary="",
        )
        storage.insert_items([item])

        similar = "SEC files a lawsuit against major crypto firm"
        assert storage.has_similar_title(similar, lookback_hours=24, threshold=0.9)
    finally:
        storage.close()


def test_url_duplicate_detection_ignores_tracking_params(tmp_path):
    db = tmp_path / "test_tracking.db"
    storage = Storage(db)
    try:
        item = NewsItem(
            timestamp_utc=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
            source="sec",
            title="SEC update",
            summary="",
            url="https://example.com/news?id=42&utm_source=x&fbclid=abc",
            guid="sec-42",
        )
        inserted = storage.insert_items([item])
        assert inserted == 1

        assert storage.exists_guid_or_url(None, "https://example.com/news?id=42")
        assert storage.exists_guid_or_url(None, "https://example.com/news?id=42&utm_campaign=y")
    finally:
        storage.close()
