#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT_DIR/scripts/systemd/codex-trader-live.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
UNIT_NAME="codex-trader-live.service"

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

mkdir -p "$UNIT_DST_DIR"
cp "$UNIT_SRC" "$UNIT_DST_DIR/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
systemctl --user status --no-pager --lines=40 "$UNIT_NAME"
