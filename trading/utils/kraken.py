from __future__ import annotations


def map_pair(pair: str) -> str:
    """
    Normalize a trading pair for Kraken WS v2 subscriptions.

    Kraken WS v2 uses ISO-style symbols like "BTC/EUR" (not "XBT/EUR").
    We still accept "XBT/*" inputs and normalize them to "BTC/*".
    """
    raw = (pair or "").strip().upper()
    if not raw:
        return raw
    raw = raw.replace("-", "/")
    if "/" not in raw:
        return raw
    base, quote = (part.strip() for part in raw.split("/", 1))
    if base == "XBT":
        base = "BTC"
    if quote == "XBT":
        quote = "BTC"
    return f"{base}/{quote}"


ASSET_MAP_REST = {
    # Common naming differences vs Kraken.
    "BTC": "XBT",
}


def to_kraken_rest_pair(pair: str) -> str:
    """
    Convert a human/WS style pair like "BTC/EUR" (or "XBT/EUR") into the REST pair altname like "XBTEUR".
    Kraken REST private endpoints (e.g. AddOrder) accept the altname without a separator.
    """
    raw = (pair or "").strip().upper()
    if not raw:
        return raw
    raw = raw.replace("-", "/")
    if "/" not in raw:
        return raw
    base, quote = (part.strip() for part in raw.split("/", 1))
    base = ASSET_MAP_REST.get(base, base)
    quote = ASSET_MAP_REST.get(quote, quote)
    return f"{base}{quote}"
