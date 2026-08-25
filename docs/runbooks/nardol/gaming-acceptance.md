# Nardol gaming and Wolf two-profile acceptance

Use this runbook after Nardol's installation and first-boot essentials in the
[install runbook](install.md). It preserves the ordered one-time Guest
pre-deployment gate and the complete two-profile deployment acceptance
procedure.

## One-time Guest pre-deployment emptiness gate

This subsection documents the target of the coordinated two-profile change; a
documentation-only revision does not make it deployable. Do not run this gate
or the acceptance procedure until the reviewed Nix/TOML/Python implementation
is present in the same deployment revision. That implementation must add the
`guest` profile and its mounts and directories, make legacy Steam
normalization profile-aware, and replace the current whole-file policy that
permits only one Steam app and one XFCE app with a per-profile policy. Stop if
any of those implementation preconditions is absent: otherwise the current
normalizer can rewrite a Guest Steam app to Edgar's mounts, or the start policy
will reject the second Steam/XFCE pair.

The deployment must also include a reviewed configuration-only migration that
inserts the complete `guest` profile into the existing
`/srv/wolf/data/cfg/config.toml` before the Wolf controller starts. The template
is copied only when `config.toml` is absent, but Nardol already has that file;
adding Guest only to `wolf-config.template.toml` is therefore inert. The
existing reconciler's image and Steam/XFCE line rewrites do not create a
profile. Stop unless the deployed migration handles the existing file and
preserves its pairing, overrides, and Edgar profile. This configuration change
does not move any of Edgar's data.

Run this once, immediately before the deployment that first creates the Guest
profile. Quiesce Wolf first:

```bash
sudo systemctl stop docker-wolf
test "$(sudo systemctl is-active docker-wolf)" = inactive

sudo docker ps -a --no-trunc
test -z "$(sudo docker ps -a --format '{{.Names}}' |
  grep -Ei 'guest|_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || true)"
```

Review the complete `docker ps -a` output as well as the assertion. Stop if any
Guest container or any application container matching the
`*_<lobby-uuid>` session pattern remains; do not assume stopping the controller
removed its children.

Use lstat-style checks that inspect the path object itself. A symlink, any
existing non-directory object, or any entry in an existing directory is a
collision:

```bash
guest_paths=(
  /srv/wolf/data/profile-data/guest
  /srv/games/guest-steamapps
  /srv/games/guest-nonsteam
  /srv/mods-guest
)

for path in "${guest_paths[@]}"; do
  if sudo test -L "$path"; then
    echo "STOP: Guest path is a symlink: $path" >&2
    exit 1
  fi
  if sudo test -e "$path" && ! sudo test -d "$path"; then
    echo "STOP: Guest path is not a directory: $path" >&2
    exit 1
  fi
  if sudo test -d "$path"; then
    if ! first_entry="$(sudo find "$path" -mindepth 1 -maxdepth 1 -print -quit)"; then
      echo "STOP: cannot inspect Guest directory: $path" >&2
      exit 1
    fi
    if test -n "$first_entry"; then
      echo "STOP: Guest path is not empty: $path" >&2
      exit 1
    fi
  fi
done
```

Every object and directory-content probe above runs through `sudo`; an
unprivileged permission denial below `/srv/wolf` must never be mistaken for an
absent or empty path.

Stop for operator review on any collision. Keep Wolf stopped throughout the
deployment and activation; start it with `sudo systemctl start docker-wolf`
only after activation has completed. This is strictly a one-time transition
gate. Never enforce it after Guest has begun using these paths, because their
non-emptiness is then expected and valuable.

Wolf, its PulseAudio fallback, and every default Wolf UI/application image are
digest-pinned and systemd-managed through `docker-wolf.service`; the controller
is no longer a privileged container. Wolf derives both its
configuration/pairing directory (`cfg/`) and per-app homes (`profile-data/`)
from `HOST_APPS_STATE_FOLDER`, so they live at `/srv/wolf/data/cfg` and
`/srv/wolf/data/profile-data`. Docker's own data root is at `/srv/docker`. The
Docker socket inside Wolf is still root-equivalent by design, because Wolf
creates the per-game containers.

The two-profile target is:

| Player | Profile ID | Display name | Steam library | Non-Steam games | Mods | Steam app home |
| --- | --- | --- | --- | --- | --- | --- |
| Edgar | `user` | `Edgar` | `/srv/games/steamapps` | `/srv/games/nonsteam` | `/srv/mods` | `/srv/wolf/data/profile-data/user/WolfSteam` |
| Guest | `guest` | `Guest` | `/srv/games/guest-steamapps` | `/srv/games/guest-nonsteam` | `/srv/mods-guest` | `/srv/wolf/data/profile-data/guest/WolfSteam` |

