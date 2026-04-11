#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="${ROOT}/apps/launch/src:${PYTHONPATH:-}"

cd "${ROOT}"
python3 -m trading_launch "$@"
