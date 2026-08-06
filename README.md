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
| `minas-tirith` | NixOS server | x86_64-linux | **Migrated 2026-08-06** — NixOS 26.05, pools imported clean, k3s agent `Ready`. Container restore outstanding; **read the runbooks below first** |
| `nardol` | NixOS | x86_64-linux | Active |
| `osgiliath` | NixOS k3s agent | x86_64-linux | Migration configuration staged — **no deployment; read the install runbook first** |
| `pelargir` | NixOS Raspberry Pi 5 | aarch64-linux | Active — direct-NVMe home-automation appliance and k3s server on the tailnet |
| `dol-amroth` | nix-darwin | aarch64-darwin | Active |

Unported (`builder-vm`, `minas-tirith-vm`, `osgiliath-vm`, `pelargir-vm`) are
deliberately **not** wired into `flake.nix`. Their sources remain in `hosts/nixos/`,
and the last state where they evaluated is on the `legacy/24.11` branch. Revive one
at a time rather than dragging broken hosts forward.

`pelargir` boots its Raspberry Pi 5 directly from NVMe through the
`nixos-raspberrypi` framework. It runs the single-server k3s control plane over
Tailscale plus the Home Assistant, Zigbee2MQTT, and Mosquitto workloads. Its
package set comes from the framework's nixpkgs pin rather than this repository's
pin: the board modules are developed and binary-cached against that matched pin,
and the framework's `nixosSystem` wrapper supplies the required overlays, vendor
kernel, firmware, and host platform.

## Everyday commands

```sh
nix fmt                                    # nixfmt-rfc-style
nix flake check --no-build                 # invariants — see "Checks" below

# Evaluate a host without building (works on macOS; see the caveat)
nix eval .#nixosConfigurations.<host>.config.system.build.toplevel.drvPath
nix eval .#darwinConfigurations.dol-amroth.config.system.build.toplevel.drvPath

# Day-to-day apply; nh defaults to ~/Development/nixos-config
nh os switch                               # on the NixOS host
nh darwin switch                           # on dol-amroth
```

`nh clean` remains available for deliberate, interactive cleanup, but its timer
is disabled. The NixOS fleet already has one weekly `nix.gc` schedule with 30-day
retention; enabling nh's timer too would create competing garbage collectors.

### Mac-side aarch64 Linux builder

Dol-amroth runs nix-darwin's persistent `nix.linux-builder`, an `aarch64-linux`
VM registered as a distributed builder with localhost SSH configuration and
substitutes enabled. Bootstrap it in two stages: nix-darwin's pinned option docs
warn that a customized guest is not available from the binary cache and cannot
be built until an uncustomized Linux builder is already running.

1. Temporarily set `bootstrapLinuxBuilder = true` in
   `hosts/darwin/dol-amroth/system.nix` and run
   `sudo darwin-rebuild switch --flake .#dol-amroth`. Use `darwin-rebuild` for
   this first activation because this same activation installs nh. It installs
   the cached upstream guest (1 core, 3 GiB RAM, 20 GiB disk). The committed
   value is `false`; do not commit the temporary bootstrap setting.
2. Verify that guest before relying on it:

```sh
sudo launchctl print system/org.nixos.linux-builder
sudo ssh builder@linux-builder uname -m
nix build nixpkgs#hello --system aarch64-linux --no-link
```

3. Restore `bootstrapLinuxBuilder = false` and run `nh darwin switch` again. The
   running default guest can now build the intended 6-core, 8-GiB-RAM,
   100-GiB-disk closure. Verify it with the same commands. For a fresh Mac or a
   lost builder disk, temporarily restore `true` and repeat both stages.

The VM is intentionally registered only for `aarch64-linux`. The x86_64 NixOS
hosts still build on themselves (for example with `--build-host`/`--target-host`)
rather than hiding slow x86 emulation inside the Mac builder.

## Checks

`nix flake check` enforces three invariants that encode mistakes actually made here:

- **`hostnames`** — every flake output name must equal its `networking.hostName`.
  `dol-amroth` was configured as `dol-amorth` for months without anyone noticing.
- **`minas-tirith-disko-targets`** — disko's destroy list must be **exactly** the one
  Samsung NVMe, and `disko.devices.zpool` must be empty. That host has nine live ZFS
  pool members holding ~98 TB with no backup; this is the invariant that keeps them
  out of disko's blast radius, and it is checked mechanically rather than by eye.
- **`pelargir-disko-targets`** — pelargir's disposable install target is still
  pinned to its serial-qualified NVMe path, and no ZFS pool may be disko-managed.

All are eval-only so they run on macOS. They have been verified to *fail* when they
should, not merely to pass.

## Fleet k3s nodes

[`modules/nixos/fleet/k3s-node.nix`](modules/nixos/fleet/k3s-node.nix) is the
shared `fleet.k3sNode` capability for both servers and agents. It configures k3s
and Tailscale, the cni0 trust boundary, tailnet-only API/kubelet/admin ports, and
the service ordering required by vpn-auth. Host modules supply only their role,
sops-rendered credentials, TLS names, and host-specific admin surface.

Agents such as `minas-tirith` and `osgiliath` import the module and join pelargir
through MagicDNS:

```nix
{ config, ... }:
{
  imports = [ ../../../modules/nixos/fleet/k3s-node.nix ];

  fleet.k3sNode = {
    enable = true;
    role = "agent";
    serverAddr = "https://pelargir:6443";
    tokenFile = config.sops.secrets.k3s_agent_token.path;
    vpnAuthFile = config.sops.templates."k3s-vpn-auth".path;
  };
}
```

The agent token and vpn-auth template must remain sops-backed; never put either
credential in the Nix store.

## minas-tirith — read before touching disks

That host is a remote server with nine live ZFS pool members. Start here:

- [`hosts/nixos/minas-tirith/disko.nix`](hosts/nixos/minas-tirith/disko.nix) — the only
  file that can destroy the pools. Read the header.
- [`INSTALL-RUNBOOK.md`](hosts/nixos/minas-tirith/INSTALL-RUNBOOK.md) — phased
  openSUSE → NixOS migration, including the fail-closed HBA-removal gate.
- [`RESTORE-RUNBOOK.md`](hosts/nixos/minas-tirith/RESTORE-RUNBOOK.md) — bringing 39
  containers and ~298 GB of service data back.

## What's next

Deferred work — CI, extracting the embedded shell scripts so their tests are
permanent, and deploy-rs for rollback-protected remote deploys — is written up
with reasoning in [`ROADMAP.md`](ROADMAP.md).

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

## Raspberry Pi images and Linux building

The configured Mac-side Linux builder can build pelargir closures without a
Docker/qemu workaround. Because the builder is registered with the Nix daemon,
run the build normally from dol-amroth:

```sh
sudo nix build .#nixosConfigurations.pelargir.config.system.build.diskoImagesScript \
  --impure --system aarch64-linux
```

The derivations execute inside the persistent `aarch64-linux` VM; macOS remains
the evaluator and client. The builder store survives restarts so repeat fleet
builds reuse their closures.
