#!/usr/bin/env bash
# Install or update the user service for this checkout (path-independent). Rebuilds the bundles only
# when the build tools are installed; the committed frontend/app.js is used otherwise.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -x node_modules/.bin/esbuild ]] && npm run build
chmod +x scripts/start.sh
unit="$HOME/.config/systemd/user/omarchy-remote.service"
mkdir -p "$(dirname "$unit")"
rm -f "$unit"   # older installs linked the repository unit, which assumed ~/Projects/omarchy-remote
sed "s|%h/Projects/omarchy-remote/web|$PWD|g" systemd/omarchy-remote.service > "$unit"
systemctl --user import-environment WAYLAND_DISPLAY HYPRLAND_INSTANCE_SIGNATURE XDG_RUNTIME_DIR
systemctl --user daemon-reload
systemctl --user enable omarchy-remote.service
# restart, not start: a running service must pick up the new build and unit path.
systemctl --user restart omarchy-remote.service
