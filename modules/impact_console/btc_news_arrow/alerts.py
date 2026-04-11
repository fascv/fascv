from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error as url_error
from urllib import request

from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import ensure_utc, parse_datetime, utcnow


DEFAULT_HYBRID_REPORT_PATH = Path("diagnostics/hybrid_eval_latest.json")
DEFAULT_ALERTS_PATH = Path("diagnostics/alerts_latest.json")
DEFAULT_ALERTS_HISTORY_DIR = Path("diagnostics/alerts_history")
DEFAULT_HYBRID_HISTORY_DIR = Path("diagnostics/hybrid_eval_history")
DEFAULT_SOURCE_QUALITY_REPORT_PATH = Path("diagnostics/source_quality_latest.json")
DEFAULT_SOURCE_QUALITY_HISTORY_DIR = Path("diagnostics/source_quality_history")
DEFAULT_WEBHOOK_STATE_PATH = Path("diagnostics/webhook_state.json")


def run_alert_checks(
    *,
    db_path: str | Path,
    hybrid_report_path: str | Path = DEFAULT_HYBRID_REPORT_PATH,
    hybrid_history_dir: str | Path = DEFAULT_HYBRID_HISTORY_DIR,
    source_quality_report_path: str | Path = DEFAULT_SOURCE_QUALITY_REPORT_PATH,
    source_quality_history_dir: str | Path = DEFAULT_SOURCE_QUALITY_HISTORY_DIR,
    freshness_minutes: int = 30,
    window_minutes: int = 60,
    min_items_threshold: int = 3,
    source_concentration_threshold: float = 0.85,
    hybrid_degraded_streak: int = 2,
    source_quality_degraded_streak: int = 2,
    source_quality_corr_drop_threshold: float = 0.05,
    output_path: str | Path = DEFAULT_ALERTS_PATH,
    history_dir: str | Path = DEFAULT_ALERTS_HISTORY_DIR,
    keep_history: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    run_now = ensure_utc(now or utcnow())
    fresh_since = run_now - timedelta(minutes=max(1, int(freshness_minutes)))
    window_since = run_now - timedelta(minutes=max(1, int(window_minutes)))

    storage = Storage(db_path)
    try:
        fresh_items = [
            i
            for i in storage.get_items_since(fresh_since)
            if ensure_utc(i.timestamp_utc) <= run_now
        ]
        window_items = [
            i
            for i in storage.get_items_since(window_since)
            if ensure_utc(i.timestamp_utc) <= run_now
        ]
    finally:
        storage.close()

    fresh_count = len(fresh_items)
    window_count = len(window_items)
    source_counts: dict[str, int] = {}
    for item in window_items:
        source_counts[item.source] = source_counts.get(item.source, 0) + 1

    top_source = None
    top_count = 0
    if source_counts:
        top_source, top_count = max(source_counts.items(), key=lambda kv: kv[1])
    top_share = (float(top_count) / float(window_count)) if window_count > 0 else 0.0

    hybrid_report = _load_json_dict(Path(hybrid_report_path))
    hybrid_current = hybrid_report.get("current") if isinstance(hybrid_report, dict) else {}
    hybrid_trend = hybrid_report.get("trend") if isinstance(hybrid_report, dict) else {}
    hybrid_ok = bool((hybrid_current or {}).get("ok")) if isinstance(hybrid_current, dict) else None
    hybrid_trend_status = (
        str((hybrid_trend or {}).get("status"))
        if isinstance(hybrid_trend, dict) and hybrid_trend.get("status") is not None
        else None
    )
    degraded_streak = _report_degraded_streak(Path(hybrid_history_dir), latest_report=hybrid_report)

    source_quality_report = _load_json_dict(Path(source_quality_report_path))
    source_quality_trend = (
        source_quality_report.get("trend") if isinstance(source_quality_report, dict) else {}
    )
    source_quality_trend_status = (
        str((source_quality_trend or {}).get("status"))
        if isinstance(source_quality_trend, dict) and source_quality_trend.get("status") is not None
        else None
    )
    source_quality_degraded = _report_degraded_streak(
        Path(source_quality_history_dir),
        latest_report=source_quality_report,
    )
    source_quality_worst_corr_delta = _source_quality_worst_corr_delta(source_quality_report)

    alerts: list[dict[str, Any]] = []
    if fresh_count <= 0:
        alerts.append(
            {
                "severity": "critical",
                "code": "no_fresh_items",
                "message": "No fresh items in freshness window",
                "details": {
                    "freshness_minutes": int(freshness_minutes),
                    "fresh_items": fresh_count,
                },
            }
        )

    if window_count < max(1, int(min_items_threshold)):
        alerts.append(
            {
                "severity": "high",
                "code": "low_item_volume",
                "message": "Insufficient item volume in monitoring window",
                "details": {
                    "window_minutes": int(window_minutes),
                    "window_items": window_count,
                    "min_items_threshold": int(min_items_threshold),
                },
            }
        )

    if (
        window_count >= max(5, int(min_items_threshold))
        and top_source is not None
        and top_share >= max(0.5, min(1.0, float(source_concentration_threshold)))
    ):
        alerts.append(
            {
                "severity": "drift",
                "code": "source_concentration_high",
                "message": "Single source dominates recent item flow",
                "details": {
                    "window_minutes": int(window_minutes),
                    "window_items": window_count,
                    "top_source": top_source,
                    "top_source_count": top_count,
                    "top_source_share": top_share,
                    "threshold": float(source_concentration_threshold),
                },
            }
        )

    if hybrid_report is None:
        alerts.append(
            {
                "severity": "quality",
                "code": "hybrid_report_missing",
                "message": "Hybrid evaluation report is missing or unreadable",
                "details": {
                    "hybrid_report_path": str(Path(hybrid_report_path)),
                },
            }
        )
    else:
        if hybrid_ok is False:
            alerts.append(
                {
                    "severity": "quality",
                    "code": "hybrid_eval_not_ok",
                    "message": "Latest hybrid evaluation is not ok",
                    "details": {
                        "reason": (hybrid_current or {}).get("reason"),
                        "samples": (hybrid_current or {}).get("samples"),
                    },
                }
            )
        if (
            hybrid_trend_status == "degraded"
            and degraded_streak >= max(1, int(hybrid_degraded_streak))
        ):
            alerts.append(
                {
                    "severity": "quality",
                    "code": "hybrid_trend_degraded_streak",
                    "message": "Hybrid quality degraded for consecutive runs",
                    "details": {
                        "trend_status": hybrid_trend_status,
                        "degraded_streak": degraded_streak,
                        "threshold": int(hybrid_degraded_streak),
                    },
                }
            )

    if source_quality_report is None:
        alerts.append(
            {
                "severity": "quality",
                "code": "source_quality_report_missing",
                "message": "Source-quality report is missing or unreadable",
                "details": {
                    "source_quality_report_path": str(Path(source_quality_report_path)),
                },
            }
        )
    else:
        if (
            source_quality_trend_status == "degraded"
            and source_quality_degraded >= max(1, int(source_quality_degraded_streak))
        ):
            alerts.append(
                {
                    "severity": "quality",
                    "code": "source_quality_trend_degraded_streak",
                    "message": "Source-quality trend degraded for consecutive runs",
                    "details": {
                        "trend_status": source_quality_trend_status,
                        "degraded_streak": source_quality_degraded,
                        "threshold": int(source_quality_degraded_streak),
                    },
                }
            )

        corr_drop_threshold = max(0.0, float(source_quality_corr_drop_threshold))
        if (
            source_quality_worst_corr_delta is not None
            and source_quality_worst_corr_delta <= -corr_drop_threshold
        ):
            alerts.append(
                {
                    "severity": "quality",
                    "code": "source_quality_corr_drop",
                    "message": "Source-quality global correlation dropped materially",
                    "details": {
                        "worst_global_corr_delta": source_quality_worst_corr_delta,
                        "threshold": -corr_drop_threshold,
                    },
                }
            )

    severity_counts: dict[str, int] = {}
    for alert in alerts:
        sev = str(alert.get("severity"))
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    summary = {
        "generated_at_utc": run_now.isoformat(),
        "ok": len(alerts) == 0,
        "alerts_total": len(alerts),
        "severity_counts": severity_counts,
        "alerts": alerts,
        "metrics": {
            "freshness_minutes": int(freshness_minutes),
            "window_minutes": int(window_minutes),
            "fresh_items": fresh_count,
            "window_items": window_count,
            "source_counts": source_counts,
            "top_source": top_source,
            "top_source_count": top_count,
            "top_source_share": top_share,
            "hybrid_trend_status": hybrid_trend_status,
            "hybrid_degraded_streak": degraded_streak,
            "source_quality_trend_status": source_quality_trend_status,
            "source_quality_degraded_streak": source_quality_degraded,
            "source_quality_worst_global_corr_delta": source_quality_worst_corr_delta,
        },
    }

    _write_json(Path(output_path), summary)
    if keep_history:
        ts_slug = run_now.strftime("%Y%m%dT%H%M%SZ")
        _write_json(Path(history_dir) / f"alerts_{ts_slug}.json", summary)
    return summary


def format_alert_summary(summary: dict[str, Any], as_json: bool = False) -> str:
    if as_json:
        return json.dumps(summary, indent=2, sort_keys=False)

    lines = [
        (
            "Alerts check: "
            f"ok={summary.get('ok')} total={summary.get('alerts_total')} "
            f"severity_counts={summary.get('severity_counts')}"
        ),
        f"Generated: {summary.get('generated_at_utc')}",
    ]
    for alert in summary.get("alerts", []):
        lines.append(
            f"- [{alert.get('severity')}] {alert.get('code')}: {alert.get('message')} details={alert.get('details')}"
        )
    return "\n".join(lines)


def should_fail(summary: dict[str, Any], fail_on: set[str]) -> bool:
    targets = {s.strip().lower() for s in fail_on if s.strip()}
    if not targets:
        return False
    for alert in summary.get("alerts", []):
        severity = str(alert.get("severity", "")).strip().lower()
        if severity in targets:
            return True
    return False


def send_webhook(
    *,
    webhook_url: str,
    summary: dict[str, Any],
    send_on: set[str],
    timeout_seconds: int = 5,
    retries: int = 2,
    retry_backoff_seconds: float = 1.5,
    cooldown_seconds: int = 300,
    state_path: str | Path = DEFAULT_WEBHOOK_STATE_PATH,
    max_alerts: int = 12,
) -> dict[str, Any]:
    url = str(webhook_url).strip()
    if not url:
        return {"sent": False, "reason": "missing_url"}

    levels = {s.strip().lower() for s in send_on if s.strip()}
    all_alerts = summary.get("alerts", [])
    matched_alerts = [
        alert
        for alert in all_alerts
        if not levels or str(alert.get("severity", "")).strip().lower() in levels
    ]
    if not matched_alerts:
        return {"sent": False, "reason": "no_matching_alert_level"}
    matched_alerts = sorted(
        matched_alerts,
        key=lambda alert: (_severity_rank(str(alert.get("severity", ""))), str(alert.get("code", ""))),
    )
    posted_alerts = matched_alerts[: max(1, int(max_alerts))]

    now = ensure_utc(utcnow())
    state_file_raw = str(state_path).strip() if state_path is not None else ""
    state_file = Path(state_file_raw) if state_file_raw else None
    cooldown = max(0, int(cooldown_seconds))
    fingerprint = _alerts_fingerprint(posted_alerts)
    state_payload = _load_json_dict(state_file) if state_file is not None else None
    if cooldown > 0 and state_file is not None and isinstance(state_payload, dict):
        prev_fp = state_payload.get("fingerprint")
        prev_sent_ts_raw = state_payload.get("sent_at_utc")
        prev_sent_dt = parse_datetime(prev_sent_ts_raw) if isinstance(prev_sent_ts_raw, str) else None
        if prev_fp == fingerprint and prev_sent_dt is not None:
            remaining = cooldown - int((now - ensure_utc(prev_sent_dt)).total_seconds())
            if remaining > 0:
                return {
                    "sent": False,
                    "reason": "cooldown_active",
                    "retry_after_seconds": remaining,
                    "matched_alerts": len(matched_alerts),
                    "posted_alerts": len(posted_alerts),
                }

    payload = {
        "kind": "btc_news_alerts",
        "generated_at_utc": summary.get("generated_at_utc"),
        "ok": summary.get("ok"),
        "alerts_total": len(matched_alerts),
        "severity_counts": summary.get("severity_counts"),
        "alerts": posted_alerts,
    }
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    attempts = max(1, int(retries) + 1)
    timeout = max(1, int(timeout_seconds))
    last_error: str | None = None

    for attempt in range(1, attempts + 1):
        req = request.Request(
            url=url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200))
            if state_file is not None:
                try:
                    _write_json(
                        state_file,
                        {
                            "sent_at_utc": now.isoformat(),
                            "fingerprint": fingerprint,
                            "status": status,
                            "alerts_total": len(matched_alerts),
                        },
                    )
                except OSError:
                    pass
            return {
                "sent": True,
                "status": status,
                "attempts": attempt,
                "matched_alerts": len(matched_alerts),
                "posted_alerts": len(posted_alerts),
            }
        except url_error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="ignore").strip()
            except OSError:
                body = ""
            last_error = f"http_{exc.code}" if not body else f"http_{exc.code}: {body[:180]}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

        if attempt < attempts and retry_backoff_seconds > 0:
            sleep_seconds = float(retry_backoff_seconds) * (2 ** (attempt - 1))
            time.sleep(min(sleep_seconds, 10.0))

    return {
        "sent": False,
        "reason": "webhook_error",
        "attempts": attempts,
        "error": last_error,
        "matched_alerts": len(matched_alerts),
        "posted_alerts": len(posted_alerts),
    }


