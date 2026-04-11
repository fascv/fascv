#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


STABLE_BASES = {
    "USDT",
    "USDC",
    "BUSD",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "EUR",
    "USD",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
ESTABLISHED_BASES = {
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "LINK",
    "ADA",
    "AVAX",
    "DOT",
    "LTC",
    "BCH",
    "XLM",
    "HBAR",
    "SUI",
    "TRX",
    "ATOM",
    "APT",
    "NEAR",
    "UNI",
    "AAVE",
    "ARB",
    "OP",
    "ETC",
    "FET",
    "INJ",
    "RENDER",
    "FIL",
    "ALGO",
    "ICP",
    "MKR",
    "TON",
}
PROFILE_STAGE2_DEFAULTS: Dict[str, Dict[str, float]] = {
    "open": {
        "min_history_days": 90.0,
        "min_quote_volume_30d": 25_000_000.0,
        "max_abs_return_1d": 18.0,
        "max_corr_btc_7d": 0.75,
        "min_rel_strength_3d": 0.0,
        "min_rel_strength_7d": 0.0,
    },
    "established": {
        "min_history_days": 90.0,
        "min_quote_volume_30d": 25_000_000.0,
        "max_abs_return_1d": 25.0,
        "max_corr_btc_7d": 0.92,
        "min_rel_strength_3d": 0.0,
        "min_rel_strength_7d": 0.0,
    },
}


@dataclass
class SymbolMeta:
    symbol: str
    base_asset: str
    quote_asset: str


class BinancePublicClient:
    def __init__(self, *, base_url: str, timeout_sec: float, sleep_sec: float):
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = float(timeout_sec)
        self.sleep_sec = max(0.0, float(sleep_sec))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "codex-trader/1.0"})

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params or {}, timeout=self.timeout_sec)
        resp.raise_for_status()
        time.sleep(self.sleep_sec)
        return resp.json()


