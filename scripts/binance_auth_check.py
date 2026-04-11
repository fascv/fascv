#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.utils.env import load_env


class BinanceHTTPError(RuntimeError):
    def __init__(self, *, status: int, code: Any, msg: Any, url: str) -> None:
        self.status = int(status)
        self.code = code
        self.msg = msg
        self.url = str(url)
        super().__init__(f"binance_http_{self.status}: code={self.code} msg={self.msg} url={self.url}")


class BinanceClient:
    def __init__(self, *, api_key: str, api_secret: str, base_url: str = "https://api.binance.com") -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")

    def _request_public(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.base_url + path
        resp = requests.get(url, params=params or {}, timeout=12.0)
        try:
            doc: Dict[str, Any] = resp.json()
        except Exception:
            resp.raise_for_status()
            raise
        if resp.status_code >= 400:
            raise BinanceHTTPError(status=resp.status_code, code=doc.get("code"), msg=doc.get("msg"), url=resp.url)
        return doc

    def _request_signed(self, path: str, params: Optional[Dict[str, Any]] = None, method: str = "GET") -> Dict[str, Any]:
        payload: Dict[str, Any] = dict(params or {})
        payload.setdefault("recvWindow", 5000)
        payload["timestamp"] = int(time.time() * 1000)
        query = urlencode({k: str(v) for k, v in payload.items()})
        sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        payload["signature"] = sig

        url = self.base_url + path
        headers = {"X-MBX-APIKEY": self.api_key}
        resp = requests.request(method.upper(), url, params=payload, headers=headers, timeout=12.0)
        try:
            doc: Dict[str, Any] = resp.json()
        except Exception:
            resp.raise_for_status()
            raise
        if resp.status_code >= 400:
            raise BinanceHTTPError(status=resp.status_code, code=doc.get("code"), msg=doc.get("msg"), url=resp.url)
        return doc


def _symbol_token(symbol: str) -> str:
    return str(symbol or "ETHUSDT").strip().upper().replace("/", "").replace("-", "")


def _sample_balances(account_doc: Dict[str, Any], limit: int = 12) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    balances = account_doc.get("balances")
    if not isinstance(balances, list):
        return out
    for item in balances:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset", "")).strip().upper()
        if not asset:
            continue
        try:
            free = float(item.get("free", "0") or 0.0)
            locked = float(item.get("locked", "0") or 0.0)
        except Exception:
            continue
        if free <= 0.0 and locked <= 0.0:
            continue
        out[asset] = {"free": free, "locked": locked, "total": free + locked}
    return {k: out[k] for k in sorted(out.keys())[:limit]}


def _extract_fee_rates(account_doc: Dict[str, Any], fee_doc: Any, symbol: str) -> Dict[str, Any]:
    maker_rate = None
    taker_rate = None
    source = None

    if isinstance(fee_doc, list) and fee_doc:
        row = fee_doc[0]
        if isinstance(row, dict):
            try:
                maker_rate = float(row.get("makerCommission"))
                taker_rate = float(row.get("takerCommission"))
                source = "sapi_trade_fee"
            except Exception:
                pass

    if maker_rate is None or taker_rate is None:
        rates = account_doc.get("commissionRates")
        if isinstance(rates, dict):
            try:
                maker_rate = float(rates.get("maker"))
                taker_rate = float(rates.get("taker"))
                source = "account_commissionRates"
            except Exception:
                pass

    if maker_rate is None or taker_rate is None:
        try:
            maker_rate = float(account_doc.get("makerCommission", 0.0)) / 10000.0
            taker_rate = float(account_doc.get("takerCommission", 0.0)) / 10000.0
            source = "account_commission_int"
        except Exception:
            maker_rate = None
            taker_rate = None

    out: Dict[str, Any] = {
        "symbol": symbol,
        "source": source,
        "maker_rate": maker_rate,
        "taker_rate": taker_rate,
        "maker_bps": (maker_rate * 10000.0) if isinstance(maker_rate, float) else None,
        "taker_bps": (taker_rate * 10000.0) if isinstance(taker_rate, float) else None,
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only Binance API check: account, balances, ticker, effective fees.")
    ap.add_argument("--env", default=".env", help="Path to .env")
    ap.add_argument("--symbol", default="ETHUSDT", help="Binance symbol, e.g. ETHUSDT")
    ap.add_argument("--base-url", default="", help="Override API base URL")
    ap.add_argument("--check-order-test", action="store_true", help="Also run /api/v3/order/test to verify trade permission.")
    ap.add_argument("--test-qty", default="0.001", help="Quantity used with --check-order-test (base asset units).")
    args = ap.parse_args()

    load_env(args.env)

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    base_url = (args.base_url or os.getenv("BINANCE_BASE_URL", "https://api.binance.com")).strip() or "https://api.binance.com"
    symbol = _symbol_token(args.symbol)

    if not api_key or not api_secret:
        raise SystemExit("missing BINANCE_API_KEY / BINANCE_API_SECRET in env")

    client = BinanceClient(api_key=api_key, api_secret=api_secret, base_url=base_url)

    # Public sanity check.
    ticker = client._request_public("/api/v3/ticker/price", {"symbol": symbol})

    account = client._request_signed("/api/v3/account")

    fee_doc: Any = None
    fee_error: Optional[str] = None
    try:
        fee_doc = client._request_signed("/sapi/v1/asset/tradeFee", {"symbol": symbol})
    except Exception as exc:
        fee_error = str(exc)

    fees = _extract_fee_rates(account, fee_doc if fee_doc is not None else None, symbol)

    order_test_ok: Optional[bool] = None
    order_test_error: Optional[str] = None
    if args.check_order_test:
        try:
            client._request_signed(
                "/api/v3/order/test",
                {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": str(args.test_qty),
                },
                method="POST",
            )
            order_test_ok = True
        except Exception as exc:
            order_test_ok = False
            order_test_error = str(exc)

    print(
        json.dumps(
            {
                "ok": True,
                "exchange": "binance",
                "base_url": base_url,
                "symbol": symbol,
                "ticker_price": ticker.get("price"),
                "can_trade": account.get("canTrade"),
                "account_type": account.get("accountType"),
                "balances_detected": len(_sample_balances(account)),
                "balances_sample": _sample_balances(account),
                "fees": fees,
                "fee_endpoint_error": fee_error,
                "order_test_ok": order_test_ok,
                "order_test_error": order_test_error,
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
