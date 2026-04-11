from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from btc_news_arrow.models import ArrowResult, NewsItem
from btc_news_arrow.utils import ensure_utc, normalize_text, parse_datetime, utcnow


@dataclass(slots=True)
class LLMArrowPayload:
    result: ArrowResult
    reasons: list[dict[str, object]]
    confidence: float
    notes: str
    events: list[dict[str, object]]
    meta: dict[str, object]


class LLMRater:
    def __init__(self, config: dict[str, Any]) -> None:
        llm_cfg = config.get("llm", {})
        self.enabled = bool(llm_cfg.get("enabled", False))
        self.require_runtime = bool(llm_cfg.get("require_runtime", False))
        self.model = str(llm_cfg.get("model", "gpt-5-mini"))
        self.fallback_model = str(llm_cfg.get("fallback_model", "")).strip()
        self.max_items = max(1, int(llm_cfg.get("max_items", 20)))
        self.max_events = max(1, int(llm_cfg.get("max_events", 7)))
        self.snippet_chars = max(120, int(llm_cfg.get("snippet_chars", 500)))
        self.timeout_seconds = float(llm_cfg.get("timeout_seconds", 30.0))
        self.request_retries = max(0, int(llm_cfg.get("request_retries", 1)))
        self.max_output_tokens = max(64, int(llm_cfg.get("max_output_tokens", 256)))
        self.system_prompt = str(
            llm_cfg.get(
                "system_prompt",
                (
                    "You rate news impact for short-term BTC trading. "
                    "Cluster duplicates, ignore price-only commentary without new facts, "
                    "focus on genuinely new developments. "
                    "Return strict JSON with keys: arrow, score, confidence, events, notes."
                ),
            )
        )

    def availability_error(self) -> str | None:
        if not self.enabled:
            return "LLM mode is disabled in config (set llm.enabled: true)"

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return "OPENAI_API_KEY is not set"
        if api_key == "DEIN_NEUER_OPENAI_KEY":
            return "OPENAI_API_KEY still contains placeholder value"

        try:
            import openai  # noqa: F401
        except ImportError:
            return "openai package missing: install with `pip install openai`"
        return None

    def ensure_available(self) -> None:
        err = self.availability_error()
        if err:
            raise RuntimeError(err)

    def ping(self) -> dict[str, object]:
        self.ensure_available()
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=self.timeout_seconds)
        response = self._request_with_model_fallback(
            client=client,
            input_messages=[{"role": "user", "content": "Reply with: PONG"}],
        )

        text = _extract_response_text(response).strip()
        response_id = getattr(response, "id", None)
        used_model = getattr(response, "model", None) or self.model
        return {
            "ok": True,
            "model": str(used_model),
            "response_id": str(response_id) if response_id else None,
            "preview": text[:120],
        }

    def rate_window(
        self,
        items: list[NewsItem],
        window_minutes: int,
        now: datetime | None = None,
    ) -> LLMArrowPayload:
        self.ensure_available()

        now_utc = ensure_utc(now or utcnow())
        prepared = self._prepare_items(items, window_minutes=window_minutes, now=now_utc)
        if not prepared:
            result = ArrowResult(
                window_minutes=window_minutes,
                score=0.0,
                arrow="▬",
                contributing_items=0,
            )
            return LLMArrowPayload(
                result=result,
                reasons=[],
                confidence=0.0,
                notes="No items in the selected time window.",
                events=[],
                meta={
                    "used_model": None,
                    "attempts": 0,
                    "used_items": 0,
                    "degraded_profile": False,
                },
            )

        response_payload, meta = self._call_model(prepared, window_minutes=window_minutes)
        arrow_label, signed_score = _normalize_arrow_and_score(
            response_payload.get("arrow"),
            response_payload.get("score"),
        )
        confidence = _clamp_float(response_payload.get("confidence"), default=0.0, lo=0.0, hi=1.0)
        notes = str(response_payload.get("notes") or "").strip()
        events = _normalize_events(response_payload.get("events"), max_events=self.max_events)

        result = ArrowResult(
            window_minutes=window_minutes,
            score=signed_score,
            arrow=_arrow_label_to_glyph(arrow_label, abs(signed_score)),
            contributing_items=len(prepared),
        )
        reasons = _events_to_reasons(events, prepared_items=prepared)
        return LLMArrowPayload(
            result=result,
            reasons=reasons,
            confidence=confidence,
            notes=notes,
            events=events,
            meta=meta,
        )

    def _prepare_items(
        self,
        items: list[NewsItem],
        window_minutes: int,
        now: datetime,
    ) -> list[dict[str, str]]:
        window_start = now - timedelta(minutes=window_minutes)
        filtered = [
            i
            for i in items
            if window_start <= ensure_utc(i.timestamp_utc) <= now
        ]
        filtered.sort(key=lambda x: ensure_utc(x.timestamp_utc), reverse=True)

        # Cheap second-pass dedupe: keep first item per normalized title hash.
        seen_titles: set[str] = set()
        out: list[dict[str, str]] = []
        for idx, item in enumerate(filtered):
            title_norm = normalize_text(item.title)
            if title_norm in seen_titles:
                continue
            seen_titles.add(title_norm)

            snippet = (item.summary or "").strip()
            if len(snippet) > self.snippet_chars:
                snippet = snippet[: self.snippet_chars].rstrip() + "..."

            out.append(
                {
                    "id": str(idx + 1),
                    "title": item.title.strip(),
                    "source": item.source.strip(),
                    "published": ensure_utc(item.timestamp_utc).isoformat(),
                    "snippet": snippet,
                }
            )
            if len(out) >= self.max_items:
                break

        return out

    def _call_model(self, items: list[dict[str, str]], window_minutes: int) -> tuple[dict[str, Any], dict[str, object]]:
        self.ensure_available()
        api_key = os.getenv("OPENAI_API_KEY", "").strip()

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package missing: install with `pip install openai`") from exc

        client = OpenAI(api_key=api_key, timeout=self.timeout_seconds)
        profiles = self._attempt_profiles(items)
        last_exc: Exception | None = None
        attempts_total = 0
        for profile in profiles:
            payload = self._payload(window_minutes=window_minutes, items=profile["items"])
            input_messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
            ]
            for model_name in self._model_candidates():
                for attempt in range(self.request_retries + 1):
                    attempts_total += 1
                    try:
                        response = client.responses.create(
                            model=model_name,
                            input=input_messages,
                            max_output_tokens=self.max_output_tokens,
                        )
                        text = _extract_response_text(response)
                        parsed = parse_llm_output_json(text)
                        return parsed, {
                            "used_model": model_name,
                            "attempts": attempts_total,
                            "used_items": int(profile["used_items"]),
                            "degraded_profile": bool(profile["degraded_profile"]),
                        }
                    except Exception as exc:  # network/provider/parse issues
                        last_exc = exc
                        if attempt < self.request_retries:
                            time.sleep(0.6 * (attempt + 1))
                        else:
                            break

        if last_exc is not None:
            raise RuntimeError(_format_openai_exception(last_exc)) from last_exc
        raise RuntimeError("OpenAI request failed")

    def _payload(self, window_minutes: int, items: list[dict[str, str]]) -> dict[str, object]:
        return {
            "window_minutes": int(window_minutes),
            "items": items,
            "rules": {
                "max_events": self.max_events,
                "ignore_if": [
                    "price-only recap",
                    "no new information",
                    "obvious duplicate",
                ],
                "prefer": [
                    "official announcements",
                    "major exchanges",
                    "ETF flows",
                    "security incidents",
                    "regulatory decisions",
                ],
            },
            "output_schema_hint": {
                "arrow": "UP|DOWN|FLAT|MIXED",
                "score": "0..100",
                "confidence": "0..1",
                "events": [
                    {
                        "title": "string",
                        "category": "MACRO|ETF_FLOWS|REGULATION|EXCHANGE|SECURITY|ONCHAIN|OTHER",
                        "direction": "UP|DOWN|MIXED|FLAT",
                        "impact": "-100..100",
                        "confidence": "0..1",
                        "sources": ["name"],
                        "published": "ISO8601 timestamp if known",
                    }
                ],
                "notes": "one short sentence",
            },
        }

    def _attempt_profiles(self, items: list[dict[str, str]]) -> list[dict[str, object]]:
        # Start with configured size, then progressively shrink for robustness.
        base_items = list(items)
        out: list[dict[str, object]] = []
        candidates = [
            (len(base_items), self.snippet_chars, False),
            (min(len(base_items), 4), min(self.snippet_chars, 200), True),
            (min(len(base_items), 2), min(self.snippet_chars, 140), True),
        ]
        seen: set[tuple[int, int]] = set()
        for item_count, snippet_chars, degraded in candidates:
            key = (max(1, int(item_count)), max(80, int(snippet_chars)))
            if key in seen:
                continue
            seen.add(key)
            trimmed: list[dict[str, str]] = []
            for entry in base_items[: key[0]]:
                copied = dict(entry)
                snippet = str(copied.get("snippet", ""))
                if len(snippet) > key[1]:
                    copied["snippet"] = snippet[: key[1]].rstrip() + "..."
                trimmed.append(copied)
            out.append(
                {
                    "items": trimmed,
                    "used_items": len(trimmed),
                    "degraded_profile": degraded,
                }
            )
        return out

    def _model_candidates(self) -> list[str]:
        seen: set[str] = set()
        models: list[str] = []
        for model_name in (self.model, self.fallback_model):
            m = str(model_name or "").strip()
            if not m or m in seen:
                continue
            seen.add(m)
            models.append(m)
        return models

    def _request_with_model_fallback(self, client: Any, input_messages: list[dict[str, str]]) -> Any:
        models = self._model_candidates()

        last_exc: Exception | None = None
        for model_name in models:
            for attempt in range(self.request_retries + 1):
                try:
                    return client.responses.create(
                        model=model_name,
                        input=input_messages,
                        max_output_tokens=self.max_output_tokens,
                    )
                except Exception as exc:  # network/provider errors from SDK
                    last_exc = exc
                    if attempt >= self.request_retries:
                        break
                    time.sleep(0.6 * (attempt + 1))

        if last_exc is not None:
            raise RuntimeError(_format_openai_exception(last_exc)) from last_exc
        raise RuntimeError("OpenAI request failed")


