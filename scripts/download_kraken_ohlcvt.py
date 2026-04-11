#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

DEFAULT_FILE_ID = "1ptNqWYidLkhb2VAKuLCxmp2OXEfGO-AP"
BASE_URL = "https://drive.google.com/uc"
DOWNLOAD_URL = "https://drive.usercontent.google.com/download"


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(max(0, int(n)))
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024.0
        i += 1
    return f"{x:.2f} {units[i]}"


def _extract_hidden(text: str, name: str) -> str | None:
    m = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', text)
    return m.group(1) if m else None


def _get_download_params(session: requests.Session, file_id: str) -> dict[str, str]:
    r = session.get(BASE_URL, params={"export": "download", "id": file_id}, timeout=60)
    r.raise_for_status()
    text = html.unescape(r.text)

    confirm = _extract_hidden(text, "confirm")
    uuid = _extract_hidden(text, "uuid")
    id_hidden = _extract_hidden(text, "id") or file_id

    params = {"id": id_hidden, "export": "download"}
    if confirm:
        params["confirm"] = confirm
    if uuid:
        params["uuid"] = uuid
    return params


def _probe(session: requests.Session, file_id: str) -> tuple[str, int]:
    params = _get_download_params(session, file_id)
    r = session.get(DOWNLOAD_URL, params=params, stream=True, timeout=60)
    r.raise_for_status()
    cdisp = r.headers.get("content-disposition", "")
    m = re.search(r'filename="([^"]+)"', cdisp)
    filename = m.group(1) if m else "Kraken_OHLCVT.zip"
    total = int(r.headers.get("content-length") or 0)
    r.close()
    return filename, total


def _download(file_id: str, out_path: Path, chunk_kb: int, max_retries: int) -> None:
    part = out_path.with_suffix(out_path.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    filename, total = _probe(session, file_id)
    if out_path.name == "":
        out_path = out_path.parent / filename
        part = out_path.with_suffix(out_path.suffix + ".part")

    print(f"target: {out_path}")
    print(f"size:   {_human_bytes(total)}")

    attempt = 0
    while True:
        current = part.stat().st_size if part.exists() else 0
        if total > 0 and current >= total:
            break
        if attempt >= max_retries:
            raise RuntimeError(f"download incomplete after {attempt} attempts: {current} < {total}")
        attempt += 1

        params = _get_download_params(session, file_id)
        q = urlencode(params)
        print(f"attempt {attempt}: {DOWNLOAD_URL}?{q}")

        headers = {}
        if current > 0:
            headers["Range"] = f"bytes={current}-"

        r = session.get(DOWNLOAD_URL, params=params, headers=headers, stream=True, timeout=120)
        r.raise_for_status()

        ctype = (r.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            # Token likely stale / warning page returned.
            snippet = r.text[:200].replace("\n", " ")
            print(f"warning: got HTML instead of binary ({snippet})")
            time.sleep(min(15, attempt * 2))
            continue

        if current > 0 and r.status_code != 206:
            # Server didn't honor resume; restart from scratch.
            current = 0
            try:
                part.unlink()
            except Exception:
                pass

        mode = "ab" if current > 0 else "wb"
        downloaded = current
        chunk_size = max(64 * 1024, int(chunk_kb) * 1024)
        start = time.time()
        last_print = 0.0
        before = downloaded

        with open(part, mode) as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_print >= 2.0:
                    elapsed = max(0.001, now - start)
                    speed = (downloaded - before) / elapsed
                    if total > 0:
                        pct = min(100.0, (downloaded / total) * 100.0)
                        eta = (total - downloaded) / speed if speed > 1e-9 else 0.0
                        print(
                            f"{pct:6.2f}%  {_human_bytes(downloaded)}/{_human_bytes(total)}  "
                            f"{_human_bytes(int(speed))}/s  eta {int(eta)}s",
                            flush=True,
                        )
                    else:
                        print(f"downloaded {_human_bytes(downloaded)}  {_human_bytes(int(speed))}/s", flush=True)
                    last_print = now

        # small backoff between resume attempts if needed
        current = part.stat().st_size if part.exists() else 0
        if total > 0 and current < total:
            print(f"partial after attempt {attempt}: {_human_bytes(current)}/{_human_bytes(total)}; resuming...")
            time.sleep(min(20, attempt * 2))

    part.replace(out_path)
    print(f"saved: {out_path} ({_human_bytes(out_path.stat().st_size)})")


def main() -> None:
    p = argparse.ArgumentParser(description="Download Kraken OHLCVT bulk zip from official Google Drive link.")
    p.add_argument("--file-id", default=DEFAULT_FILE_ID)
    p.add_argument("--out", default="data/Kraken_OHLCVT.zip")
    p.add_argument("--chunk-kb", type=int, default=1024)
    p.add_argument("--max-retries", type=int, default=200)
    args = p.parse_args()

    out_path = Path(str(args.out)).resolve()
    _download(
        file_id=str(args.file_id).strip(),
        out_path=out_path,
        chunk_kb=int(args.chunk_kb),
        max_retries=int(args.max_retries),
    )


if __name__ == "__main__":
    main()
