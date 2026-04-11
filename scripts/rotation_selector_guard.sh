#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p logs

PID_FILE="${PID_FILE:-logs/rotation_selector_guard.pid}"
DISABLE_FILE="${DISABLE_FILE:-logs/rotation_selector_guard.disabled}"
GUARD_LOG="${GUARD_LOG:-logs/rotation_selector_guard.log}"
SELECTOR_ENV_FILE="${SELECTOR_ENV_FILE:-$ROOT_DIR/configs/rotation_selector_watch_pool.env}"
SELECTOR_WATCH_RUNTIME_ENV_FILE="${SELECTOR_WATCH_RUNTIME_ENV_FILE:-$ROOT_DIR/configs/rotation_watch_pool_runtime.env}"
SELECTOR_RUNTIME_ENV_FILE="${SELECTOR_RUNTIME_ENV_FILE:-$ROOT_DIR/configs/rotation_meta_runtime.env}"

unset_runtime_overrides() {
  unset ROTATION_PROFILE ROTATION_PROFILE_OVERRIDE ROTATION_STRATEGY_SLOT_PLAN ROTATION_META_RECOMMENDATION_GENERATED_AT ROTATION_META_MODE ROTATION_META_CONFIDENCE ROTATION_META_RISK_MODE ROTATION_META_SLOT_PLAN_REASON ROTATION_META_CANDIDATE_OVERRIDES ROTATION_META_AVOID_SYMBOLS
  unset ROTATION_NEUTRAL_FRACTION_MULT ROTATION_DOWN_FRACTION_MULT
  unset ROTATION_CONT_REBOUND_CONFIRM_BARS ROTATION_CONT_REBOUND_TRIGGER_BPS ROTATION_CONT_PULLBACK_MAX_BPS ROTATION_CONT_MAX_STRUCTURE_RANGE_POS
  unset ROTATION_CONT_STAIRCASE_MIN_SLOPE_MEDIUM_BPS ROTATION_CONT_STAIRCASE_MIN_DRAWDOWN_FROM_PEAK_BPS ROTATION_CONT_STAIRCASE_MAX_CONTEXT_RANGE_POS
  unset ROTATION_SWING_REVERSAL_THRESHOLD_BPS ROTATION_BREAKOUT_TRIGGER_BPS
  unset ROTATION_ENTRY_EDGE_BPS ROTATION_ENTRY_COST_BUFFER_BPS ROTATION_ENTRY_COST_COVERAGE_RATIO ROTATION_ENTRY_COST_ROUNDTRIP_MULTIPLIER ROTATION_ENTRY_MIN_ATR_TO_COST_RATIO
  unset ROTATION_OVERRIDE_MAX_STRUCTURE_RANGE_POS ROTATION_OVERRIDE_MIN_DRAWDOWN_FROM_PEAK_BPS ROTATION_OVERRIDE_MIN_DRAWDOWN_TO_COST_RATIO ROTATION_OVERRIDE_MIN_SLOPE_SHORT_BPS ROTATION_OVERRIDE_MAX_TREND_RETURN_BPS ROTATION_OVERRIDE_MAX_CONTEXT_RANGE_POS
  unset ROTATION_GATE_COST_COVERAGE_RATIO ROTATION_GATE_COST_ROUNDTRIP_MULTIPLIER ROTATION_REENTRY_MIN_MOVE_BPS ROTATION_FAILED_START_MAX_BARS ROTATION_FAILED_START_MIN_REBOUND_BPS ROTATION_FAILED_START_LOSS_BPS ROTATION_MIN_EXIT_PROFIT_BPS ROTATION_TRAILING_ACTIVATION_BPS ROTATION_TRAILING_STOP_BPS ROTATION_CAMPAIGN_HOLD_ENABLED ROTATION_CAMPAIGN_HOLD_MIN_BARS ROTATION_CAMPAIGN_HOLD_MIN_PROFIT_BPS ROTATION_CAMPAIGN_HOLD_MIN_TREND_BPS ROTATION_CAMPAIGN_HOLD_MAX_DRAWDOWN_FROM_PEAK_BPS ROTATION_TIME_BREAK_EVEN_FLOOR_BARS ROTATION_MD_INTERVAL_SECONDS
  for strategy in BREAKOUT STAIRCASE PULLBACK_CONTINUATION BREAKOUT_RETEST CONTINUATION RELATIVE_STRENGTH REBOUND; do
    unset "ROTATION_STRATEGY_WEIGHT_${strategy}" "ROTATION_STRATEGY_ACTION_${strategy}" "ROTATION_STRATEGY_SLOT_TARGET_${strategy}" "ROTATION_STRATEGY_TOP_SYMBOLS_${strategy}"
  done
}

