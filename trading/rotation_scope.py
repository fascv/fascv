from __future__ import annotations

import re
from typing import Any, Iterable

from trading.rotation_universe import build_lanes


LANE_SYMBOLS: tuple[str, ...] = tuple(build_lanes().keys())
LANE_SYMBOL_SET: frozenset[str] = frozenset(LANE_SYMBOLS)
ROTATION_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,24}$")


def rotation_symbol_list(
    raw: Any,
    *,
    allowed_symbols: Iterable[str] | None = None,
) -> list[str]:
    allowed = (
        frozenset(str(item).strip().upper() for item in allowed_symbols)
        if allowed_symbols is not None
        else None
    )
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").split(",")

    values: list[str] = []
    for item in items:
        symbol = str(item or "").strip().upper()
        if not symbol or not ROTATION_SYMBOL_RE.fullmatch(symbol):
            continue
        if allowed is not None and symbol not in allowed:
            continue
        if symbol not in values:
            values.append(symbol)
    return values


def rotation_selected_symbols(payload: dict[str, Any]) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return rotation_symbol_list(payload.get("selected", []))


def rotation_watch_symbols(
    payload: dict[str, Any],
    *,
    include_selected: bool = True,
) -> list[str]:
    if not isinstance(payload, dict):
        return []
    values = rotation_symbol_list(payload.get("watch_symbols", []))
    if include_selected:
        for symbol in rotation_selected_symbols(payload):
            if symbol not in values:
                values.append(symbol)
    return values


def rotation_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("all_rows")
    if not isinstance(rows, list):
        rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    return [item for item in rows if isinstance(item, dict)]


def rotation_row_symbols(payload: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for row in rotation_rows(payload):
        symbol = str(row.get("symbol", "")).strip().upper()
        if symbol and ROTATION_SYMBOL_RE.fullmatch(symbol) and symbol not in values:
            values.append(symbol)
    return values


def rotation_scope_symbols(
    payload: dict[str, Any],
    *,
    running_symbols: Iterable[str] | None = None,
    include_watch: bool = True,
    include_selected: bool = True,
    include_rows: bool = True,
) -> list[str]:
    values: list[str] = []

    for symbol in rotation_symbol_list(list(running_symbols or [])):
        if symbol not in values:
            values.append(symbol)

    if include_watch:
        for symbol in rotation_watch_symbols(payload, include_selected=include_selected):
            if symbol not in values:
                values.append(symbol)
    elif include_selected:
        for symbol in rotation_selected_symbols(payload):
            if symbol not in values:
                values.append(symbol)

    if include_rows:
        for symbol in rotation_row_symbols(payload):
            if symbol not in values:
                values.append(symbol)

    return values
