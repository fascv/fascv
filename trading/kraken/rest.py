from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional


class KrakenAPIError(RuntimeError):
    def __init__(self, errors: list[str], payload: Optional[Dict[str, Any]] = None):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.payload = payload or {}

    def is_rate_limit(self) -> bool:
        rate_errors = {
            "EAPI:Rate limit exceeded",
            "EService: Throttled",
            "EService:Unavailable",
        }
        return any(err in rate_errors for err in self.errors)


@dataclass
class KrakenRestClient:
    api_key: str
    api_secret: str
    base_url: str = "https://api.kraken.com"
    api_version: str = "/0"
    timeout: float = 10.0
    http_post: Optional[Any] = None

    def _nonce(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, urlpath: str, data: Dict[str, Any]) -> str:
        postdata = urllib.parse.urlencode(data)
        encoded = (data["nonce"] + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _post_private(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        urlpath = f"{self.api_version}{path}"
        data = dict(data)
        data["nonce"] = self._nonce()
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(urlpath, data),
            "User-Agent": "codex-trader/1.0",
        }
        payload = urllib.parse.urlencode(data).encode()
        url = f"{self.base_url}{urlpath}"

        if self.http_post is not None:
            response = self.http_post(url, headers, payload)
        else:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response = resp.read()

        decoded = json.loads(response)
        errors = decoded.get("error", [])
        if errors:
            raise KrakenAPIError(errors, decoded)
        return decoded.get("result", {})

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
        data: Dict[str, Any] = {
            "pair": pair,
            "type": side,
            "ordertype": order_type,
            "volume": volume,
            "validate": "true" if validate else "false",
        }
        if price is not None:
            data["price"] = price
        if cl_ord_id:
            data["cl_ord_id"] = cl_ord_id
        if post_only:
            data["oflags"] = "post"
        return self._post_private("/private/AddOrder", data)

    def cancel_all(self) -> Dict[str, Any]:
        return self._post_private("/private/CancelAll", {})

    def cancel_all_orders_after(self, timeout: int) -> Dict[str, Any]:
        return self._post_private("/private/CancelAllOrdersAfter", {"timeout": int(timeout)})

    def open_orders(self) -> Dict[str, Any]:
        return self._post_private("/private/OpenOrders", {})

    def query_orders(self, txid: str) -> Dict[str, Any]:
        return self._post_private("/private/QueryOrders", {"txid": txid})

    def cancel_order(self, txid: Optional[str] = None, cl_ord_id: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if txid:
            data["txid"] = txid
        if cl_ord_id:
            data["cl_ord_id"] = cl_ord_id
        return self._post_private("/private/CancelOrder", data)

    def get_ws_token(self) -> str:
        result = self._post_private("/private/GetWebSocketsToken", {})
        return result.get("token", "")
