from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from btc_news_arrow.api import _regime_from_score, _select_reasons
from btc_news_arrow.models import NewsItem


def _item(title: str) -> NewsItem:
    return NewsItem(
        timestamp_utc=datetime(2026, 2, 7, 0, 0, tzinfo=UTC),
        source="src",
        title=title,
        category="macro",
    )


def test_select_reasons_for_negative_direction_returns_most_negative_first():
    c = [
        (-0.1, _item("a")),
        (-0.4, _item("b")),
        (0.6, _item("c")),
        (-0.2, _item("d")),
    ]

    reasons = _select_reasons(c, direction=-1, limit=2)

    assert [r["title"] for r in reasons] == ["b", "d"]


def test_select_reasons_for_neutral_uses_absolute_contribution():
    c = [
        (-0.1, _item("a")),
        (0.3, _item("b")),
        (-0.2, _item("c")),
    ]

    reasons = _select_reasons(c, direction=0, limit=2)

    assert [r["title"] for r in reasons] == ["b", "c"]


def test_regime_from_score_threshold_mapping():
    assert _regime_from_score(0.31, 0.3) == "risk_on"
    assert _regime_from_score(-0.31, 0.3) == "risk_off"
    assert _regime_from_score(0.1, 0.3) == "neutral"
