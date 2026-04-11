from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class KrakenAPIError(RuntimeError):
    def __init__(self, errors: list[str], payload: Optional[Dict[str, Any]] = None):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.payload = payload or {}

    def is_rate_limit(self) -> bool:
        if isinstance(self, KrakenRateLimitError):
            return True
        rate_errors = {
            "EAPI:Rate limit exceeded",
            "EService: Throttled",
            "EService:Unavailable",
        }
        return any(err in rate_errors for err in self.errors)

    def is_auth(self) -> bool:
        if isinstance(self, KrakenAuthError):
            return True
        auth_errors = {
            "EAPI:Invalid key",
            "EAPI:Invalid signature",
            "EGeneral:Permission denied",
            "EGeneral:Permission denied:Invalid key",
        }
        return any(err in auth_errors for err in self.errors)

    def is_transient(self) -> bool:
        return isinstance(self, KrakenTransientError)


class KrakenRateLimitError(KrakenAPIError):
    pass


class KrakenAuthError(KrakenAPIError):
    pass


class KrakenTransientError(KrakenAPIError):
    pass


@dataclass
class KrakenRestClient:
    api_key: str
    api_secret: str
    base_url: str = "https://api.kraken.com"
    api_version: str = "/0"
    timeout: float = 10.0
    http_post: Optional[Any] = None
    max_retries: int = 2
    retry_backoff_sec: float = 0.25
    retry_max_backoff_sec: float = 2.0

    # Kraken requires a strictly increasing nonce per API key.
    _nonce_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _nonce_last: int = field(default=0, init=False, repr=False)

    @staticmethod
    def _norm_num_str(value: str) -> str:
        # Kraken endpoints often reject scientific notation.
        # Example: "5e-05" -> "0.00005".
        try:
            d = Decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return value
        text = format(d, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    def _nonce(self) -> str:
        now = int(time.time() * 1000)
        with self._nonce_lock:
            if now <= self._nonce_last:
                now = self._nonce_last + 1
            self._nonce_last = now
            return str(now)

    def _sign(self, urlpath: str, data: Dict[str, Any]) -> str:
        postdata = urllib.parse.urlencode(data)
        encoded = (data["nonce"] + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _decode_payload(self, raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, bytes):
            return json.loads(raw.decode("utf-8"))
        if isinstance(raw, str):
            return json.loads(raw)
        raise KrakenTransientError([f"ENetwork:Unexpected response type {type(raw)!r}"])

    def _classify_api_error(self, errors: list[str], payload: Optional[Dict[str, Any]] = None) -> KrakenAPIError:
        if any("Rate limit exceeded" in err or "Throttled" in err for err in errors):
            return KrakenRateLimitError(errors, payload)
        if any("Invalid key" in err or "Invalid signature" in err or "Permission denied" in err for err in errors):
            return KrakenAuthError(errors, payload)
        return KrakenAPIError(errors, payload)

    def _request_once(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
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

        try:
            if self.http_post is not None:
                response = self.http_post(url, headers, payload)
            else:
                req = urllib.request.Request(url, data=payload, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    response = resp.read()
        except urllib.error.HTTPError as exc:
            response_body = b""
            try:
                response_body = exc.read()
            except Exception:
                response_body = b""
            payload_json: Dict[str, Any] = {}
            try:
                payload_json = self._decode_payload(response_body)
            except Exception:
                payload_json = {}
            errors = payload_json.get("error", []) if isinstance(payload_json, dict) else []
            if not errors:
                errors = [f"EHTTP:{exc.code}"]
            if exc.code == 429:
                raise KrakenRateLimitError(errors, payload_json)
            if exc.code in {401, 403}:
                raise KrakenAuthError(errors, payload_json)
            if exc.code in {408, 409, 425} or exc.code >= 500:
                raise KrakenTransientError(errors, payload_json)
            raise KrakenAPIError(errors, payload_json)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise KrakenTransientError([f"ENetwork:{exc}"])

        decoded = self._decode_payload(response)
        errors = decoded.get("error", [])
        if errors:
            raise self._classify_api_error(errors, decoded)
        return decoded.get("result", {})

    def _backoff(self, attempt: int) -> float:
        return min(self.retry_max_backoff_sec, self.retry_backoff_sec * (2 ** attempt))

    def _post_private(
        self,
        path: str,
        data: Dict[str, Any],
        *,
        max_retries: Optional[int] = None,
        retry_on_rate_limit: bool = False,
    ) -> Dict[str, Any]:
        retries = self.max_retries if max_retries is None else max(0, int(max_retries))
        attempt = 0
        while True:
            try:
                return self._request_once(path, data)
            except KrakenRateLimitError:
                if not retry_on_rate_limit or attempt >= retries:
                    raise
            except KrakenTransientError:
                if attempt >= retries:
                    raise
            time.sleep(self._backoff(attempt))
            attempt += 1

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
            "volume": self._norm_num_str(volume),
            "validate": "true" if validate else "false",
        }
        if price is not None:
            data["price"] = self._norm_num_str(price)
        if cl_ord_id:
            data["cl_ord_id"] = cl_ord_id
        if post_only:
            data["oflags"] = "post"
        # Safe retries require client idempotency key.
        retries = self.max_retries if cl_ord_id else 0
        return self._post_private("/private/AddOrder", data, max_retries=retries, retry_on_rate_limit=False)

    def cancel_all(self) -> Dict[str, Any]:
        return self._post_private("/private/CancelAll", {})

    def cancel_all_orders_after(self, timeout: int) -> Dict[str, Any]:
        return self._post_private("/private/CancelAllOrdersAfter", {"timeout": int(timeout)})

    def open_orders(self) -> Dict[str, Any]:
        return self._post_private("/private/OpenOrders", {})

    def balance(self) -> Dict[str, Any]:
        # Returns a mapping like {"ZEUR": "...", "XXBT": "..."}.
        return self._post_private("/private/Balance", {})

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
