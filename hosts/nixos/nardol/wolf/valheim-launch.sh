#!/usr/bin/env bash
# Steam launch options: /etc/nardol/valheim-launch %command%
# Gale supplies the selected profile's Doorstop arguments through -applaunch.
# Insert BepInEx INSIDE Steam's reaper/runtime command, so the loader is applied
# to the game rather than Steam or pressure-vessel. Vanilla remains unchanged.
set -Eeuo pipefail
args=("$@")
enabled=false
target=""
game_index=""
for ((i=0; i<${#args[@]}; i++)); do
    case "${args[i]}" in
        --doorstop-enabled) enabled="${args[i+1]:-false}" ;;
        --doorstop-target-assembly) target="${args[i+1]:-}" ;;
        */valheim.x86_64|valheim.x86_64) game_index="$i" ;;
    esac
done
if [[ "$enabled" != true && "$enabled" != 1 ]]; then
    exec "$@"
fi
if [[ -z "$game_index" || ! -f "$target" ]]; then
    echo "Modded Valheim requires its native executable and Gale profile preloader" >&2
    exit 1
fi
game_dir="$(cd -- "$(dirname -- "${args[game_index]}")" && pwd)"
script="$game_dir/start_game_bepinex.sh"
if [[ ! -r "$script" || ! -r "$game_dir/doorstop_libs/libdoorstop_x64.so" ]]; then
    echo "BepInEx Linux loader is missing; launch the profile through Gale first" >&2
    exit 1
fi
# start_game_bepinex.sh accepts the executable first and then Gale's arguments.
# Using bash also works when Gale copied the script without its executable bit.
exec "${args[@]:0:game_index}" bash "$script" "${args[@]:game_index}"
