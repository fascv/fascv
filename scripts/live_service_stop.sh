#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="codex-trader-live.service"

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

systemctl --user disable --now "$UNIT_NAME"
