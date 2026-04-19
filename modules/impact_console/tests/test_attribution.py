from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from btc_news_arrow.api import create_app
from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage


def _seed_prices(storage: Storage, start_ts: datetime, count: int = 260) -> datetime:
    start_epoch = int(start_ts.timestamp())
    points: dict[int, float] = {}
    price = 100.0
    for i in range(count):
        ts = start_epoch + i * 60
        # Small variance keeps z-score stable and deterministic.
        price *= 1.0 + 0.00025 * ((i % 7) - 3)
        if i == count - 1:
            price *= 1.03
        points[ts] = price
    storage.upsert_price_points(points)
    return datetime.fromtimestamp(start_epoch + (count - 1) * 60, tz=UTC)


def _base_config() -> dict:
    return {
        "llm": {"enabled": False, "require_runtime": False},
        "hybrid": {
            "rule_weight": 1.0,
            "llm_weight": 0.0,
            "learn_weight": 0.0,
            "no_signal_min_items": 1,
            "no_signal_min_relevance_sum": 0.0,
        },
        "attribution": {
            "enabled": True,
            "llm_usage": "on",
            "event_detection": {
                "horizons": ["15m", "60m"],
                "zscore_threshold": 1.0,
                "min_abs_return": 0.002,
                "cooldown_minutes": 30,
                "max_events_per_day": 24,
                "lookback_hours": 48,
                "zscore_lookback_points": 40,
            },
            "candidate_window": {"lookback_minutes": 180, "lookahead_minutes": 15, "max_candidates": 20},
            "candidate_filters": {"min_abs_impact": 0.02, "min_abs_learn_score": 0.01, "cluster_decay": 0.6, "top_k": 5},
            "scoring_weights": {"time": 0.30, "direction": 0.20, "rule": 0.30, "learn": 0.20, "llm": 0.0},
            "thresholds": {"news_driven_probability": 0.30, "news_driven_top_score": 0.20, "mixed_probability": 0.15},
        },
        "thresholds": {"60m": 0.2, "default": 0.2},
        "source_weights": {"coindesk": 0.7, "cointelegraph": 0.6},
        "category_weights": {"other": 0.5, "etf_institutional": 0.9},
        "trigger_multipliers": {},
        "keyword_rules": {
            "categories": {"etf_institutional": ["etf"]},
            "polarity": {"positive": [], "negative": []},
            "category_bias": {"etf_institutional": 1},
        },
    }


def test_signal_attribution_endpoint_returns_ranked_candidates(tmp_path):
    cfg = _base_config()
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "attr.db"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")

    storage = Storage(db_path)
    try:
        start = datetime(2026, 2, 14, 0, 0, tzinfo=UTC)
        event_ts = _seed_prices(storage, start_ts=start)
        items = [
            NewsItem(
                timestamp_utc=event_ts - timedelta(minutes=10),
                source="coindesk",
                title="ETF inflows accelerate",
                summary="Institutional ETF demand rises rapidly.",
                category="etf_institutional",
                polarity=1,
                impact=0.35,
                guid="attr-1",
                url="https://example.com/attr-1",
                raw={"event_cluster_id": "cluster-main", "scoring": {"source_quality_factor": 1.2}},
            ),
            NewsItem(
                timestamp_utc=event_ts - timedelta(minutes=35),
                source="cointelegraph",
                title="BTC momentum extends after ETF updates",
                summary="ETF related momentum remains strong.",
                category="etf_institutional",
                polarity=1,
                impact=0.22,
                guid="attr-2",
                url="https://example.com/attr-2",
                raw={"event_cluster_id": "cluster-main", "scoring": {"source_quality_factor": 1.1}},
            ),
        ]
        storage.insert_items(items)
    finally:
        storage.close()

    app = create_app(config_path=str(cfg_path), db_path=str(db_path))
    client = TestClient(app)

    resp = client.get("/signal/attribution?window=1h&limit=3")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["version"] == "v1"
    assert payload["event"] is not None
    assert payload["summary"]["candidate_count"] >= 1
    assert isinstance(payload["candidates"], list)
    assert payload["candidates"][0]["attribution_score"] >= 0.0


def test_signal_trading_includes_attribution_fields(tmp_path):
    cfg = _base_config()
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "trade_attr.db"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")

    storage = Storage(db_path)
    try:
        start = datetime(2026, 2, 14, 0, 0, tzinfo=UTC)
        event_ts = _seed_prices(storage, start_ts=start)
        storage.insert_items(
            [
                NewsItem(
                    timestamp_utc=event_ts - timedelta(minutes=8),
                    source="coindesk",
                    title="ETF related demand jumps",
                    summary="",
                    category="etf_institutional",
                    polarity=1,
                    impact=0.30,
                    guid="trade-attr-1",
                    url="https://example.com/trade-attr-1",
                )
            ]
        )
    finally:
        storage.close()

    app = create_app(config_path=str(cfg_path), db_path=str(db_path))
    client = TestClient(app)

    resp = client.get("/signal/trading?window=1h&mode=rule")
    assert resp.status_code == 200
    payload = resp.json()
    assert "attribution_state" in payload
    assert "news_driven_probability" in payload
    assert "attribution_event" in payload
    assert "top_attribution" in payload
    assert payload["attribution_version"] == "v1"


def test_signal_attribution_reports_not_applicable_without_price_events(tmp_path):
    cfg = _base_config()
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "no_price.db"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")

    app = create_app(config_path=str(cfg_path), db_path=str(db_path))
    client = TestClient(app)

    resp = client.get("/signal/attribution?window=1h")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["summary"]["classification"] == "not_applicable"
    assert payload["event"] is None