def _utc_midnight_now() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _pearson_corr(xs: List[float], ys: List[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    xs = xs[-n:]
    ys = ys[-n:]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = 0.0
    den_x = 0.0
    den_y = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        num += dx * dy
        den_x += dx * dx
        den_y += dy * dy
    if den_x <= 0.0 or den_y <= 0.0:
        return None
    return num / math.sqrt(den_x * den_y)


def _return_pct(closes: List[float], days: int) -> Optional[float]:
    need = int(days) + 1
    if len(closes) < need:
        return None
    prev = closes[-need]
    last = closes[-1]
    if prev <= 0.0:
        return None
    return (last / prev - 1.0) * 100.0


def _daily_returns(closes: List[float]) -> List[float]:
    out: List[float] = []
    for prev, cur in zip(closes[:-1], closes[1:]):
        if prev <= 0.0:
            continue
        out.append((cur / prev) - 1.0)
    return out


def _green_ratio(returns: List[float], days: int) -> Optional[float]:
    if len(returns) < days:
        return None
    tail = returns[-days:]
    if not tail:
        return None
    return sum(1.0 for r in tail if r > 0.0) / float(len(tail))


def _btc_down_divergence(sym_returns: List[float], btc_returns: List[float], days: int) -> int:
    n = min(len(sym_returns), len(btc_returns), int(days))
    if n <= 0:
        return 0
    count = 0
    for sym_r, btc_r in zip(sym_returns[-n:], btc_returns[-n:]):
        if btc_r < 0.0 and sym_r > 0.0:
            count += 1
    return count


def _is_supported_symbol(row: Dict[str, Any], quote_assets: set[str]) -> Optional[SymbolMeta]:
    symbol = str(row.get("symbol", "")).upper().strip()
    base_asset = str(row.get("baseAsset", "")).upper().strip()
    quote_asset = str(row.get("quoteAsset", "")).upper().strip()
    status = str(row.get("status", "")).upper().strip()
    permissions = row.get("permissions")
    if not symbol or not base_asset or not quote_asset:
        return None
    if status != "TRADING":
        return None
    if quote_asset not in quote_assets:
        return None
    if base_asset == "BTC":
        return None
    if base_asset in STABLE_BASES:
        return None
    if any(base_asset.endswith(sfx) for sfx in LEVERAGED_SUFFIXES):
        return None
    if isinstance(permissions, list) and permissions and "SPOT" not in permissions:
        return None
    return SymbolMeta(symbol=symbol, base_asset=base_asset, quote_asset=quote_asset)


def _fetch_exchange_symbols(client: BinancePublicClient, quote_assets: set[str]) -> List[SymbolMeta]:
    doc = client.get_json("/api/v3/exchangeInfo")
    symbols = doc.get("symbols") if isinstance(doc, dict) else None
    out: List[SymbolMeta] = []
    if not isinstance(symbols, list):
        return out
    for row in symbols:
        if not isinstance(row, dict):
            continue
        meta = _is_supported_symbol(row, quote_assets)
        if meta is not None:
            out.append(meta)
    return out


def _fetch_daily_klines(
    client: BinancePublicClient,
    *,
    symbol: str,
    days: int,
    end_exclusive: datetime,
) -> List[Dict[str, float]]:
    days = max(8, int(days))
    start_time = end_exclusive - timedelta(days=days + 2)
    params = {
        "symbol": symbol,
        "interval": "1d",
        "startTime": int(start_time.timestamp() * 1000),
        "endTime": int(end_exclusive.timestamp() * 1000) - 1,
        "limit": min(1000, days + 4),
    }
    doc = client.get_json("/api/v3/klines", params=params)
    rows: List[Dict[str, float]] = []
    if not isinstance(doc, list):
        return rows
    for item in doc:
        if not isinstance(item, list) or len(item) < 6:
            continue
        open_ts = _safe_float(item[0], 0.0)
        rows.append(
            {
                "open_ts": open_ts,
                "open": _safe_float(item[1]),
                "high": _safe_float(item[2]),
                "low": _safe_float(item[3]),
                "close": _safe_float(item[4]),
                "volume": _safe_float(item[5]),
            }
        )
    return rows


def _passes_stage2(
    row: Dict[str, Any],
    *,
    min_history_days: int,
    min_quote_volume_30d: float,
    max_abs_return_1d: float,
    max_corr_btc_7d: float,
    min_rel_strength_3d: float,
    min_rel_strength_7d: float,
) -> bool:
    history_days = int(row.get("history_days", 0) or 0)
    if history_days < int(min_history_days):
        return False
    if float(row.get("est_quote_volume_30d") or 0.0) < float(min_quote_volume_30d):
        return False
    ret_1d = row.get("return_1d_pct")
    if isinstance(ret_1d, (int, float)) and abs(float(ret_1d)) > float(max_abs_return_1d):
        return False
    corr = row.get("corr_btc_7d")
    if isinstance(corr, (int, float)) and float(corr) > float(max_corr_btc_7d):
        return False
    rel3 = row.get("rel_strength_3d_pct")
    if isinstance(rel3, (int, float)) and float(rel3) < float(min_rel_strength_3d):
        return False
    rel7 = row.get("rel_strength_7d_pct")
    if isinstance(rel7, (int, float)) and float(rel7) < float(min_rel_strength_7d):
        return False
    return True


def _passes_asset_profile(row: Dict[str, Any], profile: str) -> bool:
    mode = str(profile or "open").strip().lower()
    if mode == "open":
        return True
    if mode == "established":
        return str(row.get("base_asset") or "").upper() in ESTABLISHED_BASES
    raise ValueError(f"unsupported asset profile: {profile}")


def _resolve_stage2_filter(args: argparse.Namespace) -> Dict[str, float]:
    profile = str(getattr(args, "asset_profile", "open") or "open").strip().lower()
    defaults = PROFILE_STAGE2_DEFAULTS.get(profile)
    if defaults is None:
        raise ValueError(f"unsupported asset profile: {profile}")
    return {
        "min_history_days": float(
            defaults["min_history_days"] if args.stage2_min_history_days is None else args.stage2_min_history_days
        ),
        "min_quote_volume_30d": float(
            defaults["min_quote_volume_30d"]
            if args.stage2_min_quote_volume_30d is None
            else args.stage2_min_quote_volume_30d
        ),
        "max_abs_return_1d": float(
            defaults["max_abs_return_1d"] if args.stage2_max_abs_return_1d is None else args.stage2_max_abs_return_1d
        ),
        "max_corr_btc_7d": float(
            defaults["max_corr_btc_7d"] if args.stage2_max_corr_btc_7d is None else args.stage2_max_corr_btc_7d
        ),
        "min_rel_strength_3d": float(
            defaults["min_rel_strength_3d"]
            if args.stage2_min_rel_strength_3d is None
            else args.stage2_min_rel_strength_3d
        ),
        "min_rel_strength_7d": float(
            defaults["min_rel_strength_7d"]
            if args.stage2_min_rel_strength_7d is None
            else args.stage2_min_rel_strength_7d
        ),
    }


def _score_row(row: Dict[str, Any]) -> float:
    score = 0.0
    score += _safe_float(row.get("rel_strength_3d_pct")) * 0.8
    score += _safe_float(row.get("rel_strength_7d_pct")) * 1.0
    score += _safe_float(row.get("return_3d_pct")) * 0.35
    score += _safe_float(row.get("return_7d_pct")) * 0.5
    score += (_safe_float(row.get("green_ratio_7d"), 0.5) - 0.5) * 10.0
    score += float(int(row.get("btc_down_divergence_7d", 0))) * 0.75
    corr = row.get("corr_btc_7d")
    if isinstance(corr, (int, float)) and corr > 0.6:
        score -= (float(corr) - 0.6) * 10.0
    return score


def _classify_row(row: Dict[str, Any]) -> str:
    ret3 = row.get("return_3d_pct")
    rel3 = row.get("rel_strength_3d_pct")
    corr = row.get("corr_btc_7d")
    div = int(row.get("btc_down_divergence_7d", 0) or 0)
    if isinstance(ret3, (int, float)) and isinstance(rel3, (int, float)):
        if ret3 > 0.0 and rel3 > 0.0 and (not isinstance(corr, (int, float)) or float(corr) <= 0.6):
            return "preferred"
        if ret3 < 0.0 and rel3 < 0.0:
            return "avoid"
    if div >= 2 and isinstance(rel3, (int, float)) and rel3 > 0.0:
        return "diverging_up"
    return "neutral"


def _symbol_rows(
    symbols: Iterable[SymbolMeta],
    *,
    client: BinancePublicClient,
    btc_closes: List[float],
    btc_returns: List[float],
    lookback_days: int,
    min_base_volume_7d: float,
    progress_every: int,
) -> Dict[str, Any]:
    symbols = list(symbols)
    end_exclusive = _utc_midnight_now()
    out_rows: List[Dict[str, Any]] = []
    skipped_insufficient = 0
    skipped_low_volume = 0
    errors: List[Dict[str, str]] = []
    total = len(symbols)

    for idx, meta in enumerate(symbols, start=1):
        if progress_every > 0 and (idx == 1 or idx % progress_every == 0):
            print(f"[scan] {idx}/{total} {meta.symbol}", file=sys.stderr, flush=True)
        try:
            klines = _fetch_daily_klines(client, symbol=meta.symbol, days=lookback_days, end_exclusive=end_exclusive)
        except Exception as exc:
            errors.append({"symbol": meta.symbol, "error": str(exc)})
            continue
        closes = [row["close"] for row in klines if row["close"] > 0.0]
        if len(closes) < 8:
            skipped_insufficient += 1
            continue
        vols = [row["volume"] for row in klines]
        avg_close = sum(closes[-7:]) / max(1, min(7, len(closes)))
        base_volume_7d = sum(vols[-7:])
        est_quote_volume_7d = base_volume_7d * avg_close
        if est_quote_volume_7d < float(min_base_volume_7d):
            skipped_low_volume += 1
            continue
        est_quote_volume_30d = sum((row["volume"] * row["close"]) for row in klines[-30:])
        sym_returns = _daily_returns(closes)
        corr_7d = _pearson_corr(sym_returns[-7:], btc_returns[-7:])
        ret_1d = _return_pct(closes, 1)
        ret_3d = _return_pct(closes, 3)
        ret_7d = _return_pct(closes, 7)
        btc_ret_1d = _return_pct(btc_closes, 1)
        btc_ret_3d = _return_pct(btc_closes, 3)
        btc_ret_7d = _return_pct(btc_closes, 7)
        row: Dict[str, Any] = {
            "symbol": meta.symbol,
            "base_asset": meta.base_asset,
            "quote_asset": meta.quote_asset,
            "close": closes[-1],
            "history_days": len(closes),
            "return_1d_pct": ret_1d,
            "return_3d_pct": ret_3d,
            "return_7d_pct": ret_7d,
            "rel_strength_1d_pct": (ret_1d - btc_ret_1d) if ret_1d is not None and btc_ret_1d is not None else None,
            "rel_strength_3d_pct": (ret_3d - btc_ret_3d) if ret_3d is not None and btc_ret_3d is not None else None,
            "rel_strength_7d_pct": (ret_7d - btc_ret_7d) if ret_7d is not None and btc_ret_7d is not None else None,
            "green_ratio_7d": _green_ratio(sym_returns, 7),
            "corr_btc_7d": corr_7d,
            "btc_down_divergence_7d": _btc_down_divergence(sym_returns, btc_returns, 7),
            "est_quote_volume_7d": est_quote_volume_7d,
            "est_quote_volume_30d": est_quote_volume_30d,
        }
        row["score"] = _score_row(row)
        row["classification"] = _classify_row(row)
        out_rows.append(row)

    # Keep one pair per base asset, preferring the more liquid quote pair.
    best_by_base: Dict[str, Dict[str, Any]] = {}
    for row in out_rows:
        key = str(row.get("base_asset") or "")
        prev = best_by_base.get(key)
        if prev is None:
            best_by_base[key] = row
            continue
        prev_vol = float(prev.get("est_quote_volume_7d") or 0.0)
        row_vol = float(row.get("est_quote_volume_7d") or 0.0)
        if row_vol > prev_vol:
            best_by_base[key] = row
            continue
        if row_vol == prev_vol and float(row.get("score") or 0.0) > float(prev.get("score") or 0.0):
            best_by_base[key] = row

    out_rows = list(best_by_base.values())
    out_rows.sort(key=lambda item: (float(item.get("score", 0.0)), float(item.get("rel_strength_7d_pct") or -1e9)), reverse=True)
    return {
        "rows": out_rows,
        "skipped_insufficient": skipped_insufficient,
        "skipped_low_volume": skipped_low_volume,
        "errors": errors,
    }


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "symbol",
        "base_asset",
        "quote_asset",
        "classification",
        "score",
        "close",
        "history_days",
        "return_1d_pct",
        "return_3d_pct",
        "return_7d_pct",
        "rel_strength_1d_pct",
        "rel_strength_3d_pct",
        "rel_strength_7d_pct",
        "green_ratio_7d",
        "corr_btc_7d",
        "btc_down_divergence_7d",
        "est_quote_volume_7d",
        "est_quote_volume_30d",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan the Binance spot universe for multi-day strength vs BTC.")
    parser.add_argument("--base-url", default="https://api.binance.com")
    parser.add_argument("--quote-assets", default="USDT,USDC", help="Comma-separated quote assets to include.")
    parser.add_argument("--lookback-days", type=int, default=120, help="Must be at least 8 to compute 7d metrics.")
    parser.add_argument("--min-quote-volume-7d", type=float, default=1000000.0, help="Estimated quote-volume floor.")
    parser.add_argument("--asset-profile", choices=["open", "established"], default="open")
    parser.add_argument("--stage2-min-history-days", type=int, default=None)
    parser.add_argument("--stage2-min-quote-volume-30d", type=float, default=None)
    parser.add_argument("--stage2-max-abs-return-1d", type=float, default=None)
    parser.add_argument("--stage2-max-corr-btc-7d", type=float, default=None)
    parser.add_argument("--stage2-min-rel-strength-3d", type=float, default=None)
    parser.add_argument("--stage2-min-rel-strength-7d", type=float, default=None)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--max-symbols", type=int, default=0, help="Optional hard cap for testing; 0 means all.")
    parser.add_argument("--sleep-sec", type=float, default=0.03)
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--out-json", default="reports/binance_universe_strength_scan.json")
    parser.add_argument("--out-csv", default="reports/binance_universe_strength_scan.csv")
    args = parser.parse_args()

    quote_assets = {part.strip().upper() for part in str(args.quote_assets).split(",") if part.strip()}
    client = BinancePublicClient(base_url=str(args.base_url), timeout_sec=float(args.timeout_sec), sleep_sec=float(args.sleep_sec))
    all_symbols = _fetch_exchange_symbols(client, quote_assets)
    if int(args.max_symbols) > 0:
        all_symbols = all_symbols[: int(args.max_symbols)]

    btc_symbol = "BTCUSDT"
    btc_klines = _fetch_daily_klines(client, symbol=btc_symbol, days=max(8, int(args.lookback_days)), end_exclusive=_utc_midnight_now())
    btc_closes = [row["close"] for row in btc_klines if row["close"] > 0.0]
    if len(btc_closes) < 8:
        raise RuntimeError("insufficient BTCUSDT history for reference")
    btc_returns = _daily_returns(btc_closes)

    scan = _symbol_rows(
        all_symbols,
        client=client,
        btc_closes=btc_closes,
        btc_returns=btc_returns,
        lookback_days=max(8, int(args.lookback_days)),
        min_base_volume_7d=float(args.min_quote_volume_7d),
        progress_every=max(0, int(args.progress_every)),
    )
    rows: List[Dict[str, Any]] = scan["rows"]
    stage2_filter = _resolve_stage2_filter(args)
    stage2_rows = [
        row
        for row in rows
        if _passes_stage2(
            row,
            min_history_days=int(stage2_filter["min_history_days"]),
            min_quote_volume_30d=float(stage2_filter["min_quote_volume_30d"]),
            max_abs_return_1d=float(stage2_filter["max_abs_return_1d"]),
            max_corr_btc_7d=float(stage2_filter["max_corr_btc_7d"]),
            min_rel_strength_3d=float(stage2_filter["min_rel_strength_3d"]),
            min_rel_strength_7d=float(stage2_filter["min_rel_strength_7d"]),
        )
    ]
    active_rows = [row for row in stage2_rows if _passes_asset_profile(row, str(args.asset_profile))]

    top_n = max(1, int(args.top))
    top_rows = rows[:top_n]
    stage2_top_rows = stage2_rows[:top_n]
    active_top_rows = active_rows[:top_n]
    low_corr_rows = sorted(
        [row for row in rows if isinstance(row.get("corr_btc_7d"), (int, float))],
        key=lambda item: float(item.get("corr_btc_7d") or 0.0),
    )[:top_n]
    diverging_rows = [
        row
        for row in rows
        if int(row.get("btc_down_divergence_7d", 0) or 0) > 0
    ][:top_n]

    out_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "quote_assets": sorted(quote_assets),
        "btc_reference": btc_symbol,
        "summary": {
            "symbols_considered": len(all_symbols),
            "symbols_ranked": len(rows),
            "stage2_candidates": len(stage2_rows),
            "active_candidates": len(active_rows),
            "skipped_insufficient": int(scan["skipped_insufficient"]),
            "skipped_low_volume": int(scan["skipped_low_volume"]),
            "error_count": len(scan["errors"]),
            "btc_return_1d_pct": _return_pct(btc_closes, 1),
            "btc_return_3d_pct": _return_pct(btc_closes, 3),
            "btc_return_7d_pct": _return_pct(btc_closes, 7),
        },
        "stage2_filter": stage2_filter,
        "asset_profile": str(args.asset_profile),
        "top_ranked": top_rows,
        "stage2_top_ranked": stage2_top_rows,
        "active_top_ranked": active_top_rows,
        "lowest_correlation": low_corr_rows,
        "btc_down_divergers": diverging_rows,
        "rows": rows,
        "errors": scan["errors"][:200],
    }

    out_json = Path(str(args.out_json))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out_doc, ensure_ascii=True, indent=2), encoding="utf-8")
    _write_csv(Path(str(args.out_csv)), rows)

    print(json.dumps({"ok": True, "out_json": str(out_json), "rows": len(rows)}, ensure_ascii=True))
    print(json.dumps({"top_ranked": top_rows[: min(10, len(top_rows))]}, ensure_ascii=True))
    print(json.dumps({"stage2_top_ranked": stage2_top_rows[: min(10, len(stage2_top_rows))]}, ensure_ascii=True))
    print(json.dumps({"active_top_ranked": active_top_rows[: min(10, len(active_top_rows))]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
