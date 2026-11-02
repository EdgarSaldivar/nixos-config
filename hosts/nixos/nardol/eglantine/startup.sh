#!/usr/bin/env bash
set -Eeuo pipefail

# /entrypoint.sh runs GoW cont-init as root, then gosu's the app user before
# /opt/gow/startup.sh invokes this script. Keep Wolf's Sway session lifecycle.
test "$(id -u)" = 1000
export HOME=/home/retro
export XDG_CONFIG_HOME="${HOME}/.config"
export XDG_DATA_HOME="${HOME}/.local/share"
config="${HOME}/.config/eden/qt-config.ini"
mkdir -p "$(dirname "${config}")" "${HOME}/Games/Switch"
if ! test -e "${config}"; then
  printf '[UI]\ncheck_for_updates=false\ncheck_for_updates\\default=false\n' > "${config}"
elif grep -q '^check_for_updates=' "${config}"; then
  sed -i -E 's/^check_for_updates=.*/check_for_updates=false/' "${config}"
elif grep -q '^\[UI\]$' "${config}"; then
  sed -i '/^\[UI\]$/a check_for_updates=false' "${config}"
else
  printf '\n[UI]\ncheck_for_updates=false\ncheck_for_updates\\default=false\n' >> "${config}"
fi

# QSettings ignores a value while its companion default flag remains true.
if grep -q '^check_for_updates\\default=' "${config}"; then
  sed -i 's/^check_for_updates\\default=.*/check_for_updates\\default=false/' "${config}"
else
  sed -i '/^\[UI\]$/a check_for_updates\\default=false' "${config}"
fi

export RUN_GAMESCOPE="${RUN_GAMESCOPE:-}"
export RUN_SWAY="${RUN_SWAY:-true}"
source /opt/gow/launch-comp.sh
launcher /usr/local/bin/nardol-eglantine
