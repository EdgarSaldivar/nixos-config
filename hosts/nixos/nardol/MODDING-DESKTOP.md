# Portable Wolf modding desktop

The XFCE app is a general graphical maintenance environment, not an Elden Ring
or Nardol installer. The container image contains no host paths, game names,
credentials, mods, or deployment scripts. A Wolf configuration supplies the
storage bindings, and the image remains reusable on another host.

## Storage contract

Wolf persists the app's complete `/home/retro` automatically. That home holds
XFCE preferences, Firefox state, Flatpak applications, editor settings, and
mod-manager configuration. Back it up as user state; do not share it with a
game-runtime container.

Expose durable host storage through these conventional paths:

| Container path | Purpose | Access |
| --- | --- | --- |
| `/home/retro/Games/Steam/steamapps` | Steam's library tree: installed games, Proton prefixes, manifests, and Workshop content | read/write |
| `/home/retro/Games/NonSteam` | Game files installed in XFCE and launched later as Steam non-Steam shortcuts | read/write |
| `/home/retro/Modding` | Archives, manager profiles, staging metadata, logs, and backups | read/write |
| `/home/retro/Downloads` | Convenient alias of the workspace's `downloads` directory | read/write |

This contract deliberately covers Steam games and files launched through
Steam's non-Steam shortcut support. Other stores and launchers are outside its
scope. Host paths remain deployment inputs and can differ on every machine;
the image does not depend on their names or locations.

For Nardol, Steam's `steamapps` directory is mounted at
`Games/Steam/steamapps`. That one tree includes installed games (`common`),
Proton prefixes (`compatdata`), Workshop content, manifests, and Steam's
library metadata. Elden Ring and most Windows-game modding therefore need no
additional runtime-state mount.
Steam also creates Proton prefixes for Windows non-Steam shortcuts below its
`compatdata` tree, while the installed game itself remains in `Games/NonSteam`.

XFCE is responsible for downloading, extracting, organizing, and editing. If a
mod manager is Windows-only, keep its files below `Modding/tools`, add its
executable to Steam as a non-Steam shortcut, and select Proton in Steam's GUI.
The Steam app exposes the same `Games` and `Modding` paths, so the manager can
be configured graphically without duplicating files or exposing Steam's private
home to XFCE.

Do not expose a launcher's complete private home by default. Steam's home also
contains account sessions, client configuration, caches, and userdata that can
be corrupted if two containers write it concurrently. If a native game stores
mods outside its library, add the narrow game-specific state directory as a
separate mount only after identifying it.

## Portable Wolf example

The host-side paths in this example are illustrative and deliberately remain
outside the image:

```toml
[[profiles.apps]]
title = "Modding Desktop"

[profiles.apps.runner]
type = "docker"
name = "WolfModdingDesktop"
image = "ghcr.io/games-on-whales/xfce@sha256:2ce1db7432bcb60caf5b3da23ea0ad5a24f300f3e7f346045fd6ba74a477ebcd"
env = [
  "GOW_REQUIRED_DEVICES=/dev/input/* /dev/dri/* /dev/nvidia*",
  "STEAM_DIR=/home/retro/Games/Steam",
]
mounts = [
  "/host/games/steamapps:/home/retro/Games/Steam/steamapps:rw",
  "/host/games/nonsteam:/home/retro/Games/NonSteam:rw",
  "/host/modding:/home/retro/Modding:rw",
  "/host/modding/downloads:/home/retro/Downloads:rw",
]
```

Pin the image by digest. Declare and back up the host directories separately.
If more GUI tools are ever required, build a generic derivative from a pinned
base with versioned packages and publish it with its SBOM and provenance; do
not install mutable tools interactively or add per-game behavior to the image.

## Concurrency contract

Close the game-runtime session before changing its library from XFCE, and close
XFCE before starting that runtime again. Both views intentionally use the same
underlying files, but no two containers should modify them concurrently.
