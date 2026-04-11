#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
OPENAI_SECRET_ENV_FILE="${ROTATION_OPENAI_SECRET_ENV_FILE:-$HOME/.config/codex/rotation-openai.env}"
TRADING_SECRETS_ENV_FILE="${CODEX_TRADING_SECRETS_ENV:-$HOME/.config/codex/trading-secrets.env}"

load_env_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    return
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|\#*) continue ;;
    esac

    if [[ "$line" == *=* ]]; then
      local key="${line%%=*}"
      local value="${line#*=}"
      key="$(printf '%s' "$key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      [[ -n "$key" ]] || continue
      export "${key}=${value}"
      continue
    fi

    if [[ "$(basename "$path")" == "rotation-openai.env" ]]; then
      export "OPENAI_API_KEY=${line}"
    fi
  done < "$path"
}

# Load repo-local defaults first, then local user secrets.
load_env_file ".env"
load_env_file "$TRADING_SECRETS_ENV_FILE"
load_env_file "$OPENAI_SECRET_ENV_FILE"

# Prefer an explicit venv python if present.
# Note: this repo (and its .venv) has been moved/renamed in the past; relying on
# ".venv/bin/activate" or console scripts can break due to baked-in paths.
PY_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PY_BIN=".venv/bin/python"
fi

CONFIG_PATH="${CONFIG_PATH:-config.yaml}"
CFG_REPORT_HYBRID_REPORT_PATH=""
CFG_REPORT_HYBRID_HISTORY_DIR=""
CFG_REPORT_SOURCE_QUALITY_REPORT_PATH=""
CFG_REPORT_SOURCE_QUALITY_HISTORY_DIR=""
CFG_REPORT_ALERTS_REPORT_PATH=""
CFG_REPORT_ALERTS_HISTORY_DIR=""
CFG_ALERT_FRESHNESS_MINUTES=""
CFG_ALERT_WINDOW_MINUTES=""
CFG_ALERT_MIN_ITEMS=""
CFG_ALERT_SOURCE_CONCENTRATION_THRESHOLD=""
CFG_ALERT_HYBRID_DEGRADED_STREAK=""
CFG_ALERT_SOURCE_QUALITY_DEGRADED_STREAK=""
CFG_ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD=""
CFG_ALERT_FAIL_ON=""
CFG_ALERT_WEBHOOK_ON=""
CFG_ALERT_WEBHOOK_TIMEOUT=""
CFG_ALERT_WEBHOOK_RETRIES=""
CFG_ALERT_WEBHOOK_BACKOFF_SECONDS=""
CFG_ALERT_WEBHOOK_COOLDOWN_SECONDS=""
CFG_ALERT_WEBHOOK_STATE_PATH=""
CFG_ALERT_WEBHOOK_MAX_ALERTS=""