load_runtime_env() {
  local profile_override=""
  local force_ignore_runtime="0"
  local bypass_watch_pool="0"
  if [[ -f "$SELECTOR_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SELECTOR_ENV_FILE"
    set +a
    profile_override="${ROTATION_PROFILE_OVERRIDE:-}"
    force_ignore_runtime="${ROTATION_SELECTOR_IGNORE_META_RUNTIME:-0}"
    bypass_watch_pool="${ROTATION_SELECTOR_BYPASS_WATCH_POOL:-0}"
  fi
  unset ROTATION_WATCH_POOL_GENERATED_AT ROTATION_FIXED_WATCH_SYMBOLS ROTATION_SELECTOR_SYMBOLS
  if [[ -f "$SELECTOR_WATCH_RUNTIME_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$SELECTOR_WATCH_RUNTIME_ENV_FILE"
    set +a
  fi
  if [[ "$force_ignore_runtime" != "1" && "$force_ignore_runtime" != "true" && "$force_ignore_runtime" != "yes" && "$force_ignore_runtime" != "on" ]] && [[ -f "$SELECTOR_RUNTIME_ENV_FILE" ]]; then
    unset_runtime_overrides
    set -a
    # shellcheck disable=SC1090
    source "$SELECTOR_RUNTIME_ENV_FILE"
    set +a
    if [[ -n "${ROTATION_PROFILE_OVERRIDE:-}" ]]; then
      profile_override="${ROTATION_PROFILE_OVERRIDE}"
    fi
    ignore_runtime=0
    if [[ "${ROTATION_META_MODE:-}" == "disabled" ]]; then
      ignore_runtime=1
    elif [[ "${ROTATION_META_MODE:-}" == "fallback" ]]; then
      runtime_has_action=0
      for strategy in BREAKOUT STAIRCASE PULLBACK_CONTINUATION BREAKOUT_RETEST CONTINUATION RELATIVE_STRENGTH REBOUND; do
        action_var="ROTATION_STRATEGY_ACTION_${strategy}"
        action_value="${!action_var:-}"
        if [[ "$action_value" == "primary" || "$action_value" == "secondary" || "$action_value" == "watch" ]]; then
          runtime_has_action=1
          break
        fi
      done
      if [[ "$runtime_has_action" == "0" && -z "${ROTATION_STRATEGY_SLOT_PLAN:-}" && -z "${ROTATION_META_CANDIDATE_OVERRIDES:-}" ]]; then
        ignore_runtime=1
      fi
    fi
    if [[ "$ignore_runtime" == "1" ]]; then
      unset_runtime_overrides
      if [[ -f "$SELECTOR_ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$SELECTOR_ENV_FILE"
        set +a
        profile_override="${ROTATION_PROFILE_OVERRIDE:-}"
      fi
      unset ROTATION_WATCH_POOL_GENERATED_AT ROTATION_FIXED_WATCH_SYMBOLS ROTATION_SELECTOR_SYMBOLS
      if [[ -f "$SELECTOR_WATCH_RUNTIME_ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$SELECTOR_WATCH_RUNTIME_ENV_FILE"
        set +a
      fi
    fi
  fi
  if [[ "$bypass_watch_pool" == "1" || "$bypass_watch_pool" == "true" || "$bypass_watch_pool" == "yes" || "$bypass_watch_pool" == "on" ]]; then
    unset ROTATION_FIXED_WATCH_SYMBOLS ROTATION_SELECTOR_SYMBOLS
  fi
  SELECTOR_INTERVAL_SEC="${ROTATION_SELECTOR_INTERVAL_SEC:-${INTERVAL_SEC:-60}}"
  SELECTOR_TIMEOUT_SEC="${SELECTOR_TIMEOUT_SEC:-240}"
  SWITCH_MARGIN_SCORE="${SWITCH_MARGIN_SCORE:-8}"
  MAX_RETAIN_POSITION_PCT="${MAX_RETAIN_POSITION_PCT:-85}"
  ACTIVE_RETAIN_MIN_SCORE="${ACTIVE_RETAIN_MIN_SCORE:-40}"
  ACTIVE_TOP="${ACTIVE_TOP:-4}"
  WATCH_TOP="${WATCH_TOP:-0}"
  MIN_ACTIVE_MINUTES="${MIN_ACTIVE_MINUTES:-5}"
  APPLY_CHANGES="${APPLY_CHANGES:-1}"
  ROTATION_PROFILE="${ROTATION_PROFILE:-default}"
  if [[ -n "$profile_override" ]]; then
    ROTATION_PROFILE="$profile_override"
  fi
}

load_runtime_env

selector_mode="noapply"
if [[ "$APPLY_CHANGES" == "1" || "$APPLY_CHANGES" == "true" || "$APPLY_CHANGES" == "yes" ]]; then
  selector_mode="apply"
fi

echo "$$" > "$PID_FILE"

stop=0
on_term() {
  stop=1
}
trap on_term INT TERM

ts() { date -Is; }

echo "$(ts) [selector] start interval=${SELECTOR_INTERVAL_SEC}s mode=${selector_mode} profile=${ROTATION_PROFILE} top=${ACTIVE_TOP} watch_top=${WATCH_TOP} min_active_min=${MIN_ACTIVE_MINUTES} retain_min_score=${ACTIVE_RETAIN_MIN_SCORE} timeout=${SELECTOR_TIMEOUT_SEC}s" >> "$GUARD_LOG"

while (( stop == 0 )); do
  load_runtime_env
  if [[ -f "$DISABLE_FILE" ]]; then
    echo "$(ts) [selector] disabled via ${DISABLE_FILE}; waiting" >> "$GUARD_LOG"
    while (( stop == 0 )) && [[ -f "$DISABLE_FILE" ]]; do
      sleep 5
    done
    continue
  fi

  selector_cmd=(
    python3 scripts/rotation_auto_coin_selector.py
    --profile "$ROTATION_PROFILE"
    --top "$ACTIVE_TOP"
    --watch-top "$WATCH_TOP"
    --min-active-minutes "$MIN_ACTIVE_MINUTES"
    --active-retain-min-score "$ACTIVE_RETAIN_MIN_SCORE"
    --switch-margin-score "$SWITCH_MARGIN_SCORE"
    --max-retain-position-pct "$MAX_RETAIN_POSITION_PCT"
  )
  if [[ "$selector_mode" == "apply" ]]; then
    selector_cmd+=(--apply)
  fi

  if timeout "$SELECTOR_TIMEOUT_SEC" "${selector_cmd[@]}" >> "$GUARD_LOG" 2>&1; then
    echo "$(ts) [selector] cycle ok" >> "$GUARD_LOG"
  else
    echo "$(ts) [selector] cycle failed" >> "$GUARD_LOG"
  fi

  for _ in $(seq 1 "$SELECTOR_INTERVAL_SEC"); do
    (( stop != 0 )) && break
    sleep 1
  done
done

echo "$(ts) [selector] stop" >> "$GUARD_LOG"
rm -f "$PID_FILE"
