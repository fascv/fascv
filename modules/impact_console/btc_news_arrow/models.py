from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class NewsItem:
    timestamp_utc: datetime
    source: str
    title: str
    summary: str = ""
    url: str | None = None
    guid: str | None = None
    category: str = "other"
    polarity: int = 0
    impact: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ArrowResult:
    window_minutes: int
    score: float
    arrow: str
    contributing_items: int