if [[ -f "$CONFIG_PATH" ]] && command -v "$PY_BIN" >/dev/null 2>&1; then
  while IFS='=' read -r key value; do
    case "$key" in
      CFG_*) printf -v "$key" '%s' "$value" ;;
    esac
  done < <(CONFIG_PATH="$CONFIG_PATH" "$PY_BIN" - <<'PY'
import os
import sys

try:
    import yaml
except Exception:
    raise SystemExit(0)

path = os.environ.get("CONFIG_PATH", "config.yaml")
try:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
except Exception:
    raise SystemExit(0)

if not isinstance(cfg, dict):
    raise SystemExit(0)

reports = cfg.get("reports") if isinstance(cfg.get("reports"), dict) else {}
alerts = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
webhook = alerts.get("webhook") if isinstance(alerts.get("webhook"), dict) else {}

def emit(key: str, value) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        out = "1" if value else "0"
    elif isinstance(value, (list, tuple)):
        out = ",".join(str(v) for v in value)
    else:
        out = str(value)
    out = out.replace("\n", " ").strip()
    if not out:
        return
    print(f"{key}={out}")

emit("CFG_REPORT_HYBRID_REPORT_PATH", reports.get("hybrid_report_path"))
emit("CFG_REPORT_HYBRID_HISTORY_DIR", reports.get("hybrid_history_dir"))
emit("CFG_REPORT_SOURCE_QUALITY_REPORT_PATH", reports.get("source_quality_report_path"))
emit("CFG_REPORT_SOURCE_QUALITY_HISTORY_DIR", reports.get("source_quality_history_dir"))
emit("CFG_REPORT_ALERTS_REPORT_PATH", reports.get("alerts_report_path"))
emit("CFG_REPORT_ALERTS_HISTORY_DIR", reports.get("alerts_history_dir"))

emit("CFG_ALERT_FRESHNESS_MINUTES", alerts.get("freshness_minutes"))
emit("CFG_ALERT_WINDOW_MINUTES", alerts.get("window_minutes"))
emit("CFG_ALERT_MIN_ITEMS", alerts.get("min_items"))
emit("CFG_ALERT_SOURCE_CONCENTRATION_THRESHOLD", alerts.get("source_concentration_threshold"))
emit("CFG_ALERT_HYBRID_DEGRADED_STREAK", alerts.get("hybrid_degraded_streak"))
emit("CFG_ALERT_SOURCE_QUALITY_DEGRADED_STREAK", alerts.get("source_quality_degraded_streak"))
emit("CFG_ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD", alerts.get("source_quality_corr_drop_threshold"))
emit("CFG_ALERT_FAIL_ON", alerts.get("fail_on"))

emit("CFG_ALERT_WEBHOOK_ON", webhook.get("on"))
emit("CFG_ALERT_WEBHOOK_TIMEOUT", webhook.get("timeout_seconds"))
emit("CFG_ALERT_WEBHOOK_RETRIES", webhook.get("retries"))
emit("CFG_ALERT_WEBHOOK_BACKOFF_SECONDS", webhook.get("backoff_seconds"))
emit("CFG_ALERT_WEBHOOK_COOLDOWN_SECONDS", webhook.get("cooldown_seconds"))
emit("CFG_ALERT_WEBHOOK_STATE_PATH", webhook.get("state_path"))
emit("CFG_ALERT_WEBHOOK_MAX_ALERTS", webhook.get("max_alerts"))
PY
)
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8010}"
AUTO_COLLECT="${AUTO_COLLECT:-1}"
AUTO_RESCORE="${AUTO_RESCORE:-1}"
AUTO_LEARN_UPDATE="${AUTO_LEARN_UPDATE:-1}"
AUTO_LLM_OPTIMIZE="${AUTO_LLM_OPTIMIZE:-0}"
LLM_OPT_CYCLES="${LLM_OPT_CYCLES:-1}"
LLM_OPT_SLEEP="${LLM_OPT_SLEEP:-0}"
LLM_OPT_WINDOWS="${LLM_OPT_WINDOWS:-1h,24h}"
AUTO_LOOP="${AUTO_LOOP:-1}"
UPDATE_INTERVAL_SECONDS="${UPDATE_INTERVAL_SECONDS:-600}"
AUTO_HYBRID_OPTIMIZE="${AUTO_HYBRID_OPTIMIZE:-1}"
HYBRID_OPT_INTERVAL_SECONDS="${HYBRID_OPT_INTERVAL_SECONDS:-21600}"
HYBRID_OPT_WINDOWS="${HYBRID_OPT_WINDOWS:-1h,24h}"
HYBRID_OPT_LOOKBACK_DAYS="${HYBRID_OPT_LOOKBACK_DAYS:-7}"
HYBRID_OPT_MIN_SAMPLES="${HYBRID_OPT_MIN_SAMPLES:-40}"
HYBRID_OPT_GRID_STEP="${HYBRID_OPT_GRID_STEP:-0.05}"
AUTO_BASELINE_CALIBRATE="${AUTO_BASELINE_CALIBRATE:-1}"
BASELINE_CAL_INTERVAL_SECONDS="${BASELINE_CAL_INTERVAL_SECONDS:-86400}"
BASELINE_CAL_OUTPUT="${BASELINE_CAL_OUTPUT:-tools/regression_baseline.json}"
BASELINE_CAL_RUNS="${BASELINE_CAL_RUNS:-3}"
BASELINE_CAL_INCLUDE_HYBRID_EVAL="${BASELINE_CAL_INCLUDE_HYBRID_EVAL:-1}"
BASELINE_CAL_WINDOWS="${BASELINE_CAL_WINDOWS:-1h,24h}"
BASELINE_CAL_LOOKBACK_DAYS="${BASELINE_CAL_LOOKBACK_DAYS:-30}"
BASELINE_CAL_MIN_SAMPLES="${BASELINE_CAL_MIN_SAMPLES:-120}"
BASELINE_CAL_CORR_MARGIN="${BASELINE_CAL_CORR_MARGIN:-0.05}"
BASELINE_CAL_DIRECTIONAL_MARGIN="${BASELINE_CAL_DIRECTIONAL_MARGIN:-0.05}"
AUTO_HYBRID_EVAL_REPORT="${AUTO_HYBRID_EVAL_REPORT:-1}"
HYBRID_EVAL_INTERVAL_SECONDS="${HYBRID_EVAL_INTERVAL_SECONDS:-21600}"
HYBRID_EVAL_WINDOWS="${HYBRID_EVAL_WINDOWS:-1h,24h}"
HYBRID_EVAL_LOOKBACK_DAYS="${HYBRID_EVAL_LOOKBACK_DAYS:-30}"
HYBRID_EVAL_MIN_SAMPLES="${HYBRID_EVAL_MIN_SAMPLES:-120}"
HYBRID_EVAL_REPORT_PATH="${HYBRID_EVAL_REPORT_PATH:-${CFG_REPORT_HYBRID_REPORT_PATH:-diagnostics/hybrid_eval_latest.json}}"
HYBRID_EVAL_HISTORY_DIR="${HYBRID_EVAL_HISTORY_DIR:-${CFG_REPORT_HYBRID_HISTORY_DIR:-diagnostics/hybrid_eval_history}}"
HYBRID_EVAL_KEEP_HISTORY="${HYBRID_EVAL_KEEP_HISTORY:-1}"
AUTO_SOURCE_QUALITY_REPORT="${AUTO_SOURCE_QUALITY_REPORT:-1}"
SOURCE_QUALITY_INTERVAL_SECONDS="${SOURCE_QUALITY_INTERVAL_SECONDS:-21600}"
SOURCE_QUALITY_WINDOWS="${SOURCE_QUALITY_WINDOWS:-1h,24h}"
SOURCE_QUALITY_LOOKBACK_DAYS="${SOURCE_QUALITY_LOOKBACK_DAYS:-30}"
SOURCE_QUALITY_MIN_SAMPLES_PER_SOURCE="${SOURCE_QUALITY_MIN_SAMPLES_PER_SOURCE:-8}"
SOURCE_QUALITY_TOP_N="${SOURCE_QUALITY_TOP_N:-25}"
SOURCE_QUALITY_REPORT_PATH="${SOURCE_QUALITY_REPORT_PATH:-${CFG_REPORT_SOURCE_QUALITY_REPORT_PATH:-diagnostics/source_quality_latest.json}}"
SOURCE_QUALITY_HISTORY_DIR="${SOURCE_QUALITY_HISTORY_DIR:-${CFG_REPORT_SOURCE_QUALITY_HISTORY_DIR:-diagnostics/source_quality_history}}"
SOURCE_QUALITY_KEEP_HISTORY="${SOURCE_QUALITY_KEEP_HISTORY:-1}"
AUTO_ALERT_CHECK="${AUTO_ALERT_CHECK:-1}"
ALERT_INTERVAL_SECONDS="${ALERT_INTERVAL_SECONDS:-600}"
ALERT_FRESHNESS_MINUTES="${ALERT_FRESHNESS_MINUTES:-${CFG_ALERT_FRESHNESS_MINUTES:-90}}"
ALERT_WINDOW_MINUTES="${ALERT_WINDOW_MINUTES:-${CFG_ALERT_WINDOW_MINUTES:-180}}"
ALERT_MIN_ITEMS="${ALERT_MIN_ITEMS:-${CFG_ALERT_MIN_ITEMS:-3}}"
ALERT_SOURCE_CONCENTRATION_THRESHOLD="${ALERT_SOURCE_CONCENTRATION_THRESHOLD:-${CFG_ALERT_SOURCE_CONCENTRATION_THRESHOLD:-0.90}}"
ALERT_HYBRID_DEGRADED_STREAK="${ALERT_HYBRID_DEGRADED_STREAK:-${CFG_ALERT_HYBRID_DEGRADED_STREAK:-3}}"
ALERT_SOURCE_QUALITY_DEGRADED_STREAK="${ALERT_SOURCE_QUALITY_DEGRADED_STREAK:-${CFG_ALERT_SOURCE_QUALITY_DEGRADED_STREAK:-3}}"
ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD="${ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD:-${CFG_ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD:-0.08}}"
ALERT_FAIL_ON="${ALERT_FAIL_ON:-${CFG_ALERT_FAIL_ON:-critical,high}}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
ALERT_WEBHOOK_ON="${ALERT_WEBHOOK_ON:-${CFG_ALERT_WEBHOOK_ON:-critical,high,quality}}"
ALERT_WEBHOOK_TIMEOUT="${ALERT_WEBHOOK_TIMEOUT:-${CFG_ALERT_WEBHOOK_TIMEOUT:-5}}"
ALERT_WEBHOOK_RETRIES="${ALERT_WEBHOOK_RETRIES:-${CFG_ALERT_WEBHOOK_RETRIES:-3}}"
ALERT_WEBHOOK_BACKOFF_SECONDS="${ALERT_WEBHOOK_BACKOFF_SECONDS:-${CFG_ALERT_WEBHOOK_BACKOFF_SECONDS:-2}}"
ALERT_WEBHOOK_COOLDOWN_SECONDS="${ALERT_WEBHOOK_COOLDOWN_SECONDS:-${CFG_ALERT_WEBHOOK_COOLDOWN_SECONDS:-900}}"
ALERT_WEBHOOK_STATE_PATH="${ALERT_WEBHOOK_STATE_PATH:-${CFG_ALERT_WEBHOOK_STATE_PATH:-diagnostics/webhook_state.json}}"
ALERT_WEBHOOK_MAX_ALERTS="${ALERT_WEBHOOK_MAX_ALERTS:-${CFG_ALERT_WEBHOOK_MAX_ALERTS:-10}}"
ALERT_OUTPUT_PATH="${ALERT_OUTPUT_PATH:-${CFG_REPORT_ALERTS_REPORT_PATH:-diagnostics/alerts_latest.json}}"
ALERT_HISTORY_DIR="${ALERT_HISTORY_DIR:-${CFG_REPORT_ALERTS_HISTORY_DIR:-diagnostics/alerts_history}}"
ALERT_KEEP_HISTORY="${ALERT_KEEP_HISTORY:-1}"
MAX_PORT_SCAN="${MAX_PORT_SCAN:-30}"
REQUIRE_LLM_RUNTIME="${REQUIRE_LLM_RUNTIME:-1}"

