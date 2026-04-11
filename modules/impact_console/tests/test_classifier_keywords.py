from datetime import UTC, datetime

from btc_news_arrow.classifier import Classifier
from btc_news_arrow.models import NewsItem


def test_single_token_keyword_uses_word_boundaries():
    cfg = {
        "keyword_rules": {
            "categories": {
                "geopolitics_risk": ["war"],
                "exchange_incident": ["transactions"],
            },
            "polarity": {"positive": [], "negative": []},
            "category_bias": {"exchange_incident": -1},
        }
    }
    classifier = Classifier(cfg)

    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 12, 18, 7, tzinfo=UTC),
        source="coinbase_status",
        title="Site Performance - Transactions",
        summary="We are aware that customers may be unable to buy, sell, transfer.",
    )

    classified = classifier.classify(item)

    assert classified.category == "exchange_incident"


def test_negated_positive_keyword_flips_polarity():
    cfg = {
        "keyword_rules": {
            "categories": {"regulation": ["sec", "etf"]},
            "polarity": {"positive": ["approved"], "negative": ["rejected"]},
            "negation_terms": ["not"],
            "category_bias": {"regulation": 0},
        }
    }
    classifier = Classifier(cfg)
    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 12, 18, 7, tzinfo=UTC),
        source="sec",
        title="SEC says ETF not approved yet",
        summary="",
    )

    classified = classifier.classify(item)
    assert classified.category == "regulation"
    assert classified.polarity == -1


def test_regulation_proposal_with_uncertainty_abstains():
    cfg = {
        "keyword_rules": {
            "categories": {"regulation": ["sec"]},
            "categories_contextual": {"regulation": ["proposal", "rumor"]},
            "polarity": {"positive": ["approved"], "negative": ["lawsuit"]},
            "uncertainty_terms": ["rumor"],
            "regulation_preliminary_terms": ["proposal"],
            "regulation_final_terms": ["approved", "rejected"],
            "category_bias": {"regulation": 0},
        }
    }
    classifier = Classifier(cfg)
    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 12, 18, 7, tzinfo=UTC),
        source="sec",
        title="SEC proposal rumored for digital asset listing rules",
        summary="Market rumor only, unconfirmed",
    )

    classified = classifier.classify(item)
    assert classified.category == "regulation"
    assert classified.polarity == 0


def test_negation_terms_handles_yaml_bool_false_from_unquoted_no():
    cfg = {
        "keyword_rules": {
            "categories": {"regulation": ["sec"]},
            "polarity": {"positive": ["approved"], "negative": ["rejected"]},
            # Simulates YAML parsing of unquoted `no` into bool False.
            "negation_terms": [False, "not"],
            "category_bias": {"regulation": 0},
        }
    }
    classifier = Classifier(cfg)
    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 14, 0, 35, tzinfo=UTC),
        source="sec",
        title="SEC says ETF no approved decision yet",
        summary="",
    )

    classified = classifier.classify(item)
    assert classified.category == "regulation"
    assert classified.polarity == -1


def test_plural_etfs_matches_etf_category_keyword():
    cfg = {
        "keyword_rules": {
            "categories": {"etf_institutional": ["etf"]},
            "polarity": {"positive": [], "negative": []},
            "category_bias": {"etf_institutional": 1},
        }
    }
    classifier = Classifier(cfg)
    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 14, 9, 0, tzinfo=UTC),
        source="cointelegraph",
        title="Issuer files two new crypto ETFs",
        summary="",
    )

    classified = classifier.classify(item)
    assert classified.category == "etf_institutional"
    assert classified.polarity == 1


def test_loss_term_sets_negative_polarity():
    cfg = {
        "keyword_rules": {
            "categories": {"other": ["coinbase"]},
            "polarity": {"positive": [], "negative": ["loss"]},
            "category_bias": {"other": 0},
        }
    }
    classifier = Classifier(cfg)
    item = NewsItem(
        timestamp_utc=datetime(2026, 2, 14, 9, 0, tzinfo=UTC),
        source="gdelt:finance.yahoo.com",
        title="Coinbase posts surprise losses on trading slowdown",
        summary="",
    )

    classified = classifier.classify(item)
    assert classified.polarity == -1
