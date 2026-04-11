from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from btc_news_arrow.aggregator import Aggregator
from btc_news_arrow.attribution import AttributionEngine, build_attribution_brief
from btc_news_arrow.classifier import Classifier
from btc_news_arrow.collector import Collector
from btc_news_arrow.config import load_config
from btc_news_arrow.learner import Learner
from btc_news_arrow.llm_rater import LLMRater
from btc_news_arrow.models import ArrowResult, NewsItem
from btc_news_arrow.scorer import recency_decay
from btc_news_arrow.scorer import Scorer
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import ensure_utc, parse_datetime, parse_duration, utcnow


def create_app(config_path: str = "config.yaml", db_path: str = "btc_news_arrow.db") -> FastAPI:
    config = load_config(config_path)
    hybrid_cfg = _load_hybrid_config(config)
    llm_cfg = config.get("llm", {})
    llm_cache_ttl_seconds = max(0, int(llm_cfg.get("cache_ttl_seconds", 0)))
    llm_cooldown_seconds = max(0, int(llm_cfg.get("request_cooldown_seconds", 600)))
    llm_min_window_minutes = max(1, int(llm_cfg.get("min_window_minutes", 60)))
    llm_new_item_min_abs_impact = max(0.0, float(llm_cfg.get("new_item_min_abs_impact", 1e-6)))
    llm_short_window_mode = str(llm_cfg.get("short_window_mode", "rule")).strip().lower()
    llm_fallback_to_rule_on_error = bool(llm_cfg.get("fallback_to_rule_on_error", True))
    llm_allow_degraded_start = bool(llm_cfg.get("allow_degraded_start", False))
    llm_cache: dict[int, tuple[datetime, object]] = {}
    llm_last_payload: dict[int, object] = {}
    llm_highwater_marker: dict[int, int] = {}
    llm_cooldown_until: dict[int, datetime] = {}
    attribution_engine = AttributionEngine(config)
    llm_rater = LLMRater(config)
    llm_runtime_error: str | None = None
    if llm_rater.require_runtime:
        try:
            llm_rater.ensure_available()
        except RuntimeError as exc:
            if llm_allow_degraded_start:
                llm_runtime_error = str(exc)
            else:
                raise

    app = FastAPI(title="BTC News Arrow API", version="0.1.0")
    web_dir = Path(__file__).resolve().parent / "web"
    report_cfg = config.get("reports", {})
    hybrid_report_path = Path(str(report_cfg.get("hybrid_report_path", "diagnostics/hybrid_eval_latest.json")))
    hybrid_history_dir = Path(str(report_cfg.get("hybrid_history_dir", "diagnostics/hybrid_eval_history")))
    source_quality_report_path = Path(str(report_cfg.get("source_quality_report_path", "diagnostics/source_quality_latest.json")))
    source_quality_history_dir = Path(str(report_cfg.get("source_quality_history_dir", "diagnostics/source_quality_history")))
    alerts_report_path = Path(str(report_cfg.get("alerts_report_path", "diagnostics/alerts_latest.json")))
    alerts_history_dir = Path(str(report_cfg.get("alerts_history_dir", "diagnostics/alerts_history")))

    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def ui_index():
            return FileResponse(web_dir / "index.html")

    @app.get("/arrow")
    def arrow(
        window: str = Query(default="1h"),
        mode: str = Query(default="auto"),
        blend_weight: float = Query(default=0.5, ge=0.0, le=1.0),
        include_reasons: bool = Query(default=False),
        reason_limit: int = Query(default=3, ge=1, le=10),
    ):
        try:
            delta = parse_duration(window)
            minutes = int(delta.total_seconds() // 60)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        storage = Storage(db_path)
        try:
            agg = Aggregator(config)
            learner = Learner(config)
            now = utcnow()
            since = now - delta
            items = storage.get_items_since(since)
            rule_contributions = agg.contributions_for_window(items, minutes, now=now)
            rule_score = sum(c for c, _ in rule_contributions)
            rule_result = ArrowResult(
                window_minutes=minutes,
                score=rule_score,
                arrow=agg._score_to_arrow(rule_score, agg._threshold_for_window(minutes)),
                contributing_items=len(rule_contributions),
            )
            learn_result = learner.learned_arrow(storage=storage, window_minutes=minutes, now=now)
            coverage_relevance = _coverage_relevance(rule_contributions, hybrid_cfg)
            mode_normalized = mode.strip().lower()
            if not mode_normalized:
                mode_normalized = "auto"
            mode_effective = mode_normalized
            llm_warning: str | None = None
            try:
                blend_weight_value = float(blend_weight)
            except (TypeError, ValueError):
                blend_weight_value = 0.5
            blend_weight_value = max(0.0, min(1.0, blend_weight_value))
            llm_payload = None
            use_auto_mode = mode_normalized == "auto"
            llm_called_successfully = False
            llm_window_marker = _llm_max_db_id(
                items,
                min_abs_impact=llm_new_item_min_abs_impact,
            )
            if minutes not in llm_highwater_marker:
                # Bootstrap marker on first request after process start to avoid
                # immediate LLM refresh on historical backlog alone.
                llm_highwater_marker[minutes] = llm_window_marker
            llm_has_new_items = llm_window_marker > llm_highwater_marker.get(minutes, 0)
            latest_item_age_seconds = None
            if items:
                latest_item_ts = max(ensure_utc(i.timestamp_utc) for i in items)
                latest_item_age_seconds = max(0.0, (now - latest_item_ts).total_seconds())
            result = rule_result

            if mode_normalized == "learn":
                result = learner.learned_arrow(storage=storage, window_minutes=minutes, now=now)
            elif mode_normalized == "blend":
                rule_result = agg.arrow_for_window(items, minutes, now=now)
                learn_result = learner.learned_arrow(storage=storage, window_minutes=minutes, now=now)
                score = (1.0 - blend_weight_value) * rule_result.score + blend_weight_value * learn_result.score
                threshold = agg._threshold_for_window(minutes)
                result = ArrowResult(
                    window_minutes=minutes,
                    score=score,
                    arrow=agg._score_to_arrow(score, threshold),
                    contributing_items=rule_result.contributing_items,
                )
            elif mode_normalized in {"auto", "llm"}:
                mode_effective = "rule" if use_auto_mode else "llm"
                if llm_runtime_error is not None:
                    mode_effective = "rule_runtime_unavailable"
                    llm_warning = f"LLM runtime unavailable: {llm_runtime_error}; using rule mode"
                    result = rule_result
                elif use_auto_mode and not llm_rater.enabled:
                    result = rule_result
                else:
                    llm_resolution = _resolve_llm_result(
                        llm_rater=llm_rater,
                        items=items,
                        minutes=minutes,
                        now=now,
                        llm_has_new_items=llm_has_new_items,
                        llm_last_payload=llm_last_payload,
                        llm_cache=llm_cache,
                        llm_cooldown_until=llm_cooldown_until,
                        llm_cache_ttl_seconds=llm_cache_ttl_seconds,
                        llm_cooldown_seconds=llm_cooldown_seconds,
                        llm_min_window_minutes=llm_min_window_minutes,
                        llm_short_window_mode=llm_short_window_mode,
                        llm_fallback_to_rule_on_error=llm_fallback_to_rule_on_error,
                        default_mode_effective=mode_effective,
                    )
                    llm_payload = llm_resolution["payload"]
                    llm_warning = llm_resolution["warning"]
                    llm_called_successfully = bool(llm_resolution["called_successfully"])
                    mode_effective = str(llm_resolution["mode_effective"])
                    if llm_resolution["use_rule_result"]:
                        result = rule_result
                    elif llm_payload is not None:
                        result = llm_payload.result
                    else:
                        result = rule_result
            else:
                result = rule_result

            if llm_payload is not None:
                llm_last_payload[minutes] = llm_payload
            if llm_called_successfully:
                llm_highwater_marker[minutes] = max(llm_highwater_marker.get(minutes, 0), llm_window_marker)

            rule_score_clipped = _clip_component_score("rule", float(rule_score), hybrid_cfg)
            llm_score = (
                _clip_component_score("llm", float(llm_payload.result.score), hybrid_cfg)
                if llm_payload is not None
                else None
            )
            learn_score = _clip_component_score("learn", float(learn_result.score), hybrid_cfg)
            rule_score_raw = float(rule_score)
            llm_score_raw = float(llm_payload.result.score) if llm_payload is not None else None
            learn_score_raw = float(learn_result.score)
            final_score = float(result.score)
            final_contributing_items = int(result.contributing_items)
            score_components: dict[str, float] = {"rule": rule_score_clipped, "learn": learn_score}
            score_weights_used: dict[str, float] | None = None

            if llm_score is not None:
                score_components["llm"] = float(llm_score)

            if mode_normalized == "auto":
                final_score, score_weights_used = _combine_weighted_scores(
                    score_components=score_components,
                    target_weights=hybrid_cfg["score_weights"],
                )
                mode_effective = f"{mode_effective}_hybrid"
                final_contributing_items = int(rule_result.contributing_items)

            regime_threshold = agg._threshold_for_window(minutes)
            signal_state = _signal_state_from_metrics(
                score=final_score,
                threshold=regime_threshold,
                contributing_items=rule_result.contributing_items,
                relevance_sum=coverage_relevance,
                min_items=hybrid_cfg["no_signal_min_items"],
                min_relevance_sum=hybrid_cfg["no_signal_min_relevance_sum"],
                disagreement=_component_disagreement(score_components, threshold=regime_threshold),
                disagreement_score_abs_factor=hybrid_cfg["no_signal_disagreement_score_abs_factor"],
            )

            final_arrow = agg._score_to_arrow(final_score, regime_threshold)
            if signal_state == "no_signal":
                final_arrow = "▬"
            result = ArrowResult(
                window_minutes=minutes,
                score=final_score,
                arrow=final_arrow,
                contributing_items=final_contributing_items,
            )
            regime = _regime_from_score(final_score, regime_threshold)
            regime_strength = min(1.0, abs(float(final_score)) / max(regime_threshold, 1e-9))

            payload = {
                "window": window,
                "window_minutes": minutes,
                "mode": mode_normalized,
                "mode_effective": mode_effective,
                "arrow": result.arrow,
                "score": result.score,
                "final_score": result.score,
                "rule_score": rule_score_clipped,
                "rule_score_raw": rule_score_raw,
                "llm_score": llm_score,
                "llm_score_raw": llm_score_raw,
                "learn_score": learn_score,
                "learn_score_raw": learn_score_raw,
                "signal_state": signal_state,
                "regime": regime,
                "regime_strength": regime_strength,
                "regime_threshold": regime_threshold,
                "contributing_items": result.contributing_items,
                "coverage_relevance_sum": coverage_relevance,
                "coverage_min_items": hybrid_cfg["no_signal_min_items"],
                "coverage_min_relevance_sum": hybrid_cfg["no_signal_min_relevance_sum"],
                "llm_has_new_items": llm_has_new_items,
                "latest_item_age_seconds": latest_item_age_seconds,
            }
            if score_weights_used:
                payload["score_weights_used"] = score_weights_used
            if llm_warning:
                payload["warning"] = llm_warning
            if llm_payload is not None:
                payload["confidence"] = llm_payload.confidence
                payload["notes"] = llm_payload.notes
                payload["llm_meta"] = llm_payload.meta
            if include_reasons:
                # In hybrid mode the visible score is final(rule+llm), but the
                # actionable event trace should come from fresh rule contributions.
                if mode_effective.endswith("_hybrid"):
                    contributions = rule_contributions
                    direction = 1 if result.score > 0 else (-1 if result.score < 0 else 0)
                    if signal_state == "no_signal":
                        direction = 0
                    payload["reasons"] = _select_reasons(contributions, direction=direction, limit=reason_limit)
                    if llm_payload is not None:
                        payload["llm_reasons"] = llm_payload.reasons[:reason_limit]
                elif mode_effective.startswith("llm") and llm_payload is not None:
                    payload["reasons"] = llm_payload.reasons[:reason_limit]
                elif mode_normalized in {"rule", "blend", "auto"}:
                    contributions = rule_contributions
                    direction = 1 if result.score > 0 else (-1 if result.score < 0 else 0)
                    if signal_state == "no_signal":
                        direction = 0
                    payload["reasons"] = _select_reasons(contributions, direction=direction, limit=reason_limit)
                else:
                    payload["reasons"] = []
            attribution = attribution_engine.evaluate(
                storage=storage,
                now=now,
                window_minutes=minutes,
                window_label=window,
                learner=learner,
                llm_payload=llm_payload,
                limit=max(3, reason_limit),
            )
            payload.update(build_attribution_brief(attribution))
            payload["attribution"] = attribution
            payload["trading_signal"] = _build_trading_signal_contract(payload)
            return payload
        finally:
            storage.close()

    @app.get("/signal/trading")
    def signal_trading(
        window: str = Query(default="1h"),
        mode: str = Query(default="auto"),
        blend_weight: float = Query(default=0.5, ge=0.0, le=1.0),
        reason_limit: int = Query(default=3, ge=1, le=10),
    ):
        payload = arrow(
            window=window,
            mode=mode,
            blend_weight=blend_weight,
            include_reasons=True,
            reason_limit=reason_limit,
        )
        return payload.get("trading_signal") or _build_trading_signal_contract(payload)

    @app.get("/signal/attribution")
    def signal_attribution(
        window: str = Query(default="1h"),
        ts: str | None = Query(default=None),
        limit: int = Query(default=5, ge=1, le=20),
    ):
        try:
            delta = parse_duration(window)
            minutes = int(delta.total_seconds() // 60)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        parsed_ts = None
        if ts is not None:
            parsed_ts = parse_datetime(ts)
            if parsed_ts is None:
                raise HTTPException(status_code=400, detail="Invalid ts format")

        storage = Storage(db_path)
        try:
            learner = Learner(config)
            llm_payload = llm_last_payload.get(minutes)
            return attribution_engine.evaluate(
                storage=storage,
                now=utcnow(),
                window_minutes=minutes,
                window_label=window,
                learner=learner,
                llm_payload=llm_payload,
                limit=limit,
                ts=parsed_ts,
            )
        finally:
            storage.close()

    @app.post("/collect")
    def collect(include_gdelt: bool = True):
        storage = Storage(db_path)
        try:
            collector = Collector(
                config=config,
                storage=storage,
                classifier=Classifier(config),
                scorer=Scorer(config),
            )
            items, inserted, stats = collector.collect_once(include_gdelt=include_gdelt)
            return {
                "processed": len(items),
                "inserted": inserted,
                "stats": stats,
            }
        finally:
            storage.close()

    @app.post("/learn/update")
    def learn_update(limit: int = Query(default=500, ge=1, le=5000)):
        storage = Storage(db_path)
        try:
            learner = Learner(config)
            summary = learner.update_model(storage=storage, limit_per_horizon=limit)
            return summary
        finally:
            storage.close()

    @app.get("/llm/ping")
    def llm_ping():
        try:
            return llm_rater.ping()
        except (RuntimeError, ValueError) as exc:
            raise _llm_http_exception(exc) from exc

    @app.get("/reports/summary")
    def reports_summary():
        return {
            "generated_at_utc": utcnow().isoformat(),
            "hybrid": _report_snapshot_summary(hybrid_report_path),
            "source_quality": _report_snapshot_summary(source_quality_report_path),
            "alerts": _report_snapshot_summary(alerts_report_path),
        }

    @app.get("/reports/hybrid")
    def report_hybrid(include_history: bool = Query(default=False), limit: int = Query(default=10, ge=1, le=200)):
        latest = _require_report_payload(hybrid_report_path, name="hybrid")
        payload: dict[str, object] = {"latest": latest}
        if include_history:
            payload["history"] = _load_report_history(hybrid_history_dir, limit=limit)
        return payload

    @app.get("/reports/source-quality")
    def report_source_quality(include_history: bool = Query(default=False), limit: int = Query(default=10, ge=1, le=200)):
        latest = _require_report_payload(source_quality_report_path, name="source_quality")
        payload: dict[str, object] = {"latest": latest}
        if include_history:
            payload["history"] = _load_report_history(source_quality_history_dir, limit=limit)
        return payload

    @app.get("/reports/alerts")
    def report_alerts(include_history: bool = Query(default=False), limit: int = Query(default=10, ge=1, le=200)):
        latest = _require_report_payload(alerts_report_path, name="alerts")
        payload: dict[str, object] = {"latest": latest}
        if include_history:
            payload["history"] = _load_report_history(alerts_history_dir, limit=limit)
        return payload

    @app.get("/forensic")
    def forensic(ts: str, window: str = Query(default="30m")):
        timestamp = parse_datetime(ts)
        if not timestamp:
            raise HTTPException(status_code=400, detail="Invalid ts format")
        try:
            delta = parse_duration(window)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        storage = Storage(db_path)
        try:
            items = storage.get_items_between(timestamp - delta, timestamp + delta)
            agg = Aggregator(config)
            half_life_minutes = agg.half_life_minutes
            window_minutes = int(delta.total_seconds() // 60)
            rule_threshold = agg._threshold_for_window(window_minutes)

            forensic_items: list[dict[str, object]] = []
            rule_score_at_ts = 0.0
            for i in sorted(items, key=lambda x: ensure_utc(x.timestamp_utc), reverse=True):
                age_seconds = (timestamp - i.timestamp_utc).total_seconds()
                is_future_vs_ts = age_seconds < 0
                contribution_at_ts = 0.0
                if not is_future_vs_ts:
                    contribution_at_ts = i.impact * recency_decay(age_seconds, half_life_minutes)
                    rule_score_at_ts += contribution_at_ts

                forensic_items.append(
                    {
                        "timestamp_utc": i.timestamp_utc.isoformat(),
                        "source": i.source,
                        "category": i.category,
                        "polarity": i.polarity,
                        "impact": i.impact,
                        "contribution_at_ts": contribution_at_ts,
                        "is_future_vs_ts": is_future_vs_ts,
                        "title": i.title,
                        "url": i.url,
                    }
                )

            rule_arrow_at_ts = agg._score_to_arrow(rule_score_at_ts, rule_threshold)
            rule_regime_at_ts = _regime_from_score(rule_score_at_ts, rule_threshold)
            return {
                "window_start": (timestamp - delta).isoformat(),
                "window_end": (timestamp + delta).isoformat(),
                "count": len(items),
                "ts": timestamp.isoformat(),
                "half_life_minutes": half_life_minutes,
                "rule_threshold": rule_threshold,
                "rule_score_at_ts": rule_score_at_ts,
                "rule_arrow_at_ts": rule_arrow_at_ts,
                "rule_regime_at_ts": rule_regime_at_ts,
                "items": forensic_items,
            }
        finally:
            storage.close()

    return app


def _resolve_llm_result(
    llm_rater: LLMRater,
    items: list[NewsItem],
    minutes: int,
    now: datetime,
    llm_has_new_items: bool,
    llm_last_payload: dict[int, object],
    llm_cache: dict[int, tuple[datetime, object]],
    llm_cooldown_until: dict[int, datetime],
    llm_cache_ttl_seconds: int,
    llm_cooldown_seconds: int,
    llm_min_window_minutes: int,
    llm_short_window_mode: str,
    llm_fallback_to_rule_on_error: bool,
    default_mode_effective: str,
) -> dict[str, object]:
    mode_effective = default_mode_effective
    llm_warning: str | None = None
    llm_payload: object | None = None
    stale_llm_payload: object | None = None
    llm_called_successfully = False
    use_rule_result = False

    if minutes < llm_min_window_minutes and llm_short_window_mode == "rule":
        return {
            "payload": None,
            "warning": None,
            "called_successfully": False,
            "mode_effective": "rule_short_window",
            "use_rule_result": True,
        }

    if not llm_has_new_items and minutes in llm_last_payload:
        llm_payload = llm_last_payload[minutes]
        return {
            "payload": llm_payload,
            "warning": "No new items since last LLM rating; using cached LLM response",
            "called_successfully": False,
            "mode_effective": "llm_no_new_items",
            "use_rule_result": False,
        }

    if not llm_has_new_items and default_mode_effective == "rule":
        return {
            "payload": None,
            "warning": "No new material items since last LLM baseline; skipping LLM request",
            "called_successfully": False,
            "mode_effective": "llm_no_new_items",
            "use_rule_result": True,
        }

    if llm_cache_ttl_seconds > 0 and not llm_has_new_items:
        cached = llm_cache.get(minutes)
        if cached is not None:
            cached_ts, cached_payload = cached
            stale_llm_payload = cached_payload
            age = (now - cached_ts).total_seconds()
            if age <= llm_cache_ttl_seconds:
                llm_payload = cached_payload

    if llm_payload is None:
        cooldown_until = llm_cooldown_until.get(minutes)
        if cooldown_until is not None and now < cooldown_until:
            if stale_llm_payload is not None:
                llm_payload = stale_llm_payload
                mode_effective = "llm_stale_cache"
                llm_warning = (
                    f"LLM cooldown active until {cooldown_until.isoformat()}; "
                    "using stale cached LLM response"
                )
            else:
                raise HTTPException(
                    status_code=503,
                    detail=f"LLM cooldown active until {cooldown_until.isoformat()}",
                )
        else:
            try:
                llm_payload = llm_rater.rate_window(items=items, window_minutes=minutes, now=now)
                llm_cooldown_until.pop(minutes, None)
                llm_called_successfully = True
            except (RuntimeError, ValueError) as exc:
                if llm_cooldown_seconds > 0:
                    llm_cooldown_until[minutes] = now + timedelta(seconds=llm_cooldown_seconds)
                if llm_fallback_to_rule_on_error:
                    llm_warning = str(exc)
                    mode_effective = "rule_fallback"
                    use_rule_result = True
                elif stale_llm_payload is not None:
                    llm_payload = stale_llm_payload
                    mode_effective = "llm_stale_cache"
                    llm_warning = f"{exc}; using stale cached LLM response"
                else:
                    raise _llm_http_exception(exc) from exc

    if llm_cache_ttl_seconds > 0 and llm_payload is not None:
        llm_cache[minutes] = (now, llm_payload)
    if llm_payload is not None and mode_effective not in {"llm_stale_cache", "llm_no_new_items"}:
        mode_effective = "llm"

    return {
        "payload": llm_payload,
        "warning": llm_warning,
        "called_successfully": llm_called_successfully,
        "mode_effective": mode_effective,
        "use_rule_result": use_rule_result,
    }


def _select_reasons(
    contributions: list[tuple[float, NewsItem]],
    direction: int,
    limit: int,
) -> list[dict[str, object]]:
    filtered: list[tuple[float, NewsItem]]
    if direction > 0:
        filtered = [entry for entry in contributions if entry[0] > 0]
        ordered = sorted(filtered, key=lambda x: x[0], reverse=True)
    elif direction < 0:
        filtered = [entry for entry in contributions if entry[0] < 0]
        ordered = sorted(filtered, key=lambda x: x[0])
    else:
        filtered = [entry for entry in contributions if entry[0] != 0]
        ordered = sorted(filtered, key=lambda x: abs(x[0]), reverse=True)

    reasons: list[dict[str, object]] = []
    for contribution, item in ordered[:limit]:
        reasons.append(
            {
                "timestamp_utc": item.timestamp_utc.isoformat(),
                "source": item.source,
                "category": item.category,
                "contribution": contribution,
                "title": item.title,
                "url": item.url,
            }
        )
    return reasons


def _load_hybrid_config(config: dict[str, Any]) -> dict[str, Any]:
    hybrid = config.get("hybrid", {})
    rule_weight = float(hybrid.get("rule_weight", 0.6))
    llm_weight = float(hybrid.get("llm_weight", 0.4))
    learn_weight = float(hybrid.get("learn_weight", 0.0))
    total = rule_weight + llm_weight + learn_weight
    if total <= 0:
        rule_weight, llm_weight, learn_weight = 0.6, 0.4, 0.0
        total = 1.0

    source_defaults = hybrid.get("source_default_relevance", {})
    source_default_relevance = {str(k): float(v) for k, v in source_defaults.items()}
    keywords_strong = [str(v).lower() for v in hybrid.get("keywords_strong", [])]
    tier1_sources = {str(v) for v in hybrid.get("tier1_exchange_sources", [])}
    tier1_relevance = float(hybrid.get("tier1_exchange_relevance", 0.7))
    keyword_relevance = float(hybrid.get("keyword_strong_relevance", 1.0))
    score_clip_abs = hybrid.get("score_clip_abs", {})
    score_clip_abs_norm = {
        "rule": float(score_clip_abs.get("rule", 2.5)),
        "llm": float(score_clip_abs.get("llm", 1.5)),
        "learn": float(score_clip_abs.get("learn", 2.0)),
    }

    return {
        "score_weights": {
            "rule": rule_weight / total,
            "llm": llm_weight / total,
            "learn": learn_weight / total,
        },
        "no_signal_min_items": int(hybrid.get("no_signal_min_items", 3)),
        "no_signal_min_relevance_sum": float(hybrid.get("no_signal_min_relevance_sum", 1.2)),
        "no_signal_disagreement_score_abs_factor": float(
            hybrid.get("no_signal_disagreement_score_abs_factor", 1.2)
        ),
        "source_default_relevance": source_default_relevance,
        "keywords_strong": keywords_strong,
        "tier1_exchange_sources": tier1_sources,
        "tier1_exchange_relevance": tier1_relevance,
        "keyword_strong_relevance": keyword_relevance,
        "score_clip_abs": score_clip_abs_norm,
    }


def _clip_component_score(name: str, value: float, hybrid_cfg: dict[str, Any]) -> float:
    limits = hybrid_cfg.get("score_clip_abs", {})
    try:
        clip_abs = float(limits.get(name))
    except (TypeError, ValueError):
        return value
    if clip_abs <= 0:
        return value
    return max(-clip_abs, min(clip_abs, float(value)))


def _combine_weighted_scores(
    score_components: dict[str, float],
    target_weights: dict[str, float],
) -> tuple[float, dict[str, float]]:
    used_components = {
        name: float(score)
        for name, score in score_components.items()
        if name in target_weights and float(target_weights.get(name, 0.0)) > 0.0
    }
    if not used_components:
        return 0.0, {}

    total_weight = sum(float(target_weights[name]) for name in used_components.keys())
    if total_weight <= 0:
        total_weight = float(len(used_components))
        norm = {name: 1.0 / total_weight for name in used_components.keys()}
    else:
        norm = {name: float(target_weights[name]) / total_weight for name in used_components.keys()}

    score = 0.0
    for name, value in used_components.items():
        score += norm[name] * value
    return score, norm


def _coverage_relevance(
    contributions: list[tuple[float, NewsItem]],
    hybrid_cfg: dict[str, Any],
) -> float:
    total = 0.0
    for _, item in contributions:
        total += _item_btc_relevance(item, hybrid_cfg)
    return total


def _item_btc_relevance(item: NewsItem, hybrid_cfg: dict[str, Any]) -> float:
    raw = item.raw if isinstance(item.raw, dict) else {}
    scoring = raw.get("scoring") if isinstance(raw.get("scoring"), dict) else {}
    raw_relevance = scoring.get("btc_relevance_multiplier")
    try:
        if raw_relevance is not None:
            return max(0.0, min(1.0, float(raw_relevance)))
    except (TypeError, ValueError):
        pass

    relevance = float(hybrid_cfg["source_default_relevance"].get(item.source, 0.3))
    text = f"{item.title} {item.summary}".lower()
    if any(keyword in text for keyword in hybrid_cfg["keywords_strong"]):
        relevance = max(relevance, float(hybrid_cfg["keyword_strong_relevance"]))
    if item.source in hybrid_cfg["tier1_exchange_sources"]:
        relevance = max(relevance, float(hybrid_cfg["tier1_exchange_relevance"]))
    return max(0.0, min(1.0, relevance))


def _signal_state_from_metrics(
    score: float,
    threshold: float,
    contributing_items: int,
    relevance_sum: float,
    min_items: int,
    min_relevance_sum: float,
    disagreement: bool = False,
    disagreement_score_abs_factor: float = 1.2,
) -> str:
    if contributing_items < max(0, int(min_items)) or relevance_sum < max(0.0, float(min_relevance_sum)):
        return "no_signal"
    if disagreement and abs(float(score)) < max(1e-9, float(threshold) * float(disagreement_score_abs_factor)):
        return "no_signal"
    safe_threshold = max(float(threshold), 1e-9)
    if score >= safe_threshold:
        return "risk_on"
    if score <= -safe_threshold:
        return "risk_off"
    return "neutral"


def _component_disagreement(score_components: dict[str, float], threshold: float) -> bool:
    if not score_components:
        return False
    signs: set[int] = set()
    active_min_abs = max(1e-9, float(threshold) * 0.5)
    for value in score_components.values():
        v = float(value)
        if abs(v) < active_min_abs:
            continue
        signs.add(1 if v > 0 else -1)
    return 1 in signs and -1 in signs


def _build_trading_signal_contract(payload: dict[str, Any]) -> dict[str, Any]:
    score = float(payload.get("final_score", payload.get("score", 0.0)) or 0.0)
    threshold = max(1e-9, float(payload.get("regime_threshold", 0.25) or 0.25))
    confidence_raw = payload.get("confidence")
    if confidence_raw is None:
        confidence = min(1.0, abs(score) / threshold)
    else:
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            confidence = min(1.0, abs(score) / threshold)

    arrow = str(payload.get("arrow", "▬"))
    direction = 1 if "▲" in arrow else (-1 if "▼" in arrow else 0)
    signal_state = str(payload.get("signal_state", "no_signal"))
    no_signal = signal_state == "no_signal"
    if no_signal:
        direction = 0

    reasons = payload.get("reasons")
    top_reason = None
    if isinstance(reasons, list) and reasons:
        first = reasons[0]
        if isinstance(first, dict):
            top_reason = {
                "title": first.get("title"),
                "source": first.get("source"),
                "category": first.get("category"),
                "url": first.get("url"),
            }

    flags: list[str] = []
    if no_signal:
        flags.append("no_signal")
    try:
        if int(payload.get("contributing_items", 0)) < int(payload.get("coverage_min_items", 0)):
            flags.append("low_coverage_items")
    except (TypeError, ValueError):
        pass
    try:
        if float(payload.get("coverage_relevance_sum", 0.0)) < float(payload.get("coverage_min_relevance_sum", 0.0)):
            flags.append("low_coverage_relevance")
    except (TypeError, ValueError):
        pass
    warning = payload.get("warning")
    if isinstance(warning, str) and warning.strip():
        flags.append("runtime_warning")
    attribution_state = str(payload.get("attribution_state", "not_applicable"))
    if attribution_state == "no_clear_news_driver":
        flags.append("no_clear_news_driver")
    elif attribution_state == "mixed":
        flags.append("mixed_news_driver")

    return {
        "version": "v1",
        "generated_at_utc": utcnow().isoformat(),
        "window": payload.get("window"),
        "window_minutes": payload.get("window_minutes"),
        "signal_state": signal_state,
        "regime": payload.get("regime"),
        "arrow": arrow,
        "direction": direction,
        "score": score,
        "regime_threshold": threshold,
        "regime_strength": payload.get("regime_strength"),
        "confidence": confidence,
        "abstain": no_signal,
        "contributing_items": payload.get("contributing_items"),
        "latest_item_age_seconds": payload.get("latest_item_age_seconds"),
        "attribution_version": payload.get("attribution_version"),
        "attribution_state": attribution_state,
        "news_driven_probability": payload.get("news_driven_probability"),
        "attribution_event": payload.get("attribution_event"),
        "top_attribution": payload.get("top_attribution"),
        "components": {
            "rule": payload.get("rule_score"),
            "learn": payload.get("learn_score"),
            "llm": payload.get("llm_score"),
            "weights_used": payload.get("score_weights_used"),
        },
        "flags": flags,
        "top_reason": top_reason,
    }


def _llm_http_exception(exc: Exception) -> HTTPException:
    text = str(exc).strip()
    lower = text.lower()
    if "timed out" in lower or "timeout" in lower:
        return HTTPException(status_code=504, detail=text)
    if "rate limit" in lower:
        return HTTPException(status_code=429, detail=text)
    if "connection error" in lower:
        return HTTPException(status_code=503, detail=text)
    if (
        "openai_api_key" in lower
        or "placeholder value" in lower
        or "package missing" in lower
        or "disabled in config" in lower
    ):
        return HTTPException(status_code=400, detail=text)
    return HTTPException(status_code=502, detail=text)


def _llm_max_db_id(items: list[NewsItem], *, min_abs_impact: float = 0.0) -> int:
    max_id = 0
    for item in items:
        try:
            impact = float(item.impact)
        except (TypeError, ValueError):
            impact = 0.0
        if abs(impact) < float(min_abs_impact):
            continue
        raw = item.raw if isinstance(item.raw, dict) else {}
        raw_id = raw.get("_db_id")
        if raw_id is None:
            continue
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if item_id > max_id:
            max_id = item_id
    return max_id


def _regime_from_score(score: float, threshold: float) -> str:
    safe_threshold = max(float(threshold), 1e-9)
    if score >= safe_threshold:
        return "risk_on"
    if score <= -safe_threshold:
        return "risk_off"
    return "neutral"


def _report_snapshot_summary(path: Path) -> dict[str, object]:
    payload = _load_report_json(path)
    if payload is None:
        return {"available": False, "path": str(path)}

    trend = payload.get("trend")
    trend_status = trend.get("status") if isinstance(trend, dict) else None
    current = payload.get("current")
    current_ok = current.get("ok") if isinstance(current, dict) and "ok" in current else payload.get("ok")
    alerts_total = payload.get("alerts_total") if "alerts_total" in payload else None
    return {
        "available": True,
        "path": str(path),
        "generated_at_utc": payload.get("generated_at_utc"),
        "trend_status": trend_status,
        "ok": current_ok,
        "alerts_total": alerts_total,
    }


def _require_report_payload(path: Path, *, name: str) -> dict[str, Any]:
    payload = _load_report_json(path)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"{name}_report_missing")
    return payload


def _load_report_history(history_dir: Path, *, limit: int) -> list[dict[str, Any]]:
    if not history_dir.exists() or not history_dir.is_dir():
        return []
    files = sorted(p for p in history_dir.glob("*.json") if p.is_file())
    out: list[dict[str, Any]] = []
    for path in files[-max(1, int(limit)):]:
        payload = _load_report_json(path)
        if payload is None:
            continue
        out.append(payload)
    return out


def _load_report_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload
