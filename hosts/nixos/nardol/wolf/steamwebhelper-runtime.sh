#!/bin/sh
set -eu

# Keep the runtime hook as a pass-through for existing Steam containers.
# Zink caused CEF GPU process crashes on this NVIDIA/Xwayland stack.
exec "$HOME/.steam/steamrt64/pv-runtime/steam-runtime-steamrt/_v2-entry-point" "$@"