The existing profile ID `user` deliberately displays as `Edgar`. Do not
"fix" that mismatch: changing the ID would orphan the 4.9G live Steam home.
There is no data migration; Edgar's paths remain untouched. Guest starts with
empty, separate storage. No mount is shared between profiles except the
reviewed read-only NVIDIA allocator bind. This is mount-enforced isolation,
not a naming convention.

Wolf has no profile access control. Any paired client can select either profile
in the Wolf UI, so using the matching Steam account is a convention at the UI
layer even though the underlying storage is genuinely isolated. This is an
accepted household risk and cannot be fixed in Wolf configuration.

Wolf automatically mounts each app's persistent state directory as its complete
`/home/retro`. Native CK3 local mods, Paradox launcher playsets, and similar
state below `/home/retro/.local/share/Paradox Interactive` therefore persist in
that profile's app home under `/srv/wolf/data/profile-data`; they do not need a
second host bind mount. Each profile's explicit `steamapps` mount keeps its own
large games, Proton prefixes, and Workshop content stable independently of a
particular Wolf app title.
The current GoW Steam image uses `/home/retro/.steam/steam` as its active Steam
root; `/home/retro/.steam/debian-installation` is a separate inactive directory
and must not be used as the bind-mount target. The runner also overrides
`STEAM_DIR` to the active root so the pinned toolbox image resolves manifests
and prefixes consistently.

The Wolf `Desktop (xfce)` app is the graphical maintenance environment. Within
each profile, it bind-mounts that player's encrypted persistent data at three
visible home folders: `Games/Steam/steamapps` contains that player's Steam
library tree, `Games/NonSteam` contains game files installed through XFCE and
later added to that player's Steam as non-Steam shortcuts, `Modding` contains
that player's packages, manager state, and backups, and `Downloads` maps
directly to that player's `Modding/downloads`. Both game roots are declarative
mounts rather than paths baked into the desktop image.
The pinned XFCE image includes Firefox, Thunar, Xarchiver, Mousepad, zip, unzip,
and 7-Zip, so downloading, extracting, copying, and editing a mod does not
require SSH or a terminal. The Steam app receives the same per-player `Games`
views and a `Modding` alias, so graphical file-picker paths remain consistent
between apps. Within one profile, close that player's Steam session before
changing game files in XFCE, then close that player's XFCE before starting
Steam again; the two apps share that player's writable library and must not
modify it concurrently. Across profiles there is no writable shared storage,
so no cross-player coordination is required. Each player mods their own
library independently, and co-op partners generally need matching mod sets, so
deploy mods per player.
See [modding-desktop.md](modding-desktop.md) for the portable mount contract
and a host-independent Wolf example.

NixOS exposes NVIDIA's GLVND vendor registration at
`/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json`, outside the
Ubuntu-based Wolf image's default lookup directories. The controller therefore
sets `__EGL_VENDOR_LIBRARY_FILENAMES` to that NVIDIA manifest followed by the
image's Mesa manifest. NVIDIA must come first for the selected DRM node, while
this GLVND build requires both manifests to expose the
`EGL_EXT_device_enumeration` extension used by Wolf's compositor. Without that
ordering and combination, the compositor either selects Mesa/Zink for the
NVIDIA node or cannot enumerate EGL devices, and Moonlight reports a misleading
generic UDP-firewall error even though the advertised Wolf ports are open.

The NVIDIA container runtime provides the driver libraries but not NVRTC,
which is a separate CUDA redistributable required to register GStreamer's
`cudaconvertscale` element. Wolf mounts only Nixpkgs' `cuda_nvrtc` library
output read-only at `/opt/nardol-nvrtc` and adds that path to
`LD_LIBRARY_PATH`. Without it, Wolf silently falls back from NVENC to software
x264/x265 even though `nvidia-smi` succeeds inside the controller.

NixOS's NVIDIA container toolkit injects child-application driver libraries
below `/run/opengl-driver` and `/usr/local/nvidia`, while the GoW Steam image
checks Ubuntu's `/usr/lib/x86_64-linux-gnu` path before generating its NVIDIA
EGL and Vulkan registration files. The Steam runner therefore bind-mounts the
host's `libnvidia-allocator.so.1` at that expected path and selects the
generated NVIDIA GLVND manifest ahead of Mesa. Without this compatibility
mount, Steam's inner Sway compositor selects Mesa/Zink and exits immediately
with `EGL_NOT_INITIALIZED`, which Moonlight presents as a closed session.

