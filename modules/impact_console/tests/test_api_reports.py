from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from btc_news_arrow.api import create_app


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_reports_endpoints_return_latest_and_history(tmp_path):
    hybrid_latest = tmp_path / "diagnostics" / "hybrid_eval_latest.json"
    source_latest = tmp_path / "diagnostics" / "source_quality_latest.json"
    alerts_latest = tmp_path / "diagnostics" / "alerts_latest.json"
    hybrid_hist = tmp_path / "diagnostics" / "hybrid_eval_history"
    source_hist = tmp_path / "diagnostics" / "source_quality_history"
    alerts_hist = tmp_path / "diagnostics" / "alerts_history"

    _write_json(
        hybrid_latest,
        {
            "generated_at_utc": "2026-02-13T12:00:00+00:00",
            "current": {"ok": True},
            "trend": {"status": "flat"},
        },
    )
    _write_json(
        source_latest,
        {
            "generated_at_utc": "2026-02-13T12:00:00+00:00",
            "current": {"overall": {"samples_total": 10}},
            "trend": {"status": "improved"},
        },
    )
    _write_json(
        alerts_latest,
        {
            "generated_at_utc": "2026-02-13T12:00:00+00:00",
            "ok": False,
            "alerts_total": 1,
            "alerts": [{"severity": "high", "code": "low_item_volume", "message": "x", "details": {}}],
        },
    )
    _write_json(hybrid_hist / "hybrid_eval_1.json", {"generated_at_utc": "2026-02-13T11:00:00+00:00"})
    _write_json(source_hist / "source_quality_1.json", {"generated_at_utc": "2026-02-13T11:00:00+00:00"})
    _write_json(alerts_hist / "alerts_1.json", {"generated_at_utc": "2026-02-13T11:00:00+00:00"})

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "llm": {"enabled": False, "require_runtime": False},
                "reports": {
                    "hybrid_report_path": str(hybrid_latest),
                    "hybrid_history_dir": str(hybrid_hist),
                    "source_quality_report_path": str(source_latest),
                    "source_quality_history_dir": str(source_hist),
                    "alerts_report_path": str(alerts_latest),
                    "alerts_history_dir": str(alerts_hist),
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    app = create_app(config_path=str(cfg_path), db_path=str(tmp_path / "db.sqlite"))
    client = TestClient(app)

    summary = client.get("/reports/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["hybrid"]["available"] is True
    assert payload["source_quality"]["available"] is True
    assert payload["alerts"]["available"] is True
    assert payload["alerts"]["alerts_total"] == 1

    hybrid = client.get("/reports/hybrid?include_history=true&limit=5")
    assert hybrid.status_code == 200
    hybrid_payload = hybrid.json()
    assert hybrid_payload["latest"]["trend"]["status"] == "flat"
    assert len(hybrid_payload["history"]) == 1

    alerts = client.get("/reports/alerts")
    assert alerts.status_code == 200
    alerts_payload = alerts.json()
    assert alerts_payload["latest"]["alerts_total"] == 1


def test_report_endpoint_returns_404_when_missing(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "llm": {"enabled": False, "require_runtime": False},
                "reports": {
                    "hybrid_report_path": str(tmp_path / "missing_hybrid.json"),
                },
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    app = create_app(config_path=str(cfg_path), db_path=str(tmp_path / "db.sqlite"))
    client = TestClient(app)

    resp = client.get("/reports/hybrid")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "hybrid_report_missing"
