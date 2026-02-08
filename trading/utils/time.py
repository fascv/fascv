from __future__ import annotations

from datetime import datetime, timezone


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_ts(value: str) -> datetime:
    # Accept ISO 8601 or unix seconds
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        ts = float(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception as exc:
        raise ValueError(f"Unsupported timestamp: {value}") from exc
