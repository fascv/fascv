from __future__ import annotations


_QUOTE_SUFFIXES = (
    "USDT",
    "USDC",
    "FDUSD",
    "BUSD",
    "TUSD",
    "EUR",
    "USD",
    "BTC",
    "ETH",
    "BNB",
)


def normalize_pair(pair: str) -> str:
    raw = (pair or "").strip().upper().replace("-", "/").replace("_", "/")
    if not raw:
        return raw
    if "/" in raw:
        base, quote = (part.strip() for part in raw.split("/", 1))
        return f"{base}/{quote}"
    token = raw.replace("/", "")
    for quote in _QUOTE_SUFFIXES:
        if token.endswith(quote) and len(token) > len(quote):
            base = token[: -len(quote)]
            return f"{base}/{quote}"
    return raw


def to_binance_symbol(pair: str) -> str:
    norm = normalize_pair(pair)
    if "/" not in norm:
        return norm
    base, quote = (part.strip() for part in norm.split("/", 1))
    return f"{base}{quote}"


def split_symbol(symbol: str) -> tuple[str, str]:
    norm = normalize_pair(symbol)
    if "/" in norm:
        base, quote = (part.strip() for part in norm.split("/", 1))
        return base, quote
    token = (symbol or "").strip().upper().replace("/", "").replace("-", "")
    for quote in _QUOTE_SUFFIXES:
        if token.endswith(quote) and len(token) > len(quote):
            return token[: -len(quote)], quote
    return token, "USDT"
