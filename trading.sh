#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Resolve a project Python that has runtime deps (PyYAML + FastAPI stack).
python_has_runtime_deps() {
  local py="$1"
  "$py" - <<'PY' >/dev/null 2>&1
import fastapi  # noqa: F401
import uvicorn  # noqa: F401
import yaml  # noqa: F401
PY
}

PY_BIN=""
for candidate in ".venv/bin/python" "modules/impact_console/.venv/bin/python" "python3"; do
  if [[ "$candidate" == "python3" || -x "$candidate" ]]; then
    if python_has_runtime_deps "$candidate"; then
      PY_BIN="$candidate"
      break
    fi
  fi
done

if [[ -z "$PY_BIN" ]]; then
  echo "ERROR: no usable Python found (requires: pyyaml, fastapi, uvicorn)." >&2
  exit 3
fi

MODE="${MODE:-paper}"
CONFIG="${CONFIG:-configs/paper.yaml}"

# Track env overrides so we can safely apply config-driven defaults without breaking overrides.
CONTROL_HOST_ENV_SET=0; [[ -n "${CONTROL_HOST+x}" ]] && CONTROL_HOST_ENV_SET=1
CONTROL_PORT_ENV_SET=0; [[ -n "${CONTROL_PORT+x}" ]] && CONTROL_PORT_ENV_SET=1
IMPACT_HOST_ENV_SET=0; [[ -n "${IMPACT_HOST+x}" ]] && IMPACT_HOST_ENV_SET=1
IMPACT_PORT_ENV_SET=0; [[ -n "${IMPACT_PORT+x}" ]] && IMPACT_PORT_ENV_SET=1
JOURNAL_GUI_JSON_PATH_ENV_SET=0; [[ -n "${JOURNAL_GUI_JSON_PATH+x}" ]] && JOURNAL_GUI_JSON_PATH_ENV_SET=1
JOURNAL_GUI_DB_PATH_ENV_SET=0; [[ -n "${JOURNAL_GUI_DB_PATH+x}" ]] && JOURNAL_GUI_DB_PATH_ENV_SET=1
START_IMPACT_CONSOLE_ENV_SET=0; [[ -n "${START_IMPACT_CONSOLE+x}" ]] && START_IMPACT_CONSOLE_ENV_SET=1

CONTROL_HOST="${CONTROL_HOST-}"
CONTROL_PORT="${CONTROL_PORT-}"

IMPACT_HOST="${IMPACT_HOST-}"
IMPACT_PORT="${IMPACT_PORT-}"

EXEC_HOST="${EXEC_HOST:-${EXEC_GUI_HOST:-127.0.0.1}}"
EXEC_PORT="${EXEC_PORT:-${EXEC_GUI_PORT:-8110}}"

JOURNAL_GUI_HOST="${JOURNAL_GUI_HOST:-127.0.0.1}"
JOURNAL_GUI_PORT="${JOURNAL_GUI_PORT:-8120}"
JOURNAL_GUI_JSON_PATH="${JOURNAL_GUI_JSON_PATH-}"
JOURNAL_GUI_DB_PATH="${JOURNAL_GUI_DB_PATH-}"

CORE_GUI_HOST="${CORE_GUI_HOST:-127.0.0.1}"
CORE_GUI_PORT="${CORE_GUI_PORT:-8130}"

MD_GUI_HOST="${MD_GUI_HOST:-127.0.0.1}"
MD_GUI_PORT="${MD_GUI_PORT:-8140}"

START_IMPACT_CONSOLE="${START_IMPACT_CONSOLE-}"
START_EXEC="${START_EXEC:-${START_EXEC_GUI:-1}}"
if [[ -z "${START_JOURNAL_GUI+x}" ]]; then
  # Default off for simulation: journal is still written to disk, but the extra UI adds clutter.
  if [[ "${MODE}" == "sim" ]]; then START_JOURNAL_GUI="0"; else START_JOURNAL_GUI="1"; fi
fi
START_CORE_GUI="${START_CORE_GUI:-1}"
START_MD_GUI="${START_MD_GUI:-1}"
IMPACT_AUTO_COLLECT="${IMPACT_AUTO_COLLECT:-1}"
IMPACT_COLLECT_INTERVAL_SECONDS="${IMPACT_COLLECT_INTERVAL_SECONDS:-300}"