RUNNER=("$PY_BIN" -m btc_news_arrow.cli)

echo "Starting btc_news_arrow (base URL http://${HOST}:${PORT})"

if [[ "$REQUIRE_LLM_RUNTIME" == "1" ]]; then
  if [[ -z "${OPENAI_API_KEY:-}" ]] || [[ "${OPENAI_API_KEY}" == "DEIN_NEUER_OPENAI_KEY" ]]; then
    echo "LLM runtime required: set OPENAI_API_KEY in .env" >&2
    exit 1
  fi

  if ! "$PY_BIN" -c "import openai" >/dev/null 2>&1; then
    echo "LLM runtime required: install openai package (pip install openai)" >&2
    exit 1
  fi
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS "http://${HOST}:${PORT}/arrow?window=1h" >/dev/null 2>&1; then
    echo "Server already running on http://${HOST}:${PORT} (nothing to start)."
    exit 0
  fi
fi

if [[ "$AUTO_COLLECT" == "1" ]]; then
  echo "Running initial collect..."
  "${RUNNER[@]}" collect || true
fi

if [[ "$AUTO_RESCORE" == "1" ]]; then
  echo "Running rule rescore..."
  "${RUNNER[@]}" rescore || true
fi

if [[ "$AUTO_LEARN_UPDATE" == "1" ]]; then
  echo "Running learner update..."
  "${RUNNER[@]}" learn-update || true
