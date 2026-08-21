# Bootstrapping the aarch64-linux builder on dol-amroth

⛔ **Recovered 2026-08-21.** This procedure was deleted in the 2026-08 docs
consolidation with no replacement, while `bootstrapLinuxBuilder` remained a live
option in `hosts/darwin/dol-amroth/system.nix`. Under this repository's own rule that
runbooks own actions, that left a procedure with no home — and this one is needed
exactly when the Mac cannot build anything, which is the worst moment to reconstruct
it from git history.

## The chicken-and-egg problem

The Mac drives remote fleet builds through a `aarch64-linux` guest VM. The
**customized** guest (6 cores, 8 GiB RAM, 100 GiB disk) is not in any binary cache,
so it has to be built — and building it requires an `aarch64-linux` builder, which is
the thing being built.

`bootstrapLinuxBuilder` breaks the cycle by starting from the **uncustomized**
upstream guest (1 core, 3 GiB, 20 GiB), which *is* cached.

## Two stages, and both are required

**Stage 1 — start the cached guest.**

Set `bootstrapLinuxBuilder = true` in `hosts/darwin/dol-amroth/system.nix`, then:

```sh
sudo darwin-rebuild switch --flake .#dol-amroth
```

⚠️ `darwin-rebuild`, not `nh`, for this first activation — this same activation is
what installs `nh`.

⛔ The committed value is `false`. **Do not commit the temporary `true`.**

**Verify the guest before relying on it**, rather than assuming activation implies a
working builder:

```sh
sudo launchctl print system/org.nixos.linux-builder
sudo ssh builder@linux-builder uname -m
nix build nixpkgs#hello --system aarch64-linux --no-link
```

**Stage 2 — build the real guest.**

Restore `bootstrapLinuxBuilder = false` and:

```sh
nh darwin switch
```

The running default guest can now build the intended 6-core, 8-GiB, 100-GiB closure.
Verify with the same three commands.

## When you need this again

A fresh Mac, or a lost builder disk. Temporarily restore `true` and repeat **both**
stages — stopping after stage 1 leaves you on the 1-core guest, which works and is
slow enough to look like something else is wrong.

## Scope

The VM is registered only for `aarch64-linux`, deliberately. The x86_64 NixOS hosts
build on themselves (`--build-host`/`--target-host`) rather than hiding slow x86
emulation inside the Mac builder.