Wolf's NVIDIA zero-copy path is explicitly disabled with
`WOLF_USE_ZERO_COPY=FALSE`: at the pinned Wolf revision, that path initializes
NVENC successfully but then fails to allocate its `GsCUDABuf` DMA buffer on
this NixOS/NVIDIA stack, terminating the Moonlight session with a generic DRM
or connection error. This setting selects Wolf's supported CUDA upload/copy
path; it does not disable NVENC or cause the CPU software-encoder fallback.

An `ExecStartPre` policy creates the initial config from the reviewed Wolf v7
template, upgrades the exact known mutable tags in a restored Triforce config,
and refuses to start if any other child image lacks an `@sha256:` digest. New
apps added in Wolf UI therefore need an immutable image reference before the
next start; treat a policy failure as a supply-chain guard, not as a reason to
remove the check.

The game-agnostic custom Steam toolbox under
`hosts/nixos/nardol/steam-tools` is active through its reviewed immutable GHCR
digest. The image adds Protontricks/Winetricks, YAD, archive and installer
utilities, a lightweight terminal maintenance environment, Ludusavi, and
`nardol-modctl`; it contains no games, mods, credentials, Wine/Proton builds, or
drivers. Wolf startup migrates either the prior Steam edge tag or its previously
reviewed upstream digest to this toolbox image without touching pairing or app
state. Do not install tools with `apt` in a running child: Wolf deletes that
container at session end.

Edgar's mod archives belong in `/srv/mods/downloads`, small save backups in
`/srv/mods/backups`, and manager manifests in `/srv/mods/manifests`; Guest uses
the corresponding subdirectories below `/srv/mods-guest`. A manager that
deploys via hardlinks or atomic renames should stage below that player's own
`steamapps/.nardol-mod-staging`: `/srv/games/steamapps/.nardol-mod-staging` for
Edgar or `/srv/games/guest-steamapps/.nardol-mod-staging` for Guest. Staging
there remains within the same bind mount as its per-player target game.

