#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading.utils.env import load_env


def _utc_now_iso() -> str:
    # OKX expects RFC3339 timestamp with milliseconds and Z suffix.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _truthy(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class OKXClient:
    def __init__(self, *, api_key: str, api_secret: str, passphrase: str, base_url: str, demo: bool) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.demo = demo

    def _sign(self, timestamp: str, method: str, path_with_query: str, body: str) -> str:
        payload = f"{timestamp}{method.upper()}{path_with_query}{body}"
        mac = hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac).decode("utf-8")

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        q = ""
        if params:
            q = urlencode({k: str(v) for k, v in params.items() if v is not None})
        path_with_query = path + (f"?{q}" if q else "")
        body = ""
        ts = _utc_now_iso()
        sign = self._sign(ts, method, path_with_query, body)

        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"

        url = self.base_url + path_with_query
        resp = requests.request(method.upper(), url, headers=headers, timeout=12.0)
        try:
            doc: Dict[str, Any] = resp.json()
        except Exception:
            # Preserve HTTP context for non-JSON upstream failures.
            resp.raise_for_status()
            raise
        if resp.status_code >= 400:
            code = doc.get("code", "")
            msg = doc.get("msg", "")
            raise OKXHTTPError(status=resp.status_code, code=str(code), msg=str(msg), base_url=self.base_url)
        return doc


class OKXHTTPError(RuntimeError):
    def __init__(self, *, status: int, code: str, msg: str, base_url: str) -> None:
        self.status = int(status)
        self.code = str(code)
        self.msg = str(msg)
        self.base_url = str(base_url)
        super().__init__(f"okx_http_{self.status}: code={self.code} msg={self.msg} base_url={self.base_url}")


def _okx_code_ok(doc: Dict[str, Any]) -> bool:
    return str(doc.get("code", "")) == "0"


def _extract_balances(balance_doc: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    data = balance_doc.get("data")
    if not isinstance(data, list) or not data:
        return out
    details = data[0].get("details") if isinstance(data[0], dict) else None
    if not isinstance(details, list):
        return out
    for item in details:
        if not isinstance(item, dict):
            continue
        ccy = str(item.get("ccy", "")).strip().upper()
        if not ccy:
            continue
        try:
            eq = float(item.get("eq", "0") or 0.0)
        except Exception:
            continue
        out[ccy] = eq
    return out


def _base_url_candidates(primary: str) -> list[str]:
    candidates = [primary]
    for item in ("https://eea.okx.com", "https://www.okx.com", "https://us.okx.com"):
        if item not in candidates:
            candidates.append(item)
    return candidates


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only OKX private API check (account + balances + public ticker).")
    ap.add_argument("--env", default=".env", help="Path to .env")
    ap.add_argument("--inst", default="ETH-USDT", help="Spot instrument id, e.g. ETH-USDT")
    ap.add_argument("--base-url", default="", help="Override base URL, default from OKX_BASE_URL or https://www.okx.com")
    ap.add_argument("--demo", action="store_true", help="Force demo mode header x-simulated-trading=1")
    args = ap.parse_args()

    load_env(args.env)

    api_key = os.getenv("OKX_API_KEY", "").strip()
    api_secret = os.getenv("OKX_API_SECRET", "").strip()
    passphrase = os.getenv("OKX_API_PASSPHRASE", "").strip()
    base_url = (args.base_url or os.getenv("OKX_BASE_URL", "https://www.okx.com")).strip() or "https://www.okx.com"
    demo = bool(args.demo or _truthy(os.getenv("OKX_DEMO")))

    if not api_key or not api_secret or not passphrase:
        raise SystemExit("missing OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE in env")

    explicit_base = bool(args.base_url.strip())
    candidates = [base_url] if explicit_base else _base_url_candidates(base_url)
    errors: list[dict[str, Any]] = []

    for candidate in candidates:
        client = OKXClient(
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            base_url=candidate,
            demo=demo,
        )
        try:
            cfg = client._request("GET", "/api/v5/account/config")
            if not _okx_code_ok(cfg):
                raise SystemExit(f"okx_account_config_error: code={cfg.get('code')} msg={cfg.get('msg')}")

            balance = client._request("GET", "/api/v5/account/balance")
            if not _okx_code_ok(balance):
                raise SystemExit(f"okx_balance_error: code={balance.get('code')} msg={balance.get('msg')}")

            ticker = requests.get(
                f"{candidate.rstrip('/')}/api/v5/market/ticker",
                params={"instId": args.inst},
                timeout=12.0,
            )
            ticker.raise_for_status()
            ticker_doc: Dict[str, Any] = ticker.json()

            if str(ticker_doc.get("code", "")) != "0":
                raise SystemExit(f"okx_ticker_error: code={ticker_doc.get('code')} msg={ticker_doc.get('msg')}")
        except OKXHTTPError as exc:
            errors.append({"base_url": candidate, "status": exc.status, "code": exc.code, "msg": exc.msg})
            # Keep trying only on "key not found" when endpoint might be wrong.
            if exc.code != "50119" or explicit_base:
                break
            continue

        account_info = cfg.get("data") if isinstance(cfg.get("data"), list) else []
        account_level = None
        if account_info and isinstance(account_info[0], dict):
            account_level = account_info[0].get("acctLv")

        balances = _extract_balances(balance)
        ticker_data = ticker_doc.get("data") if isinstance(ticker_doc.get("data"), list) else []
        last = None
        ts = None
        if ticker_data and isinstance(ticker_data[0], dict):
            last = ticker_data[0].get("last")
            ts = ticker_data[0].get("ts")

        payload: Dict[str, Any] = {
            "ok": True,
            "exchange": "okx",
            "base_url": candidate,
            "demo": demo,
            "inst": args.inst,
            "account_level": account_level,
            "balances_detected": len(balances),
            "balances_sample": {k: balances[k] for k in sorted(balances.keys())[:12]},
            "ticker_last": last,
            "ticker_ts_ms": ts,
        }
        if candidate != base_url:
            payload["endpoint_autoswitched_from"] = base_url
            payload["hint"] = "Set OKX_BASE_URL to this base_url in .env"

        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return

    if errors:
        last_err = errors[-1]
        raise SystemExit(
            f"okx_auth_failed: status={last_err['status']} code={last_err['code']} msg={last_err['msg']} "
            f"tested_base_urls={[e['base_url'] for e in errors]}"
        )
    raise SystemExit("okx_auth_failed: unknown error")


if __name__ == "__main__":
    main()
