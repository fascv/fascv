from btc_news_arrow.scorer import recency_decay


def test_recency_decay_half_life():
    half_life_minutes = 30
    assert recency_decay(0, half_life_minutes) == 1.0

    # One half-life later should be 0.5
    val = recency_decay(30 * 60, half_life_minutes)
    assert round(val, 6) == 0.5

    # Two half-lives later should be 0.25
    val2 = recency_decay(60 * 60, half_life_minutes)
    assert round(val2, 6) == 0.25