Pair Moonlight again unless the old configuration was sanitised as described in
[section 0 of the install runbook](install.md#0-immutable-hardware-facts-and-stop-conditions).
For Edgar, install Elden Ring once so Steam creates app ID `1245620`
under `/srv/games/steamapps/compatdata`, stop Edgar's Steam child, and restore
the selected `EldenRing` save directory to the matching `AppData/Roaming`
path. Restore as `edgar` or run `sudo chown -R 1000:1000` on the restored
directory, then verify it is writable with
`sudo -u edgar test -w <restored-EldenRing-directory>`. Do not blindly restore
either old multi-gigabyte tree, and do not copy any of Edgar's data to Guest.

Guest signs into their own Steam account in their own session and downloads
their own copies (approximately 272G for the intended set) into the initially
empty `/srv/games/guest-steamapps`. Do not copy manifests and do not perform
`LastOwner` fixups. Steam's default library root
`/home/retro/.steam/steam` is already registered; do not add
`/home/retro/.steam/steam/steamapps` as a library, because `steamapps` is the
library root's child and adding it would be wrong. Each account must own its own
licences. Steam Family Sharing lends a library to only one person at a time and
cuts the borrower off when the owner plays, so it cannot provide simultaneous
use.

## Guest backup and recovery scope

Classify every Guest-bearing location before setting backup policy:

| Location | Classification and recovery rule |
| --- | --- |
| `/srv/wolf/data/profile-data/guest/WolfSteam` | Complete Steam app home. It includes account-local Steam state and is **not replaceable**; back it up. |
| `/srv/wolf/data/profile-data/guest/WolfXFCE` | Complete XFCE app home, including desktop and tool state. It is **not replaceable**; back it up. |
| `/srv/games/guest-steamapps` | Most installed game payloads are redownloadable. `compatdata` and any saves stored elsewhere inside the library are **not replaceable**; select them for backup. |
| `/srv/games/guest-nonsteam` | User-supplied games and files are **not replaceable**; back them up. |
| `/srv/mods-guest` | User-curated archives, manager state, manifests, backups, and mod sets are **not replaceable**; back them up. |
| Steam userdata and Steam Cloud | Validate Cloud support, sync status, and actual coverage separately for every title. Treat uncovered userdata and saves as non-replaceable. |

Do not assume every title stores saves in `compatdata`; native titles and
individual Windows games may use other locations. Record and test the actual
save path for each title before calling its recovery coverage complete.

## Two-profile deployment acceptance

Run the cheapest gate first, before any deployment or runtime work:

```bash
nix flake check --no-build
```

Stop on failure. Then perform the reviewed deployment, including the one-time
emptiness gate above for the initial Guest deployment. After activation, start
Wolf and prove that the configuration reconciler is byte-stable on a second
start:

```bash
sudo systemctl start docker-wolf
sudo systemctl is-active --quiet docker-wolf
first_config_hash="$(sudo sha256sum /srv/wolf/data/cfg/config.toml)"
sudo systemctl restart docker-wolf
sudo systemctl is-active --quiet docker-wolf
second_config_hash="$(sudo sha256sum /srv/wolf/data/cfg/config.toml)"
test "$first_config_hash" = "$second_config_hash"
sudo -u edgar test -w /srv/games/guest-steamapps
sudo -u edgar test -w /srv/games/guest-steamapps/.nardol-mod-staging
sudo -u edgar test -w /srv/games/guest-nonsteam
sudo -u edgar test -w /srv/mods-guest
sudo -u edgar test -w /srv/mods-guest/downloads
sudo -u edgar test -w /srv/mods-guest/backups
```

Next run a no-download two-session smoke test. Confirm the `Guest` profile is
visible in Wolf UI. Use two distinct, already paired Moonlight clients; select
`Edgar` from one and `Guest` from the other. Launch Edgar's existing Steam app
and Guest's `Desktop (xfce)`; optionally open the Firefox installed inside the
Guest desktop. There is no separate Guest Firefox app. Keep both sessions open
for the first container and isolation check below.

Select only application containers whose names end in the
`*_<lobby-uuid>` session pattern, then assert exactly two with distinct lobby
UUIDs:

```bash
mapfile -t application_containers < <(
  sudo docker ps --format '{{.Names}}' |
    grep -E '_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)
test "${#application_containers[@]}" -eq 2
first_lobby_uuid="${application_containers[0]##*_}"
second_lobby_uuid="${application_containers[1]##*_}"
test "$first_lobby_uuid" != "$second_lobby_uuid"
```

The `wolf` controller and audio or other helper containers also run during this
test; do not assert that only two total containers exist. Run the assertion for
both application pairs in this procedure. Inspect every mount on the two
selected application containers in each pair, including Wolf's automatic
app-home binds:

```bash
sudo docker inspect --format \
  '{{.Name}}{{range .Mounts}}{{printf "\n%s\t%s\t%s\t%t" .Type .Name .Source .RW}}{{end}}' \
  "${application_containers[@]}"
```

Canonicalize each bind-mount source and compare mounts only across the two
containers. Stop if the same canonical path, or an ancestor and descendant,
appears across containers and either side is writable. Stop if both containers
use the same named-volume identity. The sole allowlisted cross-container mount
is the canonical
`/run/opengl-driver/lib/libnvidia-allocator.so.1` bind, and it must be read-only
on both sides. Duplicate aliases inside one container are not cross-player
overlap and must not be rejected.

After recording that first pair's inspection, end both application sessions.
Keep the clients paired and distinct, then launch Edgar's `Desktop (xfce)` and
Guest's Steam app, still without downloading anything, and repeat the same
exactly-two-container assertion and mount inspection. This second pair is
mandatory: it proves that Guest Steam has Guest's library and automatic
`WolfSteam` app home, and that Edgar XFCE retains Edgar's mounts. At no point
run Steam and XFCE concurrently within the same profile. Retain both pairs'
`docker inspect` output with the acceptance record; do not approve isolation
from the TOML alone or without inspecting Guest Steam.

Only after that no-download smoke passes, Guest buys and downloads one small
test title into Guest's library. Before the two-session AAA test, write the
title's actual name and its exact, independently verified save location in the
acceptance record. Stop if either is unknown; do not substitute a guessed
`compatdata` path. With Edgar running a known game in the other session, launch
the recorded Guest title, create a recognizable save at the recorded location,
and prove both sessions remain independently usable.

While both games run, use `nvidia-smi` to show two encoder sessions and verify
audio and controller input route independently to the correct Moonlight client.
End one session and confirm the other session, its audio, and its controller
remain alive. Finally restart Wolf and repeat the persistence checks:

```bash
sudo systemctl restart docker-wolf
sudo systemctl is-active --quiet docker-wolf
```

Verify Edgar's Steam login and complete
`/srv/wolf/data/profile-data/user/WolfSteam` app home survived, existing
overrides and Moonlight pairing are untouched, and a final short two-session
test still passes.