cfg_dump="$("$PY_BIN" - "$CONFIG" <<'PY' 2>/dev/null || true
import sys
from urllib.parse import urlparse

from trading.config import load_config

path = sys.argv[1]
cfg = load_config(path).raw

def get(p, default=None):
    cur = cfg
    for part in p.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

control_host = get("control.host", "")
control_port = get("control.port", "")

journal_json_path = get("journal.json_path", get("journal.path", ""))
journal_db_path = get("journal.db_path", "")

impact_enabled = bool(get("impact.enabled", False))
impact_api_url = str(get("impact.api_url", "") or "")

impact_host = ""
impact_port = ""
try:
    u = urlparse(impact_api_url) if impact_api_url else None
    if u and u.scheme in {"http", "https"} and u.hostname:
        impact_host = u.hostname or ""
        if u.port is not None:
            impact_port = str(int(u.port))
except Exception:
    pass

print(str(control_host or ""))
print(str(control_port or ""))
print(str(journal_json_path or ""))
print(str(journal_db_path or ""))
print("1" if impact_enabled else "0")
print(impact_api_url)
print(impact_host)
print(impact_port)
PY
)"

cfg_control_host=""
cfg_control_port=""
cfg_journal_json_path=""
cfg_journal_db_path=""
cfg_impact_enabled="0"
cfg_impact_api_url=""
cfg_impact_host=""
cfg_impact_port=""

if [[ -n "$cfg_dump" ]]; then
  mapfile -t _cfg_lines <<<"$cfg_dump"
  cfg_control_host="${_cfg_lines[0]:-}"
  cfg_control_port="${_cfg_lines[1]:-}"
  cfg_journal_json_path="${_cfg_lines[2]:-}"
  cfg_journal_db_path="${_cfg_lines[3]:-}"
  cfg_impact_enabled="${_cfg_lines[4]:-0}"
  cfg_impact_api_url="${_cfg_lines[5]:-}"
  cfg_impact_host="${_cfg_lines[6]:-}"
  cfg_impact_port="${_cfg_lines[7]:-}"
fi

CONTROL_HOST_DEFAULT="127.0.0.1"
CONTROL_PORT_DEFAULT="8000"
IMPACT_HOST_DEFAULT="127.0.0.1"
IMPACT_PORT_DEFAULT="8011"
JOURNAL_GUI_JSON_PATH_DEFAULT="logs/journal_events.jsonl"
JOURNAL_GUI_DB_PATH_DEFAULT="logs/journal.db"

if (( CONTROL_HOST_ENV_SET == 0 )); then
  CONTROL_HOST="${cfg_control_host:-$CONTROL_HOST_DEFAULT}"
fi
if (( CONTROL_PORT_ENV_SET == 0 )); then
  CONTROL_PORT="${cfg_control_port:-$CONTROL_PORT_DEFAULT}"
fi

if (( JOURNAL_GUI_JSON_PATH_ENV_SET == 0 )); then
  JOURNAL_GUI_JSON_PATH="${cfg_journal_json_path:-$JOURNAL_GUI_JSON_PATH_DEFAULT}"
fi
if (( JOURNAL_GUI_DB_PATH_ENV_SET == 0 )); then
  JOURNAL_GUI_DB_PATH="${cfg_journal_db_path:-$JOURNAL_GUI_DB_PATH_DEFAULT}"
fi

if (( START_IMPACT_CONSOLE_ENV_SET == 0 )); then
  # Keep Impact visible by default in paper mode for monitoring, even when
  # impact ingestion is disabled for trading decisions.
  if [[ "${cfg_impact_enabled}" == "1" || "${MODE}" == "paper" ]]; then
    START_IMPACT_CONSOLE="1"
  else
    START_IMPACT_CONSOLE="0"
  fi
fi

if (( IMPACT_HOST_ENV_SET == 0 )); then
  IMPACT_HOST="${cfg_impact_host:-$IMPACT_HOST_DEFAULT}"
fi
if (( IMPACT_PORT_ENV_SET == 0 )); then
  IMPACT_PORT="${cfg_impact_port:-$IMPACT_PORT_DEFAULT}"
fi

