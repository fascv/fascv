from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict

from trading.kraken.rest import KrakenRestClient, KrakenAPIError
from trading.utils.env import load_env


def _nonzero_only(bal: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in bal.items():
        try:
            if float(v) != 0.0:
                out[k] = v
        except Exception:
            out[k] = v
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Kraken account balances (assets) via REST /private/Balance.")
    ap.add_argument("--env", default="../../.env", help="Path to .env with KRAKEN_API_KEY/KRAKEN_API_SECRET.")
    ap.add_argument("--nonzero", action="store_true", help="Show only non-zero balances.")
    args = ap.parse_args()

    load_env(args.env)
    key = os.getenv("KRAKEN_API_KEY", "")
    secret = os.getenv("KRAKEN_API_SECRET", "")
    if not key or not secret:
        raise SystemExit("missing KRAKEN_API_KEY/KRAKEN_API_SECRET in env")

    client = KrakenRestClient(api_key=key, api_secret=secret, timeout=10.0)
    try:
        bal = client.balance()
    except KrakenAPIError as exc:
        # Do not leak secrets; print only Kraken error strings.
        raise SystemExit(f"kraken_error:{';'.join(exc.errors)}")

    if args.nonzero:
        bal = _nonzero_only(bal)
    print(json.dumps(bal, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

