from datetime import UTC, datetime, timedelta

from btc_news_arrow.learner import Learner, to_minute_epoch
from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage


class FakePriceClient:
    def __init__(self, points: dict[int, float]) -> None:
        self.points = points

    def fetch_closes(self, start_minute_epoch: int, end_minute_epoch: int) -> dict[int, float]:
        return {
            ts: price
            for ts, price in self.points.items()
            if start_minute_epoch <= ts <= end_minute_epoch
        }


def _config() -> dict:
    return {
        "trigger_multipliers": {"outage": 1.2},
        "decay": {"half_life_minutes": 720},
        "learning": {
            "horizons": ["60m"],
            "market": {"endpoint": "https://api.binance.com", "symbol": "BTCUSDT"},
            "model": {"confidence_k": 1.0},
            "arrow": {
                "scale": 100.0,
                "thresholds": {"60m": 0.1, "default": 0.1},
            },
        },
    }


def test_learning_update_and_prediction(tmp_path):
    db = tmp_path / "learn.db"
    storage = Storage(db)

    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)
    ts = now - timedelta(hours=2)
    item = NewsItem(
        timestamp_utc=ts,
        source="kraken_status",
        title="Service outage resolved",
        summary="",
        category="exchange_incident",
        polarity=-1,
        impact=-0.5,
        url="https://example.com/outage",
        guid="outage-1",
    )
    storage.insert_items([item])

    start_m = to_minute_epoch(ts)
    end_m = start_m + 60 * 60
    price_client = FakePriceClient({start_m: 100.0, end_m: 101.0})
    learner = Learner(_config(), price_client=price_client)

    summary = learner.update_model(storage=storage, limit_per_horizon=100, now=now)
    assert summary["labeled"] == 1

    items = storage.get_items_since(now - timedelta(hours=3))
    assert len(items) == 1
    pred = learner.predict_item_return(storage, items[0], horizon_minutes=60)
    assert pred > 0

    storage.close()


def test_mixed_horizon_prediction_uses_configured_weights(tmp_path):
    db = tmp_path / "learn_mix.db"
    storage = Storage(db)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
        source="kraken_status",
        title="Service outage resolved",
        summary="",
        category="exchange_incident",
        polarity=-1,
        impact=-0.5,
        url="https://example.com/outage-2",
        guid="outage-2",
    )
    storage.insert_items([item])

    # Mean return for 5m: +0.01, for 60m: -0.02
    storage.apply_feature_updates(
        {
            ("__bias__", 5): (10, 0.10),
            ("__bias__", 60): (10, -0.20),
        }
    )

    cfg = {
        "trigger_multipliers": {"outage": 1.2},
        "decay": {"half_life_minutes": 720},
        "learning": {
            "horizons": ["5m", "60m"],
            "market": {"endpoint": "https://api.binance.com", "symbol": "BTCUSDT"},
            "model": {
                "confidence_k": 0.0,
                "window_horizon_mix": {
                    "60m": {"5m": 0.75, "60m": 0.25},
                },
            },
            "arrow": {
                "scale": 100.0,
                "thresholds": {"60m": 0.1, "default": 0.1},
            },
        },
    }
    learner = Learner(cfg, price_client=FakePriceClient({}))
    stored_item = storage.get_items_since(datetime(2026, 2, 7, 11, 0, tzinfo=UTC))[0]
    pred = learner.predict_item_return_mixed(storage, stored_item, window_minutes=60)

    # 0.75 * 0.01 + 0.25 * (-0.02) = 0.0025
    assert round(pred, 6) == 0.0025

    storage.close()


def test_feature_keys_include_scoring_and_keyword_signals():
    cfg = _config()
    cfg["hybrid"] = {"keywords_strong": ["bitcoin", "exchange", "etf"]}
    learner = Learner(cfg, price_client=FakePriceClient({}))

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
        source="coinbase_status",
        title="Bitcoin exchange outage update",
        summary="Core API latency for 12 minutes",
        category="exchange_incident",
        polarity=-1,
        impact=-0.6,
        raw={
            "scoring": {
                "resolved_direction": -1,
                "btc_relevance_multiplier": 0.82,
                "category_multiplier": 1.25,
                "trigger_multiplier": 1.30,
            }
        },
    )

    features = learner._feature_keys(item)
    assert "source_group:exchange_status" in features
    assert "impact_sign:neg" in features
    assert "rule_direction:-1" in features
    assert "relevance:high" in features
    assert "category_mult:high" in features
    assert "trigger_mult:high" in features
    assert "has_number:1" in features
    assert "text_kw:bitcoin" in features
    assert "text_kw:exchange" in features


