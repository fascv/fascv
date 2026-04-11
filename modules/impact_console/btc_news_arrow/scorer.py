from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from btc_news_arrow.models import NewsItem
from btc_news_arrow.utils import ensure_utc, parse_datetime


def recency_decay(age_seconds: float, half_life_minutes: float) -> float:
    if age_seconds < 0:
        return 0.0
    if half_life_minutes <= 0:
        return 1.0
    half_life_seconds = half_life_minutes * 60.0
    return 0.5 ** (max(0.0, age_seconds) / half_life_seconds)


class Scorer:
    def __init__(self, config: dict[str, Any]) -> None:
        self.source_weights = {
            str(k): float(v) for k, v in config.get("source_weights", {}).items()
        }
        source_quality_cfg = config.get("source_quality_weighting", {})
        self.source_quality_enabled = bool(source_quality_cfg.get("enabled", True))
        self.source_quality_report_path = Path(
            str(source_quality_cfg.get("report_path", "diagnostics/source_quality_latest.json"))
        )
        self.source_quality_window = str(source_quality_cfg.get("window", "1h"))
        self.source_quality_min_samples = max(1, int(source_quality_cfg.get("min_samples", 8)))
        self.source_quality_shrinkage_samples = max(
            1, int(source_quality_cfg.get("shrinkage_samples", 40))
        )
        self.source_quality_corr_weight = float(source_quality_cfg.get("corr_weight", 0.35))
        self.source_quality_directional_weight = float(
            source_quality_cfg.get("directional_weight", 0.25)
        )
        self.source_quality_baseline_directional = float(
            source_quality_cfg.get("baseline_directional_accuracy", 0.5)
        )
        self.source_quality_min_multiplier = float(source_quality_cfg.get("min_multiplier", 0.75))
        self.source_quality_max_multiplier = float(source_quality_cfg.get("max_multiplier", 1.25))
        self.source_quality_unknown_multiplier = float(
            source_quality_cfg.get("unknown_source_multiplier", 1.0)
        )
        self.source_quality_stale_after_minutes = max(
            0, int(source_quality_cfg.get("stale_after_minutes", 720))
        )
        self._source_quality_factor_cache: dict[str, float] = {}
        self._source_quality_cache_loaded_at: datetime | None = None

        self.category_weights = {
            str(k): float(v) for k, v in config.get("category_weights", {}).items()
        }
        self.trigger_multipliers = {
            str(k).lower(): float(v)
            for k, v in config.get("trigger_multipliers", {}).items()
        }
        heuristics = config.get("heuristics", {})
        self.context_direction_weight = float(heuristics.get("context_direction_weight", 0.7))
        self.direction_deadzone = float(heuristics.get("direction_deadzone", 0.15))
        self.macro_surprise_multiplier = float(heuristics.get("macro_surprise_multiplier", 1.3))
        self.macro_in_line_multiplier = float(heuristics.get("macro_in_line_multiplier", 0.75))
        self.regulation_final_multiplier = float(heuristics.get("regulation_final_multiplier", 1.2))
        self.regulation_preliminary_multiplier = float(heuristics.get("regulation_preliminary_multiplier", 0.7))
        self.exchange_state_multipliers = {
            str(k).lower(): float(v)
            for k, v in heuristics.get(
                "exchange_state_multipliers",
                {
                    "investigating": 1.2,
                    "identified": 1.1,
                    "monitoring": 0.85,
                    "resolved": 0.45,
                },
            ).items()
        }
        self.regulation_non_crypto_multiplier = float(heuristics.get("regulation_non_crypto_multiplier", 0.0))
        self.macro_commentary_multiplier = float(heuristics.get("macro_commentary_multiplier", 0.35))
        self.exchange_altcoin_only_multiplier = float(heuristics.get("exchange_altcoin_only_multiplier", 0.25))
        self.exchange_minor_ops_multiplier = float(heuristics.get("exchange_minor_ops_multiplier", 0.45))
        self.exchange_core_ops_multiplier = float(heuristics.get("exchange_core_ops_multiplier", 1.0))
        self.other_non_crypto_multiplier = float(heuristics.get("other_non_crypto_multiplier", 0.5))
        self.macro_cpi_bullish_max = float(heuristics.get("macro_cpi_bullish_max", 0.2))
        self.macro_cpi_bearish_min = float(heuristics.get("macro_cpi_bearish_min", 0.4))
        self.macro_core_cpi_bullish_max = float(heuristics.get("macro_core_cpi_bullish_max", 0.3))
        self.macro_core_cpi_bearish_min = float(heuristics.get("macro_core_cpi_bearish_min", 0.4))
        self.macro_ppi_bullish_max = float(heuristics.get("macro_ppi_bullish_max", 0.2))
        self.macro_ppi_bearish_min = float(heuristics.get("macro_ppi_bearish_min", 0.4))
        self.macro_numeric_signal_min_abs = float(heuristics.get("macro_numeric_signal_min_abs", 0.4))

        self.macro_positive_surprise_terms = [
            str(s).lower()
            for s in heuristics.get(
                "macro_positive_surprise_terms",
                [
                    "lower than expected",
                    "below expectations",
                    "cooler than expected",
                    "inflation eased",
                    "disinflation",
                    "rate cut",
                ],
            )
        ]
        self.macro_negative_surprise_terms = [
            str(s).lower()
            for s in heuristics.get(
                "macro_negative_surprise_terms",
                [
                    "higher than expected",
                    "above expectations",
                    "hotter than expected",
                    "sticky inflation",
                    "inflation accelerated",
                    "rate hike",
                ],
            )
        ]
        self.macro_in_line_terms = [
            str(s).lower()
            for s in heuristics.get(
                "macro_in_line_terms",
                [
                    "as expected",
                    "in line with expectations",
                    "in-line with expectations",
                    "unchanged as expected",
                ],
            )
        ]
        self.regulation_final_terms = [
            str(s).lower()
            for s in heuristics.get(
                "regulation_final_terms",
                [
                    "approved",
                    "approval granted",
                    "order issued",
                    "settled",
                    "settlement",
                    "lawsuit",
                    "charges",
                    "sued",
                    "ban",
                    "rejected",
                    "denied",
                ],
            )
        ]
        self.regulation_preliminary_terms = [
            str(s).lower()
            for s in heuristics.get(
                "regulation_preliminary_terms",
                [
                    "proposal",
                    "proposed",
                    "draft",
                    "consultation",
                    "comment period",
                    "interview",
                    "speech",
                    "remarks",
                ],
            )
        ]
        self.crypto_relevance_terms = [
            str(s).lower()
            for s in heuristics.get(
                "crypto_relevance_terms",
                [
                    "bitcoin",
                    "btc",
                    "crypto",
                    "cryptocurrency",
                    "stablecoin",
                    "etf",
                    "exchange",
                    "coinbase",
                    "kraken",
                    "binance",
                ],
            )
        ]
        self.regulation_crypto_terms = [
            str(s).lower()
            for s in heuristics.get(
                "regulation_crypto_terms",
                [
                    "crypto",
                    "digital asset",
                    "bitcoin",
                    "btc",
                    "exchange",
                    "stablecoin",
                    "token",
                    "etf",
                    "blockchain",
                    "custody",
                ],
            )
        ]
        self.macro_commentary_terms = [
            str(s).lower()
            for s in heuristics.get(
                "macro_commentary_terms",
                [
                    "interview",
                    "speech",
                    "remarks",
                    "plenary debate",
                    "podcast",
                    "op-ed",
                ],
            )
        ]
        self.exchange_minor_terms = [
            str(s).lower()
            for s in heuristics.get(
                "exchange_minor_terms",
                [
                    "email notification",
                    "account-related email",
                    "guest checkout",
                    "funding delays",
                    "delayed sends",
                    "delayed receives",
                ],
            )
        ]
        self.exchange_core_terms = [
            str(s).lower()
            for s in heuristics.get(
                "exchange_core_terms",
                [
                    "trading",
                    "api",
                    "websocket",
                    "website",
                    "transactions",
                    "onramp",
                    "derivatives",
                    "connectivity",
                    "latency",
                    "degraded performance",
                    "withdrawal",
                    "deposit",
                    "order",
                    "orders",
                ],
            )
        ]

    def score(self, item: NewsItem, now: datetime) -> NewsItem:
        now_utc = ensure_utc(now)
        text = f"{item.title} {item.summary}"
        source_base_weight = self._source_weight(item.source)
        source_quality_factor = self._source_quality_factor(item.source, now=now_utc)
        source_weight = source_base_weight * source_quality_factor
        category_weight = self.category_weights.get(item.category, self.category_weights.get("other", 0.5))
        trigger_mult = self._trigger_multiplier(text)
        direction = self._resolve_direction(item=item, text=text)
        category_mult = self._category_multiplier(category=item.category, text=text)
        relevance_mult = self._btc_relevance_multiplier(item=item, text=text)

        base = float(direction) * source_weight * category_weight * trigger_mult * category_mult * relevance_mult
        # Persist raw event impact only; time decay is applied in aggregation.
        item.impact = max(-1.0, min(1.0, base))
        if isinstance(item.raw, dict):
            item.raw["scoring"] = {
                "source_weight": source_weight,
                "source_base_weight": source_base_weight,
                "source_quality_factor": source_quality_factor,
                "category_weight": category_weight,
                "trigger_multiplier": trigger_mult,
                "category_multiplier": category_mult,
                "btc_relevance_multiplier": relevance_mult,
                "resolved_direction": direction,
            }
        return item

    def _source_weight(self, source: str) -> float:
        if source in self.source_weights:
            return self.source_weights[source]
        if source.startswith("gdelt"):
            return self.source_weights.get("gdelt", 0.6)
        return 0.5

    def _source_quality_factor(self, source: str, now: datetime) -> float:
        if not self.source_quality_enabled:
            return 1.0
        self._refresh_source_quality_cache(now=now)
        if source in self._source_quality_factor_cache:
            return self._source_quality_factor_cache[source]
        if source.startswith("gdelt:"):
            domain_key = source.split(":", 1)[1].strip().lower()
            if domain_key in self._source_quality_factor_cache:
                return self._source_quality_factor_cache[domain_key]
        return self.source_quality_unknown_multiplier

    def _refresh_source_quality_cache(self, now: datetime) -> None:
        if self._source_quality_cache_loaded_at is not None:
            age_seconds = (now - self._source_quality_cache_loaded_at).total_seconds()
            if age_seconds < 300:
                return
        self._source_quality_cache_loaded_at = now
        self._source_quality_factor_cache = self._load_source_quality_factors(now=now)

    def _load_source_quality_factors(self, now: datetime) -> dict[str, float]:
        path = self.source_quality_report_path
        if not path.exists() or not path.is_file():
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}

        if self.source_quality_stale_after_minutes > 0:
            generated = parse_datetime(str(payload.get("generated_at_utc")))
            if generated is not None:
                age_minutes = (ensure_utc(now) - ensure_utc(generated)).total_seconds() / 60.0
                if age_minutes > float(self.source_quality_stale_after_minutes):
                    return {}

        current = payload.get("current")
        if not isinstance(current, dict):
            return {}
        by_window = current.get("by_window")
        if not isinstance(by_window, dict):
            return {}

        selected_window = self.source_quality_window
        window_payload = by_window.get(selected_window)
        if not isinstance(window_payload, dict):
            # Fallback to first available window.
            for _, candidate in by_window.items():
                if isinstance(candidate, dict):
                    window_payload = candidate
                    break
        if not isinstance(window_payload, dict):
            return {}

        sources = window_payload.get("sources")
        if not isinstance(sources, list):
            return {}

        out: dict[str, float] = {}
        for row in sources:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source", "")).strip()
            if not source:
                continue
            try:
                samples = int(row.get("samples") or 0)
            except (TypeError, ValueError):
                samples = 0
            if samples < self.source_quality_min_samples:
                continue

            corr = self._safe_float(row.get("corr"), default=0.0)
            dir_acc = self._safe_float(
                row.get("directional_accuracy"),
                default=self.source_quality_baseline_directional,
            )
            dir_edge = max(
                -1.0,
                min(
                    1.0,
                    (dir_acc - self.source_quality_baseline_directional) * 2.0,
                ),
            )
            raw_factor = (
                1.0
                + self.source_quality_corr_weight * corr
                + self.source_quality_directional_weight * dir_edge
            )
            shrink = min(1.0, float(samples) / float(self.source_quality_shrinkage_samples))
            factor = 1.0 + (raw_factor - 1.0) * shrink
            factor = max(self.source_quality_min_multiplier, min(self.source_quality_max_multiplier, factor))
            out[source] = factor
        return out

    @staticmethod
    def _safe_float(value: object, default: float) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _trigger_multiplier(self, text: str) -> float:
        txt = text.lower()
        mult = 1.0
        for trigger, value in self.trigger_multipliers.items():
            if trigger in txt:
                mult = max(mult, value)
        return mult

    def _resolve_direction(self, item: NewsItem, text: str) -> int:
        base_direction = int(item.polarity)
        context_direction = self._context_direction(item, text)
        score = float(base_direction) + float(context_direction) * self.context_direction_weight
        if score > self.direction_deadzone:
            return 1
        if score < -self.direction_deadzone:
            return -1
        return 0

    def _context_direction(self, item: NewsItem, text: str) -> int:
        category = item.category
        txt = text.lower()

        if category == "macro":
            numeric = self._macro_numeric_direction(item.source, txt)
            if numeric != 0:
                return numeric
            pos = self._contains_any(txt, self.macro_positive_surprise_terms)
            neg = self._contains_any(txt, self.macro_negative_surprise_terms)
            if pos and not neg:
                return 1
            if neg and not pos:
                return -1
            return 0

        if category == "regulation":
            pos = self._contains_any(
                txt,
                [
                    "approved",
                    "approval granted",
                    "legal clarity",
                    "allowed",
                    "authorized",
                ],
            )
            neg = self._contains_any(
                txt,
                [
                    "lawsuit",
                    "charges",
                    "enforcement",
                    "rejected",
                    "denied",
                    "ban",
                ],
            )
            if pos and not neg:
                return 1
            if neg and not pos:
                return -1
            return 0

        return 0

    def _category_multiplier(self, category: str, text: str) -> float:
        txt = text.lower()
        mult = 1.0

        if category == "macro":
            if self._contains_any(txt, self.macro_positive_surprise_terms) or self._contains_any(
                txt, self.macro_negative_surprise_terms
            ):
                mult *= self.macro_surprise_multiplier
            elif self._contains_any(txt, self.macro_in_line_terms):
                mult *= self.macro_in_line_multiplier

        if category == "regulation":
            if self._contains_any(txt, self.regulation_final_terms):
                mult *= self.regulation_final_multiplier
            elif self._contains_any(txt, self.regulation_preliminary_terms):
                mult *= self.regulation_preliminary_multiplier

        if category == "exchange_incident":
            incident_state = self._incident_state(txt)
            if incident_state:
                mult *= self.exchange_state_multipliers.get(incident_state, 1.0)

        return mult

    def _btc_relevance_multiplier(self, item: NewsItem, text: str) -> float:
        txt = text.lower()
        category = item.category
        mult = 1.0
        has_crypto_context = self._contains_any(txt, self.crypto_relevance_terms)

        if category == "regulation":
            if not self._contains_any(txt, self.regulation_crypto_terms):
                mult *= self.regulation_non_crypto_multiplier

        if category == "macro":
            has_macro_surprise = self._contains_any(txt, self.macro_positive_surprise_terms) or self._contains_any(
                txt, self.macro_negative_surprise_terms
            )
            if self._contains_any(txt, self.macro_commentary_terms) and not has_macro_surprise:
                mult *= self.macro_commentary_multiplier

        if category == "exchange_incident":
            if self._is_altcoin_operational_issue(txt):
                mult *= self.exchange_altcoin_only_multiplier
            elif self._contains_any(txt, self.exchange_minor_terms):
                mult *= self.exchange_minor_ops_multiplier
            elif self._contains_any(txt, self.exchange_core_terms):
                mult *= self.exchange_core_ops_multiplier

        if category == "other" and not has_crypto_context:
            mult *= self.other_non_crypto_multiplier

        return max(0.0, mult)

    @staticmethod
    def _contains_any(text: str, terms: list[str]) -> bool:
        return any(term in text for term in terms)

    @staticmethod
    def _incident_state(text: str) -> str | None:
        for state in ("investigating", "identified", "monitoring", "resolved"):
            if state in text:
                return state
        return None

    def _macro_numeric_direction(self, source: str, text: str) -> int:
        # Numeric macro parsing for official labor/inflation feeds to avoid
        # dropping key releases into neutral when no explicit "vs expected" phrase exists.
        if source not in {"bls", "bls_latest", "bls_ppi", "bls_empsit"}:
            return 0

        score = 0.0
        cpi_mom = self._extract_percent_after(
            text,
            patterns=[
                r"consumer price index(?: for all urban consumers)?(?:[^0-9]{0,40})?(?:rose|increased|:)\s*\+?([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
                r"cpi:\s*\+?([0-9]+(?:\.[0-9]+)?)%",
            ],
        )
        if cpi_mom is not None:
            if cpi_mom <= self.macro_cpi_bullish_max:
                score += 1.0
            elif cpi_mom >= self.macro_cpi_bearish_min:
                score -= 1.0

        core_cpi_mom = self._extract_percent_after(
            text,
            patterns=[
                r"all items less food and energy(?:[^0-9]{0,50})?(?:rose|increased|:)\s*\+?([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
                r"core cpi(?:[^0-9]{0,30})?(?:rose|increased|:)\s*\+?([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
            ],
        )
        if core_cpi_mom is not None:
            if core_cpi_mom <= self.macro_core_cpi_bullish_max:
                score += 0.5
            elif core_cpi_mom >= self.macro_core_cpi_bearish_min:
                score -= 0.5

        ppi_mom = self._extract_percent_after(
            text,
            patterns=[
                r"producer price index(?:[^0-9]{0,40})?(?:rose|increased|advances|advanced|:)\s*\+?([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
                r"ppi(?:[^0-9]{0,30})?(?:rose|increased|advances|advanced|:)\s*\+?([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
            ],
        )
        if ppi_mom is not None:
            if ppi_mom <= self.macro_ppi_bullish_max:
                score += 0.5
            elif ppi_mom >= self.macro_ppi_bearish_min:
                score -= 0.5

        if abs(score) < self.macro_numeric_signal_min_abs:
            return 0
        return 1 if score > 0 else -1

    @staticmethod
    def _extract_percent_after(text: str, patterns: list[str]) -> float | None:
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _is_altcoin_operational_issue(text: str) -> bool:
        has_btc_marker = "bitcoin" in text or " btc" in text or "(btc)" in text
        if has_btc_marker:
            return False
        has_ticker = bool(re.search(r"\([a-z0-9]{2,10}\)", text))
        has_ops_delay = any(
            term in text
            for term in (
                "funding delays",
                "delayed sends",
                "delayed receives",
                "gateway",
            )
        )
        return has_ticker and has_ops_delay
