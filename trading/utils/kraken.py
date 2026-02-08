from __future__ import annotations

PAIR_MAP = {
    "BTC/EUR": "XBT/EUR",
    "BTC/USD": "XBT/USD",
}


def map_pair(pair: str) -> str:
    return PAIR_MAP.get(pair.upper(), pair)
