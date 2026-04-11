from datetime import UTC, datetime

import pytest
import yaml

from btc_news_arrow.api import _llm_http_exception, _llm_max_db_id, _resolve_llm_result, create_app
from btc_news_arrow.models import ArrowResult, NewsItem


def test_llm_timeout_maps_to_504():
    exc = RuntimeError("OpenAI request timed out (APITimeoutError)")
    http_exc = _llm_http_exception(exc)
    assert http_exc.status_code == 504


def test_llm_config_error_maps_to_400():
    exc = RuntimeError("OPENAI_API_KEY is not set")
    http_exc = _llm_http_exception(exc)
    assert http_exc.status_code == 400


class _FakePayload:
    def __init__(self, score: float = 0.4) -> None:
        self.result = ArrowResult(window_minutes=60, score=score, arrow="▲", contributing_items=1)
        self.confidence = 0.7
        self.notes = "ok"
        self.meta = {"used_model": "fake"}
        self.reasons = []


class _FakeRater:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def rate_window(self, items, window_minutes, now):  # noqa: ANN001
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic llm failure")
        return _FakePayload()


def test_resolve_llm_result_short_window_uses_rule():
    resolved = _resolve_llm_result(
        llm_rater=_FakeRater(),
        items=[],
        minutes=30,
        now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        llm_has_new_items=True,
        llm_last_payload={},
        llm_cache={},
        llm_cooldown_until={},
        llm_cache_ttl_seconds=0,
        llm_cooldown_seconds=0,
        llm_min_window_minutes=60,
        llm_short_window_mode="rule",
        llm_fallback_to_rule_on_error=False,
        default_mode_effective="llm",
    )
    assert resolved["use_rule_result"] is True
    assert resolved["mode_effective"] == "rule_short_window"


def test_resolve_llm_result_error_falls_back_to_rule_when_enabled():
    resolved = _resolve_llm_result(
        llm_rater=_FakeRater(fail=True),
        items=[],
        minutes=60,
        now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        llm_has_new_items=True,
        llm_last_payload={},
        llm_cache={},
        llm_cooldown_until={},
        llm_cache_ttl_seconds=0,
        llm_cooldown_seconds=0,
        llm_min_window_minutes=60,
        llm_short_window_mode="rule",
        llm_fallback_to_rule_on_error=True,
        default_mode_effective="llm",
    )
    assert resolved["use_rule_result"] is True
    assert resolved["mode_effective"] == "rule_fallback"
    assert "synthetic llm failure" in str(resolved["warning"])


def test_resolve_llm_result_auto_mode_skips_request_without_new_items():
    rater = _FakeRater()
    resolved = _resolve_llm_result(
        llm_rater=rater,
        items=[],
        minutes=60,
        now=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
        llm_has_new_items=False,
        llm_last_payload={},
        llm_cache={},
        llm_cooldown_until={},
        llm_cache_ttl_seconds=0,
        llm_cooldown_seconds=600,
        llm_min_window_minutes=60,
        llm_short_window_mode="rule",
        llm_fallback_to_rule_on_error=True,
        default_mode_effective="rule",
    )
    assert resolved["use_rule_result"] is True
    assert resolved["mode_effective"] == "llm_no_new_items"
    assert "No new material items" in str(resolved["warning"])
    assert rater.calls == 0


def test_llm_max_db_id_ignores_zero_impact_items_when_threshold_is_positive():
    items = [
        NewsItem(
            timestamp_utc=datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
            source="x",
            title="A",
            impact=0.0,
            raw={"_db_id": 100},
        ),
        NewsItem(
            timestamp_utc=datetime(2026, 2, 13, 12, 1, tzinfo=UTC),
            source="x",
            title="B",
            impact=0.2,
            raw={"_db_id": 80},
        ),
    ]
    assert _llm_max_db_id(items, min_abs_impact=1e-6) == 80
    assert _llm_max_db_id(items, min_abs_impact=0.0) == 100


def test_create_app_allows_degraded_start_when_configured(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "enabled": True,
                    "require_runtime": True,
                    "allow_degraded_start": True,
                }
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    def _raise(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("OPENAI_API_KEY is not set")

    monkeypatch.setattr("btc_news_arrow.llm_rater.LLMRater.ensure_available", _raise)
    app = create_app(config_path=str(cfg_path), db_path=str(tmp_path / "test.db"))
    assert app is not None


def test_create_app_requires_runtime_without_degraded_mode(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "enabled": True,
                    "require_runtime": True,
                    "allow_degraded_start": False,
                }
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    def _raise(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("OPENAI_API_KEY is not set")

    monkeypatch.setattr("btc_news_arrow.llm_rater.LLMRater.ensure_available", _raise)
    with pytest.raises(RuntimeError):
        create_app(config_path=str(cfg_path), db_path=str(tmp_path / "test.db"))
