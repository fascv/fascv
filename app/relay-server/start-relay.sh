#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
SECRETS_ENV="${CODEX_TRADING_SECRETS_ENV:-$HOME/.config/codex/trading-secrets.env}"

if [ ! -f .env ]; then
  echo "ERROR: $SCRIPT_DIR/.env fehlt" >&2
  exit 1
fi

load_env_file() {
  local path="$1"
  if [ ! -f "$path" ]; then
    return
  fi

  # Parse env files robustly (supports special chars in values without shell-eval).
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|\#*) continue ;;
    esac
    if [[ "$line" != *=* ]]; then
      continue
    fi

    key="${line%%=*}"
    value="${line#*=}"
    key="$(printf '%s' "$key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -z "$key" ]; then
      continue
    fi
    export "${key}=${value}"
  done < "$path"
}

load_env_file "./.env"
load_env_file "$SECRETS_ENV"

exec /usr/bin/python3 "$SCRIPT_DIR/relay_server.py"
