from datetime import UTC, datetime
import json

from btc_news_arrow.classifier import Classifier
from btc_news_arrow.models import NewsItem
from btc_news_arrow.scorer import Scorer


def _config():
    return {
        "keyword_rules": {
            "categories": {
                "macro": ["inflation", "cpi", "ppi", "producer price index", "rate hike", "rate cut"],
                "exchange_incident": ["outage", "halt"],
                "regulation": ["sec", "regulation", "lawsuit", "approved", "proposal"],
            },
            "polarity": {
                "positive": ["rate cut", "resumed", "approved"],
                "negative": ["rate hike", "outage", "halt", "lawsuit"],
            },
            "category_bias": {
                "macro": 0,
                "exchange_incident": -1,
                "regulation": 0,
            },
        },
        "source_weights": {
            "fed": 1.0,
            "kraken_status": 0.8,
            "coinbase_status": 0.85,
            "sec": 1.0,
            "bls": 0.9,
            "bls_latest": 0.8,
            "bls_ppi": 0.9,
        },
        "category_weights": {
            "macro": 1.0,
            "exchange_incident": 0.9,
            "regulation": 1.0,
            "other": 0.5,
        },
        "trigger_multipliers": {
            "outage": 1.2,
        },
        "decay": {
            "half_life_minutes": 60,
        },
    }


def test_hawkish_macro_is_negative():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
        source="fed",
        title="Fed signals rate hike amid sticky inflation",
        summary="",
    )

    item = classifier.classify(item)
    item = scorer.score(item, now=datetime(2026, 2, 7, 0, 0, tzinfo=UTC))

    assert item.category == "macro"
    assert item.polarity == -1
    assert item.impact < 0


def test_exchange_outage_is_negative():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
        source="kraken_status",
        title="Temporary outage on trading API",
        summary="",
    )

    item = classifier.classify(item)
    item = scorer.score(item, now=datetime(2026, 2, 7, 0, 0, tzinfo=UTC))

    assert item.category == "exchange_incident"
    assert item.polarity == -1
    assert item.impact < 0


def test_score_is_raw_event_strength_not_time_decayed():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    item_old = NewsItem(
        timestamp_utc=datetime(2026, 2, 6, 0, 0, tzinfo=UTC),
        source="fed",
        title="Fed signals rate hike amid sticky inflation",
        summary="",
    )
    item_new = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
        source="fed",
        title="Fed signals rate hike amid sticky inflation",
        summary="",
    )

    item_old = scorer.score(classifier.classify(item_old), now=datetime(2026, 2, 7, 0, 0, tzinfo=UTC))
    item_new = scorer.score(classifier.classify(item_new), now=datetime(2026, 2, 7, 0, 0, tzinfo=UTC))

    assert item_old.impact == item_new.impact


def test_macro_surprise_is_stronger_than_in_line_release():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    surprise = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
        source="fed",
        title="US CPI came in higher than expected",
        summary="",
    )
    inline = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
        source="fed",
        title="US CPI came in as expected",
        summary="",
    )

    surprise = scorer.score(classifier.classify(surprise), now=datetime(2026, 2, 7, 12, 0, tzinfo=UTC))
    inline = scorer.score(classifier.classify(inline), now=datetime(2026, 2, 7, 12, 0, tzinfo=UTC))

    assert surprise.impact < 0
    assert abs(surprise.impact) > abs(inline.impact)


def test_incident_resolved_is_weaker_than_investigating():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    investigating = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 12, 0, tzinfo=UTC),
        source="kraken_status",
        title="Trading outage on API gateway",
        summary="Investigating - we are investigating client connectivity issues.",
    )
    resolved = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 12, 30, tzinfo=UTC),
        source="kraken_status",
        title="Trading outage on API gateway",
        summary="Resolved - this incident has been resolved.",
    )

    investigating = scorer.score(classifier.classify(investigating), now=datetime(2026, 2, 7, 12, 30, tzinfo=UTC))
    resolved = scorer.score(classifier.classify(resolved), now=datetime(2026, 2, 7, 12, 30, tzinfo=UTC))

    assert investigating.impact < 0
    assert resolved.impact < 0
    assert abs(resolved.impact) < abs(investigating.impact)


def test_regulation_finality_is_stronger_than_preliminary_note():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    final_item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
        source="sec",
        title="SEC approved a new crypto listing framework",
        summary="Order issued by the commission.",
    )
    preliminary_item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
        source="sec",
        title="SEC released a proposal for crypto market structure",
        summary="Proposal enters comment period.",
    )

    final_item = scorer.score(classifier.classify(final_item), now=datetime(2026, 2, 7, 13, 0, tzinfo=UTC))
    preliminary_item = scorer.score(
        classifier.classify(preliminary_item),
        now=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
    )

    assert final_item.impact > 0
    assert preliminary_item.impact >= 0
    assert abs(final_item.impact) > abs(preliminary_item.impact)


