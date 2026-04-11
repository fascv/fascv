#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="codex-trader-live.service"

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
systemctl --user status --no-pager --lines=40 "$UNIT_NAME"
