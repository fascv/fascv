from __future__ import annotations

import re
from typing import Any

from btc_news_arrow.models import NewsItem


class Classifier:
    def __init__(self, config: dict[str, Any]) -> None:
        rules = config.get("keyword_rules", {})
        self.category_keywords_strong = self._normalize_keyword_map(rules.get("categories", {}))
        self.category_keywords_contextual = self._normalize_keyword_map(rules.get("categories_contextual", {}))
        self.category_strong_weight = float(rules.get("category_strong_weight", 2.0))
        self.category_contextual_weight = float(rules.get("category_contextual_weight", 1.0))
        polarity = rules.get("polarity", {})
        self.positive_terms = self._normalize_term_list(polarity.get("positive", []))
        self.negative_terms = self._normalize_term_list(polarity.get("negative", []))
        self.category_bias: dict[str, int] = {
            str(k): int(v) for k, v in rules.get("category_bias", {}).items()
        }
        self.negation_terms = self._normalize_term_list(
            rules.get("negation_terms", ["not", "no", "never", "without", "denied", "false"])
        )
        self.uncertainty_terms = self._normalize_term_list(
            rules.get(
                "uncertainty_terms",
                ["rumor", "rumour", "unconfirmed", "speculation", "reportedly", "alleged", "might", "could"],
            )
        )
        self.regulation_preliminary_terms = self._normalize_term_list(
            rules.get(
                "regulation_preliminary_terms",
                ["proposal", "proposed", "draft", "consultation", "comment period"],
            )
        )
        self.regulation_final_terms = self._normalize_term_list(
            rules.get(
                "regulation_final_terms",
                ["approved", "approval granted", "order issued", "rejected", "denied", "ban", "lawsuit", "charges"],
            )
        )
        self.exchange_deescalation_terms = self._normalize_term_list(
            rules.get(
                "exchange_deescalation_terms",
                ["resolved", "monitoring", "restored", "mitigated", "contained"],
            )
        )
        self.exchange_escalation_terms = self._normalize_term_list(
            rules.get(
                "exchange_escalation_terms",
                ["investigating", "identified", "outage", "halt", "degraded", "latency", "connectivity", "hacked"],
            )
        )
        self.status_exchange_terms = [
            "maintenance",
            "degraded performance",
            "latency",
            "connectivity",
            "transactions",
            "api",
            "websocket",
            "trading",
            "withdrawal",
            "deposit",
            "investigating",
            "identified",
            "monitoring",
            "resolved",
            "outage",
            "halt",
            "delayed sends",
            "delayed receives",
            "funding delays",
        ]

    def classify(self, item: NewsItem) -> NewsItem:
        text = f"{item.title} {item.summary}".lower()

        category = self._classify_category(text)
        if category == "other" and self._looks_like_exchange_status(item.source, text):
            category = "exchange_incident"
        polarity = self._classify_polarity(text, category)

        item.category = category
        item.polarity = polarity
        return item

    def _classify_category(self, text: str) -> str:
        best = "other"
        best_score = 0.0
        all_categories = set(self.category_keywords_strong.keys()) | set(self.category_keywords_contextual.keys())
        for category in all_categories:
            strong = self.category_keywords_strong.get(category, [])
            contextual = self.category_keywords_contextual.get(category, [])
            score = (
                self.category_strong_weight * sum(1 for kw in strong if self._matches_keyword(text, kw))
                + self.category_contextual_weight * sum(1 for kw in contextual if self._matches_keyword(text, kw))
            )
            if score > best_score:
                best = category
                best_score = score
        return best

    def _classify_polarity(self, text: str, category: str) -> int:
        pos_count = 0
        neg_count = 0
        for term in self.positive_terms:
            if not self._matches_keyword(text, term):
                continue
            if self._is_negated(text, term):
                neg_count += 1
            else:
                pos_count += 1
        for term in self.negative_terms:
            if not self._matches_keyword(text, term):
                continue
            if self._is_negated(text, term):
                pos_count += 1
            else:
                neg_count += 1

        if self._contains_any(text, self.uncertainty_terms) and abs(pos_count - neg_count) <= 1:
            return 0

        if category == "regulation":
            has_preliminary = self._contains_any(text, self.regulation_preliminary_terms)
            has_final = self._contains_any(text, self.regulation_final_terms)
            if has_preliminary and not has_final and abs(pos_count - neg_count) <= 1:
                return 0

        if category == "exchange_incident":
            has_deescalation = self._contains_any(text, self.exchange_deescalation_terms)
            has_escalation = self._contains_any(text, self.exchange_escalation_terms)
            if has_deescalation and not has_escalation and abs(pos_count - neg_count) <= 1:
                return 0

        if pos_count > neg_count:
            return 1
        if neg_count > pos_count:
            return -1

        return int(self.category_bias.get(category, 0))

    def _looks_like_exchange_status(self, source: str, text: str) -> bool:
        if source not in {"kraken_status", "coinbase_status"}:
            return False
        return any(term in text for term in self.status_exchange_terms)

    def _contains_any(self, text: str, terms: list[str]) -> bool:
        return any(self._matches_keyword(text, term) for term in terms)

    def _is_negated(self, text: str, term: str) -> bool:
        escaped_term = re.escape(term)
        for neg in self.negation_terms:
            escaped_neg = re.escape(neg)
            pattern = rf"(?<![a-z0-9]){escaped_neg}(?![a-z0-9])(?:[^a-z0-9]+[a-z0-9]+){{0,3}}[^a-z0-9]+{escaped_term}(?![a-z0-9])"
            if re.search(pattern, text):
                return True
        return False

    @staticmethod
    def _matches_keyword(text: str, keyword: str) -> bool:
        kw = keyword.strip()
        if not kw:
            return False
        # Single token keywords should match as complete tokens to avoid
        # false positives like "war" in "aware" or "ban" in "bank".
        if re.match(r"^[a-z0-9]+$", kw):
            variants = Classifier._token_variants(kw)
            alternatives = "|".join(re.escape(v) for v in variants)
            return re.search(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])", text) is not None
        return kw in text

    @staticmethod
    def _token_variants(token: str) -> list[str]:
        base = token.strip().lower()
        if not base:
            return []
        variants = [base]
        if len(base) >= 3:
            variants.append(f"{base}s")
            variants.append(f"{base}es")
            if base.endswith("y") and len(base) >= 4:
                variants.append(f"{base[:-1]}ies")
        dedup: list[str] = []
        seen: set[str] = set()
        for v in variants:
            if v not in seen:
                seen.add(v)
                dedup.append(v)
        return dedup

    @staticmethod
    def _normalize_term_list(raw: Any) -> list[str]:
        values = raw if isinstance(raw, list) else [raw]
        terms: list[str] = []
        for value in values:
            if value is None:
                continue
            if value is False:
                # YAML parses unquoted "no" as False.
                term = "no"
            elif value is True:
                # Keep a deterministic token for unexpected truthy bool entries.
                term = "yes"
            else:
                term = str(value).strip()
            term_l = term.lower().strip()
            if term_l:
                terms.append(term_l)
        return terms

    @classmethod
    def _normalize_keyword_map(cls, raw: Any) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[str]] = {}
        for key, value in raw.items():
            terms = cls._normalize_term_list(value)
            if terms:
                out[str(key)] = terms
        return out
