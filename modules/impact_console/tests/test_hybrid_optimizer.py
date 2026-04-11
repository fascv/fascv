from datetime import UTC, datetime, timedelta

import yaml

from btc_news_arrow.models import NewsItem
from btc_news_arrow.optimizer import optimize_hybrid_weights
from btc_news_arrow.storage import Storage


def test_hybrid_optimizer_prefers_learn_when_rule_is_inverse(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "hybrid_opt.db"

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
                    "llm_weight": 0.3,
                    "learn_weight": 0.2,
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
        for i in range(80):
            source = "src_pos" if i % 2 == 0 else "src_neg"
            impact = -0.9 if source == "src_pos" else 0.9
            ts = now - timedelta(days=2) + timedelta(minutes=i)
            items.append(
                NewsItem(
                    timestamp_utc=ts,
                    source=source,
                    title=f"sample-{i}",
                    summary="",
                    category="other",
                    polarity=0,
                    impact=impact,
                    guid=f"g-{i}",
                    url=f"https://example.com/{i}",
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

    summary = optimize_hybrid_weights(
        config_path=cfg_path,
        db_path=db_path,
        windows=["1h"],
        lookback_days=10,
        min_samples=40,
        grid_step=0.05,
    )

    assert summary["ok"] is True
    assert summary["corr_after"] > summary["corr_before"]
    assert summary["weights"]["learn_weight"] > summary["weights"]["rule_weight"]


def test_hybrid_optimizer_respects_max_share_shift_guardrail(tmp_path):
    cfg_path = tmp_path / "config_guardrail.yaml"
    db_path = tmp_path / "hybrid_opt_guardrail.db"

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
                    "rule_weight": 0.9,
                    "llm_weight": 0.0,
                    "learn_weight": 0.1,
                    "optimizer": {
                        "max_share_shift": 0.05,
                        "holdout_ratio": 0.0,
                    },
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
        for i in range(80):
            source = "src_pos" if i % 2 == 0 else "src_neg"
            impact = -0.9 if source == "src_pos" else 0.9
            ts = now - timedelta(days=2) + timedelta(minutes=i)
            items.append(
                NewsItem(
                    timestamp_utc=ts,
                    source=source,
                    title=f"shift-{i}",
                    summary="",
                    category="other",
                    polarity=0,
                    impact=impact,
                    guid=f"shift-g-{i}",
                    url=f"https://example.com/shift/{i}",
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

    summary = optimize_hybrid_weights(
        config_path=cfg_path,
        db_path=db_path,
        windows=["1h"],
        lookback_days=10,
        min_samples=40,
        grid_step=0.05,
    )

    assert summary["ok"] is True
    old_share = 0.9
    new_share = summary["weights"]["rule_share_non_llm"]
    assert abs(new_share - old_share) <= 0.051


def test_hybrid_optimizer_blocks_update_without_holdout_improvement(tmp_path):
    cfg_path = tmp_path / "config_holdout_guardrail.yaml"
    db_path = tmp_path / "hybrid_opt_holdout.db"

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
                    "optimizer": {
                        "holdout_ratio": 0.25,
                        "min_holdout_samples": 20,
                        "require_holdout_improvement": True,
                        "min_holdout_improvement": 0.01,
                        "max_share_shift": 1.0,
                    },
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
        for i in range(120):
            source = "src_pos" if i % 2 == 0 else "src_neg"
            ts = now - timedelta(days=3) + timedelta(minutes=i)
            impact = -0.9 if source == "src_pos" else 0.9
            items.append(
                NewsItem(
                    timestamp_utc=ts,
                    source=source,
                    title=f"holdout-{i}",
                    summary="",
                    category="other",
                    polarity=0,
                    impact=impact,
                    guid=f"holdout-g-{i}",
                    url=f"https://example.com/holdout/{i}",
                )
            )
        storage.insert_items(items)

        for idx, (item_id, item) in enumerate(storage.get_items_with_ids()):
            if idx < 90:
                ret = 0.02 if item.source == "src_pos" else -0.02
            else:
                ret = -0.02 if item.source == "src_pos" else 0.02
            storage.upsert_item_label(item_id=item_id, horizon_minutes=60, return_value=ret)

        storage.apply_feature_updates(
            {
                ("source:src_pos", 60): (200, 4.0),
                ("source:src_neg", 60): (200, -4.0),
            }
        )
    finally:
        storage.close()

    summary = optimize_hybrid_weights(
        config_path=cfg_path,
        db_path=db_path,
        windows=["1h"],
        lookback_days=10,
        min_samples=60,
        grid_step=0.05,
    )

    assert summary["ok"] is False
    assert summary["reason"] == "no_holdout_improvement"
