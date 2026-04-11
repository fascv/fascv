from datetime import UTC, datetime, timedelta

import yaml

from btc_news_arrow.models import NewsItem
from btc_news_arrow.optimizer import evaluate_hybrid_model
from btc_news_arrow.storage import Storage


def test_hybrid_eval_reports_metrics_and_weights(tmp_path):
    cfg_path = tmp_path / "config_eval.yaml"
    db_path = tmp_path / "hybrid_eval.db"

    cfg_path.write_text(
        yaml.safe_dump(
            {
                "trigger_multipliers": {},
                "learning": {
                    "horizons": ["60m"],
                    "market": {"endpoint": "https://api.binance.com", "symbol": "BTCUSDT"},
                    "model": {"confidence_k": 0.0},
                    "arrow": {"scale": 100.0, "thresholds": {"60m": 0.35, "default": 0.35}},
                },
                "hybrid": {
                    "rule_weight": 0.2,
                    "llm_weight": 0.0,
                    "learn_weight": 0.8,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    storage = Storage(db_path)
    try:
        now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
        items: list[NewsItem] = []
        for i in range(90):
            source = "src_pos" if i % 2 == 0 else "src_neg"
            impact = -0.8 if source == "src_pos" else 0.8
            ts = now - timedelta(days=2) + timedelta(minutes=i)
            items.append(
                NewsItem(
                    timestamp_utc=ts,
                    source=source,
                    title=f"eval-{i}",
                    summary="",
                    category="other",
                    polarity=0,
                    impact=impact,
                    guid=f"eval-g-{i}",
                    url=f"https://example.com/eval/{i}",
                )
            )
        storage.insert_items(items)

        for item_id, item in storage.get_items_with_ids():
            ret = 0.02 if item.source == "src_pos" else -0.02
            storage.upsert_item_label(item_id=item_id, horizon_minutes=60, return_value=ret)

        storage.apply_feature_updates(
            {
                ("source:src_pos", 60): (200, 4.0),
                ("source:src_neg", 60): (200, -4.0),
            }
        )
    finally:
        storage.close()

    summary = evaluate_hybrid_model(
        config_path=cfg_path,
        db_path=db_path,
        windows=["1h"],
        lookback_days=10,
        min_samples=40,
    )

    assert summary["ok"] is True
    assert summary["samples"] >= 80
    assert summary["weights_non_llm"]["rule_share"] == 0.2
    assert summary["weights_non_llm"]["learn_share"] == 0.8
    global_metrics = summary["metrics"]["global"]
    assert abs(global_metrics["hybrid"]["corr"]) >= abs(global_metrics["rule"]["corr"]) - 1e-9
    assert "1h" in summary["metrics"]["by_window"]
    walk = summary["metrics"]["walk_forward"]["global"]
    assert walk["folds"] >= 1
    assert walk["samples_oos"] > 0
    assert "adaptive_hybrid" in walk
    assert "trading" in walk


def test_hybrid_eval_returns_not_enough_samples(tmp_path):
    cfg_path = tmp_path / "config_eval_small.yaml"
    db_path = tmp_path / "hybrid_eval_small.db"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "trigger_multipliers": {},
                "learning": {
                    "horizons": ["60m"],
                    "market": {"endpoint": "https://api.binance.com", "symbol": "BTCUSDT"},
                    "model": {"confidence_k": 0.0},
                    "arrow": {"scale": 100.0, "thresholds": {"60m": 0.35, "default": 0.35}},
                },
                "hybrid": {
                    "rule_weight": 0.5,
                    "llm_weight": 0.0,
                    "learn_weight": 0.5,
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    storage = Storage(db_path)
    try:
        now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
        item = NewsItem(
            timestamp_utc=now - timedelta(hours=2),
            source="src_pos",
            title="too-few",
            summary="",
            category="other",
            polarity=0,
            impact=0.2,
            guid="few-1",
            url="https://example.com/few-1",
        )
        storage.insert_items([item])
        item_id, _ = storage.get_items_with_ids()[0]
        storage.upsert_item_label(item_id=item_id, horizon_minutes=60, return_value=0.01)
        storage.apply_feature_updates({("source:src_pos", 60): (10, 0.1)})
    finally:
        storage.close()

    summary = evaluate_hybrid_model(
        config_path=cfg_path,
        db_path=db_path,
        windows=["1h"],
        lookback_days=10,
        min_samples=40,
    )

    assert summary["ok"] is False
    assert summary["reason"] == "not_enough_samples"