def parse_llm_output_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Fallback: take first JSON object span if the model added extra prose.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        data = json.loads(candidate)
        if isinstance(data, dict):
            return data

    raise ValueError("LLM response is not valid JSON object")


def _extract_response_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct

    output = getattr(response, "output", None)
    if isinstance(output, list):
        chunks: list[str] = []
        for entry in output:
            content = _attr_or_key(entry, "content")
            if not isinstance(content, list):
                continue
            for part in content:
                text = _attr_or_key(part, "text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text)
                    continue
                if isinstance(text, dict):
                    value = text.get("value")
                    if isinstance(value, str) and value.strip():
                        chunks.append(value)
        if chunks:
            return "\n".join(chunks)

    dump_json = None
    model_dump_json = getattr(response, "model_dump_json", None)
    if callable(model_dump_json):
        dump_json = model_dump_json()
    if isinstance(dump_json, str) and dump_json.strip():
        return dump_json

    raise RuntimeError("No text output in OpenAI response")


def _attr_or_key(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _normalize_arrow_and_score(arrow: Any, score: Any) -> tuple[str, float]:
    label = str(arrow or "FLAT").strip().upper()
    if label not in {"UP", "DOWN", "FLAT", "MIXED"}:
        label = "FLAT"

    magnitude = _clamp_float(score, default=0.0, lo=0.0, hi=100.0) / 100.0
    if label == "UP":
        return label, magnitude
    if label == "DOWN":
        return label, -magnitude
    return label, 0.0


def _arrow_label_to_glyph(arrow: str, abs_score: float) -> str:
    if arrow == "UP":
        if abs_score >= 0.67:
            return "▲▲▲"
        if abs_score >= 0.34:
            return "▲▲"
        return "▲"
    if arrow == "DOWN":
        if abs_score >= 0.67:
            return "▼▼▼"
        if abs_score >= 0.34:
            return "▼▼"
        return "▼"
    return "▬"


def _normalize_events(value: Any, max_events: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []

    out: list[dict[str, object]] = []
    for raw in value[:max_events]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        category = str(raw.get("category") or "OTHER").strip().upper()
        direction = str(raw.get("direction") or "FLAT").strip().upper()
        impact = _clamp_float(raw.get("impact"), default=0.0, lo=-100.0, hi=100.0)
        confidence = _clamp_float(raw.get("confidence"), default=0.0, lo=0.0, hi=1.0)
        sources_raw = raw.get("sources")
        sources: list[str] = []
        if isinstance(sources_raw, list):
            for s in sources_raw:
                text = str(s).strip()
                if text:
                    sources.append(text)
        published: str | None = None
        for key in ("published", "timestamp_utc", "timestamp"):
            raw_ts = raw.get(key)
            text_ts = str(raw_ts).strip() if raw_ts is not None else ""
            if not text_ts:
                continue
            parsed = parse_datetime(text_ts)
            if parsed is not None:
                published = ensure_utc(parsed).isoformat()
                break

        out.append(
            {
                "title": title,
                "category": category,
                "direction": direction,
                "impact": impact,
                "confidence": confidence,
                "sources": sources,
                "published": published,
            }
        )

    return out


def _events_to_reasons(events: list[dict[str, object]], prepared_items: list[dict[str, str]]) -> list[dict[str, object]]:
    prepared = []
    for item in prepared_items:
        title = str(item.get("title", "")).strip()
        source = str(item.get("source", "")).strip().lower()
        published = str(item.get("published", "")).strip() or None
        prepared.append(
            {
                "title_norm": normalize_text(title),
                "source": source,
                "published": published,
            }
        )

    reasons: list[dict[str, object]] = []
    for event in events:
        impact = float(event.get("impact", 0.0))
        direction = str(event.get("direction", "FLAT")).upper()
        contribution = impact / 100.0
        if direction == "DOWN" and contribution > 0:
            contribution *= -1.0
        if direction == "UP" and contribution < 0:
            contribution = abs(contribution)

        sources = event.get("sources", [])
        source_text = ", ".join(str(s) for s in sources) if isinstance(sources, list) and sources else "llm"
        timestamp = _resolve_event_timestamp(event, prepared)
        reasons.append(
            {
                "timestamp_utc": timestamp,
                "source": source_text,
                "category": str(event.get("category", "OTHER")).lower(),
                "contribution": contribution,
                "title": str(event.get("title", "")),
                "url": None,
            }
        )
    return reasons


def _resolve_event_timestamp(event: dict[str, object], prepared: list[dict[str, str]]) -> str | None:
    direct_ts = event.get("published")
    if isinstance(direct_ts, str) and direct_ts.strip():
        return direct_ts.strip()

    title_norm = normalize_text(str(event.get("title", "")).strip())
    source_tokens: list[str] = []
    raw_sources = event.get("sources")
    if isinstance(raw_sources, list):
        for entry in raw_sources:
            token = str(entry).strip().lower()
            if token:
                source_tokens.append(token)

    candidates = prepared
    if title_norm:
        exact = [p for p in prepared if p["title_norm"] == title_norm and p["published"]]
        if exact:
            candidates = exact
        else:
            fuzzy = [
                p
                for p in prepared
                if p["published"] and (title_norm in p["title_norm"] or p["title_norm"] in title_norm)
            ]
            if fuzzy:
                candidates = fuzzy

    if source_tokens:
        source_matched = [
            p
            for p in candidates
            if p["published"] and any(tok in p["source"] or p["source"] in tok for tok in source_tokens)
        ]
        if source_matched:
            candidates = source_matched

    for candidate in candidates:
        published = candidate.get("published")
        if isinstance(published, str) and published.strip():
            return published.strip()

    return None


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if parsed < lo:
        return lo
    if parsed > hi:
        return hi
    return parsed


def _format_openai_exception(exc: Exception) -> str:
    name = type(exc).__name__
    text = str(exc).strip() or name
    lower = text.lower()
    if "timed out" in lower or "timeout" in lower:
        return f"OpenAI request timed out ({name})"
    if "no text output" in lower:
        return f"OpenAI returned no output text ({name})"
    if "not valid json object" in lower:
        return f"OpenAI returned invalid JSON ({name})"
    if "rate limit" in lower or "429" in lower:
        return f"OpenAI rate limit ({name})"
    if "connection" in lower or "dns" in lower or "name or service not known" in lower:
        return f"OpenAI connection error ({name})"
    return f"OpenAI request failed ({name}): {text}"