def _report_degraded_streak(history_dir: Path, latest_report: dict[str, Any] | None) -> int:
    statuses: list[str | None] = []
    if history_dir.exists() and history_dir.is_dir():
        files = sorted(p for p in history_dir.glob("*.json") if p.is_file())
        for path in files[-32:]:
            payload = _load_json_dict(path)
            if payload is None:
                continue
            trend = payload.get("trend")
            if isinstance(trend, dict):
                status = trend.get("status")
                statuses.append(str(status) if status is not None else None)

    latest_status = None
    if isinstance(latest_report, dict):
        trend = latest_report.get("trend")
        if isinstance(trend, dict):
            status = trend.get("status")
            latest_status = str(status) if status is not None else None
    if latest_status is not None:
        statuses.append(latest_status)

    streak = 0
    for status in reversed(statuses):
        if status == "degraded":
            streak += 1
            continue
        break
    return streak


def _source_quality_worst_corr_delta(report: dict[str, Any] | None) -> float | None:
    if not isinstance(report, dict):
        return None
    delta = report.get("delta")
    if not isinstance(delta, dict):
        return None
    by_window = delta.get("by_window")
    if not isinstance(by_window, dict):
        return None

    out: float | None = None
    for payload in by_window.values():
        if not isinstance(payload, dict):
            continue
        raw = payload.get("global_corr_delta")
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if out is None or val < out:
            out = val
    return out


def _alerts_fingerprint(alerts: list[dict[str, Any]]) -> str:
    minimal = [
        {
            "severity": alert.get("severity"),
            "code": alert.get("code"),
            "message": alert.get("message"),
            "details": alert.get("details"),
        }
        for alert in alerts
    ]
    blob = json.dumps(minimal, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _severity_rank(severity: str) -> int:
    normalized = severity.strip().lower()
    if normalized == "critical":
        return 0
    if normalized == "high":
        return 1
    if normalized == "quality":
        return 2
    if normalized == "drift":
        return 3
    return 4


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    # Best effort normalize timestamps that may appear in downstream payloads.
    ts = payload.get("generated_at_utc")
    if isinstance(ts, str):
        parsed = parse_datetime(ts)
        if parsed is not None:
            payload["generated_at_utc"] = parsed.isoformat()
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")
