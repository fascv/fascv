#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SYMBOL="${SYMBOL:-LINK/EUR}"
TIMEFRAME="${TIMEFRAME:-15m}"
START_DATE="${START_DATE:-2023-01-01}"
MIN_YEARS="${MIN_YEARS:-2.4}"
SYMBOL_TAG="${SYMBOL_TAG:-link_eur}"

OUT_YAML_FINAL="${OUT_YAML_FINAL:-configs/sim_optimized_professional_governed_${SYMBOL_TAG}.yaml}"
OUT_JSON_FINAL="${OUT_JSON_FINAL:-reports/sim_opt_report_professional_governed_${SYMBOL_TAG}.json}"
GOVERNANCE_JSON="${GOVERNANCE_JSON:-reports/professional_governance_summary_${SYMBOL_TAG}.json}"
GOVERNANCE_LOG="${GOVERNANCE_LOG:-reports/professional_governance_runs_${SYMBOL_TAG}.jsonl}"

exec env \
  DB_PATH="${DB_PATH-}" \
  JOBS="${JOBS-}" \
  TRIALS_LIST="${TRIALS_LIST-}" \
  SEEDS="${SEEDS-}" \
  START_TS="${START_TS-}" \
  END_TS="${END_TS-}" \
  FAMILIES="${FAMILIES-}" \
  TOPUP_LOOKBACK_SEC="${TOPUP_LOOKBACK_SEC-}" \
  MIN_YEARS="$MIN_YEARS" \
  SYMBOL="$SYMBOL" \
  TIMEFRAME="$TIMEFRAME" \
  START_DATE="$START_DATE" \
  SYMBOL_TAG="$SYMBOL_TAG" \
  OUT_YAML_FINAL="$OUT_YAML_FINAL" \
  OUT_JSON_FINAL="$OUT_JSON_FINAL" \
  GOVERNANCE_JSON="$GOVERNANCE_JSON" \
  GOVERNANCE_LOG="$GOVERNANCE_LOG" \
  scripts/start_professional_opt_3y.sh
