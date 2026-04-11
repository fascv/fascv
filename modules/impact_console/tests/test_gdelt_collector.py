from datetime import UTC, datetime

import requests

from btc_news_arrow.classifier import Classifier
from btc_news_arrow.collector import Collector
from btc_news_arrow.scorer import Scorer
from btc_news_arrow.storage import Storage


class _DummyResponse:
    def __init__(self, *, status_code: int, payload=None, text: str = "") -> None:  # noqa: ANN001
        self.status_code = int(status_code)
        self._payload = payload
        self.text = text

    def json(self):  # noqa: ANN201
        if self._payload is None:
            raise ValueError("invalid json")
        return self._payload


def _config() -> dict:
    return {
        "feeds": [],
        "gdelt": {
            "enabled": True,
            "endpoint": "https://api.gdeltproject.org/api/v2/doc/doc",
            "query": "bitcoin OR btc OR cryptocurrency",
            "timespan": "30min",
            "maxrecords": 20,
            "max_retries": 1,
            "retry_seconds": 1,
            "require_relevance": True,
            "min_relevance_matches": 1,
            "relevance_terms": ["bitcoin", "btc", "crypto"],
        },
        "dedupe": {"fuzzy_threshold": 0.92, "lookback_hours": 24},
        "ingestion": {"max_future_skew_seconds": 120},
        "http": {"user_agent": "test"},
        "source_weights": {"gdelt": 0.6},
        "category_weights": {"other": 0.5},
        "trigger_multipliers": {},
        "keyword_rules": {
            "categories": {},
            "polarity": {"positive": [], "negative": []},
            "category_bias": {},
        },
        "heuristics": {},
    }


def _collector(tmp_path):  # noqa: ANN001, ANN202
    cfg = _config()
    storage = Storage(tmp_path / "gdelt_collector.db")
    collector = Collector(
        config=cfg,
        storage=storage,
        classifier=Classifier(cfg),
        scorer=Scorer(cfg),
    )
    return collector, storage


def test_gdelt_fetch_normalizes_query_and_timespan(tmp_path, monkeypatch):
    collector, storage = _collector(tmp_path)
    captured = {}

    def fake_get(url, params, timeout, headers):  # noqa: ANN001, ANN202
        captured["url"] = url
        captured["params"] = dict(params)
        captured["timeout"] = timeout
        captured["headers"] = dict(headers)
        return _DummyResponse(status_code=200, payload={"articles": []})

    monkeypatch.setattr(requests, "get", fake_get)
    try:
        items, err = collector._fetch_gdelt(default_ts=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
        assert err is None
        assert items == []
        assert captured["params"]["query"] == "(bitcoin OR btc OR cryptocurrency)"
        assert captured["params"]["timespan"] == "1h"
    finally:
        storage.close()


def test_gdelt_fetch_retries_after_429_and_then_succeeds(tmp_path, monkeypatch):
    collector, storage = _collector(tmp_path)
    calls = {"n": 0}
    sleeps: list[int] = []

    responses = [
        _DummyResponse(status_code=429, payload=None, text="rate limited"),
        _DummyResponse(
            status_code=200,
            payload={
                "articles": [
                    {
                        "url": "https://example.com/a",
                        "title": "Bitcoin rises on ETF demand",
                        "seendate": "20260213T223000Z",
                        "source": "example",
                    }
                ]
            },
        ),
    ]

    def fake_get(url, params, timeout, headers):  # noqa: ANN001, ANN202
        _ = (url, params, timeout, headers)
        idx = calls["n"]
        calls["n"] += 1
        return responses[idx]

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("btc_news_arrow.collector.time.sleep", lambda sec: sleeps.append(int(sec)))
    try:
        items, err = collector._fetch_gdelt(default_ts=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
        assert err is None
        assert len(items) == 1
        assert items[0].source == "gdelt:example.com"
        assert calls["n"] == 2
        assert sleeps == [1]
    finally:
        storage.close()


def test_gdelt_fetch_applies_domain_cap_and_duplicate_title_filter(tmp_path, monkeypatch):
    collector, storage = _collector(tmp_path)
    collector.gdelt_max_per_domain_per_run = 1
    collector.gdelt_drop_duplicate_titles = True

    payload = {
        "articles": [
            {
                "url": "https://spam.example/a",
                "title": "Bitcoin Same Headline",
                "seendate": "20260213T223000Z",
                "source": "spam.example",
            },
            {
                "url": "https://spam.example/b",
                "title": "BTC Another Headline",
                "seendate": "20260213T223500Z",
                "source": "spam.example",
            },
            {
                "url": "https://good.example/c",
                "title": "Bitcoin Same Headline",
                "seendate": "20260213T224000Z",
                "source": "good.example",
            },
        ]
    }

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: _DummyResponse(status_code=200, payload=payload),  # noqa: ANN001
    )
    try:
        items, err = collector._fetch_gdelt(default_ts=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
        assert err is None
        # 1st item kept, 2nd dropped by domain cap, 3rd dropped by duplicate title.
        assert len(items) == 1
        assert items[0].source == "gdelt:spam.example"
    finally:
        storage.close()


def test_gdelt_fetch_applies_domain_blocklist(tmp_path, monkeypatch):
    collector, storage = _collector(tmp_path)
    collector.gdelt_domain_blocklist = {"blocked.example"}

    payload = {
        "articles": [
            {
                "url": "https://blocked.example/a",
                "title": "Crypto blocked item",
                "seendate": "20260213T223000Z",
                "source": "blocked.example",
            },
            {
                "url": "https://allowed.example/b",
                "title": "Bitcoin allowed item",
                "seendate": "20260213T224000Z",
                "source": "allowed.example",
            },
        ]
    }

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: _DummyResponse(status_code=200, payload=payload),  # noqa: ANN001
    )
    try:
        items, err = collector._fetch_gdelt(default_ts=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
        assert err is None
        assert len(items) == 1
        assert items[0].source == "gdelt:allowed.example"
    finally:
        storage.close()


def test_gdelt_fetch_drops_irrelevant_titles_when_relevance_enabled(tmp_path, monkeypatch):
    collector, storage = _collector(tmp_path)

    payload = {
        "articles": [
            {
                "url": "https://site.example/a",
                "title": "Local council reviews parking policy",
                "seendate": "20260213T223000Z",
                "source": "site.example",
            },
            {
                "url": "https://site.example/b",
                "title": "Bitcoin ETF inflows rise",
                "seendate": "20260213T224000Z",
                "source": "site.example",
            },
        ]
    }

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: _DummyResponse(status_code=200, payload=payload),  # noqa: ANN001
    )
    try:
        items, err = collector._fetch_gdelt(default_ts=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
        assert err is None
        assert len(items) == 1
        assert items[0].title == "Bitcoin ETF inflows rise"
    finally:
        storage.close()


def test_gdelt_fetch_keeps_irrelevant_titles_when_relevance_disabled(tmp_path, monkeypatch):
    collector, storage = _collector(tmp_path)
    collector.gdelt_require_relevance = False

    payload = {
        "articles": [
            {
                "url": "https://site.example/a",
                "title": "Local council reviews parking policy",
                "seendate": "20260213T223000Z",
                "source": "site.example",
            }
        ]
    }

    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: _DummyResponse(status_code=200, payload=payload),  # noqa: ANN001
    )
    try:
        items, err = collector._fetch_gdelt(default_ts=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
        assert err is None
        assert len(items) == 1
    finally:
        storage.close()
