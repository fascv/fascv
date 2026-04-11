#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PY_BIN=".venv/bin/python"
if [[ ! -x "$PY_BIN" ]]; then
  echo "Missing virtualenv python at .venv/bin/python"
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -e ."
  exit 1
fi

"$PY_BIN" - <<'PY'
from pathlib import Path
import yaml
import feedparser
import requests

cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
feeds = cfg.get("feeds", [])
http_cfg = cfg.get("http", {}) if isinstance(cfg, dict) else {}
user_agent = str(http_cfg.get("user_agent", "btc-news-arrow/1.0 (contact: you@example.com)")).strip()
if not user_agent:
    user_agent = "btc-news-arrow/1.0 (contact: you@example.com)"
headers = {
    "User-Agent": user_agent,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

if not feeds:
    print("No feeds configured.")
    raise SystemExit(0)

for feed in feeds:
    name = str(feed.get("name", "unknown"))
    url = str(feed.get("url", ""))
    try:
        resp = requests.get(url, headers=headers, timeout=25)
        status = int(resp.status_code)
        ctype = str(resp.headers.get("content-type", "")).split(";")[0]
        parsed = feedparser.parse(resp.content)
        entries = list(getattr(parsed, "entries", []))
        bozo = bool(getattr(parsed, "bozo", False))
        exc = getattr(parsed, "bozo_exception", None)
        if status >= 400:
            print(f"{name:16} ERROR (HTTP {status}, content-type={ctype})\n  {url}")
            continue
        if bozo and not entries:
            msg = f"ERROR ({exc})" if exc else "ERROR"
            print(f"{name:16} {msg} (HTTP {status}, content-type={ctype})\n  {url}")
            continue
        newest = entries[0].get("title", "-") if entries else "-"
        published = (
            entries[0].get("published")
            or entries[0].get("updated")
            or entries[0].get("pubDate")
            or "-"
        ) if entries else "-"
        print(
            f"{name:16} OK HTTP={status} entries={len(entries)} latest_ts='{published}' newest='{newest[:120]}'\n  {url}"
        )
    except Exception as err:
        print(f"{name:16} ERROR ({err})\n  {url}")
PY
