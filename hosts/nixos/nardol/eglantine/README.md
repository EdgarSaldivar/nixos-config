# Eglantine on Nardol Wolf

Eglantine is an optional, dedicated Wolf app for Edgar. The option defaults to
off. It does not add Eglantine to Guest or change the existing Steam and desktop
app identities. Wolf supplies the persistent `/home/retro` mount under its existing
`/srv/wolf/data/profile-data/user/WolfEglantine` state (UID/GID 1000); `/srv/games/switch` is mounted read-only. Keep keys, firmware, games,
saves, and personal settings out of the image. Add those to the host storage
only through the normal owner-managed process. The image stores no registry
credential. Eglantine's built-in manual update check remains present.

## Release and build

The image uses the [regular nx2 build 121 Linux x86_64
AppImage](https://github.com/Djihads80/Eglantine/releases/tag/build-121),
`eglantine-9c55e70d790a4cc9507d80dbb9a9d6224175574e-linux-x86_64.AppImage`. Its downloaded SHA256 is
`ed00dd2205606cabdf74bd60b6f91a6a0a2eeb6b0594b75cbae0ed1fbec026e3`
(also the GitHub release asset digest). This is the regular binary, not PGO or
the experimental NVIDIA cooperative matrix path. The pinned GoW `base-app`
multi-architecture digest is
`sha256:8a9f6d58c5c33c347e2f30865d0b2be24d093e6aa36e06e56d47402dbc7deb49`;
it resolves to linux/amd64 manifest
`sha256:994a9ceaf97511ca06bdf334a6617f270c391ddd1e03def99fe52e0dd874347e`.
The AppImage is verified and unpacked with `unsquashfs` at build time. Its FUSE
runtime is not used. Eglantine requires glibc 2.39 or newer, so confirm that
the base image still meets this on every pin update.

Build and smoke test locally (or run the manual-only
`nardol-eglantine.yml` workflow):

```sh
docker build --platform linux/amd64 \
  -f hosts/nixos/nardol/eglantine/Containerfile \
  -t nardol-eglantine:review hosts/nixos/nardol/eglantine
docker run --rm --platform linux/amd64 --network none --read-only \
  --user 1000:1000 \
  --entrypoint /usr/local/libexec/nardol-eglantine-smoke-test \
  nardol-eglantine:review
```

The build preserves GoW's `/entrypoint.sh` and `/opt/gow/startup.sh`: root
cont-init prepares the Wolf session, then `gosu` runs the app as UID 1000.
The image replaces only `/opt/gow/startup-app.sh` in GoW and adds a wrapper
that supplies the extracted AppImage library and plugin paths. That script starts the
extracted AppImage through GoW's Sway launcher, using XWayland (`xcb`), the platform plugin bundled in this release. Wolf supplies its existing controller, PulseAudio,
NVIDIA device, and render-node integration. Startup writes
`check_for_updates=false` in Eglantine's persistent Qt configuration.

## Configuration and verification

Only after the image has been built, reviewed, and published by an authorized
operator, set these Nix options with the **real** immutable registry digest:

```nix
nardol.eglantine = {
  enable = true;
  image = "<registry>/<image>@sha256:<published-digest>";
};
```

The option rejects missing or non-digest image references. It adds one
reviewed app to Edgar's Wolf profile. The reconciler adds that app to an
existing config while preserving UUID, paired clients, comments, and existing
video/audio overrides. It refuses an unreviewed image, mount, environment,
profile placement, or guest copy. If disabling after it has been enabled,
remove the reviewed Eglantine entry from the live Wolf config before the next
Wolf start; the disabled policy deliberately rejects a lingering app rather
than silently deleting operator state. Preserve the persistent home separately.

The controller must run full `nix flake check`, closure equivalence, host evaluation,
and the Docker build. For local preflight, `python -m pytest
hosts/nixos/nardol/tests/test_wolf_reconcile.py` and `bash -n` cover the
available preflight. A real GPU/Moonlight session test remains necessary before
deployment, including Vulkan rendering, controller input, audio, XWayland windows, and access to `/srv/games/switch` without write permission.
The manual workflow publishes the tested image only when its `publish` input
is selected. It does not change a live host.
