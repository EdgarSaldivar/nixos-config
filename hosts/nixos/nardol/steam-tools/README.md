# Nardol Steam tools image

This is a thin, game-agnostic maintenance layer on the exact Games-on-Whales
Steam image selected by `../wolf.nix`. It adds tools that would otherwise vanish
when Wolf deletes a child container. It does not contain games, mods, Steam or
Nexus credentials, Wine/Proton builds, NVIDIA drivers, or a game-specific mod
manager.

Wolf selects the published image only through the immutable digest recorded in
`../wolf.nix`; the full-commit tag is retained solely as its reviewable build
identity. Never deploy a mutable custom tag.

## Included capabilities

- Protontricks 1.14.1 and Winetricks 20260125, with immutable downloads checked
  before installation.
- YAD for Protontricks dialogs. The upstream `/usr/bin/zenity -> /usr/bin/true`
  Steam-startup workaround remains unchanged.
- Ludusavi 0.31.0 for targeted save backups.
- Current 7-Zip 26.02 plus installer, archive, and diagnostic utilities; the
  inherited Kitty terminal and Nano editor remain available for maintenance.
- `nardol-modctl`, whose app IDs are runtime arguments rather than image policy.

Run `nardol-modctl help` inside the Wolf Steam session. Steam's "Add a Non-Steam
Game" dialog can also discover the included **Nardol Mod Tools** desktop entry.
Use `nardol-modctl gui APPID` for a game-specific Winetricks GUI; requiring the
app ID lets the wrapper refuse prefix access while that game is running.

## Persistence

In the two-profile target, Wolf mounts each profile's full app home at
`/home/retro`, that player's Steam library at
`/home/retro/.steam/steam/steamapps`, and that player's mod tree at
`/home/retro/Mods`. Tool configuration, Proton prefixes, Workshop content,
downloads, manifests, and backups therefore survive child-container deletion.

The coordinated Nardol two-profile target has this profile-to-host-path map;
it does not change this toolbox image, its digest pins, or its staging
contract:

| Player | Profile ID | Display name | Steam library | Non-Steam games | Mods | Steam app home |
| --- | --- | --- | --- | --- | --- | --- |
| Edgar | `user` | `Edgar` | `/srv/games/steamapps` | `/srv/games/nonsteam` | `/srv/mods` | `/srv/wolf/data/profile-data/user/WolfSteam` |
| Guest | `guest` | `Guest` | `/srv/games/guest-steamapps` | `/srv/games/guest-nonsteam` | `/srv/mods-guest` | `/srv/wolf/data/profile-data/guest/WolfSteam` |

The existing `user` ID deliberately displays as `Edgar`. Do not rename that
ID: doing so would orphan the 4.9G live Steam home. The two profiles share no
writable mount; the only shared mount is the reviewed read-only NVIDIA
allocator bind. Isolation is enforced by mounts, not by naming convention.
Edgar's existing paths remain untouched, with no data migration.

Use `steamapps/.nardol-mod-staging` inside each player's own library for a
manager that deploys with hardlinks or atomic renames. Keeping staging and the
target within that same per-player bind mount avoids cross-mount failures.
`/home/retro/Mods/downloads` remains the generic archive and installer inbox.

## Local build and test

From the repository root:

```console
docker buildx build \
  --platform linux/amd64 \
  --load \
  --file hosts/nixos/nardol/steam-tools/Containerfile \
  --tag nardol-steam-tools:test \
  hosts/nixos/nardol/steam-tools

docker run --rm --platform linux/amd64 \
  --user 1000:1000 \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,exec,mode=1777,size=16m \
  --entrypoint /usr/local/libexec/nardol-steam-tools-smoke-test \
  nardol-steam-tools:test
```

The build itself runs the same smoke test. CI repeats it as Wolf's UID/GID 1000
with no network and a read-only root filesystem. It checks tool versions,
preserves the Zenity shim, resolves a fixture Steam manifest, checks wrapper
argument construction, verifies the full Debian package-version manifest, and
verifies that prefix mutation is rejected while a process carries the matching
`SteamAppId`.

## Inherited base risk

The reviewed upstream GoW digest currently reports Ubuntu 25.04, which reached
[end of life on January 15, 2026](https://wiki.ubuntu.com/Releases). This thin
layer cannot repair unsupported base userspace without replacing and
revalidating the GoW Steam stack. The release workflow scans both images with
the same Grype database and refuses any medium-or-higher match introduced by
the toolbox, but inherited findings remain inherited risk. Move to a maintained
GoW base as soon as one is available and repeat the full Moonlight/GPU test.

## Published deployment

The first reviewed release was built from commit
`214fce8091fc0524d64996a3b225ee3a98251c36` and is deployed only as:

```text
ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8
```

The `Nardol Steam tools image` workflow is manual-only. Its read-only job builds,
tests, scans, and records the image ID, rootfs layers, size, and SBOM without
registry credentials. A separate publish job rehydrates the same commit from a
run-scoped cache and refuses any identity drift before authenticating to GHCR.
It then refuses to replace an existing full-commit tag, checks that the registry
manifest points at the tested config digest, attaches signed SBOM/provenance
attestations, and reports the immutable digest. All packages added to the pinned
base, including transitive dependencies, are version-pinned and the complete
installed Debian package manifest has a checked hash. Running that workflow is
a package release and requires explicit production authority.

For a future update:

1. Run the manual release workflow from a reviewed commit.
2. Copy its reported `ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:...`
   reference; do not deploy the `git-...` tag.
3. Update the full-commit tag, digest, and explicit prior-digest migration in
   `../wolf.nix`, then update the Steam template key in
   `../wolf-config.template.toml`.
4. Run `nix flake check --no-build`, deploy, and repeat the Moonlight, game,
   Protontricks-GUI, prefix-persistence, and child-recreation smoke tests.

Updating any upstream tool means bumping its version and checksum, rebuilding,
reviewing the SBOM, publishing a new commit tag, and recording a new final
digest. No credentials belong in build arguments, layers, or this directory.
