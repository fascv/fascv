#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source .env
  set +a
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# Auto-detect actual running API port if a server process exists.
if [[ -z "${TARGET_URL:-}" ]]; then
  DETECTED_PORT="$(pgrep -af 'btcnews serve --host' | sed -n 's/.*--port \([0-9]\+\).*/\1/p' | tail -n 1 || true)"
  if [[ -n "${DETECTED_PORT}" ]]; then
    PORT="${DETECTED_PORT}"
  fi
  TARGET_URL="http://${HOST}:${PORT}"
fi

if [[ -x "${ROOT_DIR}/tools/bin/cloudflared" ]]; then
  CLOUDFLARED="${ROOT_DIR}/tools/bin/cloudflared"
elif command -v cloudflared >/dev/null 2>&1; then
  CLOUDFLARED="$(command -v cloudflared)"
else
  echo "cloudflared not found. Expected at tools/bin/cloudflared" >&2
  exit 1
fi

echo "Starting Cloudflare Quick Tunnel -> ${TARGET_URL}"
echo "Press CTRL+C to stop."

if command -v curl >/dev/null 2>&1; then
  if ! curl -fsS -m 4 "${TARGET_URL}/" >/dev/null 2>&1; then
    echo "Local target is not reachable: ${TARGET_URL}" >&2
    echo "Start the app first (./start.sh) or pass explicit target:" >&2
    echo "  TARGET_URL=http://127.0.0.1:<PORT> ./start_tunnel.sh" >&2
    exit 1
  fi
fi

exec "$CLOUDFLARED" tunnel --url "$TARGET_URL"