is_port_free() {
  local host="$1"
  local port="$2"
  "$PY_BIN" - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((host, port))
except OSError:
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

need_free_port() {
  local name="$1"
  local host="$2"
  local port="$3"
  if ! is_port_free "$host" "$port"; then
    echo "ERROR: ${name} port in use: ${host}:${port}" >&2
    exit 2
  fi
}

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT INT TERM

need_free_port "control" "$CONTROL_HOST" "$CONTROL_PORT"
if [[ "$START_IMPACT_CONSOLE" == "1" ]]; then
  # If the impact console is already running, reuse it instead of failing hard.
  if ! is_port_free "$IMPACT_HOST" "$IMPACT_PORT"; then
    if curl -fsS -m 0.5 "http://$IMPACT_HOST:$IMPACT_PORT/" >/dev/null 2>&1; then
      IMPACT_CONSOLE_REUSED=1
      START_IMPACT_CONSOLE="0"
    else
      echo "ERROR: impact_console port in use: ${IMPACT_HOST}:${IMPACT_PORT}" >&2
      exit 2
    fi
  fi
fi
if [[ "$START_EXEC" == "1" ]]; then
  need_free_port "exec" "$EXEC_HOST" "$EXEC_PORT"
fi
if [[ "$START_JOURNAL_GUI" == "1" ]]; then
  need_free_port "journal_gui" "$JOURNAL_GUI_HOST" "$JOURNAL_GUI_PORT"
fi
if [[ "$START_CORE_GUI" == "1" ]]; then
  need_free_port "core_gui" "$CORE_GUI_HOST" "$CORE_GUI_PORT"
fi
if [[ "$START_MD_GUI" == "1" ]]; then
  need_free_port "md_gui" "$MD_GUI_HOST" "$MD_GUI_PORT"
fi

wait_http() {
  local url="$1"
  local label="$2"
  local deadline_s="${3:-10}"
  local start_ts
  start_ts="$(date +%s)"
  while true; do
    if curl -fsS -m 0.5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    local now_ts
    now_ts="$(date +%s)"
    if (( now_ts - start_ts >= deadline_s )); then
      echo "ERROR: ${label} did not become ready: ${url}" >&2
      return 1
    fi
    sleep 0.2
  done
}

if [[ "$START_IMPACT_CONSOLE" == "1" ]]; then
  (
    cd modules/impact_console
    # Prefer impact_console's venv if present. Avoid relying on "activate" since
    # environments can get moved/renamed and console scripts may bake paths.
    IMPACT_PY_BIN="python3"
    if [[ -x ".venv/bin/python" ]]; then
      IMPACT_PY_BIN=".venv/bin/python"
    fi
    # Load OpenAI key etc when present, but args control the binding port.
    if [[ -f ".env" ]]; then
      set -a
      # shellcheck disable=SC1091
      source .env
      set +a
    fi
    "$IMPACT_PY_BIN" -m btc_news_arrow.cli serve --host "$IMPACT_HOST" --port "$IMPACT_PORT"
  ) &
  pids+=("$!")
  wait_http "http://$IMPACT_HOST:$IMPACT_PORT/" "impact_console" 10
fi

if [[ "$IMPACT_AUTO_COLLECT" == "1" ]]; then
  if [[ "$START_IMPACT_CONSOLE" == "1" || "${IMPACT_CONSOLE_REUSED:-0}" == "1" ]]; then
    (
      IMPACT_COLLECT_INTERVAL_SECONDS="$IMPACT_COLLECT_INTERVAL_SECONDS" \
        "$ROOT_DIR/scripts/impact_collect_loop.sh" >/dev/null 2>&1
    ) &
    pids+=("$!")
  fi
fi

if [[ "$START_JOURNAL_GUI" == "1" ]]; then
  (
    export PYTHONPATH="$ROOT_DIR/modules/journal/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PY_BIN" -m trading_journal.gui \
      --host "$JOURNAL_GUI_HOST" \
      --port "$JOURNAL_GUI_PORT" \
      --json-path "$JOURNAL_GUI_JSON_PATH" \
      --db-path "$JOURNAL_GUI_DB_PATH" \
      --lines 400 \
      --max-bytes 2000000
  ) &
  pids+=("$!")
  wait_http "http://$JOURNAL_GUI_HOST:$JOURNAL_GUI_PORT/" "journal_gui" 10
fi

export IMPACT_URL="http://$IMPACT_HOST:$IMPACT_PORT/"
export IMPACT_CONSOLE_URL="http://$IMPACT_HOST:$IMPACT_PORT/"
export EXEC_URL="http://$EXEC_HOST:$EXEC_PORT/"
export EXEC_GUI_URL="http://$EXEC_HOST:$EXEC_PORT/"
export JOURNAL_URL="http://$JOURNAL_GUI_HOST:$JOURNAL_GUI_PORT/"
export JOURNAL_GUI_URL="http://$JOURNAL_GUI_HOST:$JOURNAL_GUI_PORT/"
export CORE_URL="http://$CORE_GUI_HOST:$CORE_GUI_PORT/"
export CORE_GUI_URL="http://$CORE_GUI_HOST:$CORE_GUI_PORT/"
export MD_URL="http://$MD_GUI_HOST:$MD_GUI_PORT/"
export MD_GUI_URL="http://$MD_GUI_HOST:$MD_GUI_PORT/"

echo "Control GUI:      http://$CONTROL_HOST:$CONTROL_PORT/"
if [[ "${IMPACT_CONSOLE_REUSED:-0}" == "1" ]]; then
  echo "Impact Console:   http://$IMPACT_HOST:$IMPACT_PORT/ (already running)"
elif [[ "$START_IMPACT_CONSOLE" == "1" ]]; then
  echo "Impact Console:   http://$IMPACT_HOST:$IMPACT_PORT/"
else
  echo "Impact Console:   (disabled: START_IMPACT_CONSOLE=0)"
fi
if [[ "$START_EXEC" == "1" ]]; then
  echo "Exec:             http://$EXEC_HOST:$EXEC_PORT/"
else
  echo "Exec:             (disabled: START_EXEC=0)"
fi
if [[ "$START_JOURNAL_GUI" == "1" ]]; then
  echo "Journal GUI:      http://$JOURNAL_GUI_HOST:$JOURNAL_GUI_PORT/"
else
  echo "Journal GUI:      (disabled: START_JOURNAL_GUI=0)"
fi
if [[ "$START_CORE_GUI" == "1" ]]; then
  echo "Core GUI:         http://$CORE_GUI_HOST:$CORE_GUI_PORT/"
else
  echo "Core GUI:         (disabled: START_CORE_GUI=0)"
fi
if [[ "$START_MD_GUI" == "1" ]]; then
  echo "MD GUI:           http://$MD_GUI_HOST:$MD_GUI_PORT/"
else
  echo "MD GUI:           (disabled: START_MD_GUI=0)"
fi
echo
"$PY_BIN" -m trading.run_gui \
  --mode "$MODE" \
  --config "$CONFIG" \
  --host "$CONTROL_HOST" \
  --port "$CONTROL_PORT" &
control_pid="$!"
pids+=("$control_pid")
wait_http "http://$CONTROL_HOST:$CONTROL_PORT/health" "control" 10

if [[ "$START_CORE_GUI" == "1" ]]; then
  (
    export PYTHONPATH="$ROOT_DIR/modules/core/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PY_BIN" -m trading_core.gui \
      --host "$CORE_GUI_HOST" \
      --port "$CORE_GUI_PORT" \
      --control-url "http://$CONTROL_HOST:$CONTROL_PORT" \
      --timeout-sec 2.0 \
      --journal-lines 800
  ) &
  pids+=("$!")
  wait_http "http://$CORE_GUI_HOST:$CORE_GUI_PORT/" "core_gui" 10
fi

if [[ "$START_MD_GUI" == "1" ]]; then
  (
    export PYTHONPATH="$ROOT_DIR/modules/md/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PY_BIN" -m trading_md.gui \
      --host "$MD_GUI_HOST" \
      --port "$MD_GUI_PORT" \
      --control-url "http://$CONTROL_HOST:$CONTROL_PORT" \
      --timeout-sec 2.0 \
      --journal-lines 1200
  ) &
  pids+=("$!")
  wait_http "http://$MD_GUI_HOST:$MD_GUI_PORT/" "md_gui" 10
fi

if [[ "$START_EXEC" == "1" ]]; then
  (
    export PYTHONPATH="$ROOT_DIR/modules/exec/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PY_BIN" -m trading_exec.gui \
      --host "$EXEC_HOST" \
      --port "$EXEC_PORT" \
      --control-url "http://$CONTROL_HOST:$CONTROL_PORT" \
      --timeout-sec 2.0 \
      --journal-lines 500
  ) &
  pids+=("$!")
  wait_http "http://$EXEC_HOST:$EXEC_PORT/" "exec" 10
fi

wait "$control_pid"
exit $?
