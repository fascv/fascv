#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="codex-trader-live.service"
ROTATION_UNIT="codex-rotation-selector.service"

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

if systemctl --user is-active --quiet "$UNIT_NAME"; then
  systemctl --user status --no-pager --lines=40 "$UNIT_NAME"
  exit 0
fi

if systemctl --user is-active --quiet "$ROTATION_UNIT"; then
  echo "$UNIT_NAME is inactive, but rotation mode is active via $ROTATION_UNIT."
  running_rotation="$(systemctl --user --no-pager --type=service --state=running | rg -c 'codex-rotation-')"
  echo "running rotation services: ${running_rotation}"
  systemctl --user status --no-pager --lines=30 "$ROTATION_UNIT"
  exit 0
fi

systemctl --user status --no-pager --lines=40 "$UNIT_NAME"
