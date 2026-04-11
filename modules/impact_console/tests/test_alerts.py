from datetime import UTC, datetime, timedelta
import json

from btc_news_arrow.alerts import run_alert_checks, send_webhook, should_fail
from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage


def _insert_item(storage: Storage, *, ts: datetime, source: str, title: str, guid: str) -> None:
    storage.insert_items(
        [
            NewsItem(
                timestamp_utc=ts,
                source=source,
                title=title,
                summary="",
                guid=guid,
                url=f"https://example.com/{guid}",
            )
        ]
    )


def _write_hybrid_report(path, *, trend_status: str = "flat", ok: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-02-13T12:00:00+00:00",
                "current": {"ok": ok, "samples": 120},
                "trend": {"status": trend_status},
            }
        ),
        encoding="utf-8",
    )


def _write_source_quality_report(path, *, trend_status: str = "flat", corr_delta: float | None = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at_utc": "2026-02-13T12:00:00+00:00",
                "current": {"overall": {"samples_total": 120}},
                "trend": {"status": trend_status},
                "delta": {
                    "by_window": {
                        "1h": {
                            "global_corr_delta": corr_delta,
                            "global_mae_delta": 0.0,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_alerts_check_no_fresh_items_is_critical(tmp_path):
    now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
    db = tmp_path / "alerts_critical.db"
    storage = Storage(db)
    try:
        _insert_item(
            storage,
            ts=now - timedelta(hours=2),
            source="fed",
            title="old item",
            guid="old-1",
        )
    finally:
        storage.close()

    hybrid_report = tmp_path / "hybrid_latest.json"
    _write_hybrid_report(hybrid_report, trend_status="flat", ok=True)

    summary = run_alert_checks(
        db_path=db,
        hybrid_report_path=hybrid_report,
        freshness_minutes=30,
        window_minutes=60,
        min_items_threshold=1,
        output_path=tmp_path / "alerts_latest.json",
        history_dir=tmp_path / "history",
        keep_history=False,
        now=now,
    )

    codes = {a["code"] for a in summary["alerts"]}
    assert "no_fresh_items" in codes
    assert should_fail(summary, {"critical"}) is True


def test_alerts_check_detects_source_concentration_drift(tmp_path):
    now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
    db = tmp_path / "alerts_drift.db"
    storage = Storage(db)
    try:
        for i in range(10):
            _insert_item(
                storage,
                ts=now - timedelta(minutes=5),
                source="gdelt:dominant",
                title=f"item-{i}",
                guid=f"dom-{i}",
            )
        for i in range(2):
            _insert_item(
                storage,
                ts=now - timedelta(minutes=6),
                source="fed",
                title=f"alt-{i}",
                guid=f"alt-{i}",
            )
    finally:
        storage.close()

    hybrid_report = tmp_path / "hybrid_latest.json"
    _write_hybrid_report(hybrid_report, trend_status="flat", ok=True)

    summary = run_alert_checks(
        db_path=db,
        hybrid_report_path=hybrid_report,
        freshness_minutes=30,
        window_minutes=60,
        min_items_threshold=3,
        source_concentration_threshold=0.8,
        output_path=tmp_path / "alerts_latest.json",
        history_dir=tmp_path / "history",
        keep_history=False,
        now=now,
    )

    drift = [a for a in summary["alerts"] if a["severity"] == "drift"]
    assert drift
    assert drift[0]["code"] == "source_concentration_high"


def test_alerts_check_detects_hybrid_degraded_streak(tmp_path):
    now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
    db = tmp_path / "alerts_quality.db"
    storage = Storage(db)
    try:
        for i in range(4):
            _insert_item(
                storage,
                ts=now - timedelta(minutes=5),
                source="fed",
                title=f"recent-{i}",
                guid=f"recent-{i}",
            )
    finally:
        storage.close()

    history_dir = tmp_path / "hybrid_hist"
    _write_hybrid_report(history_dir / "hybrid_eval_20260213T100000Z.json", trend_status="degraded", ok=True)
    _write_hybrid_report(history_dir / "hybrid_eval_20260213T110000Z.json", trend_status="degraded", ok=True)
    latest = tmp_path / "hybrid_latest.json"
    _write_hybrid_report(latest, trend_status="degraded", ok=True)

    summary = run_alert_checks(
        db_path=db,
        hybrid_report_path=latest,
        hybrid_history_dir=history_dir,
        freshness_minutes=30,
        window_minutes=60,
        min_items_threshold=3,
        hybrid_degraded_streak=2,
        output_path=tmp_path / "alerts_latest.json",
        history_dir=tmp_path / "alerts_hist",
        keep_history=False,
        now=now,
    )

    quality = [a for a in summary["alerts"] if a["severity"] == "quality"]
    assert quality
    assert any(a["code"] == "hybrid_trend_degraded_streak" for a in quality)
    assert should_fail(summary, {"quality"}) is True


def test_alerts_check_detects_source_quality_regression(tmp_path):
    now = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
    db = tmp_path / "alerts_source_quality.db"
    storage = Storage(db)
    try:
        for i in range(5):
            _insert_item(
                storage,
                ts=now - timedelta(minutes=5),
                source="fed",
                title=f"recent-{i}",
                guid=f"recent-source-{i}",
            )
    finally:
        storage.close()

    hybrid_report = tmp_path / "hybrid_latest.json"
    _write_hybrid_report(hybrid_report, trend_status="flat", ok=True)
    source_history = tmp_path / "source_quality_hist"
    _write_source_quality_report(source_history / "source_quality_20260213T100000Z.json", trend_status="degraded", corr_delta=-0.02)
    _write_source_quality_report(source_history / "source_quality_20260213T110000Z.json", trend_status="degraded", corr_delta=-0.03)
    source_latest = tmp_path / "source_quality_latest.json"
    _write_source_quality_report(source_latest, trend_status="degraded", corr_delta=-0.15)

    summary = run_alert_checks(
        db_path=db,
        hybrid_report_path=hybrid_report,
        hybrid_history_dir=tmp_path / "hybrid_hist",
        source_quality_report_path=source_latest,
        source_quality_history_dir=source_history,
        freshness_minutes=30,
        window_minutes=60,
        min_items_threshold=3,
        source_quality_degraded_streak=2,
        source_quality_corr_drop_threshold=0.05,
        output_path=tmp_path / "alerts_latest.json",
        history_dir=tmp_path / "alerts_hist",
        keep_history=False,
        now=now,
    )

    codes = {a["code"] for a in summary["alerts"]}
    assert "source_quality_trend_degraded_streak" in codes
    assert "source_quality_corr_drop" in codes


def test_send_webhook_retries_then_cooldown_blocks_duplicate(tmp_path, monkeypatch):
    summary = {
        "generated_at_utc": "2026-02-13T12:00:00+00:00",
        "ok": False,
        "alerts_total": 1,
        "severity_counts": {"critical": 1},
        "alerts": [
            {
                "severity": "critical",
                "code": "no_fresh_items",
                "message": "No fresh items",
                "details": {"fresh_items": 0},
            }
        ],
    }

    class _Resp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    state_path = tmp_path / "webhook_state.json"
    calls = {"n": 0}

    def _urlopen(_req, timeout):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("temporary failure")
        assert timeout == 2
        return _Resp()

    monkeypatch.setattr("btc_news_arrow.alerts.request.urlopen", _urlopen)
    first = send_webhook(
        webhook_url="https://example.com/hook",
        summary=summary,
        send_on={"critical"},
        timeout_seconds=2,
        retries=2,
        retry_backoff_seconds=0,
        cooldown_seconds=300,
        state_path=state_path,
    )
    assert first["sent"] is True
    assert first["attempts"] == 2

    second = send_webhook(
        webhook_url="https://example.com/hook",
        summary=summary,
        send_on={"critical"},
        timeout_seconds=2,
        retries=2,
        retry_backoff_seconds=0,
        cooldown_seconds=300,
        state_path=state_path,
    )
    assert second["sent"] is False
    assert second["reason"] == "cooldown_active"
