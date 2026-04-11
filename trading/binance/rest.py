from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import requests

from trading.kraken.rest import KrakenAPIError, KrakenAuthError, KrakenRateLimitError, KrakenTransientError
from trading.utils.binance import split_symbol, to_binance_symbol


@dataclass
class BinanceRestClient:
    api_key: str
    api_secret: str
    base_url: str = "https://api.binance.com"
    symbol: str = "ETHUSDT"
    timeout: float = 10.0
    recv_window: int = 5000
    max_retries: int = 2
    retry_backoff_sec: float = 0.25
    retry_max_backoff_sec: float = 2.0
    _symbol_rules_cache: Dict[str, Dict[str, Decimal]] = field(default_factory=dict)
    _price_cache: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    price_cache_ttl_sec: float = 15.0

    @staticmethod
    def _norm_num_str(value: str) -> str:
        try:
            d = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
        text = format(d, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _is_rate_limit(code: Any, status: int, msg: str) -> bool:
        try:
            code_i = int(code)
        except Exception:
            code_i = None
        if status == 429:
            return True
        if status == 418:
            return True
        if code_i in {-1003, -1015}:
            return True
        text = (msg or "").lower()
        return "too many requests" in text or "rate limit" in text

    @staticmethod
    def _is_auth(code: Any, status: int, msg: str) -> bool:
        try:
            code_i = int(code)
        except Exception:
            code_i = None
        if status in {401, 403}:
            return True
        if code_i in {-2014, -2015, -1022}:
            return True
        text = (msg or "").lower()
        return "invalid api-key" in text or "signature" in text or "permission" in text

    @staticmethod
    def _is_transient(code: Any, status: int, msg: str) -> bool:
        try:
            code_i = int(code)
        except Exception:
            code_i = None
        if status in {408, 409, 425} or status >= 500:
            return True
        if code_i in {-1001, -1007}:  # disconnected/timeout
            return True
        text = (msg or "").lower()
        return "internal error" in text or "service unavailable" in text

    def _classify_error(self, *, status: int, code: Any, msg: str, payload: Optional[Dict[str, Any]] = None) -> KrakenAPIError:
        err = f"EBINANCE:{code}:{msg}"
        if self._is_rate_limit(code, status, msg):
            return KrakenRateLimitError([err], payload or {})
        if self._is_auth(code, status, msg):
            return KrakenAuthError([err], payload or {})
        if self._is_transient(code, status, msg):
            return KrakenTransientError([err], payload or {})
        return KrakenAPIError([err], payload or {})

    def _backoff(self, attempt: int) -> float:
        return min(self.retry_max_backoff_sec, self.retry_backoff_sec * (2 ** attempt))

    @staticmethod
    def _quantize_down(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        units = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return units * step

    def _symbol_rules(self, symbol: str) -> Dict[str, Decimal]:
        sym = str(symbol or "").upper()
        cached = self._symbol_rules_cache.get(sym)
        if cached is not None:
            return cached

        rules: Dict[str, Decimal] = {
            "lot_min_qty": Decimal("0"),
            "lot_step": Decimal("0"),
            "market_min_qty": Decimal("0"),
            "market_step": Decimal("0"),
            "price_tick": Decimal("0"),
        }
        try:
            info = self._request(
                "GET",
                "/api/v3/exchangeInfo",
                {"symbol": sym},
                signed=False,
                max_retries=1,
            )
            symbols = info.get("symbols") if isinstance(info, dict) else None
            row = symbols[0] if isinstance(symbols, list) and symbols else None
            filters = row.get("filters") if isinstance(row, dict) else None
            if isinstance(filters, list):
                for f in filters:
                    if not isinstance(f, dict):
                        continue
                    ftype = str(f.get("filterType", ""))
                    if ftype == "LOT_SIZE":
                        rules["lot_min_qty"] = Decimal(str(f.get("minQty", "0") or "0"))
                        rules["lot_step"] = Decimal(str(f.get("stepSize", "0") or "0"))
                    elif ftype == "MARKET_LOT_SIZE":
                        rules["market_min_qty"] = Decimal(str(f.get("minQty", "0") or "0"))
                        rules["market_step"] = Decimal(str(f.get("stepSize", "0") or "0"))
                    elif ftype in {"NOTIONAL", "MIN_NOTIONAL"}:
                        rules["min_notional"] = Decimal(str(f.get("minNotional", "0") or "0"))
                    elif ftype == "PRICE_FILTER":
                        rules["price_tick"] = Decimal(str(f.get("tickSize", "0") or "0"))
        except Exception:
            pass

        self._symbol_rules_cache[sym] = rules
        return rules

    def _normalize_qty(self, symbol: str, qty: Decimal, order_type: str) -> str:
        rules = self._symbol_rules(symbol)
        ot = str(order_type or "market").lower()
        step = rules.get("market_step", Decimal("0")) if ot == "market" else Decimal("0")
        if step <= 0:
            step = rules.get("lot_step", Decimal("0"))
        min_qty = rules.get("market_min_qty", Decimal("0")) if ot == "market" else Decimal("0")
        if min_qty <= 0:
            min_qty = rules.get("lot_min_qty", Decimal("0"))

        q = max(Decimal("0"), qty)
        if step > 0:
            q = self._quantize_down(q, step)
        if q <= 0:
            raise KrakenAPIError(["EBINANCE:INVALID_QTY:quantity_quantized_to_zero"])
        if min_qty > 0 and q < min_qty:
            raise KrakenAPIError([f"EBINANCE:INVALID_QTY:quantity_below_min_qty:{self._norm_num_str(str(min_qty))}"])
        return self._norm_num_str(str(q))

    def _normalize_price(self, symbol: str, price: Decimal) -> str:
        rules = self._symbol_rules(symbol)
        tick = rules.get("price_tick", Decimal("0"))
        p = max(Decimal("0"), price)
        if tick > 0:
            p = self._quantize_down(p, tick)
        if p <= 0:
            raise KrakenAPIError(["EBINANCE:INVALID_PRICE:price_quantized_to_zero"])
        return self._norm_num_str(str(p))

    def min_notional(self, symbol: Optional[str] = None) -> float:
        sym = str(symbol or self.symbol).upper()
        rules = self._symbol_rules(sym)
        try:
            return max(0.0, float(rules.get("min_notional", Decimal("0")) or 0.0))
        except Exception:
            return 0.0

    def ticker_price(self, symbol: Optional[str] = None) -> float:
        sym = str(symbol or self.symbol).upper()
        try:
            return max(0.0, float(self._ticker_price(sym) or 0.0))
        except Exception:
            return 0.0

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        signed: bool,
        api_key: bool = False,
        max_retries: Optional[int] = None,
    ) -> Any:
        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        payload: Dict[str, Any] = dict(params or {})
        headers = {"User-Agent": "codex-trader/1.0"}
        if signed:
            payload["timestamp"] = int(time.time() * 1000)
            payload["recvWindow"] = int(self.recv_window)
            query = urlencode({k: str(v) for k, v in payload.items() if v is not None})
            sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
            payload["signature"] = sig
            headers["X-MBX-APIKEY"] = self.api_key
        elif api_key:
            headers["X-MBX-APIKEY"] = self.api_key

        url = f"{self.base_url.rstrip('/')}{path}"
        attempt = 0
        while True:
            try:
                resp = requests.request(method.upper(), url, params=payload, headers=headers, timeout=float(self.timeout))
                text = resp.text
                doc: Any
                try:
                    doc = json.loads(text)
                except Exception:
                    doc = {"code": f"HTTP{resp.status_code}", "msg": text[:200]}

                if resp.status_code >= 400:
                    code = doc.get("code") if isinstance(doc, dict) else f"HTTP{resp.status_code}"
                    msg = str(doc.get("msg", "http_error")) if isinstance(doc, dict) else "http_error"
                    raise self._classify_error(status=resp.status_code, code=code, msg=msg, payload=doc if isinstance(doc, dict) else None)

                # Some endpoints can still return {code,msg} with 200.
                if isinstance(doc, dict) and doc.get("code") is not None and doc.get("code") != 0:
                    code = doc.get("code")
                    msg = str(doc.get("msg", "api_error"))
                    raise self._classify_error(status=200, code=code, msg=msg, payload=doc)

                return doc
            except KrakenRateLimitError:
                if attempt >= retries:
                    raise
            except KrakenTransientError:
                if attempt >= retries:
                    raise
            except (requests.RequestException, TimeoutError, socket.timeout) as exc:
                if attempt >= retries:
                    raise KrakenTransientError([f"ENetwork:{exc}"])
            time.sleep(self._backoff(attempt))
            attempt += 1

    def _order_payload(self, order: Dict[str, Any], *, include_fee: bool = False) -> Dict[str, Any]:
        status_raw = str(order.get("status", "")).upper()
        status = "open"
        if status_raw in {"FILLED"}:
            status = "closed"
        elif status_raw in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            status = "canceled" if status_raw != "EXPIRED" else "expired"

        try:
            vol = float(order.get("origQty", "0") or 0.0)
        except Exception:
            vol = 0.0
        try:
            vol_exec = float(order.get("executedQty", "0") or 0.0)
        except Exception:
            vol_exec = 0.0
        try:
            price = float(order.get("price", "0") or 0.0)
        except Exception:
            price = 0.0
        try:
            cost = float(order.get("cummulativeQuoteQty", "0") or 0.0)
        except Exception:
            cost = 0.0

        side = str(order.get("side", "")).strip().lower()
        opened = float(order.get("time", 0) or 0) / 1000.0
        updated = float(order.get("updateTime", 0) or 0) / 1000.0

        payload: Dict[str, Any] = {
            "status": status,
            "vol": str(vol),
            "vol_exec": str(vol_exec),
            "price": str(price),
            "cost": str(cost),
            "type": side,
            "opentm": opened,
            "lastupdated": updated,
            "descr": {"type": side, "price": str(price)},
        }
        if include_fee:
            fee_quote, traded_qty, traded_quote = self._order_trade_summary(order)
            if traded_qty > 0.0:
                payload["vol_exec"] = str(traded_qty)
            if traded_quote > 0.0:
                payload["cost"] = str(traded_quote)
            payload["fee"] = str(fee_quote)
        return payload

    def _ticker_price(self, symbol: str) -> float:
        sym = str(symbol or "").upper()
        if not sym:
            return 0.0
        now = time.time()
        cached = self._price_cache.get(sym)
        if cached is not None:
            cached_ts, cached_price = cached
            if now - cached_ts <= max(0.0, float(self.price_cache_ttl_sec)):
                return max(0.0, float(cached_price))
        doc = self._request(
            "GET",
            "/api/v3/ticker/price",
            {"symbol": sym},
            signed=False,
            max_retries=1,
        )
        try:
            px = float(doc.get("price", "0") if isinstance(doc, dict) else 0.0)
        except Exception:
            px = 0.0
        if px > 0.0:
            self._price_cache[sym] = (now, px)
            return px
        return 0.0

    def _asset_to_quote_rate(self, *, asset: str, quote: str) -> float:
        a = str(asset or "").upper()
        q = str(quote or "").upper()
        if not a or not q:
            return 0.0
        if a == q:
            return 1.0

        direct = self._ticker_price(f"{a}{q}")
        if direct > 0.0:
            return direct
        inverse = self._ticker_price(f"{q}{a}")
        if inverse > 0.0:
            return 1.0 / inverse

        for bridge in ("USDT", "USDC", "BTC", "ETH", "BNB"):
            if bridge in {a, q}:
                continue
            a_bridge = self._ticker_price(f"{a}{bridge}")
            if a_bridge <= 0.0:
                inv = self._ticker_price(f"{bridge}{a}")
                if inv > 0.0:
                    a_bridge = 1.0 / inv
            q_bridge = self._ticker_price(f"{q}{bridge}")
            if q_bridge <= 0.0:
                inv = self._ticker_price(f"{bridge}{q}")
                if inv > 0.0:
                    q_bridge = 1.0 / inv
            if a_bridge > 0.0 and q_bridge > 0.0:
                return a_bridge / q_bridge
        return 0.0

    def commission_to_quote(
        self,
        *,
        symbol: str,
        commission: float,
        commission_asset: str,
        trade_price: float = 0.0,
    ) -> float:
        comm = max(0.0, float(commission or 0.0))
        if comm <= 0.0:
            return 0.0
        base, quote = split_symbol(symbol)
        asset = str(commission_asset or "").upper()
        if not asset:
            return 0.0
        if asset == quote:
            return comm
        if asset == base:
            px = max(0.0, float(trade_price or 0.0))
            if px <= 0.0:
                px = self._ticker_price(str(symbol).upper())
            return comm * px if px > 0.0 else 0.0
        rate = self._asset_to_quote_rate(asset=asset, quote=quote)
        if rate <= 0.0:
            return 0.0
        return comm * rate

    def _order_trade_summary(self, order: Dict[str, Any]) -> Tuple[float, float, float]:
        order_id = order.get("orderId")
        if order_id is None:
            return 0.0, 0.0, 0.0
        sym = str(order.get("symbol", self.symbol)).upper()
        try:
            trades = self._request(
                "GET",
                "/api/v3/myTrades",
                {"symbol": sym, "orderId": int(order_id), "limit": 1000},
                signed=True,
                max_retries=1,
            )
        except Exception:
            return 0.0, 0.0, 0.0
        if not isinstance(trades, list):
            return 0.0, 0.0, 0.0

        fee_quote = 0.0
        qty_sum = 0.0
        quote_sum = 0.0
        for tr in trades:
            if not isinstance(tr, dict):
                continue
            try:
                qty = float(tr.get("qty", "0") or 0.0)
            except Exception:
                qty = 0.0
            try:
                px = float(tr.get("price", "0") or 0.0)
            except Exception:
                px = 0.0
            try:
                quote_qty = float(tr.get("quoteQty", "0") or 0.0)
            except Exception:
                quote_qty = 0.0
            if quote_qty <= 0.0 and qty > 0.0 and px > 0.0:
                quote_qty = qty * px

            try:
                comm = float(tr.get("commission", "0") or 0.0)
            except Exception:
                comm = 0.0
            comm_asset = str(tr.get("commissionAsset", "")).upper()
            fee_quote += self.commission_to_quote(
                symbol=sym,
                commission=comm,
                commission_asset=comm_asset,
                trade_price=px,
            )
            qty_sum += max(0.0, qty)
            quote_sum += max(0.0, quote_qty)
        return max(0.0, fee_quote), max(0.0, qty_sum), max(0.0, quote_sum)

    def _order_fee_quote(self, order: Dict[str, Any]) -> float:
        fee_quote, _, _ = self._order_trade_summary(order)
        return fee_quote

    def my_trades(
        self,
        *,
        symbol: Optional[str] = None,
        limit: int = 1000,
        from_id: Optional[int] = None,
    ) -> list[Dict[str, Any]]:
        sym = str(symbol or self.symbol).upper()
        params: Dict[str, Any] = {
            "symbol": sym,
            "limit": max(1, min(1000, int(limit))),
        }
        if from_id is not None:
            params["fromId"] = int(from_id)
        resp = self._request(
            "GET",
            "/api/v3/myTrades",
            params,
            signed=True,
            max_retries=1,
        )
        if not isinstance(resp, list):
            return []
        return [item for item in resp if isinstance(item, dict)]

    def my_trades_window(
        self,
        *,
        symbol: Optional[str] = None,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
        from_id: Optional[int] = None,
    ) -> list[Dict[str, Any]]:
        sym = str(symbol or self.symbol).upper()
        params: Dict[str, Any] = {
            "symbol": sym,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": max(1, min(1000, int(limit))),
        }
        if from_id is not None:
            params["fromId"] = int(from_id)
        resp = self._request(
            "GET",
            "/api/v3/myTrades",
            params,
            signed=True,
            max_retries=1,
        )
        if not isinstance(resp, list):
            return []
        return [item for item in resp if isinstance(item, dict)]

    def add_order(
        self,
        pair: str,
        side: str,
        order_type: str,
        volume: str,
        price: Optional[str] = None,
        cl_ord_id: Optional[str] = None,
        post_only: bool = False,
        validate: bool = False,
    ) -> Dict[str, Any]:
        symbol = to_binance_symbol(pair or self.symbol)
        ot = str(order_type or "market").lower()
        try:
            qty_raw = Decimal(str(volume))
        except Exception:
            raise KrakenAPIError([f"EBINANCE:INVALID_QTY:{volume}"])
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": str(side).upper(),
            "quantity": self._normalize_qty(symbol, qty_raw, ot),
        }

        if ot == "market":
            params["type"] = "MARKET"
        else:
            if price is None:
                raise KrakenAPIError(["EBINANCE:INVALID_ORDER:limit order requires price"]) 
            try:
                px_raw = Decimal(str(price))
            except Exception:
                raise KrakenAPIError([f"EBINANCE:INVALID_PRICE:{price}"])
            params["price"] = self._normalize_price(symbol, px_raw)
            if post_only:
                params["type"] = "LIMIT_MAKER"
            else:
                params["type"] = "LIMIT"
                params["timeInForce"] = "GTC"

        if cl_ord_id:
            params["newClientOrderId"] = str(cl_ord_id)[:36]

        if validate:
            self._request("POST", "/api/v3/order/test", params, signed=True)
            oid = str(cl_ord_id or f"test_{int(time.time()*1000)}")
            return {"txid": [oid]}

        resp = self._request("POST", "/api/v3/order", params, signed=True)
        if not isinstance(resp, dict):
            raise KrakenTransientError(["EBINANCE:invalid_add_order_response"])
        oid = str(resp.get("orderId", ""))
        if not oid:
            oid = str(resp.get("clientOrderId", ""))
        return {"txid": [oid] if oid else [], "raw": resp}

    def cancel_all(self) -> Dict[str, Any]:
        symbol = to_binance_symbol(self.symbol)
        try:
            resp = self._request("DELETE", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
            count = len(resp) if isinstance(resp, list) else 0
            return {"count": count}
        except KrakenAPIError as exc:
            # Binance returns -2011 "Unknown order sent." when there are no open orders.
            # Treat this as a successful no-op cancel-all.
            if any("EBINANCE:-2011" in str(err) for err in getattr(exc, "errors", [])):
                return {"count": 0}
            raise

    def cancel_all_orders_after(self, timeout: int) -> Dict[str, Any]:
        # Binance spot has no native dead-man-switch equivalent.
        if int(timeout) <= 0:
            return self.cancel_all()
        return {"timeout": int(timeout), "supported": False}

    def start_user_data_stream(self) -> str:
        doc = self._request(
            "POST",
            "/api/v3/userDataStream",
            signed=False,
            api_key=True,
            max_retries=1,
        )
        if not isinstance(doc, dict):
            raise KrakenTransientError(["EBINANCE:listen_key_invalid_response"])
        key = str(doc.get("listenKey", "")).strip()
        if not key:
            raise KrakenTransientError(["EBINANCE:listen_key_missing"])
        return key

    def keepalive_user_data_stream(self, listen_key: str) -> None:
        key = str(listen_key or "").strip()
        if not key:
            return
        self._request(
            "PUT",
            "/api/v3/userDataStream",
            {"listenKey": key},
            signed=False,
            api_key=True,
            max_retries=1,
        )

    def close_user_data_stream(self, listen_key: str) -> None:
        key = str(listen_key or "").strip()
        if not key:
            return
        try:
            self._request(
                "DELETE",
                "/api/v3/userDataStream",
                {"listenKey": key},
                signed=False,
                api_key=True,
                max_retries=0,
            )
        except Exception:
            return

    def open_orders(self) -> Dict[str, Any]:
        symbol = to_binance_symbol(self.symbol)
        resp = self._request("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)
        open_map: Dict[str, Any] = {}
        if isinstance(resp, list):
            for item in resp:
                if not isinstance(item, dict):
                    continue
                oid = str(item.get("orderId", ""))
                if not oid:
                    continue
                open_map[oid] = self._order_payload(item, include_fee=False)
        return {"open": open_map}

    def balance(self) -> Dict[str, Any]:
        doc = self._request("GET", "/api/v3/account", signed=True)
        out: Dict[str, Any] = {}
        if not isinstance(doc, dict):
            return out
        balances = doc.get("balances")
        if not isinstance(balances, list):
            return out
        for row in balances:
            if not isinstance(row, dict):
                continue
            asset = str(row.get("asset", "")).strip().upper()
            if not asset:
                continue
            try:
                free = float(row.get("free", "0") or 0.0)
                locked = float(row.get("locked", "0") or 0.0)
            except Exception:
                continue
            out[asset] = {
                "free": free,
                "locked": locked,
                "total": free + locked,
            }
        return out

    def query_orders(self, txid: str) -> Dict[str, Any]:
        symbol = to_binance_symbol(self.symbol)
        ids = [p.strip() for p in str(txid or "").split(",") if p.strip()]
        out: Dict[str, Any] = {}
        for oid in ids:
            params: Dict[str, Any] = {"symbol": symbol}
            if oid.isdigit():
                params["orderId"] = int(oid)
            else:
                params["origClientOrderId"] = oid
            order = self._request("GET", "/api/v3/order", params, signed=True)
            if not isinstance(order, dict):
                continue
            key = str(order.get("orderId", oid))
            out[key] = self._order_payload(order, include_fee=True)
        return out

    def cancel_order(self, txid: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        symbol = to_binance_symbol(self.symbol)
        params: Dict[str, Any] = {"symbol": symbol}
        if txid:
            params["orderId"] = int(txid) if str(txid).isdigit() else str(txid)
        if cl_ord_id:
            params["origClientOrderId"] = str(cl_ord_id)
        return self._request("DELETE", "/api/v3/order", params, signed=True)
