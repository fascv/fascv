from __future__ import annotations

from datetime import datetime
import re
import time
from typing import Any
from urllib.parse import urlparse

from btc_news_arrow.classifier import Classifier
from btc_news_arrow.models import NewsItem
from btc_news_arrow.scorer import Scorer
from btc_news_arrow.storage import Storage
from btc_news_arrow.utils import canonicalize_url, ensure_utc, hash_text, normalize_text, parse_datetime, strip_html, utcnow


class Collector:
    def __init__(
        self,
        config: dict[str, Any],
        storage: Storage,
        classifier: Classifier,
        scorer: Scorer,
    ) -> None:
        self.config = config
        self.storage = storage
        self.classifier = classifier
        self.scorer = scorer
        dedupe_cfg = config.get("dedupe", {})
        self.fuzzy_threshold = float(dedupe_cfg.get("fuzzy_threshold", 0.92))
        self.lookback_hours = int(dedupe_cfg.get("lookback_hours", 24))
        ingestion_cfg = config.get("ingestion", {})
        self.max_future_skew_seconds = int(ingestion_cfg.get("max_future_skew_seconds", 120))
        http_cfg = config.get("http", {})
        user_agent = str(
            http_cfg.get("user_agent", "btc-news-arrow/1.0 (contact: you@example.com)")
        ).strip()
        if not user_agent:
            user_agent = "btc-news-arrow/1.0 (contact: you@example.com)"
        self.request_headers = {
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        }
        gdelt_cfg = config.get("gdelt", {})
        self.gdelt_max_per_domain_per_run = max(1, int(gdelt_cfg.get("max_per_domain_per_run", 2)))
        self.gdelt_domain_allowlist = {
            self._normalize_domain(str(v))
            for v in gdelt_cfg.get("domain_allowlist", [])
            if self._normalize_domain(str(v))
        }
        self.gdelt_domain_blocklist = {
            self._normalize_domain(str(v))
            for v in gdelt_cfg.get("domain_blocklist", [])
            if self._normalize_domain(str(v))
        }
        self.gdelt_drop_duplicate_titles = bool(gdelt_cfg.get("drop_duplicate_titles", True))
        self.gdelt_require_relevance = bool(gdelt_cfg.get("require_relevance", True))
        self.gdelt_min_relevance_matches = max(1, int(gdelt_cfg.get("min_relevance_matches", 1)))
        self.gdelt_relevance_terms = self._normalize_terms(
            gdelt_cfg.get(
                "relevance_terms",
                [
                    "bitcoin",
                    "btc",
                    "crypto",
                    "cryptocurrency",
                    "digital asset",
                    "digital assets",
                    "stablecoin",
                    "etf",
                    "blockchain",
                    "token",
                    "binance",
                    "coinbase",
                    "kraken",
                ],
            )
        )
        aggregation_cfg = config.get("aggregation", {})
        self.event_cluster_terms_limit = max(
            3, int(aggregation_cfg.get("event_cluster_terms_limit", 8))
        )
        self.event_cluster_time_bucket_minutes = max(
            5, int(aggregation_cfg.get("event_cluster_time_bucket_minutes", 60))
        )

    def collect_once(
        self,
        include_gdelt: bool = True,
        now: datetime | None = None,
    ) -> tuple[list[NewsItem], int, dict[str, Any]]:
        run_now = ensure_utc(now or utcnow())

        stats: dict[str, Any] = {
            "totals": {
                "raw_items": 0,
                "processed": 0,
                "inserted": 0,
                "dropped_future": 0,
                "dropped_batch_duplicate": 0,
                "dropped_existing_id": 0,
                "dropped_similar_title": 0,
                "errors": 0,
            },
            "sources": {},
        }

        raw_items: list[NewsItem] = []
        for feed_cfg in self.config.get("feeds", []):
            source_name = str(feed_cfg.get("name", "unknown"))
            source_stats = self._source_stats(stats, source_name)
            fetched, err = self._fetch_feed(feed_cfg, default_ts=run_now)
            source_stats["fetched"] += len(fetched)
            stats["totals"]["raw_items"] += len(fetched)
            if err:
                source_stats["errors"] += 1
                stats["totals"]["errors"] += 1
            raw_items.extend(fetched)

        if include_gdelt and self.config.get("gdelt", {}).get("enabled", False):
            source_stats = self._source_stats(stats, "gdelt")
            fetched, err = self._fetch_gdelt(default_ts=run_now)
            source_stats["fetched"] += len(fetched)
            stats["totals"]["raw_items"] += len(fetched)
            if err:
                source_stats["errors"] += 1
                stats["totals"]["errors"] += 1
            raw_items.extend(fetched)

        seen_batch: set[str] = set()
        processed: list[NewsItem] = []

        for item in raw_items:
            source_stats = self._source_stats(stats, item.source)
            item_ts = ensure_utc(item.timestamp_utc)
            future_seconds = (item_ts - run_now).total_seconds()
            if future_seconds > self.max_future_skew_seconds:
                source_stats["dropped_future"] += 1
                stats["totals"]["dropped_future"] += 1
                continue
            if future_seconds > 0:
                item.timestamp_utc = run_now

            dedupe_key = self._dedupe_key(item)
            if dedupe_key in seen_batch:
                source_stats["dropped_batch_duplicate"] += 1
                stats["totals"]["dropped_batch_duplicate"] += 1
                continue
            seen_batch.add(dedupe_key)

            if self.storage.exists_guid_or_url(item.guid, item.url):
                source_stats["dropped_existing_id"] += 1
                stats["totals"]["dropped_existing_id"] += 1
                continue
            if self.storage.has_similar_title(item.title, self.lookback_hours, self.fuzzy_threshold):
                source_stats["dropped_similar_title"] += 1
                stats["totals"]["dropped_similar_title"] += 1
                continue

            enriched = self.classifier.classify(item)
            scored = self.scorer.score(enriched, now=run_now)
            cluster_id = self._event_cluster_id(scored)
            scored.raw = {
                **scored.raw,
                "event_cluster_id": cluster_id,
                "classification": {
                    "category": scored.category,
                    "polarity": scored.polarity,
                    "impact": scored.impact,
                },
            }
            processed.append(scored)
            source_stats["processed"] += 1

        inserted = self.storage.insert_items(processed)
        stats["totals"]["processed"] = len(processed)
        stats["totals"]["inserted"] = inserted
        return processed, inserted, stats

    @staticmethod
    def _source_stats(stats: dict[str, Any], source_name: str) -> dict[str, int]:
        sources = stats.setdefault("sources", {})
        if source_name not in sources:
            sources[source_name] = {
                "fetched": 0,
                "processed": 0,
                "dropped_future": 0,
                "dropped_batch_duplicate": 0,
                "dropped_existing_id": 0,
                "dropped_similar_title": 0,
                "errors": 0,
            }
        return sources[source_name]

    @staticmethod
    def _dedupe_key(item: NewsItem) -> str:
        if item.guid:
            return f"guid:{item.guid}"
        canonical_url = canonicalize_url(item.url)
        if canonical_url:
            return f"url:{canonical_url}"
        return f"title:{hash_text(normalize_text(item.title))}"

    def _fetch_feed(
        self,
        feed_cfg: dict[str, Any],
        default_ts: datetime,
    ) -> tuple[list[NewsItem], str | None]:
        try:
            import feedparser
            import requests
        except ImportError:
            return [], "missing_dependencies"

        name = str(feed_cfg.get("name", "unknown"))
        url = str(feed_cfg.get("url", ""))
        if not url:
            return [], "missing_feed_url"

        try:
            resp = requests.get(url, timeout=20, headers=self.request_headers)
            resp.raise_for_status()
        except requests.RequestException as exc:
            return [], str(exc)

        parsed = feedparser.parse(resp.content)
        entries = getattr(parsed, "entries", [])
        out: list[NewsItem] = []
        for entry in entries:
            title = str(entry.get("title", "")).strip()
            if not title:
                continue

            summary = strip_html(str(entry.get("summary", "") or entry.get("description", "")))
            link = canonicalize_url(str(entry.get("link", "")).strip())
            guid = str(entry.get("id", "") or entry.get("guid", "")).strip() or link

            ts_raw = (
                entry.get("published")
                or entry.get("updated")
                or entry.get("pubDate")
                or entry.get("created")
            )
            ts = parse_datetime(str(ts_raw) if ts_raw else None) or default_ts

            out.append(
                NewsItem(
                    timestamp_utc=ts,
                    source=name,
                    title=title,
                    summary=summary,
                    url=link,
                    guid=guid,
                    raw={"source_type": "feed", "feed_url": url},
                )
            )

        return out, None

    def _event_cluster_id(self, item: NewsItem) -> str:
        # Build a stable story signature from normalized title+summary terms plus time bucket.
        text = normalize_text(f"{item.title} {item.summary}")
        tokens = [tok for tok in text.split() if len(tok) >= 4 and not tok.isdigit()]
        if not tokens:
            tokens = [tok for tok in normalize_text(item.title).split() if tok]
        if not tokens:
            tokens = ["unknown"]

        top = sorted(set(tokens))[: self.event_cluster_terms_limit]
        signature = "|".join(top)
        bucket_seconds = self.event_cluster_time_bucket_minutes * 60
        ts = int(ensure_utc(item.timestamp_utc).timestamp())
        bucket = ts // bucket_seconds
        return f"{hash_text(signature)[:16]}:{bucket}"

    def _fetch_gdelt(self, default_ts: datetime) -> tuple[list[NewsItem], str | None]:
        try:
            import requests
        except ImportError:
            return [], "missing_requests_dependency"

        gdelt_cfg = self.config.get("gdelt", {})
        endpoint = str(gdelt_cfg.get("endpoint", "")).strip()
        if not endpoint:
            return [], "missing_gdelt_endpoint"

        raw_query = str(gdelt_cfg.get("query", "bitcoin OR btc OR cryptocurrency"))
        raw_timespan = str(gdelt_cfg.get("timespan", "30min"))
        params = {
            "query": self._normalize_gdelt_query(raw_query),
            "mode": "artlist",
            "format": "json",
            "timespan": self._normalize_gdelt_timespan(raw_timespan),
            "maxrecords": int(gdelt_cfg.get("maxrecords", 50)),
            "sort": "datedesc",
        }
        retry_seconds = max(1, int(gdelt_cfg.get("retry_seconds", 6)))
        max_retries = max(0, int(gdelt_cfg.get("max_retries", 1)))
        last_err: str | None = None

        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(endpoint, params=params, timeout=20, headers=self.request_headers)
            except requests.RequestException as exc:
                last_err = str(exc)
                if attempt < max_retries:
                    time.sleep(retry_seconds)
                    continue
                return [], last_err

            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 429:
                last_err = "gdelt_rate_limited_http_429"
                if attempt < max_retries:
                    time.sleep(retry_seconds)
                    continue
                return [], last_err
            if status >= 400:
                return [], f"gdelt_http_{status}"

            try:
                data = resp.json()
                if not isinstance(data, dict):
                    return [], "gdelt_invalid_json_payload"
            except ValueError:
                body_head = str(getattr(resp, "text", "")).strip().replace("\n", " ")[:180]
                return [], f"gdelt_invalid_json: {body_head}"
            break
        else:
            return [], last_err or "gdelt_fetch_failed"

        articles = data.get("articles", [])
        out: list[NewsItem] = []
        domain_counts: dict[str, int] = {}
        seen_title_norms: set[str] = set()
        for article in articles:
            title = str(article.get("title", "")).strip()
            if not title:
                continue

            link = canonicalize_url(str(article.get("url", "")).strip())
            domain_raw = urlparse(link).netloc if link else "unknown"
            domain = self._normalize_domain(domain_raw) or "unknown"
            if not self._gdelt_domain_allowed(domain):
                continue
            if domain_counts.get(domain, 0) >= self.gdelt_max_per_domain_per_run:
                continue
            title_norm = normalize_text(title)
            if self.gdelt_drop_duplicate_titles and title_norm and title_norm in seen_title_norms:
                continue
            if self.gdelt_require_relevance and not self._gdelt_title_relevant(title_norm):
                continue
            if title_norm:
                seen_title_norms.add(title_norm)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            source = f"gdelt:{domain}"
            seen_date = article.get("seendate") or article.get("date")
            ts = parse_datetime(str(seen_date) if seen_date else None) or default_ts

            out.append(
                NewsItem(
                    timestamp_utc=ts,
                    source=source,
                    title=title,
                    summary=str(article.get("source", "")),
                    url=link,
                    guid=link,
                    raw={"source_type": "gdelt", "domain": domain},
                )
            )

        return out, None

    @staticmethod
    def _normalize_gdelt_query(query: str) -> str:
        q = str(query or "").strip()
        if not q:
            return "(bitcoin OR btc OR cryptocurrency)"
        has_or = " OR " in q.upper()
        if has_or and not (q.startswith("(") and q.endswith(")")):
            return f"({q})"
        return q

    @staticmethod
    def _normalize_gdelt_timespan(timespan: str) -> str:
        raw = str(timespan or "").strip().lower()
        if not raw:
            return "1h"
        # GDELT rejects too-short windows; enforce >= 1h.
        min_match = re.fullmatch(r"(\d+)\s*(m|min|mins|minute|minutes)", raw)
        if min_match:
            minutes = int(min_match.group(1))
            if minutes < 60:
                return "1h"
            if minutes % 60 == 0:
                return f"{minutes // 60}h"
            return f"{minutes}min"
        return raw

    @staticmethod
    def _normalize_domain(value: str) -> str:
        d = str(value or "").strip().lower()
        if d.startswith("www."):
            d = d[4:]
        return d

    def _gdelt_domain_allowed(self, domain: str) -> bool:
        d = self._normalize_domain(domain)
        if not d:
            return False
        if d in self.gdelt_domain_blocklist:
            return False
        if self.gdelt_domain_allowlist and d not in self.gdelt_domain_allowlist:
            return False
        return True

    def _gdelt_title_relevant(self, title_norm: str) -> bool:
        if not self.gdelt_relevance_terms:
            return True
        matches = 0
        for term in self.gdelt_relevance_terms:
            if self._contains_term(title_norm, term):
                matches += 1
                if matches >= self.gdelt_min_relevance_matches:
                    return True
        return False

    @staticmethod
    def _contains_term(text: str, term: str) -> bool:
        t = str(term).strip().lower()
        if not t:
            return False
        if re.fullmatch(r"[a-z0-9]+", t):
            return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text) is not None
        return t in text

    @staticmethod
    def _normalize_terms(raw: Any) -> list[str]:
        values = raw if isinstance(raw, list) else [raw]
        out: list[str] = []
        for value in values:
            token = str(value or "").strip().lower()
            if token:
                out.append(token)
        return out
