from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_DURATION_RE = re.compile(r"^(?P<value>\d+)(?P<unit>[smhdw])$")
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "utm_campaign",
    "utm_content",
    "utm_id",
    "utm_medium",
    "utm_name",
    "utm_source",
    "utm_term",
}


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    v = value.strip()
    if not v:
        return None

    try:
        # Support trailing Z without dateutil.
        if v.endswith("Z"):
            return ensure_utc(datetime.fromisoformat(v[:-1] + "+00:00"))
        return ensure_utc(datetime.fromisoformat(v))
    except ValueError:
        pass

    try:
        return ensure_utc(parsedate_to_datetime(v))
    except (TypeError, ValueError):
        pass

    compact_formats = ["%Y%m%d%H%M%S", "%Y%m%dT%H%M%SZ"]
    for fmt in compact_formats:
        try:
            parsed = datetime.strptime(v, fmt)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue

    return None


def normalize_text(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.lower()).strip()
    cleaned = re.sub(r"[^a-z0-9\s]", "", cleaned)
    return cleaned


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(f"Invalid duration format: {value}")

    amount = int(match.group("value"))
    unit = match.group("unit")
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return timedelta(days=amount)


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()


def canonicalize_url(value: str | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw

    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return raw

    port = parsed.port
    netloc = host
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_items = [
        (k, v)
        for k, v in query_items
        if k.lower() not in _TRACKING_QUERY_KEYS and not k.lower().startswith("utm_")
    ]
    query = urlencode(sorted(filtered_items), doseq=True)

    return urlunparse((scheme, netloc, path, "", query, ""))
