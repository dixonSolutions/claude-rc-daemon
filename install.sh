#!/usr/bin/env bash
# Link the daemon into ~/.local/bin, seed config if missing, install and enable the user unit.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"; CFG="$HOME/.config/claude-rc-daemon"; UNITS="$HOME/.config/systemd/user"
mkdir -p "$BIN" "$CFG" "$UNITS"
ln -sfn "$HERE/claude-rc-daemon" "$BIN/claude-rc-daemon"
[[ -e "$CFG/config.toml" ]]          || cp "$HERE/config.example.toml"          "$CFG/config.toml"
[[ -e "$CFG/settings.local.json" ]]  || cp "$HERE/settings.local.example.json"  "$CFG/settings.local.json"
cp "$HERE/claude-rc-daemon.service" "$UNITS/claude-rc-daemon.service"
systemctl --user daemon-reload
systemctl --user enable --now claude-rc-daemon.service
echo "installed. edit $CFG/config.toml, then run: claude-rc-daemon --trust"
