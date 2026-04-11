#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from binance_universe_strength_scan import BinancePublicClient, _fetch_daily_klines, _utc_midnight_now


def _load_report_symbols(path: Path, list_key: str, limit: int) -> List[str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get(list_key)
    if not isinstance(rows, list):
        return []
    out: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        out.append(symbol)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _load_candidate_symbols(specs: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in specs:
        text = str(raw or "").strip()
        if not text:
            continue
        if "::" in text:
            parts = text.split("::")
            if len(parts) != 3:
                raise SystemExit(f"invalid report spec: {text}")
            path = Path(parts[0])
            list_key = parts[1]
            limit = int(parts[2])
            symbols = _load_report_symbols(path, list_key, limit)
        else:
            symbols = [s.strip().upper() for s in text.split(",") if s.strip()]
        for symbol in symbols:
            if symbol in seen or symbol == "BTCUSDT":
                continue
            seen.add(symbol)
            out.append(symbol)
    if not out:
        raise SystemExit("no candidate symbols resolved")
    return out


def _rows_to_close_map(rows: List[Dict[str, float]]) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for row in rows:
        ts = int(row.get("open_ts", 0) or 0)
        close = float(row.get("close", 0.0) or 0.0)
        if ts <= 0 or close <= 0.0:
            continue
        out[ts] = close
    return out


def _aligned_closes(a: Dict[int, float], b: Dict[int, float]) -> Tuple[List[int], List[float], List[float]]:
    keys = sorted(set(a.keys()) & set(b.keys()))
    xa = [a[k] for k in keys]
    xb = [b[k] for k in keys]
    return keys, xa, xb


def _window_returns(closes: List[float], window: int) -> List[float | None]:
    out: List[float | None] = [None] * len(closes)
    need = max(1, int(window))
    for idx in range(need, len(closes)):
        prev = closes[idx - need]
        cur = closes[idx]
        if prev <= 0.0:
            continue
        out[idx] = (cur / prev - 1.0) * 100.0
    return out


def _safe_div(num: float, den: float) -> float:
    if den == 0.0:
        return 0.0
    return num / den


def _mean(xs: List[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / float(len(xs))


def _score(row: Dict[str, Any]) -> float:
    return (
        35.0 * float(row.get("up_ratio_on_btc_down_1d", 0.0))
        + 65.0 * float(row.get("up_ratio_on_btc_down_3d", 0.0))
        + 0.35 * float(row.get("avg_excess_vs_btc_down_1d_pct", 0.0))
        + 0.65 * float(row.get("avg_excess_vs_btc_down_3d_pct", 0.0))
        + 0.25 * float(row.get("avg_coin_return_on_btc_down_3d_pct", 0.0))
    )


def _analyze_symbol(
    *,
    client: BinancePublicClient,
    symbol: str,
    btc_close_map: Dict[int, float],
    end_exclusive,
    lookback_days: int,
    btc_down_threshold_1d_pct: float,
    btc_down_threshold_3d_pct: float,
) -> Dict[str, Any] | None:
    rows = _fetch_daily_klines(client, symbol=symbol, days=lookback_days, end_exclusive=end_exclusive)
    close_map = _rows_to_close_map(rows)
    keys, coin_closes, btc_closes = _aligned_closes(close_map, btc_close_map)
    if len(keys) < 20:
        return None

    coin_1d = _window_returns(coin_closes, 1)
    btc_1d = _window_returns(btc_closes, 1)
    coin_3d = _window_returns(coin_closes, 3)
    btc_3d = _window_returns(btc_closes, 3)

    one_day_coin: List[float] = []
    one_day_btc: List[float] = []
    one_day_up = 0
    one_day_outperform = 0

    three_day_coin: List[float] = []
    three_day_btc: List[float] = []
    three_day_up = 0
    three_day_outperform = 0

    for idx in range(len(keys)):
        b1 = btc_1d[idx]
        c1 = coin_1d[idx]
        if b1 is not None and c1 is not None and b1 <= float(btc_down_threshold_1d_pct):
            one_day_btc.append(float(b1))
            one_day_coin.append(float(c1))
            if c1 > 0.0:
                one_day_up += 1
            if c1 > b1:
                one_day_outperform += 1

        b3 = btc_3d[idx]
        c3 = coin_3d[idx]
        if b3 is not None and c3 is not None and b3 <= float(btc_down_threshold_3d_pct):
            three_day_btc.append(float(b3))
            three_day_coin.append(float(c3))
            if c3 > 0.0:
                three_day_up += 1
            if c3 > b3:
                three_day_outperform += 1

    row = {
        "symbol": symbol,
        "history_days_aligned": len(keys),
        "btc_down_days_1d": len(one_day_btc),
        "coin_up_days_on_btc_down_1d": one_day_up,
        "up_ratio_on_btc_down_1d": _safe_div(float(one_day_up), float(len(one_day_btc))),
        "outperform_ratio_vs_btc_down_1d": _safe_div(float(one_day_outperform), float(len(one_day_btc))),
        "avg_coin_return_on_btc_down_1d_pct": _mean(one_day_coin),
        "avg_btc_return_on_btc_down_1d_pct": _mean(one_day_btc),
        "avg_excess_vs_btc_down_1d_pct": _mean([c - b for c, b in zip(one_day_coin, one_day_btc)]),
        "btc_down_windows_3d": len(three_day_btc),
        "coin_up_windows_on_btc_down_3d": three_day_up,
        "up_ratio_on_btc_down_3d": _safe_div(float(three_day_up), float(len(three_day_btc))),
        "outperform_ratio_vs_btc_down_3d": _safe_div(float(three_day_outperform), float(len(three_day_btc))),
        "avg_coin_return_on_btc_down_3d_pct": _mean(three_day_coin),
        "avg_btc_return_on_btc_down_3d_pct": _mean(three_day_btc),
        "avg_excess_vs_btc_down_3d_pct": _mean([c - b for c, b in zip(three_day_coin, three_day_btc)]),
    }
    row["countertrend_score"] = _score(row)
    return row


def main() -> None:
    p = argparse.ArgumentParser(description="Rank symbols by how well they counter BTC down phases over 1d and 3d windows.")
    p.add_argument(
        "--candidate-source",
        action="append",
        default=[],
        help=(
            "Either comma-separated symbols or report spec "
            "'path::list_key::limit'. Can be passed multiple times."
        ),
    )
    p.add_argument("--base-url", default="https://api.binance.com")
    p.add_argument("--lookback-days", type=int, default=180)
    p.add_argument("--btc-down-threshold-1d-pct", type=float, default=0.0)
    p.add_argument("--btc-down-threshold-3d-pct", type=float, default=0.0)
    p.add_argument("--timeout-sec", type=float, default=10.0)
    p.add_argument("--sleep-sec", type=float, default=0.03)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--out-json", default="reports/binance_btc_countertrend_scan.json")
    p.add_argument("--out-csv", default="reports/binance_btc_countertrend_scan.csv")
    args = p.parse_args()

    candidate_specs = list(args.candidate_source or [])
    if not candidate_specs:
        candidate_specs = [
            "reports/binance_universe_strength_scan_open.json::stage2_top_ranked::15",
            "reports/binance_universe_strength_scan.json::active_top_ranked::8",
        ]

    symbols = _load_candidate_symbols(candidate_specs)
    client = BinancePublicClient(
        base_url=str(args.base_url),
        timeout_sec=float(args.timeout_sec),
        sleep_sec=float(args.sleep_sec),
    )
    end_exclusive = _utc_midnight_now()
    btc_rows = _fetch_daily_klines(client, symbol="BTCUSDT", days=int(args.lookback_days), end_exclusive=end_exclusive)
    btc_close_map = _rows_to_close_map(btc_rows)
    if len(btc_close_map) < 20:
        raise SystemExit("failed to load BTC benchmark history")

    results: List[Dict[str, Any]] = []
    for idx, symbol in enumerate(symbols, start=1):
        print(f"[countertrend] {idx}/{len(symbols)} {symbol}")
        row = _analyze_symbol(
            client=client,
            symbol=symbol,
            btc_close_map=btc_close_map,
            end_exclusive=end_exclusive,
            lookback_days=int(args.lookback_days),
            btc_down_threshold_1d_pct=float(args.btc_down_threshold_1d_pct),
            btc_down_threshold_3d_pct=float(args.btc_down_threshold_3d_pct),
        )
        if row is None:
            continue
        results.append(row)

    results.sort(key=lambda x: float(x.get("countertrend_score", -1e18)), reverse=True)
    top_rows = results[: max(1, int(args.top))]

    out_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": int(args.lookback_days),
        "btc_down_threshold_1d_pct": float(args.btc_down_threshold_1d_pct),
        "btc_down_threshold_3d_pct": float(args.btc_down_threshold_3d_pct),
        "candidate_count": len(symbols),
        "evaluated_count": len(results),
        "top_ranked": top_rows,
        "all_ranked": results,
    }

    out_json = Path(str(args.out_json))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out_doc, ensure_ascii=True, indent=2), encoding="utf-8")

    out_csv = Path(str(args.out_csv))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "symbol",
        "countertrend_score",
        "history_days_aligned",
        "btc_down_days_1d",
        "coin_up_days_on_btc_down_1d",
        "up_ratio_on_btc_down_1d",
        "outperform_ratio_vs_btc_down_1d",
        "avg_coin_return_on_btc_down_1d_pct",
        "avg_btc_return_on_btc_down_1d_pct",
        "avg_excess_vs_btc_down_1d_pct",
        "btc_down_windows_3d",
        "coin_up_windows_on_btc_down_3d",
        "up_ratio_on_btc_down_3d",
        "outperform_ratio_vs_btc_down_3d",
        "avg_coin_return_on_btc_down_3d_pct",
        "avg_btc_return_on_btc_down_3d_pct",
        "avg_excess_vs_btc_down_3d_pct",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k) for k in fieldnames})

    print(json.dumps({"ok": True, "out_json": str(out_json), "evaluated": len(results)}, ensure_ascii=True))
    print(json.dumps({"top_ranked": top_rows}, ensure_ascii=True))


if __name__ == "__main__":
    main()