fi

if [[ "$AUTO_LLM_OPTIMIZE" == "1" ]]; then
  echo "Running LLM auto-optimizer (cycles=${LLM_OPT_CYCLES}, windows=${LLM_OPT_WINDOWS})..."
  "${RUNNER[@]}" llm-optimize --cycles "$LLM_OPT_CYCLES" --sleep "$LLM_OPT_SLEEP" --windows "$LLM_OPT_WINDOWS" || true
fi

if [[ "$AUTO_HYBRID_OPTIMIZE" == "1" ]]; then
  echo "Running initial hybrid optimizer (windows=${HYBRID_OPT_WINDOWS}, lookback=${HYBRID_OPT_LOOKBACK_DAYS}d)..."
  "${RUNNER[@]}" hybrid-optimize \
    --windows "$HYBRID_OPT_WINDOWS" \
    --lookback-days "$HYBRID_OPT_LOOKBACK_DAYS" \
    --min-samples "$HYBRID_OPT_MIN_SAMPLES" \
    --grid-step "$HYBRID_OPT_GRID_STEP" || true
fi

if [[ "$AUTO_BASELINE_CALIBRATE" == "1" ]]; then
  echo "Running initial regression baseline recalibration (runs=${BASELINE_CAL_RUNS})..."
  CAL_ARGS=(
    baseline-calibrate
    --output "$BASELINE_CAL_OUTPUT"
    --runs "$BASELINE_CAL_RUNS"
    --overwrite
  )
  if [[ "$BASELINE_CAL_INCLUDE_HYBRID_EVAL" == "1" ]]; then
    CAL_ARGS+=(
      --include-hybrid-eval
      --hybrid-windows "$BASELINE_CAL_WINDOWS"
      --hybrid-lookback-days "$BASELINE_CAL_LOOKBACK_DAYS"
      --hybrid-min-samples "$BASELINE_CAL_MIN_SAMPLES"
      --hybrid-corr-margin "$BASELINE_CAL_CORR_MARGIN"
      --hybrid-directional-margin "$BASELINE_CAL_DIRECTIONAL_MARGIN"
    )
  fi
  "${RUNNER[@]}" "${CAL_ARGS[@]}" || true
