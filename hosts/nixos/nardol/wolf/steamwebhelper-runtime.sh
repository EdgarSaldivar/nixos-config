#!/bin/sh
set -eu

# Steam's steamwebhelper.sh alone uses STEAM_RUNTIME_STEAMRT. Set graphics
# overrides here, after the UI process has branched from the game launcher.
export __GLX_VENDOR_LIBRARY_NAME=mesa
export MESA_LOADER_DRIVER_OVERRIDE=zink
export GALLIUM_DRIVER=zink
exec "$HOME/.steam/steamrt64/pv-runtime/steam-runtime-steamrt/_v2-entry-point" "$@"