def test_learning_update_clips_extreme_returns():
    db = ":memory:"
    storage = Storage(db)

    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)
    ts = now - timedelta(hours=2)
    item = NewsItem(
        timestamp_utc=ts,
        source="fed",
        title="Macro shock",
        summary="",
        category="macro",
        polarity=1,
        impact=0.4,
        url="https://example.com/macro-shock",
        guid="macro-shock-1",
    )
    storage.insert_items([item])

    start_m = to_minute_epoch(ts)
    end_m = start_m + 60 * 60
    # +50% return within 60m; this should be clipped.
    price_client = FakePriceClient({start_m: 100.0, end_m: 150.0})
    cfg = _config()
    cfg["learning"]["model"]["label_return_clip_abs"] = 0.05
    learner = Learner(cfg, price_client=price_client)

    summary = learner.update_model(storage=storage, limit_per_horizon=100, now=now)
    assert summary["labeled"] == 1
    assert summary["clipped_labels"] == 1

    labeled = storage.get_labeled_items(horizon_minutes=60, since_ts=now - timedelta(days=1))
    assert len(labeled) == 1
    assert round(labeled[0][2], 6) == 0.05

    storage.close()


def test_learning_update_volatility_adjust_reduces_label_when_regime_is_noisy(tmp_path):
    db = tmp_path / "learn_vol_adjust.db"
    storage = Storage(db)

    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)
    ts = now - timedelta(hours=2)
    item = NewsItem(
        timestamp_utc=ts,
        source="fed",
        title="Macro surprise event",
        summary="",
        category="macro",
        polarity=1,
        impact=0.5,
        url="https://example.com/macro-surprise",
        guid="macro-surprise-1",
    )
    storage.insert_items([item])

    start_m = to_minute_epoch(ts)
    end_m = start_m + 60 * 60
    # Very noisy trailing returns before the event timestamp.
    points = {
        start_m - 6 * 60: 100.0,
        start_m - 5 * 60: 112.0,
        start_m - 4 * 60: 95.0,
        start_m - 3 * 60: 108.0,
        start_m - 2 * 60: 91.0,
        start_m - 1 * 60: 103.0,
        start_m: 100.0,
        end_m: 110.0,  # raw return +10%
    }
    cfg = _config()
    cfg["learning"]["model"].update(
        {
            "label_volatility_adjust": {
                "enabled": True,
                "window_minutes": 6,
                "min_returns": 5,
                "target_volatility": 0.01,
                "floor_volatility": 0.001,
                "min_scale": 0.1,
                "max_scale": 1.0,
            }
        }
    )
    learner = Learner(cfg, price_client=FakePriceClient(points))

    summary = learner.update_model(storage=storage, limit_per_horizon=100, now=now)
    assert summary["labeled"] == 1
    assert summary["vol_adjusted"] == 1

    labeled = storage.get_labeled_items(horizon_minutes=60, since_ts=now - timedelta(days=1))
    assert len(labeled) == 1
    assert labeled[0][2] < 0.1
    storage.close()


def test_learning_update_deadzone_zeroes_small_label(tmp_path):
    db = tmp_path / "learn_deadzone.db"
    storage = Storage(db)

    now = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)
    ts = now - timedelta(hours=2)
    item = NewsItem(
        timestamp_utc=ts,
        source="fed",
        title="Minor macro update",
        summary="",
        category="macro",
        polarity=1,
        impact=0.2,
        url="https://example.com/macro-minor",
        guid="macro-minor-1",
    )
    storage.insert_items([item])

    start_m = to_minute_epoch(ts)
    end_m = start_m + 60 * 60
    cfg = _config()
    cfg["learning"]["model"]["label_deadzone_abs"] = 0.02
    learner = Learner(cfg, price_client=FakePriceClient({start_m: 100.0, end_m: 101.0}))

    summary = learner.update_model(storage=storage, limit_per_horizon=100, now=now)
    assert summary["labeled"] == 1
    assert summary["deadzone_zeroed"] == 1

    labeled = storage.get_labeled_items(horizon_minutes=60, since_ts=now - timedelta(days=1))
    assert len(labeled) == 1
    assert labeled[0][2] == 0.0
    storage.close()
