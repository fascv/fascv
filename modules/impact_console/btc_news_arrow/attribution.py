from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from math import exp, sqrt
from typing import Any

from btc_news_arrow.learner import Learner
from btc_news_arrow.models import NewsItem
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import ensure_utc, hash_text, normalize_text, parse_duration, utcnow


def _sign(value: float, eps: float = 1e-12) -> int:
    if value > eps:
        return 1
    if value < -eps:
        return -1
    return 0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


@dataclass(slots=True)
class PriceEvent:
    timestamp_utc: datetime
    ts_minute: int
    horizon_minutes: int
    return_value: float
    zscore: float
    direction: int


class AttributionEngine:
    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("attribution", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.llm_usage = str(cfg.get("llm_usage", "on")).strip().lower() not in {"off", "false", "0", "no"}

        event_cfg = cfg.get("event_detection", {})
        self.event_horizons_minutes = self._parse_horizons(event_cfg.get("horizons", ["15m", "60m"]))
        self.event_zscore_threshold = max(0.1, float(event_cfg.get("zscore_threshold", 2.0)))
        self.event_min_abs_return = max(0.0, float(event_cfg.get("min_abs_return", 0.004)))
        self.event_cooldown_minutes = max(0, int(event_cfg.get("cooldown_minutes", 45)))
        self.event_max_per_day = max(1, int(event_cfg.get("max_events_per_day", 24)))
        self.event_lookback_hours = max(2, int(event_cfg.get("lookback_hours", 48)))
        self.event_zscore_lookback_points = max(20, int(event_cfg.get("zscore_lookback_points", 96)))

        cand_cfg = cfg.get("candidate_window", {})
        self.candidate_lookback_minutes = max(30, int(cand_cfg.get("lookback_minutes", 180)))
        self.candidate_lookahead_minutes = max(0, int(cand_cfg.get("lookahead_minutes", 15)))
        self.max_candidates = max(1, int(cand_cfg.get("max_candidates", 50)))

        filt_cfg = cfg.get("candidate_filters", {})
        self.min_abs_impact = max(0.0, float(filt_cfg.get("min_abs_impact", 0.05)))
        self.min_abs_learn_score = max(0.0, float(filt_cfg.get("min_abs_learn_score", 0.03)))
        self.cluster_decay = _clamp(float(filt_cfg.get("cluster_decay", 0.6)), 0.05, 1.0)
        self.top_k = max(1, int(filt_cfg.get("top_k", 5)))

        score_cfg = cfg.get("scoring_weights", {})
        self.weight_time = max(0.0, float(score_cfg.get("time", 0.30)))
        self.weight_direction = max(0.0, float(score_cfg.get("direction", 0.20)))
        self.weight_rule = max(0.0, float(score_cfg.get("rule", 0.20)))
        self.weight_learn = max(0.0, float(score_cfg.get("learn", 0.20)))
        self.weight_llm = max(0.0, float(score_cfg.get("llm", 0.10)))
        w_total = self.weight_time + self.weight_direction + self.weight_rule + self.weight_learn + self.weight_llm
        if w_total <= 0:
            self.weight_time, self.weight_direction, self.weight_rule, self.weight_learn, self.weight_llm = (
                0.30,
                0.20,
                0.20,
                0.20,
                0.10,
            )
            w_total = 1.0
        self.weight_time /= w_total
        self.weight_direction /= w_total
        self.weight_rule /= w_total
        self.weight_learn /= w_total
        self.weight_llm /= w_total

        score_norm_cfg = cfg.get("score_normalization", {})
        self.time_decay_minutes = max(1.0, float(score_norm_cfg.get("time_decay_minutes", 90.0)))
        self.rule_norm_abs = max(1e-6, float(score_norm_cfg.get("rule_norm_abs", 0.6)))
        self.learn_norm_abs = max(1e-6, float(score_norm_cfg.get("learn_norm_abs", 0.02)))
        self.llm_norm_abs = max(1e-6, float(score_norm_cfg.get("llm_norm_abs", 0.6)))
        self.source_factor_min = max(0.1, float(score_norm_cfg.get("source_factor_min", 0.5)))
        self.source_factor_max = max(self.source_factor_min, float(score_norm_cfg.get("source_factor_max", 1.5)))

        th_cfg = cfg.get("thresholds", {})
        self.news_driven_probability_threshold = _clamp(
            float(th_cfg.get("news_driven_probability", 0.65)),
            0.01,
            0.99,
        )
        self.news_driven_top_score_threshold = _clamp(
            float(th_cfg.get("news_driven_top_score", 0.45)),
            0.01,
            0.99,
        )
        self.mixed_probability_threshold = _clamp(
            float(th_cfg.get("mixed_probability", 0.40)),
            0.01,
            0.99,
        )

    def evaluate(
        self,
        *,
        storage: Storage,
        now: datetime | None = None,
        window_minutes: int = 60,
        window_label: str | None = None,
        learner: Learner | None = None,
        llm_payload: object | None = None,
        limit: int = 5,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        run_now = ensure_utc(now or utcnow())
        ref_ts = ensure_utc(ts) if ts is not None else run_now
        if not self.enabled:
            return self._empty(
                classification="not_applicable",
                reason="attribution_disabled",
                now=run_now,
                window_minutes=window_minutes,
                window_label=window_label,
            )

        event = self._detect_latest_event(storage=storage, ref_ts=ref_ts)
        if event is None:
            return self._empty(
                classification="not_applicable",
                reason="no_price_event_detected",
                now=run_now,
                window_minutes=window_minutes,
                window_label=window_label,
            )

        llm_index = self._llm_reason_index(llm_payload)
        candidates = self._build_candidates(
            storage=storage,
            learner=learner,
            event=event,
            llm_index=llm_index,
        )
        ranked = self._apply_cluster_dampening(candidates)

        candidate_limit = max(1, int(limit))
        ranked_limited = ranked[:candidate_limit]
        top_k = ranked[: self.top_k]
        summary = self._summary(top_k=top_k, all_candidates=ranked)

        return {
            "version": "v1",
            "generated_at_utc": run_now.isoformat(),
            "window": window_label or f"{int(window_minutes)}m",
            "window_minutes": int(window_minutes),
            "event": {
                "timestamp_utc": event.timestamp_utc.isoformat(),
                "horizon_minutes": event.horizon_minutes,
                "return": event.return_value,
                "zscore": event.zscore,
                "direction": event.direction,
            },
            "summary": {
                **summary,
                "candidate_count": len(ranked),
                "llm_used": bool(self.llm_usage and llm_index),
            },
            "candidates": ranked_limited,
            "diagnostics": {
                "reason": "ok",
                "candidate_window_minutes": {
                    "lookback": self.candidate_lookback_minutes,
                    "lookahead": self.candidate_lookahead_minutes,
                },
            },
        }

    def _empty(
        self,
        *,
        classification: str,
        reason: str,
        now: datetime,
        window_minutes: int,
        window_label: str | None,
    ) -> dict[str, Any]:
        return {
            "version": "v1",
            "generated_at_utc": ensure_utc(now).isoformat(),
            "window": window_label or f"{int(window_minutes)}m",
            "window_minutes": int(window_minutes),
            "event": None,
            "summary": {
                "classification": classification,
                "news_driven_probability": None,
                "top_candidate_score": None,
                "candidate_count": 0,
                "llm_used": False,
            },
            "candidates": [],
            "diagnostics": {"reason": reason},
        }

    @staticmethod
    def _parse_horizons(raw: Any) -> list[int]:
        out: list[int] = []
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            try:
                if isinstance(value, (int, float)):
                    minutes = int(value)
                else:
                    minutes = int(parse_duration(str(value)).total_seconds() // 60)
            except (ValueError, TypeError):
                continue
            if minutes <= 0:
                continue
            out.append(minutes)
        dedup = sorted(set(out))
        return dedup or [15, 60]

    def _detect_latest_event(self, *, storage: Storage, ref_ts: datetime) -> PriceEvent | None:
        ref_epoch = int(ensure_utc(ref_ts).timestamp())
        end_minute = ref_epoch - (ref_epoch % 60)
        max_horizon = max(self.event_horizons_minutes)
        start_minute = end_minute - self.event_lookback_hours * 3600 - max_horizon * 60
        prices = storage.get_price_points_range(start_minute, end_minute)
        if len(prices) < (self.event_zscore_lookback_points + max_horizon + 2):
            return None

        events: list[PriceEvent] = []
        for horizon in self.event_horizons_minutes:
            events.extend(self._events_for_horizon(prices=prices, horizon_minutes=horizon, ref_end_minute=end_minute))
        if not events:
            return None
        events.sort(key=lambda e: e.ts_minute)
        return events[-1]

    def _events_for_horizon(
        self,
        *,
        prices: dict[int, float],
        horizon_minutes: int,
        ref_end_minute: int,
    ) -> list[PriceEvent]:
        step = int(horizon_minutes) * 60
        ts_sorted = sorted(prices.keys())
        returns: list[tuple[int, float]] = []
        for ts in ts_sorted:
            if ts > ref_end_minute:
                continue
            prev_ts = ts - step
            p0 = prices.get(prev_ts)
            p1 = prices.get(ts)
            if p0 is None or p1 is None or p0 <= 0:
                continue
            ret = (float(p1) - float(p0)) / float(p0)
            returns.append((ts, ret))
        if len(returns) <= self.event_zscore_lookback_points:
            return []

        accepted: list[PriceEvent] = []
        day_counts: dict[str, int] = {}
        last_ts_by_dir: dict[int, int] = {}
        cooldown_seconds = self.event_cooldown_minutes * 60
        for idx in range(self.event_zscore_lookback_points, len(returns)):
            ts, ret = returns[idx]
            past = [value for _, value in returns[idx - self.event_zscore_lookback_points : idx]]
            if not past:
                continue
            mean = sum(past) / len(past)
            var = sum((v - mean) ** 2 for v in past) / len(past)
            std = sqrt(max(0.0, var))
            if std <= 1e-12:
                continue
            z = (ret - mean) / std
            if abs(ret) < self.event_min_abs_return or abs(z) < self.event_zscore_threshold:
                continue

            direction = _sign(ret)
            if direction == 0:
                continue

            dt = datetime.fromtimestamp(ts, tz=UTC)
            day_key = f"{dt.date().isoformat()}:{horizon_minutes}"
            if day_counts.get(day_key, 0) >= self.event_max_per_day:
                continue
            last_ts = last_ts_by_dir.get(direction)
            if last_ts is not None and ts - last_ts < cooldown_seconds:
                continue

            accepted.append(
                PriceEvent(
                    timestamp_utc=dt,
                    ts_minute=ts,
                    horizon_minutes=horizon_minutes,
                    return_value=float(ret),
                    zscore=float(z),
                    direction=direction,
                )
            )
            day_counts[day_key] = day_counts.get(day_key, 0) + 1
            last_ts_by_dir[direction] = ts
        return accepted

    def _build_candidates(
        self,
        *,
        storage: Storage,
        learner: Learner | None,
        event: PriceEvent,
        llm_index: dict[str, float],
    ) -> list[dict[str, Any]]:
        start = event.timestamp_utc - timedelta(minutes=self.candidate_lookback_minutes)
        end = event.timestamp_utc + timedelta(minutes=self.candidate_lookahead_minutes)
        items = storage.get_items_between(start, end)
        if not items:
            return []

        out: list[dict[str, Any]] = []
        for item in items:
            candidate = self._score_candidate(
                storage=storage,
                learner=learner,
                event=event,
                item=item,
                llm_index=llm_index,
            )
            if candidate is not None:
                out.append(candidate)

        out.sort(key=lambda c: c["scores"]["raw_total"], reverse=True)
        return out[: self.max_candidates]

    def _score_candidate(
        self,
        *,
        storage: Storage,
        learner: Learner | None,
        event: PriceEvent,
        item: NewsItem,
        llm_index: dict[str, float],
    ) -> dict[str, Any] | None:
        item_ts = ensure_utc(item.timestamp_utc)
        lag_seconds = (event.timestamp_utc - item_ts).total_seconds()
        distance_minutes = abs(lag_seconds) / 60.0
        time_score = exp(-distance_minutes / self.time_decay_minutes)
        if lag_seconds < 0:
            time_score *= 0.85
        time_score = _clamp(time_score, 0.0, 1.0)

        learn_pred = 0.0
        if learner is not None:
            try:
                learn_pred = float(learner.predict_item_return_mixed(storage, item, event.horizon_minutes))
            except Exception:
                learn_pred = 0.0

        llm_contribution = self._matched_llm_contribution(item=item, llm_index=llm_index)
        llm_match = llm_contribution is not None
        llm_value = float(llm_contribution) if llm_contribution is not None else 0.0

        if (
            abs(float(item.impact)) < self.min_abs_impact
            and abs(learn_pred) < self.min_abs_learn_score
            and not llm_match
        ):
            return None

        votes: list[int] = []
        if abs(float(item.impact)) >= self.min_abs_impact:
            votes.append(_sign(float(item.impact)))
        if abs(learn_pred) >= self.min_abs_learn_score:
            votes.append(_sign(learn_pred))
        if llm_match and abs(llm_value) >= 1e-6:
            votes.append(_sign(llm_value))
        inferred_dir = _sign(float(sum(votes))) if votes else 0
        if inferred_dir == 0:
            direction_score = 0.5
            direction_match = None
        else:
            direction_match = bool(inferred_dir == event.direction)
            direction_score = 1.0 if direction_match else 0.0

        rule_score = _clamp(abs(float(item.impact)) / self.rule_norm_abs, 0.0, 1.0)
        learn_score = _clamp(abs(learn_pred) / self.learn_norm_abs, 0.0, 1.0)
        llm_score = _clamp(abs(llm_value) / self.llm_norm_abs, 0.0, 1.0) if llm_match else 0.0
        source_factor = self._source_quality_factor(item)

        raw_total = (
            self.weight_time * time_score
            + self.weight_direction * direction_score
            + self.weight_rule * rule_score
            + self.weight_learn * learn_score
            + self.weight_llm * llm_score
        )
        raw_total = _clamp(raw_total * source_factor, 0.0, 1.0)

        raw = item.raw if isinstance(item.raw, dict) else {}
        db_id = raw.get("_db_id")
        item_id = int(db_id) if isinstance(db_id, int) else int(db_id or 0)
        cluster_id = str(raw.get("event_cluster_id", "")).strip()
        if not cluster_id:
            cluster_id = hash_text(normalize_text(item.title))[:16]

        return {
            "item_id": item_id,
            "timestamp_utc": item_ts.isoformat(),
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "category": item.category,
            "polarity": item.polarity,
            "impact": float(item.impact),
            "lag_seconds": lag_seconds,
            "direction_match": direction_match,
            "event_cluster_id": cluster_id,
            "scores": {
                "raw_total": raw_total,
                "time": time_score,
                "direction": direction_score,
                "rule": rule_score,
                "learn": learn_score,
                "llm": llm_score,
                "source_factor": source_factor,
            },
            "learn_pred_return": learn_pred,
            "llm_contribution": llm_value if llm_match else None,
        }

    def _source_quality_factor(self, item: NewsItem) -> float:
        raw = item.raw if isinstance(item.raw, dict) else {}
        scoring = raw.get("scoring") if isinstance(raw.get("scoring"), dict) else {}
        try:
            factor = float(scoring.get("source_quality_factor", 1.0))
        except (TypeError, ValueError):
            factor = 1.0
        return _clamp(factor, self.source_factor_min, self.source_factor_max)

    def _apply_cluster_dampening(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for candidate in sorted(candidates, key=lambda c: c["scores"]["raw_total"], reverse=True):
            cluster = str(candidate.get("event_cluster_id") or "")
            repeat_idx = counts.get(cluster, 0)
            factor = float(self.cluster_decay) ** float(repeat_idx)
            candidate["scores"]["cluster_decay_factor"] = factor
            candidate["attribution_score"] = _clamp(float(candidate["scores"]["raw_total"]) * factor, 0.0, 1.0)
            counts[cluster] = repeat_idx + 1
        return sorted(candidates, key=lambda c: c.get("attribution_score", 0.0), reverse=True)

    def _summary(self, *, top_k: list[dict[str, Any]], all_candidates: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(all_candidates)
        if count == 0:
            return {
                "classification": "no_clear_news_driver",
                "news_driven_probability": 0.0,
                "top_candidate_score": 0.0,
            }

        top_score = float(top_k[0]["attribution_score"]) if top_k else 0.0
        mean_top = sum(float(c["attribution_score"]) for c in top_k) / max(1, len(top_k))
        direction_match_count = sum(1 for c in all_candidates if c.get("direction_match") is True)
        direction_share = float(direction_match_count) / float(max(1, count))
        probability = _clamp(0.55 * top_score + 0.35 * mean_top + 0.10 * direction_share, 0.0, 1.0)

        if count < 2:
            classification = "no_clear_news_driver"
        elif (
            probability >= self.news_driven_probability_threshold
            and top_score >= self.news_driven_top_score_threshold
        ):
            classification = "news_driven"
        elif probability >= self.mixed_probability_threshold:
            classification = "mixed"
        else:
            classification = "no_clear_news_driver"

        return {
            "classification": classification,
            "news_driven_probability": probability,
            "top_candidate_score": top_score,
        }

    def _llm_reason_index(self, llm_payload: object | None) -> dict[str, float]:
        if not self.llm_usage or llm_payload is None:
            return {}
        reasons = getattr(llm_payload, "reasons", None)
        if not isinstance(reasons, list):
            return {}
        index: dict[str, float] = {}
        for reason in reasons:
            if not isinstance(reason, dict):
                continue
            title = normalize_text(str(reason.get("title", "")).strip())
            if not title:
                continue
            try:
                contribution = float(reason.get("contribution", 0.0))
            except (TypeError, ValueError):
                contribution = 0.0
            prev = index.get(title)
            if prev is None or abs(contribution) > abs(prev):
                index[title] = contribution
        return index

    @staticmethod
    def _matched_llm_contribution(item: NewsItem, llm_index: dict[str, float]) -> float | None:
        if not llm_index:
            return None
        item_title_norm = normalize_text(item.title)
        if not item_title_norm:
            return None
        direct = llm_index.get(item_title_norm)
        if direct is not None:
            return direct
        best: tuple[float, float] | None = None
        for llm_title_norm, contribution in llm_index.items():
            if not llm_title_norm:
                continue
            if item_title_norm in llm_title_norm or llm_title_norm in item_title_norm:
                sim = 0.9
            else:
                sim = SequenceMatcher(a=item_title_norm, b=llm_title_norm).ratio()
            if sim < 0.72:
                continue
            if best is None or sim > best[0]:
                best = (sim, contribution)
        return best[1] if best is not None else None


def build_attribution_brief(attribution: dict[str, Any]) -> dict[str, Any]:
    summary = attribution.get("summary") if isinstance(attribution.get("summary"), dict) else {}
    event = attribution.get("event")
    candidates = attribution.get("candidates")
    top_candidate = None
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            top_candidate = {
                "item_id": first.get("item_id"),
                "source": first.get("source"),
                "title": first.get("title"),
                "score": first.get("attribution_score"),
                "direction_match": first.get("direction_match"),
                "lag_seconds": first.get("lag_seconds"),
            }
    return {
        "attribution_state": summary.get("classification", "not_applicable"),
        "news_driven_probability": summary.get("news_driven_probability"),
        "attribution_event": event if isinstance(event, dict) else None,
        "top_attribution": top_candidate,
        "attribution_version": str(attribution.get("version", "v1")),
    }
