from datetime import UTC, datetime

from btc_news_arrow.hybrid_report import generate_hybrid_eval_report


def _summary(corr: float, dir_acc: float, mae: float) -> dict:
    return {
        "ok": True,
        "samples": 120,
        "weights_non_llm": {"rule_share": 0.6, "learn_share": 0.4},
        "by_window": {"1h": 60, "24h": 60},
        "metrics": {
            "global": {
                "hybrid": {
                    "corr": corr,
                    "directional_accuracy": dir_acc,
                    "mae": mae,
                }
            },
            "by_window": {
                "1h": {"hybrid": {"corr": corr, "directional_accuracy": dir_acc, "mae": mae}},
                "24h": {"hybrid": {"corr": corr / 2.0, "directional_accuracy": dir_acc, "mae": mae * 1.5}},
            },
        },
    }


def test_generate_hybrid_report_first_run_has_no_previous(tmp_path, monkeypatch):
    report_path = tmp_path / "latest.json"
    history_dir = tmp_path / "history"

    monkeypatch.setattr(
        "btc_news_arrow.hybrid_report.evaluate_hybrid_model",
        lambda **_: _summary(corr=0.12, dir_acc=0.56, mae=0.021),
    )
    monkeypatch.setattr(
        "btc_news_arrow.hybrid_report.utcnow",
        lambda: datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
    )

    report = generate_hybrid_eval_report(
        config_path="config.yaml",
        db_path="smoke.db",
        windows=["1h", "24h"],
        lookback_days=30,
        min_samples=120,
        report_path=report_path,
        history_dir=history_dir,
        keep_history=False,
    )

    assert report_path.exists()
    assert report["previous"] is None
    assert report["trend"]["status"] == "unavailable"


def test_generate_hybrid_report_computes_delta_and_trend(tmp_path, monkeypatch):
    report_path = tmp_path / "latest.json"
    history_dir = tmp_path / "history"
    calls = {"n": 0}

    def fake_eval(**_):
        calls["n"] += 1
        if calls["n"] == 1:
            return _summary(corr=0.10, dir_acc=0.55, mae=0.025)
        return _summary(corr=0.14, dir_acc=0.58, mae=0.020)

    monkeypatch.setattr("btc_news_arrow.hybrid_report.evaluate_hybrid_model", fake_eval)
    monkeypatch.setattr(
        "btc_news_arrow.hybrid_report.utcnow",
        lambda: datetime(2026, 2, 13, 12, 0, tzinfo=UTC),
    )

    generate_hybrid_eval_report(
        config_path="config.yaml",
        db_path="smoke.db",
        report_path=report_path,
        history_dir=history_dir,
        keep_history=False,
    )
    report = generate_hybrid_eval_report(
        config_path="config.yaml",
        db_path="smoke.db",
        report_path=report_path,
        history_dir=history_dir,
        keep_history=False,
    )

    global_delta = report["delta"]["global"]
    assert round(global_delta["hybrid_corr"]["delta"], 6) == 0.04
    assert round(global_delta["hybrid_directional_accuracy"]["delta"], 6) == 0.03
    assert round(global_delta["hybrid_mae"]["delta"], 6) == -0.005
    assert report["trend"]["status"] == "improved"
