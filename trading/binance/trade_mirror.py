from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from trading.utils.binance import split_symbol

EPS = 1e-12


def default_mirror_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "logs" / "binance_trade_mirror"


def parse_iso_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_from_ms(time_ms: int) -> str:
    return datetime.fromtimestamp(int(time_ms) / 1000.0, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def normalize_usdc_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("/", "").replace("_", "").replace("-", "")
    text = "".join(ch for ch in text if ch.isalnum())
    if not text:
        return ""
    if not text.endswith("USDC"):
        text = f"{text}USDC"
    return text


def _default_fee_to_quote(symbol: str, commission: float, commission_asset: str, trade_price: float) -> float:
    base_coin, quote_coin = split_symbol(symbol)
    asset = str(commission_asset or "").upper()
    commission = max(0.0, float(commission or 0.0))
    if commission <= 0.0:
        return 0.0
    if asset == quote_coin:
        return commission
    if asset == base_coin:
        return commission * max(0.0, float(trade_price or 0.0))
    return 0.0


def normalize_binance_trade(
    symbol: str,
    item: dict[str, Any],
    *,
    fee_to_quote: Callable[[str, float, str, float], float] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    normalized_symbol = normalize_usdc_symbol(symbol or item.get("symbol"))
    if not normalized_symbol:
        return None

    try:
        qty = float(item.get("qty", "0") or 0.0)
    except Exception:
        qty = 0.0
    try:
        price = float(item.get("price", "0") or 0.0)
    except Exception:
        price = 0.0
    try:
        quote_qty = float(item.get("quoteQty", "0") or 0.0)
    except Exception:
        quote_qty = 0.0
    if quote_qty <= 0.0 and qty > 0.0 and price > 0.0:
        quote_qty = qty * price

    if qty <= EPS or price <= EPS or quote_qty <= EPS:
        return None

    side = "BUY" if bool(item.get("isBuyer")) else "SELL"
    try:
        commission = float(item.get("commission", "0") or 0.0)
    except Exception:
        commission = 0.0
    commission_asset = str(item.get("commissionAsset", "")).upper()
    fee_quote = (
        fee_to_quote(normalized_symbol, commission, commission_asset, price)
        if callable(fee_to_quote)
        else _default_fee_to_quote(normalized_symbol, commission, commission_asset, price)
    )
    base_coin, quote_coin = split_symbol(normalized_symbol)
    fee_in_quote = commission_asset == quote_coin

    if side == "BUY":
        effective_qty = qty
        if commission_asset == base_coin:
            effective_qty = max(qty - commission, 0.0)
        # If fee is charged in base asset, quantity adjustment already captures cost.
        gross_usdc = quote_qty + (fee_quote if fee_in_quote else 0.0)
    else:
        effective_qty = qty
        if commission_asset == base_coin:
            effective_qty = qty + commission
        # If fee is charged in base asset, quantity adjustment already captures cost.
        gross_usdc = quote_qty - (fee_quote if fee_in_quote else 0.0)

    if effective_qty <= EPS or gross_usdc <= 0.0:
        return None

    time_ms = int(item.get("time") or 0)
    order_id = str(item.get("orderId", "")).strip()
    trade_id = str(item.get("id", "")).strip()
    return {
        "symbol": normalized_symbol,
        "coin": base_coin,
        "side": side,
        "orderId": order_id,
        "tradeId": trade_id,
        "timeMs": time_ms,
        "timeIso": iso_from_ms(time_ms),
        "quantity": float(effective_qty),
        "grossUsdc": float(gross_usdc),
        "price": float(price),
        "quoteQty": float(quote_qty),
        "feeUsdc": float(fee_quote),
        "commissionAsset": commission_asset or quote_coin,
    }


def aggregate_order_fills(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    fallback_counter = 0

    for trade in trades:
        symbol = str(trade["symbol"])
        side = str(trade["side"])
        order_id = str(trade.get("orderId", "")).strip()
        trade_id = str(trade.get("tradeId", "")).strip()

        group_id = order_id
        if not group_id:
            if trade_id:
                group_id = f"trade:{trade_id}"
            else:
                group_id = f"fallback:{symbol}:{side}:{fallback_counter}"
                fallback_counter += 1

        key = (symbol, side, group_id)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = {
                "symbol": symbol,
                "coin": str(trade["coin"]),
                "side": side,
                "orderId": order_id,
                "timeMinMs": int(trade["timeMs"]),
                "timeMaxMs": int(trade["timeMs"]),
                "quantity": float(trade["quantity"]),
                "grossUsdc": float(trade["grossUsdc"]),
            }
            continue

        time_ms = int(trade["timeMs"])
        existing["timeMinMs"] = min(int(existing["timeMinMs"]), time_ms)
        existing["timeMaxMs"] = max(int(existing["timeMaxMs"]), time_ms)
        existing["quantity"] = float(existing["quantity"]) + float(trade["quantity"])
        existing["grossUsdc"] = float(existing["grossUsdc"]) + float(trade["grossUsdc"])

    aggregated: list[dict[str, Any]] = []
    for item in grouped.values():
        side = str(item["side"])
        chosen_ms = int(item["timeMinMs"]) if side == "BUY" else int(item["timeMaxMs"])
        aggregated.append(
            {
                "symbol": str(item["symbol"]),
                "coin": str(item["coin"]),
                "side": side,
                "orderId": str(item.get("orderId", "")),
                "timeMs": chosen_ms,
                "timeIso": iso_from_ms(chosen_ms),
                "quantity": float(item["quantity"]),
                "grossUsdc": float(item["grossUsdc"]),
            }
        )

    aggregated.sort(key=lambda item: (int(item["timeMs"]), str(item["symbol"]), str(item["side"])))
    return aggregated


def build_report(
    from_iso: str,
    to_iso: str,
    normalized_trades: list[dict[str, Any]],
    *,
    settle_by_sell_time: bool = True,
) -> dict[str, Any]:
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    total_buy_all = 0.0
    total_sell_all = 0.0
    trade_count = 0
    for trade in normalized_trades:
        symbol = str(trade["symbol"])
        by_symbol.setdefault(symbol, []).append(trade)
        side = str(trade.get("side", "")).upper()
        gross = float(trade.get("grossUsdc", 0.0) or 0.0)
        if gross > EPS:
            if side == "BUY":
                total_buy_all += gross
            elif side == "SELL":
                total_sell_all += gross
        trade_count += 1

    bundles: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    for symbol, trades in by_symbol.items():
        trades.sort(key=lambda item: int(item["timeMs"]))
        buy_lots: list[dict[str, Any]] = []

        for trade in trades:
            qty = float(trade["quantity"])
            gross = float(trade["grossUsdc"])
            side = str(trade["side"])
            if qty <= EPS:
                continue

            if side == "BUY":
                buy_lots.append(
                    {
                        "remainingQty": qty,
                        "unitCost": gross / qty,
                        "buyTime": str(trade["timeIso"]),
                    }
                )
                continue

            remaining = qty
            sell_unit = gross / qty
            sell_time = str(trade["timeIso"])
            while remaining > EPS and buy_lots:
                lot = buy_lots[0]
                lot_qty = float(lot["remainingQty"])
                matched_qty = min(lot_qty, remaining)

                buy_gross = matched_qty * float(lot["unitCost"])
                sell_gross = matched_qty * sell_unit
                proceeds = sell_gross - buy_gross
                bundles.append(
                    {
                        "symbol": symbol,
                        "quantity": round(matched_qty, 8),
                        "buyTime": str(lot["buyTime"]),
                        "sellTime": sell_time,
                        "buyGrossUsdc": round(buy_gross, 8),
                        "sellGrossUsdc": round(sell_gross, 8),
                        "proceedsUsdc": round(proceeds, 8),
                    }
                )
                trade_rows.append(
                    {
                        "symbol": symbol,
                        "quantity": round(matched_qty, 8),
                        "buyTime": str(lot["buyTime"]),
                        "sellTime": sell_time,
                        "buyPrice": round(float(lot["unitCost"]), 12),
                        "sellPrice": round(float(sell_unit), 12),
                        "buyGrossUsdc": round(buy_gross, 8),
                        "sellGrossUsdc": round(sell_gross, 8),
                        "proceedsUsdc": round(proceeds, 8),
                        "closed": True,
                    }
                )

                remaining -= matched_qty
                lot["remainingQty"] = lot_qty - matched_qty
                if float(lot["remainingQty"]) <= EPS:
                    buy_lots.pop(0)

        for lot in buy_lots:
            remaining_qty = float(lot.get("remainingQty", 0.0) or 0.0)
            unit_cost = float(lot.get("unitCost", 0.0) or 0.0)
            if remaining_qty <= EPS or unit_cost <= EPS:
                continue
            trade_rows.append(
                {
                    "symbol": symbol,
                    "quantity": round(remaining_qty, 8),
                    "buyTime": str(lot.get("buyTime") or ""),
                    "sellTime": "",
                    "buyPrice": round(unit_cost, 12),
                    "sellPrice": 0.0,
                    "buyGrossUsdc": round(remaining_qty * unit_cost, 8),
                    "sellGrossUsdc": 0.0,
                    "proceedsUsdc": None,
                    "closed": False,
                }
            )

    bundles.sort(key=lambda item: (item["sellTime"], item["symbol"]))
    trade_rows.sort(key=lambda row: (str(row.get("buyTime") or ""), str(row.get("symbol") or "")))

    symbol_map: dict[str, dict[str, Any]] = {}
    for bundle in bundles:
        symbol = str(bundle["symbol"])
        entry = symbol_map.setdefault(
            symbol,
            {
                "symbol": symbol,
                "bundleCount": 0,
                "buyGrossUsdc": 0.0,
                "sellGrossUsdc": 0.0,
                "proceedsUsdc": 0.0,
            },
        )
        entry["bundleCount"] += 1
        entry["buyGrossUsdc"] += float(bundle["buyGrossUsdc"])
        entry["sellGrossUsdc"] += float(bundle["sellGrossUsdc"])
        entry["proceedsUsdc"] += float(bundle["proceedsUsdc"])

    symbol_summaries = [
        {
            "symbol": item["symbol"],
            "bundleCount": int(item["bundleCount"]),
            "buyGrossUsdc": round(float(item["buyGrossUsdc"]), 8),
            "sellGrossUsdc": round(float(item["sellGrossUsdc"]), 8),
            "proceedsUsdc": round(float(item["proceedsUsdc"]), 8),
        }
        for item in sorted(symbol_map.values(), key=lambda item: str(item["symbol"]))
    ]

    report_rows = trade_rows
    if settle_by_sell_time:
        from_dt = parse_iso_utc(from_iso)
        to_dt = parse_iso_utc(to_iso)
        filtered_rows: list[dict[str, Any]] = []
        for row in trade_rows:
            if not bool(row.get("closed")):
                continue
            sell_time = str(row.get("sellTime") or "").strip()
            if not sell_time:
                continue
            try:
                sell_dt = parse_iso_utc(sell_time)
            except Exception:
                continue
            if from_dt <= sell_dt < to_dt:
                filtered_rows.append(row)
        report_rows = filtered_rows

    if settle_by_sell_time:
        bundles = [
            {
                "symbol": str(row.get("symbol") or ""),
                "quantity": round(float(row.get("quantity") or 0.0), 8),
                "buyTime": str(row.get("buyTime") or ""),
                "sellTime": str(row.get("sellTime") or ""),
                "buyGrossUsdc": round(float(row.get("buyGrossUsdc") or 0.0), 8),
                "sellGrossUsdc": round(float(row.get("sellGrossUsdc") or 0.0), 8),
                "proceedsUsdc": round(float(row.get("proceedsUsdc") or 0.0), 8),
            }
            for row in report_rows
        ]
        bundles.sort(key=lambda item: (item["sellTime"], item["symbol"]))

        symbol_map = {}
        for row in report_rows:
            symbol = str(row.get("symbol") or "")
            entry = symbol_map.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "bundleCount": 0,
                    "buyGrossUsdc": 0.0,
                    "sellGrossUsdc": 0.0,
                    "proceedsUsdc": 0.0,
                },
            )
            entry["bundleCount"] += 1
            entry["buyGrossUsdc"] += float(row.get("buyGrossUsdc") or 0.0)
            entry["sellGrossUsdc"] += float(row.get("sellGrossUsdc") or 0.0)
            entry["proceedsUsdc"] += float(row.get("proceedsUsdc") or 0.0)

        symbol_summaries = [
            {
                "symbol": item["symbol"],
                "bundleCount": int(item["bundleCount"]),
                "buyGrossUsdc": round(float(item["buyGrossUsdc"]), 8),
                "sellGrossUsdc": round(float(item["sellGrossUsdc"]), 8),
                "proceedsUsdc": round(float(item["proceedsUsdc"]), 8),
            }
            for item in sorted(symbol_map.values(), key=lambda item: str(item["symbol"]))
        ]

    total_buy = sum(float(item["buyGrossUsdc"]) for item in symbol_summaries)
    total_sell = sum(float(item["sellGrossUsdc"]) for item in symbol_summaries)
    total_proceeds = sum(float(item["proceedsUsdc"]) for item in symbol_summaries)

    if settle_by_sell_time:
        total_buy_all = round(total_buy, 8)
        total_sell_all = round(total_sell, 8)
    else:
        total_buy_all = round(total_buy_all, 8)
        total_sell_all = round(total_sell_all, 8)
    matched_buy = round(total_buy, 8)
    matched_sell = round(total_sell, 8)
    matched_proceeds = round(total_proceeds, 8)
    net_cashflow = round(total_sell_all - total_buy_all, 8)

    return {
        "fromIso": from_iso,
        "toIso": to_iso,
        "tradeRows": report_rows,
        "bundles": bundles,
        "symbolSummaries": symbol_summaries,
        "daySummary": {
            "bundleCount": len(bundles),
            "tradeRowCount": len(report_rows),
            "closedTradeRowCount": len(report_rows) if settle_by_sell_time else sum(
                1 for row in report_rows if bool(row.get("closed"))
            ),
            "openTradeRowCount": 0 if settle_by_sell_time else sum(
                1 for row in report_rows if not bool(row.get("closed"))
            ),
            "symbolCount": len(symbol_summaries),
            "tradeCount": (len(report_rows) * 2) if settle_by_sell_time else trade_count,
            "buyGrossUsdc": total_buy_all,
            "sellGrossUsdc": total_sell_all,
            "proceedsUsdc": matched_proceeds,
            "matchedBuyGrossUsdc": matched_buy,
            "matchedSellGrossUsdc": matched_sell,
            "matchedProceedsUsdc": matched_proceeds,
            "netCashflowUsdc": net_cashflow,
        },
    }


def mirror_path_for_symbol(symbol: str, mirror_dir: Path | None = None) -> Path:
    normalized_symbol = normalize_usdc_symbol(symbol)
    return (mirror_dir or default_mirror_dir()) / f"{normalized_symbol}.jsonl"


def load_mirror_rows(symbol: str, mirror_dir: Path | None = None) -> list[dict[str, Any]]:
    path = mirror_path_for_symbol(symbol, mirror_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                raw = raw_line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    rows.sort(
        key=lambda item: (
            int(item.get("timeMs") or 0),
            str(item.get("tradeId") or ""),
            str(item.get("orderId") or ""),
        )
    )
    return rows


def _mirror_row_key(item: dict[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        str(item.get("symbol") or ""),
        str(item.get("tradeId") or ""),
        str(item.get("orderId") or ""),
        int(item.get("timeMs") or 0),
        str(item.get("side") or ""),
    )


def write_mirror_rows(symbol: str, rows: list[dict[str, Any]], mirror_dir: Path | None = None) -> Path:
    path = mirror_path_for_symbol(symbol, mirror_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_sorted = sorted(
        rows,
        key=lambda item: (
            int(item.get("timeMs") or 0),
            str(item.get("tradeId") or ""),
            str(item.get("orderId") or ""),
        ),
    )
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        tmp_path = Path(handle.name)
        for row in rows_sorted:
            handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")
    tmp_path.replace(path)
    return path


def merge_mirror_rows(existing_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
    for row in existing_rows:
        if isinstance(row, dict):
            merged[_mirror_row_key(row)] = row
    for row in new_rows:
        if isinstance(row, dict):
            merged[_mirror_row_key(row)] = row
    return list(merged.values())


def collect_trades_mirror(
    from_iso: str,
    to_iso: str,
    symbols_filter: list[str] | None = None,
    *,
    mirror_dir: Path | None = None,
) -> dict[str, Any]:
    from_dt = parse_iso_utc(from_iso)
    to_dt = parse_iso_utc(to_iso)
    if to_dt <= from_dt:
        raise ValueError("Ungueltiges Zeitfenster: toIso muss groesser als fromIso sein.")

    normalized_trades: list[dict[str, Any]] = []
    symbols = [normalize_usdc_symbol(item) for item in (symbols_filter or []) if normalize_usdc_symbol(item)]
    if symbols:
        files = [mirror_path_for_symbol(symbol, mirror_dir) for symbol in symbols]
    else:
        files = sorted((mirror_dir or default_mirror_dir()).glob("*USDC.jsonl"))
    scanned_files = 0
    for path in files:
        if not path.is_file():
            continue
        scanned_files += 1
        symbol = normalize_usdc_symbol(path.stem)
        coin = symbol.removesuffix("USDC")
        _, quote_coin = split_symbol(symbol)
        for row in load_mirror_rows(symbol, mirror_dir):
            try:
                trade_dt = parse_iso_utc(str(row.get("timeIso") or ""))
            except Exception:
                continue
            if not (trade_dt < to_dt):
                continue
            qty = float(row.get("quantity") or 0.0)
            if qty <= EPS:
                continue
            side = str(row.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue
            gross_usdc = float(row.get("grossUsdc") or 0.0)
            quote_qty = float(row.get("quoteQty") or 0.0)
            fee_usdc = max(0.0, float(row.get("feeUsdc") or 0.0))
            commission_asset = str(row.get("commissionAsset") or "").upper()
            if quote_qty > EPS:
                if commission_asset == quote_coin:
                    gross_usdc = quote_qty + fee_usdc if side == "BUY" else quote_qty - fee_usdc
                else:
                    # Fee in base/other asset: quote flow is already complete in quoteQty.
                    gross_usdc = quote_qty
            if gross_usdc <= 0.0:
                continue
            normalized_trades.append(
                {
                    "symbol": symbol,
                    "coin": coin,
                    "side": side,
                    "orderId": str(row.get("orderId") or ""),
                    "tradeId": str(row.get("tradeId") or ""),
                    "timeMs": int(row.get("timeMs") or 0),
                    "timeIso": str(row.get("timeIso") or ""),
                    "quantity": qty,
                    "grossUsdc": gross_usdc,
                }
            )

    if scanned_files <= 0:
        raise ValueError("Keine Binance-Trade-Mirror-Dateien gefunden.")

    merged_trades = aggregate_order_fills(normalized_trades)
    report = build_report(
        from_iso=from_dt.isoformat().replace("+00:00", "Z"),
        to_iso=to_dt.isoformat().replace("+00:00", "Z"),
        normalized_trades=merged_trades,
    )
    report["source"] = "binance_mirror"
    report["scannedMirrorFiles"] = scanned_files
    report["fillEvents"] = len(normalized_trades)
    return report


def sync_symbol_window(
    client: Any,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
    mirror_dir: Path | None = None,
) -> dict[str, Any]:
    normalized_symbol = normalize_usdc_symbol(symbol)
    raw_rows: list[dict[str, Any]] = []
    chunk_start_ms = int(start_ms)
    hard_end_ms = int(end_ms)
    max_window_ms = 24 * 60 * 60 * 1000

    while chunk_start_ms < hard_end_ms:
        chunk_end_ms = min(hard_end_ms, chunk_start_ms + max_window_ms - 1)
        seen_from_ids: set[int] = set()
        from_id: int | None = None
        while True:
            batch = client.my_trades_window(
                symbol=normalized_symbol,
                start_ms=chunk_start_ms,
                end_ms=chunk_end_ms,
                limit=1000,
                from_id=from_id,
            )
            if not batch:
                break
            raw_rows.extend(batch)
            ids = [int(item.get("id")) for item in batch if str(item.get("id", "")).strip().isdigit()]
            if len(batch) < 1000 or not ids:
                break
            next_from_id = max(ids) + 1
            if next_from_id in seen_from_ids:
                break
            seen_from_ids.add(next_from_id)
            from_id = next_from_id
        chunk_start_ms = chunk_end_ms + 1

    normalized_rows = [
        item
        for item in (
            normalize_binance_trade(
                normalized_symbol,
                raw,
                fee_to_quote=lambda sym, commission, commission_asset, trade_price: client.commission_to_quote(
                    symbol=sym,
                    commission=commission,
                    commission_asset=commission_asset,
                    trade_price=trade_price,
                ),
            )
            for raw in raw_rows
        )
        if item is not None
    ]
    existing = load_mirror_rows(normalized_symbol, mirror_dir)
    merged = merge_mirror_rows(existing, normalized_rows)
    write_mirror_rows(normalized_symbol, merged, mirror_dir)
    return {
        "symbol": normalized_symbol,
        "fetchedRows": len(raw_rows),
        "normalizedRows": len(normalized_rows),
        "newRows": max(0, len(merged) - len(existing)),
        "totalRows": len(merged),
    }