def test_non_crypto_regulation_is_dampened_for_btc():
    cfg = _config()
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
        source="fed",
        title="Federal Reserve Board issues enforcement action with former employee of Regions Bank",
        summary="",
        category="regulation",
        polarity=-1,
    )

    item = scorer.score(item, now=datetime(2026, 2, 7, 13, 0, tzinfo=UTC))

    assert item.impact <= 0
    assert abs(item.impact) <= 0.05


def test_altcoin_only_exchange_delay_is_dampened():
    cfg = _config()
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
        source="coinbase_status",
        title="Delayed Sends/Receives - Algorand (ALGO)",
        summary="Investigating delayed sends and receives for Algorand.",
        category="exchange_incident",
        polarity=-1,
    )

    item = scorer.score(item, now=datetime(2026, 2, 7, 13, 0, tzinfo=UTC))

    assert item.impact < 0
    assert abs(item.impact) < 0.4


def test_exchange_core_platform_issue_remains_material():
    cfg = _config()
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
        source="coinbase_status",
        title="Degraded performance on Coinbase transactions API",
        summary="Investigating transaction latency and API errors.",
        category="exchange_incident",
        polarity=-1,
    )

    item = scorer.score(item, now=datetime(2026, 2, 7, 13, 0, tzinfo=UTC))

    assert item.impact <= -0.6


def test_status_source_fallback_classifies_maintenance_as_exchange_incident():
    cfg = _config()
    classifier = Classifier(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 13, 0, tzinfo=UTC),
        source="kraken_status",
        title="Kraken Website and API Maintenance",
        summary="Monitoring and investigating connectivity.",
    )

    item = classifier.classify(item)

    assert item.category == "exchange_incident"


def test_bls_cpi_numeric_macro_signal_turns_positive():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 13, 8, 30, tzinfo=UTC),
        source="bls",
        title="CPI for all items rises 0.2% in January; shelter up",
        summary=(
            "In January, the Consumer Price Index for All Urban Consumers rose 0.2 percent, "
            "seasonally adjusted. The index for all items less food and energy increased 0.3 percent."
        ),
    )

    item = scorer.score(classifier.classify(item), now=datetime(2026, 2, 13, 8, 31, tzinfo=UTC))

    assert item.category == "macro"
    assert item.impact > 0


def test_bls_ppi_hot_print_turns_negative():
    cfg = _config()
    classifier = Classifier(cfg)
    scorer = Scorer(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 1, 30, 8, 30, tzinfo=UTC),
        source="bls_ppi",
        title="PPI for final demand advances 0.5% in December",
        summary="Producer Price Index for final demand advanced 0.5 percent in December.",
    )

    item = scorer.score(classifier.classify(item), now=datetime(2026, 1, 30, 8, 31, tzinfo=UTC))

    assert item.category == "macro"
    assert item.impact < 0


def test_source_quality_weighting_adjusts_source_strength(tmp_path):
    report_path = tmp_path / "source_quality_latest.json"
    report_path.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-02-13T12:00:00+00:00",
                "current": {
                    "by_window": {
                        "1h": {
                            "sources": [
                                {"source": "src_good", "samples": 50, "corr": 0.6, "directional_accuracy": 0.8},
                                {"source": "src_bad", "samples": 50, "corr": -0.6, "directional_accuracy": 0.2},
                            ]
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = _config()
    cfg["source_weights"] = {"src_good": 1.0, "src_bad": 1.0}
    cfg["source_quality_weighting"] = {
        "enabled": True,
        "report_path": str(report_path),
        "window": "1h",
        "min_samples": 8,
        "corr_weight": 0.4,
        "directional_weight": 0.3,
        "min_multiplier": 0.7,
        "max_multiplier": 1.3,
    }
    scorer = Scorer(cfg)

    good = NewsItem(
        timestamp_utc=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        source="src_good",
        title="risk on bitcoin outlook",
        summary="approved and resumed",
        category="other",
        polarity=1,
    )
    bad = NewsItem(
        timestamp_utc=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        source="src_bad",
        title="risk on bitcoin outlook",
        summary="approved and resumed",
        category="other",
        polarity=1,
    )

    good = scorer.score(good, now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))
    bad = scorer.score(bad, now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC))

    assert good.impact > bad.impact
    assert good.raw["scoring"]["source_quality_factor"] > 1.0
    assert bad.raw["scoring"]["source_quality_factor"] < 1.0
