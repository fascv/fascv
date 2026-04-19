from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from btc_news_arrow.api import (
    _clip_component_score,
    _combine_weighted_scores,
    _coverage_relevance,
    _load_hybrid_config,
    _signal_state_from_metrics,
)
from btc_news_arrow.models import NewsItem


def _item_with_relevance(mult: float) -> NewsItem:
    return NewsItem(
        timestamp_utc=datetime(2026, 2, 12, 12, 0, tzinfo=UTC),
        source="coinbase_status",
        title="Sample",
        raw={"scoring": {"btc_relevance_multiplier": mult}},
    )


def test_signal_state_reports_no_signal_when_coverage_is_thin():
    state = _signal_state_from_metrics(
        score=-0.8,
        threshold=0.45,
        contributing_items=2,
        relevance_sum=0.9,
        min_items=3,
        min_relevance_sum=1.2,
    )
    assert state == "no_signal"


def test_signal_state_uses_score_when_coverage_is_sufficient():
    state = _signal_state_from_metrics(
        score=-0.8,
        threshold=0.45,
        contributing_items=4,
        relevance_sum=2.0,
        min_items=3,
        min_relevance_sum=1.2,
    )
    assert state == "risk_off"


def test_signal_state_abstains_on_component_disagreement():
    state = _signal_state_from_metrics(
        score=0.3,
        threshold=0.25,
        contributing_items=5,
        relevance_sum=2.0,
        min_items=3,
        min_relevance_sum=1.2,
        disagreement=True,
        disagreement_score_abs_factor=1.5,
    )
    assert state == "no_signal"


def test_coverage_relevance_uses_item_scoring_multiplier():
    cfg = _load_hybrid_config({})
    contributions = [
        (-0.1, _item_with_relevance(0.8)),
        (-0.2, _item_with_relevance(0.4)),
    ]
    assert _coverage_relevance(contributions, cfg) == pytest.approx(1.2)


def test_combine_weighted_scores_normalizes_available_components():
    score, weights = _combine_weighted_scores(
        score_components={"rule": -1.0, "learn": -0.5},
        target_weights={"rule": 0.5, "llm": 0.35, "learn": 0.15},
    )

    assert score == pytest.approx(-0.8846153846)
    assert weights["rule"] == pytest.approx(0.5 / 0.65)
    assert weights["learn"] == pytest.approx(0.15 / 0.65)
    assert "llm" not in weights


def test_load_hybrid_config_exposes_learn_weight():
    cfg = _load_hybrid_config({"hybrid": {"rule_weight": 0.5, "llm_weight": 0.3, "learn_weight": 0.2}})
    assert cfg["score_weights"]["rule"] == pytest.approx(0.5)
    assert cfg["score_weights"]["llm"] == pytest.approx(0.3)
    assert cfg["score_weights"]["learn"] == pytest.approx(0.2)


def test_clip_component_score_applies_limits():
    cfg = _load_hybrid_config({"hybrid": {"score_clip_abs": {"learn": 2.0}}})
    assert _clip_component_score("learn", -3.244, cfg) == pytest.approx(-2.0)
    assert _clip_component_score("learn", -1.2, cfg) == pytest.approx(-1.2)
