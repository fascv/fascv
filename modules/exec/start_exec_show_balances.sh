#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_PATH="${EXEC_BAL_ENV:-${SCRIPT_DIR}/../../.env}"
NONZERO="${EXEC_BAL_NONZERO:-1}"

export PYTHONPATH="${SCRIPT_DIR}/src:${SCRIPT_DIR}/../..${PYTHONPATH:+:${PYTHONPATH}}"

ARGS=(--env "${ENV_PATH}")
if [[ "${NONZERO}" == "1" || "${NONZERO}" == "true" ]]; then
  ARGS+=(--nonzero)
fi

exec python3 -m trading_exec.show_balances "${ARGS[@]}" "$@"

