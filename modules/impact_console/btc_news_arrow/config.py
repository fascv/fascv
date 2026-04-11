from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("config.yaml")


DEFAULT_CONFIG: dict[str, Any] = {
    "feeds": [],
    "gdelt": {
        "enabled": False,
        "endpoint": "https://api.gdeltproject.org/api/v2/doc/doc",
        "query": "(bitcoin OR btc OR cryptocurrency)",
        "timespan": "1h",
        "maxrecords": 50,
        "max_retries": 1,
        "retry_seconds": 6,
        "max_per_domain_per_run": 2,
        "domain_allowlist": [],
        "domain_blocklist": [],
        "drop_duplicate_titles": True,
    },
    "dedupe": {
        "fuzzy_threshold": 0.92,
        "lookback_hours": 24,
    },
    "aggregation": {
        "source_repeat_half_life_items": 3.0,
    },
    "ingestion": {
        "max_future_skew_seconds": 120,
    },
    "http": {
        "user_agent": "btc-news-arrow/1.0 (contact: you@example.com)",
    },
    "source_weights": {},
    "category_weights": {},
    "trigger_multipliers": {},
    "heuristics": {
        "context_direction_weight": 0.7,
        "direction_deadzone": 0.15,
        "macro_surprise_multiplier": 1.3,
        "macro_in_line_multiplier": 0.75,
        "regulation_final_multiplier": 1.2,
        "regulation_preliminary_multiplier": 0.7,
        "regulation_non_crypto_multiplier": 0.0,
        "macro_cpi_bullish_max": 0.2,
        "macro_cpi_bearish_min": 0.4,
        "macro_core_cpi_bullish_max": 0.3,
        "macro_core_cpi_bearish_min": 0.4,
        "macro_ppi_bullish_max": 0.2,
        "macro_ppi_bearish_min": 0.4,
        "macro_numeric_signal_min_abs": 0.4,
        "exchange_state_multipliers": {
            "investigating": 1.2,
            "identified": 1.1,
            "monitoring": 0.85,
            "resolved": 0.45,
        },
        "macro_positive_surprise_terms": [
            "lower than expected",
            "below expectations",
            "cooler than expected",
            "inflation eased",
            "disinflation",
            "rate cut",
        ],
        "macro_negative_surprise_terms": [
            "higher than expected",
            "above expectations",
            "hotter than expected",
            "sticky inflation",
            "inflation accelerated",
            "rate hike",
        ],
        "macro_in_line_terms": [
            "as expected",
            "in line with expectations",
            "in-line with expectations",
            "unchanged as expected",
        ],
        "regulation_final_terms": [
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
        "regulation_preliminary_terms": [
            "proposal",
            "proposed",
            "draft",
            "consultation",
            "comment period",
            "interview",
            "speech",
            "remarks",
        ],
    },
    "keyword_rules": {
        "categories": {},
        "polarity": {
            "positive": [],
            "negative": [],
        },
        "category_bias": {},
    },
    "decay": {
        "half_life_minutes": 720,
    },
    "thresholds": {
        "60m": 0.25,
        "1440m": 0.45,
        "10080m": 0.7,
        "default": 0.25,
    },
    "learning": {
        "horizons": ["5m", "15m", "60m", "24h", "1w"],
        "market": {
            "endpoint": "https://api.binance.com",
            "symbol": "BTCUSDT",
        },
        "model": {
            "confidence_k": 10.0,
            "max_effective_n": 200.0,
            "feature_recency_half_life_days": 7.0,
            "label_return_clip_abs": 0.25,
            "window_horizon_mix": {
                "60m": {
                    "5m": 0.35,
                    "15m": 0.35,
                    "60m": 0.30,
                },
                "1440m": {
                    "15m": 0.15,
                    "60m": 0.35,
                    "24h": 0.50,
                },
                "10080m": {
                    "60m": 0.15,
                    "24h": 0.45,
                    "1w": 0.40,
                },
                "default": {
                    "60m": 1.0,
                },
            },
        },
        "arrow": {
            "scale": 100.0,
            "thresholds": {
                "60m": 0.35,
                "1440m": 0.8,
                "10080m": 1.2,
                "default": 0.35,
            },
        },
    },
    "llm": {
        "enabled": False,
        "require_runtime": True,
        "allow_degraded_start": False,
        "model": "gpt-4.1-mini",
        "fallback_model": "gpt-5-mini",
        "max_items": 6,
        "max_events": 5,
        "snippet_chars": 280,
        "timeout_seconds": 120,
        "request_retries": 1,
        "max_output_tokens": 900,
        "request_cooldown_seconds": 600,
        "cache_ttl_seconds": 7200,
        "new_item_min_abs_impact": 1e-6,
        "min_window_minutes": 60,
        "short_window_mode": "rule",
        "fallback_to_rule_on_error": False,
    },
    "hybrid": {
        "rule_weight": 0.5,
        "llm_weight": 0.35,
        "learn_weight": 0.15,
        "optimizer": {
            "holdout_ratio": 0.2,
            "min_holdout_samples": 50,
            "require_holdout_improvement": True,
            "min_holdout_improvement": 0.0,
            "min_train_improvement": 0.0,
            "max_share_shift": 0.5,
        },
        "no_signal_min_items": 3,
        "no_signal_min_relevance_sum": 1.2,
        "tier1_exchange_sources": ["coinbase_status", "kraken_status"],
        "tier1_exchange_relevance": 0.7,
        "keyword_strong_relevance": 1.0,
        "keywords_strong": [
            "btc",
            "bitcoin",
            "crypto",
            "etf",
            "stablecoin",
            "exchange",
            "derivatives",
            "spot",
            "custody",
        ],
        "source_default_relevance": {
            "coinbase_status": 0.8,
            "kraken_status": 0.8,
            "fed": 0.2,
            "ecb": 0.2,
            "cftc": 0.4,
            "sec": 0.4,
            "sec_litigation": 0.4,
            "bls": 0.4,
            "eurostat": 0.3,
            "fed_speeches": 0.25,
            "gdelt": 0.5,
        },
        "score_clip_abs": {
            "rule": 2.5,
            "llm": 1.5,
            "learn": 2.0,
        },
    },
    "attribution": {
        "enabled": True,
        "llm_usage": "on",
        "event_detection": {
            "horizons": ["15m", "60m"],
            "zscore_threshold": 2.0,
            "min_abs_return": 0.004,
            "cooldown_minutes": 45,
            "max_events_per_day": 24,
            "lookback_hours": 48,
            "zscore_lookback_points": 96,
        },
        "candidate_window": {
            "lookback_minutes": 180,
            "lookahead_minutes": 15,
            "max_candidates": 50,
        },
        "candidate_filters": {
            "min_abs_impact": 0.05,
            "min_abs_learn_score": 0.03,
            "cluster_decay": 0.6,
            "top_k": 5,
        },
        "scoring_weights": {
            "time": 0.30,
            "direction": 0.20,
            "rule": 0.20,
            "learn": 0.20,
            "llm": 0.10,
        },
        "score_normalization": {
            "time_decay_minutes": 90.0,
            "rule_norm_abs": 0.6,
            "learn_norm_abs": 0.02,
            "llm_norm_abs": 0.6,
            "source_factor_min": 0.5,
            "source_factor_max": 1.5,
        },
        "thresholds": {
            "news_driven_probability": 0.65,
            "news_driven_top_score": 0.45,
            "mixed_probability": 0.40,
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    merged = deepcopy(DEFAULT_CONFIG)
    if not config_path.exists():
        return merged

    with config_path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a mapping")

    return _deep_merge(merged, loaded)