fi

if [[ "$AUTO_HYBRID_EVAL_REPORT" == "1" ]]; then
  echo "Running initial hybrid trend report..."
  REPORT_ARGS=(
    hybrid-report
    --windows "$HYBRID_EVAL_WINDOWS"
    --lookback-days "$HYBRID_EVAL_LOOKBACK_DAYS"
    --min-samples "$HYBRID_EVAL_MIN_SAMPLES"
    --report-path "$HYBRID_EVAL_REPORT_PATH"
    --history-dir "$HYBRID_EVAL_HISTORY_DIR"
  )
  if [[ "$HYBRID_EVAL_KEEP_HISTORY" != "1" ]]; then
    REPORT_ARGS+=(--no-history)
  fi
  "${RUNNER[@]}" "${REPORT_ARGS[@]}" || true
fi

if [[ "$AUTO_SOURCE_QUALITY_REPORT" == "1" ]]; then
  echo "Running initial source quality report..."
  SRC_QUALITY_ARGS=(
    source-quality-report
    --windows "$SOURCE_QUALITY_WINDOWS"
    --lookback-days "$SOURCE_QUALITY_LOOKBACK_DAYS"
    --min-samples-per-source "$SOURCE_QUALITY_MIN_SAMPLES_PER_SOURCE"
    --top-n "$SOURCE_QUALITY_TOP_N"
    --report-path "$SOURCE_QUALITY_REPORT_PATH"
    --history-dir "$SOURCE_QUALITY_HISTORY_DIR"
  )
  if [[ "$SOURCE_QUALITY_KEEP_HISTORY" != "1" ]]; then
    SRC_QUALITY_ARGS+=(--no-history)
  fi
  "${RUNNER[@]}" "${SRC_QUALITY_ARGS[@]}" || true
