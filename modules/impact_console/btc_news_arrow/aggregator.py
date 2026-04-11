from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from btc_news_arrow.models import ArrowResult, NewsItem
from btc_news_arrow.scorer import recency_decay
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import ensure_utc, hash_text, normalize_text


class Aggregator:
    def __init__(self, config: dict[str, Any]) -> None:
        self.half_life_minutes = float(config.get("decay", {}).get("half_life_minutes", 45))
        self.thresholds = {
            str(k): float(v) for k, v in config.get("thresholds", {}).items()
        }
        aggregation_cfg = config.get("aggregation", {})
        self.source_repeat_half_life_items = float(
            aggregation_cfg.get("source_repeat_half_life_items", 3.0)
        )
        self.cluster_repeat_half_life_items = float(
            aggregation_cfg.get("cluster_repeat_half_life_items", 2.0)
        )

    def arrow_for_window(
        self,
        items: list[NewsItem],
        window_minutes: int,
        now: datetime,
    ) -> ArrowResult:
        contributions = self.contributions_for_window(items, window_minutes, now)
        score = sum(c for c, _ in contributions)

        threshold = self._threshold_for_window(window_minutes)
        arrow = self._score_to_arrow(score, threshold)

        return ArrowResult(
            window_minutes=window_minutes,
            score=score,
            arrow=arrow,
            contributing_items=len(contributions),
        )

    def arrow_from_storage(
        self,
        storage: Storage,
        window_minutes: int,
        now: datetime,
    ) -> ArrowResult:
        since = ensure_utc(now) - timedelta(minutes=window_minutes)
        items = storage.get_items_since(since)
        return self.arrow_for_window(items, window_minutes=window_minutes, now=now)

    def contributions_for_window(
        self,
        items: list[NewsItem],
        window_minutes: int,
        now: datetime,
    ) -> list[tuple[float, NewsItem]]:
        now_utc = ensure_utc(now)
        window_start = now_utc - timedelta(minutes=window_minutes)
        filtered = [
            i
            for i in items
            if window_start <= ensure_utc(i.timestamp_utc) <= now_utc
        ]

        out: list[tuple[float, NewsItem]] = []
        source_counts: dict[str, int] = {}
        cluster_counts: dict[str, int] = {}
        for item in filtered:
            age_seconds = (now_utc - ensure_utc(item.timestamp_utc)).total_seconds()
            contribution = item.impact * recency_decay(age_seconds, self.half_life_minutes)
            repeat_idx = source_counts.get(item.source, 0)
            source_counts[item.source] = repeat_idx + 1
            contribution *= self._source_repeat_multiplier(repeat_idx)
            cluster_key = self._event_cluster_key(item)
            cluster_repeat_idx = cluster_counts.get(cluster_key, 0)
            cluster_counts[cluster_key] = cluster_repeat_idx + 1
            contribution *= self._cluster_repeat_multiplier(cluster_repeat_idx)
            out.append((contribution, item))
        return out

    def _threshold_for_window(self, window_minutes: int) -> float:
        key = f"{window_minutes}m"
        if key in self.thresholds:
            return self.thresholds[key]
        return self.thresholds.get("default", 0.22)

    @staticmethod
    def _score_to_arrow(score: float, threshold: float) -> str:
        if abs(score) < threshold:
            return "▬"

        # 1-3 arrows based on strength over threshold.
        strength = min(3, max(1, int(abs(score) / max(threshold, 1e-9))))
        if score > 0:
            return "▲" * strength
        return "▼" * strength

    def _source_repeat_multiplier(self, repeat_idx: int) -> float:
        if self.source_repeat_half_life_items <= 0:
            return 1.0
        return 0.5 ** (float(max(0, repeat_idx)) / self.source_repeat_half_life_items)

    def _cluster_repeat_multiplier(self, repeat_idx: int) -> float:
        if self.cluster_repeat_half_life_items <= 0:
            return 1.0
        return 0.5 ** (float(max(0, repeat_idx)) / self.cluster_repeat_half_life_items)

    @staticmethod
    def _event_cluster_key(item: NewsItem) -> str:
        raw = item.raw if isinstance(item.raw, dict) else {}
        cluster_id = str(raw.get("event_cluster_id", "")).strip()
        if cluster_id:
            return cluster_id
        normalized = normalize_text(item.title)
        if not normalized:
            normalized = "unknown"
        return hash_text(normalized)[:16]
