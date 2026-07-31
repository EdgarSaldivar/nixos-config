# nixos-config

Nix flake managing Edgar's machines: NixOS hosts plus one nix-darwin Mac.

> **This repository is PUBLIC.** Secrets live in sops-encrypted files
> (`secrets/*.yaml`) and are never committed in plaintext. MAC addresses, RFC1918
> addresses and disk serials *are* committed deliberately — the config cannot match
> hardware without them, and none is usable from outside the LAN.

## Hosts

Only these are exported. Anything else under `hosts/` is unported legacy.

| Host | Kind | System | Status |
|---|---|---|---|
| `minas-tirith` | NixOS server | x86_64-linux | Migration from openSUSE in progress — **read the runbooks below first** |
| `nardol` | NixOS | x86_64-linux | Active |
| `dol-amroth` | nix-darwin | aarch64-darwin | Active |

Unported (`builder-vm`, `minas-tirith-vm`, `osgiliath[-vm]`, `pelargir[-vm]`) are
deliberately **not** wired into `flake.nix`. Their sources remain in `hosts/nixos/`,
and the last state where they evaluated is on the `legacy/24.11` branch. Revive one
at a time rather than dragging broken hosts forward.

## Everyday commands

```sh
nix fmt                                    # nixfmt-rfc-style
nix flake check --no-build                 # invariants — see "Checks" below

# Evaluate a host without building (works on macOS; see the caveat)
nix eval .#nixosConfigurations.<host>.config.system.build.toplevel.drvPath
nix eval .#darwinConfigurations.dol-amroth.config.system.build.toplevel.drvPath

# Apply, on the machine itself
sudo nixos-rebuild switch --flake .#<host>
darwin-rebuild switch --flake .#dol-amroth
```

### ⚠️ This Mac cannot build Linux derivations

`aarch64-darwin` with no `linux-builder` and no remote builders — `extra-platforms`
lists only `x86_64-darwin`. Evaluation works; **building does not**:

```
error: a 'x86_64-linux' ... is required to build, but I am a 'aarch64-darwin'
```

So anything that builds a NixOS closure from here must build on the target
(`--build-on remote`), or you need a Linux builder. This is why the install runbook
passes that flag explicitly rather than relying on `--build-on auto`.

## Checks

`nix flake check` enforces two invariants that encode mistakes actually made here:

- **`hostnames`** — every flake output name must equal its `networking.hostName`.
  `dol-amroth` was configured as `dol-amorth` for months without anyone noticing.
- **`minas-tirith-disko-targets`** — disko's destroy list must be **exactly** the one
  Samsung NVMe, and `disko.devices.zpool` must be empty. That host has nine live ZFS
  pool members holding ~98 TB with no backup; this is the invariant that keeps them
  out of disko's blast radius, and it is checked mechanically rather than by eye.

Both are eval-only so they run on macOS. Both have been verified to *fail* when they
should, not merely to pass.

## minas-tirith — read before touching disks

That host is a remote server with nine live ZFS pool members. Start here:

- [`hosts/nixos/minas-tirith/disko.nix`](hosts/nixos/minas-tirith/disko.nix) — the only
  file that can destroy the pools. Read the header.
- [`INSTALL-RUNBOOK.md`](hosts/nixos/minas-tirith/INSTALL-RUNBOOK.md) — phased
  openSUSE → NixOS migration, including the fail-closed HBA-removal gate.
- [`RESTORE-RUNBOOK.md`](hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md) — bringing 39
  containers and ~298 GB of service data back.

## What's next

Deferred work — CI, sharing a base module across hosts, extracting the embedded
shell scripts so their tests are permanent, and deploy-rs for rollback-protected
remote deploys — is written up with reasoning in [`ROADMAP.md`](ROADMAP.md).

## Secrets

sops + age. Host keys are derived from each machine's SSH ed25519 host key
(`ssh-to-age`), so nothing extra needs provisioning onto a box.

The admin identity is derived from `~/.ssh/id_ed25519` — **not** from any standalone
age key. To edit:

```sh
ssh-to-age -private-key -i ~/.ssh/id_ed25519 -o /tmp/age.key
SOPS_AGE_KEY_FILE=/tmp/age.key sops secrets/minas-tirith.yaml
shred -u /tmp/age.key
```

Recipients live in [`.sops.yaml`](.sops.yaml). After adding one, run
`sops updatekeys <file>`.

## Legacy: Raspberry Pi images and cross-building

Kept because it is hard-won and still applies when `pelargir`/`osgiliath` are revived.

```sh
sudo nix build .#nixosConfigurations.pelargir.config.system.build.diskoImagesScript \
  --impure --system aarch64-linux
```

Must be built on x86_64-linux or aarch64-linux, or via a remote builder. binfmt
cross-compilation is Linux-kernel-only, so there is no direct-on-darwin path. One way
to get a builder on macOS via Docker:

```sh
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
docker run --rm -it -v "$(pwd)":/mnt -w /mnt nixos/nix:latest bash
# then, in /etc/nix/nix.conf on the host:
#   builders = ssh://username@container-ip
```