fi

if [[ "$AUTO_ALERT_CHECK" == "1" ]]; then
  echo "Running initial alerts check..."
  ALERT_ARGS=(
    alerts-check
    --hybrid-report-path "$HYBRID_EVAL_REPORT_PATH"
    --hybrid-history-dir "$HYBRID_EVAL_HISTORY_DIR"
    --source-quality-report-path "$SOURCE_QUALITY_REPORT_PATH"
    --source-quality-history-dir "$SOURCE_QUALITY_HISTORY_DIR"
    --freshness-minutes "$ALERT_FRESHNESS_MINUTES"
    --window-minutes "$ALERT_WINDOW_MINUTES"
    --min-items "$ALERT_MIN_ITEMS"
    --source-concentration-threshold "$ALERT_SOURCE_CONCENTRATION_THRESHOLD"
    --hybrid-degraded-streak "$ALERT_HYBRID_DEGRADED_STREAK"
    --source-quality-degraded-streak "$ALERT_SOURCE_QUALITY_DEGRADED_STREAK"
    --source-quality-corr-drop-threshold "$ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD"
    --output-path "$ALERT_OUTPUT_PATH"
    --history-dir "$ALERT_HISTORY_DIR"
    --fail-on "$ALERT_FAIL_ON"
    --webhook-on "$ALERT_WEBHOOK_ON"
    --webhook-timeout "$ALERT_WEBHOOK_TIMEOUT"
    --webhook-retries "$ALERT_WEBHOOK_RETRIES"
    --webhook-backoff-seconds "$ALERT_WEBHOOK_BACKOFF_SECONDS"
    --webhook-cooldown-seconds "$ALERT_WEBHOOK_COOLDOWN_SECONDS"
    --webhook-state-path "$ALERT_WEBHOOK_STATE_PATH"
    --webhook-max-alerts "$ALERT_WEBHOOK_MAX_ALERTS"
  )
  if [[ "$ALERT_KEEP_HISTORY" != "1" ]]; then
    ALERT_ARGS+=(--no-history)
  fi
  if [[ -n "$ALERT_WEBHOOK_URL" ]]; then
    ALERT_ARGS+=(--webhook-url "$ALERT_WEBHOOK_URL")
  fi
  "${RUNNER[@]}" "${ALERT_ARGS[@]}" || true
fi

