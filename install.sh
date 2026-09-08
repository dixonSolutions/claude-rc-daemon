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
loginctl enable-linger "$USER" 2>/dev/null || true   # start at boot, not only at login
# Accept workspace trust for every project found now, so the first rollout does not stall.
# Later folders are handled by auto_trust = true in the config.
"$BIN/claude-rc-daemon" --trust --yes
echo "installed. config: $CFG/config.toml   status: claude-rc-daemon --status"
