from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import sqrt
import re
from typing import Any

import requests

from btc_news_arrow.models import ArrowResult, NewsItem
from btc_news_arrow.scorer import recency_decay
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import ensure_utc, parse_datetime, parse_duration, utcnow


def to_minute_epoch(value: datetime) -> int:
    ts = int(ensure_utc(value).timestamp())
    return (ts // 60) * 60


class BinanceMinutePriceClient:
    def __init__(self, endpoint: str = "https://api.binance.com", symbol: str = "BTCUSDT") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.symbol = symbol

    def fetch_closes(self, start_minute_epoch: int, end_minute_epoch: int) -> dict[int, float]:
        if end_minute_epoch < start_minute_epoch:
            return {}

        out: dict[int, float] = {}
        start_ms = start_minute_epoch * 1000
        end_ms = (end_minute_epoch + 60) * 1000 - 1

        while start_ms <= end_ms:
            params = {
                "symbol": self.symbol,
                "interval": "1m",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            }
            resp = requests.get(f"{self.endpoint}/api/v3/klines", params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break

            for kline in data:
                open_ms = int(kline[0])
                close_price = float(kline[4])
                out[open_ms // 1000] = close_price

            last_open_ms = int(data[-1][0])
            next_start = last_open_ms + 60_000
            if next_start <= start_ms:
                break
            start_ms = next_start
            if len(data) < 1000:
                break

        return out


class Learner:
    def __init__(self, config: dict[str, Any], price_client: BinanceMinutePriceClient | None = None) -> None:
        learning_cfg = config.get("learning", {})

        horizon_values = learning_cfg.get("horizons", ["60m", "24h", "1w"])
        self.horizons_minutes = sorted(
            {
                max(1, int(parse_duration(str(v)).total_seconds() // 60))
                for v in horizon_values
            }
        )

        market_cfg = learning_cfg.get("market", {})
        endpoint = str(market_cfg.get("endpoint", "https://api.binance.com"))
        symbol = str(market_cfg.get("symbol", "BTCUSDT"))
        self.price_client = price_client or BinanceMinutePriceClient(endpoint=endpoint, symbol=symbol)

        model_cfg = learning_cfg.get("model", {})
        self.confidence_k = float(model_cfg.get("confidence_k", 10.0))
        self.max_effective_n = max(1.0, float(model_cfg.get("max_effective_n", 200.0)))
        self.feature_recency_half_life_days = max(
            0.1, float(model_cfg.get("feature_recency_half_life_days", 7.0))
        )
        self.label_return_clip_abs = max(0.0, float(model_cfg.get("label_return_clip_abs", 0.25)))
        self.label_deadzone_abs = max(0.0, float(model_cfg.get("label_deadzone_abs", 0.0)))
        self.label_sqrt_time_normalize = bool(model_cfg.get("label_sqrt_time_normalize", False))
        vol_cfg = model_cfg.get("label_volatility_adjust", {})
        self.label_vol_adjust_enabled = bool(vol_cfg.get("enabled", False))
        self.label_vol_window_minutes = max(30, int(vol_cfg.get("window_minutes", 360)))
        self.label_vol_min_returns = max(5, int(vol_cfg.get("min_returns", 20)))
        self.label_vol_target = max(1e-6, float(vol_cfg.get("target_volatility", 0.02)))
        self.label_vol_floor = max(1e-6, float(vol_cfg.get("floor_volatility", 0.001)))
        self.label_vol_min_scale = max(0.01, float(vol_cfg.get("min_scale", 0.5)))
        self.label_vol_max_scale = max(self.label_vol_min_scale, float(vol_cfg.get("max_scale", 2.0)))
        self.window_horizon_mix = self._load_window_horizon_mix(
            model_cfg.get("window_horizon_mix", {})
        )

        arrow_cfg = learning_cfg.get("arrow", {})
        self.arrow_scale = float(arrow_cfg.get("scale", 100.0))
        self.thresholds = {str(k): float(v) for k, v in arrow_cfg.get("thresholds", {}).items()}

        self.half_life_minutes = float(config.get("decay", {}).get("half_life_minutes", 720))
        self.trigger_terms = [
            str(k).lower() for k in config.get("trigger_multipliers", {}).keys()
        ]
        self.feature_keywords = self._load_feature_keywords(config)

    def update_model(self, storage: Storage, limit_per_horizon: int = 500, now: datetime | None = None) -> dict[str, int]:
        run_now = ensure_utc(now or utcnow())
        inserted_labels = 0
        clipped_labels = 0
        deadzone_zeroed = 0
        vol_adjusted = 0
        feature_updates: dict[tuple[str, int], tuple[int, float]] = defaultdict(lambda: (0, 0.0))

        for horizon in self.horizons_minutes:
            cutoff = run_now - timedelta(minutes=horizon)
            items = storage.get_unlabeled_items(horizon_minutes=horizon, cutoff_ts=cutoff, limit=limit_per_horizon)
            if not items:
                continue

            required_minutes: set[int] = set()
            for _, item in items:
                start_m = to_minute_epoch(item.timestamp_utc)
                end_m = start_m + horizon * 60
                required_minutes.add(start_m)
                required_minutes.add(end_m)
                if self.label_vol_adjust_enabled:
                    required_minutes.add(start_m - self.label_vol_window_minutes * 60)

            min_m = min(required_minutes)
            max_m = max(required_minutes)
            prices = storage.get_price_points_range(min_m, max_m)

            if not required_minutes.issubset(prices.keys()):
                try:
                    fetched = self.price_client.fetch_closes(min_m, max_m)
                except requests.RequestException:
                    fetched = {}
                if fetched:
                    storage.upsert_price_points(fetched)
                    prices.update(fetched)

            labels_to_upsert: list[tuple[int, int, float]] = []
            for item_id, item in items:
                start_m = to_minute_epoch(item.timestamp_utc)
                end_m = start_m + horizon * 60
                p0 = prices.get(start_m)
                p1 = prices.get(end_m)
                if p0 is None or p1 is None or p0 <= 0:
                    continue

                raw_ret = (p1 - p0) / p0
                ret, meta = self._normalize_label_return(
                    raw_ret=raw_ret,
                    horizon_minutes=horizon,
                    start_minute_epoch=start_m,
                    prices=prices,
                )
                if meta["vol_adjusted"]:
                    vol_adjusted += 1
                if meta["deadzone_zeroed"]:
                    deadzone_zeroed += 1
                if abs(ret - raw_ret) > 1e-12:
                    clipped_labels += 1
                labels_to_upsert.append((item_id, horizon, ret))
                inserted_labels += 1

                for feature in self._feature_keys(item):
                    n, s = feature_updates[(feature, horizon)]
                    feature_updates[(feature, horizon)] = (n + 1, s + ret)

            storage.upsert_item_labels(labels_to_upsert)

        if feature_updates:
            storage.apply_feature_updates(feature_updates)

        return {
            "labeled": inserted_labels,
            "clipped_labels": clipped_labels,
            "vol_adjusted": vol_adjusted,
            "deadzone_zeroed": deadzone_zeroed,
            "feature_updates": len(feature_updates),
        }

    def learned_arrow(self, storage: Storage, window_minutes: int, now: datetime | None = None) -> ArrowResult:
        run_now = ensure_utc(now or utcnow())

        since = run_now - timedelta(minutes=window_minutes)
        items = [i for i in storage.get_items_since(since) if ensure_utc(i.timestamp_utc) <= run_now]

        weighted_score_sum = 0.0
        decay_weight_sum = 0.0
        for item in items:
            pred_ret = self.predict_item_return_mixed(storage, item, window_minutes)
            age_seconds = (run_now - ensure_utc(item.timestamp_utc)).total_seconds()
            decay = recency_decay(age_seconds, self.half_life_minutes)
            weighted_score_sum += pred_ret * self.arrow_scale * decay
            decay_weight_sum += decay

        # Normalize by active decay mass so longer windows with many items do
        # not trivially saturate downstream hybrid clipping.
        score = weighted_score_sum / decay_weight_sum if decay_weight_sum > 0 else 0.0

        threshold = self._threshold_for_window(window_minutes)
        arrow = self._score_to_arrow(score, threshold)
        return ArrowResult(window_minutes=window_minutes, score=score, arrow=arrow, contributing_items=len(items))

    def predict_item_return(self, storage: Storage, item: NewsItem, horizon_minutes: int) -> float:
        features = self._feature_keys(item)
        stats = storage.get_feature_stats_with_meta(features, horizon_minutes)
        now = utcnow()

        weighted_sum = 0.0
        weight_total = 0.0
        for feature, (n, mean_return, updated_at) in stats.items():
            if n <= 0:
                continue
            effective_n = min(float(n), self.max_effective_n)
            freshness = self._feature_freshness_multiplier(updated_at, now=now)
            weight = (effective_n / (effective_n + self.confidence_k)) * freshness
            if weight <= 0:
                continue
            weighted_sum += weight * mean_return
            weight_total += weight

        if weight_total <= 0:
            return 0.0
        return weighted_sum / weight_total

    def predict_item_return_mixed(self, storage: Storage, item: NewsItem, window_minutes: int) -> float:
        mix = self._mix_for_window(window_minutes)
        weighted = 0.0
        total = 0.0
        for horizon_minutes, horizon_weight in mix:
            pred = self.predict_item_return(storage, item, horizon_minutes)
            weighted += horizon_weight * pred
            total += horizon_weight
        if total <= 0:
            return 0.0
        return weighted / total

    def _feature_keys(self, item: NewsItem) -> list[str]:
        features = [
            "__bias__",
            f"source:{item.source}",
            f"source_group:{self._source_group(item.source)}",
            f"category:{item.category}",
            f"polarity:{item.polarity}",
        ]

        text = f"{item.title} {item.summary}".lower()
        for trigger in self.trigger_terms:
            if trigger in text:
                features.append(f"trigger:{trigger}")

        if re.search(r"\d", text):
            features.append("has_number:1")

        if item.impact >= 0.1:
            features.append("impact_sign:pos")
        elif item.impact <= -0.1:
            features.append("impact_sign:neg")
        else:
            features.append("impact_sign:flat")

        raw = item.raw if isinstance(item.raw, dict) else {}
        scoring = raw.get("scoring") if isinstance(raw.get("scoring"), dict) else {}
        resolved_direction = scoring.get("resolved_direction")
        if resolved_direction in {-1, 0, 1}:
            features.append(f"rule_direction:{int(resolved_direction)}")

        relevance = self._safe_float(scoring.get("btc_relevance_multiplier"))
        if relevance is not None:
            features.append(f"relevance:{self._relevance_bucket(relevance)}")

        category_mult = self._safe_float(scoring.get("category_multiplier"))
        if category_mult is not None:
            features.append(f"category_mult:{self._magnitude_bucket(category_mult)}")

        trigger_mult = self._safe_float(scoring.get("trigger_multiplier"))
        if trigger_mult is not None:
            features.append(f"trigger_mult:{self._magnitude_bucket(trigger_mult)}")

        keyword_hits = 0
        for kw in self.feature_keywords:
            if self._contains_keyword(text, kw):
                features.append(f"text_kw:{kw}")
                keyword_hits += 1
                if keyword_hits >= 4:
                    break

        # Keep order stable while deduplicating.
        deduped: list[str] = []
        seen: set[str] = set()
        for feature in features:
            if feature in seen:
                continue
            seen.add(feature)
            deduped.append(feature)
        return deduped

    def _nearest_horizon(self, window_minutes: int) -> int:
        return min(self.horizons_minutes, key=lambda h: abs(h - window_minutes))

    def _mix_for_window(self, window_minutes: int) -> list[tuple[int, float]]:
        key = f"{window_minutes}m"
        if key in self.window_horizon_mix:
            mix = self.window_horizon_mix[key]
            if mix:
                return mix

        if "default" in self.window_horizon_mix and self.window_horizon_mix["default"]:
            return self.window_horizon_mix["default"]

        return [(self._nearest_horizon(window_minutes), 1.0)]

    def _load_window_horizon_mix(self, raw: Any) -> dict[str, list[tuple[int, float]]]:
        if not isinstance(raw, dict):
            return {}

        out: dict[str, list[tuple[int, float]]] = {}
        for window_key, horizon_map in raw.items():
            if not isinstance(horizon_map, dict):
                continue
            parsed: list[tuple[int, float]] = []
            for horizon_value, weight in horizon_map.items():
                try:
                    horizon_minutes = max(
                        1, int(parse_duration(str(horizon_value)).total_seconds() // 60)
                    )
                    weight_f = float(weight)
                except (ValueError, TypeError):
                    continue
                if horizon_minutes not in self.horizons_minutes:
                    continue
                if weight_f <= 0:
                    continue
                parsed.append((horizon_minutes, weight_f))

            total = sum(w for _, w in parsed)
            if total > 0:
                out[str(window_key)] = [(h, w / total) for h, w in parsed]

        return out

    def _threshold_for_window(self, window_minutes: int) -> float:
        key = f"{window_minutes}m"
        if key in self.thresholds:
            return self.thresholds[key]
        return float(self.thresholds.get("default", 0.35))

    def _feature_freshness_multiplier(self, updated_at: str, now: datetime) -> float:
        updated = parse_datetime(updated_at)
        if updated is None:
            return 1.0
        age_days = max(0.0, (ensure_utc(now) - ensure_utc(updated)).total_seconds() / 86400.0)
        return 0.5 ** (age_days / self.feature_recency_half_life_days)

    @staticmethod
    def _score_to_arrow(score: float, threshold: float) -> str:
        if abs(score) < threshold:
            return "▬"
        strength = min(3, max(1, int(abs(score) / max(threshold, 1e-9))))
        return ("▲" if score > 0 else "▼") * strength

    @staticmethod
    def _source_group(source: str) -> str:
        s = source.strip().lower()
        if s.startswith("gdelt"):
            return "gdelt"
        if s in {"coinbase_status", "kraken_status"}:
            return "exchange_status"
        if s.startswith("bls") or s in {"fed", "ecb", "dol_releases", "fed_speeches"}:
            return "macro_official"
        if s in {"sec", "sec_litigation", "cftc"}:
            return "regulator"
        return "other"

    @staticmethod
    def _safe_float(value: object) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _relevance_bucket(value: float) -> str:
        if value >= 0.75:
            return "high"
        if value >= 0.35:
            return "mid"
        return "low"

    @staticmethod
    def _magnitude_bucket(value: float) -> str:
        if value >= 1.2:
            return "high"
        if value >= 0.85:
            return "mid"
        return "low"

    def _load_feature_keywords(self, config: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        hybrid_keywords = config.get("hybrid", {}).get("keywords_strong", [])
        if isinstance(hybrid_keywords, list):
            candidates.extend(str(v).strip().lower() for v in hybrid_keywords)

        heuristic_keywords = config.get("heuristics", {}).get("crypto_relevance_terms", [])
        if isinstance(heuristic_keywords, list):
            candidates.extend(str(v).strip().lower() for v in heuristic_keywords)

        out: list[str] = []
        seen: set[str] = set()
        for kw in candidates:
            if not kw:
                continue
            # Keep feature dimensionality bounded and stable.
            if len(kw) < 3 or len(kw) > 20:
                continue
            if kw in seen:
                continue
            seen.add(kw)
            out.append(kw)
            if len(out) >= 24:
                break
        return out

    @staticmethod
    def _contains_keyword(text: str, keyword: str) -> bool:
        if re.fullmatch(r"[a-z0-9]+", keyword):
            return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
        return keyword in text

    def _clip_label_return(self, value: float) -> float:
        clip = float(self.label_return_clip_abs)
        if clip <= 0:
            return float(value)
        return max(-clip, min(clip, float(value)))

    def _normalize_label_return(
        self,
        *,
        raw_ret: float,
        horizon_minutes: int,
        start_minute_epoch: int,
        prices: dict[int, float],
    ) -> tuple[float, dict[str, bool]]:
        value = float(raw_ret)
        vol_adjusted = False
        deadzone_zeroed = False

        if self.label_sqrt_time_normalize and horizon_minutes > 0:
            value = value / max(1.0, sqrt(float(horizon_minutes) / 60.0))

        if self.label_vol_adjust_enabled:
            vol = self._trailing_return_volatility(
                prices=prices,
                end_minute_epoch=start_minute_epoch,
                window_minutes=self.label_vol_window_minutes,
            )
            if vol is not None:
                scale = self.label_vol_target / max(self.label_vol_floor, vol)
                scale = max(self.label_vol_min_scale, min(self.label_vol_max_scale, scale))
                value *= scale
                vol_adjusted = True

        if self.label_deadzone_abs > 0 and abs(value) < self.label_deadzone_abs:
            value = 0.0
            deadzone_zeroed = True

        value = self._clip_label_return(value)
        return value, {"vol_adjusted": vol_adjusted, "deadzone_zeroed": deadzone_zeroed}

    def _trailing_return_volatility(
        self,
        *,
        prices: dict[int, float],
        end_minute_epoch: int,
        window_minutes: int,
    ) -> float | None:
        start_epoch = int(end_minute_epoch) - max(1, int(window_minutes)) * 60
        closes = [
            (ts, prices[ts])
            for ts in sorted(prices.keys())
            if start_epoch <= ts <= int(end_minute_epoch)
        ]
        if len(closes) <= self.label_vol_min_returns:
            return None

        rets: list[float] = []
        prev_price = None
        for _, price in closes:
            if prev_price is None:
                prev_price = price
                continue
            if prev_price <= 0:
                prev_price = price
                continue
            rets.append((price - prev_price) / prev_price)
            prev_price = price
        if len(rets) < self.label_vol_min_returns:
            return None

        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        vol = sqrt(max(0.0, var))
        if vol <= 0:
            return None
        return vol