if [[ "$AUTO_LOOP" == "1" ]]; then
  echo "Starting background auto-update loop (every ${UPDATE_INTERVAL_SECONDS}s)"
  NEXT_HYBRID_OPT_TS=0
  NEXT_BASELINE_CAL_TS=0
  NEXT_HYBRID_EVAL_TS=0
  NEXT_SOURCE_QUALITY_TS=0
  NEXT_ALERT_TS=0
  NOW_TS="$(date +%s)"
  if [[ "$AUTO_HYBRID_OPTIMIZE" == "1" ]]; then
    NEXT_HYBRID_OPT_TS=$((NOW_TS + HYBRID_OPT_INTERVAL_SECONDS))
  fi
  if [[ "$AUTO_BASELINE_CALIBRATE" == "1" ]]; then
    NEXT_BASELINE_CAL_TS=$((NOW_TS + BASELINE_CAL_INTERVAL_SECONDS))
  fi
  if [[ "$AUTO_HYBRID_EVAL_REPORT" == "1" ]]; then
    NEXT_HYBRID_EVAL_TS=$((NOW_TS + HYBRID_EVAL_INTERVAL_SECONDS))
  fi
  if [[ "$AUTO_SOURCE_QUALITY_REPORT" == "1" ]]; then
    NEXT_SOURCE_QUALITY_TS=$((NOW_TS + SOURCE_QUALITY_INTERVAL_SECONDS))
  fi
  if [[ "$AUTO_ALERT_CHECK" == "1" ]]; then
    NEXT_ALERT_TS=$((NOW_TS + ALERT_INTERVAL_SECONDS))
  fi
  (
    while true; do
      "${RUNNER[@]}" collect || true
      "${RUNNER[@]}" rescore || true
      "${RUNNER[@]}" learn-update || true
      NOW_TS="$(date +%s)"
      if [[ "$AUTO_HYBRID_OPTIMIZE" == "1" ]]; then
        if (( NOW_TS >= NEXT_HYBRID_OPT_TS )); then
          "${RUNNER[@]}" hybrid-optimize \
            --windows "$HYBRID_OPT_WINDOWS" \
            --lookback-days "$HYBRID_OPT_LOOKBACK_DAYS" \
            --min-samples "$HYBRID_OPT_MIN_SAMPLES" \
            --grid-step "$HYBRID_OPT_GRID_STEP" || true
          NEXT_HYBRID_OPT_TS=$((NOW_TS + HYBRID_OPT_INTERVAL_SECONDS))
        fi
      fi
      if [[ "$AUTO_BASELINE_CALIBRATE" == "1" ]]; then
        if (( NOW_TS >= NEXT_BASELINE_CAL_TS )); then
          CAL_ARGS=(
            baseline-calibrate
            --output "$BASELINE_CAL_OUTPUT"
            --runs "$BASELINE_CAL_RUNS"
            --overwrite
          )
          if [[ "$BASELINE_CAL_INCLUDE_HYBRID_EVAL" == "1" ]]; then
            CAL_ARGS+=(
              --include-hybrid-eval
              --hybrid-windows "$BASELINE_CAL_WINDOWS"
              --hybrid-lookback-days "$BASELINE_CAL_LOOKBACK_DAYS"
              --hybrid-min-samples "$BASELINE_CAL_MIN_SAMPLES"
              --hybrid-corr-margin "$BASELINE_CAL_CORR_MARGIN"
              --hybrid-directional-margin "$BASELINE_CAL_DIRECTIONAL_MARGIN"
            )
          fi
          "${RUNNER[@]}" "${CAL_ARGS[@]}" || true
          NEXT_BASELINE_CAL_TS=$((NOW_TS + BASELINE_CAL_INTERVAL_SECONDS))
        fi
      fi
      if [[ "$AUTO_HYBRID_EVAL_REPORT" == "1" ]]; then
        if (( NOW_TS >= NEXT_HYBRID_EVAL_TS )); then
          REPORT_ARGS=(
            hybrid-report
            --windows "$HYBRID_EVAL_WINDOWS"
            --lookback-days "$HYBRID_EVAL_LOOKBACK_DAYS"
            --min-samples "$HYBRID_EVAL_MIN_SAMPLES"
            --report-path "$HYBRID_EVAL_REPORT_PATH"
            --history-dir "$HYBRID_EVAL_HISTORY_DIR"
          )
          if [[ "$HYBRID_EVAL_KEEP_HISTORY" != "1" ]]; then
            REPORT_ARGS+=(--no-history)
          fi
          "${RUNNER[@]}" "${REPORT_ARGS[@]}" || true
          NEXT_HYBRID_EVAL_TS=$((NOW_TS + HYBRID_EVAL_INTERVAL_SECONDS))
        fi
      fi
      if [[ "$AUTO_SOURCE_QUALITY_REPORT" == "1" ]]; then
        if (( NOW_TS >= NEXT_SOURCE_QUALITY_TS )); then
          SRC_QUALITY_ARGS=(
            source-quality-report
            --windows "$SOURCE_QUALITY_WINDOWS"
            --lookback-days "$SOURCE_QUALITY_LOOKBACK_DAYS"
            --min-samples-per-source "$SOURCE_QUALITY_MIN_SAMPLES_PER_SOURCE"
            --top-n "$SOURCE_QUALITY_TOP_N"
            --report-path "$SOURCE_QUALITY_REPORT_PATH"
            --history-dir "$SOURCE_QUALITY_HISTORY_DIR"
          )
          if [[ "$SOURCE_QUALITY_KEEP_HISTORY" != "1" ]]; then
            SRC_QUALITY_ARGS+=(--no-history)
          fi
          "${RUNNER[@]}" "${SRC_QUALITY_ARGS[@]}" || true
          NEXT_SOURCE_QUALITY_TS=$((NOW_TS + SOURCE_QUALITY_INTERVAL_SECONDS))
        fi
      fi
      if [[ "$AUTO_ALERT_CHECK" == "1" ]]; then
        if (( NOW_TS >= NEXT_ALERT_TS )); then
          ALERT_ARGS=(
            alerts-check
            --hybrid-report-path "$HYBRID_EVAL_REPORT_PATH"
            --hybrid-history-dir "$HYBRID_EVAL_HISTORY_DIR"
            --source-quality-report-path "$SOURCE_QUALITY_REPORT_PATH"
            --source-quality-history-dir "$SOURCE_QUALITY_HISTORY_DIR"
            --freshness-minutes "$ALERT_FRESHNESS_MINUTES"
            --window-minutes "$ALERT_WINDOW_MINUTES"
            --min-items "$ALERT_MIN_ITEMS"
            --source-concentration-threshold "$ALERT_SOURCE_CONCENTRATION_THRESHOLD"
            --hybrid-degraded-streak "$ALERT_HYBRID_DEGRADED_STREAK"
            --source-quality-degraded-streak "$ALERT_SOURCE_QUALITY_DEGRADED_STREAK"
            --source-quality-corr-drop-threshold "$ALERT_SOURCE_QUALITY_CORR_DROP_THRESHOLD"
            --output-path "$ALERT_OUTPUT_PATH"
            --history-dir "$ALERT_HISTORY_DIR"
            --fail-on "$ALERT_FAIL_ON"
            --webhook-on "$ALERT_WEBHOOK_ON"
            --webhook-timeout "$ALERT_WEBHOOK_TIMEOUT"
            --webhook-retries "$ALERT_WEBHOOK_RETRIES"
            --webhook-backoff-seconds "$ALERT_WEBHOOK_BACKOFF_SECONDS"
            --webhook-cooldown-seconds "$ALERT_WEBHOOK_COOLDOWN_SECONDS"
            --webhook-state-path "$ALERT_WEBHOOK_STATE_PATH"
            --webhook-max-alerts "$ALERT_WEBHOOK_MAX_ALERTS"
          )
          if [[ "$ALERT_KEEP_HISTORY" != "1" ]]; then
            ALERT_ARGS+=(--no-history)
          fi
          if [[ -n "$ALERT_WEBHOOK_URL" ]]; then
            ALERT_ARGS+=(--webhook-url "$ALERT_WEBHOOK_URL")
          fi
          "${RUNNER[@]}" "${ALERT_ARGS[@]}" || true
          NEXT_ALERT_TS=$((NOW_TS + ALERT_INTERVAL_SECONDS))
        fi
      fi
      sleep "$UPDATE_INTERVAL_SECONDS"
    done
  ) &
  LOOP_PID=$!
  cleanup() {
    if [[ -n "${LOOP_PID:-}" ]]; then
      kill "$LOOP_PID" >/dev/null 2>&1 || true
    fi
  }
  trap cleanup EXIT INT TERM
fi

for ((i = 0; i <= MAX_PORT_SCAN; i++)); do
  CANDIDATE_PORT=$((PORT + i))
  if [[ "$i" -gt 0 ]]; then
    echo "Previous port unavailable, trying ${CANDIDATE_PORT}..."
  fi
  if "${RUNNER[@]}" serve --host "$HOST" --port "$CANDIDATE_PORT"; then
    exit 0
  fi
done

echo "Could not start server on ports ${PORT}..$((PORT + MAX_PORT_SCAN))." >&2
exit 1
