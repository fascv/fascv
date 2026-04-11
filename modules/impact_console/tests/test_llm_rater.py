from datetime import UTC, datetime, timedelta

from btc_news_arrow.llm_rater import LLMRater, parse_llm_output_json
from btc_news_arrow.models import NewsItem


def _config() -> dict:
    return {
        "llm": {
            "enabled": True,
            "max_items": 20,
            "max_events": 7,
            "snippet_chars": 200,
        }
    }


def test_parse_llm_output_json_accepts_code_fence():
    text = """```json
{"arrow":"DOWN","score":72,"confidence":0.8,"events":[],"notes":"test"}
```"""
    payload = parse_llm_output_json(text)
    assert payload["arrow"] == "DOWN"
    assert payload["score"] == 72


def test_llm_rater_prepare_items_dedupes_titles():
    rater = LLMRater(_config())
    now = datetime(2026, 2, 8, 12, 0, tzinfo=UTC)
    items = [
        NewsItem(
            timestamp_utc=now - timedelta(minutes=1),
            source="a",
            title="ETF flows increase",
            summary="same",
        ),
        NewsItem(
            timestamp_utc=now - timedelta(minutes=2),
            source="b",
            title="ETF flows increase",
            summary="duplicate",
        ),
    ]

    prepared = rater._prepare_items(items, window_minutes=60, now=now)
    assert len(prepared) == 1
    assert prepared[0]["title"] == "ETF flows increase"


def test_llm_rater_availability_detects_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rater = LLMRater(_config())
    err = rater.availability_error()
    assert err == "OPENAI_API_KEY is not set"


def test_llm_rater_attempt_profiles_shrink_payload():
    rater = LLMRater(_config())
    items = [
        {"id": "1", "title": "A", "source": "s", "published": "2026-02-10T00:00:00Z", "snippet": "x" * 300},
        {"id": "2", "title": "B", "source": "s", "published": "2026-02-10T00:00:00Z", "snippet": "y" * 300},
        {"id": "3", "title": "C", "source": "s", "published": "2026-02-10T00:00:00Z", "snippet": "z" * 300},
    ]
    profiles = rater._attempt_profiles(items)
    assert len(profiles) >= 2
    assert profiles[0]["used_items"] >= profiles[-1]["used_items"]
