#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG="${EXEC_SMOKE_CONFIG:-${SCRIPT_DIR}/../../configs/live.yaml}"
ENV_PATH="${EXEC_SMOKE_ENV:-${SCRIPT_DIR}/../../.env}"
QTY_BTC="${EXEC_SMOKE_QTY_BTC:-0.00005}"
TIMEOUT_SEC="${EXEC_SMOKE_TIMEOUT_SEC:-30}"
CANARY="${EXEC_SMOKE_CANARY:-0}"

export PYTHONPATH="${SCRIPT_DIR}/src:${SCRIPT_DIR}/../..${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=(--config "${CONFIG}" --env "${ENV_PATH}" --qty-btc "${QTY_BTC}" --timeout-sec "${TIMEOUT_SEC}" --mode live)
if [[ "${CANARY}" == "1" || "${CANARY}" == "true" ]]; then
  ARGS+=(--canary)
fi

echo "Exec live smoke: config=${CONFIG} env=${ENV_PATH} qty_btc=${QTY_BTC} timeout_sec=${TIMEOUT_SEC} canary=${CANARY}"
exec python3 -m trading_exec.smoke_live "${ARGS[@]}" "$@"

