from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from btc_news_arrow.api import create_app
from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage


def test_signal_trading_endpoint_returns_contract(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "signal.db"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "llm": {"enabled": False, "require_runtime": False},
                "thresholds": {"60m": 0.2, "default": 0.2},
                "source_weights": {"fed": 1.0},
                "category_weights": {"macro": 1.0, "other": 0.5},
                "trigger_multipliers": {},
                "keyword_rules": {
                    "categories": {"macro": ["cpi"]},
                    "polarity": {"positive": [], "negative": []},
                    "category_bias": {"macro": 1},
                },
                "hybrid": {
                    "rule_weight": 1.0,
                    "llm_weight": 0.0,
                    "learn_weight": 0.0,
                    "no_signal_min_items": 1,
                    "no_signal_min_relevance_sum": 0.0,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    storage = Storage(db_path)
    try:
        now = datetime.now(tz=UTC)
        items = [
            NewsItem(
                timestamp_utc=now - timedelta(minutes=5),
                source="fed",
                title="CPI update",
                summary="",
                category="macro",
                polarity=1,
                impact=0.8,
                guid=f"sig-{i}",
                url=f"https://example.com/sig/{i}",
                raw={"scoring": {"btc_relevance_multiplier": 1.0}},
            )
            for i in range(2)
        ]
        storage.insert_items(items)
    finally:
        storage.close()

    app = create_app(config_path=str(cfg_path), db_path=str(db_path))
    client = TestClient(app)
    resp = client.get("/signal/trading?window=1h&mode=rule")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["version"] == "v1"
    assert payload["window"] == "1h"
    assert payload["abstain"] is False
    assert payload["direction"] == 1
    assert "components" in payload
    assert "flags" in payload
    assert "attribution_state" in payload
    assert "news_driven_probability" in payload
